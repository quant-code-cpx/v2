"""从冻结的 ETF 产品目录 publication 解析可恢复的双市场执行全集。"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from uuid import UUID

from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session

from service_data_sync.domain.etf import EtfIdentifier
from service_data_sync.infrastructure.database.models.canonical import (
    CanonicalDataset,
    DatasetRelease,
)
from service_data_sync.infrastructure.database.models.etf import EtfProfileVersion
from service_data_sync.infrastructure.database.models.market.identity import (
    EtfListing,
    InstrumentIdentifierVersion,
    TradingVenue,
)
from service_data_sync.infrastructure.database.models.publication.dataset_publication import (
    DatasetPublication,
)

_PROFILE_DATASET = "fund.etf.profile.reported"
_PROFILE_SCHEMA_VERSION = 2
_PROFILE_PARTITIONS = {"SSE": "venue:SSE", "SZSE": "venue:SZSE"}
_IDENTIFIER_SCHEME = "venue_symbol"
_INCLUDED_LISTING_STATUSES = frozenset({"LISTED", "SUSPENDED", "UNKNOWN"})
_MONEY_MARKET_PROFILE_TYPES = frozenset({"交易型货币基金", "货币市场基金"})
_NAV_UNSUPPORTED_REASON = "NAV_SEMANTICS_UNSUPPORTED_MONEY_MARKET"


class EtfUniverseUnavailable(RuntimeError):
    """表示冻结目录 publication 缺失、不可读或不能唯一解析双市场 ETF 全集。"""

    def __init__(self, message: str, *, reason_code: str = "etf-profile-publication-unavailable"):
        """保存可跨控制面传递的稳定原因码，异常正文不对外透传。"""
        super().__init__(message)
        self.reason_code = reason_code


@dataclass(frozen=True, slots=True)
class EtfNavUnsupportedMember:
    """保存来源目录明确表明 NAV 语义不兼容的 ETF 及其稳定审计原因。"""

    identifier: EtfIdentifier
    profile_type: str
    reason_code: str = _NAV_UNSUPPORTED_REASON


@dataclass(frozen=True, slots=True)
class EtfUniverseSnapshot:
    """保存双市场目录版本、唯一 ETF 身份、NAV 支持集及确定性摘要。"""

    profile_data_versions: dict[str, UUID]
    identifiers: tuple[EtfIdentifier, ...]
    universe_hash: str
    nav_unsupported: tuple[EtfNavUnsupportedMember, ...] = ()

    @property
    def count(self) -> int:
        """返回冻结全集内不重复 ETF 上市工具数量。"""
        return len(self.identifiers)

    @property
    def nav_unsupported_count(self) -> int:
        """返回由官方 profile 类型明确冻结为 NAV 暂不支持的实体数。"""
        return len(self.nav_unsupported)

    @property
    def nav_eligible_count(self) -> int:
        """返回可以请求来源直报 UNIT/ACCUMULATED NAV 的实体数。"""
        return self.count - self.nav_unsupported_count


@dataclass(frozen=True, slots=True)
class _ProfilePublication:
    """保存一个场所 exact publication 的事实日、知识截止点和方法学。"""

    venue: str
    data_version: UUID
    effective_on: date
    knowledge_cutoff: datetime
    methodology_version_id: UUID


@dataclass(frozen=True, slots=True)
class _PublicationMembers:
    """保存单场所 publication 的全部身份和 NAV 暂不支持子集。"""

    identifiers: tuple[EtfIdentifier, ...]
    nav_unsupported: tuple[EtfNavUnsupportedMember, ...]


def resolve_current_etf_profile_data_versions(session: Session) -> dict[str, UUID]:
    """读取当前 SSE、SZSE schema v2 产品目录 publication，并要求两市同时可用。"""
    resolved: dict[str, UUID] = {}
    for venue, partition_key in _PROFILE_PARTITIONS.items():
        rows = session.execute(
            _profile_publication_statement().where(
                DatasetPublication.partition_key == partition_key,
                DatasetPublication.superseded_at.is_(None),
            )
        ).all()
        if len(rows) != 1:
            raise EtfUniverseUnavailable(
                f"current ETF profile publication for {venue} is missing or ambiguous"
            )
        resolved[venue] = UUID(str(rows[0].data_version))
    return resolved


def load_frozen_etf_universe(
    session: Session,
    *,
    profile_data_versions: Mapping[str, UUID],
) -> EtfUniverseSnapshot:
    """按 exact 双市场 dataVersion 读取每个实体唯一最新资料，明确退市者才排除。"""
    if set(profile_data_versions) != set(_PROFILE_PARTITIONS):
        raise EtfUniverseUnavailable("ETF profile data versions must contain exact SSE and SZSE")
    identifiers: list[EtfIdentifier] = []
    nav_unsupported: list[EtfNavUnsupportedMember] = []
    normalized_versions: dict[str, UUID] = {}
    for venue in ("SSE", "SZSE"):
        version = UUID(str(profile_data_versions[venue]))
        publication = _exact_profile_publication(session, venue=venue, data_version=version)
        normalized_versions[venue] = version
        members = _publication_identifiers(session, publication=publication)
        identifiers.extend(members.identifiers)
        nav_unsupported.extend(members.nav_unsupported)
    ordered = tuple(sorted(identifiers, key=lambda value: (value.venue, value.symbol)))
    if not ordered:
        raise EtfUniverseUnavailable(
            "frozen ETF profile universe has no eligible members",
            reason_code="etf-profile-universe-empty",
        )
    qualified = [value.qualified_key for value in ordered]
    if len(set(qualified)) != len(qualified):
        raise EtfUniverseUnavailable("frozen ETF profile universe contains duplicate identities")
    unsupported_by_key = {
        item.identifier.qualified_key: item.reason_code for item in nav_unsupported
    }
    evidence = [
        f"{qualified_key}|NAV:{unsupported_by_key.get(qualified_key, 'ELIGIBLE')}"
        for qualified_key in qualified
    ]
    universe_hash = hashlib.sha256("\n".join(evidence).encode()).hexdigest()
    return EtfUniverseSnapshot(
        profile_data_versions=normalized_versions,
        identifiers=ordered,
        universe_hash=universe_hash,
        nav_unsupported=tuple(
            sorted(
                nav_unsupported,
                key=lambda item: (
                    item.identifier.venue,
                    item.identifier.symbol,
                ),
            )
        ),
    )


def _profile_publication_statement() -> Select[tuple[UUID, date | None, datetime | None, UUID]]:
    """构造只允许 canonical schema v2 release 的产品目录 publication 查询。"""
    return (
        select(
            DatasetPublication.data_version,
            DatasetPublication.effective_as_of,
            DatasetPublication.knowledge_cutoff,
            DatasetRelease.methodology_version_id,
        )
        .join(DatasetRelease, DatasetRelease.release_id == DatasetPublication.release_id)
        .join(CanonicalDataset, CanonicalDataset.dataset_id == DatasetRelease.dataset_id)
        .where(
            DatasetPublication.dataset == _PROFILE_DATASET,
            DatasetPublication.quality_status.in_(("passed", "warned")),
            DatasetPublication.release_id.is_not(None),
            DatasetRelease.quality_status.in_(("passed", "warned")),
            CanonicalDataset.code == _PROFILE_DATASET,
            CanonicalDataset.schema_version == _PROFILE_SCHEMA_VERSION,
        )
    )


def _exact_profile_publication(
    session: Session,
    *,
    venue: str,
    data_version: UUID,
) -> _ProfilePublication:
    """验证 exact dataVersion 属于指定场所且具备完整时间语义。"""
    rows = session.execute(
        _profile_publication_statement().where(
            DatasetPublication.partition_key == _PROFILE_PARTITIONS[venue],
            DatasetPublication.data_version == data_version,
        )
    ).all()
    if len(rows) != 1:
        raise EtfUniverseUnavailable(
            f"frozen ETF profile publication for {venue} is missing or ambiguous"
        )
    row = rows[0]
    if row.knowledge_cutoff is None:
        raise EtfUniverseUnavailable(
            f"frozen ETF profile publication for {venue} has no knowledge cutoff"
        )
    if row.effective_as_of is None:
        raise EtfUniverseUnavailable(
            f"frozen ETF profile publication for {venue} has no effective date"
        )
    return _ProfilePublication(
        venue=venue,
        data_version=UUID(str(row.data_version)),
        effective_on=row.effective_as_of,
        knowledge_cutoff=row.knowledge_cutoff,
        methodology_version_id=UUID(str(row.methodology_version_id)),
    )


def _publication_identifiers(
    session: Session,
    *,
    publication: _ProfilePublication,
) -> _PublicationMembers:
    """按 publication 双时间选每个 ETF 最新资料，避免连续目录观察造成重复 fan-out。"""
    ranked_profiles = (
        select(
            EtfProfileVersion.etf_id.label("etf_id"),
            EtfProfileVersion.listing_status.label("listing_status"),
            EtfProfileVersion.display_name.label("display_name"),
            EtfProfileVersion.etf_type.label("etf_type"),
            func.row_number()
            .over(
                partition_by=EtfProfileVersion.etf_id,
                order_by=(
                    EtfProfileVersion.effective_from.desc(),
                    EtfProfileVersion.known_from.desc(),
                    EtfProfileVersion.profile_version_id.desc(),
                ),
            )
            .label("row_number"),
        )
        .where(
            EtfProfileVersion.methodology_version_id == publication.methodology_version_id,
            EtfProfileVersion.known_from <= publication.knowledge_cutoff,
            or_(
                EtfProfileVersion.known_to.is_(None),
                EtfProfileVersion.known_to > publication.knowledge_cutoff,
            ),
            EtfProfileVersion.effective_from <= publication.effective_on,
            or_(
                EtfProfileVersion.effective_to.is_(None),
                EtfProfileVersion.effective_to > publication.effective_on,
            ),
        )
        .subquery()
    )
    eligible_profiles = session.execute(
        select(
            ranked_profiles.c.etf_id,
            ranked_profiles.c.display_name,
            ranked_profiles.c.etf_type,
        )
        .join(EtfListing, EtfListing.instrument_id == ranked_profiles.c.etf_id)
        .join(TradingVenue, TradingVenue.venue_id == EtfListing.venue_id)
        .where(
            ranked_profiles.c.row_number == 1,
            ranked_profiles.c.listing_status.in_(_INCLUDED_LISTING_STATUSES),
            TradingVenue.code == publication.venue,
        )
    ).all()
    if not eligible_profiles:
        raise EtfUniverseUnavailable(
            f"frozen ETF profile publication for {publication.venue} has no eligible profiles",
            reason_code="etf-profile-universe-empty",
        )
    if any(
        row.display_name is None or not str(row.display_name).strip() for row in eligible_profiles
    ):
        raise EtfUniverseUnavailable(
            f"frozen ETF profile publication for {publication.venue} has incomplete profiles"
        )
    rows = session.execute(
        select(
            InstrumentIdentifierVersion.entity_id,
            InstrumentIdentifierVersion.identifier_value,
            ranked_profiles.c.etf_type,
        )
        .join(
            ranked_profiles,
            ranked_profiles.c.etf_id == InstrumentIdentifierVersion.entity_id,
        )
        .join(EtfListing, EtfListing.instrument_id == ranked_profiles.c.etf_id)
        .join(TradingVenue, TradingVenue.venue_id == EtfListing.venue_id)
        .where(
            ranked_profiles.c.row_number == 1,
            ranked_profiles.c.listing_status.in_(_INCLUDED_LISTING_STATUSES),
            TradingVenue.code == publication.venue,
            InstrumentIdentifierVersion.entity_kind == "ETF_LISTING",
            InstrumentIdentifierVersion.identifier_scheme == _IDENTIFIER_SCHEME,
            InstrumentIdentifierVersion.venue_id == TradingVenue.venue_id,
            InstrumentIdentifierVersion.known_from <= publication.knowledge_cutoff,
            or_(
                InstrumentIdentifierVersion.known_to.is_(None),
                InstrumentIdentifierVersion.known_to > publication.knowledge_cutoff,
            ),
            InstrumentIdentifierVersion.effective_from <= publication.effective_on,
            or_(
                InstrumentIdentifierVersion.effective_to.is_(None),
                InstrumentIdentifierVersion.effective_to > publication.effective_on,
            ),
        )
        .order_by(InstrumentIdentifierVersion.identifier_value)
    ).all()
    eligible_entity_ids = {UUID(str(row.etf_id)) for row in eligible_profiles}
    entity_ids = [UUID(str(row.entity_id)) for row in rows]
    if len(set(entity_ids)) != len(entity_ids) or set(entity_ids) != eligible_entity_ids:
        raise EtfUniverseUnavailable(
            f"frozen ETF profile publication for {publication.venue} has ambiguous identifiers"
        )
    try:
        identifiers = tuple(
            EtfIdentifier(venue=publication.venue, symbol=str(row.identifier_value)) for row in rows
        )
        return _PublicationMembers(
            identifiers=identifiers,
            nav_unsupported=tuple(
                EtfNavUnsupportedMember(
                    identifier=identifier,
                    profile_type=str(row.etf_type),
                )
                for identifier, row in zip(identifiers, rows, strict=True)
                if str(row.etf_type).strip() in _MONEY_MARKET_PROFILE_TYPES
            ),
        )
    except ValueError as error:
        raise EtfUniverseUnavailable(
            f"frozen ETF profile publication for {publication.venue} has invalid identifiers"
        ) from error
