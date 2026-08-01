"""使用 `ORM` 表达式实现的个股行情与参考数据版本化仓储。

日线、周线、月线、累计复权因子、公司行动和公司概况分别保存并独立发布；它们不能互相
补值或跨物理表聚合。事实日身份按双时间主数据解析，退市后的同代码行情必须等待明确
确认；内容哈希仅在业务值变化时追加 `revision`，重复来源抓取不会制造伪修订。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, cast
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5
from zoneinfo import ZoneInfo

from sqlalchemy import Select, and_, func, insert, or_, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.orm import Session

from service_data_sync.application.ports.canonical_release import CanonicalLineageRecord
from service_data_sync.application.ports.market_data import (
    EquityAvailabilityObservation,
    EquityBarWindowCoveragePublication,
    EquityDatasetPublication,
    EquityIdentityReadConflictError,
    EquityMarketDataRepository,
    EquityPublicationSource,
    EquitySourceObservation,
    PublishedDailyBars,
    PublishedEquityDataset,
    StoredAdjustmentFactor,
    StoredCompanyProfile,
    StoredCorporateAction,
    StoredEquityBar,
    StoredEquityInstrument,
)
from service_data_sync.domain.equity import (
    EquityAdjustmentFactor,
    EquityBarPeriod,
    EquityCompanyProfile,
    EquityCorporateAction,
    EquityDailyBar,
    EquityIdentifier,
    EquityPeriodBar,
)
from service_data_sync.domain.equity_master import EquityIdentityResolutionStatus
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.database.fenced_execution import current_fenced_execution
from service_data_sync.infrastructure.database.models.canonical import (
    CanonicalDataset,
    DatasetRelease,
    MethodologyVersion,
    NormalizationRun,
)
from service_data_sync.infrastructure.database.models.provenance.source_batch import SourceBatch
from service_data_sync.infrastructure.persistence.bar_window_coverage import (
    BarWindowIdentity,
    publish_bar_window_coverage,
    resolve_bar_window_identity,
)
from service_data_sync.infrastructure.persistence.canonical_release_repository import (
    SqlAlchemyCanonicalReleaseRepository,
)
from service_data_sync.infrastructure.persistence.equity_identity_resolver import (
    require_single_confirmed_identity_on_connection,
    resolve_identity_on_connection,
)
from service_data_sync.infrastructure.persistence.event_window_coverage import (
    EventCoverageIdentity,
    EventCoverageRecords,
    publish_event_window_coverages,
    resolve_event_coverage_identities,
)
from service_data_sync.infrastructure.persistence.legacy_canonical_release_bridge import (
    publish_legacy_snapshot,
)
from service_data_sync.infrastructure.persistence.source_batch import record_source_observation

from ..database.models.equity.identity.equity_identifier_version import (
    EquityIdentifierVersion,
)
from ..database.models.equity.identity.equity_instrument import EquityInstrument
from ..database.models.equity.identity.equity_listing_status_version import (
    EquityListingStatusVersion,
)
from ..database.models.equity.identity.equity_profile_version import EquityProfileVersion
from ..database.models.equity.market_data.equity_adjustment_factor import (
    EquityAdjustmentFactor as EquityAdjustmentFactorModel,
)
from ..database.models.equity.market_data.equity_corporate_action_version import (
    EquityCorporateActionVersion,
)
from ..database.models.equity.market_data.equity_daily_bar import (
    EquityDailyBar as EquityDailyBarModel,
)
from ..database.models.equity.market_data.equity_monthly_bar import EquityMonthlyBar
from ..database.models.equity.market_data.equity_sync_checkpoint import EquitySyncCheckpoint
from ..database.models.equity.market_data.equity_weekly_bar import EquityWeeklyBar
from ..database.models.publication.dataset_availability_observation import (
    DatasetAvailabilityObservation,
)
from ..database.models.publication.dataset_publication import DatasetPublication
from ..database.models.publication.equity_bar_window_coverage import (
    EquityBarWindowCoverage,
)

_DAILY_DATASET = "equity.bar.1d.raw"
_FACTOR_DATASET = "equity.adjustment_factor"
_ACTION_DATASET = "equity.corporate_action"
_PROFILE_DATASET = "equity.profile"
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_RELEASE_MAPPING_VERSION = "equity-market-data-release-bridge-v1"
_PERIOD_MODELS = {
    EquityBarPeriod.WEEK_1: EquityWeeklyBar,
    EquityBarPeriod.MONTH_1: EquityMonthlyBar,
}


def _security_partition_key(security_id: int) -> str:
    """把永久证券主键编码为不会随交易代码复用的 publication 分区。"""
    if security_id <= 0:
        raise ValueError("security_id must be positive")
    return f"security:{security_id}"


def _availability_partition_key(identifier: EquityIdentifier, start: date, end: date) -> str:
    """把无身份事实窗口编码为精确查询分区，避免空观测跨日期误用。"""
    if start > end:
        raise ValueError("start must not be after end")
    return f"{identifier.qualified_symbol}:{start.isoformat()}:{end.isoformat()}"


def _current_publication_statement(
    *,
    dataset: str,
    partition_key: str,
) -> Select[tuple[UUID, datetime, str]]:
    """构造质量通过且尚未被替换的 publication 查询，并保留真实质量状态。"""
    return select(
        DatasetPublication.data_version,
        DatasetPublication.published_at,
        DatasetPublication.quality_status,
    ).where(
        DatasetPublication.dataset == dataset,
        DatasetPublication.partition_key == partition_key,
        DatasetPublication.quality_status == "passed",
        DatasetPublication.superseded_at.is_(None),
    )


def _at_knowledge_time(
    statement: Select[Any],
    *,
    model: Any,
    known_at: datetime | None,
) -> Select[Any]:
    """把 revision 查询固定到 publication 知识截止点，避免发布切换竞态混入新事实。"""
    if known_at is None:
        return statement.where(model.valid_to.is_(None))
    if known_at.tzinfo is None:
        raise ValueError("known_at must include a timezone")
    return statement.where(
        model.valid_from <= known_at,
        or_(model.valid_to.is_(None), model.valid_to > known_at),
    )


class PossibleCodeReuseError(ValueError):
    """表示退市后出现同代码行情，必须等待主数据显式确认而非误绑旧身份。"""


class SqlAlchemyEquityMarketDataRepository(EquityMarketDataRepository):
    """持久化带来源链接的日线，采用追加修订和原子发布切换。"""

    def __init__(self, database: DatabaseClient) -> None:
        """使用服务私有 Session 工厂，不向应用调用方暴露它。"""
        self._database = database
        self._release_repository = SqlAlchemyCanonicalReleaseRepository(database)

    def publish_daily_bars(
        self,
        *,
        identifier: EquityIdentifier,
        bars: Sequence[EquityDailyBar],
        source: EquitySourceObservation,
        start: date,
        end: date,
    ) -> PublishedDailyBars:
        """原子发布日线 DATA 或合法零记录 coverage，并冻结完整来源与身份窗口。"""
        if start > end:
            raise ValueError("daily bar coverage window is invalid")
        if source.capability != EquityBarPeriod.DAY_1.capability:
            raise ValueError("daily bar source capability is invalid")
        if any(not start <= bar.trade_date <= end for bar in bars):
            raise ValueError("daily bar falls outside the requested coverage window")
        now = datetime.now(UTC)
        with self._database.transaction() as connection:
            source_batch_id = self._record_source_batch(
                connection,
                capability=_DAILY_DATASET,
                provider_id=source.provider_id,
                source_payload_sha256=source.source_payload_sha256,
                raw_uri=source.raw_uri,
                observed_at=source.observed_at,
                created_at=now,
                upstream_source=source.upstream_source,
                adapter_version=source.adapter_version,
                schema_fingerprint=source.schema_fingerprint,
            )
            coverage_identity = resolve_bar_window_identity(
                connection,
                period=EquityBarPeriod.DAY_1,
                identifier=identifier,
                start=start,
                end=end,
            )
            instrument = self._bar_window_instrument_on_connection(
                connection,
                identifier=identifier,
                identity=coverage_identity,
            )
            inserted_count, unchanged_count = (
                self._write_revisions(
                    connection,
                    security_id=instrument.security_id,
                    bars=bars,
                    source_batch_id=source_batch_id,
                    observed_at=source.observed_at,
                )
                if bars
                else (0, 0)
            )
            data_publication_version = (
                self._publish(
                    connection,
                    dataset=_DAILY_DATASET,
                    instrument=instrument,
                    source_batch_id=source_batch_id,
                    published_at=now,
                )
                if bars
                else None
            )
            coverage = publish_bar_window_coverage(
                connection,
                release_repository=self._release_repository,
                period=EquityBarPeriod.DAY_1,
                identity=coverage_identity,
                source=source,
                source_batch_id=source_batch_id,
                record_count=len(bars),
                data_publication_version=data_publication_version,
                now=now,
            )
            # 新 publication 与旧诊断观察共享事务；零记录 coverage 取代成功空集的可变状态行。
            self._clear_daily_bar_availability_on_connection(
                connection,
                identifier=identifier,
                start=start,
                end=end,
                cleared_at=now,
            )
        return PublishedDailyBars(
            data_version=coverage.data_version,
            inserted_count=inserted_count,
            unchanged_count=unchanged_count,
            instrument=instrument,
            coverage_version=coverage.coverage_version,
            source_batch_id=coverage.source_batch_id,
            publication_kind=coverage.publication_kind,
        )

    def record_daily_bar_availability(
        self,
        *,
        identifier: EquityIdentifier,
        start: date,
        end: date,
        availability: str,
        reason_code: str,
        provider_id: str | None,
        observed_at: datetime,
    ) -> EquityAvailabilityObservation:
        """持久化旧任务或运维探针诊断，不创建事实且不构成成功覆盖。"""
        if availability not in {"empty", "source_unavailable"}:
            raise ValueError("daily-bar availability is invalid")
        if observed_at.tzinfo is None:
            raise ValueError("observed_at must include a timezone")
        partition_key = _availability_partition_key(identifier, start, end)
        with self._database.transaction() as connection:
            # 同一请求窗口只保留一个当前观测；历史用于诊断，不参与读取选择。
            connection.execute(
                update(DatasetAvailabilityObservation)
                .where(
                    DatasetAvailabilityObservation.dataset == _DAILY_DATASET,
                    DatasetAvailabilityObservation.partition_key == partition_key,
                    DatasetAvailabilityObservation.superseded_at.is_(None),
                )
                .values(superseded_at=observed_at)
            )
            # 重试可能复用同一来源时间戳；以唯一键回写该观测，保证任务可安全重跑。
            connection.execute(
                postgresql_insert(DatasetAvailabilityObservation)
                .values(
                    observation_id=uuid4(),
                    dataset=_DAILY_DATASET,
                    partition_key=partition_key,
                    availability=availability,
                    reason_code=reason_code,
                    provider_id=provider_id,
                    observed_at=observed_at,
                    superseded_at=None,
                    detail=None,
                )
                .on_conflict_do_update(
                    constraint="uq_dataset_availability_observation_time",
                    set_={
                        "availability": availability,
                        "reason_code": reason_code,
                        "provider_id": provider_id,
                        "superseded_at": None,
                        "detail": None,
                    },
                )
            )
        return EquityAvailabilityObservation(
            availability=availability,
            reason_code=reason_code,
            observed_at=observed_at,
        )

    def clear_daily_bar_availability(
        self,
        *,
        identifier: EquityIdentifier,
        start: date,
        end: date,
        cleared_at: datetime,
    ) -> None:
        """在日线真实发布后终结精确窗口的旧空集或来源不可用观测。"""
        if cleared_at.tzinfo is None:
            raise ValueError("cleared_at must include a timezone")
        with self._database.transaction() as connection:
            self._clear_daily_bar_availability_on_connection(
                connection,
                identifier=identifier,
                start=start,
                end=end,
                cleared_at=cleared_at,
            )

    def _clear_daily_bar_availability_on_connection(
        self,
        connection: Session,
        *,
        identifier: EquityIdentifier,
        start: date,
        end: date,
        cleared_at: datetime,
    ) -> None:
        """在调用方已有事务内终结精确窗口空集观测，供 publication 原子复用。"""
        partition_key = _availability_partition_key(identifier, start, end)
        connection.execute(
            update(DatasetAvailabilityObservation)
            .where(
                DatasetAvailabilityObservation.dataset == _DAILY_DATASET,
                DatasetAvailabilityObservation.partition_key == partition_key,
                DatasetAvailabilityObservation.superseded_at.is_(None),
            )
            .values(superseded_at=cleared_at)
        )

    def get_instrument(self, instrument_id: UUID) -> StoredEquityInstrument | None:
        """按公开 UUID 读取一只证券，不关联供应商专有表。"""
        statement = select(
            EquityInstrument.security_id,
            EquityInstrument.instrument_id,
            EquityInstrument.exchange,
            EquityInstrument.symbol,
            EquityInstrument.name,
            EquityInstrument.listing_status,
        ).where(EquityInstrument.instrument_id == instrument_id)
        with self._database.session() as connection:
            row = connection.execute(statement).mappings().one_or_none()
        return None if row is None else _stored_instrument(row)

    def get_instrument_by_identifier(
        self,
        identifier: EquityIdentifier,
        *,
        fact_start: date | None,
        fact_end: date | None,
    ) -> StoredEquityInstrument | None:
        """按事实窗口与当前知识解析唯一确认身份，不回退到身份锚当前列。"""
        if fact_start is not None and fact_end is not None and fact_start > fact_end:
            raise ValueError("fact_start must not be after fact_end")
        known_at = datetime.now(UTC)
        statement = (
            select(
                EquityInstrument.security_id,
                EquityInstrument.instrument_id,
                EquityIdentifierVersion.exchange.label("exchange"),
                EquityIdentifierVersion.symbol.label("symbol"),
                EquityInstrument.name,
                EquityInstrument.listing_status,
                EquityIdentifierVersion.identity_state,
            )
            .join(
                EquityIdentifierVersion,
                EquityIdentifierVersion.security_id == EquityInstrument.security_id,
            )
            .where(
                EquityIdentifierVersion.exchange == identifier.exchange.value,
                EquityIdentifierVersion.symbol == identifier.symbol,
                EquityIdentifierVersion.knowledge_range.op("@>")(known_at),
            )
            .distinct()
            .order_by(EquityInstrument.security_id)
        )
        if fact_start is not None:
            statement = statement.where(
                or_(
                    EquityIdentifierVersion.effective_to.is_(None),
                    EquityIdentifierVersion.effective_to > fact_start,
                )
            )
        if fact_end is not None:
            statement = statement.where(EquityIdentifierVersion.effective_from <= fact_end)
        with self._database.session() as connection:
            rows = connection.execute(statement).mappings().all()
        if not rows:
            return None
        security_ids = {int(row["security_id"]) for row in rows}
        if len(security_ids) != 1 or any(row["identity_state"] != "CONFIRMED" for row in rows):
            # 无界或有界请求只要覆盖代码复用/PENDING 区间，就不能任选其中一只证券。
            raise EquityIdentityReadConflictError(
                "equity identifier does not resolve to one confirmed security"
            )
        return _stored_instrument(rows[0])

    def list_instruments(
        self, *, query: str | None, limit: int
    ) -> Sequence[StoredEquityInstrument]:
        """返回有上限且按交易所、代码排序的证券，供内部目录读取。"""
        normalized = query.strip() if query is not None else None
        statement = (
            select(
                EquityInstrument.security_id,
                EquityInstrument.instrument_id,
                EquityIdentifierVersion.exchange.label("exchange"),
                EquityIdentifierVersion.symbol.label("symbol"),
                EquityInstrument.name,
                EquityInstrument.listing_status,
            )
            .join(
                EquityIdentifierVersion,
                EquityIdentifierVersion.security_id == EquityInstrument.security_id,
            )
            .where(
                EquityIdentifierVersion.identity_state == "CONFIRMED",
                EquityIdentifierVersion.effective_to.is_(None),
                EquityIdentifierVersion.known_to.is_(None),
            )
        )
        if normalized:
            pattern = f"{normalized}%"
            statement = statement.where(
                or_(
                    EquityIdentifierVersion.symbol.like(pattern),
                    func.coalesce(EquityInstrument.name, "").ilike(pattern),
                )
            )
        statement = statement.order_by(
            EquityIdentifierVersion.exchange,
            EquityIdentifierVersion.symbol,
            EquityInstrument.instrument_id,
        ).limit(limit)
        with self._database.session() as connection:
            rows = connection.execute(statement).mappings().all()
        return tuple(_stored_instrument(row) for row in rows)

    def list_daily_bars(
        self,
        *,
        instrument_id: UUID,
        start: date,
        end: date,
    ) -> Sequence[tuple[EquityDailyBar, int, bool]]:
        """按交易日升序读取一只证券有界窗口内的当前修订。"""
        statement = (
            select(
                EquityDailyBarModel.trade_date,
                EquityDailyBarModel.open_price,
                EquityDailyBarModel.high_price,
                EquityDailyBarModel.low_price,
                EquityDailyBarModel.close_price,
                EquityDailyBarModel.volume_shares,
                EquityDailyBarModel.amount_cny,
                EquityDailyBarModel.turnover_rate,
                EquityDailyBarModel.revision,
                EquityDailyBarModel.is_final,
            )
            .join(
                EquityInstrument,
                EquityInstrument.security_id == EquityDailyBarModel.security_id,
            )
            .where(
                EquityInstrument.instrument_id == instrument_id,
                EquityDailyBarModel.trade_date >= start,
                EquityDailyBarModel.trade_date <= end,
                EquityDailyBarModel.valid_to.is_(None),
            )
            .order_by(EquityDailyBarModel.trade_date)
        )
        with self._database.session() as connection:
            rows = connection.execute(statement).mappings().all()
        return tuple(
            (
                EquityDailyBar(
                    trade_date=row["trade_date"],
                    open_price=Decimal(row["open_price"]),
                    high_price=Decimal(row["high_price"]),
                    low_price=Decimal(row["low_price"]),
                    close_price=Decimal(row["close_price"]),
                    volume_shares=row["volume_shares"],
                    amount_cny=Decimal(row["amount_cny"]),
                    turnover_rate=(
                        None if row["turnover_rate"] is None else Decimal(row["turnover_rate"])
                    ),
                ),
                row["revision"],
                row["is_final"],
            )
            for row in rows
        )

    def publish_period_bars(
        self,
        *,
        identifier: EquityIdentifier,
        period: EquityBarPeriod,
        bars: Sequence[EquityPeriodBar],
        source: EquitySourceObservation,
        start: date,
        end: date,
    ) -> PublishedEquityDataset:
        """原子发布上游原生周/月 DATA 或合法零记录 coverage 与 checkpoint。"""
        if period not in _PERIOD_MODELS:
            raise ValueError("period must be weekly or monthly")
        if start > end:
            raise ValueError("period bar coverage window is invalid")
        if source.capability != period.capability:
            raise ValueError("period bar source capability does not match requested period")
        if any(bar.period is not period for bar in bars):
            raise ValueError("period bars must match requested period")
        if any(not start <= bar.period_end <= end for bar in bars):
            raise ValueError("period bar falls outside the requested coverage window")
        now = datetime.now(UTC)
        model = _PERIOD_MODELS[period]
        with self._database.transaction() as connection:
            source_batch_id = self._record_source_batch(
                connection,
                capability=source.capability,
                provider_id=source.provider_id,
                source_payload_sha256=source.source_payload_sha256,
                raw_uri=source.raw_uri,
                observed_at=source.observed_at,
                created_at=now,
                upstream_source=source.upstream_source,
                adapter_version=source.adapter_version,
                schema_fingerprint=source.schema_fingerprint,
            )
            coverage_identity = resolve_bar_window_identity(
                connection,
                period=period,
                identifier=identifier,
                start=start,
                end=end,
            )
            instrument = self._bar_window_instrument_on_connection(
                connection,
                identifier=identifier,
                identity=coverage_identity,
            )
            inserted_count, unchanged_count = (
                self._write_period_revisions(
                    connection,
                    model=model,
                    security_id=instrument.security_id,
                    bars=bars,
                    source_batch_id=source_batch_id,
                    observed_at=source.observed_at,
                )
                if bars
                else (0, 0)
            )
            data_publication_version = (
                self._publish(
                    connection,
                    dataset=period.capability,
                    instrument=instrument,
                    source_batch_id=source_batch_id,
                    published_at=now,
                )
                if bars
                else None
            )
            coverage = publish_bar_window_coverage(
                connection,
                release_repository=self._release_repository,
                period=period,
                identity=coverage_identity,
                source=source,
                source_batch_id=source_batch_id,
                record_count=len(bars),
                data_publication_version=data_publication_version,
                now=now,
            )
            self._advance_checkpoint(
                connection,
                capability=period.capability,
                identifier=identifier,
                window_end=end,
                data_version=coverage.data_version,
                updated_at=now,
            )
        return PublishedEquityDataset(
            data_version=coverage.data_version,
            published_at=now,
            inserted_count=inserted_count,
            unchanged_count=unchanged_count,
            instrument=instrument,
            coverage_version=coverage.coverage_version,
            source_batch_id=coverage.source_batch_id,
            publication_kind=coverage.publication_kind,
        )

    def publish_adjustment_factors(
        self,
        *,
        identifier: EquityIdentifier,
        factors: Sequence[EquityAdjustmentFactor],
        source: EquitySourceObservation,
        window_end: date,
    ) -> PublishedEquityDataset:
        """追加变化因子，并以单一 factor_version 原子发布完整当前序列。

        累计因子是按生效日变化的稀疏序列。已通过 adapter 结构校验但窗口内没有新点时，
        本方法仍登记来源、冻结当前序列并推进 checkpoint；它不写入数值为零的伪因子，
        也不会删除此前版本中的有效因子。
        """
        now = datetime.now(UTC)
        next_factor_version = uuid4()
        with self._database.transaction() as connection:
            source_batch_id = self._record_source_batch(
                connection,
                capability=source.capability,
                provider_id=source.provider_id,
                source_payload_sha256=source.source_payload_sha256,
                raw_uri=source.raw_uri,
                observed_at=source.observed_at,
                created_at=now,
                upstream_source=source.upstream_source,
                adapter_version=source.adapter_version,
                schema_fingerprint=source.schema_fingerprint,
            )
            instrument = self._confirmed_instrument_on_connection(
                connection,
                identifier=identifier,
                # 空窗口没有事实日可解析，使用包含端 window_end 锁定这次来源观察对应的
                # 确认身份；有因子时仍逐事实日解析，避免跨代码复用或生命周期边界写入。
                fact_dates=(
                    tuple(factor.effective_date for factor in factors) if factors else (window_end,)
                ),
                known_at=now,
            )
            if factors:
                inserted_count, unchanged_count = self._write_factor_revisions(
                    connection,
                    security_id=instrument.security_id,
                    factors=factors,
                    factor_version=next_factor_version,
                    source_batch_id=source_batch_id,
                    observed_at=source.observed_at,
                )
            else:
                inserted_count, unchanged_count = 0, 0
            data_version = self._publish(
                connection,
                dataset=_FACTOR_DATASET,
                instrument=instrument,
                source_batch_id=source_batch_id,
                published_at=now,
            )
            self._advance_checkpoint(
                connection,
                capability=_FACTOR_DATASET,
                identifier=identifier,
                window_end=window_end,
                data_version=data_version,
                updated_at=now,
            )
        return PublishedEquityDataset(
            data_version=data_version,
            published_at=now,
            inserted_count=inserted_count,
            unchanged_count=unchanged_count,
            instrument=instrument,
            source_batch_id=source_batch_id,
        )

    def publish_corporate_actions(
        self,
        *,
        identifier: EquityIdentifier,
        actions: Sequence[EquityCorporateAction],
        source: EquitySourceObservation,
        start: date,
        end: date,
    ) -> PublishedEquityDataset:
        """在同事务追加公司行动、累积 release 与精确窗口 coverage。"""
        if start > end:
            raise ValueError("corporate action publication window is invalid")
        action_fact_dates = tuple(_action_fact_date(action) for action in actions)
        if any(not start <= fact_date <= end for fact_date in action_fact_dates):
            raise ValueError("corporate action fact falls outside the requested coverage window")
        now = datetime.now(UTC)
        with self._database.transaction() as connection:
            source_batch_id = self._record_source_batch(
                connection,
                capability=source.capability,
                provider_id=source.provider_id,
                source_payload_sha256=source.source_payload_sha256,
                raw_uri=source.raw_uri,
                observed_at=source.observed_at,
                created_at=now,
                upstream_source=source.upstream_source,
                adapter_version=source.adapter_version,
                schema_fingerprint=source.schema_fingerprint,
            )
            (
                coverage_identities,
                coverage_scope,
                universe_hash,
            ) = resolve_event_coverage_identities(
                connection,
                start=start,
                end=end,
                identifier=identifier,
            )
            instrument = self._confirmed_instrument_on_connection(
                connection,
                identifier=identifier,
                fact_dates=action_fact_dates or (start, end),
                known_at=now,
            )
            if (
                len(coverage_identities) != 1
                or coverage_identities[0].security_id != instrument.security_id
            ):
                raise ValueError("corporate action coverage identity does not match instrument")
            inserted_count, unchanged_count = self._write_action_revisions(
                connection,
                identifier=identifier,
                security_id=instrument.security_id,
                actions=actions,
                source_batch_id=source_batch_id,
                observed_at=source.observed_at,
            )
            data_version = self._publish(
                connection,
                dataset=_ACTION_DATASET,
                instrument=instrument,
                source_batch_id=source_batch_id,
                published_at=now,
            )
            dataset_id, methodology_version_id = _release_bridge_context(
                connection,
                dataset=_ACTION_DATASET,
            )
            publish_event_window_coverages(
                connection,
                release_repository=self._release_repository,
                dataset_id=dataset_id,
                dataset_code=_ACTION_DATASET,
                methodology_version_id=methodology_version_id,
                mapping_version=_RELEASE_MAPPING_VERSION,
                source=source,
                source_batch_id=source_batch_id,
                identities=coverage_identities,
                coverage_scope=coverage_scope,
                universe_hash=universe_hash,
                families=("CORPORATE_ACTION",),
                records_for=lambda current_session, frozen_identities, family: (
                    _action_coverage_records_by_identity(
                        current_session,
                        identities=frozen_identities,
                        family=family,
                        provider_id=source.provider_id,
                    )
                ),
                now=now,
            )
            self._advance_checkpoint(
                connection,
                capability=_ACTION_DATASET,
                identifier=identifier,
                window_end=end,
                data_version=data_version,
                updated_at=now,
            )
        return PublishedEquityDataset(
            data_version=data_version,
            published_at=now,
            inserted_count=inserted_count,
            unchanged_count=unchanged_count,
            instrument=instrument,
        )

    def publish_company_profile(
        self,
        *,
        identifier: EquityIdentifier,
        profile: EquityCompanyProfile,
        source: EquitySourceObservation,
    ) -> PublishedEquityDataset:
        """合并来源非空字段后追加公司概况修订，并推进独立发布。"""
        now = datetime.now(UTC)
        with self._database.transaction() as connection:
            source_batch_id = self._record_source_batch(
                connection,
                capability=source.capability,
                provider_id=source.provider_id,
                source_payload_sha256=source.source_payload_sha256,
                raw_uri=source.raw_uri,
                observed_at=source.observed_at,
                created_at=now,
                upstream_source=source.upstream_source,
                adapter_version=source.adapter_version,
                schema_fingerprint=source.schema_fingerprint,
            )
            instrument = self._confirmed_instrument_on_connection(
                connection,
                identifier=identifier,
                fact_dates=(source.observed_at.astimezone(_SHANGHAI).date(),),
                known_at=now,
            )
            inserted_count, unchanged_count = self._write_profile_revision(
                connection,
                security_id=instrument.security_id,
                profile=profile,
                source_batch_id=source_batch_id,
                observed_at=source.observed_at,
            )
            data_version = self._publish(
                connection,
                dataset=_PROFILE_DATASET,
                instrument=instrument,
                source_batch_id=source_batch_id,
                published_at=now,
            )
            self._advance_checkpoint(
                connection,
                capability=_PROFILE_DATASET,
                identifier=identifier,
                window_end=None,
                data_version=data_version,
                updated_at=now,
            )
        return PublishedEquityDataset(
            data_version=data_version,
            published_at=now,
            inserted_count=inserted_count,
            unchanged_count=unchanged_count,
            instrument=instrument,
        )

    def get_current_publication(
        self,
        *,
        dataset: str,
        instrument: StoredEquityInstrument,
    ) -> EquityDatasetPublication | None:
        """优先读取永久证券分区，仅为从未复用的代码兼容旧 publication。"""
        stable_partition_key = _security_partition_key(instrument.security_id)
        with self._database.session() as connection:
            row = (
                connection.execute(
                    _current_publication_statement(
                        dataset=dataset,
                        partition_key=stable_partition_key,
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                known_at = datetime.now(UTC)
                legacy_security_rows = (
                    connection.execute(
                        select(EquityIdentifierVersion.security_id)
                        .where(
                            EquityIdentifierVersion.exchange
                            == instrument.identifier.exchange.value,
                            EquityIdentifierVersion.symbol == instrument.identifier.symbol,
                            EquityIdentifierVersion.identity_state == "CONFIRMED",
                            EquityIdentifierVersion.knowledge_range.op("@>")(known_at),
                        )
                        .distinct()
                        .order_by(EquityIdentifierVersion.security_id)
                    )
                    .mappings()
                    .all()
                )
                legacy_security_ids = {
                    int(legacy_row["security_id"]) for legacy_row in legacy_security_rows
                }
                if legacy_security_ids == {instrument.security_id}:
                    row = (
                        connection.execute(
                            _current_publication_statement(
                                dataset=dataset,
                                partition_key=instrument.identifier.qualified_symbol,
                            )
                        )
                        .mappings()
                        .one_or_none()
                    )
        if row is None:
            return None
        return EquityDatasetPublication(
            data_version=UUID(str(row["data_version"])),
            published_at=row["published_at"],
            quality_status=str(row.get("quality_status", "passed")),
        )

    def get_publication_source(
        self,
        *,
        dataset: str,
        data_version: UUID,
    ) -> EquityPublicationSource | None:
        """读取 immutable release 唯一绑定的来源观察，不以当前事实行或 raw URI 猜测来源。"""
        statement = (
            select(
                SourceBatch.source_batch_id,
                SourceBatch.provider_id,
                SourceBatch.upstream_source,
                SourceBatch.adapter_version,
            )
            .select_from(DatasetPublication)
            .join(DatasetRelease, DatasetRelease.release_id == DatasetPublication.release_id)
            .join(
                NormalizationRun,
                NormalizationRun.normalization_run_id == DatasetRelease.normalization_run_id,
            )
            .join(
                SourceBatch,
                and_(
                    SourceBatch.run_id == NormalizationRun.run_id,
                    SourceBatch.capability == DatasetPublication.dataset,
                    SourceBatch.adapter_version == NormalizationRun.adapter_version,
                    SourceBatch.schema_fingerprint == NormalizationRun.schema_fingerprint,
                ),
            )
            .where(
                DatasetPublication.dataset == dataset,
                DatasetPublication.data_version == data_version,
            )
        )
        with self._database.session() as connection:
            rows = connection.execute(statement).mappings().all()
        # 同一发布若不能精确归结到一个来源批次，返回空而不是任选一条错误谱系。
        if len(rows) != 1:
            return None
        row = rows[0]
        return EquityPublicationSource(
            source_batch_id=UUID(str(row["source_batch_id"])),
            provider_id=str(row["provider_id"]),
            upstream_source=str(row["upstream_source"]),
            adapter_version=str(row["adapter_version"]),
        )

    def get_bar_window_coverage(
        self,
        *,
        identifier: EquityIdentifier,
        security_id: int,
        period: EquityBarPeriod,
        start: date,
        end: date,
        data_version: UUID,
    ) -> EquityBarWindowCoveragePublication | None:
        """选择一个当前且精确绑定身份、窗口、版本和 publication 的行情 coverage。"""
        if start > end:
            raise ValueError("bar coverage request window is invalid")
        statement = (
            select(
                EquityBarWindowCoverage.coverage_version,
                EquityBarWindowCoverage.source_batch_id,
                EquityBarWindowCoverage.publication_kind,
                EquityBarWindowCoverage.record_count,
                EquityBarWindowCoverage.observed_at,
                DatasetPublication.data_version,
                DatasetPublication.published_at,
            )
            .join(
                DatasetPublication,
                DatasetPublication.publication_id == EquityBarWindowCoverage.publication_id,
            )
            .join(
                EquityIdentifierVersion,
                EquityIdentifierVersion.version_id == EquityBarWindowCoverage.identifier_version_id,
            )
            .where(
                EquityBarWindowCoverage.period == period.value,
                EquityBarWindowCoverage.capability == period.capability,
                EquityBarWindowCoverage.security_id == security_id,
                EquityBarWindowCoverage.coverage_from == start,
                EquityBarWindowCoverage.coverage_to == end,
                EquityBarWindowCoverage.data_version == data_version,
                EquityBarWindowCoverage.quality_status == "passed",
                EquityBarWindowCoverage.superseded_at.is_(None),
                EquityIdentifierVersion.security_id == security_id,
                EquityIdentifierVersion.exchange == identifier.exchange.value,
                EquityIdentifierVersion.symbol == identifier.symbol,
                EquityIdentifierVersion.identity_state == "CONFIRMED",
                # 读取方已按当前知识解析证券；coverage 绑定的同一身份版本也必须仍是
                # 当前知识，并完整覆盖请求窗口，不能把官方更正前的版本留作可读证据。
                EquityIdentifierVersion.effective_from <= start,
                or_(
                    EquityIdentifierVersion.effective_to.is_(None),
                    EquityIdentifierVersion.effective_to > end,
                ),
                EquityIdentifierVersion.known_to.is_(None),
                DatasetPublication.dataset == period.capability,
                DatasetPublication.data_version == EquityBarWindowCoverage.data_version,
                DatasetPublication.data_version == data_version,
                DatasetPublication.release_id.is_not(None),
                DatasetPublication.quality_status == "passed",
            )
        )
        with self._database.session() as connection:
            rows = connection.execute(statement).mappings().all()
        if not rows:
            return None
        if len(rows) != 1:
            # 虽有数据库唯一约束，reader 仍拒绝意外重复，以免任选覆盖掩盖血缘漂移。
            return None
        row = rows[0]
        publication_kind = str(row["publication_kind"])
        record_count = int(row["record_count"])
        if (publication_kind, record_count) not in {
            ("ZERO_RECORD_COVERAGE", 0),
        } and not (publication_kind == "DATA" and record_count > 0):
            return None
        return EquityBarWindowCoveragePublication(
            data_version=UUID(str(row["data_version"])),
            published_at=row["published_at"],
            coverage_version=UUID(str(row["coverage_version"])),
            source_batch_id=UUID(str(row["source_batch_id"])),
            publication_kind=publication_kind,
            record_count=record_count,
            observed_at=row["observed_at"],
        )

    def get_daily_bar_availability(
        self,
        *,
        identifier: EquityIdentifier,
        start: date,
        end: date,
    ) -> EquityAvailabilityObservation | None:
        """读取精确窗口当前的非事实观测，禁止把它当成 canonical publication。"""
        partition_key = _availability_partition_key(identifier, start, end)
        with self._database.session() as connection:
            row = (
                connection.execute(
                    select(
                        DatasetAvailabilityObservation.availability,
                        DatasetAvailabilityObservation.reason_code,
                        DatasetAvailabilityObservation.observed_at,
                    ).where(
                        DatasetAvailabilityObservation.dataset == _DAILY_DATASET,
                        DatasetAvailabilityObservation.partition_key == partition_key,
                        DatasetAvailabilityObservation.superseded_at.is_(None),
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            return None
        return EquityAvailabilityObservation(
            availability=str(row["availability"]),
            reason_code=str(row["reason_code"]),
            observed_at=row["observed_at"],
        )

    def list_bars(
        self,
        *,
        security_id: int,
        period: EquityBarPeriod,
        start: date,
        end: date,
        known_at: datetime | None = None,
    ) -> Sequence[StoredEquityBar]:
        """从周期物理表读取 publication 截止点可见 revision，绝不跨表聚合。"""
        if start > end:
            raise ValueError("start must not be after end")
        if period is EquityBarPeriod.DAY_1:
            model = EquityDailyBarModel
            date_column = model.trade_date
        else:
            model = _PERIOD_MODELS[period]
            date_column = model.period_end
        statement = select(
            date_column.label("period_end"),
            model.open_price,
            model.high_price,
            model.low_price,
            model.close_price,
            model.volume_shares,
            model.amount_cny,
            model.turnover_rate,
            model.revision,
            model.is_final,
        ).where(
            model.security_id == security_id,
            date_column >= start,
            date_column <= end,
        )
        statement = _at_knowledge_time(statement, model=model, known_at=known_at).order_by(
            date_column
        )
        with self._database.session() as connection:
            rows = connection.execute(statement).mappings().all()
        return tuple(_stored_bar(row, period=period) for row in rows)

    def list_adjustment_factors(
        self,
        *,
        security_id: int,
        end: date,
        known_at: datetime | None = None,
    ) -> Sequence[StoredAdjustmentFactor]:
        """读取 publication 截止点可见的累计因子，以便按生效日向后选择。"""
        statement = select(
            EquityAdjustmentFactorModel.effective_date,
            EquityAdjustmentFactorModel.cumulative_factor,
            EquityAdjustmentFactorModel.revision,
            EquityAdjustmentFactorModel.factor_version,
            EquityAdjustmentFactorModel.source_batch_id,
        ).where(
            EquityAdjustmentFactorModel.security_id == security_id,
            EquityAdjustmentFactorModel.effective_date <= end,
        )
        statement = _at_knowledge_time(
            statement,
            model=EquityAdjustmentFactorModel,
            known_at=known_at,
        ).order_by(EquityAdjustmentFactorModel.effective_date)
        with self._database.session() as connection:
            rows = connection.execute(statement).mappings().all()
        return tuple(
            StoredAdjustmentFactor(
                factor=EquityAdjustmentFactor(
                    effective_date=row["effective_date"],
                    cumulative_factor=Decimal(row["cumulative_factor"]),
                ),
                revision=int(row["revision"]),
                factor_version=UUID(str(row["factor_version"])),
                source_batch_id=UUID(str(row["source_batch_id"])),
            )
            for row in rows
        )

    def list_corporate_actions(
        self,
        *,
        security_id: int,
        start: date | None,
        end: date | None,
        known_at: datetime | None = None,
    ) -> Sequence[StoredCorporateAction]:
        """按报告期升序读取 publication 截止点可见的公司行动 revision。"""
        statement = select(
            EquityCorporateActionVersion.action_id,
            EquityCorporateActionVersion.revision,
            EquityCorporateActionVersion.source_event_key,
            EquityCorporateActionVersion.report_period,
            EquityCorporateActionVersion.status,
            EquityCorporateActionVersion.announcement_date,
            EquityCorporateActionVersion.record_date,
            EquityCorporateActionVersion.ex_date,
            EquityCorporateActionVersion.cash_dividend_per_10,
            EquityCorporateActionVersion.bonus_shares_per_10,
            EquityCorporateActionVersion.transfer_shares_per_10,
            EquityCorporateActionVersion.source_batch_id,
        ).where(
            EquityCorporateActionVersion.security_id == security_id,
        )
        statement = _at_knowledge_time(
            statement,
            model=EquityCorporateActionVersion,
            known_at=known_at,
        )
        if start is not None:
            statement = statement.where(EquityCorporateActionVersion.report_period >= start)
        if end is not None:
            statement = statement.where(EquityCorporateActionVersion.report_period <= end)
        statement = statement.order_by(
            EquityCorporateActionVersion.report_period,
            EquityCorporateActionVersion.action_id,
        )
        with self._database.session() as connection:
            rows = connection.execute(statement).mappings().all()
        return tuple(_stored_action(row) for row in rows)

    def get_company_profile(
        self,
        *,
        security_id: int,
        known_at: datetime | None = None,
    ) -> StoredCompanyProfile | None:
        """读取 publication 截止点可见的公司概况 revision。"""
        statement = select(EquityProfileVersion).where(
            EquityProfileVersion.security_id == security_id,
        )
        statement = (
            _at_knowledge_time(
                statement,
                model=EquityProfileVersion,
                known_at=known_at,
            )
            .order_by(EquityProfileVersion.revision.desc())
            .limit(1)
        )
        with self._database.session() as connection:
            row = connection.execute(statement).scalar_one_or_none()
        if row is None:
            return None
        return StoredCompanyProfile(
            profile=_profile_from_model(row),
            revision=row.revision,
            source_batch_id=UUID(str(row.source_batch_id)),
        )

    def _confirmed_instrument_on_connection(
        self,
        connection: Session,
        *,
        identifier: EquityIdentifier,
        fact_dates: Sequence[date],
        known_at: datetime,
    ) -> StoredEquityInstrument:
        """按每个事实日期解析同一已确认证券，参考数据不得创建或跨越身份。"""
        security_id = require_single_confirmed_identity_on_connection(
            connection,
            exchange=identifier.exchange,
            symbol=identifier.symbol,
            fact_dates=fact_dates,
            known_at=known_at,
        )
        row = (
            connection.execute(
                select(
                    EquityInstrument.security_id,
                    EquityInstrument.instrument_id,
                    EquityInstrument.exchange,
                    EquityInstrument.symbol,
                    EquityInstrument.name,
                    EquityInstrument.listing_status,
                ).where(EquityInstrument.security_id == security_id)
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise ValueError("confirmed equity instrument is required")
        return _stored_instrument(row)

    def _bar_window_instrument_on_connection(
        self,
        connection: Session,
        *,
        identifier: EquityIdentifier,
        identity: BarWindowIdentity,
    ) -> StoredEquityInstrument:
        """读取已由完整窗口解析器锁定的证券锚，并核对外部标识没有漂移。"""
        if identity.exchange != identifier.exchange.value or identity.symbol != identifier.symbol:
            raise ValueError("bar coverage identity does not match requested identifier")
        row = (
            connection.execute(
                select(
                    EquityInstrument.security_id,
                    EquityInstrument.instrument_id,
                    EquityInstrument.exchange,
                    EquityInstrument.symbol,
                    EquityInstrument.name,
                    EquityInstrument.listing_status,
                ).where(
                    EquityInstrument.security_id == identity.security_id,
                    EquityInstrument.master_confirmed_at.is_not(None),
                    EquityInstrument.listing_status != "PENDING",
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise ValueError("confirmed bar window instrument is required")
        return _stored_instrument(row)

    def _assert_instrument_identity_dates(
        self,
        connection: Session,
        *,
        identifier: EquityIdentifier,
        security_id: int,
        fact_dates: Sequence[date],
        known_at: datetime,
    ) -> None:
        """确认首日之外的行情仍属同一身份；首日已由 `_ensure_instrument` 校验。"""
        ordered_fact_dates = sorted(set(fact_dates))
        if not ordered_fact_dates:
            raise ValueError("equity facts require at least one identity date")
        for fact_date in ordered_fact_dates[1:]:
            resolution = resolve_identity_on_connection(
                connection,
                exchange=identifier.exchange,
                symbol=identifier.symbol,
                fact_date=fact_date,
                known_at=known_at,
            )
            if (
                resolution.status is not EquityIdentityResolutionStatus.RESOLVED
                or resolution.security_id != security_id
            ):
                raise PossibleCodeReuseError("equity facts cross an identity boundary")
            if resolution.identity_state == "CONFIRMED" and self._is_delisted_on_fact_date(
                connection,
                security_id=security_id,
                fact_date=fact_date,
                known_at=known_at,
            ):
                raise PossibleCodeReuseError("possible code reuse after delisting")

    def _write_period_revisions(
        self,
        connection: Session,
        *,
        model: Any,
        security_id: int,
        bars: Sequence[EquityPeriodBar],
        source_batch_id: UUID,
        observed_at: datetime,
    ) -> tuple[int, int]:
        """在周期专属表中追加变化 revision，不接触日线表。"""
        inserted_count = 0
        unchanged_count = 0
        for bar in bars:
            content_hash = _period_bar_content_hash(bar)
            current = (
                connection.execute(
                    select(model.revision, model.content_sha256).where(
                        model.security_id == security_id,
                        model.period_end == bar.period_end,
                        model.valid_to.is_(None),
                    )
                )
                .mappings()
                .one_or_none()
            )
            if current is not None and current["content_sha256"] == content_hash:
                unchanged_count += 1
                continue
            revision = 1 if current is None else int(current["revision"]) + 1
            if current is not None:
                connection.execute(
                    update(model)
                    .where(
                        model.security_id == security_id,
                        model.period_end == bar.period_end,
                        model.valid_to.is_(None),
                    )
                    .values(valid_to=observed_at)
                )
            connection.execute(
                insert(model).values(
                    security_id=security_id,
                    period_end=bar.period_end,
                    revision=revision,
                    open_price=bar.open_price,
                    high_price=bar.high_price,
                    low_price=bar.low_price,
                    close_price=bar.close_price,
                    volume_shares=bar.volume_shares,
                    amount_cny=bar.amount_cny,
                    turnover_rate=bar.turnover_rate,
                    is_final=True,
                    content_sha256=content_hash,
                    source_batch_id=source_batch_id,
                    valid_from=observed_at,
                    valid_to=None,
                )
            )
            inserted_count += 1
        return inserted_count, unchanged_count

    def _write_factor_revisions(
        self,
        connection: Session,
        *,
        security_id: int,
        factors: Sequence[EquityAdjustmentFactor],
        factor_version: UUID,
        source_batch_id: UUID,
        observed_at: datetime,
    ) -> tuple[int, int]:
        """追加变化的累计因子生效点，并保留历史修订。"""
        inserted_count = 0
        unchanged_count = 0
        for factor in factors:
            content_hash = _factor_content_hash(factor)
            current = (
                connection.execute(
                    select(
                        EquityAdjustmentFactorModel.revision,
                        EquityAdjustmentFactorModel.content_sha256,
                    ).where(
                        EquityAdjustmentFactorModel.security_id == security_id,
                        EquityAdjustmentFactorModel.effective_date == factor.effective_date,
                        EquityAdjustmentFactorModel.valid_to.is_(None),
                    )
                )
                .mappings()
                .one_or_none()
            )
            if current is not None and current["content_sha256"] == content_hash:
                unchanged_count += 1
                continue
            revision = 1 if current is None else int(current["revision"]) + 1
            if current is not None:
                connection.execute(
                    update(EquityAdjustmentFactorModel)
                    .where(
                        EquityAdjustmentFactorModel.security_id == security_id,
                        EquityAdjustmentFactorModel.effective_date == factor.effective_date,
                        EquityAdjustmentFactorModel.valid_to.is_(None),
                    )
                    .values(valid_to=observed_at)
                )
            connection.execute(
                insert(EquityAdjustmentFactorModel).values(
                    security_id=security_id,
                    effective_date=factor.effective_date,
                    revision=revision,
                    cumulative_factor=factor.cumulative_factor,
                    factor_version=factor_version,
                    content_sha256=content_hash,
                    source_batch_id=source_batch_id,
                    valid_from=observed_at,
                    valid_to=None,
                )
            )
            inserted_count += 1
        return inserted_count, unchanged_count

    def _write_action_revisions(
        self,
        connection: Session,
        *,
        identifier: EquityIdentifier,
        security_id: int,
        actions: Sequence[EquityCorporateAction],
        source_batch_id: UUID,
        observed_at: datetime,
    ) -> tuple[int, int]:
        """按来源事件身份追加变化公司行动 revision。"""
        inserted_count = 0
        unchanged_count = 0
        for action in actions:
            action_id = uuid5(
                NAMESPACE_URL,
                f"quant-v2:{identifier.qualified_symbol}:corporate-action:{action.source_event_key}",
            )
            content_hash = _action_content_hash(action)
            current = (
                connection.execute(
                    select(
                        EquityCorporateActionVersion.revision,
                        EquityCorporateActionVersion.content_sha256,
                    ).where(
                        EquityCorporateActionVersion.action_id == action_id,
                        EquityCorporateActionVersion.valid_to.is_(None),
                    )
                )
                .mappings()
                .one_or_none()
            )
            if current is not None and current["content_sha256"] == content_hash:
                unchanged_count += 1
                continue
            revision = 1 if current is None else int(current["revision"]) + 1
            if current is not None:
                connection.execute(
                    update(EquityCorporateActionVersion)
                    .where(
                        EquityCorporateActionVersion.action_id == action_id,
                        EquityCorporateActionVersion.valid_to.is_(None),
                    )
                    .values(valid_to=observed_at)
                )
            connection.execute(
                insert(EquityCorporateActionVersion).values(
                    action_id=action_id,
                    revision=revision,
                    security_id=security_id,
                    source_event_key=action.source_event_key,
                    report_period=action.report_period,
                    status=action.status,
                    announcement_date=action.announcement_date,
                    record_date=action.record_date,
                    ex_date=action.ex_date,
                    cash_dividend_per_10=action.cash_dividend_per_10,
                    bonus_shares_per_10=action.bonus_shares_per_10,
                    transfer_shares_per_10=action.transfer_shares_per_10,
                    content_sha256=content_hash,
                    source_batch_id=source_batch_id,
                    valid_from=observed_at,
                    valid_to=None,
                    source_description=None,
                )
            )
            inserted_count += 1
        return inserted_count, unchanged_count

    def _write_profile_revision(
        self,
        connection: Session,
        *,
        security_id: int,
        profile: EquityCompanyProfile,
        source_batch_id: UUID,
        observed_at: datetime,
    ) -> tuple[int, int]:
        """合并非空来源字段；内容未变化时保持当前 revision。"""
        current = connection.execute(
            select(EquityProfileVersion)
            .where(
                EquityProfileVersion.security_id == security_id,
                EquityProfileVersion.valid_to.is_(None),
            )
            .order_by(EquityProfileVersion.revision.desc())
            .limit(1)
        ).scalar_one_or_none()
        merged = (
            profile if current is None else _merge_profile(_profile_from_model(current), profile)
        )
        content_hash = _profile_content_hash(merged)
        if current is not None and current.content_sha256 == content_hash:
            return 0, 1
        revision = 1 if current is None else current.revision + 1
        if current is not None:
            connection.execute(
                update(EquityProfileVersion)
                .where(
                    EquityProfileVersion.security_id == security_id,
                    EquityProfileVersion.valid_to.is_(None),
                )
                .values(valid_to=observed_at)
            )
        connection.execute(
            insert(EquityProfileVersion).values(
                security_id=security_id,
                revision=revision,
                company_name=merged.company_name,
                english_name=merged.english_name,
                industry=merged.industry,
                legal_representative=merged.legal_representative,
                established_on=merged.established_on,
                website=merged.website,
                email=merged.email,
                phone=merged.phone,
                registered_address=merged.registered_address,
                office_address=merged.office_address,
                main_business=merged.main_business,
                business_scope=merged.business_scope,
                summary=merged.summary,
                content_sha256=content_hash,
                source_batch_id=source_batch_id,
                valid_from=observed_at,
                valid_to=None,
            )
        )
        return 1, 0

    def _advance_checkpoint(
        self,
        connection: Session,
        *,
        capability: str,
        identifier: EquityIdentifier,
        window_end: date | None,
        data_version: UUID,
        updated_at: datetime,
    ) -> None:
        """在 publication 成功后才推进能力独立检查点。"""
        partition_key = identifier.qualified_symbol
        exists = connection.execute(
            select(EquitySyncCheckpoint.capability).where(
                EquitySyncCheckpoint.capability == capability,
                EquitySyncCheckpoint.partition_key == partition_key,
            )
        ).scalar_one_or_none()
        if exists is None:
            connection.execute(
                insert(EquitySyncCheckpoint).values(
                    capability=capability,
                    partition_key=partition_key,
                    last_window_end=window_end,
                    data_version=data_version,
                    updated_at=updated_at,
                )
            )
            return
        connection.execute(
            update(EquitySyncCheckpoint)
            .where(
                EquitySyncCheckpoint.capability == capability,
                EquitySyncCheckpoint.partition_key == partition_key,
            )
            .values(
                last_window_end=window_end,
                data_version=data_version,
                updated_at=updated_at,
            )
        )

    def _ensure_instrument(
        self,
        connection: Session,
        *,
        identifier: EquityIdentifier,
        fact_date: date,
        source_batch_id: UUID,
        now: datetime,
    ) -> StoredEquityInstrument:
        """日线早于主数据发布时创建带事实日期和证据的 `PENDING` 身份。"""
        resolution = resolve_identity_on_connection(
            connection,
            exchange=identifier.exchange,
            symbol=identifier.symbol,
            fact_date=fact_date,
            known_at=now,
        )
        if resolution.status is EquityIdentityResolutionStatus.CONFLICT:
            # 多个历史身份命中属于 canonical 损坏；不能依名称、状态或排序任取一条写日线。
            raise ValueError("equity identity resolution conflict")
        if resolution.status is EquityIdentityResolutionStatus.RESOLVED:
            if self._is_delisted_on_fact_date(
                connection,
                security_id=resolution.security_id,
                fact_date=fact_date,
                known_at=now,
            ):
                # 退市后同代码行情只能作为可能复用候选隔离，不能借当前标识回写旧证券。
                raise PossibleCodeReuseError("possible code reuse after delisting")
            existing = (
                connection.execute(
                    select(
                        EquityInstrument.security_id,
                        EquityInstrument.instrument_id,
                        EquityInstrument.exchange,
                        EquityInstrument.symbol,
                        EquityInstrument.name,
                        EquityInstrument.listing_status,
                    ).where(EquityInstrument.security_id == resolution.security_id)
                )
                .mappings()
                .one_or_none()
            )
            if existing is None:
                raise ValueError("resolved equity identity anchor is missing")
            return _stored_instrument(existing)
        pending = (
            connection.execute(
                select(
                    EquityInstrument.security_id,
                    EquityInstrument.instrument_id,
                    EquityInstrument.exchange,
                    EquityInstrument.symbol,
                    EquityInstrument.name,
                    EquityInstrument.listing_status,
                )
                .join(
                    EquityIdentifierVersion,
                    EquityIdentifierVersion.security_id == EquityInstrument.security_id,
                )
                .where(
                    EquityIdentifierVersion.exchange == identifier.exchange.value,
                    EquityIdentifierVersion.symbol == identifier.symbol,
                    EquityIdentifierVersion.identity_state == "PENDING",
                    EquityIdentifierVersion.known_to.is_(None),
                )
                .order_by(EquityInstrument.security_id)
            )
            .mappings()
            .one_or_none()
        )
        if pending is not None:
            # 同代码历史占位只能由同一 PENDING 身份复用；绝不从确认身份当前列回退。
            return _stored_instrument(pending)
        instrument_id = uuid4()
        # 主数据同步会补全该占位证券。
        # 行情同步绝不能自行猜测名称或上市状态。
        security_id = connection.execute(
            insert(EquityInstrument)
            .values(
                instrument_id=instrument_id,
                exchange=identifier.exchange.value,
                symbol=identifier.symbol,
                listing_status="PENDING",
                created_at=now,
                updated_at=now,
            )
            .returning(EquityInstrument.security_id)
        ).scalar_one()
        # PENDING 版本只为历史写入保留稳定锚点；主数据确认前绝不对 API 发布。
        connection.execute(
            insert(EquityIdentifierVersion).values(
                version_id=uuid4(),
                security_id=security_id,
                exchange=identifier.exchange.value,
                symbol=identifier.symbol,
                identity_state="PENDING",
                effective_from=fact_date,
                effective_to=None,
                known_from=now,
                known_to=None,
                effective_date_precision="OBSERVATION_DATE",
                source_batch_id=source_batch_id,
                content_sha256=_pending_identity_content_hash(identifier, fact_date),
            )
        )
        return StoredEquityInstrument(
            security_id=int(security_id),
            instrument_id=instrument_id,
            identifier=identifier,
            name=None,
            listing_status="PENDING",
        )

    def _is_delisted_on_fact_date(
        self,
        connection: Session,
        *,
        security_id: int | None,
        fact_date: date,
        known_at: datetime,
    ) -> bool:
        """检查已解析身份在事实日是否已有明确退市状态，不以当前投影替代历史。"""
        if security_id is None:
            raise ValueError("resolved equity identity must include security_id")
        row = connection.execute(
            select(EquityListingStatusVersion.version_id).where(
                EquityListingStatusVersion.security_id == security_id,
                EquityListingStatusVersion.status == "DELISTED",
                EquityListingStatusVersion.effective_range.op("@>")(fact_date),
                EquityListingStatusVersion.knowledge_range.op("@>")(known_at),
            )
        ).scalar_one_or_none()
        return row is not None

    def _record_source_batch(
        self,
        connection: Session,
        *,
        capability: str,
        provider_id: str,
        source_payload_sha256: str,
        raw_uri: str,
        observed_at: datetime,
        created_at: datetime,
        upstream_source: str | None = None,
        adapter_version: str = "unversioned",
        schema_fingerprint: str | None = None,
    ) -> UUID:
        """登记独立外部观测；相同 payload 不得折叠来源批次。"""
        return record_source_observation(
            connection,
            provider_id=provider_id,
            capability=capability,
            source_payload_sha256=source_payload_sha256,
            raw_uri=raw_uri,
            observed_at=observed_at,
            created_at=created_at,
            upstream_source=upstream_source,
            adapter_version=adapter_version,
            schema_fingerprint=schema_fingerprint,
        )

    def _write_revisions(
        self,
        connection: Session,
        *,
        security_id: int,
        bars: Sequence[EquityDailyBar],
        source_batch_id: UUID,
        observed_at: datetime,
    ) -> tuple[int, int]:
        """仅关闭发生变化的当前修订，并插入不可变后继版本。"""
        inserted_count = 0
        unchanged_count = 0
        for bar in bars:
            content_hash = _bar_content_hash(bar)
            current = (
                connection.execute(
                    select(
                        EquityDailyBarModel.revision,
                        EquityDailyBarModel.content_sha256,
                    ).where(
                        EquityDailyBarModel.security_id == security_id,
                        EquityDailyBarModel.trade_date == bar.trade_date,
                        EquityDailyBarModel.valid_to.is_(None),
                    )
                )
                .mappings()
                .one_or_none()
            )
            if current is not None and current["content_sha256"] == content_hash:
                # 业务值相同即使重复抓取，也必须保留原修订和发布版本。
                unchanged_count += 1
                continue
            revision = 1 if current is None else current["revision"] + 1
            if current is not None:
                # 已发布观测永不覆盖。
                # 必须先关闭旧版本，再插入后继修订。
                connection.execute(
                    update(EquityDailyBarModel)
                    .where(
                        EquityDailyBarModel.security_id == security_id,
                        EquityDailyBarModel.trade_date == bar.trade_date,
                        EquityDailyBarModel.valid_to.is_(None),
                    )
                    .values(valid_to=observed_at)
                )
            connection.execute(
                insert(EquityDailyBarModel).values(
                    security_id=security_id,
                    trade_date=bar.trade_date,
                    revision=revision,
                    open_price=bar.open_price,
                    high_price=bar.high_price,
                    low_price=bar.low_price,
                    close_price=bar.close_price,
                    volume_shares=bar.volume_shares,
                    amount_cny=bar.amount_cny,
                    turnover_rate=bar.turnover_rate,
                    is_final=True,
                    content_sha256=content_hash,
                    source_batch_id=source_batch_id,
                    valid_from=observed_at,
                    valid_to=None,
                )
            )
            inserted_count += 1
        return inserted_count, unchanged_count

    def _publish(
        self,
        connection: Session,
        *,
        dataset: str,
        instrument: StoredEquityInstrument,
        source_batch_id: UUID,
        published_at: datetime,
    ) -> UUID:
        """把当前证券快照通过统一 release 仓储发布，并返回真实消费者版本。"""
        partition_key = _security_partition_key(instrument.security_id)
        records, fact_min, fact_max = _current_release_records(
            connection,
            dataset=dataset,
            security_id=instrument.security_id,
        )
        published = publish_legacy_snapshot(
            connection,
            release_repository=self._release_repository,
            dataset_code=dataset,
            partition_key=partition_key,
            domain="equity",
            grain="equity security + dataset-native current observation",
            semantic_family="reported-equity-market-data",
            mapping_version=_RELEASE_MAPPING_VERSION,
            source_batch_id=source_batch_id,
            records=records,
            fact_min=fact_min,
            fact_max=fact_max,
            now=published_at,
        )
        # `data_version` 仅由统一 release 仓储生成；控制面记录的也是这条真实消费者版本。
        _record_fenced_publication_checkpoint(published.data_version)
        return published.data_version


def _release_bridge_context(
    connection: Session,
    *,
    dataset: str,
) -> tuple[UUID, UUID]:
    """读取刚由 legacy bridge 冻结的真实 dataset 与方法学身份。"""
    dataset_id = connection.execute(
        select(CanonicalDataset.dataset_id).where(
            CanonicalDataset.code == dataset,
            CanonicalDataset.schema_version == 1,
        )
    ).scalar_one()
    methodology_version_id = connection.execute(
        select(MethodologyVersion.methodology_version_id).where(
            MethodologyVersion.code == f"{dataset}.release-bridge",
            MethodologyVersion.version == 1,
        )
    ).scalar_one()
    return UUID(str(dataset_id)), UUID(str(methodology_version_id))


def _action_coverage_records_by_identity(
    connection: Session,
    *,
    identities: Sequence[EventCoverageIdentity],
    family: str,
    provider_id: str,
) -> Mapping[EventCoverageIdentity, EventCoverageRecords]:
    """一次读取当前公司行动，并按公开事件日分配到冻结身份窗口。"""
    if family != "CORPORATE_ACTION":
        raise ValueError("corporate action coverage family is invalid")
    identity_values = tuple(identities)
    result_records: dict[EventCoverageIdentity, list[CanonicalLineageRecord]] = {
        identity: [] for identity in identity_values
    }
    result_dates: dict[EventCoverageIdentity, list[date]] = {
        identity: [] for identity in identity_values
    }
    if not identity_values:
        return {}
    fact_date = func.coalesce(
        EquityCorporateActionVersion.ex_date,
        EquityCorporateActionVersion.record_date,
        EquityCorporateActionVersion.announcement_date,
        EquityCorporateActionVersion.report_period,
    )
    rows = connection.execute(
        select(
            EquityCorporateActionVersion.security_id,
            EquityCorporateActionVersion.action_id,
            EquityCorporateActionVersion.content_sha256,
            EquityCorporateActionVersion.source_batch_id,
            fact_date.label("fact_date"),
        )
        .join(
            SourceBatch,
            SourceBatch.source_batch_id == EquityCorporateActionVersion.source_batch_id,
        )
        .where(
            EquityCorporateActionVersion.security_id.in_(
                {identity.security_id for identity in identity_values}
            ),
            EquityCorporateActionVersion.valid_to.is_(None),
            SourceBatch.provider_id == provider_id,
            fact_date >= min(identity.coverage_from for identity in identity_values),
            fact_date <= max(identity.coverage_to for identity in identity_values),
        )
        .order_by(
            EquityCorporateActionVersion.security_id,
            fact_date,
            EquityCorporateActionVersion.action_id,
        )
    ).all()
    identities_by_security: dict[int, list[EventCoverageIdentity]] = {}
    for identity in identity_values:
        identities_by_security.setdefault(identity.security_id, []).append(identity)
    for security_id, action_id, content_hash, source_batch_id, event_date in rows:
        matches = [
            identity
            for identity in identities_by_security.get(int(security_id), ())
            if identity.coverage_from <= event_date <= identity.coverage_to
        ]
        if len(matches) > 1:
            raise ValueError("corporate action coverage identity windows overlap")
        if not matches:
            continue
        identity = matches[0]
        result_records[identity].append(
            _release_record(
                dataset=_ACTION_DATASET,
                security_id=int(security_id),
                native_key=str(action_id),
                content_hash=content_hash,
                source_batch_id=source_batch_id,
            )
        )
        result_dates[identity].append(event_date)
    return {
        identity: EventCoverageRecords(
            records=tuple(result_records[identity]),
            fact_dates=tuple(result_dates[identity]),
        )
        for identity in identity_values
    }


def _current_release_records(
    connection: Session,
    *,
    dataset: str,
    security_id: int,
) -> tuple[tuple[CanonicalLineageRecord, ...], date | None, date | None]:
    """读取一个证券分区全部当前 revision，确保 release 不会只冻结本次增量。"""
    if dataset == _DAILY_DATASET:
        rows = (
            connection.execute(
                select(
                    EquityDailyBarModel.trade_date.label("fact_date"),
                    EquityDailyBarModel.content_sha256,
                    EquityDailyBarModel.source_batch_id,
                )
                .where(
                    EquityDailyBarModel.security_id == security_id,
                    EquityDailyBarModel.valid_to.is_(None),
                )
                .order_by(EquityDailyBarModel.trade_date)
            )
            .mappings()
            .all()
        )
        return _dated_release_records(
            dataset=dataset,
            security_id=security_id,
            rows=rows,
        )
    period_model = _period_model_for_dataset(dataset)
    if period_model is not None:
        rows = (
            connection.execute(
                select(
                    period_model.period_end.label("fact_date"),
                    period_model.content_sha256,
                    period_model.source_batch_id,
                )
                .where(
                    period_model.security_id == security_id,
                    period_model.valid_to.is_(None),
                )
                .order_by(period_model.period_end)
            )
            .mappings()
            .all()
        )
        return _dated_release_records(
            dataset=dataset,
            security_id=security_id,
            rows=rows,
        )
    if dataset == _FACTOR_DATASET:
        rows = (
            connection.execute(
                select(
                    EquityAdjustmentFactorModel.effective_date.label("fact_date"),
                    EquityAdjustmentFactorModel.content_sha256,
                    EquityAdjustmentFactorModel.source_batch_id,
                )
                .where(
                    EquityAdjustmentFactorModel.security_id == security_id,
                    EquityAdjustmentFactorModel.valid_to.is_(None),
                )
                .order_by(EquityAdjustmentFactorModel.effective_date)
            )
            .mappings()
            .all()
        )
        return _dated_release_records(
            dataset=dataset,
            security_id=security_id,
            rows=rows,
        )
    if dataset == _ACTION_DATASET:
        rows = (
            connection.execute(
                select(
                    EquityCorporateActionVersion.action_id,
                    func.coalesce(
                        EquityCorporateActionVersion.ex_date,
                        EquityCorporateActionVersion.record_date,
                        EquityCorporateActionVersion.announcement_date,
                        EquityCorporateActionVersion.report_period,
                    ).label("fact_date"),
                    EquityCorporateActionVersion.content_sha256,
                    EquityCorporateActionVersion.source_batch_id,
                )
                .where(
                    EquityCorporateActionVersion.security_id == security_id,
                    EquityCorporateActionVersion.valid_to.is_(None),
                )
                .order_by(
                    func.coalesce(
                        EquityCorporateActionVersion.ex_date,
                        EquityCorporateActionVersion.record_date,
                        EquityCorporateActionVersion.announcement_date,
                        EquityCorporateActionVersion.report_period,
                    ),
                    EquityCorporateActionVersion.action_id,
                )
            )
            .mappings()
            .all()
        )
        records = tuple(
            _release_record(
                dataset=dataset,
                security_id=security_id,
                native_key=str(row["action_id"]),
                content_hash=row["content_sha256"],
                source_batch_id=row["source_batch_id"],
            )
            for row in rows
        )
        dates = tuple(cast(date, row["fact_date"]) for row in rows)
        return records, (min(dates) if dates else None), (max(dates) if dates else None)
    if dataset == _PROFILE_DATASET:
        row = (
            connection.execute(
                select(
                    EquityProfileVersion.content_sha256,
                    EquityProfileVersion.source_batch_id,
                ).where(
                    EquityProfileVersion.security_id == security_id,
                    EquityProfileVersion.valid_to.is_(None),
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise ValueError("equity profile publication has no current revision")
        return (
            (
                _release_record(
                    dataset=dataset,
                    security_id=security_id,
                    native_key="profile",
                    content_hash=row["content_sha256"],
                    source_batch_id=row["source_batch_id"],
                ),
            ),
            None,
            None,
        )
    raise ValueError("equity dataset is not supported by the canonical release bridge")


def _period_model_for_dataset(
    dataset: str,
) -> type[EquityWeeklyBar] | type[EquityMonthlyBar] | None:
    """将周/月 capability 映射到各自物理 revision 表，禁止跨周期拼接快照。"""
    for period, model in _PERIOD_MODELS.items():
        if period.capability == dataset:
            return model
    return None


def _dated_release_records(
    *,
    dataset: str,
    security_id: int,
    rows: Sequence[Mapping[Any, Any]],
) -> tuple[tuple[CanonicalLineageRecord, ...], date | None, date | None]:
    """将按事实日期排序的当前 revision 投影为完整 release 血缘和日期范围。"""
    dates = tuple(cast(date, row["fact_date"]) for row in rows)
    records = tuple(
        _release_record(
            dataset=dataset,
            security_id=security_id,
            native_key=fact_date.isoformat(),
            content_hash=row["content_sha256"],
            source_batch_id=row["source_batch_id"],
        )
        for fact_date, row in zip(dates, rows, strict=True)
    )
    return records, (min(dates) if dates else None), (max(dates) if dates else None)


def _release_record(
    *,
    dataset: str,
    security_id: int,
    native_key: str,
    content_hash: object,
    source_batch_id: object,
) -> CanonicalLineageRecord:
    """以真实 revision 摘要和来源批次构造 release 血缘，不能用随机值代替。"""
    record_key_hash = hashlib.sha256(
        f"equity:{dataset}:{security_id}:{native_key}".encode()
    ).hexdigest()
    return CanonicalLineageRecord(
        record_key_hash=record_key_hash,
        content_hash=_hex_hash(content_hash),
        source_batch_id=UUID(str(source_batch_id)),
        transform_hash=hashlib.sha256(_RELEASE_MAPPING_VERSION.encode()).hexdigest(),
    )


def _hex_hash(value: object) -> str:
    """将数据库 `bytea` 内容摘要转换为统一小写十六进制，拒绝不完整血缘。"""
    if isinstance(value, bytes):
        result = value.hex()
    elif isinstance(value, memoryview):
        result = value.tobytes().hex()
    else:
        result = str(value)
    if len(result) != 64 or any(character not in "0123456789abcdef" for character in result):
        raise ValueError("equity revision content hash is invalid")
    return result


def _record_fenced_publication_checkpoint(data_version: UUID) -> None:
    """把已提交或复用的 canonical dataVersion 交给同事务控制面写入脱敏 checkpoint。"""
    execution = current_fenced_execution()
    if execution is not None:
        execution.record_checkpoint(kind="data-version", position=str(data_version))


def _stored_instrument(row: Mapping[Any, Any]) -> StoredEquityInstrument:
    """将一条 SQL 映射行转换为数据源无关的已存证券记录。"""
    return StoredEquityInstrument(
        security_id=int(row["security_id"]),
        instrument_id=UUID(str(row["instrument_id"])),
        identifier=EquityIdentifier.parse(f"{row['exchange']}.{row['symbol']}"),
        name=None if row["name"] is None else str(row["name"]),
        listing_status=str(row["listing_status"]),
    )


def _bar_content_hash(bar: EquityDailyBar) -> bytes:
    """对标准业务字段计算哈希，避免重复来源批次制造伪修订。"""
    serialized = json.dumps(
        {
            "tradeDate": bar.trade_date.isoformat(),
            "open": str(bar.open_price),
            "high": str(bar.high_price),
            "low": str(bar.low_price),
            "close": str(bar.close_price),
            "volumeShares": bar.volume_shares,
            "amountCny": str(bar.amount_cny),
            "turnoverRate": None if bar.turnover_rate is None else str(bar.turnover_rate),
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(serialized).digest()


def _pending_identity_content_hash(identifier: EquityIdentifier, fact_date: date) -> bytes:
    """为行情创建的 PENDING 标识保存可复验的最小业务摘要。"""
    serialized = f"{identifier.qualified_symbol}|{fact_date.isoformat()}|PENDING".encode()
    return hashlib.sha256(serialized).digest()


def _stored_bar(row: Mapping[Any, Any], *, period: EquityBarPeriod) -> StoredEquityBar:
    """把周期专属表行投影为统一行情读取记录。"""
    common = {
        "open_price": Decimal(row["open_price"]),
        "high_price": Decimal(row["high_price"]),
        "low_price": Decimal(row["low_price"]),
        "close_price": Decimal(row["close_price"]),
        "volume_shares": int(row["volume_shares"]),
        "amount_cny": Decimal(row["amount_cny"]),
        "turnover_rate": (None if row["turnover_rate"] is None else Decimal(row["turnover_rate"])),
    }
    bar: EquityDailyBar | EquityPeriodBar
    if period is EquityBarPeriod.DAY_1:
        bar = EquityDailyBar(trade_date=row["period_end"], **common)
    else:
        bar = EquityPeriodBar(period=period, period_end=row["period_end"], **common)
    return StoredEquityBar(
        bar=bar,
        revision=int(row["revision"]),
        is_final=bool(row["is_final"]),
    )


def _stored_action(row: Mapping[Any, Any]) -> StoredCorporateAction:
    """把公司行动 SQL 行投影为稳定事件读取记录。"""
    return StoredCorporateAction(
        action_id=UUID(str(row["action_id"])),
        revision=int(row["revision"]),
        source_batch_id=UUID(str(row["source_batch_id"])),
        action=EquityCorporateAction(
            source_event_key=str(row["source_event_key"]),
            report_period=row["report_period"],
            status=str(row["status"]),
            announcement_date=row["announcement_date"],
            record_date=row["record_date"],
            ex_date=row["ex_date"],
            cash_dividend_per_10=_optional_row_decimal(row["cash_dividend_per_10"]),
            bonus_shares_per_10=_optional_row_decimal(row["bonus_shares_per_10"]),
            transfer_shares_per_10=_optional_row_decimal(row["transfer_shares_per_10"]),
        ),
    )


def _optional_row_decimal(value: object) -> Decimal | None:
    """把可空数据库 numeric 值映射为精确小数。"""
    return None if value is None else Decimal(str(value))


def _profile_from_model(row: EquityProfileVersion) -> EquityCompanyProfile:
    """把当前公司概况 ORM 实体投影为领域值。"""
    return EquityCompanyProfile(
        company_name=row.company_name,
        english_name=row.english_name,
        industry=row.industry,
        legal_representative=row.legal_representative,
        established_on=row.established_on,
        website=row.website,
        email=row.email,
        phone=row.phone,
        registered_address=row.registered_address,
        office_address=row.office_address,
        main_business=row.main_business,
        business_scope=row.business_scope,
        summary=row.summary,
    )


def _merge_profile(
    current: EquityCompanyProfile,
    incoming: EquityCompanyProfile,
) -> EquityCompanyProfile:
    """用来源非空值更新概况，同时防止暂时缺字段清空已发布值。"""
    return EquityCompanyProfile(
        company_name=incoming.company_name or current.company_name,
        english_name=incoming.english_name or current.english_name,
        industry=incoming.industry or current.industry,
        legal_representative=incoming.legal_representative or current.legal_representative,
        established_on=incoming.established_on or current.established_on,
        website=incoming.website or current.website,
        email=incoming.email or current.email,
        phone=incoming.phone or current.phone,
        registered_address=incoming.registered_address or current.registered_address,
        office_address=incoming.office_address or current.office_address,
        main_business=incoming.main_business or current.main_business,
        business_scope=incoming.business_scope or current.business_scope,
        summary=incoming.summary or current.summary,
    )


def _period_bar_content_hash(bar: EquityPeriodBar) -> bytes:
    """对上游原生周期行情标准业务值计算哈希。"""
    return _content_hash(
        {
            "period": bar.period.value,
            "periodEnd": bar.period_end.isoformat(),
            "open": str(bar.open_price),
            "high": str(bar.high_price),
            "low": str(bar.low_price),
            "close": str(bar.close_price),
            "volumeShares": bar.volume_shares,
            "amountCny": str(bar.amount_cny),
            "turnoverRate": (None if bar.turnover_rate is None else str(bar.turnover_rate)),
        }
    )


def _factor_content_hash(factor: EquityAdjustmentFactor) -> bytes:
    """对累计因子标准业务值计算哈希。"""
    return _content_hash(
        {
            "effectiveDate": factor.effective_date.isoformat(),
            "cumulativeFactor": str(factor.cumulative_factor),
        }
    )


def _action_fact_date(action: EquityCorporateAction) -> date:
    """按公开事件筛选口径选择除权、登记、公告或报告期中的首个可用日期。"""
    return action.ex_date or action.record_date or action.announcement_date or action.report_period


def _action_content_hash(action: EquityCorporateAction) -> bytes:
    """对公司行动可修订业务字段计算哈希。"""
    return _content_hash(
        {
            "sourceEventKey": action.source_event_key,
            "reportPeriod": action.report_period.isoformat(),
            "status": action.status,
            "announcementDate": _date_text(action.announcement_date),
            "recordDate": _date_text(action.record_date),
            "exDate": _date_text(action.ex_date),
            "cashDividendPer10": _decimal_text(action.cash_dividend_per_10),
            "bonusSharesPer10": _decimal_text(action.bonus_shares_per_10),
            "transferSharesPer10": _decimal_text(action.transfer_shares_per_10),
        }
    )


def _profile_content_hash(profile: EquityCompanyProfile) -> bytes:
    """对合并后的公司概况标准字段计算哈希。"""
    return _content_hash(
        {
            "companyName": profile.company_name,
            "englishName": profile.english_name,
            "industry": profile.industry,
            "legalRepresentative": profile.legal_representative,
            "establishedOn": _date_text(profile.established_on),
            "website": profile.website,
            "email": profile.email,
            "phone": profile.phone,
            "registeredAddress": profile.registered_address,
            "officeAddress": profile.office_address,
            "mainBusiness": profile.main_business,
            "businessScope": profile.business_scope,
            "summary": profile.summary,
        }
    )


def _content_hash(value: Mapping[str, Any]) -> bytes:
    """用稳定 JSON 对标准业务字段计算 SHA-256。"""
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    ).digest()


def _date_text(value: date | None) -> str | None:
    """把可空日期转换为 ISO 文本。"""
    return None if value is None else value.isoformat()


def _decimal_text(value: Decimal | None) -> str | None:
    """把可空精确小数转换为字符串。"""
    return None if value is None else str(value)
