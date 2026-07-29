"""龙虎榜与大宗交易 P0 的原子 canonical 发布仓储。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from uuid import UUID, uuid4

from sqlalchemy import insert, select, update
from sqlalchemy.orm import Session

from service_data_sync.application.ports.canonical_release import (
    CanonicalLineageRecord,
    CanonicalQualityDecision,
    CanonicalQualityRule,
    CanonicalReleaseCandidate,
)
from service_data_sync.application.ports.trading_events import (
    BlockTradeRepository,
    DragonTigerRepository,
    PublishedTradingEvents,
    TradingEventsSourceObservation,
)
from service_data_sync.domain.trading_events import BlockTrade, DragonTigerEvent
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.database.models.canonical import CanonicalCheckpoint
from service_data_sync.infrastructure.database.models.equity.identity.equity_identifier_version import (  # noqa: E501
    EquityIdentifierVersion,
)
from service_data_sync.infrastructure.database.models.market import (
    BlockTradeExecutionRevision,
    DragonTigerEventRevision,
    DragonTigerSeatItem,
    TradingDisclosureReasonMapVersion,
    TradingVenue,
)
from service_data_sync.infrastructure.database.models.provenance.source_batch import SourceBatch
from service_data_sync.infrastructure.persistence.canonical_release_repository import (
    SqlAlchemyCanonicalReleaseRepository,
)
from service_data_sync.infrastructure.persistence.typed_p0_support import (
    TypedP0SourceApproval,
    ensure_dataset,
    ensure_methodology,
    ensure_source_dataset,
    record_manifest,
    record_normalization_run,
    record_source_batch,
)

_DRAGON_DATASET = "equity.dragon_tiger.disclosure.reported"
_BLOCK_DATASET = "equity.block_trade.execution.reported"
_DRAGON_METHODOLOGY = "dragon-tiger-disclosure-reported"
_BLOCK_METHODOLOGY = "block-trade-execution-reported"
_DRAGON_MAPPING = "dragon-tiger-v1"
_BLOCK_MAPPING = "block-trade-v1"


@dataclass(frozen=True, slots=True)
class TradingEventsSourceApproval(TypedP0SourceApproval):
    """标识可用于交易公开信息生产发布的经批准真实来源。"""


@dataclass(frozen=True, slots=True)
class _PreparedDragon:
    """封装待写龙虎榜 revision 的领域事件、永久身份、原因家族与摘要。"""

    value: DragonTigerEvent
    security_id: int
    venue_id: UUID
    reason_family: str
    content_hash: str
    revision_no: int


@dataclass(frozen=True, slots=True)
class _PreparedBlock:
    """封装待写大宗交易 revision 的领域事实、永久身份、经济摘要与版本。"""

    value: BlockTrade
    security_id: int
    venue_id: UUID
    economic_fingerprint: str
    content_hash: str
    revision_no: int


class SqlAlchemyTradingEventsRepository(DragonTigerRepository, BlockTradeRepository):
    """只发布已批准来源、可解析证券身份且不混入事后指标的交易公开事实。"""

    def __init__(
        self,
        database: DatabaseClient,
        *,
        approved_sources: Mapping[str, TradingEventsSourceApproval] | None = None,
    ) -> None:
        """保存事务工厂和来源批准表；没有批准项时所有发布保持 fail-closed。"""
        self._database = database
        self._approved_sources = dict(approved_sources or {})
        self._release_repository = SqlAlchemyCanonicalReleaseRepository(database)

    def publish_dragon_tiger(
        self,
        *,
        events: Sequence[DragonTigerEvent],
        source: TradingEventsSourceObservation,
    ) -> PublishedTradingEvents:
        """原子发布龙虎榜事件和席位，来源键相同且内容不变时复用现有 release。"""
        values = tuple(events)
        if not values or len({value.source_event_key for value in values}) != len(values):
            raise ValueError("dragon-tiger events must be non-empty and unique by source event key")
        approval = _approved_source(self._approved_sources, source)
        prepared: list[_PreparedDragon] = []
        source_batch_id: UUID | None = None

        def prepare(session: Session) -> CanonicalReleaseCandidate:
            """在单事务内登记来源、解析证券及交易所、映射原因并生成 release 候选。"""
            nonlocal source_batch_id
            now = datetime.now(UTC)
            dataset_id = ensure_dataset(
                session,
                code=_DRAGON_DATASET,
                domain="trading_disclosure",
                grain="security + trade date + source event + disclosed seat",
                now=now,
            )
            methodology_id = ensure_methodology(
                session,
                code=_DRAGON_METHODOLOGY,
                semantic_family="reported-dragon-tiger-disclosure",
                mapping_version=_DRAGON_MAPPING,
                documentation_ref="docs/service-data-sync/0025-trading-events/index.html",
            )
            source_dataset_id = ensure_source_dataset(
                session,
                approval=approval,
                capability=source.capability,
                native_grain="dragon-tiger event + seat",
            )
            source_batch_id = record_source_batch(
                session, source=source, source_dataset_id=source_dataset_id, now=now
            )
            resolved = {
                value.source_event_key: _resolve_security_and_venue(
                    session, source_code=value.source_security_code, trade_date=value.trade_date
                )
                for value in values
            }
            current = _current_dragon(
                session, provider_id=source.provider_id, methodology_version_id=methodology_id
            )
            prepared[:] = []
            for value in values:
                security_id, venue_id = resolved[value.source_event_key]
                reason_family = _reason_family(
                    session,
                    venue_id=venue_id,
                    reason_code=value.reason_code,
                    trade_date=value.trade_date,
                )
                content_hash = _dragon_hash(value, reason_family)
                existing = current.get(value.source_event_key)
                if existing is None or content_hash != existing.content_hash:
                    prepared.append(
                        _PreparedDragon(
                            value=value,
                            security_id=security_id,
                            venue_id=venue_id,
                            reason_family=reason_family,
                            content_hash=content_hash,
                            revision_no=1 if existing is None else existing.revision_no + 1,
                        )
                    )
            partition_key = f"provider:{source.provider_id}"
            normalization_run_id = record_normalization_run(
                session,
                dataset_id=dataset_id,
                partition_key=partition_key,
                source=source,
                source_batch_id=source_batch_id,
                mapping_version=_DRAGON_MAPPING,
                now=now,
            )
            return _dragon_candidate(
                session,
                dataset_id=dataset_id,
                methodology_version_id=methodology_id,
                normalization_run_id=normalization_run_id,
                partition_key=partition_key,
                current=current,
                changed={item.value.source_event_key: item for item in prepared},
                source_batch_id=source_batch_id,
                now=now,
            )

        def write(session: Session, candidate: CanonicalReleaseCandidate, release_id: UUID) -> None:
            """关闭相同来源事件的旧知识版本，追加事件头和不合并的原始席位集合。"""
            if source_batch_id is None:
                raise AssertionError("dragon-tiger preparation did not resolve source batch")
            for item in prepared:
                session.execute(
                    update(DragonTigerEventRevision)
                    .where(
                        DragonTigerEventRevision.source_event_key == item.value.source_event_key,
                        DragonTigerEventRevision.methodology_version_id
                        == candidate.methodology_version_id,
                        DragonTigerEventRevision.known_to.is_(None),
                    )
                    .values(known_to=candidate.created_at)
                )
                revision_id = uuid4()
                session.execute(
                    insert(DragonTigerEventRevision).values(
                        trade_date=item.value.trade_date,
                        event_revision_id=revision_id,
                        security_id=item.security_id,
                        venue_id=item.venue_id,
                        reason_code=item.value.reason_code,
                        reason_raw=item.value.reason_text,
                        reason_family=item.reason_family,
                        close_price=item.value.close_price,
                        turnover_amount=item.value.market_turnover_amount,
                        buy_amount=item.value.buy_amount,
                        sell_amount=item.value.sell_amount,
                        net_amount=item.value.net_amount,
                        deal_amount=item.value.deal_amount,
                        deal_ratio=item.value.deal_ratio,
                        net_ratio=item.value.net_ratio,
                        turnover_ratio=item.value.turnover_ratio,
                        currency="CNY",
                        source_event_key=item.value.source_event_key,
                        methodology_version_id=candidate.methodology_version_id,
                        release_id=release_id,
                        revision_no=item.revision_no,
                        source_batch_id=source_batch_id,
                        source_published_at=item.value.source_published_at,
                        source_time_precision=item.value.visible_time_precision,
                        public_usable_at=item.value.visible_at,
                        availability_basis="OFFICIAL_DISCLOSURE",
                        known_from=candidate.created_at,
                        known_to=None,
                        content_hash=item.content_hash,
                        quality_status="passed",
                    )
                )
                session.execute(
                    insert(DragonTigerSeatItem).values(
                        [
                            {
                                "trade_date": item.value.trade_date,
                                "event_revision_id": revision_id,
                                "side": seat.list_side,
                                "rank_no": seat.rank,
                                "seat_code": seat.seat_code,
                                "seat_name_raw": seat.seat_name,
                                "seat_type": None,
                                "amount": seat.buy_amount
                                if seat.list_side == "BUY"
                                else seat.sell_amount,
                                "ratio": seat.buy_ratio
                                if seat.list_side == "BUY"
                                else seat.sell_ratio,
                                "buy_amount": seat.buy_amount,
                                "sell_amount": seat.sell_amount,
                                "net_amount": seat.net_amount,
                                "buy_ratio": seat.buy_ratio,
                                "sell_ratio": seat.sell_ratio,
                            }
                            for seat in item.value.seats
                        ]
                    )
                )
                record_manifest(
                    session,
                    normalization_run_id=candidate.normalization_run_id,
                    record_key_hash=_hash(item.value.source_event_key),
                    canonical_table=DragonTigerEventRevision.__tablename__,
                    canonical_pk={"eventRevisionId": str(revision_id)},
                    content_hash=item.content_hash,
                )

        publication = self._release_repository.publish_prepared(
            prepare_candidate=prepare, write_facts=write
        )
        return PublishedTradingEvents(
            data_version=publication.data_version,
            inserted_count=len(prepared),
            unchanged_count=len(values) - len(prepared),
        )

    def publish_block_trades(
        self,
        *,
        trades: Sequence[BlockTrade],
        source: TradingEventsSourceObservation,
    ) -> PublishedTradingEvents:
        """原子发布大宗逐笔成交，保留来源 occurrence 而不按经济字段错误去重。"""
        values = tuple(trades)
        if not values or len(
            {(value.source_trade_key, value.occurrence_no) for value in values}
        ) != len(values):
            raise ValueError("block trades must be non-empty and unique by source key/occurrence")
        approval = _approved_source(self._approved_sources, source)
        prepared: list[_PreparedBlock] = []
        source_batch_id: UUID | None = None

        def prepare(session: Session) -> CanonicalReleaseCandidate:
            """在事务内注册来源、解析证券场所并按来源逐笔键构造 release 候选。"""
            nonlocal source_batch_id
            now = datetime.now(UTC)
            dataset_id = ensure_dataset(
                session,
                code=_BLOCK_DATASET,
                domain="trading_disclosure",
                grain="security + trade date + source trade + occurrence",
                now=now,
            )
            methodology_id = ensure_methodology(
                session,
                code=_BLOCK_METHODOLOGY,
                semantic_family="reported-block-trade-execution",
                mapping_version=_BLOCK_MAPPING,
                documentation_ref="docs/service-data-sync/0025-trading-events/index.html",
            )
            source_dataset_id = ensure_source_dataset(
                session,
                approval=approval,
                capability=source.capability,
                native_grain="block-trade execution",
            )
            source_batch_id = record_source_batch(
                session, source=source, source_dataset_id=source_dataset_id, now=now
            )
            resolved = {
                (value.source_trade_key, value.occurrence_no): _resolve_security_and_venue(
                    session, source_code=value.source_security_code, trade_date=value.trade_date
                )
                for value in values
            }
            current = _current_block(
                session, provider_id=source.provider_id, methodology_version_id=methodology_id
            )
            prepared[:] = []
            for value in values:
                key = (value.source_trade_key, value.occurrence_no)
                security_id, venue_id = resolved[key]
                economic_fingerprint = _block_economic_fingerprint(value)
                content_hash = _block_hash(value, economic_fingerprint)
                existing = current.get(key)
                if existing is None or content_hash != existing.content_hash:
                    prepared.append(
                        _PreparedBlock(
                            value=value,
                            security_id=security_id,
                            venue_id=venue_id,
                            economic_fingerprint=economic_fingerprint,
                            content_hash=content_hash,
                            revision_no=1 if existing is None else existing.revision_no + 1,
                        )
                    )
            partition_key = f"provider:{source.provider_id}"
            normalization_run_id = record_normalization_run(
                session,
                dataset_id=dataset_id,
                partition_key=partition_key,
                source=source,
                source_batch_id=source_batch_id,
                mapping_version=_BLOCK_MAPPING,
                now=now,
            )
            return _block_candidate(
                session,
                dataset_id=dataset_id,
                methodology_version_id=methodology_id,
                normalization_run_id=normalization_run_id,
                partition_key=partition_key,
                current=current,
                changed={
                    (item.value.source_trade_key, item.value.occurrence_no): item
                    for item in prepared
                },
                source_batch_id=source_batch_id,
                now=now,
            )

        def write(session: Session, candidate: CanonicalReleaseCandidate, release_id: UUID) -> None:
            """关闭同来源逐笔键旧知识版本，写入单笔真实金额和允许重复的 occurrence。"""
            if source_batch_id is None:
                raise AssertionError("block-trade preparation did not resolve source batch")
            for item in prepared:
                session.execute(
                    update(BlockTradeExecutionRevision)
                    .where(
                        BlockTradeExecutionRevision.source_event_key == item.value.source_trade_key,
                        BlockTradeExecutionRevision.occurrence_no == item.value.occurrence_no,
                        BlockTradeExecutionRevision.methodology_version_id
                        == candidate.methodology_version_id,
                        BlockTradeExecutionRevision.known_to.is_(None),
                    )
                    .values(known_to=candidate.created_at)
                )
                revision_id = uuid4()
                session.execute(
                    insert(BlockTradeExecutionRevision).values(
                        trade_date=item.value.trade_date,
                        execution_revision_id=revision_id,
                        security_id=item.security_id,
                        venue_id=item.venue_id,
                        price=item.value.execution_price,
                        quantity=item.value.quantity_shares,
                        quantity_unit="SHARE",
                        amount=item.value.notional_cny,
                        currency="CNY",
                        buyer_seat_raw=item.value.buyer_seat_name,
                        seller_seat_raw=item.value.seller_seat_name,
                        buyer_seat_code=item.value.buyer_seat_code,
                        seller_seat_code=item.value.seller_seat_code,
                        reference_price=item.value.reference_close_price,
                        reference_price_type="CLOSE"
                        if item.value.reference_close_price is not None
                        else None,
                        premium_ratio=item.value.premium_discount_ratio,
                        source_event_key=item.value.source_trade_key,
                        source_daily_rank=item.value.source_daily_rank,
                        economic_fingerprint=item.economic_fingerprint,
                        occurrence_no=item.value.occurrence_no,
                        methodology_version_id=candidate.methodology_version_id,
                        release_id=release_id,
                        revision_no=item.revision_no,
                        source_batch_id=source_batch_id,
                        source_published_at=item.value.source_published_at,
                        source_time_precision=item.value.visible_time_precision,
                        public_usable_at=item.value.visible_at,
                        availability_basis="OFFICIAL_DISCLOSURE",
                        known_from=candidate.created_at,
                        known_to=None,
                        content_hash=item.content_hash,
                        quality_status="passed",
                    )
                )
                record_manifest(
                    session,
                    normalization_run_id=candidate.normalization_run_id,
                    record_key_hash=_hash(
                        f"{item.value.source_trade_key}:{item.value.occurrence_no}"
                    ),
                    canonical_table=BlockTradeExecutionRevision.__tablename__,
                    canonical_pk={"executionRevisionId": str(revision_id)},
                    content_hash=item.content_hash,
                )

        publication = self._release_repository.publish_prepared(
            prepare_candidate=prepare, write_facts=write
        )
        return PublishedTradingEvents(
            data_version=publication.data_version,
            inserted_count=len(prepared),
            unchanged_count=len(values) - len(prepared),
        )


def _approved_source(
    approvals: Mapping[str, TradingEventsSourceApproval], source: TradingEventsSourceObservation
) -> TradingEventsSourceApproval:
    """取得来源批准项，技术 adapter 不能绕过数据权利和留存审查。"""
    approval = approvals.get(source.provider_id)
    if approval is None:
        raise ValueError("trading-events source provider is not approved for publication")
    return approval


def _resolve_security_and_venue(
    session: Session, *, source_code: str, trade_date: date
) -> tuple[int, UUID]:
    """按事实日期解析唯一确认的股票与交易所，代码跨场所歧义时拒绝发布。"""
    rows = session.execute(
        select(EquityIdentifierVersion.security_id, EquityIdentifierVersion.exchange).where(
            EquityIdentifierVersion.symbol == source_code,
            EquityIdentifierVersion.identity_state == "CONFIRMED",
            EquityIdentifierVersion.effective_from <= trade_date,
            (EquityIdentifierVersion.effective_to.is_(None))
            | (EquityIdentifierVersion.effective_to > trade_date),
            EquityIdentifierVersion.known_to.is_(None),
        )
    ).all()
    candidates = {(int(security_id), str(exchange)) for security_id, exchange in rows}
    if len(candidates) != 1:
        raise ValueError("trading disclosure security identity is missing or ambiguous")
    security_id, exchange = candidates.pop()
    venue_ids = {
        UUID(str(value))
        for value in session.execute(
            select(TradingVenue.venue_id).where(TradingVenue.code == exchange)
        )
        .scalars()
        .all()
    }
    if len(venue_ids) != 1:
        raise ValueError("trading disclosure venue identity is missing or ambiguous")
    return security_id, venue_ids.pop()


def _reason_family(session: Session, *, venue_id: UUID, reason_code: str, trade_date: date) -> str:
    """查询事实日适用的原因映射；未治理新原因显式保留为 UNKNOWN 而不杜撰分类。"""
    rows = (
        session.execute(
            select(TradingDisclosureReasonMapVersion.canonical_family).where(
                TradingDisclosureReasonMapVersion.venue_id == venue_id,
                TradingDisclosureReasonMapVersion.source_reason_code == reason_code,
                TradingDisclosureReasonMapVersion.effective_from <= trade_date,
                (TradingDisclosureReasonMapVersion.effective_to.is_(None))
                | (TradingDisclosureReasonMapVersion.effective_to > trade_date),
            )
        )
        .scalars()
        .all()
    )
    families = {str(row) for row in rows}
    if len(families) > 1:
        raise ValueError("trading disclosure reason mapping is ambiguous")
    return "UNKNOWN" if not families else families.pop()


def _current_dragon(
    session: Session, *, provider_id: str, methodology_version_id: UUID
) -> dict[str, DragonTigerEventRevision]:
    """读取同 provider 当前龙虎榜事件，避免其他来源同名键进入本发布分区。"""
    rows = (
        session.execute(
            select(DragonTigerEventRevision)
            .join(
                SourceBatch, SourceBatch.source_batch_id == DragonTigerEventRevision.source_batch_id
            )
            .where(
                SourceBatch.provider_id == provider_id,
                DragonTigerEventRevision.methodology_version_id == methodology_version_id,
                DragonTigerEventRevision.known_to.is_(None),
            )
        )
        .scalars()
        .all()
    )
    result = {str(row.source_event_key): row for row in rows}
    if len(result) != len(rows):
        raise ValueError("dragon-tiger current revision is ambiguous")
    return result


def _current_block(
    session: Session, *, provider_id: str, methodology_version_id: UUID
) -> dict[tuple[str, int], BlockTradeExecutionRevision]:
    """读取同 provider 当前大宗逐笔记录，来源 occurrence 是保留合法重数的组成部分。"""
    rows = (
        session.execute(
            select(BlockTradeExecutionRevision)
            .join(
                SourceBatch,
                SourceBatch.source_batch_id == BlockTradeExecutionRevision.source_batch_id,
            )
            .where(
                SourceBatch.provider_id == provider_id,
                BlockTradeExecutionRevision.methodology_version_id == methodology_version_id,
                BlockTradeExecutionRevision.known_to.is_(None),
            )
        )
        .scalars()
        .all()
    )
    result = {(str(row.source_event_key), row.occurrence_no): row for row in rows}
    if len(result) != len(rows):
        raise ValueError("block-trade current revision is ambiguous")
    return result


def _dragon_candidate(
    session: Session,
    *,
    dataset_id: UUID,
    methodology_version_id: UUID,
    normalization_run_id: UUID,
    partition_key: str,
    current: Mapping[str, DragonTigerEventRevision],
    changed: Mapping[str, _PreparedDragon],
    source_batch_id: UUID,
    now: datetime,
) -> CanonicalReleaseCandidate:
    """合并当前龙虎榜和变化事件，未变化行继续指向其首次接受的来源批次。"""
    records: list[CanonicalLineageRecord] = []
    dates: list[date] = []
    for key in sorted({*current, *changed}):
        item = changed.get(key)
        if item is None:
            existing = current[key]
            content_hash, batch_id, trade_date = (
                existing.content_hash,
                UUID(str(existing.source_batch_id)),
                existing.trade_date,
            )
        else:
            content_hash, batch_id, trade_date = (
                item.content_hash,
                source_batch_id,
                item.value.trade_date,
            )
        dates.append(trade_date)
        records.append(
            CanonicalLineageRecord(
                record_key_hash=_hash(key),
                content_hash=content_hash,
                source_batch_id=batch_id,
                transform_hash=_hash(_DRAGON_MAPPING),
            )
        )
    return _candidate(
        session,
        dataset_id=dataset_id,
        dataset_code=_DRAGON_DATASET,
        methodology_version_id=methodology_version_id,
        normalization_run_id=normalization_run_id,
        partition_key=partition_key,
        records=records,
        dates=dates,
        checkpoint_position={"tradeDate": max(dates).isoformat()},
        policy_code="dragon-tiger.disclosure.quality",
        rule_code="source-seat-and-amount-reconciliation",
        now=now,
    )


def _block_candidate(
    session: Session,
    *,
    dataset_id: UUID,
    methodology_version_id: UUID,
    normalization_run_id: UUID,
    partition_key: str,
    current: Mapping[tuple[str, int], BlockTradeExecutionRevision],
    changed: Mapping[tuple[str, int], _PreparedBlock],
    source_batch_id: UUID,
    now: datetime,
) -> CanonicalReleaseCandidate:
    """合并当前大宗逐笔记录和变化项，来源键与 occurrence 一起构成稳定业务键。"""
    records: list[CanonicalLineageRecord] = []
    dates: list[date] = []
    for key in sorted({*current, *changed}):
        item = changed.get(key)
        if item is None:
            existing = current[key]
            content_hash, batch_id, trade_date = (
                existing.content_hash,
                UUID(str(existing.source_batch_id)),
                existing.trade_date,
            )
        else:
            content_hash, batch_id, trade_date = (
                item.content_hash,
                source_batch_id,
                item.value.trade_date,
            )
        dates.append(trade_date)
        records.append(
            CanonicalLineageRecord(
                record_key_hash=_hash(f"{key[0]}:{key[1]}"),
                content_hash=content_hash,
                source_batch_id=batch_id,
                transform_hash=_hash(_BLOCK_MAPPING),
            )
        )
    latest_key = max({*current, *changed})
    return _candidate(
        session,
        dataset_id=dataset_id,
        dataset_code=_BLOCK_DATASET,
        methodology_version_id=methodology_version_id,
        normalization_run_id=normalization_run_id,
        partition_key=partition_key,
        records=records,
        dates=dates,
        checkpoint_position={"tradeDate": max(dates).isoformat(), "sourceKey": latest_key[0]},
        policy_code="block-trade.execution.quality",
        rule_code="occurrence-preserved",
        now=now,
    )


def _candidate(
    session: Session,
    *,
    dataset_id: UUID,
    dataset_code: str,
    methodology_version_id: UUID,
    normalization_run_id: UUID,
    partition_key: str,
    records: Sequence[CanonicalLineageRecord],
    dates: Sequence[date],
    checkpoint_position: dict[str, object],
    policy_code: str,
    rule_code: str,
    now: datetime,
) -> CanonicalReleaseCandidate:
    """构造带 fencing token 的 canonical release 候选，防止并发同步倒退检查点。"""
    fencing_token = session.execute(
        select(CanonicalCheckpoint.fencing_token)
        .where(
            CanonicalCheckpoint.dataset_id == dataset_id,
            CanonicalCheckpoint.partition_key == partition_key,
            CanonicalCheckpoint.checkpoint_kind == "published",
        )
        .with_for_update()
    ).scalar_one_or_none()
    return CanonicalReleaseCandidate(
        dataset_id=dataset_id,
        dataset_code=dataset_code,
        partition_key=partition_key,
        methodology_version_id=methodology_version_id,
        normalization_run_id=normalization_run_id,
        records=tuple(records),
        quality=CanonicalQualityDecision(
            status="passed",
            policy_code=policy_code,
            policy_version=1,
            rules=(CanonicalQualityRule(rule_code, "blocking", True),),
        ),
        fact_min=min(dates),
        fact_max=max(dates),
        checkpoint_kind="published",
        checkpoint_position=checkpoint_position,
        expected_fencing_token=0 if fencing_token is None else int(fencing_token),
        created_at=now,
    )


def _dragon_hash(value: DragonTigerEvent, reason_family: str) -> str:
    """计算包含原因映射和席位集合的摘要，任何披露修订都触发新事件版本。"""
    return _hash_payload(
        {
            "sourceEventKey": value.source_event_key,
            "securityCode": value.source_security_code,
            "tradeDate": value.trade_date.isoformat(),
            "reasonCode": value.reason_code,
            "reasonText": value.reason_text,
            "reasonFamily": reason_family,
            "closePrice": _decimal(value.close_price),
            "buyAmount": _decimal(value.buy_amount),
            "sellAmount": _decimal(value.sell_amount),
            "netAmount": _decimal(value.net_amount),
            "dealAmount": _decimal(value.deal_amount),
            "marketTurnoverAmount": _decimal(value.market_turnover_amount),
            "dealRatio": _decimal(value.deal_ratio),
            "netRatio": _decimal(value.net_ratio),
            "turnoverRatio": _decimal(value.turnover_ratio),
            "sourcePublishedAt": _datetime(value.source_published_at),
            "visibleAt": _datetime(value.visible_at),
            "visibleTimePrecision": value.visible_time_precision,
            "seats": [
                {
                    "side": seat.list_side,
                    "rank": seat.rank,
                    "seatCode": seat.seat_code,
                    "seatName": seat.seat_name,
                    "buyAmount": _decimal(seat.buy_amount),
                    "sellAmount": _decimal(seat.sell_amount),
                    "netAmount": _decimal(seat.net_amount),
                    "buyRatio": _decimal(seat.buy_ratio),
                    "sellRatio": _decimal(seat.sell_ratio),
                }
                for seat in value.seats
            ],
        }
    )


def _block_economic_fingerprint(value: BlockTrade) -> str:
    """计算不含来源键和 occurrence 的经济字段摘要，用于审计相同经济成交的合法重数。"""
    return _hash_payload(
        {
            "securityCode": value.source_security_code,
            "tradeDate": value.trade_date.isoformat(),
            "executionPrice": _decimal(value.execution_price),
            "quantityShares": value.quantity_shares,
            "notionalCny": _decimal(value.notional_cny),
            "buyerSeatCode": value.buyer_seat_code,
            "buyerSeatName": value.buyer_seat_name,
            "sellerSeatCode": value.seller_seat_code,
            "sellerSeatName": value.seller_seat_name,
        }
    )


def _block_hash(value: BlockTrade, economic_fingerprint: str) -> str:
    """计算完整逐笔摘要，来源键、occurrence、可见时间和参考价均参与 revision 身份。"""
    return _hash_payload(
        {
            "sourceTradeKey": value.source_trade_key,
            "occurrenceNo": value.occurrence_no,
            "economicFingerprint": economic_fingerprint,
            "referenceClosePrice": _decimal(value.reference_close_price),
            "premiumDiscountRatio": _decimal(value.premium_discount_ratio),
            "sourceDailyRank": value.source_daily_rank,
            "sourcePublishedAt": _datetime(value.source_published_at),
            "visibleAt": _datetime(value.visible_at),
            "visibleTimePrecision": value.visible_time_precision,
        }
    )


def _hash_payload(payload: Mapping[str, object]) -> str:
    """以规范 JSON 计算 SHA-256，精确数值先转文本避免浮点格式制造伪修订。"""
    return _hash(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _hash(value: str) -> str:
    """计算 UTF-8 SHA-256，用于内容、映射和业务键摘要。"""
    return hashlib.sha256(value.encode()).hexdigest()


def _decimal(value: object) -> str | None:
    """把可选精确数值稳定投影为字符串，未披露空值不会与真实零混同。"""
    return None if value is None else str(value)


def _datetime(value: datetime | None) -> str | None:
    """把可选带时区时间稳定转为 ISO 文本，避免序列化实现影响内容摘要。"""
    return None if value is None else value.isoformat()
