"""沪深港通 `P0` 通道统计与活跃榜的原子 `canonical` 发布仓储。

沪深通、南北向和各披露制度是不同分区，金额、币种和字段可用性只能按事实日对应的
制度解释。活跃榜依赖同日通道统计 `release`，同一证券代码也不会跨 A 股与港股市场
合并；未披露数值保持带状态的空值，不能由明细或名称猜测回算。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import and_, func, insert, literal, or_, select, update
from sqlalchemy.orm import Session

from service_data_sync.application.ports.canonical_release import (
    CanonicalLineageRecord,
    CanonicalQualityDecision,
    CanonicalQualityRule,
    CanonicalReleaseCandidate,
)
from service_data_sync.application.ports.stock_connect import (
    PublishedStockConnectActiveSecurities,
    PublishedStockConnectMarketDaily,
    StockConnectActiveSecurityRepository,
    StockConnectMarketDailyRepository,
    StockConnectSourceObservation,
)
from service_data_sync.domain.stock_connect import (
    StockConnectActiveSecurity,
    StockConnectChannel,
    StockConnectInstrumentMaster,
    StockConnectMarketDaily,
)
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.database.models.canonical import CanonicalCheckpoint
from service_data_sync.infrastructure.database.models.equity.identity.equity_identifier_version import (  # noqa: E501
    EquityIdentifierVersion,
)
from service_data_sync.infrastructure.database.models.equity.identity.equity_instrument import (
    EquityInstrument,
)
from service_data_sync.infrastructure.database.models.market import (
    StockConnectActiveSecurityRevision,
    StockConnectChannelDailyRevision,
    StockConnectDisclosureRegime,
)
from service_data_sync.infrastructure.database.models.market.identity import (
    InstrumentIdentifierVersion,
    InstrumentLifecycleVersion,
    MarketEntity,
    MarketInstrument,
    TradingVenue,
)
from service_data_sync.infrastructure.database.models.market.stock_connect_identity import (
    StockConnectHkexInstrumentIdentity,
)
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

_MARKET_DATASET = "market.stock_connect.market_stat.reported"
_ACTIVE_DATASET = "market.stock_connect.active_security.snapshot"
_MARKET_METHODOLOGY = "stock-connect-channel-reported"
_ACTIVE_METHODOLOGY = "stock-connect-active-security-reported"
_MARKET_MAPPING = "stock-connect-channel-v1"
_ACTIVE_MAPPING = "stock-connect-active-security-v1"
_IDENTIFIER_SCHEME = "venue_symbol"
_METRIC_FIELDS = {
    "buy_amount",
    "sell_amount",
    "turnover_amount",
    "net_buy_amount",
    "quota_balance",
    "trade_count",
    "etf_turnover_amount",
}


@dataclass(frozen=True, slots=True)
class StockConnectSourceApproval(TypedP0SourceApproval):
    """标识已完成权利、留存和内部使用审查的沪深港通来源。"""


@dataclass(frozen=True, slots=True)
class _PreparedMarketRecord:
    """封装待写通道统计 revision 的值、制度、摘要与修订序号。"""

    value: StockConnectMarketDaily
    regime_id: UUID
    content_hash: str
    revision_no: int


@dataclass(frozen=True, slots=True)
class _PreparedActiveRecord:
    """封装待写活跃榜 revision 的值、工具身份、依赖统计 release 与修订序号。"""

    value: StockConnectActiveSecurity
    instrument_id: UUID | None
    market_stat_release_id: UUID
    content_hash: str
    revision_no: int


class SqlAlchemyStockConnectMarketDataRepository(
    StockConnectMarketDailyRepository, StockConnectActiveSecurityRepository
):
    """仅发布已批准官方来源且通过制度、身份和统计依赖门的港通 P0 事实。"""

    def __init__(
        self,
        database: DatabaseClient,
        *,
        approved_sources: Mapping[str, StockConnectSourceApproval] | None = None,
    ) -> None:
        """保存事务工厂和显式来源批准表；空表默认拒绝所有生产写入。"""
        self._database = database
        self._approved_sources = dict(approved_sources or {})
        self._release_repository = SqlAlchemyCanonicalReleaseRepository(database)

    def publish_market_daily(
        self,
        *,
        channel: StockConnectChannel,
        records: Sequence[StockConnectMarketDaily],
        source: StockConnectSourceObservation,
    ) -> PublishedStockConnectMarketDaily:
        """发布通道方向当前统计快照；每个日期必须绑定已登记披露制度。"""
        values = tuple(records)
        if not values or len({value.trade_date for value in values}) != len(values):
            raise ValueError(
                "stock-connect market records must be non-empty and unique by trade date"
            )
        approval = _approved_source(self._approved_sources, source)
        prepared: list[_PreparedMarketRecord] = []
        source_batch_id: UUID | None = None

        def prepare(session: Session) -> CanonicalReleaseCandidate:
            """在单事务内登记来源、解析每日期制度并构造完整分区 release。"""
            nonlocal source_batch_id
            now = datetime.now(UTC)
            dataset_id = ensure_dataset(
                session,
                code=_MARKET_DATASET,
                domain="stock_connect",
                grain="channel + direction + trade date + reported disclosure regime",
                now=now,
            )
            methodology_id = ensure_methodology(
                session,
                code=_MARKET_METHODOLOGY,
                semantic_family="reported-stock-connect-channel-daily",
                mapping_version=_MARKET_MAPPING,
                documentation_ref=(
                    "docs/service-web/0010-stock-connect-center/service-data-sync.html"
                ),
            )
            _ensure_default_regimes(session, channel=channel, methodology_version_id=methodology_id)
            source_dataset_id = ensure_source_dataset(
                session,
                approval=approval,
                capability=source.capability,
                native_grain="channel + direction + trade date",
            )
            source_batch_id = record_source_batch(
                session, source=source, source_dataset_id=source_dataset_id, now=now
            )
            regimes = {
                value.trade_date: _resolve_regime(
                    session, channel=channel, trade_date=value.trade_date
                )
                for value in values
            }
            for value in values:
                _validate_regime_value(value, regimes[value.trade_date])
            partition_key = _channel_partition(channel)
            normalization_run_id = record_normalization_run(
                session,
                dataset_id=dataset_id,
                partition_key=partition_key,
                source=source,
                source_batch_id=source_batch_id,
                mapping_version=_MARKET_MAPPING,
                now=now,
            )
            current = _current_market(
                session, channel=channel, methodology_version_id=methodology_id
            )
            incoming = {value.trade_date: value for value in values}
            prepared[:] = [
                _PreparedMarketRecord(
                    value=value,
                    regime_id=UUID(str(regimes[value.trade_date].regime_id)),
                    content_hash=_market_hash(
                        value, UUID(str(regimes[value.trade_date].regime_id))
                    ),
                    revision_no=current[value.trade_date].revision_no + 1
                    if value.trade_date in current
                    else 1,
                )
                for value in values
                if value.trade_date not in current
                or _market_hash(value, UUID(str(regimes[value.trade_date].regime_id)))
                != current[value.trade_date].content_hash
            ]
            return _market_candidate(
                session,
                dataset_id=dataset_id,
                methodology_version_id=methodology_id,
                normalization_run_id=normalization_run_id,
                channel=channel,
                current=current,
                incoming=incoming,
                changed={item.value.trade_date: item for item in prepared},
                source_batch_id=source_batch_id,
                now=now,
            )

        def write(session: Session, candidate: CanonicalReleaseCandidate, release_id: UUID) -> None:
            """关闭同日旧知识版本，写入连同制度 ID 的不可变通道统计事实。"""
            if source_batch_id is None:
                raise AssertionError(
                    "stock-connect market preparation did not resolve source batch"
                )
            for item in prepared:
                session.execute(
                    update(StockConnectChannelDailyRevision)
                    .where(
                        StockConnectChannelDailyRevision.channel == channel.channel,
                        StockConnectChannelDailyRevision.direction == channel.direction,
                        StockConnectChannelDailyRevision.trade_date == item.value.trade_date,
                        StockConnectChannelDailyRevision.methodology_version_id
                        == candidate.methodology_version_id,
                        StockConnectChannelDailyRevision.known_to.is_(None),
                    )
                    .values(known_to=candidate.created_at)
                )
                row_id = uuid4()
                session.execute(
                    insert(StockConnectChannelDailyRevision).values(
                        trade_date=item.value.trade_date,
                        row_id=row_id,
                        channel=channel.channel,
                        direction=channel.direction,
                        regime_id=item.regime_id,
                        buy_amount=item.value.buy_amount,
                        sell_amount=item.value.sell_amount,
                        turnover_amount=item.value.turnover_amount,
                        net_buy_amount=item.value.net_buy_amount,
                        quota_balance=item.value.quota_balance,
                        currency=item.value.currency,
                        availability_status=item.value.availability_status,
                        trade_count=item.value.trade_count,
                        etf_turnover_amount=item.value.etf_turnover_amount,
                        field_availability=dict(item.value.field_availability),
                        center_schema_version=1,
                        methodology_version_id=candidate.methodology_version_id,
                        release_id=release_id,
                        revision_no=item.revision_no,
                        source_batch_id=source_batch_id,
                        source_published_at=None,
                        source_time_precision="UNKNOWN",
                        public_usable_at=candidate.created_at,
                        availability_basis="OBSERVED_ONLY",
                        known_from=candidate.created_at,
                        known_to=None,
                        content_hash=item.content_hash,
                        quality_status="passed",
                    )
                )
                record_manifest(
                    session,
                    normalization_run_id=candidate.normalization_run_id,
                    record_key_hash=_market_record_key(channel, item.value.trade_date),
                    canonical_table=StockConnectChannelDailyRevision.__tablename__,
                    canonical_pk={
                        "tradeDate": item.value.trade_date.isoformat(),
                        "rowId": str(row_id),
                    },
                    content_hash=item.content_hash,
                )

        publication = self._release_repository.publish_prepared(
            prepare_candidate=prepare, write_facts=write
        )
        return PublishedStockConnectMarketDaily(
            data_version=publication.data_version,
            inserted_count=len(prepared),
            unchanged_count=len(values) - len(prepared),
            channel=channel,
        )

    def ensure_hkex_instruments(
        self,
        *,
        records: Sequence[StockConnectInstrumentMaster],
        target_source_codes: set[str],
        source: StockConnectSourceObservation,
    ) -> dict[str, UUID]:
        """以稳定证券 ID 处理完整主档快照，但仅扩张到港股通活跃榜目标身份。"""
        values = tuple(records)
        if not values or len({item.source_instrument_code for item in values}) != len(values):
            raise ValueError("HKEX master records must be non-empty and unique by code")
        effective_dates = {item.effective_from for item in values}
        if len(effective_dates) != 1:
            raise ValueError("HKEX master snapshot must use exactly one effective date")
        stable_ids = [
            item.source_security_id for item in values if item.source_security_id is not None
        ]
        if len(set(stable_ids)) != len(stable_ids):
            raise ValueError("HKEX master stable security ids must be unique")
        snapshot_date = effective_dates.pop()
        try:
            snapshot_end = snapshot_date + timedelta(days=1)
        except OverflowError as error:
            raise ValueError("HKEX master snapshot date exceeds supported range") from error
        approval = _approved_source(self._approved_sources, source)
        resolved: dict[str, UUID] = {}
        with self._database.transaction() as session:
            now = datetime.now(UTC)
            session.execute(
                select(
                    func.pg_advisory_xact_lock(
                        func.hashtextextended(literal("stock-connect:hkex-master"), literal(0))
                    )
                )
            )
            ensure_dataset(
                session,
                code="market.stock_connect.instrument_master.reported",
                domain="stock_connect",
                grain="HKEX stable security id + venue code + effective trade date",
                now=now,
            )
            source_dataset_id = ensure_source_dataset(
                session,
                approval=approval,
                capability=source.capability,
                native_grain="HKEX stable security id + instrument code + effective trade date",
            )
            source_batch_id = record_source_batch(
                session, source=source, source_dataset_id=source_dataset_id, now=now
            )
            venue_id = _ensure_hkex_venue(session)
            tracked = {
                item.source_security_id: item
                for item in session.execute(
                    select(StockConnectHkexInstrumentIdentity).with_for_update()
                )
                .scalars()
                .all()
            }
            present_ids = set(stable_ids)
            for source_security_id, identity in tracked.items():
                if source_security_id not in present_ids:
                    _record_hkex_lifecycle_day(
                        session,
                        identity=identity,
                        snapshot_date=snapshot_date,
                        snapshot_end=snapshot_end,
                        status_code="RETIRED",
                        event_kind="HKEX_MASTER_ABSENT",
                        source_batch_id=source_batch_id,
                        evidence_ref=source.raw_uri,
                        now=now,
                    )
                    if snapshot_date > identity.last_seen_on:
                        identity.updated_at = now
                        session.execute(
                            update(MarketInstrument)
                            .where(
                                MarketInstrument.instrument_id == identity.instrument_id,
                                MarketInstrument.tradable_to.is_(None),
                            )
                            .values(tradable_to=snapshot_date)
                        )
            for value in values:
                source_security_id = value.source_security_id
                if source_security_id is None or (
                    value.source_instrument_code not in target_source_codes
                    and source_security_id not in tracked
                ):
                    continue
                identity = tracked.get(source_security_id)
                if identity is None:
                    identity = _create_hkex_identity(
                        session,
                        value=value,
                        venue_id=venue_id,
                        source_batch_id=source_batch_id,
                        snapshot_end=snapshot_end,
                        now=now,
                    )
                    tracked[source_security_id] = identity
                    # 标识与生命周期版本通过复合外键引用实体，但 ORM 未声明对象关系；先落身份根，
                    # 避免最终 flush 将版本行排在 `market_entity` 前而触发外键失败。
                    session.flush()
                else:
                    _observe_hkex_identity(
                        session,
                        identity=identity,
                        snapshot_date=snapshot_date,
                        snapshot_end=snapshot_end,
                        source_batch_id=source_batch_id,
                        now=now,
                    )
                _record_hkex_identifier_day(
                    session,
                    identity=identity,
                    venue_id=venue_id,
                    value=value,
                    snapshot_end=snapshot_end,
                    source_batch_id=source_batch_id,
                    now=now,
                )
                _record_hkex_lifecycle_day(
                    session,
                    identity=identity,
                    snapshot_date=snapshot_date,
                    snapshot_end=snapshot_end,
                    status_code="ACTIVE",
                    event_kind="HKEX_MASTER_PRESENT",
                    source_batch_id=source_batch_id,
                    evidence_ref=source.raw_uri,
                    now=now,
                )
                resolved[value.source_instrument_code] = UUID(str(identity.instrument_id))
        return resolved

    def publish_active_securities(
        self,
        *,
        channel: StockConnectChannel,
        records: Sequence[StockConnectActiveSecurity],
        source: StockConnectSourceObservation,
    ) -> PublishedStockConnectActiveSecurities:
        """发布活跃榜；近期身份必须精确，历史证据缺口则显式隔离并保留来源代码。"""
        values = tuple(records)
        if not values or len({(value.trade_date, value.rank_no) for value in values}) != len(
            values
        ):
            raise ValueError(
                "stock-connect active records must be non-empty and unique by date/rank"
            )
        approval = _approved_source(self._approved_sources, source)
        prepared: list[_PreparedActiveRecord] = []
        source_batch_id: UUID | None = None

        def prepare(session: Session) -> CanonicalReleaseCandidate:
            """在单事务内解析每行跨市场身份和统计依赖，拒绝代码猜测或孤立排行。"""
            nonlocal source_batch_id
            now = datetime.now(UTC)
            dataset_id = ensure_dataset(
                session,
                code=_ACTIVE_DATASET,
                domain="stock_connect",
                grain="channel + direction + trade date + rank + instrument",
                now=now,
            )
            methodology_id = ensure_methodology(
                session,
                code=_ACTIVE_METHODOLOGY,
                semantic_family="reported-stock-connect-active-security",
                mapping_version=_ACTIVE_MAPPING,
                documentation_ref=(
                    "docs/service-web/0010-stock-connect-center/service-data-sync.html"
                ),
            )
            market_methodology_id = ensure_methodology(
                session,
                code=_MARKET_METHODOLOGY,
                semantic_family="reported-stock-connect-channel-daily",
                mapping_version=_MARKET_MAPPING,
                documentation_ref=(
                    "docs/service-web/0010-stock-connect-center/service-data-sync.html"
                ),
            )
            source_dataset_id = ensure_source_dataset(
                session,
                approval=approval,
                capability=source.capability,
                native_grain="channel + direction + trade date + rank",
            )
            source_batch_id = record_source_batch(
                session, source=source, source_dataset_id=source_dataset_id, now=now
            )
            resolved = {
                (value.trade_date, value.rank_no): (
                    _resolve_active_instrument(session, channel=channel, value=value),
                    _resolve_market_release(
                        session,
                        channel=channel,
                        trade_date=value.trade_date,
                        methodology_version_id=market_methodology_id,
                    ),
                )
                for value in values
            }
            partition_key = _channel_partition(channel)
            normalization_run_id = record_normalization_run(
                session,
                dataset_id=dataset_id,
                partition_key=partition_key,
                source=source,
                source_batch_id=source_batch_id,
                mapping_version=_ACTIVE_MAPPING,
                now=now,
            )
            current = _current_active(
                session, channel=channel, methodology_version_id=methodology_id
            )
            incoming = {(value.trade_date, value.rank_no): value for value in values}
            prepared[:] = [
                _PreparedActiveRecord(
                    value=value,
                    instrument_id=resolved[(value.trade_date, value.rank_no)][0],
                    market_stat_release_id=resolved[(value.trade_date, value.rank_no)][1],
                    content_hash=_active_hash(
                        value,
                        resolved[(value.trade_date, value.rank_no)][0],
                        resolved[(value.trade_date, value.rank_no)][1],
                    ),
                    revision_no=current[(value.trade_date, value.rank_no)].revision_no + 1
                    if (value.trade_date, value.rank_no) in current
                    else 1,
                )
                for value in values
                if (value.trade_date, value.rank_no) not in current
                or _active_hash(
                    value,
                    resolved[(value.trade_date, value.rank_no)][0],
                    resolved[(value.trade_date, value.rank_no)][1],
                )
                != current[(value.trade_date, value.rank_no)].content_hash
            ]
            return _active_candidate(
                session,
                dataset_id=dataset_id,
                methodology_version_id=methodology_id,
                normalization_run_id=normalization_run_id,
                channel=channel,
                current=current,
                incoming=incoming,
                changed={(item.value.trade_date, item.value.rank_no): item for item in prepared},
                source_batch_id=source_batch_id,
                now=now,
            )

        def write(session: Session, candidate: CanonicalReleaseCandidate, release_id: UUID) -> None:
            """关闭同日期名次旧知识版本，并保存精确工具与统计 release 外键。"""
            if source_batch_id is None:
                raise AssertionError(
                    "stock-connect active preparation did not resolve source batch"
                )
            for item in prepared:
                session.execute(
                    update(StockConnectActiveSecurityRevision)
                    .where(
                        StockConnectActiveSecurityRevision.channel == channel.channel,
                        StockConnectActiveSecurityRevision.direction == channel.direction,
                        StockConnectActiveSecurityRevision.trade_date == item.value.trade_date,
                        StockConnectActiveSecurityRevision.rank_no == item.value.rank_no,
                        StockConnectActiveSecurityRevision.methodology_version_id
                        == candidate.methodology_version_id,
                        StockConnectActiveSecurityRevision.known_to.is_(None),
                    )
                    .values(known_to=candidate.created_at)
                )
                row_id = uuid4()
                session.execute(
                    insert(StockConnectActiveSecurityRevision).values(
                        trade_date=item.value.trade_date,
                        row_id=row_id,
                        channel=channel.channel,
                        direction=channel.direction,
                        instrument_id=item.instrument_id,
                        source_instrument_code=item.value.source_instrument_code,
                        source_instrument_name=item.value.source_instrument_name,
                        identity_status=(
                            "RESOLVED" if item.instrument_id is not None else "SOURCE_UNRESOLVED"
                        ),
                        market_stat_release_id=item.market_stat_release_id,
                        rank_no=item.value.rank_no,
                        buy_amount=item.value.buy_amount,
                        sell_amount=item.value.sell_amount,
                        turnover_amount=item.value.turnover_amount,
                        currency=item.value.currency,
                        field_availability=dict(item.value.field_availability),
                        methodology_version_id=candidate.methodology_version_id,
                        release_id=release_id,
                        revision_no=item.revision_no,
                        source_batch_id=source_batch_id,
                        source_published_at=None,
                        source_time_precision="UNKNOWN",
                        public_usable_at=candidate.created_at,
                        availability_basis="OBSERVED_ONLY",
                        known_from=candidate.created_at,
                        known_to=None,
                        content_hash=item.content_hash,
                        quality_status="passed",
                    )
                )
                record_manifest(
                    session,
                    normalization_run_id=candidate.normalization_run_id,
                    record_key_hash=_active_record_key(
                        channel, item.value.trade_date, item.value.rank_no
                    ),
                    canonical_table=StockConnectActiveSecurityRevision.__tablename__,
                    canonical_pk={
                        "tradeDate": item.value.trade_date.isoformat(),
                        "rowId": str(row_id),
                    },
                    content_hash=item.content_hash,
                )

        publication = self._release_repository.publish_prepared(
            prepare_candidate=prepare, write_facts=write
        )
        return PublishedStockConnectActiveSecurities(
            data_version=publication.data_version,
            inserted_count=len(prepared),
            unchanged_count=len(values) - len(prepared),
            channel=channel,
        )


def _approved_source(
    approvals: Mapping[str, StockConnectSourceApproval], source: StockConnectSourceObservation
) -> StockConnectSourceApproval:
    """读取来源批准项；adapter 已实现不等于其权利和留存边界已获批准。"""
    approval = approvals.get(source.provider_id)
    if approval is None:
        raise ValueError("stock-connect source provider is not approved for publication")
    return approval


def _ensure_hkex_venue(session: Session) -> UUID:
    """幂等确保 HKEX 场所字典存在，正式名称和时区不由证券文件猜测。"""
    existing = session.execute(
        select(TradingVenue.venue_id).where(TradingVenue.code == "HKEX")
    ).scalar_one_or_none()
    if existing is not None:
        return UUID(str(existing))
    venue_id = uuid5(NAMESPACE_URL, "quant-v2:trading-venue:HKEX")
    session.add(
        TradingVenue(
            venue_id=venue_id,
            mic="XHKG",
            code="HKEX",
            name="Hong Kong Exchanges and Clearing Limited",
            timezone="Asia/Hong_Kong",
            country="HK",
            active=True,
        )
    )
    session.flush()
    return venue_id


def _create_hkex_identity(
    session: Session,
    *,
    value: StockConnectInstrumentMaster,
    venue_id: UUID,
    source_batch_id: UUID,
    snapshot_end: date,
    now: datetime,
) -> StockConnectHkexInstrumentIdentity:
    """仅用官方稳定证券 ID 创建永久实体；代码和名称不会进入 UUID 输入。"""
    if value.source_security_id is None:
        raise AssertionError("HKEX stable security id is required to create identity")
    instrument_id = uuid5(
        NAMESPACE_URL,
        f"quant-v2:HKEX:EQUITY:security-id:{value.source_security_id}",
    )
    session.add(
        MarketEntity(
            entity_id=instrument_id,
            entity_kind="EQUITY",
            created_at=now,
            retired_at=None,
        )
    )
    session.add(
        MarketInstrument(
            instrument_id=instrument_id,
            instrument_kind="EQUITY",
            primary_venue_id=venue_id,
            tradable_from=value.effective_from,
            tradable_to=snapshot_end,
        )
    )
    identity = StockConnectHkexInstrumentIdentity(
        source_security_id=value.source_security_id,
        instrument_id=instrument_id,
        first_seen_on=value.effective_from,
        last_seen_on=value.effective_from,
        first_source_batch_id=source_batch_id,
        last_source_batch_id=source_batch_id,
        created_at=now,
        updated_at=now,
    )
    session.add(identity)
    return identity


def _observe_hkex_identity(
    session: Session,
    *,
    identity: StockConnectHkexInstrumentIdentity,
    snapshot_date: date,
    snapshot_end: date,
    source_batch_id: UUID,
    now: datetime,
) -> None:
    """合并乱序快照的观察边界，并让顶层工具覆盖已确认的最早至最新日期。"""
    identity.first_seen_on = min(identity.first_seen_on, snapshot_date)
    identity.last_seen_on = max(identity.last_seen_on, snapshot_date)
    identity.last_source_batch_id = source_batch_id
    identity.updated_at = now
    instrument = session.get(MarketInstrument, identity.instrument_id)
    if instrument is None:
        raise ValueError("HKEX stable identity has no market instrument")
    instrument.tradable_from = (
        snapshot_date
        if instrument.tradable_from is None
        else min(instrument.tradable_from, snapshot_date)
    )
    instrument.tradable_to = (
        snapshot_end
        if instrument.tradable_to is None
        else max(instrument.tradable_to, snapshot_end)
    )


def _record_hkex_identifier_day(
    session: Session,
    *,
    identity: StockConnectHkexInstrumentIdentity,
    venue_id: UUID,
    value: StockConnectInstrumentMaster,
    snapshot_end: date,
    source_batch_id: UUID,
    now: datetime,
) -> None:
    """为快照业务日写一个半开代码版本，稳定实体与代码复用因此互不混淆。"""
    current = (
        session.execute(
            select(InstrumentIdentifierVersion)
            .where(
                InstrumentIdentifierVersion.entity_kind == "EQUITY",
                InstrumentIdentifierVersion.identifier_scheme == _IDENTIFIER_SCHEME,
                or_(
                    InstrumentIdentifierVersion.entity_id == identity.instrument_id,
                    and_(
                        InstrumentIdentifierVersion.venue_id == venue_id,
                        InstrumentIdentifierVersion.identifier_value
                        == value.source_instrument_code,
                    ),
                ),
                InstrumentIdentifierVersion.effective_from <= value.effective_from,
                (InstrumentIdentifierVersion.effective_to.is_(None))
                | (InstrumentIdentifierVersion.effective_to > value.effective_from),
                InstrumentIdentifierVersion.known_to.is_(None),
            )
            .with_for_update()
        )
        .scalars()
        .all()
    )
    exact = [
        item
        for item in current
        if UUID(str(item.entity_id)) == UUID(str(identity.instrument_id))
        and item.venue_id == venue_id
        and item.identifier_value == value.source_instrument_code
        and item.effective_from == value.effective_from
        and item.effective_to == snapshot_end
    ]
    if len(exact) > 1:
        raise ValueError("HKEX stable identity has duplicate current code versions")
    if exact:
        return
    for item in current:
        session.execute(
            update(InstrumentIdentifierVersion)
            .where(InstrumentIdentifierVersion.version_id == item.version_id)
            .values(known_to=now)
        )
    session.add(
        InstrumentIdentifierVersion(
            version_id=uuid5(
                NAMESPACE_URL,
                "quant-v2:HKEX:identifier:"
                f"{identity.source_security_id}:{value.source_instrument_code}:"
                f"{value.effective_from.isoformat()}:{source_batch_id}",
            ),
            entity_id=identity.instrument_id,
            entity_kind="EQUITY",
            venue_id=venue_id,
            identifier_scheme=_IDENTIFIER_SCHEME,
            identifier_value=value.source_instrument_code,
            effective_from=value.effective_from,
            effective_to=snapshot_end,
            known_from=now,
            known_to=None,
            source_time_precision="DATE_ONLY",
            source_batch_id=source_batch_id,
        )
    )


def _record_hkex_lifecycle_day(
    session: Session,
    *,
    identity: StockConnectHkexInstrumentIdentity,
    snapshot_date: date,
    snapshot_end: date,
    status_code: str,
    event_kind: str,
    source_batch_id: UUID,
    evidence_ref: str,
    now: datetime,
) -> None:
    """按完整快照在场或缺席写日期化生命周期，乱序回补不会重新打开错误区间。"""
    current = (
        session.execute(
            select(InstrumentLifecycleVersion)
            .where(
                InstrumentLifecycleVersion.entity_id == identity.instrument_id,
                InstrumentLifecycleVersion.effective_from <= snapshot_date,
                (InstrumentLifecycleVersion.effective_to.is_(None))
                | (InstrumentLifecycleVersion.effective_to > snapshot_date),
                InstrumentLifecycleVersion.known_to.is_(None),
            )
            .with_for_update()
        )
        .scalars()
        .all()
    )
    exact = [
        item
        for item in current
        if item.status_code == status_code
        and item.effective_from == snapshot_date
        and item.effective_to == snapshot_end
    ]
    if len(exact) > 1:
        raise ValueError("HKEX stable identity has duplicate lifecycle versions")
    if exact:
        return
    for item in current:
        session.execute(
            update(InstrumentLifecycleVersion)
            .where(InstrumentLifecycleVersion.version_id == item.version_id)
            .values(known_to=now)
        )
    session.add(
        InstrumentLifecycleVersion(
            version_id=uuid5(
                NAMESPACE_URL,
                "quant-v2:HKEX:lifecycle:"
                f"{identity.source_security_id}:{snapshot_date.isoformat()}:"
                f"{status_code}:{source_batch_id}",
            ),
            entity_id=identity.instrument_id,
            entity_kind="EQUITY",
            status_code=status_code,
            event_kind=event_kind,
            effective_from=snapshot_date,
            effective_to=snapshot_end,
            known_from=now,
            known_to=None,
            evidence_ref=evidence_ref,
            source_batch_id=source_batch_id,
        )
    )


def _resolve_regime(
    session: Session, *, channel: StockConnectChannel, trade_date: date
) -> StockConnectDisclosureRegime:
    """按通道、方向和事实日解析唯一披露制度，制度缺失绝不由当前字段集猜测。"""
    rows = (
        session.execute(
            select(StockConnectDisclosureRegime).where(
                StockConnectDisclosureRegime.channel == channel.channel,
                StockConnectDisclosureRegime.direction == channel.direction,
                StockConnectDisclosureRegime.effective_from <= trade_date,
                (StockConnectDisclosureRegime.effective_to.is_(None))
                | (StockConnectDisclosureRegime.effective_to > trade_date),
            )
        )
        .scalars()
        .all()
    )
    if len(rows) != 1:
        raise ValueError("stock-connect disclosure regime is missing or ambiguous")
    return rows[0]


def _ensure_default_regimes(
    session: Session,
    *,
    channel: StockConnectChannel,
    methodology_version_id: UUID,
) -> None:
    """幂等登记已评审官方披露制度，避免全新环境依赖人工 SQL 才能首次发布。"""
    existing = session.execute(
        select(StockConnectDisclosureRegime.regime_id).where(
            StockConnectDisclosureRegime.channel == channel.channel,
            StockConnectDisclosureRegime.direction == channel.direction,
        )
    ).first()
    if existing is not None:
        return
    common_fields = [
        "buy_amount",
        "sell_amount",
        "turnover_amount",
        "trade_count",
        "etf_turnover_amount",
    ]
    if channel.direction == "NORTHBOUND":
        regimes = (
            (
                date(2014, 11, 17) if channel.channel == "SH" else date(2016, 12, 5),
                date(2024, 8, 19),
                common_fields,
                "HKEX Stock Connect Daily Statistics disclosure before 2024-08-19",
            ),
            (
                date(2024, 8, 19),
                None,
                ["turnover_amount", "trade_count", "etf_turnover_amount"],
                "HKEX Northbound disclosure adjustment effective 2024-08-19",
            ),
        )
    else:
        regimes = (
            (
                date(2014, 11, 17) if channel.channel == "SH" else date(2016, 12, 5),
                None,
                common_fields,
                "HKEX Stock Connect Southbound Daily Statistics disclosure",
            ),
        )
    for effective_from, effective_to, available_fields, evidence_ref in regimes:
        stable_key = (
            f"stock-connect-regime:{channel.channel}:{channel.direction}:"
            f"{effective_from.isoformat()}"
        )
        session.add(
            StockConnectDisclosureRegime(
                regime_id=uuid5(NAMESPACE_URL, stable_key),
                channel=channel.channel,
                direction=channel.direction,
                effective_from=effective_from,
                effective_to=effective_to,
                available_fields=available_fields,
                methodology_version_id=methodology_version_id,
                evidence_ref=evidence_ref,
            )
        )
    session.flush()


def _validate_regime_value(
    value: StockConnectMarketDaily, regime: StockConnectDisclosureRegime
) -> None:
    """验证非空数值只出现在制度白名单；缺失仍由状态保持为可审计空值。"""
    available = set(regime.available_fields)
    unknown = available.difference(_METRIC_FIELDS)
    if unknown:
        raise ValueError("stock-connect disclosure regime contains unsupported fields")
    reported = {
        "buy_amount": value.buy_amount,
        "sell_amount": value.sell_amount,
        "turnover_amount": value.turnover_amount,
        "net_buy_amount": value.net_buy_amount,
        "quota_balance": value.quota_balance,
        "trade_count": value.trade_count,
        "etf_turnover_amount": value.etf_turnover_amount,
    }
    if any(amount is not None and field not in available for field, amount in reported.items()):
        raise ValueError("stock-connect value contradicts disclosure regime")


def _current_market(
    session: Session, *, channel: StockConnectChannel, methodology_version_id: UUID
) -> dict[date, StockConnectChannelDailyRevision]:
    """读取一个通道方向的当前统计知识；重复日期表明 revision 约束已失效。"""
    rows = (
        session.execute(
            select(StockConnectChannelDailyRevision).where(
                StockConnectChannelDailyRevision.channel == channel.channel,
                StockConnectChannelDailyRevision.direction == channel.direction,
                StockConnectChannelDailyRevision.methodology_version_id == methodology_version_id,
                StockConnectChannelDailyRevision.known_to.is_(None),
            )
        )
        .scalars()
        .all()
    )
    result = {row.trade_date: row for row in rows}
    if len(result) != len(rows):
        raise ValueError("stock-connect market current revision is ambiguous")
    return result


def _resolve_active_instrument(
    session: Session, *, channel: StockConnectChannel, value: StockConnectActiveSecurity
) -> UUID | None:
    """按方向使用股票权威身份或港股工具身份解析，不能把相同代码跨市场合并。"""
    if channel.direction == "NORTHBOUND":
        exchange = "SSE" if channel.channel == "SH" else "SZSE"
        rows = (
            session.execute(
                select(EquityInstrument.instrument_id)
                .join(
                    MarketInstrument,
                    MarketInstrument.instrument_id == EquityInstrument.instrument_id,
                )
                .join(
                    EquityIdentifierVersion,
                    EquityIdentifierVersion.security_id == EquityInstrument.security_id,
                )
                .where(
                    EquityIdentifierVersion.exchange == exchange,
                    EquityIdentifierVersion.symbol == value.source_instrument_code,
                    EquityIdentifierVersion.identity_state == "CONFIRMED",
                    EquityIdentifierVersion.effective_from <= value.trade_date,
                    (EquityIdentifierVersion.effective_to.is_(None))
                    | (EquityIdentifierVersion.effective_to > value.trade_date),
                    EquityIdentifierVersion.known_to.is_(None),
                )
            )
            .scalars()
            .all()
        )
    else:
        rows = (
            session.execute(
                select(MarketInstrument.instrument_id)
                .join(
                    InstrumentIdentifierVersion,
                    InstrumentIdentifierVersion.entity_id == MarketInstrument.instrument_id,
                )
                .join(
                    StockConnectHkexInstrumentIdentity,
                    StockConnectHkexInstrumentIdentity.instrument_id
                    == MarketInstrument.instrument_id,
                )
                .join(TradingVenue, TradingVenue.venue_id == InstrumentIdentifierVersion.venue_id)
                .where(
                    MarketInstrument.instrument_kind == "EQUITY",
                    TradingVenue.code == "HKEX",
                    InstrumentIdentifierVersion.entity_kind == "EQUITY",
                    InstrumentIdentifierVersion.identifier_scheme == _IDENTIFIER_SCHEME,
                    InstrumentIdentifierVersion.identifier_value == value.source_instrument_code,
                    InstrumentIdentifierVersion.effective_from <= value.trade_date,
                    (InstrumentIdentifierVersion.effective_to.is_(None))
                    | (InstrumentIdentifierVersion.effective_to > value.trade_date),
                    InstrumentIdentifierVersion.known_to.is_(None),
                )
            )
            .scalars()
            .all()
        )
    candidates = {UUID(str(row)) for row in rows}
    if len(candidates) > 1:
        raise ValueError("stock-connect active instrument identity is ambiguous")
    if candidates:
        return candidates.pop()
    return None


def _resolve_market_release(
    session: Session,
    *,
    channel: StockConnectChannel,
    trade_date: date,
    methodology_version_id: UUID,
) -> UUID:
    """取得同日当前通道统计 release，使活跃榜消费者可复现其依赖的市场版本。"""
    rows = (
        session.execute(
            select(StockConnectChannelDailyRevision.release_id).where(
                StockConnectChannelDailyRevision.channel == channel.channel,
                StockConnectChannelDailyRevision.direction == channel.direction,
                StockConnectChannelDailyRevision.trade_date == trade_date,
                StockConnectChannelDailyRevision.methodology_version_id == methodology_version_id,
                StockConnectChannelDailyRevision.known_to.is_(None),
            )
        )
        .scalars()
        .all()
    )
    candidates = {UUID(str(row)) for row in rows}
    if len(candidates) != 1:
        raise ValueError("stock-connect active record requires one current market-stat release")
    return candidates.pop()


def _current_active(
    session: Session, *, channel: StockConnectChannel, methodology_version_id: UUID
) -> dict[tuple[date, int], StockConnectActiveSecurityRevision]:
    """读取通道方向当前活跃榜知识，重复日期名次代表无法确定的来源事实。"""
    rows = (
        session.execute(
            select(StockConnectActiveSecurityRevision).where(
                StockConnectActiveSecurityRevision.channel == channel.channel,
                StockConnectActiveSecurityRevision.direction == channel.direction,
                StockConnectActiveSecurityRevision.methodology_version_id == methodology_version_id,
                StockConnectActiveSecurityRevision.known_to.is_(None),
            )
        )
        .scalars()
        .all()
    )
    result = {(row.trade_date, row.rank_no): row for row in rows}
    if len(result) != len(rows):
        raise ValueError("stock-connect active current revision is ambiguous")
    return result


def _market_candidate(
    session: Session,
    *,
    dataset_id: UUID,
    methodology_version_id: UUID,
    normalization_run_id: UUID,
    channel: StockConnectChannel,
    current: Mapping[date, StockConnectChannelDailyRevision],
    incoming: Mapping[date, StockConnectMarketDaily],
    changed: Mapping[date, _PreparedMarketRecord],
    source_batch_id: UUID,
    now: datetime,
) -> CanonicalReleaseCandidate:
    """把未变化统计及新 revision 合成完整通道分区 release，保留原始来源批次。"""
    records: list[CanonicalLineageRecord] = []
    dates = sorted({*current, *incoming})
    for trade_date in dates:
        item = changed.get(trade_date)
        if item is None:
            existing = current[trade_date]
            content_hash = existing.content_hash
            batch_id = UUID(str(existing.source_batch_id))
        else:
            content_hash = item.content_hash
            batch_id = source_batch_id
        records.append(
            CanonicalLineageRecord(
                record_key_hash=_market_record_key(channel, trade_date),
                content_hash=content_hash,
                source_batch_id=batch_id,
                transform_hash=hashlib.sha256(_MARKET_MAPPING.encode()).hexdigest(),
            )
        )
    return _candidate(
        session,
        dataset_id=dataset_id,
        dataset_code=_MARKET_DATASET,
        methodology_version_id=methodology_version_id,
        normalization_run_id=normalization_run_id,
        partition_key=_channel_partition(channel),
        records=records,
        dates=dates,
        checkpoint_position={"tradeDate": max(dates).isoformat()},
        quality=CanonicalQualityDecision(
            status="passed",
            policy_code="stock-connect.market.quality",
            policy_version=1,
            rules=(CanonicalQualityRule("disclosure-regime-gate", "blocking", True),),
        ),
        now=now,
    )


def _active_candidate(
    session: Session,
    *,
    dataset_id: UUID,
    methodology_version_id: UUID,
    normalization_run_id: UUID,
    channel: StockConnectChannel,
    current: Mapping[tuple[date, int], StockConnectActiveSecurityRevision],
    incoming: Mapping[tuple[date, int], StockConnectActiveSecurity],
    changed: Mapping[tuple[date, int], _PreparedActiveRecord],
    source_batch_id: UUID,
    now: datetime,
) -> CanonicalReleaseCandidate:
    """把现有活跃榜和本次变化组成 release；缺席名次不被擅自解释成下榜。"""
    records: list[CanonicalLineageRecord] = []
    keys = sorted({*current, *incoming})
    for trade_date, rank_no in keys:
        item = changed.get((trade_date, rank_no))
        if item is None:
            existing = current[(trade_date, rank_no)]
            content_hash = existing.content_hash
            batch_id = UUID(str(existing.source_batch_id))
        else:
            content_hash = item.content_hash
            batch_id = source_batch_id
        records.append(
            CanonicalLineageRecord(
                record_key_hash=_active_record_key(channel, trade_date, rank_no),
                content_hash=content_hash,
                source_batch_id=batch_id,
                transform_hash=hashlib.sha256(_ACTIVE_MAPPING.encode()).hexdigest(),
            )
        )
    dates = sorted({trade_date for trade_date, _rank_no in keys})
    latest_date, latest_rank = max(keys)
    return _candidate(
        session,
        dataset_id=dataset_id,
        dataset_code=_ACTIVE_DATASET,
        methodology_version_id=methodology_version_id,
        normalization_run_id=normalization_run_id,
        partition_key=_channel_partition(channel),
        records=records,
        dates=dates,
        checkpoint_position={"tradeDate": latest_date.isoformat(), "rankNo": latest_rank},
        quality=CanonicalQualityDecision(
            status="passed",
            policy_code="stock-connect.active-security.quality",
            policy_version=1,
            rules=(
                CanonicalQualityRule("resolved-instrument-only", "blocking", True),
                CanonicalQualityRule("market-release-bound", "blocking", True),
            ),
        ),
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
    quality: CanonicalQualityDecision,
    now: datetime,
) -> CanonicalReleaseCandidate:
    """构造带悲观 fencing token 的标准 release 候选，避免并发任务覆盖新检查点。"""
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
        quality=quality,
        fact_min=min(dates),
        fact_max=max(dates),
        checkpoint_kind="published",
        checkpoint_position=checkpoint_position,
        expected_fencing_token=0 if fencing_token is None else int(fencing_token),
        created_at=now,
    )


def _channel_partition(channel: StockConnectChannel) -> str:
    """生成由通道和方向组成的稳定分区键，不混淆北向和南向币种及披露制度。"""
    return f"channel:{channel.channel}:direction:{channel.direction}"


def _market_record_key(channel: StockConnectChannel, trade_date: date) -> str:
    """计算通道统计业务键摘要，日期外还固定方向以阻止南北向混写。"""
    return _hash_text(f"{channel.channel}:{channel.direction}:{trade_date.isoformat()}")


def _active_record_key(channel: StockConnectChannel, trade_date: date, rank_no: int) -> str:
    """计算活跃榜名次业务键摘要，同日名次在单通道方向内唯一。"""
    return _hash_text(f"{channel.channel}:{channel.direction}:{trade_date.isoformat()}:{rank_no}")


def _market_hash(value: StockConnectMarketDaily, regime_id: UUID) -> str:
    """计算包含制度版本的通道统计摘要，制度更正即使数值不变也必须产生新 revision。"""
    return _hash_payload(
        {
            "tradeDate": value.trade_date.isoformat(),
            "buyAmount": _decimal(value.buy_amount),
            "sellAmount": _decimal(value.sell_amount),
            "turnoverAmount": _decimal(value.turnover_amount),
            "netBuyAmount": _decimal(value.net_buy_amount),
            "quotaBalance": _decimal(value.quota_balance),
            "tradeCount": value.trade_count,
            "etfTurnoverAmount": _decimal(value.etf_turnover_amount),
            "fieldAvailability": dict(value.field_availability),
            "currency": value.currency,
            "availabilityStatus": value.availability_status,
            "regimeId": str(regime_id),
        }
    )


def _active_hash(
    value: StockConnectActiveSecurity,
    instrument_id: UUID | None,
    market_stat_release_id: UUID,
) -> str:
    """计算包含精确工具和统计依赖 release 的活跃榜摘要，依赖变更不会静默漂移。"""
    return _hash_payload(
        {
            "instrumentCode": value.source_instrument_code,
            "instrumentName": value.source_instrument_name,
            "tradeDate": value.trade_date.isoformat(),
            "rankNo": value.rank_no,
            "buyAmount": _decimal(value.buy_amount),
            "sellAmount": _decimal(value.sell_amount),
            "turnoverAmount": _decimal(value.turnover_amount),
            "currency": value.currency,
            "instrumentId": None if instrument_id is None else str(instrument_id),
            "fieldAvailability": dict(value.field_availability),
            "marketStatReleaseId": str(market_stat_release_id),
        }
    )


def _hash_payload(payload: Mapping[str, object]) -> str:
    """以规范 JSON 生成业务内容摘要，空值与真实零值都稳定参与 revision 身份。"""
    return _hash_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )


def _hash_text(value: str) -> str:
    """计算 UTF-8 文本 SHA-256，逻辑键与内容键都使用同一确定性实现。"""
    return hashlib.sha256(value.encode()).hexdigest()


def _decimal(value: object) -> str | None:
    """把精确金额稳定投影为字符串，避免 JSON 浮点显示格式改变内容摘要。"""
    return None if value is None else str(value)
