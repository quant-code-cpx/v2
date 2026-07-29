"""衍生品 `P0` 真实合约日线的原子 `canonical` 发布仓储。

只接受明确场所和真实合约代码，连续合约、名称猜测或用收盘价替代结算价均不能进入
发布。首次可信日线可创建不虚构挂牌、到期或产品规格的最小身份；以后每次内容变化
追加不可变 `revision`，完整分区血缘确保未变化日期仍可回溯最初来源证据。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from service_data_sync.application.ports.canonical_release import (
    CanonicalLineageRecord,
    CanonicalQualityDecision,
    CanonicalQualityRule,
    CanonicalReleaseCandidate,
)
from service_data_sync.application.ports.derivative_market import (
    DerivativeDailyBarRepository,
    DerivativeSourceObservation,
    PublishedDerivativeDailyBars,
)
from service_data_sync.domain.derivative import DerivativeContractIdentifier, DerivativeDailyBar
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.database.models.canonical import (
    CanonicalCheckpoint,
    CanonicalDataset,
    DataSource,
    MethodologyVersion,
    NormalizationRun,
    NormalizedRecordManifest,
    RawPayloadManifest,
    SourceDataset,
)
from service_data_sync.infrastructure.database.models.market.derivative_revisions import (
    DerivativeDailyBarRevision,
)
from service_data_sync.infrastructure.database.models.market.identity import (
    DerivativeContract,
    InstrumentIdentifierVersion,
    MarketEntity,
    MarketInstrument,
    TradingVenue,
)
from service_data_sync.infrastructure.database.models.provenance.source_batch import SourceBatch
from service_data_sync.infrastructure.persistence.canonical_release_repository import (
    SqlAlchemyCanonicalReleaseRepository,
)
from service_data_sync.infrastructure.persistence.source_batch import record_source_observation

_DATASET_CODE = "derivative.bar.1d.reported"
_METHODOLOGY_CODE = "derivative-real-contract-daily-bar"
_MAPPING_VERSION = "derivative-daily-bar-v1"
_IDENTIFIER_SCHEME = "venue_contract_code"
_DOMESTIC_DERIVATIVE_VENUES = {
    "CFFEX": "中国金融期货交易所",
    "SHFE": "上海期货交易所",
    "DCE": "大连商品交易所",
    "CZCE": "郑州商品交易所",
    "GFEX": "广州期货交易所",
    "INE": "上海国际能源交易中心",
}


@dataclass(frozen=True, slots=True)
class DerivativeSourceApproval:
    """记录一个已通过来源、留存与内部使用审查的 provider-neutral adapter 身份。"""

    provider_id: str
    source_code: str
    legal_name: str
    source_kind: str
    rights_status: str
    license_scope: str

    def __post_init__(self) -> None:
        """拒绝缺少权限归属或许可范围的批准项，防止普通 adapter 被误作可发布来源。"""
        if not all(
            value.strip()
            for value in (
                self.provider_id,
                self.source_code,
                self.legal_name,
                self.source_kind,
                self.rights_status,
                self.license_scope,
            )
        ):
            raise ValueError("derivative source approval is incomplete")


@dataclass(frozen=True, slots=True)
class _PreparedBar:
    """封装一个新 revision 的业务值、哈希和顺序，避免写入时重新解释 adapter 载荷。"""

    bar: DerivativeDailyBar
    content_hash: str
    revision_no: int


class SqlAlchemyDerivativeDailyBarRepository(DerivativeDailyBarRepository):
    """发布真实合约日线，并为首次可信观察建立可空规格的最小身份。"""

    def __init__(
        self,
        database: DatabaseClient,
        *,
        approved_sources: Mapping[str, DerivativeSourceApproval] | None = None,
    ) -> None:
        """保存私有数据库与显式批准表；默认空表使未配置来源保持 fail-closed。"""
        self._database = database
        self._approved_sources = dict(approved_sources or {})
        self._release_repository = SqlAlchemyCanonicalReleaseRepository(database)

    def publish_daily_bars(
        self,
        *,
        contract: DerivativeContractIdentifier,
        bars: Sequence[DerivativeDailyBar],
        source: DerivativeSourceObservation,
    ) -> PublishedDerivativeDailyBars:
        """写入一个真实合约的完整当前快照，并把 revision、血缘和 publication 放入同一事务。"""
        normalized_bars = tuple(bars)
        if not normalized_bars or len({bar.trade_date for bar in normalized_bars}) != len(
            normalized_bars
        ):
            raise ValueError("derivative daily bars must be non-empty and unique by trade date")
        approval = self._approved_sources.get(source.provider_id)
        if approval is None:
            raise ValueError("derivative source provider is not approved for publication")
        prepared_changes: list[_PreparedBar] = []
        resolved_contract_id: UUID | None = None
        source_batch_id: UUID | None = None

        def prepare_candidate(session: Session) -> CanonicalReleaseCandidate:
            """在发布事务中固化来源、运行和当前快照，保证内容摘要覆盖完整合约分区。"""
            nonlocal resolved_contract_id, source_batch_id
            now = datetime.now(UTC)
            dataset_id = _ensure_dataset(session, now=now)
            methodology_id = _ensure_methodology(session)
            source_dataset_id = _ensure_source_dataset(session, approval=approval, source=source)
            source_batch_id = _record_source_batch(
                session,
                source=source,
                source_dataset_id=source_dataset_id,
                now=now,
            )
            contract_id = _ensure_contract_id(
                session,
                contract=contract,
                fact_start=min(bar.trade_date for bar in normalized_bars),
                fact_end=max(bar.trade_date for bar in normalized_bars),
                source_batch_id=source_batch_id,
                now=now,
            )
            resolved_contract_id = contract_id
            # publication 分区必须只依赖永久合约 UUID，代码复用或展示变化不能改变消费者快照边界。
            partition_key = f"contract:{contract_id}"
            normalization_run_id = _record_normalization_run(
                session,
                dataset_id=dataset_id,
                partition_key=partition_key,
                source=source,
                source_batch_id=source_batch_id,
                now=now,
            )
            current = _current_bars(
                session, contract_id=contract_id, methodology_version_id=methodology_id
            )
            incoming = {bar.trade_date: bar for bar in normalized_bars}
            prepared_changes[:] = [
                _PreparedBar(
                    bar=bar,
                    content_hash=_bar_content_hash(bar),
                    revision_no=(
                        current[bar.trade_date].revision_no + 1 if bar.trade_date in current else 1
                    ),
                )
                for bar in normalized_bars
                if bar.trade_date not in current
                or _bar_content_hash(bar) != current[bar.trade_date].content_hash
            ]
            snapshot_records = _lineage_records(
                current=current,
                incoming=incoming,
                changed={item.bar.trade_date: item for item in prepared_changes},
                contract_id=contract_id,
                source_batch_id=source_batch_id,
            )
            checkpoint = session.execute(
                select(CanonicalCheckpoint.fencing_token)
                .where(
                    CanonicalCheckpoint.dataset_id == dataset_id,
                    CanonicalCheckpoint.partition_key == partition_key,
                    CanonicalCheckpoint.checkpoint_kind == "published",
                )
                .with_for_update()
            ).scalar_one_or_none()
            fact_dates = tuple(sorted({*current, *incoming}))
            return CanonicalReleaseCandidate(
                dataset_id=dataset_id,
                dataset_code=_DATASET_CODE,
                partition_key=partition_key,
                methodology_version_id=methodology_id,
                normalization_run_id=normalization_run_id,
                records=snapshot_records,
                quality=CanonicalQualityDecision(
                    status="passed",
                    policy_code="derivative.daily-bar.quality",
                    policy_version=1,
                    rules=(CanonicalQualityRule("real-contract-ohlc", "blocking", True),),
                ),
                fact_min=min(fact_dates),
                fact_max=max(fact_dates),
                checkpoint_kind="published",
                checkpoint_position={"tradeDate": max(fact_dates).isoformat()},
                expected_fencing_token=0 if checkpoint is None else int(checkpoint),
                created_at=now,
            )

        def write_facts(
            session: Session, candidate: CanonicalReleaseCandidate, release_id: UUID
        ) -> None:
            """仅为内容变化日期关闭旧知识区间并插入新 revision，未变化行复用旧 release 内容。"""
            if resolved_contract_id is None or source_batch_id is None:
                raise AssertionError(
                    "derivative release preparation did not resolve required identities"
                )
            for prepared in prepared_changes:
                session.execute(
                    update(DerivativeDailyBarRevision)
                    .where(
                        DerivativeDailyBarRevision.contract_id == resolved_contract_id,
                        DerivativeDailyBarRevision.trade_date == prepared.bar.trade_date,
                        DerivativeDailyBarRevision.methodology_version_id
                        == candidate.methodology_version_id,
                        DerivativeDailyBarRevision.known_to.is_(None),
                    )
                    .values(known_to=candidate.created_at)
                )
                row_id = uuid4()
                session.execute(
                    insert(DerivativeDailyBarRevision).values(
                        trade_date=prepared.bar.trade_date,
                        row_id=row_id,
                        contract_id=resolved_contract_id,
                        open_price=prepared.bar.open_price,
                        high_price=prepared.bar.high_price,
                        low_price=prepared.bar.low_price,
                        close_price=prepared.bar.close_price,
                        pre_close_price=prepared.bar.pre_close_price,
                        settlement_price=prepared.bar.settlement_price,
                        pre_settlement_price=prepared.bar.pre_settlement_price,
                        volume_value=prepared.bar.volume_value,
                        open_interest_value=prepared.bar.open_interest_value,
                        turnover_value=prepared.bar.turnover_value,
                        turnover_currency=prepared.bar.turnover_currency,
                        turnover_unit=prepared.bar.turnover_unit,
                        trade_status=prepared.bar.trade_status,
                        methodology_version_id=candidate.methodology_version_id,
                        release_id=release_id,
                        revision_no=prepared.revision_no,
                        source_batch_id=source_batch_id,
                        source_published_at=None,
                        source_time_precision="UNKNOWN",
                        public_usable_at=candidate.created_at,
                        availability_basis="OBSERVED_ONLY",
                        known_from=candidate.created_at,
                        known_to=None,
                        content_hash=prepared.content_hash,
                        quality_status="passed",
                    )
                )
                session.execute(
                    insert(NormalizedRecordManifest).values(
                        normalization_run_id=candidate.normalization_run_id,
                        record_key_hash=_record_key_hash(
                            resolved_contract_id, prepared.bar.trade_date
                        ),
                        canonical_table=DerivativeDailyBarRevision.__tablename__,
                        canonical_pk={
                            "tradeDate": prepared.bar.trade_date.isoformat(),
                            "rowId": str(row_id),
                        },
                        content_hash=prepared.content_hash,
                        disposition="accepted",
                    )
                )

        published = self._release_repository.publish_prepared(
            prepare_candidate=prepare_candidate,
            write_facts=write_facts,
        )
        if resolved_contract_id is None:
            raise AssertionError("derivative publication completed without a resolved contract")
        return PublishedDerivativeDailyBars(
            data_version=published.data_version,
            inserted_count=len(prepared_changes),
            unchanged_count=len(normalized_bars) - len(prepared_changes),
            contract=contract,
        )


def _ensure_dataset(session: Session, *, now: datetime) -> UUID:
    """幂等登记真实合约日线 dataset，只有 candidate 状态才可进入受控 publication。"""
    dataset_id = uuid5(NAMESPACE_URL, f"quant-v2:canonical-dataset:{_DATASET_CODE}:1")
    session.execute(
        pg_insert(CanonicalDataset)
        .values(
            dataset_id=dataset_id,
            code=_DATASET_CODE,
            schema_version=1,
            domain="derivative",
            grain="real derivative contract + trade date + reported methodology",
            status="candidate",
            owner_service="service-data-sync",
            created_at=now,
        )
        .on_conflict_do_nothing(index_elements=("code", "schema_version"))
    )
    return UUID(
        str(
            session.execute(
                select(CanonicalDataset.dataset_id).where(CanonicalDataset.code == _DATASET_CODE)
            ).scalar_one()
        )
    )


def _ensure_methodology(session: Session) -> UUID:
    """登记冻结的 reported 日线映射版本；来源字段变化必须新建版本而非覆盖此记录。"""
    methodology_id = uuid5(NAMESPACE_URL, f"quant-v2:methodology:{_METHODOLOGY_CODE}:1")
    session.execute(
        pg_insert(MethodologyVersion)
        .values(
            methodology_version_id=methodology_id,
            code=_METHODOLOGY_CODE,
            version=1,
            semantic_family="reported-daily-bar",
            kind="reported",
            formula_hash=hashlib.sha256(_MAPPING_VERSION.encode()).hexdigest(),
            effective_from=None,
            effective_to=None,
            status="validated",
            documentation_ref="docs/service-data-sync/0026-derivatives/index.html",
        )
        .on_conflict_do_nothing(index_elements=("code", "version"))
    )
    return UUID(
        str(
            session.execute(
                select(MethodologyVersion.methodology_version_id).where(
                    MethodologyVersion.code == _METHODOLOGY_CODE,
                    MethodologyVersion.version == 1,
                )
            ).scalar_one()
        )
    )


def _ensure_source_dataset(
    session: Session, *, approval: DerivativeSourceApproval, source: DerivativeSourceObservation
) -> UUID:
    """登记已批准的真实来源产品；adapter 名只作技术追踪，权利主体由批准表决定。"""
    source_id = uuid5(NAMESPACE_URL, f"quant-v2:data-source:{approval.source_code}")
    source_dataset_code = f"{approval.source_code}:{source.capability}"
    source_dataset_id = uuid5(NAMESPACE_URL, f"quant-v2:source-dataset:{source_dataset_code}")
    session.execute(
        pg_insert(DataSource)
        .values(
            source_id=source_id,
            code=approval.source_code,
            legal_name=approval.legal_name,
            source_kind=approval.source_kind,
            timezone="Asia/Shanghai",
            rights_status=approval.rights_status,
            rights_evidence_ref=None,
        )
        .on_conflict_do_nothing(index_elements=("code",))
    )
    session.execute(
        pg_insert(SourceDataset)
        .values(
            source_dataset_id=source_dataset_id,
            source_id=source_id,
            code=source_dataset_code,
            capability=source.capability,
            native_grain="real derivative contract + trade date",
            native_unit_json={},
            history_from=None,
            history_to=None,
            license_scope=approval.license_scope,
            active=True,
        )
        .on_conflict_do_nothing(index_elements=("source_id", "code"))
    )
    return UUID(
        str(
            session.execute(
                select(SourceDataset.source_dataset_id).where(
                    SourceDataset.source_id == source_id,
                    SourceDataset.code == source_dataset_code,
                )
            ).scalar_one()
        )
    )


def _record_source_batch(
    session: Session,
    *,
    source: DerivativeSourceObservation,
    source_dataset_id: UUID,
    now: datetime,
) -> UUID:
    """写入独立来源观察和双对象 manifest，相同字节的重复获取也不折叠。"""
    source_batch_id = record_source_observation(
        session,
        provider_id=source.provider_id,
        capability=source.capability,
        source_payload_sha256=source.raw_payload_sha256,
        raw_uri=source.raw_uri,
        observed_at=source.observed_at,
        created_at=now,
        upstream_source=source.upstream_source,
        adapter_version=source.adapter_version,
        schema_fingerprint=source.schema_fingerprint,
        source_dataset_id=source_dataset_id,
    )
    session.execute(
        insert(RawPayloadManifest).values(
            [
                {
                    "raw_payload_id": uuid4(),
                    "source_batch_id": source_batch_id,
                    "sequence_no": 1,
                    "role": "raw",
                    "object_uri": source.raw_uri,
                    "sha256": source.raw_payload_sha256,
                    "content_type": source.raw_content_type,
                    "byte_size": source.raw_byte_size,
                    "fetched_at": source.observed_at,
                },
                {
                    "raw_payload_id": uuid4(),
                    "source_batch_id": source_batch_id,
                    "sequence_no": 1,
                    "role": "normalized",
                    "object_uri": source.normalized_uri,
                    "sha256": source.normalized_payload_sha256,
                    "content_type": source.normalized_content_type,
                    "byte_size": source.normalized_byte_size,
                    "fetched_at": source.observed_at,
                },
            ]
        )
    )
    return source_batch_id


def _ensure_contract_id(
    session: Session,
    *,
    contract: DerivativeContractIdentifier,
    fact_start: date,
    fact_end: date,
    source_batch_id: UUID,
    now: datetime,
) -> UUID:
    """解析真实合约；首个可信日线可建立不虚构产品、挂牌日或到期日的最小身份。"""
    rows = (
        session.execute(
            select(DerivativeContract.instrument_id)
            .join(
                InstrumentIdentifierVersion,
                InstrumentIdentifierVersion.entity_id == DerivativeContract.instrument_id,
            )
            .join(TradingVenue, TradingVenue.venue_id == InstrumentIdentifierVersion.venue_id)
            .where(
                TradingVenue.code == contract.venue,
                InstrumentIdentifierVersion.entity_kind.in_(("FUTURE", "OPTION")),
                InstrumentIdentifierVersion.identifier_scheme == _IDENTIFIER_SCHEME,
                InstrumentIdentifierVersion.identifier_value == contract.contract_code,
                InstrumentIdentifierVersion.effective_from <= fact_start,
                (InstrumentIdentifierVersion.effective_to.is_(None))
                | (InstrumentIdentifierVersion.effective_to > fact_end),
                InstrumentIdentifierVersion.known_to.is_(None),
            )
        )
        .scalars()
        .all()
    )
    candidates = {UUID(str(value)) for value in rows}
    if len(candidates) == 1:
        return candidates.pop()
    if candidates:
        raise ValueError("derivative contract identity is missing or ambiguous")
    historical_rows = (
        session.execute(
            select(DerivativeContract.instrument_id)
            .join(
                InstrumentIdentifierVersion,
                InstrumentIdentifierVersion.entity_id == DerivativeContract.instrument_id,
            )
            .join(TradingVenue, TradingVenue.venue_id == InstrumentIdentifierVersion.venue_id)
            .where(
                TradingVenue.code == contract.venue,
                InstrumentIdentifierVersion.entity_kind == "FUTURE",
                InstrumentIdentifierVersion.identifier_scheme == _IDENTIFIER_SCHEME,
                InstrumentIdentifierVersion.identifier_value == contract.contract_code,
                InstrumentIdentifierVersion.known_to.is_(None),
            )
        )
        .scalars()
        .all()
    )
    historical_candidates = {UUID(str(value)) for value in historical_rows}
    if len(historical_candidates) == 1:
        # 已确认同一现行代码但其首个同步窗口更晚时，不能重复造身份或改写既有代码时间线。
        return historical_candidates.pop()
    if historical_candidates:
        raise ValueError("derivative contract identity is ambiguous")
    venue_id = _ensure_derivative_venue(session, venue=contract.venue)
    contract_id = uuid4()
    session.execute(
        insert(MarketEntity).values(
            entity_id=contract_id,
            entity_kind="FUTURE",
            created_at=now,
            retired_at=None,
        )
    )
    session.execute(
        insert(MarketInstrument).values(
            instrument_id=contract_id,
            instrument_kind="FUTURE",
            primary_venue_id=venue_id,
            tradable_from=None,
            tradable_to=None,
        )
    )
    session.execute(
        insert(DerivativeContract).values(
            instrument_id=contract_id,
            product_entity_id=None,
            expiry_date=None,
            call_put=None,
            strike_price=None,
            underlying_entity_id=None,
            listed_date=None,
        )
    )
    session.execute(
        insert(InstrumentIdentifierVersion).values(
            version_id=uuid4(),
            entity_id=contract_id,
            entity_kind="FUTURE",
            venue_id=venue_id,
            identifier_scheme=_IDENTIFIER_SCHEME,
            identifier_value=contract.contract_code,
            effective_from=fact_start,
            effective_to=None,
            known_from=now,
            known_to=None,
            source_time_precision="DATE_ONLY",
            source_batch_id=source_batch_id,
        )
    )
    return contract_id


def _ensure_derivative_venue(session: Session, *, venue: str) -> UUID:
    """确保已知境内期货场所可供首笔真实合约引用，未知场所仍须先有目录证据。"""
    rows = (
        session.execute(select(TradingVenue.venue_id).where(TradingVenue.code == venue))
        .scalars()
        .all()
    )
    candidates = {UUID(str(value)) for value in rows}
    if len(candidates) == 1:
        return candidates.pop()
    if candidates:
        raise ValueError("derivative trading venue identity is ambiguous")
    name = _DOMESTIC_DERIVATIVE_VENUES.get(venue)
    if name is None:
        raise ValueError("derivative trading venue requires catalog evidence")
    venue_id = uuid4()
    session.execute(
        insert(TradingVenue).values(
            venue_id=venue_id,
            mic=None,
            code=venue,
            name=name,
            timezone="Asia/Shanghai",
            country="CN",
            active=True,
        )
    )
    return venue_id


def _record_normalization_run(
    session: Session,
    *,
    dataset_id: UUID,
    partition_key: str,
    source: DerivativeSourceObservation,
    source_batch_id: UUID,
    now: datetime,
) -> UUID:
    """建立一次确定性标准化运行，输入摘要同时绑定 raw 和标准 JSON 证据。"""
    run_id = UUID(
        str(
            session.execute(
                select(SourceBatch.run_id).where(SourceBatch.source_batch_id == source_batch_id)
            ).scalar_one()
        )
    )
    normalization_run_id = uuid4()
    inserted = session.execute(
        pg_insert(NormalizationRun)
        .values(
            normalization_run_id=normalization_run_id,
            dataset_id=dataset_id,
            partition_key=partition_key,
            run_id=run_id,
            adapter_version=source.adapter_version,
            schema_fingerprint=source.schema_fingerprint,
            mapping_version=_MAPPING_VERSION,
            input_set_hash=hashlib.sha256(
                f"{source.raw_payload_sha256}:{source.normalized_payload_sha256}".encode()
            ).hexdigest(),
            status="passed",
            started_at=now,
            finished_at=now,
        )
        .on_conflict_do_nothing(
            index_elements=("dataset_id", "partition_key", "input_set_hash", "mapping_version")
        )
        .returning(NormalizationRun.normalization_run_id)
    ).scalar_one_or_none()
    if inserted is not None:
        return UUID(str(inserted))
    return UUID(
        str(
            session.execute(
                select(NormalizationRun.normalization_run_id).where(
                    NormalizationRun.dataset_id == dataset_id,
                    NormalizationRun.partition_key == partition_key,
                    NormalizationRun.input_set_hash
                    == hashlib.sha256(
                        f"{source.raw_payload_sha256}:{source.normalized_payload_sha256}".encode()
                    ).hexdigest(),
                    NormalizationRun.mapping_version == _MAPPING_VERSION,
                )
            ).scalar_one()
        )
    )


def _current_bars(
    session: Session, *, contract_id: UUID, methodology_version_id: UUID
) -> dict[date, DerivativeDailyBarRevision]:
    """读取当前知识区间的全合约快照；同日多行代表数据损坏，拒绝生成不确定 release。"""
    rows = (
        session.execute(
            select(DerivativeDailyBarRevision).where(
                DerivativeDailyBarRevision.contract_id == contract_id,
                DerivativeDailyBarRevision.methodology_version_id == methodology_version_id,
                DerivativeDailyBarRevision.known_to.is_(None),
            )
        )
        .scalars()
        .all()
    )
    result = {row.trade_date: row for row in rows}
    if len(result) != len(rows):
        raise ValueError("derivative daily bar current revision is ambiguous")
    return result


def _lineage_records(
    *,
    current: Mapping[date, DerivativeDailyBarRevision],
    incoming: Mapping[date, DerivativeDailyBar],
    changed: Mapping[date, _PreparedBar],
    contract_id: UUID,
    source_batch_id: UUID,
) -> tuple[CanonicalLineageRecord, ...]:
    """从旧当前快照与本次变化生成完整分区血缘，内容不变行继续引用原始证据。"""
    records: list[CanonicalLineageRecord] = []
    for trade_date in sorted({*current, *incoming}):
        changed_bar = changed.get(trade_date)
        if changed_bar is not None:
            content_hash = changed_bar.content_hash
            batch_id = source_batch_id
        else:
            existing = current[trade_date]
            content_hash = existing.content_hash
            batch_id = UUID(str(existing.source_batch_id))
        records.append(
            CanonicalLineageRecord(
                record_key_hash=_record_key_hash(contract_id, trade_date),
                content_hash=content_hash,
                source_batch_id=batch_id,
                transform_hash=hashlib.sha256(_MAPPING_VERSION.encode()).hexdigest(),
            )
        )
    return tuple(records)


def _record_key_hash(contract_id: UUID | None, trade_date: date) -> str:
    """计算逻辑事实键摘要；调用方必须提供已解析的真实合约 UUID。"""
    if contract_id is None:
        raise AssertionError("derivative record key requires a resolved contract identity")
    return hashlib.sha256(f"{contract_id}:{trade_date.isoformat()}".encode()).hexdigest()


def _bar_content_hash(bar: DerivativeDailyBar) -> str:
    """以规范化领域值生成业务内容摘要，避免上游字段顺序或浮点格式影响 revision。"""
    payload = {
        "tradeDate": bar.trade_date.isoformat(),
        "open": str(bar.open_price),
        "high": str(bar.high_price),
        "low": str(bar.low_price),
        "close": str(bar.close_price),
        "preClose": _decimal_text(bar.pre_close_price),
        "settlement": _decimal_text(bar.settlement_price),
        "preSettlement": _decimal_text(bar.pre_settlement_price),
        "volume": str(bar.volume_value),
        "openInterest": str(bar.open_interest_value),
        "turnover": _decimal_text(bar.turnover_value),
        "turnoverCurrency": bar.turnover_currency,
        "turnoverUnit": bar.turnover_unit,
        "tradeStatus": bar.trade_status,
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _decimal_text(value: Decimal | None) -> str | None:
    """把可选精确小数稳定投影为文本，真实空值不转换为零。"""
    return None if value is None else str(value)
