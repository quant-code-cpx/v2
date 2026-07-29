"""融资融券 P0 场所市场汇总的原子 canonical 发布仓储。"""

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
from service_data_sync.application.ports.margin_market import (
    MarginEligibilityRepository,
    MarginMarketDailyRepository,
    MarginSecurityDailyRepository,
    MarginSourceObservation,
    PublishedMarginEligibility,
    PublishedMarginMarketDaily,
    PublishedMarginSecurityDaily,
)
from service_data_sync.domain.margin import (
    MarginEligibility,
    MarginMarketDaily,
    MarginSecurityDaily,
    MarginVenue,
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
    MarginEligibilityRevision,
    MarginMarketDailyRevision,
    MarginSecurityDailyRevision,
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

_DATASET = "market.margin.market.1d.reported"
_METHODOLOGY = "margin-venue-daily-reported"
_MAPPING_VERSION = "margin-market-daily-v1"
_SECURITY_DATASET = "market.margin.security.1d.reported"
_SECURITY_METHODOLOGY = "margin-security-daily-reported"
_SECURITY_MAPPING_VERSION = "margin-security-daily-v1"
_ELIGIBILITY_DATASET = "market.margin.eligibility.reported"
_ELIGIBILITY_METHODOLOGY = "margin-eligibility-reported"
_ELIGIBILITY_MAPPING_VERSION = "margin-eligibility-v1"


@dataclass(frozen=True, slots=True)
class MarginSourceApproval(TypedP0SourceApproval):
    """标识可用于融资融券生产发布的来源批准项。"""


@dataclass(frozen=True, slots=True)
class _PreparedRecord:
    """封装一条待写市场汇总 revision 的领域值、内容摘要与递增版本。"""

    value: MarginMarketDaily
    content_hash: str
    revision_no: int


@dataclass(frozen=True, slots=True)
class _PreparedSecurityRecord:
    """封装待写证券两融 revision 的来源值、已解析永久身份和递增版本。"""

    value: MarginSecurityDaily
    security_id: int
    content_hash: str
    revision_no: int


@dataclass(frozen=True, slots=True)
class _PreparedEligibilityRecord:
    """封装待写两融资格 revision 的领域值、永久证券身份、摘要与序号。"""

    value: MarginEligibility
    security_id: int
    content_hash: str
    revision_no: int


class SqlAlchemyMarginMarketDataRepository(
    MarginMarketDailyRepository, MarginSecurityDailyRepository, MarginEligibilityRepository
):
    """仅向已批准来源和已登记交易场所发布两融市场汇总。"""

    def __init__(
        self,
        database: DatabaseClient,
        *,
        approved_sources: Mapping[str, MarginSourceApproval] | None = None,
    ) -> None:
        """保存事务工厂和显式来源批准表；默认空表确保未知来源不会生产发布。"""
        self._database = database
        self._approved_sources = dict(approved_sources or {})
        self._release_repository = SqlAlchemyCanonicalReleaseRepository(database)

    def publish_market_daily(
        self,
        *,
        venue: MarginVenue,
        records: Sequence[MarginMarketDaily],
        source: MarginSourceObservation,
    ) -> PublishedMarginMarketDaily:
        """原子发布一个场所完整当前汇总快照，禁止由证券明细或公式补齐任何字段。"""
        values = tuple(records)
        if not values or len({item.trade_date for item in values}) != len(values):
            raise ValueError("margin market records must be non-empty and unique by trade date")
        approval = self._approved_sources.get(source.provider_id)
        if approval is None:
            raise ValueError("margin source provider is not approved for publication")
        prepared: list[_PreparedRecord] = []
        venue_id: UUID | None = None
        source_batch_id: UUID | None = None

        def prepare(session: Session) -> CanonicalReleaseCandidate:
            """在同一事务中固化来源、场所、当前 revision 和完整 release 候选。"""
            nonlocal venue_id, source_batch_id
            now = datetime.now(UTC)
            dataset_id = ensure_dataset(
                session,
                code=_DATASET,
                domain="margin",
                grain="trading venue + trade date + reported methodology",
                now=now,
            )
            methodology_id = ensure_methodology(
                session,
                code=_METHODOLOGY,
                semantic_family="reported-margin-venue-daily",
                mapping_version=_MAPPING_VERSION,
                documentation_ref="docs/service-data-sync/0021-margin-trading/index.html",
            )
            source_dataset_id = ensure_source_dataset(
                session,
                approval=approval,
                capability=source.capability,
                native_grain="trading venue + trade date",
            )
            source_batch_id = record_source_batch(
                session, source=source, source_dataset_id=source_dataset_id, now=now
            )
            venue_id = _resolve_venue_id(session, venue=venue)
            partition_key = _partition_key(venue_id)
            normalization_run_id = record_normalization_run(
                session,
                dataset_id=dataset_id,
                partition_key=partition_key,
                source=source,
                source_batch_id=source_batch_id,
                mapping_version=_MAPPING_VERSION,
                now=now,
            )
            current = _current_records(
                session, venue_id=venue_id, methodology_version_id=methodology_id
            )
            incoming = {item.trade_date: item for item in values}
            prepared[:] = [
                _PreparedRecord(
                    value=item,
                    content_hash=_content_hash(item),
                    revision_no=current[item.trade_date].revision_no + 1
                    if item.trade_date in current
                    else 1,
                )
                for item in values
                if item.trade_date not in current
                or _content_hash(item) != current[item.trade_date].content_hash
            ]
            return _candidate(
                session,
                dataset_id=dataset_id,
                methodology_version_id=methodology_id,
                normalization_run_id=normalization_run_id,
                partition_key=partition_key,
                venue_id=venue_id,
                current=current,
                incoming=incoming,
                changed={item.value.trade_date: item for item in prepared},
                source_batch_id=source_batch_id,
                now=now,
            )

        def write(session: Session, candidate: CanonicalReleaseCandidate, release_id: UUID) -> None:
            """关闭内容变化日的旧知识区间并写入新 revision 和标准化 manifest。"""
            if venue_id is None or source_batch_id is None:
                raise AssertionError("margin market preparation did not resolve required state")
            for item in prepared:
                session.execute(
                    update(MarginMarketDailyRevision)
                    .where(
                        MarginMarketDailyRevision.venue_id == venue_id,
                        MarginMarketDailyRevision.trade_date == item.value.trade_date,
                        MarginMarketDailyRevision.methodology_version_id
                        == candidate.methodology_version_id,
                        MarginMarketDailyRevision.known_to.is_(None),
                    )
                    .values(known_to=candidate.created_at)
                )
                row_id = uuid4()
                session.execute(
                    insert(MarginMarketDailyRevision).values(
                        trade_date=item.value.trade_date,
                        row_id=row_id,
                        venue_id=venue_id,
                        financing_balance=item.value.financing_balance,
                        financing_buy_amount=item.value.financing_buy_amount,
                        financing_repayment_amount=item.value.financing_repayment_amount,
                        lending_balance_amount=item.value.lending_balance_amount,
                        lending_balance_qty=item.value.lending_balance_qty,
                        lending_sell_qty=item.value.lending_sell_qty,
                        lending_repayment_qty=item.value.lending_repayment_qty,
                        total_balance=item.value.total_balance,
                        currency=item.value.currency,
                        quantity_unit=item.value.quantity_unit,
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
                    record_key_hash=_record_key(venue_id, item.value.trade_date),
                    canonical_table=MarginMarketDailyRevision.__tablename__,
                    canonical_pk={
                        "tradeDate": item.value.trade_date.isoformat(),
                        "rowId": str(row_id),
                    },
                    content_hash=item.content_hash,
                )

        publication = self._release_repository.publish_prepared(
            prepare_candidate=prepare, write_facts=write
        )
        return PublishedMarginMarketDaily(
            data_version=publication.data_version,
            inserted_count=len(prepared),
            unchanged_count=len(values) - len(prepared),
            venue=venue,
        )

    def publish_security_daily(
        self,
        *,
        venue: MarginVenue,
        records: Sequence[MarginSecurityDaily],
        source: MarginSourceObservation,
    ) -> PublishedMarginSecurityDaily:
        """发布交易所内完整证券两融快照；身份或单位异常不能退化为按代码写入。"""
        values = tuple(records)
        if not values or len(
            {(item.source_security_code, item.trade_date) for item in values}
        ) != len(values):
            raise ValueError(
                "margin security records must be non-empty and unique by source code/date"
            )
        approval = self._approved_sources.get(source.provider_id)
        if approval is None:
            raise ValueError("margin source provider is not approved for publication")
        prepared: list[_PreparedSecurityRecord] = []
        venue_id: UUID | None = None
        source_batch_id: UUID | None = None

        def prepare(session: Session) -> CanonicalReleaseCandidate:
            """在单事务内固定场所全快照、来源观察、解析身份与完整 release 候选。"""
            nonlocal venue_id, source_batch_id
            now = datetime.now(UTC)
            dataset_id = ensure_dataset(
                session,
                code=_SECURITY_DATASET,
                domain="margin",
                grain="equity security + trade date + reported methodology",
                now=now,
            )
            methodology_id = ensure_methodology(
                session,
                code=_SECURITY_METHODOLOGY,
                semantic_family="reported-margin-security-daily",
                mapping_version=_SECURITY_MAPPING_VERSION,
                documentation_ref="docs/service-data-sync/0021-margin-trading/index.html",
            )
            source_dataset_id = ensure_source_dataset(
                session,
                approval=approval,
                capability=source.capability,
                native_grain="equity security + trade date",
            )
            source_batch_id = record_source_batch(
                session, source=source, source_dataset_id=source_dataset_id, now=now
            )
            venue_id = _resolve_venue_id(session, venue=venue)
            resolved = {
                (item.source_security_code, item.trade_date): _resolve_security_id(
                    session,
                    venue=venue,
                    source_code=item.source_security_code,
                    fact_date=item.trade_date,
                )
                for item in values
            }
            partition_key = _partition_key(venue_id)
            normalization_run_id = record_normalization_run(
                session,
                dataset_id=dataset_id,
                partition_key=partition_key,
                source=source,
                source_batch_id=source_batch_id,
                mapping_version=_SECURITY_MAPPING_VERSION,
                now=now,
            )
            current = _current_security_records(
                session, venue=venue, methodology_version_id=methodology_id
            )
            incoming = {
                (resolved[(item.source_security_code, item.trade_date)], item.trade_date): item
                for item in values
            }
            prepared[:] = [
                _PreparedSecurityRecord(
                    value=item,
                    security_id=resolved[(item.source_security_code, item.trade_date)],
                    content_hash=_security_content_hash(item),
                    revision_no=current[
                        (resolved[(item.source_security_code, item.trade_date)], item.trade_date)
                    ].revision_no
                    + 1
                    if (resolved[(item.source_security_code, item.trade_date)], item.trade_date)
                    in current
                    else 1,
                )
                for item in values
                if (resolved[(item.source_security_code, item.trade_date)], item.trade_date)
                not in current
                or _security_content_hash(item)
                != current[
                    (resolved[(item.source_security_code, item.trade_date)], item.trade_date)
                ].content_hash
            ]
            return _security_candidate(
                session,
                dataset_id=dataset_id,
                methodology_version_id=methodology_id,
                normalization_run_id=normalization_run_id,
                partition_key=partition_key,
                current=current,
                incoming=incoming,
                changed={(item.security_id, item.value.trade_date): item for item in prepared},
                source_batch_id=source_batch_id,
                now=now,
            )

        def write(session: Session, candidate: CanonicalReleaseCandidate, release_id: UUID) -> None:
            """关闭变化证券日的旧知识版本并写入新 revision；直报偿还字段不产生派生列。"""
            if source_batch_id is None:
                raise AssertionError("margin security preparation did not resolve source batch")
            for item in prepared:
                session.execute(
                    update(MarginSecurityDailyRevision)
                    .where(
                        MarginSecurityDailyRevision.security_id == item.security_id,
                        MarginSecurityDailyRevision.trade_date == item.value.trade_date,
                        MarginSecurityDailyRevision.methodology_version_id
                        == candidate.methodology_version_id,
                        MarginSecurityDailyRevision.known_to.is_(None),
                    )
                    .values(known_to=candidate.created_at)
                )
                row_id = uuid4()
                session.execute(
                    insert(MarginSecurityDailyRevision).values(
                        trade_date=item.value.trade_date,
                        row_id=row_id,
                        security_id=item.security_id,
                        financing_balance=item.value.financing_balance,
                        financing_buy_amount=item.value.financing_buy_amount,
                        financing_repayment_reported=item.value.financing_repayment_reported,
                        financing_repayment_derived=None,
                        derived_methodology_version_id=None,
                        lending_balance_qty=item.value.lending_balance_qty,
                        quantity_unit=item.value.quantity_unit,
                        currency=item.value.currency,
                        null_reason=item.value.null_reason,
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
                    record_key_hash=_security_record_key(item.security_id, item.value.trade_date),
                    canonical_table=MarginSecurityDailyRevision.__tablename__,
                    canonical_pk={
                        "tradeDate": item.value.trade_date.isoformat(),
                        "rowId": str(row_id),
                    },
                    content_hash=item.content_hash,
                )

        publication = self._release_repository.publish_prepared(
            prepare_candidate=prepare, write_facts=write
        )
        return PublishedMarginSecurityDaily(
            data_version=publication.data_version,
            inserted_count=len(prepared),
            unchanged_count=len(values) - len(prepared),
            venue=venue,
        )

    def publish_eligibility(
        self,
        *,
        venue: MarginVenue,
        records: Sequence[MarginEligibility],
        source: MarginSourceObservation,
    ) -> PublishedMarginEligibility:
        """发布两融资格双时间记录；观察名单只能追加知识，不会关闭未出现在本次列表的历史资格。"""
        values = tuple(records)
        if not values or len(
            {(item.source_security_code, item.effective_from) for item in values}
        ) != len(values):
            raise ValueError("margin eligibility records must be non-empty and uniquely evidenced")
        approval = self._approved_sources.get(source.provider_id)
        if approval is None:
            raise ValueError("margin source provider is not approved for publication")
        prepared: list[_PreparedEligibilityRecord] = []
        venue_id: UUID | None = None
        source_batch_id: UUID | None = None

        def prepare(session: Session) -> CanonicalReleaseCandidate:
            """在同一事务中解析资格证券、来源证据和场所完整 release 快照。"""
            nonlocal venue_id, source_batch_id
            now = datetime.now(UTC)
            dataset_id = ensure_dataset(
                session,
                code=_ELIGIBILITY_DATASET,
                domain="margin",
                grain="equity security + eligibility effective range + reported methodology",
                now=now,
            )
            methodology_id = ensure_methodology(
                session,
                code=_ELIGIBILITY_METHODOLOGY,
                semantic_family="reported-margin-eligibility",
                mapping_version=_ELIGIBILITY_MAPPING_VERSION,
                documentation_ref="docs/service-data-sync/0021-margin-trading/index.html",
            )
            source_dataset_id = ensure_source_dataset(
                session,
                approval=approval,
                capability=source.capability,
                native_grain="equity security + eligibility effective range",
            )
            source_batch_id = record_source_batch(
                session, source=source, source_dataset_id=source_dataset_id, now=now
            )
            venue_id = _resolve_venue_id(session, venue=venue)
            resolved = {
                (item.source_security_code, item.effective_from): _resolve_security_id(
                    session,
                    venue=venue,
                    source_code=item.source_security_code,
                    fact_date=item.effective_from,
                )
                for item in values
            }
            partition_key = _partition_key(venue_id)
            normalization_run_id = record_normalization_run(
                session,
                dataset_id=dataset_id,
                partition_key=partition_key,
                source=source,
                source_batch_id=source_batch_id,
                mapping_version=_ELIGIBILITY_MAPPING_VERSION,
                now=now,
            )
            current = _current_eligibility_records(
                session, venue=venue, methodology_version_id=methodology_id
            )
            incoming = {
                (
                    resolved[(item.source_security_code, item.effective_from)],
                    item.effective_from,
                ): item
                for item in values
            }
            prepared[:] = [
                _PreparedEligibilityRecord(
                    value=item,
                    security_id=resolved[(item.source_security_code, item.effective_from)],
                    content_hash=_eligibility_content_hash(item),
                    revision_no=current[
                        (
                            resolved[(item.source_security_code, item.effective_from)],
                            item.effective_from,
                        )
                    ].revision_no
                    + 1
                    if (
                        resolved[(item.source_security_code, item.effective_from)],
                        item.effective_from,
                    )
                    in current
                    else 1,
                )
                for item in values
                if (resolved[(item.source_security_code, item.effective_from)], item.effective_from)
                not in current
                or _eligibility_content_hash(item)
                != current[
                    (
                        resolved[(item.source_security_code, item.effective_from)],
                        item.effective_from,
                    )
                ].content_hash
            ]
            return _eligibility_candidate(
                session,
                dataset_id=dataset_id,
                methodology_version_id=methodology_id,
                normalization_run_id=normalization_run_id,
                partition_key=partition_key,
                current=current,
                incoming=incoming,
                changed={(item.security_id, item.value.effective_from): item for item in prepared},
                source_batch_id=source_batch_id,
                now=now,
            )

        def write(session: Session, candidate: CanonicalReleaseCandidate, release_id: UUID) -> None:
            """关闭被同一资格起点替代的旧知识版本，其他名单项不因缺席而自动失效。"""
            if source_batch_id is None:
                raise AssertionError("margin eligibility preparation did not resolve source batch")
            for item in prepared:
                session.execute(
                    update(MarginEligibilityRevision)
                    .where(
                        MarginEligibilityRevision.security_id == item.security_id,
                        MarginEligibilityRevision.effective_from == item.value.effective_from,
                        MarginEligibilityRevision.methodology_version_id
                        == candidate.methodology_version_id,
                        MarginEligibilityRevision.known_to.is_(None),
                    )
                    .values(known_to=candidate.created_at)
                )
                row_id = uuid4()
                session.execute(
                    insert(MarginEligibilityRevision).values(
                        eligibility_revision_id=row_id,
                        security_id=item.security_id,
                        status=item.value.status,
                        evidence_basis=item.value.evidence_basis,
                        announcement_at=item.value.announcement_on,
                        effective_from=item.value.effective_from,
                        effective_to=item.value.effective_to,
                        methodology_version_id=candidate.methodology_version_id,
                        release_id=release_id,
                        revision_no=item.revision_no,
                        source_batch_id=source_batch_id,
                        source_published_at=None,
                        source_time_precision="DATE_ONLY"
                        if item.value.announcement_on is not None
                        else "UNKNOWN",
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
                    record_key_hash=_eligibility_record_key(
                        item.security_id, item.value.effective_from
                    ),
                    canonical_table=MarginEligibilityRevision.__tablename__,
                    canonical_pk={
                        "eligibilityRevisionId": str(row_id),
                        "securityId": str(item.security_id),
                    },
                    content_hash=item.content_hash,
                )

        publication = self._release_repository.publish_prepared(
            prepare_candidate=prepare, write_facts=write
        )
        return PublishedMarginEligibility(
            data_version=publication.data_version,
            inserted_count=len(prepared),
            unchanged_count=len(values) - len(prepared),
            venue=venue,
        )


def _resolve_venue_id(session: Session, *, venue: MarginVenue) -> UUID:
    """解析已治理交易场所 UUID；场所目录缺失或重复时不创建临时身份。"""
    rows = (
        session.execute(select(TradingVenue.venue_id).where(TradingVenue.code == venue.code))
        .scalars()
        .all()
    )
    candidates = {UUID(str(value)) for value in rows}
    if len(candidates) != 1:
        raise ValueError("margin trading venue identity is missing or ambiguous")
    return candidates.pop()


def _current_records(
    session: Session, *, venue_id: UUID, methodology_version_id: UUID
) -> dict[date, MarginMarketDailyRevision]:
    """读取场所当前知识快照；同日重复行代表损坏并拒绝构造不确定 release。"""
    rows = (
        session.execute(
            select(MarginMarketDailyRevision).where(
                MarginMarketDailyRevision.venue_id == venue_id,
                MarginMarketDailyRevision.methodology_version_id == methodology_version_id,
                MarginMarketDailyRevision.known_to.is_(None),
            )
        )
        .scalars()
        .all()
    )
    result = {item.trade_date: item for item in rows}
    if len(result) != len(rows):
        raise ValueError("margin market current revision is ambiguous")
    return result


def _resolve_security_id(
    session: Session,
    *,
    venue: MarginVenue,
    source_code: str,
    fact_date: date,
) -> int:
    """按交易所、代码、事实日期和当前知识解析唯一 CONFIRMED 证券，不以当前投影猜测复用代码。"""
    rows = (
        session.execute(
            select(EquityIdentifierVersion.security_id).where(
                EquityIdentifierVersion.exchange == venue.code,
                EquityIdentifierVersion.symbol == source_code,
                EquityIdentifierVersion.identity_state == "CONFIRMED",
                EquityIdentifierVersion.effective_from <= fact_date,
                (EquityIdentifierVersion.effective_to.is_(None))
                | (EquityIdentifierVersion.effective_to > fact_date),
                EquityIdentifierVersion.known_to.is_(None),
            )
        )
        .scalars()
        .all()
    )
    candidates = {int(value) for value in rows}
    if len(candidates) != 1:
        raise ValueError("margin security identity is missing or ambiguous")
    return candidates.pop()


def _current_security_records(
    session: Session, *, venue: MarginVenue, methodology_version_id: UUID
) -> dict[tuple[int, date], MarginSecurityDailyRevision]:
    """读取一个场所全部当前证券快照，release 不会因有界同步请求遗失其他证券历史。"""
    rows = (
        session.execute(
            select(MarginSecurityDailyRevision)
            .join(
                EquityInstrument,
                EquityInstrument.security_id == MarginSecurityDailyRevision.security_id,
            )
            .where(
                EquityInstrument.exchange == venue.code,
                MarginSecurityDailyRevision.methodology_version_id == methodology_version_id,
                MarginSecurityDailyRevision.known_to.is_(None),
            )
        )
        .scalars()
        .all()
    )
    result = {(item.security_id, item.trade_date): item for item in rows}
    if len(result) != len(rows):
        raise ValueError("margin security current revision is ambiguous")
    return result


def _candidate(
    session: Session,
    *,
    dataset_id: UUID,
    methodology_version_id: UUID,
    normalization_run_id: UUID,
    partition_key: str,
    venue_id: UUID,
    current: Mapping[date, MarginMarketDailyRevision],
    incoming: Mapping[date, MarginMarketDaily],
    changed: Mapping[date, _PreparedRecord],
    source_batch_id: UUID,
    now: datetime,
) -> CanonicalReleaseCandidate:
    """将旧快照和新变化合成完整 release 血缘，未变化行继续关联原始来源批次。"""
    records: list[CanonicalLineageRecord] = []
    for trade_date in sorted({*current, *incoming}):
        changed_record = changed.get(trade_date)
        if changed_record is None:
            existing = current[trade_date]
            content_hash = existing.content_hash
            batch_id = UUID(str(existing.source_batch_id))
        else:
            content_hash = changed_record.content_hash
            batch_id = source_batch_id
        records.append(
            CanonicalLineageRecord(
                record_key_hash=_record_key(venue_id, trade_date),
                content_hash=content_hash,
                source_batch_id=batch_id,
                transform_hash=hashlib.sha256(_MAPPING_VERSION.encode()).hexdigest(),
            )
        )
    fact_dates = tuple(sorted({*current, *incoming}))
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
        dataset_code=_DATASET,
        partition_key=partition_key,
        methodology_version_id=methodology_version_id,
        normalization_run_id=normalization_run_id,
        records=tuple(records),
        quality=CanonicalQualityDecision(
            status="passed",
            policy_code="margin.market-daily.quality",
            policy_version=1,
            rules=(CanonicalQualityRule("reported-values-only", "blocking", True),),
        ),
        fact_min=min(fact_dates),
        fact_max=max(fact_dates),
        checkpoint_kind="published",
        checkpoint_position={"tradeDate": max(fact_dates).isoformat()},
        expected_fencing_token=0 if fencing_token is None else int(fencing_token),
        created_at=now,
    )


def _security_candidate(
    session: Session,
    *,
    dataset_id: UUID,
    methodology_version_id: UUID,
    normalization_run_id: UUID,
    partition_key: str,
    current: Mapping[tuple[int, date], MarginSecurityDailyRevision],
    incoming: Mapping[tuple[int, date], MarginSecurityDaily],
    changed: Mapping[tuple[int, date], _PreparedSecurityRecord],
    source_batch_id: UUID,
    now: datetime,
) -> CanonicalReleaseCandidate:
    """由场所内完整证券快照构造 release，未变化事实保留其最初来源批次与内容摘要。"""
    records: list[CanonicalLineageRecord] = []
    for security_id, trade_date in sorted({*current, *incoming}):
        changed_record = changed.get((security_id, trade_date))
        if changed_record is None:
            existing = current[(security_id, trade_date)]
            content_hash = existing.content_hash
            batch_id = UUID(str(existing.source_batch_id))
        else:
            content_hash = changed_record.content_hash
            batch_id = source_batch_id
        records.append(
            CanonicalLineageRecord(
                record_key_hash=_security_record_key(security_id, trade_date),
                content_hash=content_hash,
                source_batch_id=batch_id,
                transform_hash=hashlib.sha256(_SECURITY_MAPPING_VERSION.encode()).hexdigest(),
            )
        )
    fact_dates = tuple(sorted({trade_date for _security_id, trade_date in {*current, *incoming}}))
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
        dataset_code=_SECURITY_DATASET,
        partition_key=partition_key,
        methodology_version_id=methodology_version_id,
        normalization_run_id=normalization_run_id,
        records=tuple(records),
        quality=CanonicalQualityDecision(
            status="passed",
            policy_code="margin.security-daily.quality",
            policy_version=1,
            rules=(CanonicalQualityRule("reported-repayment-only", "blocking", True),),
        ),
        fact_min=min(fact_dates),
        fact_max=max(fact_dates),
        checkpoint_kind="published",
        checkpoint_position={"tradeDate": max(fact_dates).isoformat()},
        expected_fencing_token=0 if fencing_token is None else int(fencing_token),
        created_at=now,
    )


def _record_key(venue_id: UUID, trade_date: date) -> str:
    """计算场所市场汇总逻辑键摘要，场所 UUID 和交易日共同决定事实身份。"""
    return hashlib.sha256(f"{venue_id}:{trade_date.isoformat()}".encode()).hexdigest()


def _content_hash(value: MarginMarketDaily) -> str:
    """以规范化直报值生成内容摘要，空值和真实零值都会稳定参与 revision 身份。"""
    payload = {
        "tradeDate": value.trade_date.isoformat(),
        "financingBalance": _decimal(value.financing_balance),
        "financingBuyAmount": _decimal(value.financing_buy_amount),
        "financingRepaymentAmount": _decimal(value.financing_repayment_amount),
        "lendingBalanceAmount": _decimal(value.lending_balance_amount),
        "lendingBalanceQty": _decimal(value.lending_balance_qty),
        "lendingSellQty": _decimal(value.lending_sell_qty),
        "lendingRepaymentQty": _decimal(value.lending_repayment_qty),
        "totalBalance": _decimal(value.total_balance),
        "currency": value.currency,
        "quantityUnit": value.quantity_unit,
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _security_content_hash(value: MarginSecurityDaily) -> str:
    """计算证券直报字段摘要，直报偿还空值和禁止的派生偿还语义不会发生混淆。"""
    payload = {
        "securityCode": value.source_security_code,
        "tradeDate": value.trade_date.isoformat(),
        "financingBalance": _decimal(value.financing_balance),
        "financingBuyAmount": _decimal(value.financing_buy_amount),
        "financingRepaymentReported": _decimal(value.financing_repayment_reported),
        "lendingBalanceQty": _decimal(value.lending_balance_qty),
        "quantityUnit": value.quantity_unit,
        "currency": value.currency,
        "nullReason": value.null_reason,
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _decimal(value: object) -> str | None:
    """把可选精确十进制稳定投影为文本，避免数据库或 JSON 浮点格式影响内容摘要。"""
    return None if value is None else str(value)


def _partition_key(venue_id: UUID) -> str:
    """生成只依赖永久场所 UUID 的 publication 分区键，不受展示名称或来源代码变化影响。"""
    return f"venue:{venue_id}"


def _security_record_key(security_id: int, trade_date: date) -> str:
    """计算证券两融逻辑键摘要，永久 security_id 使代码复用不能覆盖旧事实。"""
    return hashlib.sha256(f"{security_id}:{trade_date.isoformat()}".encode()).hexdigest()


def _current_eligibility_records(
    session: Session, *, venue: MarginVenue, methodology_version_id: UUID
) -> dict[tuple[int, date], MarginEligibilityRevision]:
    """读取场所当前资格知识，按有效起点定位同一业务事实的后续修订。"""
    rows = (
        session.execute(
            select(MarginEligibilityRevision)
            .join(
                EquityInstrument,
                EquityInstrument.security_id == MarginEligibilityRevision.security_id,
            )
            .where(
                EquityInstrument.exchange == venue.code,
                MarginEligibilityRevision.methodology_version_id == methodology_version_id,
                MarginEligibilityRevision.known_to.is_(None),
            )
        )
        .scalars()
        .all()
    )
    result = {(item.security_id, item.effective_from): item for item in rows}
    if len(result) != len(rows):
        raise ValueError("margin eligibility current revision is ambiguous")
    return result


def _eligibility_candidate(
    session: Session,
    *,
    dataset_id: UUID,
    methodology_version_id: UUID,
    normalization_run_id: UUID,
    partition_key: str,
    current: Mapping[tuple[int, date], MarginEligibilityRevision],
    incoming: Mapping[tuple[int, date], MarginEligibility],
    changed: Mapping[tuple[int, date], _PreparedEligibilityRecord],
    source_batch_id: UUID,
    now: datetime,
) -> CanonicalReleaseCandidate:
    """把当前资格知识与本次证据合成 release；无新事实时保留原始来源血缘。"""
    records: list[CanonicalLineageRecord] = []
    keys = sorted({*current, *incoming})
    for security_id, effective_from in keys:
        changed_record = changed.get((security_id, effective_from))
        if changed_record is None:
            existing = current[(security_id, effective_from)]
            content_hash = existing.content_hash
            batch_id = UUID(str(existing.source_batch_id))
        else:
            content_hash = changed_record.content_hash
            batch_id = source_batch_id
        records.append(
            CanonicalLineageRecord(
                record_key_hash=_eligibility_record_key(security_id, effective_from),
                content_hash=content_hash,
                source_batch_id=batch_id,
                transform_hash=hashlib.sha256(_ELIGIBILITY_MAPPING_VERSION.encode()).hexdigest(),
            )
        )
    effective_dates = tuple(effective_from for _security_id, effective_from in keys)
    fencing_token = session.execute(
        select(CanonicalCheckpoint.fencing_token)
        .where(
            CanonicalCheckpoint.dataset_id == dataset_id,
            CanonicalCheckpoint.partition_key == partition_key,
            CanonicalCheckpoint.checkpoint_kind == "published",
        )
        .with_for_update()
    ).scalar_one_or_none()
    latest_security_id, latest_effective_from = max(keys, key=lambda item: (item[1], item[0]))
    return CanonicalReleaseCandidate(
        dataset_id=dataset_id,
        dataset_code=_ELIGIBILITY_DATASET,
        partition_key=partition_key,
        methodology_version_id=methodology_version_id,
        normalization_run_id=normalization_run_id,
        records=tuple(records),
        quality=CanonicalQualityDecision(
            status="passed",
            policy_code="margin.eligibility.quality",
            policy_version=1,
            rules=(
                CanonicalQualityRule("explicit-evidence-only", "blocking", True),
                CanonicalQualityRule("no-list-diff-revocation", "blocking", True),
            ),
        ),
        fact_min=min(effective_dates),
        fact_max=max(effective_dates),
        checkpoint_kind="published",
        checkpoint_position={
            "effectiveFrom": latest_effective_from.isoformat(),
            "securityId": str(latest_security_id),
        },
        expected_fencing_token=0 if fencing_token is None else int(fencing_token),
        created_at=now,
    )


def _eligibility_record_key(security_id: int, effective_from: date) -> str:
    """计算资格逻辑键摘要；永久身份与有效起点决定被修订的业务事实。"""
    return hashlib.sha256(f"{security_id}:{effective_from.isoformat()}".encode()).hexdigest()


def _eligibility_content_hash(value: MarginEligibility) -> str:
    """计算资格证据摘要，状态、公告日、范围和证据类型都会触发知识修订。"""
    payload = {
        "securityCode": value.source_security_code,
        "status": value.status,
        "effectiveFrom": value.effective_from.isoformat(),
        "effectiveTo": None if value.effective_to is None else value.effective_to.isoformat(),
        "announcementOn": None
        if value.announcement_on is None
        else value.announcement_on.isoformat(),
        "evidenceBasis": value.evidence_basis,
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
