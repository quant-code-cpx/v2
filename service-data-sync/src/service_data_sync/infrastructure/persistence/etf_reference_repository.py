"""`ETF` 产品资料与日级状态的原子 `canonical` 发布仓储。

目录首次见到已批准的 `ETF` 时可建立最小上市身份链；已有身份的代码歧义则拒绝写入。
产品资料与申购、赎回、交易状态有各自时间轴，任一状态修订不能覆盖其他维度。有限
目录快照也不会把本次缺席的既有上市工具解释为退市或删除。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import insert, select, update
from sqlalchemy.orm import Session

from service_data_sync.application.ports.canonical_release import (
    CanonicalLineageRecord,
    CanonicalQualityDecision,
    CanonicalQualityRule,
    CanonicalReleaseCandidate,
)
from service_data_sync.application.ports.etf_market import (
    EtfReferenceRepository,
    EtfSourceObservation,
    PublishedEtfReference,
)
from service_data_sync.domain.etf import EtfDailyStatus, EtfIdentifier, EtfProfile
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.database.models.canonical import CanonicalCheckpoint
from service_data_sync.infrastructure.database.models.etf import (
    EtfProfileVersion,
    EtfStatusRevision,
)
from service_data_sync.infrastructure.database.models.market.identity import (
    EtfListing,
    FundLegalEntity,
    FundShareClass,
    InstrumentIdentifierVersion,
    MarketEntity,
    MarketInstrument,
    TradingVenue,
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

_IDENTIFIER_SCHEME = "venue_symbol"
_PROFILE_DATASET = "fund.etf.profile.reported"
_STATUS_DATASET = "fund.etf.trading_state.reported"
_PROFILE_METHODOLOGY = "etf-profile-reported"
_STATUS_METHODOLOGY = "etf-trading-state-reported"
_PROFILE_MAPPING = "etf-profile-v2"
_STATUS_MAPPING = "etf-trading-state-v1"


@dataclass(frozen=True, slots=True)
class EtfReferenceSourceApproval(TypedP0SourceApproval):
    """标识已通过产品资料或状态事实生产发布审查的 ETF 来源。"""


@dataclass(frozen=True, slots=True)
class _PreparedProfile:
    """封装待写 ETF 产品资料版本、永久上市工具、摘要和修订序号。"""

    value: EtfProfile
    etf_id: UUID
    content_hash: str
    effective_to: date | None
    superseded: EtfProfileVersion | None
    clone_prior_segment: bool


@dataclass(frozen=True, slots=True)
class _PreparedStatus:
    """封装待写 ETF 状态版本、永久上市工具、摘要和修订序号。"""

    value: EtfDailyStatus
    etf_id: UUID
    content_hash: str
    revision_no: int


class SqlAlchemyEtfReferenceRepository(EtfReferenceRepository):
    """发布已批准 ETF 资料与状态，并由目录原子建立首次出现的身份链。"""

    def __init__(
        self,
        database: DatabaseClient,
        *,
        approved_sources: Mapping[str, EtfReferenceSourceApproval] | None = None,
    ) -> None:
        """保存事务工厂与显式来源批准表；默认拒绝任何生产发布。"""
        self._database = database
        self._approved_sources = dict(approved_sources or {})
        self._release_repository = SqlAlchemyCanonicalReleaseRepository(database)

    def publish_profiles(
        self,
        *,
        profiles: Sequence[EtfProfile],
        source: EtfSourceObservation,
    ) -> PublishedEtfReference:
        """发布一个交易所目录内可解析 ETF 的资料版本，目录差集不会关闭任何既有身份。"""
        values = tuple(profiles)
        if not values or len({value.etf.qualified_key for value in values}) != len(values):
            raise ValueError("ETF profiles must be non-empty and unique by qualified identifier")
        venues = {value.etf.venue for value in values}
        if len(venues) != 1:
            raise ValueError("ETF profile batch must contain exactly one venue")
        approval = _approved_source(self._approved_sources, source)
        prepared: list[_PreparedProfile] = []
        source_batch_id: UUID | None = None

        def prepare(session: Session) -> CanonicalReleaseCandidate:
            """在同一事务中解析身份、登记来源并构造一个场所目录 release 候选。"""
            nonlocal source_batch_id
            now = datetime.now(UTC)
            dataset_id = ensure_dataset(
                session,
                code=_PROFILE_DATASET,
                domain="etf",
                grain="ETF listing + profile effective range",
                now=now,
                schema_version=2,
            )
            methodology_id = ensure_methodology(
                session,
                code=_PROFILE_METHODOLOGY,
                semantic_family="reported-etf-profile",
                mapping_version=_PROFILE_MAPPING,
                documentation_ref="docs/service-data-sync/0020-etf-market-data/index.html",
            )
            source_dataset_id = ensure_source_dataset(
                session,
                approval=approval,
                capability=source.capability,
                native_grain="ETF listing catalog profile",
            )
            source_batch_id = record_source_batch(
                session, source=source, source_dataset_id=source_dataset_id, now=now
            )
            venue = values[0].etf.venue
            resolved = {
                value.etf.qualified_key: _ensure_etf_listing_identity(
                    session,
                    profile=value,
                    source_batch_id=source_batch_id,
                    now=now,
                )
                for value in values
            }
            current = _current_profiles(session, venue=venue, methodology_version_id=methodology_id)
            current_by_etf: dict[UUID, list[EtfProfileVersion]] = {}
            for (current_etf_id, _effective_from), row in current.items():
                current_by_etf.setdefault(current_etf_id, []).append(row)
            prepared[:] = []
            for value in values:
                etf_id = resolved[value.etf.qualified_key]
                content_hash = _profile_hash(value)
                timelines = sorted(
                    current_by_etf.get(etf_id, []),
                    key=lambda row: row.effective_from,
                )
                active = next(
                    (
                        row
                        for row in reversed(timelines)
                        if row.effective_from <= value.effective_from
                        and (row.effective_to is None or row.effective_to > value.effective_from)
                    ),
                    None,
                )
                if active is not None and _stored_profile_state_hash(active) == _profile_state_hash(
                    value
                ):
                    continue
                next_effective = next(
                    (
                        row.effective_from
                        for row in timelines
                        if row.effective_from > value.effective_from
                    ),
                    None,
                )
                prepared.append(
                    _PreparedProfile(
                        value=value,
                        etf_id=etf_id,
                        content_hash=content_hash,
                        effective_to=active.effective_to if active is not None else next_effective,
                        superseded=active,
                        clone_prior_segment=(
                            active is not None and active.effective_from < value.effective_from
                        ),
                    )
                )
            partition_key = f"venue:{venue}"
            normalization_run_id = record_normalization_run(
                session,
                dataset_id=dataset_id,
                partition_key=partition_key,
                source=source,
                source_batch_id=source_batch_id,
                mapping_version=_PROFILE_MAPPING,
                now=now,
            )
            return _candidate(
                session,
                dataset_id=dataset_id,
                dataset_code=_PROFILE_DATASET,
                methodology_version_id=methodology_id,
                normalization_run_id=normalization_run_id,
                partition_key=partition_key,
                current=current,
                changed={(item.etf_id, item.value.effective_from): item for item in prepared},
                source_batch_id=source_batch_id,
                mapping_version=_PROFILE_MAPPING,
                policy_code="etf.profile.quality",
                rule_code="governed-listing-only",
                observed_at=source.observed_at,
                now=now,
            )

        def write(session: Session, candidate: CanonicalReleaseCandidate, release_id: UUID) -> None:
            """关闭同上市工具和生效日起点的旧知识版本，写入不可变资料版本。"""
            if source_batch_id is None:
                raise AssertionError("ETF profile preparation did not resolve source batch")
            for item in prepared:
                if item.superseded is not None:
                    session.execute(
                        update(EtfProfileVersion)
                        .where(
                            EtfProfileVersion.profile_version_id
                            == item.superseded.profile_version_id,
                            EtfProfileVersion.known_to.is_(None),
                        )
                        .values(known_to=candidate.created_at)
                    )
                if item.clone_prior_segment and item.superseded is not None:
                    prior_id = uuid4()
                    session.execute(
                        insert(EtfProfileVersion).values(
                            profile_version_id=prior_id,
                            etf_id=item.superseded.etf_id,
                            display_name=item.superseded.display_name,
                            etf_type=item.superseded.etf_type,
                            management_mode=item.superseded.management_mode,
                            manager_name=item.superseded.manager_name,
                            custodian_name=item.superseded.custodian_name,
                            established_on=item.superseded.established_on,
                            listed_on=item.superseded.listed_on,
                            delisted_on=item.superseded.delisted_on,
                            quote_currency=item.superseded.quote_currency,
                            nav_currency=item.superseded.nav_currency,
                            listing_status=item.superseded.listing_status,
                            effective_from=item.superseded.effective_from,
                            effective_to=item.value.effective_from,
                            known_from=candidate.created_at,
                            known_to=None,
                            source_time_precision=item.superseded.source_time_precision,
                            methodology_version_id=candidate.methodology_version_id,
                            release_id=release_id,
                            source_batch_id=item.superseded.source_batch_id,
                            content_hash=item.superseded.content_hash,
                        )
                    )
                    record_manifest(
                        session,
                        normalization_run_id=candidate.normalization_run_id,
                        record_key_hash=_hash(
                            f"{item.etf_id}:{item.superseded.effective_from.isoformat()}"
                        ),
                        canonical_table=EtfProfileVersion.__tablename__,
                        canonical_pk={"profileVersionId": str(prior_id)},
                        content_hash=item.superseded.content_hash,
                    )
                row_id = uuid4()
                session.execute(
                    insert(EtfProfileVersion).values(
                        profile_version_id=row_id,
                        etf_id=item.etf_id,
                        display_name=item.value.display_name,
                        etf_type=item.value.etf_type,
                        management_mode=item.value.management_mode,
                        manager_name=item.value.manager_name,
                        custodian_name=item.value.custodian_name,
                        established_on=item.value.established_on,
                        listed_on=item.value.listed_on,
                        delisted_on=item.value.delisted_on,
                        quote_currency=item.value.quote_currency,
                        nav_currency=item.value.nav_currency,
                        listing_status=item.value.listing_status,
                        effective_from=item.value.effective_from,
                        effective_to=item.effective_to,
                        known_from=candidate.created_at,
                        known_to=None,
                        source_time_precision=item.value.source_time_precision,
                        methodology_version_id=candidate.methodology_version_id,
                        release_id=release_id,
                        source_batch_id=source_batch_id,
                        content_hash=item.content_hash,
                    )
                )
                record_manifest(
                    session,
                    normalization_run_id=candidate.normalization_run_id,
                    record_key_hash=_hash(f"{item.etf_id}:{item.value.effective_from.isoformat()}"),
                    canonical_table=EtfProfileVersion.__tablename__,
                    canonical_pk={"profileVersionId": str(row_id)},
                    content_hash=item.content_hash,
                )

        publication = self._release_repository.publish_prepared(
            prepare_candidate=prepare, write_facts=write
        )
        return PublishedEtfReference(
            data_version=publication.data_version,
            inserted_count=len(prepared),
            unchanged_count=len(values) - len(prepared),
            etf=None,
        )

    def publish_statuses(
        self,
        *,
        etf: EtfIdentifier,
        statuses: Sequence[EtfDailyStatus],
        source: EtfSourceObservation,
    ) -> PublishedEtfReference:
        """发布一个 ETF 的独立状态维度；缺少一个维度不会生成其他维度的停用事实。"""
        values = tuple(statuses)
        if not values or len(
            {(value.status_dimension, value.effective_from) for value in values}
        ) != len(values):
            raise ValueError(
                "ETF statuses must be non-empty and unique by dimension/effective start"
            )
        if any(value.etf != etf for value in values):
            raise ValueError("ETF status batch contains another listing")
        approval = _approved_source(self._approved_sources, source)
        prepared: list[_PreparedStatus] = []
        source_batch_id: UUID | None = None

        def prepare(session: Session) -> CanonicalReleaseCandidate:
            """在单事务中固定上市工具、来源证据和状态知识快照，再生成 release 候选。"""
            nonlocal source_batch_id
            now = datetime.now(UTC)
            dataset_id = ensure_dataset(
                session,
                code=_STATUS_DATASET,
                domain="etf",
                grain="ETF listing + status dimension + effective range",
                now=now,
                schema_version=2,
            )
            methodology_id = ensure_methodology(
                session,
                code=_STATUS_METHODOLOGY,
                semantic_family="reported-etf-trading-state",
                mapping_version=_STATUS_MAPPING,
                documentation_ref="docs/service-data-sync/0020-etf-market-data/index.html",
            )
            source_dataset_id = ensure_source_dataset(
                session,
                approval=approval,
                capability=source.capability,
                native_grain="ETF status event",
            )
            source_batch_id = record_source_batch(
                session, source=source, source_dataset_id=source_dataset_id, now=now
            )
            etf_id = _resolve_etf_id(
                session, etf=etf, fact_date=min(value.effective_from for value in values)
            )
            current = _current_statuses(
                session, etf_id=etf_id, methodology_version_id=methodology_id
            )
            prepared[:] = []
            for value in values:
                key = (value.status_dimension, value.effective_from)
                content_hash = _status_hash(value)
                existing = current.get(key)
                if existing is None or content_hash != existing.content_hash:
                    prepared.append(
                        _PreparedStatus(
                            value=value,
                            etf_id=etf_id,
                            content_hash=content_hash,
                            revision_no=1 if existing is None else existing.revision_no + 1,
                        )
                    )
            partition_key = f"etf:{etf_id}"
            normalization_run_id = record_normalization_run(
                session,
                dataset_id=dataset_id,
                partition_key=partition_key,
                source=source,
                source_batch_id=source_batch_id,
                mapping_version=_STATUS_MAPPING,
                now=now,
            )
            return _candidate(
                session,
                dataset_id=dataset_id,
                dataset_code=_STATUS_DATASET,
                methodology_version_id=methodology_id,
                normalization_run_id=normalization_run_id,
                partition_key=partition_key,
                current=current,
                changed={
                    (item.value.status_dimension, item.value.effective_from): item
                    for item in prepared
                },
                source_batch_id=source_batch_id,
                mapping_version=_STATUS_MAPPING,
                policy_code="etf.status.quality",
                rule_code="independent-status-dimensions",
                observed_at=source.observed_at,
                now=now,
            )

        def write(session: Session, candidate: CanonicalReleaseCandidate, release_id: UUID) -> None:
            """关闭同维度同生效日的旧知识版本，并写入独立状态 revision。"""
            if source_batch_id is None:
                raise AssertionError("ETF status preparation did not resolve source batch")
            for item in prepared:
                session.execute(
                    update(EtfStatusRevision)
                    .where(
                        EtfStatusRevision.etf_id == item.etf_id,
                        EtfStatusRevision.status_dimension == item.value.status_dimension,
                        EtfStatusRevision.effective_from == item.value.effective_from,
                        EtfStatusRevision.methodology_version_id
                        == candidate.methodology_version_id,
                        EtfStatusRevision.known_to.is_(None),
                    )
                    .values(known_to=candidate.created_at)
                )
                row_id = uuid4()
                session.execute(
                    insert(EtfStatusRevision).values(
                        status_revision_id=row_id,
                        etf_id=item.etf_id,
                        status_dimension=item.value.status_dimension,
                        status_code=item.value.status_code,
                        reason=item.value.reason,
                        effective_from=item.value.effective_from,
                        effective_to=item.value.effective_to,
                        known_from=candidate.created_at,
                        known_to=None,
                        methodology_version_id=candidate.methodology_version_id,
                        release_id=release_id,
                        revision_no=item.revision_no,
                        source_batch_id=source_batch_id,
                        content_hash=item.content_hash,
                        quality_status="passed",
                    )
                )
                record_manifest(
                    session,
                    normalization_run_id=candidate.normalization_run_id,
                    record_key_hash=_hash(
                        f"{item.etf_id}:{item.value.status_dimension}:{item.value.effective_from.isoformat()}"
                    ),
                    canonical_table=EtfStatusRevision.__tablename__,
                    canonical_pk={"statusRevisionId": str(row_id)},
                    content_hash=item.content_hash,
                )

        publication = self._release_repository.publish_prepared(
            prepare_candidate=prepare, write_facts=write
        )
        return PublishedEtfReference(
            data_version=publication.data_version,
            inserted_count=len(prepared),
            unchanged_count=len(values) - len(prepared),
            etf=etf,
        )


def _approved_source(
    approvals: Mapping[str, EtfReferenceSourceApproval], source: EtfSourceObservation
) -> EtfReferenceSourceApproval:
    """读取来源批准项；可调用 adapter 并不代表可对外发布数据。"""
    approval = approvals.get(source.provider_id)
    if approval is None:
        raise ValueError("ETF reference source provider is not approved for publication")
    return approval


def _resolve_etf_id(session: Session, *, etf: EtfIdentifier, fact_date: date) -> UUID:
    """按场所、代码、事实日解析唯一 ETF 上市工具，缺失身份链不得临时创建。"""
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


def _ensure_etf_listing_identity(
    session: Session,
    *,
    profile: EtfProfile,
    source_batch_id: UUID,
    now: datetime,
) -> UUID:
    """在目录首次看见 ETF 时创建最小身份链，并用官方上市日修复过晚的代码起点。"""
    try:
        instrument_id = _resolve_etf_id(
            session,
            etf=profile.etf,
            fact_date=profile.effective_from,
        )
        _backfill_etf_identifier_start(
            session,
            profile=profile,
            instrument_id=instrument_id,
            source_batch_id=source_batch_id,
            now=now,
        )
        return instrument_id
    except ValueError as error:
        # 歧义代码代表既有数据完整性问题，不能把它误当作“缺失”再插入另一套身份。
        if _etf_identity_candidates(
            session,
            etf=profile.etf,
            fact_date=profile.effective_from,
        ):
            raise error
    venue_id = _ensure_etf_venue(session, venue=profile.etf.venue)
    fund_id = uuid4()
    share_class_id = uuid4()
    instrument_id = uuid4()
    # AKShare 目录没有法律基金统一代码；以每个上市工具独立的未知法律基金根保存，避免错误跨场所合并。
    session.execute(
        insert(MarketEntity).values(
            entity_id=fund_id,
            entity_kind="FUND",
            created_at=now,
            retired_at=None,
        )
    )
    session.execute(
        insert(FundLegalEntity).values(
            entity_id=fund_id,
            legal_fund_code=None,
            manager_entity_ref=None,
            fund_type="ETF",
            base_currency=profile.nav_currency,
        )
    )
    session.execute(
        insert(MarketEntity).values(
            entity_id=share_class_id,
            entity_kind="FUND_SHARE",
            created_at=now,
            retired_at=None,
        )
    )
    session.execute(
        insert(FundShareClass).values(
            entity_id=share_class_id,
            fund_entity_id=fund_id,
            share_class_code=profile.etf.symbol,
            currency=profile.nav_currency,
            accumulation_kind="UNKNOWN",
        )
    )
    session.execute(
        insert(MarketEntity).values(
            entity_id=instrument_id,
            entity_kind="ETF_LISTING",
            created_at=now,
            retired_at=None,
        )
    )
    session.execute(
        insert(MarketInstrument).values(
            instrument_id=instrument_id,
            instrument_kind="ETF_LISTING",
            primary_venue_id=venue_id,
            # 目录来源没有首次可交易日期，不能将本次观测日伪装为历史上市日。
            tradable_from=profile.listed_on,
            tradable_to=profile.delisted_on,
        )
    )
    session.execute(
        insert(EtfListing).values(
            instrument_id=instrument_id,
            share_class_entity_id=share_class_id,
            venue_id=venue_id,
            management_mode=profile.management_mode,
        )
    )
    session.execute(
        insert(InstrumentIdentifierVersion).values(
            version_id=uuid4(),
            entity_id=instrument_id,
            entity_kind="ETF_LISTING",
            venue_id=venue_id,
            identifier_scheme=_IDENTIFIER_SCHEME,
            identifier_value=profile.etf.symbol,
            # 只有交易所目录明确给出上市日时才允许回溯代码有效期；否则保守使用本次观察日。
            effective_from=profile.listed_on or profile.effective_from,
            effective_to=None,
            known_from=now,
            known_to=None,
            source_time_precision=profile.source_time_precision,
            source_batch_id=source_batch_id,
        )
    )
    return instrument_id


def _backfill_etf_identifier_start(
    session: Session,
    *,
    profile: EtfProfile,
    instrument_id: UUID,
    source_batch_id: UUID,
    now: datetime,
) -> None:
    """以交易所明确上市日追加代码知识修订，使目录后接入时仍能安全承接历史事实。"""
    if profile.listed_on is None:
        return
    venue_id = session.execute(
        select(TradingVenue.venue_id).where(TradingVenue.code == profile.etf.venue)
    ).scalar_one()
    current = session.execute(
        select(InstrumentIdentifierVersion).where(
            InstrumentIdentifierVersion.entity_id == instrument_id,
            InstrumentIdentifierVersion.entity_kind == "ETF_LISTING",
            InstrumentIdentifierVersion.venue_id == venue_id,
            InstrumentIdentifierVersion.identifier_scheme == _IDENTIFIER_SCHEME,
            InstrumentIdentifierVersion.identifier_value == profile.etf.symbol,
            InstrumentIdentifierVersion.effective_from <= profile.effective_from,
            (InstrumentIdentifierVersion.effective_to.is_(None))
            | (InstrumentIdentifierVersion.effective_to > profile.effective_from),
            InstrumentIdentifierVersion.known_to.is_(None),
        )
    ).scalar_one()
    if profile.listed_on >= current.effective_from:
        return
    conflicting = session.execute(
        select(InstrumentIdentifierVersion.version_id).where(
            InstrumentIdentifierVersion.entity_kind == "ETF_LISTING",
            InstrumentIdentifierVersion.venue_id == venue_id,
            InstrumentIdentifierVersion.identifier_scheme == _IDENTIFIER_SCHEME,
            InstrumentIdentifierVersion.identifier_value == profile.etf.symbol,
            InstrumentIdentifierVersion.entity_id != instrument_id,
            InstrumentIdentifierVersion.effective_from < current.effective_from,
            (InstrumentIdentifierVersion.effective_to.is_(None))
            | (InstrumentIdentifierVersion.effective_to > profile.listed_on),
            InstrumentIdentifierVersion.known_to.is_(None),
        )
    ).first()
    if conflicting is not None:
        # 代码复用历史存在交叠时必须交由数据治理确认，不能把当前工具自动覆盖到旧工具区间。
        raise ValueError("ETF listing history conflicts with the reported listing date")
    session.execute(
        update(MarketInstrument)
        .where(
            MarketInstrument.instrument_id == instrument_id,
            (MarketInstrument.tradable_from.is_(None))
            | (MarketInstrument.tradable_from > profile.listed_on),
        )
        .values(tradable_from=profile.listed_on)
    )
    session.execute(
        update(InstrumentIdentifierVersion)
        .where(
            InstrumentIdentifierVersion.version_id == current.version_id,
            InstrumentIdentifierVersion.known_to.is_(None),
        )
        .values(known_to=now)
    )
    session.execute(
        insert(InstrumentIdentifierVersion).values(
            version_id=uuid4(),
            entity_id=current.entity_id,
            entity_kind=current.entity_kind,
            venue_id=current.venue_id,
            identifier_scheme=current.identifier_scheme,
            identifier_value=current.identifier_value,
            effective_from=profile.listed_on,
            effective_to=current.effective_to,
            known_from=now,
            known_to=None,
            source_time_precision=profile.source_time_precision,
            source_batch_id=source_batch_id,
        )
    )


def _etf_identity_candidates(session: Session, *, etf: EtfIdentifier, fact_date: date) -> set[UUID]:
    """读取当前有效 ETF 代码候选，用于区分首次出现与既有代码歧义。"""
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
    return {UUID(str(value)) for value in rows}


def _ensure_etf_venue(session: Session, *, venue: str) -> UUID:
    """读取或初始化 ETF 所在交易所静态字典，避免首次目录依赖人工插入场所行。"""
    existing = session.execute(
        select(TradingVenue.venue_id).where(TradingVenue.code == venue)
    ).scalar_one_or_none()
    if existing is not None:
        return UUID(str(existing))
    definitions = {
        "SSE": {"mic": "XSHG", "name": "上海证券交易所"},
        "SZSE": {"mic": "XSHE", "name": "深圳证券交易所"},
    }
    definition = definitions.get(venue)
    if definition is None:
        raise ValueError("unsupported ETF venue")
    venue_id = uuid4()
    session.execute(
        insert(TradingVenue).values(
            venue_id=venue_id,
            code=venue,
            mic=definition["mic"],
            name=definition["name"],
            timezone="Asia/Shanghai",
            country="CN",
            active=True,
        )
    )
    return venue_id


def _current_profiles(
    session: Session, *, venue: str, methodology_version_id: UUID
) -> dict[tuple[UUID, date], EtfProfileVersion]:
    """读取场所当前产品资料知识，目录有限同步不会删除本次未出现的既有 listing。"""
    rows = (
        session.execute(
            select(EtfProfileVersion)
            .join(EtfListing, EtfListing.instrument_id == EtfProfileVersion.etf_id)
            .join(TradingVenue, TradingVenue.venue_id == EtfListing.venue_id)
            .where(
                TradingVenue.code == venue,
                EtfProfileVersion.methodology_version_id == methodology_version_id,
                EtfProfileVersion.known_to.is_(None),
            )
        )
        .scalars()
        .all()
    )
    result = {(UUID(str(row.etf_id)), row.effective_from): row for row in rows}
    if len(result) != len(rows):
        raise ValueError("ETF profile current revision is ambiguous")
    return result


def _current_statuses(
    session: Session, *, etf_id: UUID, methodology_version_id: UUID
) -> dict[tuple[str, date], EtfStatusRevision]:
    """读取一个 ETF 当前状态知识，三个维度各自保留自己的时间轴。"""
    rows = (
        session.execute(
            select(EtfStatusRevision).where(
                EtfStatusRevision.etf_id == etf_id,
                EtfStatusRevision.methodology_version_id == methodology_version_id,
                EtfStatusRevision.known_to.is_(None),
            )
        )
        .scalars()
        .all()
    )
    result = {(row.status_dimension, row.effective_from): row for row in rows}
    if len(result) != len(rows):
        raise ValueError("ETF status current revision is ambiguous")
    return result


def _candidate(
    session: Session,
    *,
    dataset_id: UUID,
    dataset_code: str,
    methodology_version_id: UUID,
    normalization_run_id: UUID,
    partition_key: str,
    current: Mapping[Any, Any],
    changed: Mapping[Any, _PreparedProfile | _PreparedStatus],
    source_batch_id: UUID,
    mapping_version: str,
    policy_code: str,
    rule_code: str,
    observed_at: datetime,
    now: datetime,
) -> CanonicalReleaseCandidate:
    """合成当前和变化的资料或状态血缘，并以 fencing token 防止并发任务回退检查点。"""
    records: list[CanonicalLineageRecord] = []
    dates: list[date] = []
    for key in sorted({*current, *changed}, key=str):
        item = changed.get(key)
        if item is None:
            existing = current[key]
            content_hash = existing.content_hash
            batch_id = UUID(str(existing.source_batch_id))
            fact_date = existing.effective_from
        else:
            content_hash = item.content_hash
            batch_id = source_batch_id
            fact_date = item.value.effective_from
        dates.append(fact_date)
        records.append(
            CanonicalLineageRecord(
                record_key_hash=_hash(str(key)),
                content_hash=content_hash,
                source_batch_id=batch_id,
                transform_hash=_hash(mapping_version),
            )
        )
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
        checkpoint_position={
            "effectiveFrom": max(dates).isoformat(),
            "observedAt": observed_at.isoformat(),
        },
        expected_fencing_token=0 if fencing_token is None else int(fencing_token),
        created_at=now,
    )


def _profile_hash(value: EtfProfile) -> str:
    """计算产品资料摘要，目录条目内容变化才建立新的知识版本。"""
    return _payload_hash(
        {
            "etf": value.etf.qualified_key,
            "displayName": value.display_name,
            "etfType": value.etf_type,
            "managementMode": value.management_mode,
            "managerName": value.manager_name,
            "custodianName": value.custodian_name,
            "establishedOn": _date(value.established_on),
            "listedOn": _date(value.listed_on),
            "delistedOn": _date(value.delisted_on),
            "quoteCurrency": value.quote_currency,
            "navCurrency": value.nav_currency,
            "listingStatus": value.listing_status,
            "effectiveFrom": value.effective_from.isoformat(),
            "sourceTimePrecision": value.source_time_precision,
        }
    )


def _profile_state_hash(value: EtfProfile) -> str:
    """忽略观察日起点比较产品状态，连续日相同目录不能制造重叠业务版本。"""
    return _payload_hash(
        {
            "displayName": value.display_name,
            "etfType": value.etf_type,
            "managementMode": value.management_mode,
            "managerName": value.manager_name,
            "custodianName": value.custodian_name,
            "establishedOn": _date(value.established_on),
            "listedOn": _date(value.listed_on),
            "delistedOn": _date(value.delisted_on),
            "quoteCurrency": value.quote_currency,
            "navCurrency": value.nav_currency,
            "listingStatus": value.listing_status,
            "sourceTimePrecision": value.source_time_precision,
        }
    )


def _stored_profile_state_hash(value: EtfProfileVersion) -> str:
    """把已存资料投影到与来源领域对象相同的状态摘要口径。"""
    return _payload_hash(
        {
            "displayName": value.display_name,
            "etfType": value.etf_type,
            "managementMode": value.management_mode,
            "managerName": value.manager_name,
            "custodianName": value.custodian_name,
            "establishedOn": _date(value.established_on),
            "listedOn": _date(value.listed_on),
            "delistedOn": _date(value.delisted_on),
            "quoteCurrency": value.quote_currency,
            "navCurrency": value.nav_currency,
            "listingStatus": value.listing_status,
            "sourceTimePrecision": value.source_time_precision,
        }
    )


def _status_hash(value: EtfDailyStatus) -> str:
    """计算单维度状态摘要，停牌不会覆盖申购或赎回事实。"""
    return _payload_hash(
        {
            "etf": value.etf.qualified_key,
            "dimension": value.status_dimension,
            "statusCode": value.status_code,
            "effectiveFrom": value.effective_from.isoformat(),
            "effectiveTo": _date(value.effective_to),
            "reason": value.reason,
        }
    )


def _payload_hash(payload: Mapping[str, object]) -> str:
    """将规范 JSON 计算为内容 SHA-256，空值和真实文本的身份保持稳定。"""
    return _hash(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _hash(value: str) -> str:
    """计算文本 SHA-256，供业务键和变换摘要复用。"""
    return hashlib.sha256(value.encode()).hexdigest()


def _date(value: date | None) -> str | None:
    """把可选日期稳定投影为 ISO 文本，避免摘要受对象表示差异影响。"""
    return None if value is None else value.isoformat()
