"""日频资金流方法学、日序列与 supplier ranking 的 Session 写仓储。"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import insert, select, update
from sqlalchemy.orm import Session

from service_data_sync.application.ports.money_flow import (
    MoneyFlowRepository,
    MoneyFlowSourceObservation,
    PublishedMoneyFlow,
)
from service_data_sync.domain.money_flow import (
    MoneyFlowDailyObservation,
    MoneyFlowMeasure,
    MoneyFlowMethodology,
    MoneyFlowRankingItem,
    MoneyFlowRankingSnapshot,
    MoneyFlowScope,
    MoneyFlowScopeType,
)
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.database.models.money_flow import (
    MoneyFlowBucketDefinition as BucketModel,
)
from service_data_sync.infrastructure.database.models.money_flow import (
    MoneyFlowDailyObservation as DailyObservationModel,
)
from service_data_sync.infrastructure.database.models.money_flow import (
    MoneyFlowMethodology as MethodologyModel,
)
from service_data_sync.infrastructure.database.models.money_flow import (
    MoneyFlowMethodologyScope as MethodologyScopeModel,
)
from service_data_sync.infrastructure.database.models.money_flow import (
    MoneyFlowMethodologyVersion as MethodologyVersionModel,
)
from service_data_sync.infrastructure.database.models.money_flow import (
    MoneyFlowMethodologyWindow as MethodologyWindowModel,
)
from service_data_sync.infrastructure.database.models.money_flow import (
    MoneyFlowQualityResult,
    MoneyFlowRankingManifest,
    MoneyFlowRankingMetric,
    MoneyFlowSeries,
    MoneyFlowUniverseVersion,
)
from service_data_sync.infrastructure.database.models.money_flow import (
    MoneyFlowRankingItem as RankingItemModel,
)
from service_data_sync.infrastructure.database.models.money_flow import (
    MoneyFlowRankingSnapshot as RankingSnapshotModel,
)
from service_data_sync.infrastructure.database.models.publication.dataset_publication import (
    DatasetPublication,
)
from service_data_sync.infrastructure.database.models.sector.catalog.sector_entity import (
    SectorEntity,
)
from service_data_sync.infrastructure.persistence.equity_identity_resolver import (
    EquityIdentityWriteConflictError,
    require_single_confirmed_identity_on_connection,
)
from service_data_sync.infrastructure.persistence.source_batch import (
    record_source_observation,
)

_DAILY_DATASET = "money_flow.daily"
_RANKING_DATASET = "money_flow.ranking"
_METHODOLOGY_DATASET = "money_flow.methodology"
_CATALOG_PARTITION = "catalog"


class SqlAlchemyMoneyFlowRepository(MoneyFlowRepository):
    """使用 ORM 表达式原子写入来源观测、知识修订、质量结果和 publication。"""

    def __init__(self, database: DatabaseClient) -> None:
        """保存服务私有短生命周期 Session 工厂。"""
        self._database = database

    def publish_daily(
        self,
        *,
        methodology: MoneyFlowMethodology,
        observations: Sequence[MoneyFlowDailyObservation],
        source: MoneyFlowSourceObservation,
        run_id: UUID | None = None,
        partition_key: str | None = None,
    ) -> PublishedMoneyFlow:
        """逐 trade_date 解析 canonical identity，并只为变化内容追加知识修订。"""
        if not observations:
            raise ValueError("money-flow daily observations must not be empty")
        scopes = {observation.scope for observation in observations}
        if len(scopes) != 1:
            raise ValueError("one money-flow daily batch must contain exactly one scope")
        self._validate_observation_measures(methodology, observations)
        now = datetime.now(UTC)
        resolved_partition = partition_key or _daily_partition_key(
            methodology, observations[0].scope
        )
        with self._database.transaction() as session:
            method = self._ensure_methodology(session, methodology, now=now)
            source_batch_id = _record_source(
                session,
                source=source,
                now=now,
                run_id=run_id,
                partition_key=partition_key,
            )
            try:
                equity_identities = self._preflight_equity_identity_batches(
                    session,
                    observations=observations,
                    known_at=now,
                )
            except EquityIdentityWriteConflictError:
                self._record_quality(
                    session,
                    dataset_kind="daily",
                    partition_key=resolved_partition,
                    rule_code="identity-resolution",
                    severity="error",
                    status="rejected",
                    affected_count=len(observations),
                    raw_uri=source.raw_uri,
                    now=now,
                )
                return PublishedMoneyFlow(
                    data_version=None,
                    inserted_count=0,
                    revised_count=0,
                    unchanged_count=0,
                    published=False,
                    quality_status="partial",
                )
            universe_id = self._ensure_universe(
                session,
                methodology=methodology,
                scope_type=observations[0].scope.scope_type,
                effective_from=min(item.trade_date for item in observations),
            )
            inserted = 0
            revised = 0
            unchanged = 0
            changed_series: dict[UUID, date] = {}
            for observation in observations:
                if observation.scope.scope_type is MoneyFlowScopeType.EQUITY:
                    scope_identity = equity_identities.get(observation.scope)
                else:
                    scope_identity = self._resolve_scope(
                        session,
                        observation.scope,
                        fact_date=observation.trade_date,
                        known_at=now,
                    )
                if scope_identity is None:
                    self._record_quality(
                        session,
                        dataset_kind="daily",
                        partition_key=resolved_partition,
                        rule_code="identity-resolution",
                        severity="error",
                        status="rejected",
                        affected_count=1,
                        raw_uri=source.raw_uri,
                        now=now,
                    )
                    continue
                bucket_id = method.bucket_ids[observation.bucket]
                series_id = self._ensure_series(
                    session,
                    methodology_version_id=method.version_id,
                    universe_version_id=universe_id,
                    bucket_id=bucket_id,
                    scope=observation.scope,
                    scope_identity=scope_identity,
                    window_type="daily_source",
                    window_size=1,
                    now=now,
                )
                outcome = self._write_daily_revision(
                    session,
                    series_id=series_id,
                    observation=observation,
                    source_batch_id=source_batch_id,
                    now=now,
                )
                if outcome == "inserted":
                    inserted += 1
                    changed_series[series_id] = max(
                        observation.trade_date,
                        changed_series.get(series_id, observation.trade_date),
                    )
                elif outcome == "revised":
                    revised += 1
                    changed_series[series_id] = max(
                        observation.trade_date,
                        changed_series.get(series_id, observation.trade_date),
                    )
                else:
                    unchanged += 1
            rejected = len(observations) - inserted - revised - unchanged
            if rejected:
                return PublishedMoneyFlow(
                    data_version=None,
                    inserted_count=inserted,
                    revised_count=revised,
                    unchanged_count=unchanged,
                    published=False,
                    quality_status="partial",
                )
            data_version: UUID | None = None
            if methodology.production_enabled:
                for series_id, effective_as_of in changed_series.items():
                    data_version = self._publish_dataset(
                        session,
                        dataset=_DAILY_DATASET,
                        partition_key=f"series:{series_id}",
                        effective_as_of=effective_as_of,
                        knowledge_cutoff=now,
                        now=now,
                    )
            return PublishedMoneyFlow(
                data_version=data_version,
                inserted_count=inserted,
                revised_count=revised,
                unchanged_count=unchanged,
                published=data_version is not None,
                quality_status="passed",
            )

    def publish_ranking(
        self,
        *,
        methodology: MoneyFlowMethodology,
        snapshot: MoneyFlowRankingSnapshot,
        source: MoneyFlowSourceObservation,
        run_id: UUID | None = None,
        partition_key: str | None = None,
    ) -> PublishedMoneyFlow:
        """验证上游 total 与全部日期感知身份后，发布不可变 supplier ranking。"""
        now = datetime.now(UTC)
        resolved_partition = partition_key or _ranking_partition_key(methodology, snapshot)
        with self._database.transaction() as session:
            method = self._ensure_methodology(session, methodology, now=now)
            source_batch_id = _record_source(
                session,
                source=source,
                now=now,
                run_id=run_id,
                partition_key=partition_key,
            )
            # 当前 AKShare SDK 只返回合并 DataFrame，无法证明页号连续与 total；
            # raw 仍保留，但这种观测不能进入 canonical ranking publication。
            if not snapshot.is_complete:
                self._record_quality(
                    session,
                    dataset_kind="ranking",
                    partition_key=resolved_partition,
                    rule_code="ranking-completeness",
                    severity="error",
                    status="rejected",
                    affected_count=len({item.supplier_position for item in snapshot.items}),
                    raw_uri=source.raw_uri,
                    now=now,
                )
                return PublishedMoneyFlow(
                    data_version=None,
                    inserted_count=0,
                    revised_count=0,
                    unchanged_count=0,
                    published=False,
                    quality_status="partial",
                )
            universe_id = self._ensure_universe(
                session,
                methodology=methodology,
                scope_type=snapshot.scope_type,
                effective_from=snapshot.target_trade_date,
            )
            resolved_items = self._resolve_ranking_items(session, snapshot=snapshot, known_at=now)
            if resolved_items is None:
                self._record_quality(
                    session,
                    dataset_kind="ranking",
                    partition_key=resolved_partition,
                    rule_code="identity-resolution",
                    severity="error",
                    status="rejected",
                    affected_count=len({item.supplier_position for item in snapshot.items}),
                    raw_uri=source.raw_uri,
                    now=now,
                )
                return PublishedMoneyFlow(
                    data_version=None,
                    inserted_count=0,
                    revised_count=0,
                    unchanged_count=0,
                    published=False,
                    quality_status="partial",
                )
            business_hash = _ranking_hash(snapshot, resolved_items)
            ranking_bucket_id = method.bucket_ids[snapshot.ranking_bucket]
            current = self._current_ranking(
                session,
                method_version_id=method.version_id,
                universe_version_id=universe_id,
                snapshot=snapshot,
                ranking_bucket_id=ranking_bucket_id,
            )
            if current is not None and current["business_hash"] == business_hash:
                return PublishedMoneyFlow(
                    data_version=_current_data_version(
                        session, _RANKING_DATASET, resolved_partition
                    ),
                    inserted_count=0,
                    revised_count=0,
                    unchanged_count=len(resolved_items),
                    published=methodology.production_enabled,
                    quality_status=str(current["quality_status"]),
                )
            revision = 1
            if current is not None:
                revision = int(current["revision"]) + 1
                session.execute(
                    update(RankingSnapshotModel)
                    .where(
                        RankingSnapshotModel.target_trade_date == snapshot.target_trade_date,
                        RankingSnapshotModel.snapshot_id == current["snapshot_id"],
                    )
                    .values(status="superseded", superseded_at=now)
                )
            snapshot_id = uuid4()
            session.execute(
                insert(RankingSnapshotModel).values(
                    target_trade_date=snapshot.target_trade_date,
                    snapshot_id=snapshot_id,
                    methodology_version_id=method.version_id,
                    scope_type=snapshot.scope_type.value,
                    universe_version_id=universe_id,
                    window_type=snapshot.window_type.value,
                    window_size=snapshot.window_size,
                    ranking_bucket_id=ranking_bucket_id,
                    ranking_basis=snapshot.ranking_basis,
                    source_cutoff_at=snapshot.source_cutoff_at,
                    observed_at=snapshot.observed_at,
                    revision=revision,
                    row_count=len(resolved_items),
                    business_hash=business_hash,
                    quality_status="passed",
                    status="published",
                    published_at=now,
                    superseded_at=None,
                )
            )
            for position, (scope, identity, metrics) in sorted(resolved_items.items()):
                session.execute(
                    insert(RankingItemModel).values(
                        target_trade_date=snapshot.target_trade_date,
                        snapshot_id=snapshot_id,
                        supplier_position=position,
                        scope_type=scope.scope_type.value,
                        security_id=(
                            identity if scope.scope_type is MoneyFlowScopeType.EQUITY else None
                        ),
                        sector_key=(
                            identity if scope.scope_type is MoneyFlowScopeType.SECTOR else None
                        ),
                        scope_name_at_snapshot=scope.name,
                    )
                )
                for metric in metrics:
                    session.execute(
                        insert(MoneyFlowRankingMetric).values(
                            target_trade_date=snapshot.target_trade_date,
                            snapshot_id=snapshot_id,
                            supplier_position=position,
                            bucket_id=method.bucket_ids[metric.bucket],
                            gross_inflow=metric.gross_inflow,
                            gross_outflow=metric.gross_outflow,
                            net_amount=metric.net_amount,
                            net_ratio=metric.net_ratio,
                        )
                    )
            session.execute(
                insert(MoneyFlowRankingManifest).values(
                    target_trade_date=snapshot.target_trade_date,
                    snapshot_id=snapshot_id,
                    source_batch_id=source_batch_id,
                    source_row_count=len(resolved_items),
                    upstream_total=len(resolved_items),
                    completeness_basis=snapshot.completeness_basis,
                    is_complete=True,
                )
            )
            data_version = (
                self._publish_dataset(
                    session,
                    dataset=_RANKING_DATASET,
                    partition_key=resolved_partition,
                    effective_as_of=snapshot.target_trade_date,
                    knowledge_cutoff=now,
                    now=now,
                )
                if methodology.production_enabled
                else None
            )
            return PublishedMoneyFlow(
                data_version=data_version,
                inserted_count=len(resolved_items) if current is None else 0,
                revised_count=len(resolved_items) if current is not None else 0,
                unchanged_count=0,
                published=data_version is not None,
                quality_status="passed",
            )

    @staticmethod
    def _validate_observation_measures(
        methodology: MoneyFlowMethodology,
        observations: Sequence[MoneyFlowDailyObservation],
    ) -> None:
        """校验未支持度量恒空，已支持的净度量不被全批缺失掩盖。"""
        supports = methodology.supported_measures
        value_by_measure = {
            MoneyFlowMeasure.GROSS_INFLOW: tuple(item.gross_inflow for item in observations),
            MoneyFlowMeasure.GROSS_OUTFLOW: tuple(item.gross_outflow for item in observations),
            MoneyFlowMeasure.NET_AMOUNT: tuple(item.net_amount for item in observations),
            MoneyFlowMeasure.NET_RATIO: tuple(item.net_ratio for item in observations),
        }
        for measure, values in value_by_measure.items():
            if measure not in supports and any(value is not None for value in values):
                raise ValueError(f"unsupported money-flow measure {measure.value} must stay null")
            if measure in supports and all(value is None for value in values):
                raise ValueError(
                    f"supported money-flow measure {measure.value} is entirely missing"
                )

    def _ensure_methodology(
        self, session: Session, methodology: MoneyFlowMethodology, *, now: datetime
    ) -> _StoredMethodology:
        """创建或验证不可变方法学版本，并在新增时推进内部目录 publication。"""
        stable = (
            session.execute(
                select(MethodologyModel.methodology_id).where(
                    MethodologyModel.public_key == methodology.public_key
                )
            )
            .mappings()
            .one_or_none()
        )
        created = stable is None
        methodology_id = uuid4() if stable is None else UUID(str(stable["methodology_id"]))
        if stable is None:
            session.execute(
                insert(MethodologyModel).values(
                    methodology_id=methodology_id,
                    public_key=methodology.public_key,
                    owner="service-data-sync",
                    created_at=now,
                )
            )
        version_row = (
            session.execute(
                select(*MethodologyVersionModel.__table__.c).where(
                    MethodologyVersionModel.methodology_id == methodology_id,
                    MethodologyVersionModel.version == methodology.version,
                )
            )
            .mappings()
            .one_or_none()
        )
        version_id = uuid4() if version_row is None else UUID(str(version_row["version_id"]))
        expected = _methodology_version_values(
            methodology, methodology_id=methodology_id, version_id=version_id, now=now
        )
        if version_row is None:
            created = True
            session.execute(insert(MethodologyVersionModel).values(**expected))
            for scope_type in methodology.scope_types:
                session.execute(
                    insert(MethodologyScopeModel).values(
                        version_id=version_id,
                        scope_type=scope_type.value,
                        universe_id=_universe_for_scope(methodology, scope_type),
                    )
                )
            for window_type, window_size, source_label in methodology.windows:
                session.execute(
                    insert(MethodologyWindowModel).values(
                        version_id=version_id,
                        window_type=window_type.value,
                        window_size=window_size,
                        source_label=source_label,
                    )
                )
            bucket_ids: dict[str, UUID] = {}
            for bucket in methodology.buckets:
                bucket_id = uuid4()
                bucket_ids[bucket.code] = bucket_id
                session.execute(
                    insert(BucketModel).values(
                        bucket_id=bucket_id,
                        version_id=version_id,
                        bucket_code=bucket.code,
                        label=bucket.label,
                        definition_status=bucket.definition_status,
                        threshold_min=bucket.threshold_min,
                        threshold_max=bucket.threshold_max,
                        threshold_unit=bucket.threshold_unit,
                    )
                )
        else:
            _assert_immutable_methodology(dict(version_row), expected)
            bucket_ids = {
                str(row["bucket_code"]): UUID(str(row["bucket_id"]))
                for row in session.execute(
                    select(BucketModel.bucket_code, BucketModel.bucket_id).where(
                        BucketModel.version_id == version_id
                    )
                )
                .mappings()
                .all()
            }
            if set(bucket_ids) != {bucket.code for bucket in methodology.buckets}:
                raise RuntimeError("stored money-flow methodology bucket set changed")
        if created:
            self._publish_dataset(
                session,
                dataset=_METHODOLOGY_DATASET,
                partition_key=_CATALOG_PARTITION,
                effective_as_of=None,
                knowledge_cutoff=now,
                now=now,
            )
        return _StoredMethodology(version_id=version_id, bucket_ids=bucket_ids)

    @staticmethod
    def _ensure_universe(
        session: Session,
        *,
        methodology: MoneyFlowMethodology,
        scope_type: MoneyFlowScopeType,
        effective_from: date,
    ) -> UUID:
        """复用当前 universe 版本；未知成员集合通过稳定 unknown 哈希明确表达。"""
        universe_code = _universe_for_scope(methodology, scope_type)
        current = (
            session.execute(
                select(MoneyFlowUniverseVersion.universe_version_id).where(
                    MoneyFlowUniverseVersion.universe_code == universe_code,
                    MoneyFlowUniverseVersion.scope_type == scope_type.value,
                    MoneyFlowUniverseVersion.effective_to.is_(None),
                )
            )
            .mappings()
            .one_or_none()
        )
        if current is not None:
            return UUID(str(current["universe_version_id"]))
        universe_version_id = uuid4()
        content_hash = hashlib.sha256(
            f"unknown:{universe_code}:{scope_type.value}".encode()
        ).hexdigest()
        session.execute(
            insert(MoneyFlowUniverseVersion).values(
                universe_version_id=universe_version_id,
                universe_code=universe_code,
                scope_type=scope_type.value,
                identity_data_version=None,
                membership_release_id=None,
                effective_from=effective_from,
                effective_to=None,
                member_count=None,
                content_sha256=content_hash,
            )
        )
        return universe_version_id

    @staticmethod
    def _preflight_equity_identity_batches(
        session: Session,
        *,
        observations: Sequence[MoneyFlowDailyObservation],
        known_at: datetime,
    ) -> dict[MoneyFlowScope, int]:
        """逐来源证券批次检查全部事实日，跨代码复用边界时整批拒绝。"""
        fact_dates_by_scope: dict[MoneyFlowScope, set[date]] = defaultdict(set)
        for observation in observations:
            if observation.scope.scope_type is MoneyFlowScopeType.EQUITY:
                fact_dates_by_scope[observation.scope].add(observation.trade_date)
        resolved: dict[MoneyFlowScope, int] = {}
        for scope, fact_dates in fact_dates_by_scope.items():
            if scope.exchange is None or scope.symbol is None:
                raise EquityIdentityWriteConflictError(
                    "equity money-flow scope lacks exchange or symbol"
                )
            resolved[scope] = require_single_confirmed_identity_on_connection(
                session,
                exchange=scope.exchange,
                symbol=scope.symbol,
                fact_dates=tuple(fact_dates),
                known_at=known_at,
            )
        return resolved

    @staticmethod
    def _resolve_scope(
        session: Session,
        scope: MoneyFlowScope,
        *,
        fact_date: date,
        known_at: datetime,
    ) -> int | str | None:
        """按事实日解析证券，或读取稳定 sector/market 身份，绝不 current get-or-create。"""
        if scope.scope_type is MoneyFlowScopeType.EQUITY:
            if scope.exchange is None or scope.symbol is None:
                return None
            try:
                return require_single_confirmed_identity_on_connection(
                    session,
                    exchange=scope.exchange,
                    symbol=scope.symbol,
                    fact_dates=(fact_date,),
                    known_at=known_at,
                )
            except EquityIdentityWriteConflictError:
                return None
        if scope.scope_type is MoneyFlowScopeType.SECTOR:
            if scope.sector_scheme is None or scope.sector_code is None:
                return None
            row = (
                session.execute(
                    select(SectorEntity.sector_key).where(
                        SectorEntity.scheme == scope.sector_scheme,
                        SectorEntity.sector_code == scope.sector_code,
                        SectorEntity.status == "ACTIVE",
                    )
                )
                .mappings()
                .one_or_none()
            )
            return None if row is None else int(row["sector_key"])
        return scope.market_code

    @staticmethod
    def _ensure_series(
        session: Session,
        *,
        methodology_version_id: UUID,
        universe_version_id: UUID,
        bucket_id: UUID,
        scope: MoneyFlowScope,
        scope_identity: int | str,
        window_type: str,
        window_size: int,
        now: datetime,
    ) -> UUID:
        """按完整强身份复用或创建 active series。"""
        conditions = [
            MoneyFlowSeries.methodology_version_id == methodology_version_id,
            MoneyFlowSeries.scope_type == scope.scope_type.value,
            MoneyFlowSeries.universe_version_id == universe_version_id,
            MoneyFlowSeries.bucket_id == bucket_id,
            MoneyFlowSeries.window_type == window_type,
            MoneyFlowSeries.window_size == window_size,
            MoneyFlowSeries.retired_at.is_(None),
        ]
        if scope.scope_type is MoneyFlowScopeType.EQUITY:
            conditions.append(MoneyFlowSeries.security_id == int(scope_identity))
        elif scope.scope_type is MoneyFlowScopeType.SECTOR:
            conditions.append(MoneyFlowSeries.sector_key == int(scope_identity))
        else:
            conditions.append(MoneyFlowSeries.market_code == str(scope_identity))
        row = (
            session.execute(select(MoneyFlowSeries.series_id).where(*conditions))
            .mappings()
            .one_or_none()
        )
        if row is not None:
            return UUID(str(row["series_id"]))
        series_id = uuid4()
        session.execute(
            insert(MoneyFlowSeries).values(
                series_id=series_id,
                methodology_version_id=methodology_version_id,
                scope_type=scope.scope_type.value,
                security_id=(
                    int(scope_identity) if scope.scope_type is MoneyFlowScopeType.EQUITY else None
                ),
                sector_key=(
                    int(scope_identity) if scope.scope_type is MoneyFlowScopeType.SECTOR else None
                ),
                market_code=(
                    str(scope_identity) if scope.scope_type is MoneyFlowScopeType.MARKET else None
                ),
                universe_version_id=universe_version_id,
                bucket_id=bucket_id,
                window_type=window_type,
                window_size=window_size,
                created_at=now,
                retired_at=None,
            )
        )
        return series_id

    @staticmethod
    def _write_daily_revision(
        session: Session,
        *,
        series_id: UUID,
        observation: MoneyFlowDailyObservation,
        source_batch_id: UUID,
        now: datetime,
    ) -> str:
        """比较 canonical hash，相同内容 no-op，变化时关闭旧 current 并追加 revision。"""
        content_hash = _daily_hash(observation)
        current = (
            session.execute(
                select(
                    DailyObservationModel.observation_id,
                    DailyObservationModel.revision,
                    DailyObservationModel.content_sha256,
                )
                .where(
                    DailyObservationModel.series_id == series_id,
                    DailyObservationModel.trade_date == observation.trade_date,
                    DailyObservationModel.known_to.is_(None),
                )
                .with_for_update()
            )
            .mappings()
            .one_or_none()
        )
        if current is not None and current["content_sha256"] == content_hash:
            return "unchanged"
        revision = 1
        outcome = "inserted"
        if current is not None:
            revision = int(current["revision"]) + 1
            outcome = "revised"
            session.execute(
                update(DailyObservationModel)
                .where(
                    DailyObservationModel.trade_date == observation.trade_date,
                    DailyObservationModel.observation_id == current["observation_id"],
                )
                .values(known_to=now)
            )
        session.execute(
            insert(DailyObservationModel).values(
                trade_date=observation.trade_date,
                observation_id=uuid4(),
                series_id=series_id,
                revision=revision,
                gross_inflow=observation.gross_inflow,
                gross_outflow=observation.gross_outflow,
                net_amount=observation.net_amount,
                net_ratio=observation.net_ratio,
                source_batch_id=source_batch_id,
                observed_at=observation.observed_at,
                known_from=now,
                known_to=None,
                content_sha256=content_hash,
                quality_status="passed",
            )
        )
        return outcome

    def _resolve_ranking_items(
        self,
        session: Session,
        *,
        snapshot: MoneyFlowRankingSnapshot,
        known_at: datetime,
    ) -> dict[int, tuple[MoneyFlowScope, int, tuple[MoneyFlowRankingItem, ...]]] | None:
        """按目标日解析每个供应商位置，任一零解、多解或重复 scope 均整批拒绝。"""
        grouped: dict[int, list[MoneyFlowRankingItem]] = defaultdict(list)
        for item in snapshot.items:
            grouped[item.supplier_position].append(item)
        resolved: dict[int, tuple[MoneyFlowScope, int, tuple[MoneyFlowRankingItem, ...]]] = {}
        identities: set[int] = set()
        for position, metrics in grouped.items():
            scope = metrics[0].scope
            identity = self._resolve_scope(
                session,
                scope,
                fact_date=snapshot.target_trade_date,
                known_at=known_at,
            )
            if not isinstance(identity, int) or identity in identities:
                return None
            identities.add(identity)
            resolved[position] = (scope, identity, tuple(metrics))
        return resolved

    @staticmethod
    def _current_ranking(
        session: Session,
        *,
        method_version_id: UUID,
        universe_version_id: UUID,
        snapshot: MoneyFlowRankingSnapshot,
        ranking_bucket_id: UUID,
    ) -> Mapping[str, Any] | None:
        """锁定同逻辑身份 current ranking，供 no-op 或 revision 替换。"""
        row = (
            session.execute(
                select(
                    RankingSnapshotModel.snapshot_id,
                    RankingSnapshotModel.revision,
                    RankingSnapshotModel.business_hash,
                    RankingSnapshotModel.quality_status,
                )
                .where(
                    RankingSnapshotModel.target_trade_date == snapshot.target_trade_date,
                    RankingSnapshotModel.methodology_version_id == method_version_id,
                    RankingSnapshotModel.scope_type == snapshot.scope_type.value,
                    RankingSnapshotModel.universe_version_id == universe_version_id,
                    RankingSnapshotModel.window_type == snapshot.window_type.value,
                    RankingSnapshotModel.window_size == snapshot.window_size,
                    RankingSnapshotModel.ranking_bucket_id == ranking_bucket_id,
                    RankingSnapshotModel.superseded_at.is_(None),
                )
                .with_for_update()
            )
            .mappings()
            .one_or_none()
        )
        return None if row is None else dict(row)

    @staticmethod
    def _record_quality(
        session: Session,
        *,
        dataset_kind: str,
        partition_key: str,
        rule_code: str,
        severity: str,
        status: str,
        affected_count: int,
        raw_uri: str,
        now: datetime,
    ) -> None:
        """保存阻断或告警结果，并只引用 raw URI。"""
        session.execute(
            insert(MoneyFlowQualityResult).values(
                result_id=uuid4(),
                dataset_kind=dataset_kind,
                partition_key=partition_key,
                rule_code=rule_code,
                severity=severity,
                status=status,
                actual_value=None,
                threshold_value=None,
                affected_count=affected_count,
                sample_raw_uri=raw_uri,
                created_at=now,
            )
        )

    @staticmethod
    def _publish_dataset(
        session: Session,
        *,
        dataset: str,
        partition_key: str,
        effective_as_of: date | None,
        knowledge_cutoff: datetime,
        now: datetime,
    ) -> UUID:
        """原子 supersede 旧指针并发布不可变 dataVersion。"""
        current = (
            session.execute(
                select(DatasetPublication.publication_id)
                .where(
                    DatasetPublication.dataset == dataset,
                    DatasetPublication.partition_key == partition_key,
                    DatasetPublication.superseded_at.is_(None),
                )
                .with_for_update()
            )
            .mappings()
            .one_or_none()
        )
        if current is not None:
            session.execute(
                update(DatasetPublication)
                .where(DatasetPublication.publication_id == current["publication_id"])
                .values(superseded_at=now)
            )
        data_version = uuid4()
        session.execute(
            insert(DatasetPublication).values(
                publication_id=uuid4(),
                dataset=dataset,
                partition_key=partition_key,
                data_version=data_version,
                quality_status="passed",
                published_at=now,
                superseded_at=None,
                effective_as_of=effective_as_of,
                knowledge_cutoff=knowledge_cutoff,
            )
        )
        return data_version


class _StoredMethodology:
    """携带同一事务内后续写入需要的方法学版本与 bucket 身份。"""

    def __init__(self, *, version_id: UUID, bucket_ids: dict[str, UUID]) -> None:
        """保存不可变版本和 bucket 映射。"""
        self.version_id = version_id
        self.bucket_ids = bucket_ids


def _record_source(
    session: Session,
    *,
    source: MoneyFlowSourceObservation,
    now: datetime,
    run_id: UUID | None,
    partition_key: str | None,
) -> UUID:
    """登记一次不可折叠 source observation，并复用已有或手工 run。"""
    return record_source_observation(
        session,
        provider_id=source.provider_id,
        capability=source.capability,
        source_payload_sha256=source.source_payload_sha256,
        raw_uri=source.raw_uri,
        observed_at=source.observed_at,
        created_at=now,
        upstream_source=source.upstream_source,
        adapter_version=source.adapter_version,
        schema_fingerprint=source.schema_fingerprint,
        run_id=run_id,
        partition_key=partition_key,
    )


def _methodology_version_values(
    methodology: MoneyFlowMethodology,
    *,
    methodology_id: UUID,
    version_id: UUID,
    now: datetime,
) -> dict[str, object]:
    """将领域方法学投影为不可变版本列。"""
    supports = methodology.supported_measures
    return {
        "version_id": version_id,
        "methodology_id": methodology_id,
        "version": methodology.version,
        "status": methodology.status,
        "adapter_provider": methodology.adapter_provider,
        "upstream_source": methodology.upstream_source,
        "source_dataset": methodology.source_dataset,
        "semantic_family": methodology.semantic_family.value,
        "direction_definition": methodology.direction_definition,
        "ratio_denominator": methodology.ratio_denominator,
        "finality": methodology.finality.value,
        "currency": methodology.currency,
        "raw_amount_unit": methodology.raw_amount_unit,
        "standard_amount_unit": methodology.standard_amount_unit,
        "conversion_version": methodology.conversion_version,
        "supports_gross_inflow": MoneyFlowMeasure.GROSS_INFLOW in supports,
        "supports_gross_outflow": MoneyFlowMeasure.GROSS_OUTFLOW in supports,
        "supports_net_amount": MoneyFlowMeasure.NET_AMOUNT in supports,
        "supports_net_ratio": MoneyFlowMeasure.NET_RATIO in supports,
        "production_enabled": methodology.production_enabled,
        "effective_from": now,
        "retired_at": None,
    }


def _assert_immutable_methodology(
    stored: Mapping[str, Any], expected: Mapping[str, object]
) -> None:
    """拒绝同 public key/version 下来源、单位或算法被静默改写。"""
    immutable_keys = (
        "status",
        "adapter_provider",
        "upstream_source",
        "source_dataset",
        "semantic_family",
        "direction_definition",
        "ratio_denominator",
        "finality",
        "currency",
        "raw_amount_unit",
        "standard_amount_unit",
        "conversion_version",
        "supports_gross_inflow",
        "supports_gross_outflow",
        "supports_net_amount",
        "supports_net_ratio",
        "production_enabled",
    )
    if any(stored[key] != expected[key] for key in immutable_keys):
        raise RuntimeError("money-flow methodology version is immutable")


def _universe_for_scope(methodology: MoneyFlowMethodology, scope_type: MoneyFlowScopeType) -> str:
    """按 scope 选择显式 universe，拒绝隐式跨 universe fallback。"""
    if scope_type is MoneyFlowScopeType.SECTOR and "eastmoney-industry" in methodology.universe_ids:
        return "eastmoney-industry"
    if "cn-a" in methodology.universe_ids:
        return "cn-a"
    if len(methodology.universe_ids) == 1:
        return methodology.universe_ids[0]
    raise ValueError("money-flow methodology has no unambiguous universe for scope")


def _daily_hash(observation: MoneyFlowDailyObservation) -> str:
    """计算固定四度量的 canonical business hash。"""
    return _hash_json(
        {
            "grossInflow": _decimal_json(observation.gross_inflow),
            "grossOutflow": _decimal_json(observation.gross_outflow),
            "netAmount": _decimal_json(observation.net_amount),
            "netRatio": _decimal_json(observation.net_ratio),
        }
    )


def _ranking_hash(
    snapshot: MoneyFlowRankingSnapshot,
    resolved: Mapping[int, tuple[MoneyFlowScope, int, tuple[MoneyFlowRankingItem, ...]]],
) -> str:
    """计算 supplier position、canonical identity 与全部 bucket 度量哈希。"""
    return _hash_json(
        {
            "targetTradeDate": snapshot.target_trade_date.isoformat(),
            "window": [snapshot.window_type.value, snapshot.window_size],
            "rankingBucket": snapshot.ranking_bucket,
            "items": [
                {
                    "position": position,
                    "identity": identity,
                    "metrics": [
                        {
                            "bucket": metric.bucket,
                            "grossInflow": _decimal_json(metric.gross_inflow),
                            "grossOutflow": _decimal_json(metric.gross_outflow),
                            "netAmount": _decimal_json(metric.net_amount),
                            "netRatio": _decimal_json(metric.net_ratio),
                        }
                        for metric in sorted(metrics, key=lambda item: item.bucket)
                    ],
                }
                for position, (_scope, identity, metrics) in sorted(resolved.items())
            ],
        }
    )


def _hash_json(value: object) -> str:
    """以稳定 JSON 编码计算 SHA-256。"""
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _decimal_json(value: Decimal | None) -> str | None:
    """把精确数值编码为无科学计数法文本。"""
    return None if value is None else format(value, "f")


def _daily_partition_key(methodology: MoneyFlowMethodology, scope: MoneyFlowScope) -> str:
    """生成不依赖数据库主键的日序列运行分区键。"""
    return (
        f"{methodology.public_key}/{methodology.version}/daily/"
        f"{scope.scope_type.value}/{scope.exchange or scope.sector_scheme or scope.market_code}/"
        f"{scope.symbol or scope.sector_code or scope.market_code}"
    )


def _ranking_partition_key(
    methodology: MoneyFlowMethodology, snapshot: MoneyFlowRankingSnapshot
) -> str:
    """生成供应商排行稳定运行分区键。"""
    return (
        f"{methodology.public_key}/{methodology.version}/ranking/"
        f"{snapshot.scope_type.value}/{snapshot.universe_id}/"
        f"{snapshot.window_type.value}/{snapshot.window_size}/"
        f"{snapshot.ranking_bucket}/{snapshot.target_trade_date.isoformat()}"
    )


def _current_data_version(session: Session, dataset: str, partition_key: str) -> UUID | None:
    """读取一个 current publication 的 dataVersion。"""
    value = session.execute(
        select(DatasetPublication.data_version).where(
            DatasetPublication.dataset == dataset,
            DatasetPublication.partition_key == partition_key,
            DatasetPublication.superseded_at.is_(None),
        )
    ).scalar_one_or_none()
    return None if value is None else UUID(str(value))


__all__ = ["SqlAlchemyMoneyFlowRepository"]
