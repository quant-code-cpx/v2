"""互联互通中心完整包发布仓储。

仓储只从已发布的 canonical 通道统计与活跃榜 release 读取，再在同一事务中写入官方日历观察、
状态 revision 和完整包当前指针。任何组件失败时旧完整包继续可见，不会暴露半套新数据。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from itertools import combinations
from uuid import UUID, uuid4

from sqlalchemy import insert, select, update
from sqlalchemy.orm import Session

from service_data_sync.application.ports.stock_connect import PublishedStockConnectBundle
from service_data_sync.domain.stock_connect import (
    StockConnectCalendarDay,
    StockConnectChannel,
    StockConnectChannelStatus,
)
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.database.fenced_execution import (
    current_fenced_execution,
)
from service_data_sync.infrastructure.database.models.canonical.release import (
    CanonicalRecordLineage,
    DatasetRelease,
)
from service_data_sync.infrastructure.database.models.market import (
    StockConnectActiveSecurityRevision,
    StockConnectBundlePublication,
    StockConnectCalendarObservation,
    StockConnectChannelDailyRevision,
    StockConnectChannelStatusRevision,
    StockConnectOverviewGeneration,
    StockConnectOverviewGenerationComponent,
    StockConnectOverviewPublication,
)
from service_data_sync.infrastructure.database.models.operations import (
    DataOperationPartition,
    DataOperationRun,
)
from service_data_sync.infrastructure.database.models.publication.dataset_publication import (
    DatasetPublication,
)

_BUY_MINUS_SELL_METHODOLOGY = "buy-minus-sell-v1"
_CHANNEL_CODES = {
    "SH_NORTHBOUND",
    "SH_SOUTHBOUND",
    "SZ_NORTHBOUND",
    "SZ_SOUTHBOUND",
}


class SqlAlchemyStockConnectCenterRepository:
    """把已发布组件组装为内部 API 唯一允许读取的不可变完整包。"""

    def __init__(self, database: DatabaseClient) -> None:
        """保存事务工厂，所有可见性切换都在数据库事务中完成。"""
        self._database = database

    def publish_bundle(
        self,
        *,
        channel: StockConnectChannel,
        overview_generation_id: UUID,
        overview_channels: Sequence[str],
        market_data_version: UUID,
        active_data_version: UUID | None,
        calendar: StockConnectCalendarDay,
        calendar_source_ref: Mapping[str, object],
        calendar_observed_at: datetime,
        status: StockConnectChannelStatus,
        quality_issues: Sequence[Mapping[str, str]],
        source_refs: Sequence[Mapping[str, object]],
    ) -> PublishedStockConnectBundle:
        """发布一个真实交易日完整包；统计、状态、日历或活跃榜依赖不一致即回滚。"""
        with self._database.transaction() as session:
            now = datetime.now(UTC)
            generation = _ensure_overview_generation(
                session,
                generation_id=overview_generation_id,
                trade_date=calendar.calendar_date,
                expected_channels=overview_channels,
                now=now,
            )
            if generation.completed_at is not None:
                return _completed_generation_bundle(
                    session,
                    generation=generation,
                    channel_code=_channel_code(channel),
                )
            market_release_id = _release_id(session, market_data_version)
            active_release_id = (
                None if active_data_version is None else _release_id(session, active_data_version)
            )
            market = _market_row(
                session,
                channel=channel,
                trade_date=calendar.calendar_date,
                release_id=market_release_id,
            )
            active = _active_rows(
                session,
                channel=channel,
                trade_date=calendar.calendar_date,
                release_id=active_release_id,
            )
            if market.turnover_amount is not None and market.turnover_amount > 0 and not active:
                raise ValueError("non-zero stock-connect turnover requires an official active list")
            calendar_observation_id = _calendar_observation(
                session,
                calendar=calendar,
                source_ref=calendar_source_ref,
                observed_at=calendar_observed_at,
            )
            status_revision_id = _status_revision(session, status=status, now=now)
            issues = [dict(item) for item in quality_issues]
            issues.extend(_identity_issues(active))
            issues = _deduplicate_issues(issues)
            if len(issues) > 20:
                raise ValueError("stock-connect bundle has too many quality issues")
            summary = _summary(channel=channel, market=market, active=active, status=status)
            active_payload = [_active_item(channel=channel, row=row) for row in active]
            refs = _deduplicate_source_refs(source_refs)
            if not 1 <= len(refs) <= 12:
                raise ValueError("stock-connect bundle source reference count is invalid")
            payload = {
                "tradeDate": calendar.calendar_date.isoformat(),
                "channel": _channel_code(channel),
                "marketReleaseId": str(market_release_id),
                "activeReleaseId": (None if active_release_id is None else str(active_release_id)),
                "statusRevisionId": str(status_revision_id),
                "calendarObservationId": str(calendar_observation_id),
                "summary": summary,
                "activeSecurities": active_payload,
                "qualityIssues": issues,
                "sourceRefs": refs,
            }
            content_hash = _hash_payload(payload)
            current = session.execute(
                select(StockConnectBundlePublication)
                .where(
                    StockConnectBundlePublication.trade_date == calendar.calendar_date,
                    StockConnectBundlePublication.channel == channel.channel,
                    StockConnectBundlePublication.direction == channel.direction,
                    StockConnectBundlePublication.superseded_at.is_(None),
                )
                .with_for_update()
            ).scalar_one_or_none()
            if current is not None and current.content_hash == content_hash:
                _stage_overview_component(
                    session,
                    generation=generation,
                    channel_code=_channel_code(channel),
                    bundle=current,
                    trade_date=calendar.calendar_date,
                    now=now,
                )
                _record_bundle_progress(
                    session=session,
                    channel=channel,
                    trade_date=calendar.calendar_date,
                    active_count=len(active),
                )
                return PublishedStockConnectBundle(
                    bundle_release_id=UUID(str(current.bundle_release_id)),
                    data_version=current.data_version,
                    reused=True,
                )
            if current is not None:
                session.execute(
                    update(StockConnectBundlePublication)
                    .where(
                        StockConnectBundlePublication.bundle_release_id == current.bundle_release_id
                    )
                    .values(superseded_at=now)
                )
            bundle_release_id = uuid4()
            data_version = str(uuid4())
            session.execute(
                insert(StockConnectBundlePublication).values(
                    bundle_release_id=bundle_release_id,
                    data_version=data_version,
                    trade_date=calendar.calendar_date,
                    channel=channel.channel,
                    direction=channel.direction,
                    market_release_id=market_release_id,
                    active_release_id=active_release_id,
                    status_revision_id=status_revision_id,
                    calendar_observation_id=calendar_observation_id,
                    summary_json=summary,
                    active_securities_json=active_payload,
                    quality_status=("APPROVED_WITH_WARNINGS" if issues else "APPROVED"),
                    quality_issues=issues,
                    source_refs=refs,
                    content_hash=content_hash,
                    published_at=now,
                    superseded_at=None,
                    active_security_count=len(active_payload),
                )
            )
            created = session.get(StockConnectBundlePublication, bundle_release_id)
            if created is None:
                raise AssertionError(
                    "stock-connect bundle insert was not visible in its transaction"
                )
            _stage_overview_component(
                session,
                generation=generation,
                channel_code=_channel_code(channel),
                bundle=created,
                trade_date=calendar.calendar_date,
                now=now,
            )
            _record_bundle_progress(
                session=session,
                channel=channel,
                trade_date=calendar.calendar_date,
                active_count=len(active),
            )
            return PublishedStockConnectBundle(
                bundle_release_id=bundle_release_id,
                data_version=data_version,
                reused=False,
            )


def _ensure_overview_generation(
    session: Session,
    *,
    generation_id: UUID,
    trade_date: date,
    expected_channels: Sequence[str],
    now: datetime,
) -> StockConnectOverviewGeneration:
    """锁定或创建精确目标 header，重试不得改变同一 run 的通道集合。"""
    normalized = tuple(sorted(set(expected_channels)))
    if (
        not normalized
        or len(normalized) != len(expected_channels)
        or not set(normalized) <= _CHANNEL_CODES
    ):
        raise ValueError("stock-connect overview generation channels are invalid")
    channel_set = ",".join(normalized)
    current = session.execute(
        select(StockConnectOverviewGeneration)
        .where(
            StockConnectOverviewGeneration.generation_id == generation_id,
            StockConnectOverviewGeneration.trade_date == trade_date,
        )
        .with_for_update()
    ).scalar_one_or_none()
    if current is None:
        current = StockConnectOverviewGeneration(
            generation_id=generation_id,
            trade_date=trade_date,
            channel_set=channel_set,
            expected_channel_count=len(normalized),
            created_at=now,
            completed_at=None,
        )
        session.add(current)
        session.flush()
        return current
    if current.channel_set != channel_set or current.expected_channel_count != len(normalized):
        raise ValueError("stock-connect overview generation target set drifted")
    return current


def _completed_generation_bundle(
    session: Session,
    *,
    generation: StockConnectOverviewGeneration,
    channel_code: str,
) -> PublishedStockConnectBundle:
    """完成后的 run 只返回首次 staging 的 bundle，绝不倒退后来 publication。"""
    if channel_code not in generation.channel_set.split(","):
        raise ValueError("stock-connect channel is outside the completed generation")
    component = session.execute(
        select(StockConnectOverviewGenerationComponent).where(
            StockConnectOverviewGenerationComponent.generation_id == generation.generation_id,
            StockConnectOverviewGenerationComponent.trade_date == generation.trade_date,
            StockConnectOverviewGenerationComponent.channel_code == channel_code,
        )
    ).scalar_one_or_none()
    if component is None:
        raise ValueError("completed stock-connect generation has no channel component")
    bundle = session.get(StockConnectBundlePublication, component.bundle_release_id)
    if bundle is None:
        raise ValueError("completed stock-connect generation bundle is unavailable")
    return PublishedStockConnectBundle(
        bundle_release_id=UUID(str(bundle.bundle_release_id)),
        data_version=bundle.data_version,
        reused=True,
    )


def _stage_overview_component(
    session: Session,
    *,
    generation: StockConnectOverviewGeneration,
    channel_code: str,
    bundle: StockConnectBundlePublication,
    trade_date: date,
    now: datetime,
) -> None:
    """登记本 run 的通道 bundle；只有精确目标集合齐备才推进其全部可查询子集。"""
    expected = set(generation.channel_set.split(","))
    if (
        channel_code not in expected
        or bundle.trade_date != trade_date
        or f"{bundle.channel}_{bundle.direction}" != channel_code
    ):
        raise ValueError("stock-connect staged bundle does not match generation target")
    staged = session.execute(
        select(StockConnectOverviewGenerationComponent).where(
            StockConnectOverviewGenerationComponent.generation_id == generation.generation_id,
            StockConnectOverviewGenerationComponent.trade_date == trade_date,
            StockConnectOverviewGenerationComponent.channel_code == channel_code,
        )
    ).scalar_one_or_none()
    if staged is None:
        session.add(
            StockConnectOverviewGenerationComponent(
                generation_id=generation.generation_id,
                trade_date=trade_date,
                channel_code=channel_code,
                bundle_release_id=bundle.bundle_release_id,
                staged_at=now,
            )
        )
        session.flush()
    elif staged.bundle_release_id != bundle.bundle_release_id:
        raise ValueError("stock-connect overview generation component changed during retry")
    components = (
        session.execute(
            select(StockConnectOverviewGenerationComponent).where(
                StockConnectOverviewGenerationComponent.generation_id == generation.generation_id,
                StockConnectOverviewGenerationComponent.trade_date == trade_date,
            )
        )
        .scalars()
        .all()
    )
    actual = {item.channel_code for item in components}
    if not actual <= expected:
        raise ValueError("stock-connect overview generation contains an unexpected channel")
    if actual != expected:
        return
    rows = (
        session.execute(
            select(StockConnectBundlePublication)
            .where(
                StockConnectBundlePublication.bundle_release_id.in_(
                    [item.bundle_release_id for item in components]
                )
            )
            .with_for_update()
        )
        .scalars()
        .all()
    )
    by_channel = {f"{row.channel}_{row.direction}": row for row in rows}
    if set(by_channel) != expected or any(
        row.trade_date != trade_date or row.superseded_at is not None for row in rows
    ):
        raise ValueError("stock-connect generation components are no longer current")
    _publish_overview_subsets(
        session,
        trade_date=trade_date,
        by_channel=by_channel,
        now=now,
    )
    generation.completed_at = now


def _publish_overview_subsets(
    session: Session,
    *,
    trade_date: date,
    by_channel: Mapping[str, StockConnectBundlePublication],
    now: datetime,
) -> None:
    """从同一完整 generation 原子发布全部非空子集，禁止混入其他运行的当前行。"""
    available = sorted(by_channel)
    for size in range(1, len(available) + 1):
        for subset in combinations(available, size):
            selected = [by_channel[channel] for channel in subset]
            channel_set = ",".join(subset)
            issues = _deduplicate_issues(
                [issue for row in selected for issue in row.quality_issues]
            )
            refs = _deduplicate_source_refs([ref for row in selected for ref in row.source_refs])
            if len(refs) > 12:
                raise ValueError("stock-connect overview has too many source references")
            component_ids = {
                channel: str(by_channel[channel].bundle_release_id) for channel in subset
            }
            payload: dict[str, object] = {
                "components": {
                    channel: {
                        "bundleReleaseId": component_ids[channel],
                        "dataVersion": by_channel[channel].data_version,
                    }
                    for channel in subset
                },
                "qualityIssues": issues,
                "sourceRefs": refs,
            }
            content_hash = _hash_payload(payload)
            current = session.execute(
                select(StockConnectOverviewPublication)
                .where(
                    StockConnectOverviewPublication.trade_date == trade_date,
                    StockConnectOverviewPublication.channel_set == channel_set,
                    StockConnectOverviewPublication.superseded_at.is_(None),
                )
                .with_for_update()
            ).scalar_one_or_none()
            if current is not None and current.content_hash == content_hash:
                continue
            if current is not None:
                session.execute(
                    update(StockConnectOverviewPublication)
                    .where(
                        StockConnectOverviewPublication.overview_release_id
                        == current.overview_release_id
                    )
                    .values(superseded_at=now)
                )
            session.execute(
                insert(StockConnectOverviewPublication).values(
                    overview_release_id=uuid4(),
                    data_version=str(uuid4()),
                    trade_date=trade_date,
                    channel_set=channel_set,
                    component_bundle_ids=component_ids,
                    quality_status=("APPROVED_WITH_WARNINGS" if issues else "APPROVED"),
                    quality_issues=issues,
                    source_refs=refs,
                    content_hash=content_hash,
                    published_at=now,
                    superseded_at=None,
                )
            )


def _release_id(session: Session, data_version: UUID) -> UUID:
    """把 canonical publication 版本解析为不可变 release，拒绝不存在或已被篡改的版本。"""
    value = session.execute(
        select(DatasetPublication.release_id).where(DatasetPublication.data_version == data_version)
    ).scalar_one_or_none()
    if value is None:
        raise ValueError("stock-connect component publication is unavailable")
    return UUID(str(value))


def _market_row(
    session: Session,
    *,
    channel: StockConnectChannel,
    trade_date: date,
    release_id: UUID,
) -> StockConnectChannelDailyRevision:
    """按 release 冻结时刻和逐记录血缘读取唯一通道日统计。"""
    release, lineage = _release_snapshot(session, release_id=release_id)
    record_key = _stock_connect_record_key(channel=channel, trade_date=trade_date)
    candidates = (
        session.execute(
            select(StockConnectChannelDailyRevision).where(
                StockConnectChannelDailyRevision.trade_date == trade_date,
                StockConnectChannelDailyRevision.channel == channel.channel,
                StockConnectChannelDailyRevision.direction == channel.direction,
                StockConnectChannelDailyRevision.methodology_version_id
                == release.methodology_version_id,
                StockConnectChannelDailyRevision.known_from <= release.created_at,
                (StockConnectChannelDailyRevision.known_to.is_(None))
                | (StockConnectChannelDailyRevision.known_to > release.created_at),
            )
        )
        .scalars()
        .all()
    )
    rows = [row for row in candidates if (record_key, UUID(str(row.source_batch_id))) in lineage]
    if len(rows) != 1:
        raise ValueError("stock-connect market release does not contain exactly one requested day")
    return rows[0]


def _active_rows(
    session: Session,
    *,
    channel: StockConnectChannel,
    trade_date: date,
    release_id: UUID | None,
) -> list[StockConnectActiveSecurityRevision]:
    """按 release 冻结时刻和逐记录血缘读取当天名次；空 release 表达合法空榜。"""
    if release_id is None:
        return []
    release, lineage = _release_snapshot(session, release_id=release_id)
    candidates = (
        session.execute(
            select(StockConnectActiveSecurityRevision)
            .where(
                StockConnectActiveSecurityRevision.trade_date == trade_date,
                StockConnectActiveSecurityRevision.channel == channel.channel,
                StockConnectActiveSecurityRevision.direction == channel.direction,
                StockConnectActiveSecurityRevision.methodology_version_id
                == release.methodology_version_id,
                StockConnectActiveSecurityRevision.known_from <= release.created_at,
                (StockConnectActiveSecurityRevision.known_to.is_(None))
                | (StockConnectActiveSecurityRevision.known_to > release.created_at),
            )
            .order_by(StockConnectActiveSecurityRevision.rank_no)
        )
        .scalars()
        .all()
    )
    rows = [
        row
        for row in candidates
        if (
            _stock_connect_record_key(
                channel=channel,
                trade_date=trade_date,
                rank_no=row.rank_no,
            ),
            UUID(str(row.source_batch_id)),
        )
        in lineage
    ]
    if len(rows) > 10 or len({row.rank_no for row in rows}) != len(rows):
        raise ValueError("stock-connect active release ranking is invalid")
    return list(rows)


def _release_snapshot(
    session: Session,
    *,
    release_id: UUID,
) -> tuple[DatasetRelease, set[tuple[str, UUID]]]:
    """读取 immutable release 及其主要逐记录血缘，用于重建当时的双时态事实集合。"""
    release = session.get(DatasetRelease, release_id)
    if release is None:
        raise ValueError("stock-connect canonical release is unavailable")
    lineage = {
        (str(record_key_hash), UUID(str(source_batch_id)))
        for record_key_hash, source_batch_id in session.execute(
            select(
                CanonicalRecordLineage.record_key_hash,
                CanonicalRecordLineage.source_batch_id,
            ).where(
                CanonicalRecordLineage.release_id == release_id,
                CanonicalRecordLineage.role == "primary",
            )
        ).all()
    }
    if not lineage:
        raise ValueError("stock-connect canonical release has no primary record lineage")
    return release, lineage


def _stock_connect_record_key(
    *,
    channel: StockConnectChannel,
    trade_date: date,
    rank_no: int | None = None,
) -> str:
    """重建 canonical 通道统计或活跃名次业务键，必须与生产发布键算法完全一致。"""
    value = f"{channel.channel}:{channel.direction}:{trade_date.isoformat()}"
    if rank_no is not None:
        value = f"{value}:{rank_no}"
    return hashlib.sha256(value.encode()).hexdigest()


def _calendar_observation(
    session: Session,
    *,
    calendar: StockConnectCalendarDay,
    source_ref: Mapping[str, object],
    observed_at: datetime,
) -> UUID:
    """幂等保存年度日历文件中的一天，并验证来源摘要和方向语义。"""
    digest = str(source_ref.get("sourceFileSha256", ""))
    if len(digest) != 64:
        raise ValueError("stock-connect calendar source digest is invalid")
    existing = session.execute(
        select(StockConnectCalendarObservation.observation_id).where(
            StockConnectCalendarObservation.calendar_date == calendar.calendar_date,
            StockConnectCalendarObservation.source_file_sha256 == digest,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return UUID(str(existing))
    observation_id = uuid4()
    session.execute(
        insert(StockConnectCalendarObservation).values(
            observation_id=observation_id,
            calendar_date=calendar.calendar_date,
            northbound_trading=calendar.northbound_trading,
            southbound_trading=calendar.southbound_trading,
            hong_kong_state=calendar.hong_kong_state,
            mainland_state=calendar.mainland_state,
            source_publication_at=datetime.fromisoformat(
                str(source_ref["sourcePublicationAt"]).replace("Z", "+00:00")
            ),
            source_file_sha256=digest,
            source_ref=dict(source_ref),
            observed_at=observed_at,
        )
    )
    return observation_id


def _status_revision(session: Session, *, status: StockConnectChannelStatus, now: datetime) -> UUID:
    """幂等保存一条日终状态；历史来源缺失使用真实缺源观察而不制造额度值。"""
    payload = {
        "tradeDate": status.trade_date.isoformat(),
        "tradingDay": status.trading_day,
        "sessionState": status.session_state,
        "sessionAvailability": status.session_availability,
        "buyOrderAccepted": status.buy_order_accepted,
        "sellOrderAccepted": status.sell_order_accepted,
        "quotaState": status.quota_state,
        "quotaBalance": _decimal(status.quota_balance),
        "observedAt": status.observed_at.isoformat(),
        "sourceCode": status.source_code,
        "sourceFileSha256": status.source_file_sha256,
    }
    content_hash = _hash_payload(payload)
    existing = session.execute(
        select(StockConnectChannelStatusRevision.status_revision_id).where(
            StockConnectChannelStatusRevision.trade_date == status.trade_date,
            StockConnectChannelStatusRevision.channel == status.channel,
            StockConnectChannelStatusRevision.direction == status.direction,
            StockConnectChannelStatusRevision.content_hash == content_hash,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return UUID(str(existing))
    status_revision_id = uuid4()
    if status.source_file_sha256 is None:
        source_ref = None
    else:
        publication_at = status.source_publication_at
        if publication_at is None:
            raise ValueError("stock-connect status digest requires a source publication time")
        source_ref = {
            "sourceCode": status.source_code,
            "productName": status.product_name,
            "sourcePublicationAvailability": "REPORTED",
            "sourcePublicationAt": _timestamp(publication_at),
            "sourceObservedAt": _timestamp(status.observed_at),
            "sourceFileSha256": status.source_file_sha256,
        }
    session.execute(
        insert(StockConnectChannelStatusRevision).values(
            status_revision_id=status_revision_id,
            trade_date=status.trade_date,
            channel=status.channel,
            direction=status.direction,
            trading_day=status.trading_day,
            session_state=status.session_state,
            session_availability=status.session_availability,
            buy_order_accepted=status.buy_order_accepted,
            sell_order_accepted=status.sell_order_accepted,
            quota_state=status.quota_state,
            quota_balance=status.quota_balance,
            observed_at=status.observed_at,
            source_ref=source_ref,
            content_hash=content_hash,
            published_at=now,
        )
    )
    return status_revision_id


def _summary(
    *,
    channel: StockConnectChannel,
    market: StockConnectChannelDailyRevision,
    active: Sequence[StockConnectActiveSecurityRevision],
    status: StockConnectChannelStatus,
) -> dict[str, object]:
    """构造符合合同的通道摘要，净额只在买卖金额均真实可用时派生。"""
    availability = dict(market.field_availability)
    lineage = _projection_input_lineage(
        source_kind="market",
        release_id=market.release_id,
        row_id=market.row_id,
        channel=channel,
        trade_date=market.trade_date,
        currency=market.currency,
    )
    stats = {
        "buyAmount": _money_fact(
            market.buy_amount,
            market.currency,
            availability.get("buyAmount", "SOURCE_MISSING"),
            lineage,
        ),
        "sellAmount": _money_fact(
            market.sell_amount,
            market.currency,
            availability.get("sellAmount", "SOURCE_MISSING"),
            lineage,
        ),
        "turnoverAmount": _money_fact(
            market.turnover_amount,
            market.currency,
            availability.get("turnoverAmount", "SOURCE_MISSING"),
            lineage,
        ),
        "netBuyAmount": _derived_net_fact(
            market.buy_amount,
            market.sell_amount,
            market.currency,
            availability,
            lineage,
        ),
        "tradeCount": _count_fact(
            market.trade_count,
            availability.get("tradeCount", "SOURCE_MISSING"),
            lineage,
        ),
        "etfTurnoverAmount": _money_fact(
            market.etf_turnover_amount,
            market.currency,
            availability.get("etfTurnoverAmount", "SOURCE_MISSING"),
            lineage,
        ),
    }
    return {
        "channel": _channel_code(channel),
        "direction": channel.direction,
        "route": "SHANGHAI" if channel.channel == "SH" else "SHENZHEN",
        "tradeDate": market.trade_date.isoformat(),
        "stats": stats,
        "status": _status_payload(status),
        "activeSecurityCount": len(active),
    }


def _active_item(
    *, channel: StockConnectChannel, row: StockConnectActiveSecurityRevision
) -> dict[str, object]:
    """投影一条来源活跃榜，并把榜内可用净额标成 DERIVED 而非来源直报。"""
    availability = dict(row.field_availability)
    lineage = _projection_input_lineage(
        source_kind="active",
        release_id=row.release_id,
        row_id=row.row_id,
        channel=channel,
        trade_date=row.trade_date,
        currency=row.currency,
        rank_no=row.rank_no,
    )
    return {
        "rankingRank": row.rank_no,
        "sourceRank": row.rank_no,
        "identity": {
            "identityAvailability": row.identity_status,
            "instrumentEntityRef": (
                None if row.instrument_id is None else f"market-instrument:{row.instrument_id}"
            ),
            "sourceSecurityCode": row.source_instrument_code,
            "displayName": row.source_instrument_name,
            "listingVenue": (
                "SSE"
                if channel.direction == "NORTHBOUND" and channel.channel == "SH"
                else "SZSE"
                if channel.direction == "NORTHBOUND"
                else "HKEX"
            ),
        },
        "buyAmount": _money_fact(
            row.buy_amount,
            row.currency,
            availability.get("buyAmount", "SOURCE_MISSING"),
            lineage,
        ),
        "sellAmount": _money_fact(
            row.sell_amount,
            row.currency,
            availability.get("sellAmount", "SOURCE_MISSING"),
            lineage,
        ),
        "turnoverAmount": _money_fact(
            row.turnover_amount,
            row.currency,
            availability.get("turnoverAmount", "SOURCE_MISSING"),
            lineage,
        ),
        "netBuyAmount": _derived_net_fact(
            row.buy_amount,
            row.sell_amount,
            row.currency,
            availability,
            lineage,
        ),
    }


def _money_fact(
    value: Decimal | None,
    currency: str,
    availability: str,
    lineage: str,
) -> dict[str, object]:
    """构造金额事实；有值与可用性不一致时立即拒绝而不是修饰状态。"""
    if value is not None and availability not in {"REPORTED", "DERIVED"}:
        raise ValueError("stock-connect money value contradicts availability")
    if value is None and availability in {"REPORTED", "DERIVED"}:
        raise ValueError("stock-connect available money fact is missing a value")
    return {
        "availability": availability,
        "value": (
            None if value is None else {"amount": str(value), "currency": currency, "unit": "BASE"}
        ),
        "lineageRef": lineage if value is not None else None,
    }


def _count_fact(value: int | None, availability: str, lineage: str) -> dict[str, object]:
    """构造计数事实，来源未提供时值和血缘同时为空。"""
    if (value is None) == (availability in {"REPORTED", "DERIVED"}):
        raise ValueError("stock-connect count value contradicts availability")
    return {
        "availability": availability,
        "value": value,
        "lineageRef": lineage if value is not None else None,
    }


def _derived_net_fact(
    buy: Decimal | None,
    sell: Decimal | None,
    currency: str,
    availability: Mapping[str, str],
    lineage: str,
) -> dict[str, object]:
    """在同一 bundle 内用版本化方法从同一行买卖额派生净额，成交额绝不参与。"""
    if buy is not None and sell is not None:
        return _money_fact(
            buy - sell,
            currency,
            "DERIVED",
            (
                f"derived-projection:{_BUY_MINUS_SELL_METHODOLOGY}:"
                f"inputs:buyAmount,sellAmount:{lineage}"
            ),
        )
    states = {
        availability.get("buyAmount", "SOURCE_MISSING"),
        availability.get("sellAmount", "SOURCE_MISSING"),
    }
    missing = (
        "NOT_DISCLOSED_BY_REGIME"
        if "NOT_DISCLOSED_BY_REGIME" in states
        else "SOURCE_MISSING"
        if "SOURCE_MISSING" in states
        else "NOT_APPLICABLE"
    )
    return _money_fact(None, currency, missing, lineage)


def _projection_input_lineage(
    *,
    source_kind: str,
    release_id: UUID,
    row_id: UUID,
    channel: StockConnectChannel,
    trade_date: date,
    currency: str,
    rank_no: int | None = None,
) -> str:
    """冻结派生输入的同 release、同行、通道、交易日和币种身份。"""
    rank = "" if rank_no is None else f":rank:{rank_no}"
    return (
        f"{source_kind}-release:{release_id}:row:{row_id}:"
        f"channel:{_channel_code(channel)}:trade-date:{trade_date.isoformat()}:"
        f"currency:{currency}{rank}"
    )


def _status_payload(status: StockConnectChannelStatus) -> dict[str, object]:
    """投影通道状态；额度充足的空值与额度耗尽的真实零值严格区分。"""
    availability = (
        "REPORTED"
        if status.quota_state in {"ACTUAL_REPORTED", "EXHAUSTED"}
        else "NOT_DISCLOSED_BY_REGIME"
        if status.quota_state == "SUFFICIENT"
        else status.quota_state
    )
    lineage = (
        None if status.source_file_sha256 is None else f"status-source:{status.source_file_sha256}"
    )
    quota = _money_fact(
        status.quota_balance,
        "CNY",
        availability,
        lineage or "status-source-missing",
    )
    return {
        "tradingDay": status.trading_day,
        "sessionState": status.session_state,
        "buyOrderAccepted": status.buy_order_accepted,
        "sellOrderAccepted": status.sell_order_accepted,
        "quotaState": status.quota_state,
        "quotaBalance": quota,
        "observedAt": _timestamp(status.observed_at),
        "finality": "END_OF_DAY",
    }


def _identity_issues(
    active: Sequence[StockConnectActiveSecurityRevision],
) -> list[dict[str, str]]:
    """把来源身份缺口转换为受控非阻断质量问题。"""
    unresolved = [row for row in active if row.instrument_id is None]
    if not unresolved:
        return []
    return [
        {
            "code": "IDENTITY_SOURCE_UNRESOLVED",
            "component": "active-securities",
            "detail": f"{len(unresolved)} source-active identities remain unresolved",
        }
    ]


def _deduplicate_issues(
    issues: Sequence[Mapping[str, str]],
) -> list[dict[str, str]]:
    """稳定去重质量问题，避免重试改变完整包内容摘要。"""
    values = {(str(item["code"]), str(item["component"]), str(item["detail"])) for item in issues}
    return [
        {"code": code, "component": component, "detail": detail}
        for code, component, detail in sorted(values)
    ]


def _deduplicate_source_refs(
    refs: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    """按来源、产品和文件摘要去重，同一 licensed CSV 不因两个 capability 重复出现。"""
    unique: dict[tuple[str, str, object], dict[str, object]] = {}
    for item in refs:
        value = dict(item)
        key = (
            str(value.get("sourceCode")),
            str(value.get("productName")),
            value.get("sourceFileSha256"),
        )
        unique[key] = value
    return [unique[key] for key in sorted(unique, key=lambda item: tuple(map(str, item)))]


def _channel_code(channel: StockConnectChannel) -> str:
    """生成内部合同固定通道枚举。"""
    return f"{channel.channel}_{channel.direction}"


def _hash_payload(payload: Mapping[str, object]) -> str:
    """对完整 JSON 业务内容计算确定性 SHA-256。"""
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _decimal(value: Decimal | None) -> str | None:
    """稳定序列化可选 Decimal，保留真实零值。"""
    return None if value is None else str(value)


def _timestamp(value: datetime) -> str:
    """将带时区时间规范化为 UTC RFC 3339 文本。"""
    if value.tzinfo is None:
        raise ValueError("stock-connect timestamp must include timezone")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _record_bundle_progress(
    *,
    session: Session,
    channel: StockConnectChannel,
    trade_date: date,
    active_count: int,
) -> None:
    """在完整包同一事务提交分区水位，并同步控制面终态进度和检查点。"""
    execution = current_fenced_execution()
    if execution is None:
        return
    partition_key = f"stock-connect:{trade_date.isoformat()}:{channel.channel}:{channel.direction}"
    partition = session.get(
        DataOperationPartition,
        {"run_id": execution.run_id, "partition_key": partition_key},
    )
    run = session.get(DataOperationRun, execution.run_id)
    if partition is None or run is None:
        raise RuntimeError("stock-connect execution partition is unavailable")
    partition.status = "SUCCEEDED"
    partition.attempt = run.attempt
    partition.checkpoint_hash = hashlib.sha256(partition_key.encode()).hexdigest()
    partition.checkpoint_kind = "stock-connect-bundle"
    partition.checkpoint_updated_at = datetime.now(UTC)
    partition.error_json = None
    execution.record_publication_progress(record_count=1 + active_count)
    execution.record_checkpoint(
        kind="trade_date",
        position=trade_date.isoformat(),
    )
