"""由已发布 canonical 组件构建股票发现冻结 EOD 横截面。"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import String, and_, cast, func, insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from service_data_sync.application.ports.canonical_release import (
    CanonicalLineageRecord,
    CanonicalQualityDecision,
    CanonicalQualityRule,
    CanonicalReleaseCandidate,
)
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.database.models.canonical import (
    CanonicalCheckpoint,
    CanonicalRecordLineage,
    DatasetRelease,
    MethodologyVersion,
    NormalizationRun,
)
from service_data_sync.infrastructure.database.models.equity.workspace import (
    EquityDiscoveryAvailability,
    EquityDiscoveryMembership,
    EquityDiscoverySnapshot,
    EquityShareCapitalRevision,
    EquityTradingStatusRevision,
    SwMembershipItem,
    SwMembershipRelease,
)
from service_data_sync.infrastructure.database.models.execution.sync_run import SyncRun
from service_data_sync.infrastructure.database.models.financial.financial_methodology import (
    FinancialMethodology,
)
from service_data_sync.infrastructure.database.models.financial.financial_metric_definition import (
    FinancialMetricDefinition,
)
from service_data_sync.infrastructure.database.models.financial.financial_publication import (
    FinancialPublication,
)
from service_data_sync.infrastructure.database.models.money_flow.money_flow_methodology import (
    MoneyFlowMethodology,
)
from service_data_sync.infrastructure.database.models.money_flow.money_flow_series import (
    MoneyFlowSeries,
)
from service_data_sync.infrastructure.database.models.provenance.source_batch import SourceBatch
from service_data_sync.infrastructure.database.models.publication.dataset_publication import (
    DatasetPublication,
)
from service_data_sync.infrastructure.database.models.sector.catalog.sector_entity import (
    SectorEntity,
)
from service_data_sync.infrastructure.database.models.sector.sw import (
    SwSectorNodeRevision,
    SwSectorPublication,
)
from service_data_sync.infrastructure.persistence.canonical_release_repository import (
    SqlAlchemyCanonicalReleaseRepository,
)
from service_data_sync.infrastructure.persistence.typed_p0_support import ensure_dataset

from ..database.models.equity.identity.equity_identifier_version import (
    EquityIdentifierVersion,
)
from ..database.models.equity.identity.equity_instrument import (
    EquityInstrument,
)
from ..database.models.equity.identity.equity_listing_status_version import (
    EquityListingStatusVersion,
)
from ..database.models.equity.identity.equity_name_version import (
    EquityNameVersion,
)
from ..database.models.equity.market_data.equity_daily_bar import (
    EquityDailyBar,
)
from ..database.models.financial.valuation_observation_revision import (
    ValuationObservationRevision,
)
from ..database.models.money_flow.money_flow_bucket_definition import (
    MoneyFlowBucketDefinition,
)
from ..database.models.money_flow.money_flow_daily_observation import (
    MoneyFlowDailyObservation,
)
from ..database.models.money_flow.money_flow_methodology_version import (
    MoneyFlowMethodologyVersion,
)
from ..database.models.publication.dataset_publication_component import (
    DatasetPublicationComponent,
)
from ..database.models.sector.membership.sector_membership_item import (
    SectorMembershipItem,
)
from ..database.models.sector.membership.sector_membership_release import (
    SectorMembershipRelease,
)
from ..database.models.sector.membership.sector_membership_release_sector import (
    SectorMembershipReleaseSector,
)
from ..database.models.sector.membership.sector_membership_snapshot import (
    SectorMembershipSnapshot,
)

_DATASET = "equity.discovery.eod"
_PARTITION = "CN_A"
_MAPPING_VERSION = "equity-discovery-eod-v1"
_METHODOLOGY_CODE = "equity-discovery-eod-derived"
_DOCUMENTATION = "docs/service-web/0007-equity-market-workspace/index.html"
_MASTER_DATASET = "equity.master.cn-a"
_MASTER_PARTITION = "CN_A_STABLE"
_MASTER_CHILD_DATASET = "equity.master.catalog"
_LIFECYCLE_DATASET = "equity.lifecycle.explicit"
_DAILY_DATASET = "equity.bar.1d.raw"
_TRADING_DATASET = "equity.trading_status.1d"
_CAPITAL_DATASET = "equity.share_capital.reported"
_VALUATION_DATASET = "financial.valuation"
_MONEY_FLOW_DATASET = "money_flow.daily"
_SECTOR_MEMBERSHIP_DATASET = "sector.membership.release"
_SW_MEMBERSHIP_DATASET = "sector.sw2021.membership.snapshot"
_SW_MEMBERSHIP_AGGREGATE_DATASET = "sector.sw2021.membership.aggregate"
_SW_MEMBERSHIP_AGGREGATE_PARTITION = "SW2021"
_SW_MEMBERSHIP_AGGREGATE_MAPPING = "sw2021-membership-aggregate-v1"
_SW_TAXONOMY_DATASET = "sector.sw.taxonomy"
_SECTOR_SCHEMES = ("eastmoney.industry", "eastmoney.concept")


class DiscoveryBuildUnavailable(RuntimeError):
    """表示 BASE 横截面组件不完整，旧 publication 必须保持可见。"""

    def __init__(self, reason_code: str, detail: str) -> None:
        """保存稳定原因码和不含业务原文的排障说明。"""
        super().__init__(detail)
        self.reason_code = reason_code


@dataclass(frozen=True, slots=True)
class PublishedEquityDiscovery:
    """描述发现横截面 publication 与完整度。"""

    data_version: UUID
    completeness: str
    row_count: int


@dataclass(frozen=True, slots=True)
class _Component:
    """冻结一个输入数据集分区版本。"""

    dataset: str
    partition_key: str
    data_version: UUID

    @property
    def key(self) -> str:
        """生成聚合组件清单内不会跨数据集冲突的稳定键。"""
        return f"{self.dataset}|{self.partition_key}"


@dataclass(frozen=True, slots=True)
class _LockedReferenceComponent:
    """保存回填计划封存的 publication、release 与原始证据边界。"""

    publication: DatasetPublication
    source_batch_ids: tuple[UUID, ...]

    @property
    def component(self) -> _Component:
        """投影出派生 publication manifest 使用的稳定输入版本。"""
        return _Component(
            dataset=self.publication.dataset,
            partition_key=self.publication.partition_key,
            data_version=UUID(str(self.publication.data_version)),
        )


@dataclass(frozen=True, slots=True)
class _OptionalFamily:
    """保存某证券一个可选语义族的值、血缘和可用性。"""

    values: Mapping[str, object]
    availability: str
    reason_code: str | None
    data_version: UUID | None
    source_label: str | None
    methodology: Mapping[str, object] | None


@dataclass(frozen=True, slots=True)
class _Membership:
    """保存发现投影的一条多值分类归属。"""

    scheme: str
    code: str
    name: str
    level: str | None
    observed_on: date


@dataclass(frozen=True, slots=True)
class _Projection:
    """保存一只证券待发布主行、分类和原因化可用性。"""

    values: Mapping[str, object]
    memberships: tuple[_Membership, ...]
    availability: tuple[Mapping[str, object], ...]
    source_batch_id: UUID
    content_hash: str


@dataclass(frozen=True, slots=True)
class _IdentityProjection:
    """保存发现行身份值及标识、名称各自真实来源批次。"""

    security_id: int
    exchange: str
    symbol: str
    name: str
    identifier_source_batch_id: UUID
    name_source_batch_id: UUID


@dataclass(frozen=True, slots=True)
class _FactComponent:
    """保存一个事实来源在本次冻结输入中的真实组件版本。"""

    data_version: UUID
    source_label: str


@dataclass(frozen=True, slots=True)
class _SwNode:
    """保存冻结 taxonomy 中一个申万节点及其直接父级。"""

    code: str
    name: str
    level: int
    parent_code: str | None


class SqlAlchemyEquityDiscoveryRepository:
    """在单事务内冻结 BASE 组件、派生市值并发布无 N+1 横截面。"""

    def __init__(self, database: DatabaseClient) -> None:
        """保存事务工厂和通用 canonical release 仓储。"""
        self._database = database
        self._releases = SqlAlchemyCanonicalReleaseRepository(database)

    def build(
        self,
        *,
        as_of: date,
        reference_manifest: Sequence[Mapping[str, Any]] | None = None,
    ) -> PublishedEquityDiscovery:
        """构建指定 EOD 日期；回填调用只消费封印引用，详情族保持独立 partial。

        普通独立刷新沿用当前 publication 选择语义。股票中心历史回填传入
        `reference_manifest` 时，BASE 输入必须按清单中的精确 publication、release 与知识
        截止点重放；即使 current 指针已经推进，也绝不回退或混读新版本。行情、股本、估值和
        资金流不属于该 bundle 的 BASE，故在该模式只写原因化不可用状态，等待独立刷新链路发布。
        """
        projections: tuple[_Projection, ...] = ()
        components: tuple[_Component, ...] = ()
        completeness = "PARTIAL"
        frozen_reference = (
            None
            if reference_manifest is None
            else tuple(dict(component) for component in reference_manifest)
        )

        def prepare_candidate(session: Session) -> CanonicalReleaseCandidate:
            """锁定输入版本并准备确定性 projection 候选。"""
            nonlocal projections, components, completeness
            now = datetime.now(UTC)
            locked_reference = (
                ()
                if frozen_reference is None
                else _locked_reference_components(
                    session,
                    as_of=as_of,
                    manifest=frozen_reference,
                )
            )
            reference_components = tuple(item.component for item in locked_reference)
            master: DatasetPublication | None = None
            if frozen_reference is None:
                master, master_components = _master_components(session)
                identities = _identities(session)
            else:
                master_components = _frozen_master_components(
                    session,
                    references=locked_reference,
                )
                identities = _frozen_identities(
                    session,
                    master_components=master_components,
                )
            if not identities:
                raise DiscoveryBuildUnavailable(
                    "IDENTITY_PUBLICATION_EMPTY",
                    "CN_A_STABLE contains no confirmed equity identities",
                )
            security_ids = tuple(sorted(identities))
            if frozen_reference is None:
                lifecycle, lifecycle_components = _lifecycle_components(
                    session,
                    security_ids=security_ids,
                    as_of=as_of,
                )
                trading_publication = _single_publication(
                    session,
                    dataset=_TRADING_DATASET,
                    partition_key=f"date:{as_of.isoformat()}",
                    reason_code="TRADING_STATUS_PUBLICATION_UNAVAILABLE",
                )
                statuses = _trading_statuses(session, security_ids=security_ids, as_of=as_of)
            else:
                lifecycle, lifecycle_components = _frozen_lifecycle_components(
                    session,
                    identities=identities,
                    as_of=as_of,
                    references=locked_reference,
                )
                trading_reference = _reference_component(
                    locked_reference,
                    dataset=_TRADING_DATASET,
                    partition_key=f"date:{as_of.isoformat()}",
                )
                trading_publication = trading_reference.publication
                statuses = _frozen_trading_statuses(
                    session,
                    security_ids=security_ids,
                    as_of=as_of,
                    reference=trading_reference,
                )
            fact_components = _fact_components(
                session,
                identities=identities,
                lifecycle=lifecycle,
                master_components=master_components,
                lifecycle_components=lifecycle_components,
            )
            daily_publications = (
                {}
                if frozen_reference is not None
                else _available_security_publications(
                    session,
                    dataset=_DAILY_DATASET,
                    security_ids=security_ids,
                )
            )
            capital_publications = (
                {}
                if frozen_reference is not None
                else _available_security_publications(
                    session,
                    dataset=_CAPITAL_DATASET,
                    security_ids=security_ids,
                )
            )
            bars = (
                {}
                if frozen_reference is not None
                else _latest_bars(session, security_ids=security_ids, as_of=as_of)
            )
            capitals = (
                {}
                if frozen_reference is not None
                else _latest_capitals(session, security_ids=security_ids, as_of=as_of)
            )
            selected_market_versions = {
                UUID(str(trading_publication.data_version)),
                *daily_publications.values(),
                *capital_publications.values(),
            }
            publication_source_labels = _publication_source_labels(
                session,
                data_versions=selected_market_versions,
            )
            fact_source_batch_ids = {
                UUID(str(row["source_batch_id"])) for values in bars.values() for row in values
            }
            fact_source_batch_ids.update(
                UUID(str(row["source_batch_id"])) for row in capitals.values()
            )
            fact_source_batch_ids.update(
                UUID(str(row.source_batch_id)) for row in statuses.values()
            )
            fact_source_labels = _source_batch_labels(
                session,
                source_batch_ids=fact_source_batch_ids,
            )
            valuation, valuation_components = (
                (
                    {
                        security_id: _unavailable_family("VALUATION_SEPARATE_CURRENT_REFRESH")
                        for security_id in security_ids
                    },
                    (),
                )
                if frozen_reference is not None
                else _valuation_families(session, security_ids=security_ids, as_of=as_of)
            )
            money_flow, money_flow_components = (
                (
                    {
                        security_id: _unavailable_family("METHODOLOGY_NOT_FROZEN")
                        for security_id in security_ids
                    },
                    (),
                )
                if frozen_reference is not None
                else _money_flow_families(session, security_ids=security_ids, as_of=as_of)
            )
            if frozen_reference is None:
                memberships, membership_states, membership_components = _membership_families(
                    session,
                    security_ids=security_ids,
                    as_of=as_of,
                    release_repository=self._releases,
                )
            else:
                memberships, membership_states, membership_components = _frozen_membership_families(
                    session,
                    security_ids=security_ids,
                    as_of=as_of,
                    references=locked_reference,
                )
            components = (
                reference_components
                if frozen_reference is not None
                else _deduplicate_components(
                    (
                        _Component(
                            dataset=_MASTER_DATASET,
                            partition_key=_MASTER_PARTITION,
                            data_version=_current_master_data_version(master),
                        ),
                        *master_components,
                        *lifecycle_components,
                        _Component(
                            dataset=_TRADING_DATASET,
                            partition_key=f"date:{as_of.isoformat()}",
                            data_version=UUID(str(trading_publication.data_version)),
                        ),
                        *(
                            _Component(_DAILY_DATASET, f"security:{key}", value)
                            for key, value in daily_publications.items()
                        ),
                        *(
                            _Component(_CAPITAL_DATASET, f"security:{key}", value)
                            for key, value in capital_publications.items()
                        ),
                        *valuation_components,
                        *money_flow_components,
                        *membership_components,
                    )
                )
            )
            projections = tuple(
                _projection(
                    as_of=as_of,
                    identity=identities[security_id],
                    lifecycle=lifecycle[security_id],
                    identifier_component=fact_components[
                        (
                            identities[security_id].identifier_source_batch_id,
                            identities[security_id].exchange,
                        )
                    ],
                    name_component=fact_components[
                        (
                            identities[security_id].name_source_batch_id,
                            identities[security_id].exchange,
                        )
                    ],
                    lifecycle_component=fact_components[
                        (
                            UUID(str(lifecycle[security_id].source_batch_id)),
                            identities[security_id].exchange,
                        )
                    ],
                    bars=bars.get(security_id, ()),
                    trading_status=statuses.get(security_id),
                    capital=capitals.get(security_id),
                    trading_version=UUID(str(trading_publication.data_version)),
                    bar_version=daily_publications.get(security_id),
                    capital_version=capital_publications.get(security_id),
                    publication_source_labels=publication_source_labels,
                    fact_source_labels=fact_source_labels,
                    valuation=valuation[security_id],
                    money_flow=money_flow[security_id],
                    memberships=memberships[security_id],
                    membership_states=membership_states[security_id],
                )
                for security_id in security_ids
            )
            completeness = (
                "FULL"
                if all(
                    item["availability"]
                    not in {"SOURCE_UNAVAILABLE", "QUARANTINED", "STALE_LAST_GOOD"}
                    for projection in projections
                    for item in projection.availability
                )
                else "PARTIAL"
            )
            dataset_id = ensure_dataset(
                session,
                code=_DATASET,
                domain="equity",
                grain="publication release + security",
                now=now,
            )
            methodology_id = _ensure_discovery_methodology(session)
            input_hash = _component_hash(components)
            normalization_run_id = _ensure_derived_run(
                session,
                dataset_id=dataset_id,
                dataset_code=_DATASET,
                partition_key=_PARTITION,
                mapping_version=_MAPPING_VERSION,
                request_key_prefix="equity-discovery",
                schema_fingerprint=_sha256("equity-discovery-component-manifest-v1"),
                input_hash=input_hash,
                as_of=as_of,
                now=now,
            )
            return CanonicalReleaseCandidate(
                dataset_id=dataset_id,
                dataset_code=_DATASET,
                partition_key=_PARTITION,
                methodology_version_id=methodology_id,
                normalization_run_id=normalization_run_id,
                records=tuple(
                    CanonicalLineageRecord(
                        record_key_hash=_sha256(str(item.values["security_id"])),
                        content_hash=item.content_hash,
                        source_batch_id=item.source_batch_id,
                        transform_hash=_sha256(_MAPPING_VERSION),
                        role="input",
                    )
                    for item in projections
                ),
                quality=CanonicalQualityDecision(
                    status="passed" if completeness == "FULL" else "warned",
                    policy_code="equity.discovery.eod.quality",
                    policy_version=1,
                    rules=(
                        CanonicalQualityRule("base-component-coverage", "blocking", True),
                        CanonicalQualityRule(
                            "optional-component-coverage",
                            "info" if completeness == "FULL" else "warn",
                            completeness == "FULL",
                        ),
                    ),
                ),
                fact_min=as_of,
                fact_max=as_of,
                checkpoint_kind="trading_date",
                checkpoint_position={"tradeDate": as_of.isoformat()},
                expected_fencing_token=_checkpoint_token(
                    session, dataset_id=dataset_id, kind="trading_date"
                ),
                created_at=now,
                publication_effective_as_of=as_of,
            )

        def write_facts(
            session: Session, candidate: CanonicalReleaseCandidate, release_id: UUID
        ) -> None:
            """写入主行、分类和可用性；每一行都绑定同一个 immutable release。"""
            for projection in projections:
                session.execute(
                    insert(EquityDiscoverySnapshot).values(
                        release_id=release_id,
                        **projection.values,
                    )
                )
                for membership in projection.memberships:
                    session.execute(
                        insert(EquityDiscoveryMembership).values(
                            release_id=release_id,
                            security_id=projection.values["security_id"],
                            scheme=membership.scheme,
                            code=membership.code,
                            name=membership.name,
                            level=membership.level,
                            observed_on=membership.observed_on,
                        )
                    )
                for availability in projection.availability:
                    session.execute(
                        insert(EquityDiscoveryAvailability).values(
                            release_id=release_id,
                            security_id=projection.values["security_id"],
                            **availability,
                        )
                    )

        def write_publication(
            session: Session,
            candidate: CanonicalReleaseCandidate,
            publication_id: UUID,
            data_version: UUID,
            release_id: UUID,
        ) -> None:
            """把全部输入版本写入聚合 publication 的不可变 component manifest。"""
            del candidate, data_version, release_id
            session.execute(
                insert(DatasetPublicationComponent).values(
                    [
                        {
                            "aggregate_publication_id": publication_id,
                            "component_partition_key": item.key,
                            "component_data_version": item.data_version,
                        }
                        for item in components
                    ]
                )
            )

        published = self._releases.publish_prepared(
            prepare_candidate=prepare_candidate,
            write_facts=write_facts,
            write_publication=write_publication,
        )
        return PublishedEquityDiscovery(
            data_version=published.data_version,
            completeness=completeness,
            row_count=len(projections),
        )


def _locked_reference_components(
    session: Session,
    *,
    as_of: date,
    manifest: Sequence[Mapping[str, Any]],
) -> tuple[_LockedReferenceComponent, ...]:
    """校验回填 claim 的精确引用组件仍可读取，拒绝向 current 指针回退。

    回填控制面已在 claim 时校验 bundle、release 和 lineage；这里再次锁住同一组
    `DatasetPublication`。历史 publication 被 supersede 后仍是合法不可变输入，恢复必须按
    它的 `release` 和 `knowledge_cutoff` 重放，绝不能要求它继续担任 current 指针。
    """
    required_counts = {
        _MASTER_DATASET: 1,
        _LIFECYCLE_DATASET: 3,
        "sector.catalog.raw": 2,
        _SECTOR_MEMBERSHIP_DATASET: 2,
        _SW_TAXONOMY_DATASET: 1,
        _TRADING_DATASET: 1,
    }
    components: list[_LockedReferenceComponent] = []
    counts: dict[str, int] = defaultdict(int)
    seen: set[tuple[str, str]] = set()
    for item in manifest:
        try:
            dataset = str(item["datasetCode"])
            partition_key = str(item["partitionKey"])
            publication_id = UUID(str(item["publicationId"]))
            data_version = UUID(str(item["dataVersion"]))
            release_id = UUID(str(item["releaseId"]))
            source_batch_ids = tuple(UUID(str(value)) for value in item["sourceBatchIds"])
        except (KeyError, TypeError, ValueError) as error:
            raise DiscoveryBuildUnavailable(
                "REFERENCE_MANIFEST_INVALID",
                "equity backfill reference component has an invalid identity",
            ) from error
        key = (dataset, partition_key)
        if (
            dataset not in {*required_counts, _SW_MEMBERSHIP_DATASET}
            or key in seen
            or not source_batch_ids
        ):
            raise DiscoveryBuildUnavailable(
                "REFERENCE_MANIFEST_INVALID",
                "equity backfill reference component coverage is invalid",
            )
        seen.add(key)
        counts[dataset] += 1
        publication = session.get(DatasetPublication, publication_id, with_for_update=True)
        if (
            publication is None
            or publication.dataset != dataset
            or publication.partition_key != partition_key
            or publication.data_version != data_version
            or publication.release_id != release_id
            or publication.quality_status not in {"passed", "warned"}
        ):
            raise DiscoveryBuildUnavailable(
                "REFERENCE_COMPONENT_IMMUTABLE_IDENTITY_MISMATCH",
                "sealed reference component is no longer the exact readable publication",
            )
        source_count = int(
            session.scalar(
                select(func.count())
                .select_from(SourceBatch)
                .where(SourceBatch.source_batch_id.in_(source_batch_ids))
            )
            or 0
        )
        if source_count != len(set(source_batch_ids)):
            raise DiscoveryBuildUnavailable(
                "REFERENCE_SOURCE_EVIDENCE_UNAVAILABLE",
                "sealed reference component has incomplete source evidence",
            )
        cutoff = _reference_knowledge_cutoff(
            _LockedReferenceComponent(
                publication=publication,
                source_batch_ids=tuple(sorted(set(source_batch_ids), key=str)),
            )
        )
        _assert_fact_sources_before_cutoff(
            session,
            source_batch_ids=set(source_batch_ids),
            cutoff=cutoff,
            reason_code="REFERENCE_SOURCE_AFTER_KNOWLEDGE_CUTOFF",
        )
        components.append(
            _LockedReferenceComponent(
                publication=publication,
                source_batch_ids=tuple(sorted(set(source_batch_ids), key=str)),
            )
        )
    if (
        {dataset: counts.get(dataset, 0) for dataset in required_counts} != required_counts
        or counts.get(_SW_MEMBERSHIP_DATASET, 0) < 1
        or set(counts) != {*required_counts, _SW_MEMBERSHIP_DATASET}
    ):
        raise DiscoveryBuildUnavailable(
            "REFERENCE_COMPONENT_COVERAGE_INCOMPLETE",
            "sealed reference bundle does not contain every discovery BASE component",
        )
    trading_component = next(
        component for component in components if component.publication.dataset == _TRADING_DATASET
    )
    if trading_component.publication.partition_key != f"date:{as_of.isoformat()}":
        raise DiscoveryBuildUnavailable(
            "REFERENCE_TRADING_DATE_MISMATCH",
            "sealed trading-status component does not match discovery trade date",
        )
    return tuple(components)


def _reference_component(
    references: Sequence[_LockedReferenceComponent],
    *,
    dataset: str,
    partition_key: str,
) -> _LockedReferenceComponent:
    """从已校验的封存引用中取唯一组件，缺失或重复都拒绝继续。"""
    matches = [
        item
        for item in references
        if item.publication.dataset == dataset and item.publication.partition_key == partition_key
    ]
    if len(matches) != 1:
        raise DiscoveryBuildUnavailable(
            "REFERENCE_COMPONENT_COVERAGE_INCOMPLETE",
            "sealed reference bundle has no unique required component",
        )
    return matches[0]


def _only_reference_component(
    references: Sequence[_LockedReferenceComponent],
    *,
    dataset: str,
) -> _LockedReferenceComponent:
    """读取某数据集唯一封存组件，适用于按观测日产生单分区的 taxonomy。"""
    matches = [item for item in references if item.publication.dataset == dataset]
    if len(matches) != 1:
        raise DiscoveryBuildUnavailable(
            "REFERENCE_COMPONENT_COVERAGE_INCOMPLETE",
            "sealed reference bundle has no unique dataset component",
        )
    return matches[0]


def _reference_knowledge_cutoff(reference: _LockedReferenceComponent) -> datetime:
    """读取封存 publication 的知识边界，缺失时拒绝把 current 事实伪装成历史。"""
    cutoff = reference.publication.knowledge_cutoff
    if not isinstance(cutoff, datetime) or cutoff.tzinfo is None:
        raise DiscoveryBuildUnavailable(
            "REFERENCE_KNOWLEDGE_CUTOFF_UNAVAILABLE",
            "sealed reference publication has no timezone-aware knowledge cutoff",
        )
    return cutoff


def _current_master_data_version(master: DatasetPublication | None) -> UUID:
    """提取当前刷新已锁定的 CN_A 聚合版本，防止分支意外缺失输入。"""
    if master is None:
        raise DiscoveryBuildUnavailable(
            "IDENTITY_PUBLICATION_UNAVAILABLE",
            "current discovery build has no locked CN_A_STABLE publication",
        )
    return UUID(str(master.data_version))


def _frozen_master_components(
    session: Session,
    *,
    references: Sequence[_LockedReferenceComponent],
) -> tuple[_Component, ...]:
    """从封存 CN_A 聚合 manifest 重放三所交易所 child publication。"""
    master = _reference_component(
        references,
        dataset=_MASTER_DATASET,
        partition_key=_MASTER_PARTITION,
    )
    manifest = (
        session.execute(
            select(
                DatasetPublicationComponent.component_partition_key,
                DatasetPublicationComponent.component_data_version,
            ).where(
                DatasetPublicationComponent.aggregate_publication_id
                == master.publication.publication_id
            )
        )
        .mappings()
        .all()
    )
    expected = {"SSE", "SZSE", "BSE"}
    frozen = {
        str(row["component_partition_key"]): UUID(str(row["component_data_version"]))
        for row in manifest
    }
    if set(frozen) != expected:
        raise DiscoveryBuildUnavailable(
            "IDENTITY_COMPONENT_INCOMPLETE",
            "sealed CN_A_STABLE does not bind all exchanges",
        )
    rows = (
        session.execute(
            select(DatasetPublication)
            .where(
                DatasetPublication.dataset == _MASTER_CHILD_DATASET,
                DatasetPublication.data_version.in_(set(frozen.values())),
            )
            .with_for_update()
        )
        .scalars()
        .all()
    )
    by_exchange = {str(row.partition_key): row for row in rows}
    if (
        set(by_exchange) != expected
        or len(by_exchange) != len(rows)
        or any(
            UUID(str(by_exchange[exchange].data_version)) != frozen[exchange]
            or by_exchange[exchange].quality_status not in {"passed", "warned"}
            for exchange in expected
        )
    ):
        raise DiscoveryBuildUnavailable(
            "IDENTITY_COMPONENT_IMMUTABLE_IDENTITY_MISMATCH",
            "sealed CN_A_STABLE child publication is unavailable or changed identity",
        )
    for publication in by_exchange.values():
        _reference_knowledge_cutoff(
            _LockedReferenceComponent(publication=publication, source_batch_ids=())
        )
    return tuple(
        _Component(_MASTER_CHILD_DATASET, exchange, frozen[exchange]) for exchange in sorted(frozen)
    )


def _frozen_identities(
    session: Session,
    *,
    master_components: Sequence[_Component],
) -> dict[int, _IdentityProjection]:
    """按每所交易所 child publication 的知识截止点重放确认身份与名称。"""
    expected = {"SSE", "SZSE", "BSE"}
    rows = (
        session.execute(
            select(
                DatasetPublication.partition_key,
                DatasetPublication.data_version,
                DatasetPublication.knowledge_cutoff,
            ).where(
                DatasetPublication.dataset == _MASTER_CHILD_DATASET,
                DatasetPublication.data_version.in_(
                    {item.data_version for item in master_components}
                ),
            )
        )
        .mappings()
        .all()
    )
    cutoffs: dict[str, datetime] = {}
    versions: dict[str, UUID] = {}
    for row in rows:
        exchange = str(row["partition_key"])
        cutoff = row["knowledge_cutoff"]
        if exchange in cutoffs or not isinstance(cutoff, datetime) or cutoff.tzinfo is None:
            raise DiscoveryBuildUnavailable(
                "IDENTITY_COMPONENT_KNOWLEDGE_CUTOFF_INVALID",
                "sealed identity child publication has no unique knowledge cutoff",
            )
        cutoffs[exchange] = cutoff
        versions[exchange] = UUID(str(row["data_version"]))
    expected_versions = {item.partition_key: item.data_version for item in master_components}
    if set(cutoffs) != expected or versions != expected_versions:
        raise DiscoveryBuildUnavailable(
            "IDENTITY_COMPONENT_IMMUTABLE_IDENTITY_MISMATCH",
            "sealed identity child publications cannot be reconstructed",
        )
    identifier_rows: list[Mapping[Any, Any]] = []
    for exchange, cutoff in sorted(cutoffs.items()):
        identifier_rows.extend(
            session.execute(
                select(
                    EquityIdentifierVersion.security_id,
                    EquityIdentifierVersion.exchange,
                    EquityIdentifierVersion.symbol,
                    EquityIdentifierVersion.effective_from,
                    EquityIdentifierVersion.version_id,
                    EquityIdentifierVersion.source_batch_id,
                ).where(
                    EquityIdentifierVersion.exchange == exchange,
                    EquityIdentifierVersion.identity_state == "CONFIRMED",
                    EquityIdentifierVersion.known_from <= cutoff,
                    (EquityIdentifierVersion.known_to.is_(None))
                    | (EquityIdentifierVersion.known_to > cutoff),
                )
            )
            .mappings()
            .all()
        )
    identifiers = _latest_fact_rows(
        identifier_rows,
        reason_code="IDENTITY_VERSION_AMBIGUOUS",
    )
    if not identifiers:
        return {}
    name_rows: list[Mapping[Any, Any]] = []
    for security_id, identifier in identifiers.items():
        cutoff = cutoffs[str(identifier["exchange"])]
        name_rows.extend(
            session.execute(
                select(
                    EquityNameVersion.security_id,
                    EquityNameVersion.name,
                    EquityNameVersion.effective_from,
                    EquityNameVersion.version_id,
                    EquityNameVersion.source_batch_id,
                ).where(
                    EquityNameVersion.security_id == security_id,
                    EquityNameVersion.known_from <= cutoff,
                    (EquityNameVersion.known_to.is_(None)) | (EquityNameVersion.known_to > cutoff),
                )
            )
            .mappings()
            .all()
        )
    names = _latest_fact_rows(name_rows, reason_code="IDENTITY_NAME_VERSION_AMBIGUOUS")
    if set(names) != set(identifiers):
        raise DiscoveryBuildUnavailable(
            "IDENTITY_VERSION_INCOMPLETE",
            "sealed confirmed identities do not have complete name history",
        )
    result: dict[int, _IdentityProjection] = {}
    for security_id, identifier in identifiers.items():
        name = names[security_id]
        display_name = str(name["name"]).strip()
        if not display_name:
            raise DiscoveryBuildUnavailable(
                "IDENTITY_NAME_EMPTY",
                "sealed confirmed identity has an empty display name",
            )
        result[security_id] = _IdentityProjection(
            security_id=security_id,
            exchange=str(identifier["exchange"]),
            symbol=str(identifier["symbol"]),
            name=display_name,
            identifier_source_batch_id=UUID(str(identifier["source_batch_id"])),
            name_source_batch_id=UUID(str(name["source_batch_id"])),
        )
    return result


def _frozen_lifecycle_components(
    session: Session,
    *,
    identities: Mapping[int, _IdentityProjection],
    as_of: date,
    references: Sequence[_LockedReferenceComponent],
) -> tuple[dict[int, EquityListingStatusVersion], tuple[_Component, ...]]:
    """按三所生命周期 publication 各自知识边界重放目标日状态。"""
    expected = {"SSE", "SZSE", "BSE"}
    components: list[_Component] = []
    lifecycle: dict[int, EquityListingStatusVersion] = {}
    for exchange in sorted(expected):
        reference = _reference_component(
            references,
            dataset=_LIFECYCLE_DATASET,
            partition_key=exchange,
        )
        cutoff = _reference_knowledge_cutoff(reference)
        security_ids = tuple(
            security_id
            for security_id, identity in identities.items()
            if identity.exchange == exchange
        )
        if not security_ids:
            components.append(reference.component)
            continue
        rows = (
            session.execute(
                select(EquityListingStatusVersion).where(
                    EquityListingStatusVersion.security_id.in_(security_ids),
                    EquityListingStatusVersion.effective_from <= as_of,
                    (EquityListingStatusVersion.effective_to.is_(None))
                    | (EquityListingStatusVersion.effective_to > as_of),
                    EquityListingStatusVersion.known_from <= cutoff,
                    (EquityListingStatusVersion.known_to.is_(None))
                    | (EquityListingStatusVersion.known_to > cutoff),
                )
            )
            .scalars()
            .all()
        )
        resolved = {int(row.security_id): row for row in rows}
        if len(resolved) != len(rows) or set(resolved) != set(security_ids):
            raise DiscoveryBuildUnavailable(
                "LIFECYCLE_COVERAGE_INCOMPLETE",
                "sealed lifecycle publication cannot resolve every confirmed security",
            )
        lifecycle.update(resolved)
        components.append(reference.component)
    if set(lifecycle) != set(identities):
        raise DiscoveryBuildUnavailable(
            "LIFECYCLE_COVERAGE_INCOMPLETE",
            "sealed lifecycle publications do not cover every confirmed security",
        )
    return lifecycle, tuple(components)


def _frozen_trading_statuses(
    session: Session,
    *,
    security_ids: Sequence[int],
    as_of: date,
    reference: _LockedReferenceComponent,
) -> dict[int, EquityTradingStatusRevision]:
    """按封存交易状态 publication 的知识截止点读取停复牌事实。"""
    cutoff = _reference_knowledge_cutoff(reference)
    rows = (
        session.execute(
            select(EquityTradingStatusRevision).where(
                EquityTradingStatusRevision.security_id.in_(security_ids),
                EquityTradingStatusRevision.trade_date == as_of,
                EquityTradingStatusRevision.known_from <= cutoff,
                (EquityTradingStatusRevision.known_to.is_(None))
                | (EquityTradingStatusRevision.known_to > cutoff),
            )
        )
        .scalars()
        .all()
    )
    result = {int(row.security_id): row for row in rows}
    if len(result) != len(rows):
        raise DiscoveryBuildUnavailable(
            "TRADING_STATUS_AMBIGUOUS",
            "sealed ordinary trading status is ambiguous",
        )
    _assert_fact_sources_before_cutoff(
        session,
        source_batch_ids={UUID(str(row.source_batch_id)) for row in rows},
        cutoff=cutoff,
        reason_code="TRADING_STATUS_SOURCE_AFTER_REFERENCE_CUTOFF",
    )
    return result


def _assert_fact_sources_before_cutoff(
    session: Session,
    *,
    source_batch_ids: set[UUID],
    cutoff: datetime,
    reason_code: str,
) -> None:
    """验证历史事实的证据不晚于封存 publication 的知识边界。"""
    if not source_batch_ids:
        return
    rows = (
        session.execute(
            select(SourceBatch.source_batch_id, SourceBatch.created_at).where(
                SourceBatch.source_batch_id.in_(source_batch_ids)
            )
        )
        .mappings()
        .all()
    )
    sources = {UUID(str(row["source_batch_id"])): row["created_at"] for row in rows}
    if set(sources) != source_batch_ids or any(
        not isinstance(created_at, datetime) or created_at > cutoff
        for created_at in sources.values()
    ):
        raise DiscoveryBuildUnavailable(
            reason_code,
            "sealed reference cannot prove every fact source preceded its knowledge cutoff",
        )


def _master_components(
    session: Session,
) -> tuple[DatasetPublication, tuple[_Component, ...]]:
    """锁定 CN_A_STABLE 与沪深京三 child version，并拒绝 aggregate 落后。"""
    master = session.execute(
        select(DatasetPublication)
        .where(
            DatasetPublication.dataset == _MASTER_DATASET,
            DatasetPublication.partition_key == _MASTER_PARTITION,
            DatasetPublication.superseded_at.is_(None),
        )
        .with_for_update()
    ).scalar_one_or_none()
    if master is None:
        raise DiscoveryBuildUnavailable(
            "IDENTITY_PUBLICATION_UNAVAILABLE", "CN_A_STABLE publication is unavailable"
        )
    manifest = (
        session.execute(
            select(
                DatasetPublicationComponent.component_partition_key,
                DatasetPublicationComponent.component_data_version,
            ).where(DatasetPublicationComponent.aggregate_publication_id == master.publication_id)
        )
        .mappings()
        .all()
    )
    expected = {"SSE", "SZSE", "BSE"}
    if {str(row["component_partition_key"]) for row in manifest} != expected:
        raise DiscoveryBuildUnavailable(
            "IDENTITY_COMPONENT_INCOMPLETE", "CN_A_STABLE does not bind all exchanges"
        )
    child_rows = (
        session.execute(
            select(DatasetPublication.partition_key, DatasetPublication.data_version)
            .where(
                DatasetPublication.dataset == _MASTER_CHILD_DATASET,
                DatasetPublication.partition_key.in_(expected),
                DatasetPublication.superseded_at.is_(None),
            )
            .with_for_update()
        )
        .mappings()
        .all()
    )
    current = {str(row["partition_key"]): UUID(str(row["data_version"])) for row in child_rows}
    frozen = {
        str(row["component_partition_key"]): UUID(str(row["component_data_version"]))
        for row in manifest
    }
    if current != frozen:
        raise DiscoveryBuildUnavailable(
            "IDENTITY_AGGREGATE_STALE", "CN_A_STABLE is behind current exchange publications"
        )
    return master, tuple(
        _Component(_MASTER_CHILD_DATASET, key, value) for key, value in sorted(frozen.items())
    )


def _identities(session: Session) -> dict[int, _IdentityProjection]:
    """读取每个永久证券当前采用的确认代码、简称及各自来源批次。"""
    instruments = (
        session.execute(
            select(EquityInstrument).where(
                EquityInstrument.master_confirmed_at.is_not(None),
                EquityInstrument.listing_status != "PENDING",
            )
        )
        .scalars()
        .all()
    )
    instrument_by_id = {int(row.security_id): row for row in instruments}
    if len(instrument_by_id) != len(instruments):
        raise DiscoveryBuildUnavailable(
            "IDENTITY_AMBIGUOUS", "confirmed equity identities are ambiguous"
        )
    security_ids = tuple(instrument_by_id)
    identifier_rows = (
        session.execute(
            select(
                EquityIdentifierVersion.security_id,
                EquityIdentifierVersion.exchange,
                EquityIdentifierVersion.symbol,
                EquityIdentifierVersion.effective_from,
                EquityIdentifierVersion.version_id,
                EquityIdentifierVersion.source_batch_id,
            ).where(
                EquityIdentifierVersion.security_id.in_(security_ids),
                EquityIdentifierVersion.identity_state == "CONFIRMED",
                EquityIdentifierVersion.known_to.is_(None),
            )
        )
        .mappings()
        .all()
    )
    name_rows = (
        session.execute(
            select(
                EquityNameVersion.security_id,
                EquityNameVersion.name,
                EquityNameVersion.effective_from,
                EquityNameVersion.version_id,
                EquityNameVersion.source_batch_id,
            ).where(
                EquityNameVersion.security_id.in_(security_ids),
                EquityNameVersion.known_to.is_(None),
            )
        )
        .mappings()
        .all()
    )
    identifiers = _latest_fact_rows(identifier_rows, reason_code="IDENTITY_VERSION_AMBIGUOUS")
    names = _latest_fact_rows(name_rows, reason_code="IDENTITY_NAME_VERSION_AMBIGUOUS")
    if set(identifiers) != set(instrument_by_id) or set(names) != set(instrument_by_id):
        raise DiscoveryBuildUnavailable(
            "IDENTITY_VERSION_INCOMPLETE",
            "confirmed instruments do not have complete identifier and name history",
        )
    result: dict[int, _IdentityProjection] = {}
    for security_id, instrument in instrument_by_id.items():
        identifier = identifiers[security_id]
        name = names[security_id]
        display_name = str(name["name"]).strip()
        if (
            str(identifier["exchange"]) != instrument.exchange
            or str(identifier["symbol"]) != instrument.symbol
            or not display_name
            or display_name != instrument.name
        ):
            raise DiscoveryBuildUnavailable(
                "IDENTITY_PROJECTION_STALE",
                "instrument compatibility projection is behind bitemporal identity facts",
            )
        result[security_id] = _IdentityProjection(
            security_id=security_id,
            exchange=str(identifier["exchange"]),
            symbol=str(identifier["symbol"]),
            name=display_name,
            identifier_source_batch_id=UUID(str(identifier["source_batch_id"])),
            name_source_batch_id=UUID(str(name["source_batch_id"])),
        )
    return result


def _latest_fact_rows(
    rows: Sequence[Mapping[Any, Any]],
    *,
    reason_code: str,
) -> dict[int, Mapping[Any, Any]]:
    """按业务生效日起序选择当前知识下最新版本，同日多行则拒绝任取。"""
    grouped: dict[int, list[Mapping[Any, Any]]] = defaultdict(list)
    for row in rows:
        grouped[int(row["security_id"])].append(row)
    result: dict[int, Mapping[Any, Any]] = {}
    for security_id, values in grouped.items():
        ordered = sorted(
            values,
            key=lambda item: (
                item["effective_from"],
                str(item["version_id"]),
            ),
            reverse=True,
        )
        if len(ordered) > 1 and ordered[0]["effective_from"] == ordered[1]["effective_from"]:
            raise DiscoveryBuildUnavailable(
                reason_code,
                "multiple current-knowledge facts share the latest effective date",
            )
        result[security_id] = ordered[0]
    return result


def _lifecycle_components(
    session: Session,
    *,
    security_ids: Sequence[int],
    as_of: date,
) -> tuple[dict[int, EquityListingStatusVersion], tuple[_Component, ...]]:
    """锁定三所交易所显式生命周期发布，并读取目标日唯一有效状态。"""
    expected = {"SSE", "SZSE", "BSE"}
    rows = (
        session.execute(
            select(DatasetPublication.partition_key, DatasetPublication.data_version)
            .where(
                DatasetPublication.dataset == _LIFECYCLE_DATASET,
                DatasetPublication.partition_key.in_(expected),
                DatasetPublication.superseded_at.is_(None),
            )
            .with_for_update()
        )
        .mappings()
        .all()
    )
    versions = {str(row["partition_key"]): UUID(str(row["data_version"])) for row in rows}
    if set(versions) != expected:
        raise DiscoveryBuildUnavailable(
            "LIFECYCLE_PUBLICATION_INCOMPLETE",
            "current lifecycle publications do not cover all exchanges",
        )
    lifecycle = _lifecycle(session, security_ids=security_ids, as_of=as_of)
    if set(lifecycle) != set(security_ids):
        raise DiscoveryBuildUnavailable(
            "LIFECYCLE_COVERAGE_INCOMPLETE",
            "lifecycle facts do not cover every confirmed security",
        )
    return lifecycle, tuple(
        _Component(_LIFECYCLE_DATASET, key, value) for key, value in sorted(versions.items())
    )


def _fact_components(
    session: Session,
    *,
    identities: Mapping[int, _IdentityProjection],
    lifecycle: Mapping[int, EquityListingStatusVersion],
    master_components: Sequence[_Component],
    lifecycle_components: Sequence[_Component],
) -> dict[tuple[UUID, str], _FactComponent]:
    """按事实来源 capability 选择真实冻结组件，并校验证据早于组件知识截止点。"""
    requested: set[tuple[UUID, str]] = set()
    for security_id, identity in identities.items():
        requested.add((identity.identifier_source_batch_id, identity.exchange))
        requested.add((identity.name_source_batch_id, identity.exchange))
        requested.add(
            (
                UUID(str(lifecycle[security_id].source_batch_id)),
                identity.exchange,
            )
        )
    source_batch_ids = {source_batch_id for source_batch_id, _ in requested}
    source_rows = (
        session.execute(
            select(
                SourceBatch.source_batch_id,
                SourceBatch.capability,
                SourceBatch.created_at,
                SourceBatch.upstream_source,
            ).where(SourceBatch.source_batch_id.in_(source_batch_ids))
        )
        .mappings()
        .all()
    )
    sources = {UUID(str(row["source_batch_id"])): row for row in source_rows}
    if set(sources) != source_batch_ids:
        raise DiscoveryBuildUnavailable(
            "FACT_SOURCE_BATCH_INCOMPLETE",
            "identity or lifecycle fact has no immutable source batch",
        )
    master_by_exchange = {item.partition_key: item for item in master_components}
    lifecycle_by_exchange = {item.partition_key: item for item in lifecycle_components}
    all_components = (*master_components, *lifecycle_components)
    publication_rows = (
        session.execute(
            select(
                DatasetPublication.data_version,
                DatasetPublication.knowledge_cutoff,
            ).where(
                DatasetPublication.data_version.in_({item.data_version for item in all_components})
            )
        )
        .mappings()
        .all()
    )
    cutoffs = {UUID(str(row["data_version"])): row["knowledge_cutoff"] for row in publication_rows}
    if set(cutoffs) != {item.data_version for item in all_components}:
        raise DiscoveryBuildUnavailable(
            "FACT_COMPONENT_PUBLICATION_INCOMPLETE",
            "frozen identity or lifecycle component publication is unavailable",
        )
    result: dict[tuple[UUID, str], _FactComponent] = {}
    for source_batch_id, exchange in requested:
        source = sources[source_batch_id]
        capability = str(source["capability"])
        if capability == _MASTER_CHILD_DATASET:
            component = master_by_exchange.get(exchange)
        elif capability == _LIFECYCLE_DATASET:
            component = lifecycle_by_exchange.get(exchange)
        else:
            raise DiscoveryBuildUnavailable(
                "FACT_SOURCE_CAPABILITY_UNSUPPORTED",
                "identity or lifecycle fact source is outside frozen BASE components",
            )
        if component is None:
            raise DiscoveryBuildUnavailable(
                "FACT_SOURCE_COMPONENT_UNAVAILABLE",
                "fact exchange has no matching frozen component",
            )
        cutoff = cutoffs[component.data_version]
        created_at = source["created_at"]
        if not isinstance(cutoff, datetime) or not isinstance(created_at, datetime):
            raise DiscoveryBuildUnavailable(
                "FACT_SOURCE_KNOWLEDGE_CUTOFF_MISSING",
                "frozen component cannot prove its fact knowledge boundary",
            )
        if created_at > cutoff:
            raise DiscoveryBuildUnavailable(
                "FACT_SOURCE_AFTER_COMPONENT_CUTOFF",
                "fact source was learned after the selected frozen component",
            )
        result[(source_batch_id, exchange)] = _FactComponent(
            data_version=component.data_version,
            source_label=_source_label(source["upstream_source"]),
        )
    return result


def _available_security_publications(
    session: Session, *, dataset: str, security_ids: Sequence[int]
) -> dict[int, UUID]:
    """锁定已有的稳定 security 分区 current publication，不把覆盖缺口升级为 BASE 失败。"""
    partitions = {f"security:{security_id}" for security_id in security_ids}
    rows = (
        session.execute(
            select(DatasetPublication.partition_key, DatasetPublication.data_version)
            .where(
                DatasetPublication.dataset == dataset,
                DatasetPublication.partition_key.in_(partitions),
                DatasetPublication.superseded_at.is_(None),
            )
            .with_for_update()
        )
        .mappings()
        .all()
    )
    result = {
        int(str(row["partition_key"]).removeprefix("security:")): UUID(str(row["data_version"]))
        for row in rows
    }
    return result


def _source_batch_labels(
    session: Session,
    *,
    source_batch_ids: Iterable[UUID],
) -> dict[UUID, str]:
    """读取事实直接引用的真实上游来源，缺失或空标签时失败关闭。"""
    requested = set(source_batch_ids)
    if not requested:
        return {}
    rows = (
        session.execute(
            select(SourceBatch.source_batch_id, SourceBatch.upstream_source).where(
                SourceBatch.source_batch_id.in_(requested)
            )
        )
        .mappings()
        .all()
    )
    result = {
        UUID(str(row["source_batch_id"])): _source_label(row["upstream_source"]) for row in rows
    }
    if set(result) != requested:
        raise DiscoveryBuildUnavailable(
            "FACT_SOURCE_BATCH_INCOMPLETE",
            "market fact has no immutable source batch",
        )
    return result


def _publication_source_labels(
    session: Session,
    *,
    data_versions: Iterable[UUID],
) -> dict[UUID, str]:
    """从冻结 publication 的 release 血缘读取来源；合法空 release 回退到其规范化输入。"""
    requested = set(data_versions)
    if not requested:
        return {}
    lineage_rows = (
        session.execute(
            select(
                DatasetPublication.data_version,
                SourceBatch.upstream_source,
            )
            .join(
                DatasetRelease,
                DatasetRelease.release_id == DatasetPublication.release_id,
            )
            .join(
                CanonicalRecordLineage,
                CanonicalRecordLineage.release_id == DatasetRelease.release_id,
            )
            .join(
                SourceBatch,
                SourceBatch.source_batch_id == CanonicalRecordLineage.source_batch_id,
            )
            .where(DatasetPublication.data_version.in_(requested))
        )
        .mappings()
        .all()
    )
    fallback_rows = (
        session.execute(
            select(
                DatasetPublication.data_version,
                SourceBatch.upstream_source,
            )
            .join(
                DatasetRelease,
                DatasetRelease.release_id == DatasetPublication.release_id,
            )
            .join(
                NormalizationRun,
                NormalizationRun.normalization_run_id == DatasetRelease.normalization_run_id,
            )
            .join(SourceBatch, SourceBatch.run_id == NormalizationRun.run_id)
            .where(
                DatasetPublication.data_version.in_(requested),
                SourceBatch.created_at <= DatasetPublication.knowledge_cutoff,
            )
        )
        .mappings()
        .all()
    )
    lineage: dict[UUID, set[str]] = defaultdict(set)
    fallback: dict[UUID, set[str]] = defaultdict(set)
    for row in lineage_rows:
        lineage[UUID(str(row["data_version"]))].add(_source_label(row["upstream_source"]))
    for row in fallback_rows:
        fallback[UUID(str(row["data_version"]))].add(_source_label(row["upstream_source"]))
    result: dict[UUID, str] = {}
    for data_version in requested:
        labels = lineage.get(data_version) or fallback.get(data_version)
        if not labels:
            raise DiscoveryBuildUnavailable(
                "COMPONENT_SOURCE_LINEAGE_UNAVAILABLE",
                "frozen market component has no auditable upstream source",
            )
        result[data_version] = _combined_source_label(labels)
    return result


def _source_label(value: object) -> str:
    """校验来源标签来自不可变 source batch 且可安全写入公开可用性。"""
    label = str(value).strip()
    if not label:
        raise DiscoveryBuildUnavailable(
            "COMPONENT_SOURCE_LABEL_MISSING",
            "source batch has no upstream source label",
        )
    if len(label) > 120:
        raise DiscoveryBuildUnavailable(
            "COMPONENT_SOURCE_LABEL_TOO_LONG",
            "source label exceeds discovery availability storage budget",
        )
    return label


def _combined_source_label(values: Iterable[str]) -> str:
    """以稳定顺序组合一个派生值实际使用的全部来源标签。"""
    labels = sorted({_source_label(value) for value in values})
    if not labels:
        raise DiscoveryBuildUnavailable(
            "COMPONENT_SOURCE_LINEAGE_UNAVAILABLE",
            "derived family has no auditable upstream source",
        )
    return _source_label(" | ".join(labels))


def _single_publication(
    session: Session, *, dataset: str, partition_key: str, reason_code: str
) -> DatasetPublication:
    """锁定一个精确分区 current publication。"""
    publication = session.execute(
        select(DatasetPublication)
        .where(
            DatasetPublication.dataset == dataset,
            DatasetPublication.partition_key == partition_key,
            DatasetPublication.superseded_at.is_(None),
        )
        .with_for_update()
    ).scalar_one_or_none()
    if publication is None:
        raise DiscoveryBuildUnavailable(
            reason_code, f"{dataset}/{partition_key} publication is unavailable"
        )
    return publication


def _latest_bars(
    session: Session, *, security_ids: Sequence[int], as_of: date
) -> dict[int, tuple[Mapping[Any, Any], ...]]:
    """以窗口函数读取每只证券截至目标日最后两条最终未复权日线。"""
    ranked = (
        select(
            EquityDailyBar.security_id,
            EquityDailyBar.trade_date,
            EquityDailyBar.close_price,
            EquityDailyBar.volume_shares,
            EquityDailyBar.amount_cny,
            EquityDailyBar.turnover_rate,
            EquityDailyBar.is_final,
            EquityDailyBar.source_batch_id,
            func.row_number()
            .over(
                partition_by=EquityDailyBar.security_id,
                order_by=EquityDailyBar.trade_date.desc(),
            )
            .label("position"),
        )
        .where(
            EquityDailyBar.security_id.in_(security_ids),
            EquityDailyBar.trade_date <= as_of,
            EquityDailyBar.valid_to.is_(None),
            EquityDailyBar.is_final.is_(True),
        )
        .subquery()
    )
    rows = session.execute(select(ranked).where(ranked.c.position <= 2)).mappings().all()
    grouped: dict[int, list[Mapping[Any, Any]]] = defaultdict(list)
    for row in rows:
        grouped[int(row["security_id"])].append(row)
    return {
        key: tuple(sorted(value, key=lambda item: int(item["position"])))
        for key, value in grouped.items()
    }


def _latest_capitals(
    session: Session, *, security_ids: Sequence[int], as_of: date
) -> dict[int, Mapping[Any, Any]]:
    """读取每只证券目标日已生效的最新当前股本结构。"""
    ranked = (
        select(
            EquityShareCapitalRevision.security_id,
            EquityShareCapitalRevision.effective_on,
            EquityShareCapitalRevision.total_shares,
            EquityShareCapitalRevision.listed_tradable_a_shares,
            EquityShareCapitalRevision.source_batch_id,
            func.row_number()
            .over(
                partition_by=EquityShareCapitalRevision.security_id,
                order_by=EquityShareCapitalRevision.effective_on.desc(),
            )
            .label("position"),
        )
        .where(
            EquityShareCapitalRevision.security_id.in_(security_ids),
            EquityShareCapitalRevision.effective_on <= as_of,
            EquityShareCapitalRevision.known_to.is_(None),
        )
        .subquery()
    )
    return {
        int(row["security_id"]): row
        for row in session.execute(select(ranked).where(ranked.c.position == 1)).mappings().all()
    }


def _lifecycle(
    session: Session, *, security_ids: Sequence[int], as_of: date
) -> dict[int, EquityListingStatusVersion]:
    """读取目标日和当前知识下唯一生命周期版本。"""
    rows = (
        session.execute(
            select(EquityListingStatusVersion).where(
                EquityListingStatusVersion.security_id.in_(security_ids),
                EquityListingStatusVersion.effective_from <= as_of,
                (EquityListingStatusVersion.effective_to.is_(None))
                | (EquityListingStatusVersion.effective_to > as_of),
                EquityListingStatusVersion.known_to.is_(None),
            )
        )
        .scalars()
        .all()
    )
    result = {int(row.security_id): row for row in rows}
    if len(result) != len(rows):
        raise DiscoveryBuildUnavailable(
            "LIFECYCLE_AMBIGUOUS", "equity lifecycle is ambiguous at discovery date"
        )
    return result


def _trading_statuses(
    session: Session, *, security_ids: Sequence[int], as_of: date
) -> dict[int, EquityTradingStatusRevision]:
    """读取目标日普通停牌 current revision；响应缺席由 publication 证明而非推断。"""
    rows = (
        session.execute(
            select(EquityTradingStatusRevision).where(
                EquityTradingStatusRevision.security_id.in_(security_ids),
                EquityTradingStatusRevision.trade_date == as_of,
                EquityTradingStatusRevision.known_to.is_(None),
            )
        )
        .scalars()
        .all()
    )
    result = {int(row.security_id): row for row in rows}
    if len(result) != len(rows):
        raise DiscoveryBuildUnavailable(
            "TRADING_STATUS_AMBIGUOUS", "ordinary trading status is ambiguous"
        )
    return result


def _valuation_families(
    session: Session, *, security_ids: Sequence[int], as_of: date
) -> tuple[dict[int, _OptionalFamily], tuple[_Component, ...]]:
    """读取同一观察日齐备的三项估值；异步到达指标不得拼成伪造横截面。"""
    eligible = (
        select(
            FinancialPublication.security_id,
            FinancialPublication.data_version,
            FinancialMethodology.code.label("methodology_code"),
            FinancialMethodology.version.label("methodology_version"),
            FinancialMetricDefinition.code.label("metric_code"),
            ValuationObservationRevision.observation_date,
            ValuationObservationRevision.value,
        )
        .join(
            DatasetPublication,
            DatasetPublication.data_version == FinancialPublication.data_version,
        )
        .join(
            FinancialMethodology,
            FinancialMethodology.methodology_id == FinancialPublication.methodology_id,
        )
        .join(
            ValuationObservationRevision,
            and_(
                ValuationObservationRevision.security_id == FinancialPublication.security_id,
                ValuationObservationRevision.methodology_id == FinancialPublication.methodology_id,
            ),
        )
        .join(
            FinancialMetricDefinition,
            FinancialMetricDefinition.metric_id == ValuationObservationRevision.metric_id,
        )
        .where(
            DatasetPublication.dataset == _VALUATION_DATASET,
            DatasetPublication.superseded_at.is_(None),
            FinancialPublication.capability == _VALUATION_DATASET,
            FinancialPublication.security_id.in_(security_ids),
            FinancialMethodology.code == "akshare.eastmoney.financial-valuation",
            FinancialMethodology.status == "validated",
            ValuationObservationRevision.observation_date <= as_of,
            ValuationObservationRevision.known_to.is_(None),
            ValuationObservationRevision.quality_status.in_(("passed", "warned")),
            FinancialMetricDefinition.code.in_(("pe_ttm", "pb", "ps_ttm")),
        )
        .subquery()
    )
    complete_dates = (
        select(
            eligible.c.security_id,
            eligible.c.observation_date,
        )
        .group_by(eligible.c.security_id, eligible.c.observation_date)
        .having(func.count(func.distinct(eligible.c.metric_code)) == 3)
        .subquery()
    )
    latest_dates = (
        select(
            complete_dates.c.security_id,
            func.max(complete_dates.c.observation_date).label("observation_date"),
        )
        .group_by(complete_dates.c.security_id)
        .subquery()
    )
    rows = (
        session.execute(
            select(eligible).join(
                latest_dates,
                and_(
                    latest_dates.c.security_id == eligible.c.security_id,
                    latest_dates.c.observation_date == eligible.c.observation_date,
                ),
            )
        )
        .mappings()
        .all()
    )
    observed_security_ids = {
        int(value) for value in session.execute(select(eligible.c.security_id).distinct()).scalars()
    }
    grouped: dict[int, list[Mapping[Any, Any]]] = defaultdict(list)
    for row in rows:
        grouped[int(row["security_id"])].append(row)
    publication_sources = _publication_source_labels(
        session,
        data_versions={UUID(str(row["data_version"])) for row in rows},
    )
    result: dict[int, _OptionalFamily] = {}
    components: list[_Component] = []
    for security_id in security_ids:
        values = grouped.get(security_id, [])
        if not values:
            result[security_id] = _unavailable_family(
                "VALUATION_COMMON_DATE_INCOMPLETE"
                if security_id in observed_security_ids
                else "VALUATION_PUBLICATION_UNAVAILABLE"
            )
            continue
        versions = {UUID(str(item["data_version"])) for item in values}
        if len(versions) != 1:
            raise DiscoveryBuildUnavailable(
                "VALUATION_METHODOLOGY_AMBIGUOUS",
                "valuation publication selects multiple versions for one security",
            )
        data_version = versions.pop()
        by_code = {str(item["metric_code"]): item for item in values}
        if set(by_code) != {"pe_ttm", "pb", "ps_ttm"} or len(values) != 3:
            raise DiscoveryBuildUnavailable(
                "VALUATION_COMMON_DATE_AMBIGUOUS",
                "valuation common date has duplicate or incomplete metric facts",
            )
        observation_dates = {item["observation_date"] for item in values}
        if len(observation_dates) != 1:
            raise DiscoveryBuildUnavailable(
                "VALUATION_COMMON_DATE_AMBIGUOUS",
                "valuation projection selected more than one observation date",
            )
        observed = observation_dates.pop()
        first = values[0]
        source_label = publication_sources[data_version]
        result[security_id] = _OptionalFamily(
            values={
                "valuation_date": observed,
                "pe_ttm": _value(by_code.get("pe_ttm")),
                "pb": _value(by_code.get("pb")),
                "ps_ttm": _value(by_code.get("ps_ttm")),
                "valuation_source_label": source_label,
                "valuation_methodology_code": str(first["methodology_code"]),
                "valuation_methodology_version": str(first["methodology_version"]),
            },
            availability="DATA",
            reason_code=None,
            data_version=data_version,
            source_label=source_label,
            methodology={
                "code": str(first["methodology_code"]),
                "version": str(first["methodology_version"]),
            },
        )
        components.append(_Component(_VALUATION_DATASET, f"security:{security_id}", data_version))
    return result, _deduplicate_components(components)


def _money_flow_families(
    session: Session, *, security_ids: Sequence[int], as_of: date
) -> tuple[dict[int, _OptionalFamily], tuple[_Component, ...]]:
    """只读取单位、币种、方法学和来源血缘均已冻结的主力日序列。"""
    ranked = (
        select(
            MoneyFlowSeries.security_id,
            MoneyFlowSeries.series_id,
            DatasetPublication.data_version,
            MoneyFlowMethodology.public_key,
            MoneyFlowMethodologyVersion.version.label("methodology_version"),
            MoneyFlowMethodologyVersion.upstream_source,
            MoneyFlowDailyObservation.trade_date,
            MoneyFlowDailyObservation.net_amount,
            MoneyFlowDailyObservation.net_ratio,
            func.row_number()
            .over(
                partition_by=MoneyFlowSeries.security_id,
                order_by=MoneyFlowDailyObservation.trade_date.desc(),
            )
            .label("position"),
        )
        .join(
            MoneyFlowMethodologyVersion,
            MoneyFlowMethodologyVersion.version_id == MoneyFlowSeries.methodology_version_id,
        )
        .join(
            MoneyFlowMethodology,
            MoneyFlowMethodology.methodology_id == MoneyFlowMethodologyVersion.methodology_id,
        )
        .join(
            MoneyFlowBucketDefinition,
            MoneyFlowBucketDefinition.bucket_id == MoneyFlowSeries.bucket_id,
        )
        .join(
            DatasetPublication,
            and_(
                DatasetPublication.dataset == _MONEY_FLOW_DATASET,
                DatasetPublication.partition_key
                == func.concat("series:", cast(MoneyFlowSeries.series_id, String)),
                DatasetPublication.superseded_at.is_(None),
            ),
        )
        .join(
            MoneyFlowDailyObservation,
            MoneyFlowDailyObservation.series_id == MoneyFlowSeries.series_id,
        )
        .where(
            MoneyFlowSeries.scope_type == "equity",
            MoneyFlowSeries.security_id.in_(security_ids),
            MoneyFlowSeries.retired_at.is_(None),
            MoneyFlowBucketDefinition.bucket_code == "main",
            MoneyFlowMethodology.public_key == "eastmoney-order-size",
            MoneyFlowMethodologyVersion.status == "validated",
            MoneyFlowMethodologyVersion.production_enabled.is_(True),
            MoneyFlowMethodologyVersion.currency == "CNY",
            MoneyFlowMethodologyVersion.standard_amount_unit == "CNY",
            MoneyFlowDailyObservation.trade_date <= as_of,
            MoneyFlowDailyObservation.known_to.is_(None),
            MoneyFlowDailyObservation.quality_status.in_(("passed", "warned")),
        )
        .subquery()
    )
    rows = session.execute(select(ranked).where(ranked.c.position == 1)).mappings().all()
    grouped: dict[int, list[Mapping[Any, Any]]] = defaultdict(list)
    for row in rows:
        grouped[int(row["security_id"])].append(row)
    publication_sources = _publication_source_labels(
        session,
        data_versions={UUID(str(row["data_version"])) for row in rows},
    )
    result: dict[int, _OptionalFamily] = {}
    components: list[_Component] = []
    for security_id in security_ids:
        values = grouped.get(security_id, [])
        if not values:
            result[security_id] = _unavailable_family("METHODOLOGY_NOT_FROZEN")
            continue
        if len(values) != 1:
            raise DiscoveryBuildUnavailable(
                "MONEY_FLOW_METHODOLOGY_AMBIGUOUS",
                "money flow selects multiple production series for one security",
            )
        value = values[0]
        data_version = UUID(str(value["data_version"]))
        source_label = publication_sources[data_version]
        result[security_id] = _OptionalFamily(
            values={
                "money_flow_date": value["trade_date"],
                "money_flow_net_amount_cny": value["net_amount"],
                "money_flow_net_ratio": value["net_ratio"],
                "money_flow_source_label": source_label,
                "money_flow_methodology_code": str(value["public_key"]),
                "money_flow_methodology_version": str(value["methodology_version"]),
            },
            availability="DATA",
            reason_code=None,
            data_version=data_version,
            source_label=source_label,
            methodology={
                "code": str(value["public_key"]),
                "version": str(value["methodology_version"]),
            },
        )
        components.append(
            _Component(
                _MONEY_FLOW_DATASET,
                f"series:{value['series_id']}",
                data_version,
            )
        )
    return result, _deduplicate_components(components)


def _membership_families(
    session: Session,
    *,
    security_ids: Sequence[int],
    as_of: date,
    release_repository: SqlAlchemyCanonicalReleaseRepository,
) -> tuple[
    dict[int, tuple[_Membership, ...]],
    dict[int, dict[str, _OptionalFamily]],
    tuple[_Component, ...],
]:
    """读取当前行业、概念和完整申万节点组件；缺口仅降低 completeness。"""
    memberships: dict[int, list[_Membership]] = {security_id: [] for security_id in security_ids}
    states: dict[int, dict[str, _OptionalFamily]] = {
        security_id: {} for security_id in security_ids
    }
    components: list[_Component] = []
    for scheme in _SECTOR_SCHEMES:
        release = session.execute(
            select(SectorMembershipRelease).where(
                SectorMembershipRelease.scheme == scheme,
                SectorMembershipRelease.superseded_at.is_(None),
            )
        ).scalar_one_or_none()
        family = "industry" if scheme.endswith("industry") else "concept"
        if release is None:
            for security_id in security_ids:
                states[security_id][family] = _unavailable_family(
                    f"{family.upper()}_MEMBERSHIP_PUBLICATION_UNAVAILABLE"
                )
            continue
        source_label = _sector_membership_source_label(
            session,
            release_id=UUID(str(release.release_id)),
        )
        rows = (
            session.execute(
                select(
                    SectorMembershipItem.security_id,
                    SectorEntity.sector_code,
                    SectorEntity.name,
                    SectorMembershipItem.snapshot_date,
                )
                .join(
                    SectorMembershipReleaseSector,
                    SectorMembershipReleaseSector.snapshot_id == SectorMembershipItem.snapshot_id,
                )
                .join(
                    SectorEntity,
                    SectorEntity.sector_key == SectorMembershipReleaseSector.sector_key,
                )
                .where(
                    SectorMembershipReleaseSector.release_id == release.release_id,
                    SectorMembershipItem.security_id.in_(security_ids),
                )
            )
            .mappings()
            .all()
        )
        grouped: dict[int, int] = defaultdict(int)
        for row in rows:
            security_id = int(row["security_id"])
            grouped[security_id] += 1
            name = row["name"]
            if name is None:
                raise DiscoveryBuildUnavailable(
                    "SECTOR_MEMBERSHIP_NAME_MISSING",
                    "published sector membership has no sector name",
                )
            memberships[security_id].append(
                _Membership(
                    scheme=scheme,
                    code=str(row["sector_code"]),
                    name=str(name),
                    level=None,
                    observed_on=row["snapshot_date"],
                )
            )
        for security_id in security_ids:
            states[security_id][family] = _OptionalFamily(
                values={},
                availability="DATA" if grouped[security_id] else "LEGITIMATE_EMPTY",
                reason_code=None if grouped[security_id] else "NO_REPORTED_MEMBERSHIP",
                data_version=UUID(str(release.data_version)),
                source_label=source_label,
                methodology={"code": scheme, "version": "1"},
            )
        components.append(
            _Component(
                _SECTOR_MEMBERSHIP_DATASET,
                scheme,
                UUID(str(release.data_version)),
            )
        )
    _append_sw_memberships(
        session,
        security_ids=security_ids,
        as_of=as_of,
        memberships=memberships,
        states=states,
        components=components,
        release_repository=release_repository,
    )
    return (
        {
            key: tuple(sorted(value, key=lambda item: (item.scheme, item.code)))
            for key, value in memberships.items()
        },
        states,
        _deduplicate_components(components),
    )


def _frozen_membership_families(
    session: Session,
    *,
    security_ids: Sequence[int],
    as_of: date,
    references: Sequence[_LockedReferenceComponent],
) -> tuple[
    dict[int, tuple[_Membership, ...]],
    dict[int, dict[str, _OptionalFamily]],
    tuple[_Component, ...],
]:
    """按封存板块、申万 publication 重放分类；目录名称无历史证据时明确降级。"""
    memberships: dict[int, list[_Membership]] = {security_id: [] for security_id in security_ids}
    states: dict[int, dict[str, _OptionalFamily]] = {
        security_id: {} for security_id in security_ids
    }
    for scheme in _SECTOR_SCHEMES:
        family = "industry" if scheme.endswith("industry") else "concept"
        release_reference = _reference_component(
            references,
            dataset=_SECTOR_MEMBERSHIP_DATASET,
            partition_key=scheme,
        )
        catalog_reference = _reference_component(
            references,
            dataset="sector.catalog.raw",
            partition_key=scheme,
        )
        release = session.execute(
            select(SectorMembershipRelease).where(
                SectorMembershipRelease.release_id == release_reference.publication.release_id,
                SectorMembershipRelease.data_version == release_reference.publication.data_version,
                SectorMembershipRelease.scheme == scheme,
            )
        ).scalar_one_or_none()
        if release is None:
            _mark_family_unavailable(
                states,
                security_ids,
                family,
                "FROZEN_SECTOR_MEMBERSHIP_RELEASE_UNAVAILABLE",
            )
            continue
        source_label = _sector_membership_source_label(
            session,
            release_id=UUID(str(release.release_id)),
        )
        rows = (
            session.execute(
                select(
                    SectorMembershipItem.security_id,
                    SectorEntity.sector_code,
                    SectorEntity.name,
                    SectorEntity.updated_at,
                    SectorMembershipItem.snapshot_date,
                )
                .join(
                    SectorMembershipReleaseSector,
                    SectorMembershipReleaseSector.snapshot_id == SectorMembershipItem.snapshot_id,
                )
                .join(
                    SectorEntity,
                    SectorEntity.sector_key == SectorMembershipReleaseSector.sector_key,
                )
                .where(
                    SectorMembershipReleaseSector.release_id == release.release_id,
                    SectorMembershipItem.security_id.in_(security_ids),
                )
            )
            .mappings()
            .all()
        )
        catalog_cutoff = _reference_knowledge_cutoff(catalog_reference)
        # `SectorEntity` 仅保留当前显示名；若它在封存目录之后变更，不能把新名泄漏到旧计划。
        if any(
            row["name"] is None
            or not isinstance(row["updated_at"], datetime)
            or row["updated_at"] > catalog_cutoff
            for row in rows
        ):
            _mark_family_unavailable(
                states,
                security_ids,
                family,
                "FROZEN_SECTOR_CATALOG_NAME_HISTORY_UNAVAILABLE",
            )
            continue
        grouped: dict[int, int] = defaultdict(int)
        for row in rows:
            security_id = int(row["security_id"])
            grouped[security_id] += 1
            memberships[security_id].append(
                _Membership(
                    scheme=scheme,
                    code=str(row["sector_code"]),
                    name=str(row["name"]),
                    level=None,
                    observed_on=row["snapshot_date"],
                )
            )
        for security_id in security_ids:
            states[security_id][family] = _OptionalFamily(
                values={},
                availability="DATA" if grouped[security_id] else "LEGITIMATE_EMPTY",
                reason_code=None if grouped[security_id] else "NO_REPORTED_MEMBERSHIP",
                data_version=UUID(str(release.data_version)),
                source_label=source_label,
                methodology={"code": scheme, "version": "1"},
            )
    _append_frozen_sw_memberships(
        session,
        security_ids=security_ids,
        as_of=as_of,
        memberships=memberships,
        states=states,
        references=references,
    )
    return (
        {
            key: tuple(sorted(value, key=lambda item: (item.scheme, item.code)))
            for key, value in memberships.items()
        },
        states,
        (),
    )


def _mark_family_unavailable(
    states: dict[int, dict[str, _OptionalFamily]],
    security_ids: Sequence[int],
    family: str,
    reason_code: str,
) -> None:
    """为整个分类语义族写入可审计不可用状态，而不是改读 current 版本。"""
    for security_id in security_ids:
        states[security_id][family] = _unavailable_family(reason_code)


def _append_frozen_sw_memberships(
    session: Session,
    *,
    security_ids: Sequence[int],
    as_of: date,
    memberships: dict[int, list[_Membership]],
    states: dict[int, dict[str, _OptionalFamily]],
    references: Sequence[_LockedReferenceComponent],
) -> None:
    """按封存 taxonomy 与每个三级节点 publication 重放申万完整归属。"""
    taxonomy_reference = _only_reference_component(
        references,
        dataset=_SW_TAXONOMY_DATASET,
    )
    taxonomy = session.execute(
        select(SwSectorPublication).where(
            SwSectorPublication.data_version == taxonomy_reference.publication.data_version,
            SwSectorPublication.capability == _SW_TAXONOMY_DATASET,
        )
    ).scalar_one_or_none()
    if taxonomy is None:
        _mark_sw_unavailable(states, security_ids, "FROZEN_SW_TAXONOMY_UNAVAILABLE")
        return
    cutoff = _reference_knowledge_cutoff(taxonomy_reference)
    node_rows = (
        session.execute(
            select(
                SwSectorNodeRevision.sector_code,
                SwSectorNodeRevision.name,
                SwSectorNodeRevision.level,
                SwSectorNodeRevision.parent_code,
            ).where(
                SwSectorNodeRevision.snapshot_date == taxonomy.snapshot_date,
                SwSectorNodeRevision.methodology_id == taxonomy.methodology_id,
                SwSectorNodeRevision.known_from <= cutoff,
                (SwSectorNodeRevision.known_to.is_(None))
                | (SwSectorNodeRevision.known_to > cutoff),
                SwSectorNodeRevision.quality_status.in_(("passed", "warned")),
            )
        )
        .mappings()
        .all()
    )
    nodes = {
        _sw_code(row["sector_code"]): _SwNode(
            code=_sw_code(row["sector_code"]),
            name=str(row["name"]),
            level=int(row["level"]),
            parent_code=(None if row["parent_code"] is None else _sw_code(row["parent_code"])),
        )
        for row in node_rows
    }
    if len(nodes) != len(node_rows):
        raise DiscoveryBuildUnavailable(
            "SW_TAXONOMY_AMBIGUOUS",
            "sealed taxonomy contains duplicate normalized node codes",
        )
    third_level_nodes = {code: node for code, node in nodes.items() if node.level == 3}
    by_node: dict[str, tuple[_LockedReferenceComponent, SwMembershipRelease, str]] = {}
    for reference in references:
        if reference.publication.dataset != _SW_MEMBERSHIP_DATASET:
            continue
        release = session.execute(
            select(SwMembershipRelease).where(
                SwMembershipRelease.release_id == reference.publication.release_id,
                SwMembershipRelease.source_batch_id.in_(reference.source_batch_ids),
                SwMembershipRelease.observation_date <= as_of,
            )
        ).scalar_one_or_none()
        if release is None:
            _mark_sw_unavailable(
                states,
                security_ids,
                "FROZEN_SW_MEMBERSHIP_RELEASE_UNAVAILABLE",
            )
            return
        node_code = _sw_code(release.node_code)
        if node_code in by_node:
            raise DiscoveryBuildUnavailable(
                "SW_MEMBERSHIP_AMBIGUOUS",
                "sealed reference has multiple membership releases for one third-level node",
            )
        source = _source_batch_labels(
            session,
            source_batch_ids={UUID(str(release.source_batch_id))},
        )[UUID(str(release.source_batch_id))]
        by_node[node_code] = (reference, release, source)
    if not third_level_nodes or set(by_node) != set(third_level_nodes):
        _mark_sw_unavailable(states, security_ids, "SW_MEMBERSHIP_COVERAGE_INCOMPLETE")
        return
    release_ids = [release.release_id for _reference, release, _source in by_node.values()]
    rows = (
        session.execute(
            select(
                SwMembershipItem.security_id,
                SwMembershipItem.third_level_node_code,
                SwMembershipRelease.observation_date,
            )
            .join(
                SwMembershipRelease,
                SwMembershipRelease.release_id == SwMembershipItem.release_id,
            )
            .where(
                SwMembershipItem.release_id.in_(release_ids),
                SwMembershipItem.security_id.in_(security_ids),
            )
        )
        .mappings()
        .all()
    )
    grouped: dict[int, list[Mapping[Any, Any]]] = defaultdict(list)
    for row in rows:
        grouped[int(row["security_id"])].append(row)
    if any(len(value) > 1 for value in grouped.values()):
        raise DiscoveryBuildUnavailable(
            "SW_MEMBERSHIP_AMBIGUOUS",
            "one security belongs to multiple sealed SW third-level nodes",
        )
    combined_source = _combined_source_label(
        source for _reference, _release, source in by_node.values()
    )
    for security_id in security_ids:
        values = grouped.get(security_id, [])
        if values:
            value = values[0]
            code = _sw_code(value["third_level_node_code"])
            path = _sw_path(nodes, third_level_code=code)
            memberships[security_id].extend(
                _Membership(
                    scheme="sw.industry",
                    code=node.code,
                    name=node.name,
                    level=str(node.level),
                    observed_on=value["observation_date"],
                )
                for node in path
            )
            reference, _release, source_label = by_node[code]
            data_version = UUID(str(reference.publication.data_version))
        else:
            source_label = combined_source
            data_version = None
        states[security_id]["sw"] = _OptionalFamily(
            values={},
            availability="DATA" if values else "LEGITIMATE_EMPTY",
            reason_code=None if values else "NO_REPORTED_MEMBERSHIP",
            data_version=data_version,
            source_label=source_label,
            methodology={"code": "SW2021", "version": "1"},
        )


def _sector_membership_source_label(session: Session, *, release_id: UUID) -> str:
    """从板块 release 冻结的全部快照读取真实来源，禁止按分类体系猜供应商。"""
    labels = (
        session.execute(
            select(SourceBatch.upstream_source)
            .join(
                SectorMembershipSnapshot,
                SectorMembershipSnapshot.source_batch_id == SourceBatch.source_batch_id,
            )
            .join(
                SectorMembershipReleaseSector,
                SectorMembershipReleaseSector.snapshot_id == SectorMembershipSnapshot.snapshot_id,
            )
            .where(SectorMembershipReleaseSector.release_id == release_id)
            .distinct()
        )
        .scalars()
        .all()
    )
    return _combined_source_label(str(value) for value in labels)


def _append_sw_memberships(
    session: Session,
    *,
    security_ids: Sequence[int],
    as_of: date,
    memberships: dict[int, list[_Membership]],
    states: dict[int, dict[str, _OptionalFamily]],
    components: list[_Component],
    release_repository: SqlAlchemyCanonicalReleaseRepository,
) -> None:
    """冻结完整申万组件，并沿 taxonomy 父链物化每只证券真实的三级路径。"""
    taxonomy_row = session.execute(
        select(DatasetPublication, SwSectorPublication)
        .join(
            SwSectorPublication,
            DatasetPublication.data_version == SwSectorPublication.data_version,
        )
        .where(
            DatasetPublication.dataset == _SW_TAXONOMY_DATASET,
            DatasetPublication.superseded_at.is_(None),
            SwSectorPublication.capability == _SW_TAXONOMY_DATASET,
        )
        .order_by(
            SwSectorPublication.snapshot_date.desc(),
            SwSectorPublication.published_at.desc(),
        )
        .limit(1)
    ).one_or_none()
    if taxonomy_row is None:
        _mark_sw_unavailable(states, security_ids, "SW_TAXONOMY_PUBLICATION_UNAVAILABLE")
        return
    taxonomy_publication, taxonomy = taxonomy_row
    node_rows = (
        session.execute(
            select(
                SwSectorNodeRevision.sector_code,
                SwSectorNodeRevision.name,
                SwSectorNodeRevision.level,
                SwSectorNodeRevision.parent_code,
            ).where(
                SwSectorNodeRevision.snapshot_date == taxonomy.snapshot_date,
                SwSectorNodeRevision.methodology_id == taxonomy.methodology_id,
                SwSectorNodeRevision.known_to.is_(None),
                SwSectorNodeRevision.quality_status.in_(("passed", "warned")),
            )
        )
        .mappings()
        .all()
    )
    nodes = {
        _sw_code(row["sector_code"]): _SwNode(
            code=_sw_code(row["sector_code"]),
            name=str(row["name"]),
            level=int(row["level"]),
            parent_code=(None if row["parent_code"] is None else _sw_code(row["parent_code"])),
        )
        for row in node_rows
    }
    if len(nodes) != len(node_rows):
        raise DiscoveryBuildUnavailable(
            "SW_TAXONOMY_AMBIGUOUS",
            "frozen taxonomy contains duplicate normalized node codes",
        )
    third_level_nodes = {code: node for code, node in nodes.items() if node.level == 3}
    publications = session.execute(
        select(
            DatasetPublication,
            SwMembershipRelease,
            SourceBatch.upstream_source,
        )
        .join(
            SwMembershipRelease,
            SwMembershipRelease.release_id == DatasetPublication.release_id,
        )
        .join(
            SourceBatch,
            SourceBatch.source_batch_id == SwMembershipRelease.source_batch_id,
        )
        .where(
            DatasetPublication.dataset == _SW_MEMBERSHIP_DATASET,
            DatasetPublication.superseded_at.is_(None),
            SwMembershipRelease.observation_date <= as_of,
        )
    ).all()
    by_node = {
        _sw_code(release.node_code): (
            publication,
            release,
            _source_label(upstream_source),
        )
        for publication, release, upstream_source in publications
    }
    if len(by_node) != len(publications):
        raise DiscoveryBuildUnavailable(
            "SW_MEMBERSHIP_AMBIGUOUS",
            "multiple current membership publications target one third-level node",
        )
    if not third_level_nodes or set(by_node) != set(third_level_nodes):
        _mark_sw_unavailable(states, security_ids, "SW_MEMBERSHIP_COVERAGE_INCOMPLETE")
        return
    node_components = tuple(
        _Component(
            _SW_MEMBERSHIP_DATASET,
            f"SW2021:{node_code}",
            UUID(str(publication.data_version)),
        )
        for node_code, (publication, _release, _source_label_value) in sorted(by_node.items())
    )
    aggregate_version = _publish_sw_membership_aggregate(
        session,
        as_of=as_of,
        by_node=by_node,
        components=node_components,
        release_repository=release_repository,
    )
    release_ids = [release.release_id for _, release, _ in by_node.values()]
    rows = (
        session.execute(
            select(
                SwMembershipItem.security_id,
                SwMembershipItem.third_level_node_code,
                SwMembershipRelease.observation_date,
            )
            .join(
                SwMembershipRelease,
                SwMembershipRelease.release_id == SwMembershipItem.release_id,
            )
            .where(
                SwMembershipItem.release_id.in_(release_ids),
                SwMembershipItem.security_id.in_(security_ids),
            )
        )
        .mappings()
        .all()
    )
    grouped: dict[int, list[Mapping[Any, Any]]] = defaultdict(list)
    for row in rows:
        grouped[int(row["security_id"])].append(row)
    if any(len(value) > 1 for value in grouped.values()):
        raise DiscoveryBuildUnavailable(
            "SW_MEMBERSHIP_AMBIGUOUS", "one security belongs to multiple SW third-level nodes"
        )
    for security_id in security_ids:
        values = grouped.get(security_id, [])
        if values:
            value = values[0]
            code = _sw_code(value["third_level_node_code"])
            path = _sw_path(nodes, third_level_code=code)
            memberships[security_id].extend(
                _Membership(
                    scheme="sw.industry",
                    code=node.code,
                    name=node.name,
                    level=str(node.level),
                    observed_on=value["observation_date"],
                )
                for node in path
            )
            publication, _release, source_label = by_node[code]
            component_version = UUID(str(publication.data_version))
        else:
            source_label = _combined_source_label(
                source for _publication, _release, source in by_node.values()
            )
            component_version = aggregate_version
        states[security_id]["sw"] = _OptionalFamily(
            values={},
            availability="DATA" if values else "LEGITIMATE_EMPTY",
            reason_code=None if values else "NO_REPORTED_MEMBERSHIP",
            data_version=component_version,
            source_label=source_label,
            methodology={"code": "SW2021", "version": "1"},
        )
    components.extend(node_components)
    components.extend(
        (
            _Component(
                _SW_MEMBERSHIP_AGGREGATE_DATASET,
                _SW_MEMBERSHIP_AGGREGATE_PARTITION,
                aggregate_version,
            ),
            _Component(
                _SW_TAXONOMY_DATASET,
                str(taxonomy_publication.partition_key),
                UUID(str(taxonomy_publication.data_version)),
            ),
        )
    )


def _publish_sw_membership_aggregate(
    session: Session,
    *,
    as_of: date,
    by_node: Mapping[
        str,
        tuple[DatasetPublication, SwMembershipRelease, str],
    ],
    components: Sequence[_Component],
    release_repository: SqlAlchemyCanonicalReleaseRepository,
) -> UUID:
    """发布真实全节点 aggregate，使无归属证券的合法空集也绑定可审计版本。"""
    now = datetime.now(UTC)
    dataset_id = ensure_dataset(
        session,
        code=_SW_MEMBERSHIP_AGGREGATE_DATASET,
        domain="sector",
        grain="SW2021 complete third-level membership component manifest",
        now=now,
    )
    methodology_id = _ensure_sw_membership_aggregate_methodology(session)
    input_hash = _component_hash(components)
    normalization_run_id = _ensure_derived_run(
        session,
        dataset_id=dataset_id,
        dataset_code=_SW_MEMBERSHIP_AGGREGATE_DATASET,
        partition_key=_SW_MEMBERSHIP_AGGREGATE_PARTITION,
        mapping_version=_SW_MEMBERSHIP_AGGREGATE_MAPPING,
        request_key_prefix="sw2021-membership-aggregate",
        schema_fingerprint=_sha256("sw2021-membership-component-manifest-v1"),
        input_hash=input_hash,
        as_of=as_of,
        now=now,
    )
    observations = [release.observation_date for _, release, _ in by_node.values()]
    candidate = CanonicalReleaseCandidate(
        dataset_id=dataset_id,
        dataset_code=_SW_MEMBERSHIP_AGGREGATE_DATASET,
        partition_key=_SW_MEMBERSHIP_AGGREGATE_PARTITION,
        methodology_version_id=methodology_id,
        normalization_run_id=normalization_run_id,
        records=tuple(
            CanonicalLineageRecord(
                record_key_hash=_sha256(node_code),
                content_hash=_sha256(str(publication.data_version)),
                source_batch_id=UUID(str(release.source_batch_id)),
                transform_hash=_sha256(_SW_MEMBERSHIP_AGGREGATE_MAPPING),
                role="input",
            )
            for node_code, (publication, release, _source) in sorted(by_node.items())
        ),
        quality=CanonicalQualityDecision(
            status="passed",
            policy_code="sector.sw2021-membership.aggregate.quality",
            policy_version=1,
            rules=(
                CanonicalQualityRule(
                    "all-third-level-components-published",
                    "blocking",
                    True,
                ),
            ),
        ),
        fact_min=min(observations),
        fact_max=max(observations),
        checkpoint_kind="observation_date",
        checkpoint_position={"observationDate": as_of.isoformat()},
        expected_fencing_token=_checkpoint_token(
            session,
            dataset_id=dataset_id,
            partition_key=_SW_MEMBERSHIP_AGGREGATE_PARTITION,
            kind="observation_date",
        ),
        created_at=now,
        publication_effective_as_of=as_of,
    )

    def write_publication(
        current_session: Session,
        publication_id: UUID,
        data_version: UUID,
        release_id: UUID,
    ) -> None:
        """把 aggregate 采用的全部节点版本与 publication 原子固定。"""
        del data_version, release_id
        current_session.execute(
            insert(DatasetPublicationComponent).values(
                [
                    {
                        "aggregate_publication_id": publication_id,
                        "component_partition_key": item.key,
                        "component_data_version": item.data_version,
                    }
                    for item in components
                ]
            )
        )

    published = release_repository.publish_in_session(
        session=session,
        candidate=candidate,
        write_publication=write_publication,
    )
    return published.data_version


def _ensure_sw_membership_aggregate_methodology(session: Session) -> UUID:
    """登记申万全节点成分组合所使用的派生方法学。"""
    code = _SW_MEMBERSHIP_AGGREGATE_MAPPING
    methodology_id = uuid5(NAMESPACE_URL, f"quant-v2:methodology:{code}:1")
    session.execute(
        pg_insert(MethodologyVersion)
        .values(
            methodology_version_id=methodology_id,
            code=code,
            version=1,
            semantic_family="derived-sw-membership-complete-manifest",
            kind="derived",
            formula_hash=_sha256(_SW_MEMBERSHIP_AGGREGATE_MAPPING),
            effective_from=None,
            effective_to=None,
            status="validated",
            documentation_ref=_DOCUMENTATION,
        )
        .on_conflict_do_nothing(index_elements=("code", "version"))
    )
    return UUID(
        str(
            session.execute(
                select(MethodologyVersion.methodology_version_id).where(
                    MethodologyVersion.code == code,
                    MethodologyVersion.version == 1,
                )
            ).scalar_one()
        )
    )


def _sw_code(value: object) -> str:
    """规范化申万代码展示后缀，但不按代码前缀推断任何父子关系。"""
    code = str(value).strip().removesuffix(".SI")
    if not code:
        raise DiscoveryBuildUnavailable(
            "SW_TAXONOMY_CODE_MISSING",
            "frozen taxonomy contains a blank node code",
        )
    return code


def _sw_path(
    nodes: Mapping[str, _SwNode],
    *,
    third_level_code: str,
) -> tuple[_SwNode, _SwNode, _SwNode]:
    """严格沿冻结 `parent_code` 还原 L3、L2、L1，禁止使用代码前缀猜层级。"""
    leaf = nodes.get(third_level_code)
    parent = None if leaf is None or leaf.parent_code is None else nodes.get(leaf.parent_code)
    root = None if parent is None or parent.parent_code is None else nodes.get(parent.parent_code)
    if (
        leaf is None
        or leaf.level != 3
        or parent is None
        or parent.level != 2
        or root is None
        or root.level != 1
        or root.parent_code is not None
    ):
        raise DiscoveryBuildUnavailable(
            "SW_TAXONOMY_PATH_INCOMPLETE",
            "third-level membership cannot resolve an exact L3-L2-L1 parent chain",
        )
    return leaf, parent, root


def _mark_sw_unavailable(
    states: dict[int, dict[str, _OptionalFamily]],
    security_ids: Sequence[int],
    reason_code: str,
) -> None:
    """为全部证券设置同一申万组件不可用原因。"""
    for security_id in security_ids:
        states[security_id]["sw"] = _unavailable_family(reason_code)


def _public_trading_status(
    *,
    as_of: date,
    lifecycle: EquityListingStatusVersion,
    latest_bar: Mapping[Any, Any] | None,
    reported: EquityTradingStatusRevision | None,
) -> tuple[str, str | None, str, str | None]:
    """把生命周期、停复牌披露和最终日线收敛为公开交易状态。

    缺少最终日线不能因“未披露停牌”或显式复牌而推断成交；颜色或供应商状态也不能替代
    成交事实。生命周期终态优先于普通停复牌，矛盾证据则阻断本次横截面发布。
    """
    has_final_bar = latest_bar is not None and latest_bar["trade_date"] == as_of
    if lifecycle.status == "DELISTED":
        return "NOT_APPLICABLE", "SECURITY_DELISTED", "NOT_APPLICABLE", "SECURITY_DELISTED"
    if lifecycle.status == "SUSPENDED":
        return "NO_SESSION", "LISTING_SUSPENDED", "NOT_APPLICABLE", "LISTING_SUSPENDED"
    if reported is not None and reported.status == "SUSPENDED":
        if has_final_bar:
            raise DiscoveryBuildUnavailable(
                "TRADING_STATUS_BAR_CONFLICT",
                "reported suspension conflicts with a final daily bar",
            )
        return (
            "TRADE_SUSPENDED",
            reported.reason,
            "DATA",
            None,
        )
    if has_final_bar:
        return "TRADED", None, "DATA", None
    if reported is not None and reported.status == "RESUMED":
        return (
            "UNKNOWN",
            reported.reason or "REPORTED_RESUMPTION_WITHOUT_FINAL_BAR",
            "DATA",
            "FINAL_DAILY_BAR_UNAVAILABLE_AFTER_RESUMPTION",
        )
    if reported is not None and reported.status == "TRADED":
        return (
            "UNKNOWN",
            "REPORTED_TRADED_WITHOUT_FINAL_BAR",
            "QUARANTINED",
            "FINAL_DAILY_BAR_UNAVAILABLE_FOR_REPORTED_TRADE",
        )
    return (
        "UNKNOWN",
        "FINAL_DAILY_BAR_UNAVAILABLE",
        "LEGITIMATE_EMPTY",
        "NO_REPORTED_SUSPENSION",
    )


def _projection(
    *,
    as_of: date,
    identity: _IdentityProjection,
    lifecycle: EquityListingStatusVersion,
    identifier_component: _FactComponent,
    name_component: _FactComponent,
    lifecycle_component: _FactComponent,
    bars: Sequence[Mapping[Any, Any]],
    trading_status: EquityTradingStatusRevision | None,
    capital: Mapping[Any, Any] | None,
    trading_version: UUID,
    bar_version: UUID | None,
    capital_version: UUID | None,
    publication_source_labels: Mapping[UUID, str],
    fact_source_labels: Mapping[UUID, str],
    valuation: _OptionalFamily,
    money_flow: _OptionalFamily,
    memberships: Sequence[_Membership],
    membership_states: Mapping[str, _OptionalFamily],
) -> _Projection:
    """组合一只证券冻结值；可选组件缺失时只保留原因化空值。"""
    # 未被 publication 选择的现存 revision 仍不可消费，不能泄漏到发现横截面。
    visible_bars = bars if bar_version is not None else ()
    visible_capital = capital if capital_version is not None else None
    latest = visible_bars[0] if visible_bars else None
    previous = visible_bars[1] if len(visible_bars) > 1 else None
    close = None if latest is None else Decimal(str(latest["close_price"]))
    previous_close = None if previous is None else Decimal(str(previous["close_price"]))
    change_amount = None if close is None or previous_close is None else close - previous_close
    change_percent = (
        None
        if close is None or previous_close is None or previous_close == 0
        else (close - previous_close) / previous_close
    )
    total_shares = (
        None if visible_capital is None else Decimal(str(visible_capital["total_shares"]))
    )
    listed_a = (
        None
        if visible_capital is None or visible_capital["listed_tradable_a_shares"] is None
        else Decimal(str(visible_capital["listed_tradable_a_shares"]))
    )
    (
        status,
        status_reason,
        trading_availability,
        trading_reason,
    ) = _public_trading_status(
        as_of=as_of,
        lifecycle=lifecycle,
        latest_bar=latest,
        reported=trading_status,
    )
    if bar_version is None:
        market_availability = "SOURCE_UNAVAILABLE"
        market_reason = "DAILY_BAR_PUBLICATION_UNAVAILABLE"
    elif latest is None:
        market_availability = "SOURCE_UNAVAILABLE"
        market_reason = "FINAL_DAILY_BAR_FACT_UNAVAILABLE"
    else:
        market_availability = "DATA"
        market_reason = None
    if capital_version is None:
        capitalization_availability = "SOURCE_UNAVAILABLE"
        capitalization_reason = "SHARE_CAPITAL_PUBLICATION_UNAVAILABLE"
    elif visible_capital is None:
        capitalization_availability = "SOURCE_UNAVAILABLE"
        capitalization_reason = "SHARE_CAPITAL_FACT_UNAVAILABLE"
    elif close is None:
        capitalization_availability = "SOURCE_UNAVAILABLE"
        capitalization_reason = "FINAL_DAILY_BAR_UNAVAILABLE_FOR_CAPITALIZATION"
    else:
        capitalization_availability = "DATA"
        capitalization_reason = None
    values: dict[str, object] = {
        "security_id": identity.security_id,
        "exchange": identity.exchange,
        "symbol": identity.symbol,
        "name": identity.name,
        "lifecycle_status": lifecycle.status,
        "trading_status": status,
        "trading_status_reason": status_reason,
        "listed_on": lifecycle.listed_on,
        "delisted_on": lifecycle.delisted_on,
        "trade_date": None if latest is None else latest["trade_date"],
        "close_price": close,
        "previous_close_price": previous_close,
        "change_amount": change_amount,
        "change_percent": change_percent,
        "volume_shares": None if latest is None else int(latest["volume_shares"]),
        "amount_cny": (None if latest is None else Decimal(str(latest["amount_cny"]))),
        "turnover_rate": (
            None
            if latest is None or latest["turnover_rate"] is None
            else Decimal(str(latest["turnover_rate"]))
        ),
        "capital_effective_on": (
            None if visible_capital is None else visible_capital["effective_on"]
        ),
        "total_shares": total_shares,
        "listed_tradable_a_shares": listed_a,
        "total_market_cap_cny": (
            None if close is None or total_shares is None else close * total_shares
        ),
        "float_market_cap_cny": (None if close is None or listed_a is None else close * listed_a),
        "valuation_date": None,
        "pe_ttm": None,
        "pb": None,
        "ps_ttm": None,
        "valuation_source_label": None,
        "valuation_methodology_code": None,
        "valuation_methodology_version": None,
        "money_flow_date": None,
        "money_flow_net_amount_cny": None,
        "money_flow_net_ratio": None,
        "money_flow_source_label": None,
        "money_flow_methodology_code": None,
        "money_flow_methodology_version": None,
        **valuation.values,
        **money_flow.values,
    }
    market_sources = [
        fact_source_labels[UUID(str(row["source_batch_id"]))] for row in visible_bars[:2]
    ]
    if not market_sources and bar_version is not None:
        market_sources.append(publication_source_labels[bar_version])
    market_source_label = None if not market_sources else _combined_source_label(market_sources)
    trading_source_label = (
        publication_source_labels[trading_version]
        if trading_status is None
        else fact_source_labels[UUID(str(trading_status.source_batch_id))]
    )
    capitalization_sources = list(market_sources)
    if visible_capital is not None:
        capitalization_sources.append(
            fact_source_labels[UUID(str(visible_capital["source_batch_id"]))]
        )
    elif capital_version is not None:
        capitalization_sources.append(publication_source_labels[capital_version])
    capitalization_source_label = (
        None if not capitalization_sources else _combined_source_label(capitalization_sources)
    )
    availability = [
        _availability(
            "identity",
            "DATA",
            None,
            identifier_component.data_version,
            identifier_component.source_label,
            None,
        ),
        _availability(
            "name",
            "DATA",
            None,
            name_component.data_version,
            name_component.source_label,
            None,
        ),
        _availability(
            "lifecycle",
            "DATA",
            None,
            lifecycle_component.data_version,
            lifecycle_component.source_label,
            None,
        ),
        _availability(
            "market",
            market_availability,
            market_reason,
            bar_version,
            market_source_label,
            None,
        ),
        _availability(
            "trading_status",
            trading_availability,
            trading_reason,
            trading_version,
            trading_source_label,
            None,
        ),
        _availability(
            "capitalization",
            capitalization_availability,
            capitalization_reason,
            capital_version,
            capitalization_source_label,
            {"code": "unadjusted-close-x-reported-shares", "version": "1"},
        ),
        _family_availability("valuation", valuation),
        _family_availability("money_flow", money_flow),
    ]
    availability.extend(
        _family_availability(family, membership_states[family])
        for family in ("industry", "concept", "sw")
    )
    content_hash = _json_hash(
        {key: _json_value(value) for key, value in values.items() if key != "security_id"}
    )
    return _Projection(
        values=values,
        memberships=tuple(memberships),
        availability=tuple(availability),
        source_batch_id=UUID(str(lifecycle.source_batch_id)),
        content_hash=content_hash,
    )


def _availability(
    family: str,
    availability: str,
    reason_code: str | None,
    data_version: UUID | None,
    source_label: str | None,
    methodology: Mapping[str, object] | None,
) -> Mapping[str, object]:
    """构造一条原因化可用性行。"""
    return {
        "family": family,
        "availability": availability,
        "null_reason": reason_code,
        "component_data_version": data_version,
        "source_label": source_label,
        "methodology": None if methodology is None else dict(methodology),
    }


def _family_availability(family: str, value: _OptionalFamily) -> Mapping[str, object]:
    """把可选族状态转为持久化行。"""
    return _availability(
        family,
        value.availability,
        value.reason_code,
        value.data_version,
        value.source_label,
        value.methodology,
    )


def _unavailable_family(reason_code: str) -> _OptionalFamily:
    """构造不含任何数值的不可用语义族。"""
    return _OptionalFamily(
        values={},
        availability="SOURCE_UNAVAILABLE",
        reason_code=reason_code,
        data_version=None,
        source_label=None,
        methodology=None,
    )


def _value(row: Mapping[Any, Any] | None) -> object | None:
    """从估值窗口行读取精确值。"""
    return None if row is None else row["value"]


def _ensure_discovery_methodology(session: Session) -> UUID:
    """登记冻结的派生横截面方法学。"""
    methodology_id = uuid5(NAMESPACE_URL, f"quant-v2:methodology:{_METHODOLOGY_CODE}:1")
    session.execute(
        pg_insert(MethodologyVersion)
        .values(
            methodology_version_id=methodology_id,
            code=_METHODOLOGY_CODE,
            version=1,
            semantic_family="derived-equity-discovery-eod",
            kind="derived",
            formula_hash=_sha256(_MAPPING_VERSION),
            effective_from=None,
            effective_to=None,
            status="validated",
            documentation_ref=_DOCUMENTATION,
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


def _ensure_derived_run(
    session: Session,
    *,
    dataset_id: UUID,
    dataset_code: str,
    partition_key: str,
    mapping_version: str,
    request_key_prefix: str,
    schema_fingerprint: str,
    input_hash: str,
    as_of: date,
    now: datetime,
) -> UUID:
    """以 component manifest 建立可复算的派生同步与 normalization run。"""
    request_key = f"{request_key_prefix}:{as_of.isoformat()}:{input_hash}"
    run_id = uuid5(NAMESPACE_URL, f"quant-v2:sync-run:{request_key}")
    session.execute(
        pg_insert(SyncRun)
        .values(
            run_id=run_id,
            capability=dataset_code,
            mode="scheduled",
            request_key=request_key,
            target_date=as_of,
            status="succeeded",
            requested_at=now,
            started_at=now,
            finished_at=now,
            created_at=now,
        )
        .on_conflict_do_nothing(index_elements=("request_key",))
    )
    normalization_id = uuid4()
    inserted = session.execute(
        pg_insert(NormalizationRun)
        .values(
            normalization_run_id=normalization_id,
            dataset_id=dataset_id,
            partition_key=partition_key,
            run_id=run_id,
            adapter_version="platform-derived",
            schema_fingerprint=schema_fingerprint,
            mapping_version=mapping_version,
            input_set_hash=input_hash,
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
                    NormalizationRun.input_set_hash == input_hash,
                    NormalizationRun.mapping_version == mapping_version,
                )
            ).scalar_one()
        )
    )


def _checkpoint_token(
    session: Session,
    *,
    dataset_id: UUID,
    kind: str,
    partition_key: str = _PARTITION,
) -> int:
    """锁定指定派生分区的 canonical checkpoint 并返回 CAS token。"""
    value = session.execute(
        select(CanonicalCheckpoint.fencing_token)
        .where(
            CanonicalCheckpoint.dataset_id == dataset_id,
            CanonicalCheckpoint.partition_key == partition_key,
            CanonicalCheckpoint.checkpoint_kind == kind,
        )
        .with_for_update()
    ).scalar_one_or_none()
    return 0 if value is None else int(value)


def _component_hash(components: Sequence[_Component]) -> str:
    """计算排序稳定的输入版本 manifest 摘要。"""
    return _json_hash(
        [
            {
                "dataset": item.dataset,
                "partitionKey": item.partition_key,
                "dataVersion": str(item.data_version),
            }
            for item in components
        ]
    )


def _deduplicate_components(
    components: Iterable[_Component],
) -> tuple[_Component, ...]:
    """按数据集和分区去重，冲突版本立即失败。"""
    result: dict[str, _Component] = {}
    for item in components:
        existing = result.get(item.key)
        if existing is not None and existing.data_version != item.data_version:
            raise DiscoveryBuildUnavailable(
                "COMPONENT_VERSION_CONFLICT",
                f"component {item.key} resolves to multiple data versions",
            )
        result[item.key] = item
    return tuple(result[key] for key in sorted(result))


def _json_hash(value: object) -> str:
    """以确定性 JSON 计算 SHA-256。"""
    return _sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def _json_value(value: object) -> object:
    """把日期、UUID 和 Decimal 投影为稳定 JSON 标量。"""
    if isinstance(value, (date, datetime, UUID, Decimal)):
        return str(value)
    return value


def _sha256(value: str) -> str:
    """计算小写十六进制 SHA-256。"""
    return hashlib.sha256(value.encode()).hexdigest()
