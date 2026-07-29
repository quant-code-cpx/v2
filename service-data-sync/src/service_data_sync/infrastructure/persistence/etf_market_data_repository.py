"""`ETF P0` 日线与 `NAV` 的原子 `canonical` 发布仓储。

日线与单位、累计净值是不同事实类型，按已治理的上市工具、交易日和净值类型分别建键。
仓储只接受获批来源和可解析 `ETF` 身份；同日重复、来源不明或分区不完整都会阻止发布，
而不是选择其中一行。内容不变的重试复用现有版本与原始血缘。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any
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
from service_data_sync.application.ports.etf_market import (
    EtfDailyBarRepository,
    EtfNavRepository,
    EtfSourceObservation,
    PublishedEtfDailyBars,
    PublishedEtfNavs,
)
from service_data_sync.domain.etf import EtfDailyBar, EtfIdentifier, EtfNav
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
from service_data_sync.infrastructure.database.models.etf import EtfDailyBarRevision, EtfNavRevision
from service_data_sync.infrastructure.database.models.market.identity import (
    EtfListing,
    InstrumentIdentifierVersion,
    TradingVenue,
)
from service_data_sync.infrastructure.database.models.provenance.source_batch import SourceBatch
from service_data_sync.infrastructure.persistence.canonical_release_repository import (
    SqlAlchemyCanonicalReleaseRepository,
)
from service_data_sync.infrastructure.persistence.source_batch import record_source_observation

_IDENTIFIER_SCHEME = "venue_symbol"
_BAR_DATASET = "fund.etf.bar.1d.reported"
_NAV_DATASET = "fund.etf.nav.1d.reported"
_BAR_METHODOLOGY = "etf-unadjusted-daily-bar"
_NAV_METHODOLOGY = "etf-reported-daily-nav"


@dataclass(frozen=True, slots=True)
class EtfSourceApproval:
    """记录已通过权利、留存和内部使用审查的 ETF adapter 来源。"""

    provider_id: str
    source_code: str
    legal_name: str
    source_kind: str
    rights_status: str
    license_scope: str

    def __post_init__(self) -> None:
        """拒绝不完整批准项，避免仅因技术 adapter 已注册就推进 P0 发布。"""
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
            raise ValueError("ETF source approval is incomplete")


@dataclass(frozen=True, slots=True)
class _PreparedBar:
    """封装待写 ETF 日线 revision 的领域值、内容摘要和序号。"""

    value: EtfDailyBar
    content_hash: str
    revision_no: int


@dataclass(frozen=True, slots=True)
class _PreparedNav:
    """封装待写 ETF NAV revision 的领域值、内容摘要和序号。"""

    value: EtfNav
    content_hash: str
    revision_no: int


class SqlAlchemyEtfMarketDataRepository(EtfDailyBarRepository, EtfNavRepository):
    """只向已批准来源和已登记 ETF 上市工具发布 P0 日线/NAV。"""

    def __init__(
        self,
        database: DatabaseClient,
        *,
        approved_sources: Mapping[str, EtfSourceApproval] | None = None,
    ) -> None:
        """保存事务工厂和显式批准来源表；默认空表使所有生产发布 fail-closed。"""
        self._database = database
        self._approved_sources = dict(approved_sources or {})
        self._release_repository = SqlAlchemyCanonicalReleaseRepository(database)

    def publish_daily_bars(
        self,
        *,
        etf: EtfIdentifier,
        bars: Sequence[EtfDailyBar],
        source: EtfSourceObservation,
    ) -> PublishedEtfDailyBars:
        """以一个 ETF 全分区当前快照发布未复权日线，内容不变重放复用 immutable release。"""
        values = tuple(bars)
        _assert_unique_dates(values, label="ETF daily bars")
        approval = _approved_source(self._approved_sources, source=source)
        prepared: list[_PreparedBar] = []
        etf_id: UUID | None = None
        source_batch_id: UUID | None = None

        def prepare(session: Session) -> CanonicalReleaseCandidate:
            """在同一事务中固化来源、身份、现有快照及 release 候选。"""
            nonlocal etf_id, source_batch_id
            now = datetime.now(UTC)
            dataset_id = _ensure_dataset(session, code=_BAR_DATASET, domain="etf", now=now)
            methodology_id = _ensure_methodology(
                session,
                code=_BAR_METHODOLOGY,
                semantic_family="unadjusted-daily-bar",
                mapping_version="etf-daily-bar-v1",
                documentation_ref="docs/service-data-sync/0020-etf-market-data/index.html",
            )
            source_dataset_id = _ensure_source_dataset(
                session,
                approval=approval,
                capability=source.capability,
                native_grain="ETF listing + trade date",
            )
            source_batch_id = _record_source_batch(
                session, source=source, source_dataset_id=source_dataset_id, now=now
            )
            etf_id = _resolve_etf_id(
                session, etf=etf, fact_date=min(item.trade_date for item in values)
            )
            partition_key = f"etf:{etf_id}"
            run_id = _record_normalization_run(
                session,
                dataset_id=dataset_id,
                partition_key=partition_key,
                source=source,
                source_batch_id=source_batch_id,
                mapping_version="etf-daily-bar-v1",
                now=now,
            )
            current = _current_bars(session, etf_id=etf_id, methodology_id=methodology_id)
            incoming = {item.trade_date: item for item in values}
            prepared[:] = [
                _PreparedBar(
                    value=item,
                    content_hash=_bar_hash(item),
                    revision_no=current[item.trade_date].revision_no + 1
                    if item.trade_date in current
                    else 1,
                )
                for item in values
                if item.trade_date not in current
                or _bar_hash(item) != current[item.trade_date].content_hash
            ]
            return _candidate(
                session,
                dataset_id=dataset_id,
                dataset_code=_BAR_DATASET,
                partition_key=partition_key,
                methodology_id=methodology_id,
                normalization_run_id=run_id,
                current=current,
                incoming=incoming,
                changed={item.value.trade_date: item for item in prepared},
                entity_id=etf_id,
                source_batch_id=source_batch_id,
                mapping_version="etf-daily-bar-v1",
                now=now,
            )

        def write(session: Session, candidate: CanonicalReleaseCandidate, release_id: UUID) -> None:
            """关闭变化日期的旧知识版本并以新 release UUID 写入强类型 ETF 日线。"""
            if etf_id is None or source_batch_id is None:
                raise AssertionError("ETF daily-bar preparation did not resolve state")
            for item in prepared:
                session.execute(
                    update(EtfDailyBarRevision)
                    .where(
                        EtfDailyBarRevision.etf_id == etf_id,
                        EtfDailyBarRevision.trade_date == item.value.trade_date,
                        EtfDailyBarRevision.methodology_version_id
                        == candidate.methodology_version_id,
                        EtfDailyBarRevision.known_to.is_(None),
                    )
                    .values(known_to=candidate.created_at)
                )
                row_id = uuid4()
                session.execute(
                    insert(EtfDailyBarRevision).values(
                        trade_date=item.value.trade_date,
                        row_id=row_id,
                        etf_id=etf_id,
                        methodology_version_id=candidate.methodology_version_id,
                        release_id=release_id,
                        revision_no=item.revision_no,
                        open_price=item.value.open_price,
                        high_price=item.value.high_price,
                        low_price=item.value.low_price,
                        close_price=item.value.close_price,
                        volume_value=item.value.volume_value,
                        volume_unit=item.value.volume_unit,
                        amount_value=item.value.amount_value,
                        currency=item.value.currency,
                        trade_status=item.value.trade_status,
                        source_published_at=None,
                        public_usable_at=candidate.created_at,
                        availability_basis="OBSERVED_ONLY",
                        known_from=candidate.created_at,
                        known_to=None,
                        source_batch_id=source_batch_id,
                        content_hash=item.content_hash,
                        quality_status="passed",
                    )
                )
                _record_manifest(
                    session,
                    normalization_run_id=candidate.normalization_run_id,
                    record_key_hash=_record_key(etf_id, item.value.trade_date),
                    canonical_table=EtfDailyBarRevision.__tablename__,
                    canonical_pk={
                        "tradeDate": item.value.trade_date.isoformat(),
                        "rowId": str(row_id),
                    },
                    content_hash=item.content_hash,
                )

        publication = self._release_repository.publish_prepared(
            prepare_candidate=prepare, write_facts=write
        )
        return PublishedEtfDailyBars(
            data_version=publication.data_version,
            inserted_count=len(prepared),
            unchanged_count=len(values) - len(prepared),
            etf=etf,
        )

    def publish_navs(
        self,
        *,
        etf: EtfIdentifier,
        navs: Sequence[EtfNav],
        source: EtfSourceObservation,
    ) -> PublishedEtfNavs:
        """以一个 ETF 全分区当前快照发布单位/累计 NAV，类型不同永远不互相覆盖。"""
        values = tuple(navs)
        if not values or len({(item.nav_date, item.nav_kind) for item in values}) != len(values):
            raise ValueError("ETF NAVs must be non-empty and unique by date and kind")
        approval = _approved_source(self._approved_sources, source=source)
        prepared: list[_PreparedNav] = []
        etf_id: UUID | None = None
        source_batch_id: UUID | None = None

        def prepare(session: Session) -> CanonicalReleaseCandidate:
            """在同一事务中固化 NAV 来源、身份、当前快照和 immutable release 候选。"""
            nonlocal etf_id, source_batch_id
            now = datetime.now(UTC)
            dataset_id = _ensure_dataset(session, code=_NAV_DATASET, domain="etf", now=now)
            methodology_id = _ensure_methodology(
                session,
                code=_NAV_METHODOLOGY,
                semantic_family="reported-daily-nav",
                mapping_version="etf-nav-v1",
                documentation_ref="docs/service-data-sync/0020-etf-market-data/index.html",
            )
            source_dataset_id = _ensure_source_dataset(
                session,
                approval=approval,
                capability=source.capability,
                native_grain="ETF listing + NAV date + NAV kind",
            )
            source_batch_id = _record_source_batch(
                session, source=source, source_dataset_id=source_dataset_id, now=now
            )
            etf_id = _resolve_etf_id(
                session, etf=etf, fact_date=min(item.nav_date for item in values)
            )
            partition_key = f"etf:{etf_id}"
            run_id = _record_normalization_run(
                session,
                dataset_id=dataset_id,
                partition_key=partition_key,
                source=source,
                source_batch_id=source_batch_id,
                mapping_version="etf-nav-v1",
                now=now,
            )
            current = _current_navs(session, etf_id=etf_id, methodology_id=methodology_id)
            incoming = {(item.nav_date, item.nav_kind): item for item in values}
            prepared[:] = [
                _PreparedNav(
                    value=item,
                    content_hash=_nav_hash(item),
                    revision_no=current[(item.nav_date, item.nav_kind)].revision_no + 1
                    if (item.nav_date, item.nav_kind) in current
                    else 1,
                )
                for item in values
                if (item.nav_date, item.nav_kind) not in current
                or _nav_hash(item) != current[(item.nav_date, item.nav_kind)].content_hash
            ]
            return _candidate(
                session,
                dataset_id=dataset_id,
                dataset_code=_NAV_DATASET,
                partition_key=partition_key,
                methodology_id=methodology_id,
                normalization_run_id=run_id,
                current=current,
                incoming=incoming,
                changed={(item.value.nav_date, item.value.nav_kind): item for item in prepared},
                entity_id=etf_id,
                source_batch_id=source_batch_id,
                mapping_version="etf-nav-v1",
                now=now,
            )

        def write(session: Session, candidate: CanonicalReleaseCandidate, release_id: UUID) -> None:
            """关闭变化的日期/类型版本，插入新的 NAV revision 与标准记录 manifest。"""
            if etf_id is None or source_batch_id is None:
                raise AssertionError("ETF NAV preparation did not resolve state")
            for item in prepared:
                session.execute(
                    update(EtfNavRevision)
                    .where(
                        EtfNavRevision.etf_id == etf_id,
                        EtfNavRevision.nav_date == item.value.nav_date,
                        EtfNavRevision.nav_kind == item.value.nav_kind,
                        EtfNavRevision.methodology_version_id == candidate.methodology_version_id,
                        EtfNavRevision.known_to.is_(None),
                    )
                    .values(known_to=candidate.created_at)
                )
                row_id = uuid4()
                session.execute(
                    insert(EtfNavRevision).values(
                        nav_date=item.value.nav_date,
                        row_id=row_id,
                        etf_id=etf_id,
                        nav_kind=item.value.nav_kind,
                        nav_value=item.value.nav_value,
                        currency=item.value.currency,
                        finality=item.value.finality,
                        null_reason=None,
                        methodology_version_id=candidate.methodology_version_id,
                        release_id=release_id,
                        revision_no=item.revision_no,
                        source_published_at=None,
                        public_usable_at=candidate.created_at,
                        availability_basis="OBSERVED_ONLY",
                        known_from=candidate.created_at,
                        known_to=None,
                        source_batch_id=source_batch_id,
                        content_hash=item.content_hash,
                        quality_status="passed",
                    )
                )
                _record_manifest(
                    session,
                    normalization_run_id=candidate.normalization_run_id,
                    record_key_hash=_record_key(etf_id, item.value.nav_date, item.value.nav_kind),
                    canonical_table=EtfNavRevision.__tablename__,
                    canonical_pk={"navDate": item.value.nav_date.isoformat(), "rowId": str(row_id)},
                    content_hash=item.content_hash,
                )

        publication = self._release_repository.publish_prepared(
            prepare_candidate=prepare, write_facts=write
        )
        return PublishedEtfNavs(
            data_version=publication.data_version,
            inserted_count=len(prepared),
            unchanged_count=len(values) - len(prepared),
            etf=etf,
        )


def _assert_unique_dates(values: Sequence[EtfDailyBar], *, label: str) -> None:
    """校验一个 ETF 日线窗口非空且每个交易日只有一行，防止 release 快照不确定。"""
    if not values or len({item.trade_date for item in values}) != len(values):
        raise ValueError(f"{label} must be non-empty and unique by trade date")


def _approved_source(
    approvals: Mapping[str, EtfSourceApproval], *, source: EtfSourceObservation
) -> EtfSourceApproval:
    """按 provider-neutral adapter 身份读取批准项，未获准来源一律不能发布。"""
    approval = approvals.get(source.provider_id)
    if approval is None:
        raise ValueError("ETF source provider is not approved for publication")
    return approval


def _ensure_dataset(session: Session, *, code: str, domain: str, now: datetime) -> UUID:
    """幂等登记 ETF dataset，只有 candidate/production 状态可进入受控 release 流程。"""
    dataset_id = uuid5(NAMESPACE_URL, f"quant-v2:canonical-dataset:{code}:1")
    session.execute(
        pg_insert(CanonicalDataset)
        .values(
            dataset_id=dataset_id,
            code=code,
            schema_version=1,
            domain=domain,
            grain="ETF listing + dated reported fact",
            status="candidate",
            owner_service="service-data-sync",
            created_at=now,
        )
        .on_conflict_do_nothing(index_elements=("code", "schema_version"))
    )
    return UUID(
        str(
            session.execute(
                select(CanonicalDataset.dataset_id).where(CanonicalDataset.code == code)
            ).scalar_one()
        )
    )


def _ensure_methodology(
    session: Session,
    *,
    code: str,
    semantic_family: str,
    mapping_version: str,
    documentation_ref: str,
) -> UUID:
    """登记冻结的 ETF 映射方法学，字段或来源语义变化必须创建新版本。"""
    methodology_id = uuid5(NAMESPACE_URL, f"quant-v2:methodology:{code}:1")
    session.execute(
        pg_insert(MethodologyVersion)
        .values(
            methodology_version_id=methodology_id,
            code=code,
            version=1,
            semantic_family=semantic_family,
            kind="reported",
            formula_hash=hashlib.sha256(mapping_version.encode()).hexdigest(),
            effective_from=None,
            effective_to=None,
            status="validated",
            documentation_ref=documentation_ref,
        )
        .on_conflict_do_nothing(index_elements=("code", "version"))
    )
    return UUID(
        str(
            session.execute(
                select(MethodologyVersion.methodology_version_id).where(
                    MethodologyVersion.code == code, MethodologyVersion.version == 1
                )
            ).scalar_one()
        )
    )


def _ensure_source_dataset(
    session: Session, *, approval: EtfSourceApproval, capability: str, native_grain: str
) -> UUID:
    """登记批准的业务来源及其 capability，技术 adapter 名不作为权利主体。"""
    source_id = uuid5(NAMESPACE_URL, f"quant-v2:data-source:{approval.source_code}")
    code = f"{approval.source_code}:{capability}"
    source_dataset_id = uuid5(NAMESPACE_URL, f"quant-v2:source-dataset:{code}")
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
            code=code,
            capability=capability,
            native_grain=native_grain,
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
                    SourceDataset.source_id == source_id, SourceDataset.code == code
                )
            ).scalar_one()
        )
    )


def _record_source_batch(
    session: Session, *, source: EtfSourceObservation, source_dataset_id: UUID, now: datetime
) -> UUID:
    """写入独立来源观察及 raw/normalized 双 manifest，重复获取仍保留单独证据批次。"""
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


def _resolve_etf_id(session: Session, *, etf: EtfIdentifier, fact_date: date) -> UUID:
    """按场所、代码和日期解析预先治理的 ETF 上市工具；缺失或复用冲突一律拒绝发布。"""
    rows = (
        session.execute(
            select(EtfListing.instrument_id)
            .join(
                InstrumentIdentifierVersion,
                InstrumentIdentifierVersion.entity_id == EtfListing.instrument_id,
            )
            .join(TradingVenue, TradingVenue.venue_id == InstrumentIdentifierVersion.venue_id)
            .where(
                TradingVenue.code == etf.venue,
                InstrumentIdentifierVersion.entity_kind == "ETF_LISTING",
                InstrumentIdentifierVersion.identifier_scheme == _IDENTIFIER_SCHEME,
                InstrumentIdentifierVersion.identifier_value == etf.symbol,
                InstrumentIdentifierVersion.effective_from <= fact_date,
                (InstrumentIdentifierVersion.effective_to.is_(None))
                | (InstrumentIdentifierVersion.effective_to > fact_date),
                InstrumentIdentifierVersion.known_to.is_(None),
            )
        )
        .scalars()
        .all()
    )
    candidates = {UUID(str(value)) for value in rows}
    if len(candidates) != 1:
        raise ValueError("ETF listing identity is missing or ambiguous")
    return candidates.pop()


def _record_normalization_run(
    session: Session,
    *,
    dataset_id: UUID,
    partition_key: str,
    source: EtfSourceObservation,
    source_batch_id: UUID,
    mapping_version: str,
    now: datetime,
) -> UUID:
    """建立或复用确定性标准化运行，输入摘要同时绑定 raw 与 normalized 对象。"""
    run_id = UUID(
        str(
            session.execute(
                select(SourceBatch.run_id).where(SourceBatch.source_batch_id == source_batch_id)
            ).scalar_one()
        )
    )
    input_set_hash = hashlib.sha256(
        f"{source.raw_payload_sha256}:{source.normalized_payload_sha256}".encode()
    ).hexdigest()
    inserted = session.execute(
        pg_insert(NormalizationRun)
        .values(
            normalization_run_id=uuid4(),
            dataset_id=dataset_id,
            partition_key=partition_key,
            run_id=run_id,
            adapter_version=source.adapter_version,
            schema_fingerprint=source.schema_fingerprint,
            mapping_version=mapping_version,
            input_set_hash=input_set_hash,
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
                    NormalizationRun.input_set_hash == input_set_hash,
                    NormalizationRun.mapping_version == mapping_version,
                )
            ).scalar_one()
        )
    )


def _current_bars(
    session: Session, *, etf_id: UUID, methodology_id: UUID
) -> dict[date, EtfDailyBarRevision]:
    """读取 ETF 当前知识区间日线，任何同日重复都使新 release 失败而非任选一行。"""
    rows = (
        session.execute(
            select(EtfDailyBarRevision).where(
                EtfDailyBarRevision.etf_id == etf_id,
                EtfDailyBarRevision.methodology_version_id == methodology_id,
                EtfDailyBarRevision.known_to.is_(None),
            )
        )
        .scalars()
        .all()
    )
    result = {item.trade_date: item for item in rows}
    if len(result) != len(rows):
        raise ValueError("ETF daily-bar current revision is ambiguous")
    return result


def _current_navs(
    session: Session, *, etf_id: UUID, methodology_id: UUID
) -> dict[tuple[date, str], EtfNavRevision]:
    """读取 ETF 当前 NAV 快照，日期和类型重复代表数据损坏并拒绝继续发布。"""
    rows = (
        session.execute(
            select(EtfNavRevision).where(
                EtfNavRevision.etf_id == etf_id,
                EtfNavRevision.methodology_version_id == methodology_id,
                EtfNavRevision.known_to.is_(None),
            )
        )
        .scalars()
        .all()
    )
    result = {(item.nav_date, item.nav_kind): item for item in rows}
    if len(result) != len(rows):
        raise ValueError("ETF NAV current revision is ambiguous")
    return result


def _candidate(
    session: Session,
    *,
    dataset_id: UUID,
    dataset_code: str,
    partition_key: str,
    methodology_id: UUID,
    normalization_run_id: UUID,
    current: Mapping[Any, Any],
    incoming: Mapping[Any, EtfDailyBar | EtfNav],
    changed: Mapping[Any, _PreparedBar | _PreparedNav],
    entity_id: UUID,
    source_batch_id: UUID,
    mapping_version: str,
    now: datetime,
) -> CanonicalReleaseCandidate:
    """由旧当前快照与变更生成完整 release 血缘，未变化事实继续引用其原始来源批次。"""
    records: list[CanonicalLineageRecord] = []
    for key in sorted({*current, *incoming}, key=str):
        item = changed.get(key)
        if item is None:
            existing = current[key]
            content_hash = existing.content_hash
            batch_id = UUID(str(existing.source_batch_id))
        else:
            content_hash = item.content_hash
            batch_id = source_batch_id
        fact_date = _fact_date(incoming.get(key, current.get(key)))
        record_kind = key[1] if isinstance(key, tuple) else None
        records.append(
            CanonicalLineageRecord(
                record_key_hash=_record_key(entity_id, fact_date, record_kind),
                content_hash=content_hash,
                source_batch_id=batch_id,
                transform_hash=hashlib.sha256(mapping_version.encode()).hexdigest(),
            )
        )
    dates = tuple(_fact_date(value) for value in (*current.values(), *incoming.values()))
    checkpoint = session.execute(
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
        methodology_version_id=methodology_id,
        normalization_run_id=normalization_run_id,
        records=tuple(records),
        quality=CanonicalQualityDecision(
            status="passed",
            policy_code=f"{dataset_code}.quality",
            policy_version=1,
            rules=(CanonicalQualityRule("typed-p0-facts", "blocking", True),),
        ),
        fact_min=min(dates),
        fact_max=max(dates),
        checkpoint_kind="published",
        checkpoint_position={"date": max(dates).isoformat()},
        expected_fencing_token=0 if checkpoint is None else int(checkpoint),
        created_at=now,
    )


def _fact_date(value: object) -> date:
    """读取 ETF 日线或 NAV 的事实日期，禁止错误 revision 类型静默进入 release。"""
    if isinstance(value, (EtfDailyBar, EtfDailyBarRevision)):
        return value.trade_date
    if isinstance(value, (EtfNav, EtfNavRevision)):
        return value.nav_date
    raise TypeError("ETF release fact has an unsupported type")


def _record_manifest(
    session: Session,
    *,
    normalization_run_id: UUID,
    record_key_hash: str,
    canonical_table: str,
    canonical_pk: dict[str, str],
    content_hash: str,
) -> None:
    """记录每个新强类型 revision 的标准化 manifest，供 raw replay 与审计关联。"""
    session.execute(
        insert(NormalizedRecordManifest).values(
            normalization_run_id=normalization_run_id,
            record_key_hash=record_key_hash,
            canonical_table=canonical_table,
            canonical_pk=canonical_pk,
            content_hash=content_hash,
            disposition="accepted",
        )
    )


def _record_key(etf_id: UUID, fact_date: date, fact_kind: str | None = None) -> str:
    """计算 ETF 逻辑事实键摘要，NAV 类型参与键而日线只有日期参与。"""
    return hashlib.sha256(
        f"{etf_id}:{fact_date.isoformat()}:{fact_kind or ''}".encode()
    ).hexdigest()


def _bar_hash(value: EtfDailyBar) -> str:
    """以规范化领域值生成日线内容摘要，不受 Provider 字段顺序或数字文本样式影响。"""
    return _hash(
        {
            "tradeDate": value.trade_date.isoformat(),
            "open": str(value.open_price),
            "high": str(value.high_price),
            "low": str(value.low_price),
            "close": str(value.close_price),
            "volume": str(value.volume_value),
            "volumeUnit": value.volume_unit,
            "amount": str(value.amount_value),
            "currency": value.currency,
            "tradeStatus": value.trade_status,
        }
    )


def _nav_hash(value: EtfNav) -> str:
    """以日期、类型、净值、币种和终态生成稳定摘要，累计与单位 NAV 不会碰撞。"""
    return _hash(
        {
            "navDate": value.nav_date.isoformat(),
            "navKind": value.nav_kind,
            "nav": str(value.nav_value),
            "currency": value.currency,
            "finality": value.finality,
        }
    )


def _hash(value: dict[str, str | None]) -> str:
    """按稳定 JSON 编码计算 SHA-256，空值仍参与内容身份。"""
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
