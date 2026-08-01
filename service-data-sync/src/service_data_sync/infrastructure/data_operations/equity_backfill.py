"""生成股票中心全量回填的不可变 child 拓扑。

本模块只根据已经冻结的身份名单、来源合同和计划边界生成控制面规格，不访问 Provider。
数据库持久化与分阶段提交由相邻编排模块负责，避免“边生成边执行”留下不可恢复的半计划。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID, uuid5

PHASES = (
    "RAW_SECURITY",
    "CORPORATE_ACTION",
    "DERIVED_SECURITY",
    "GLOBAL_EVENT",
    "DISCOVERY_BUILD",
)

_DATE_DATASETS = (
    "equity.bar.1d.raw",
    "equity.bar.1w.raw",
    "equity.bar.1mo.raw",
    "equity.adjustment_factor",
)
_CURRENT_REFRESH_DATASETS = (
    "equity.profile",
    "equity.share_capital.reported",
    "financial.report",
    "financial.provider-metric",
    "financial.valuation",
    "financial.derived-metric",
)
_EVENT_DATASETS = (
    "equity.corporate_event.earnings.reported",
    "equity.dragon_tiger.disclosure.reported",
    "equity.block_trade.execution.reported",
)
_PLANNED_DATASETS = frozenset(
    (
        *_DATE_DATASETS,
        "equity.corporate_action",
        *_EVENT_DATASETS,
        "equity.discovery.eod",
    )
)
_HISTORICAL_SOURCE_DATASETS = frozenset(
    (*_DATE_DATASETS, "equity.corporate_action", *_EVENT_DATASETS)
)
_MONEY_FLOW_DATASET = "money_flow.daily"
_EMPTY_INPUT_CONTRACT_HASH = hashlib.sha256(b"[]").hexdigest()
_REFERENCE_FIXED_COMPONENT_COUNTS = {
    "equity.master.cn-a": 1,
    "equity.lifecycle.explicit": 3,
    "sector.catalog.raw": 2,
    "sector.membership.release": 2,
    "sector.sw.taxonomy": 1,
    "equity.trading_status.1d": 1,
}
_REFERENCE_FIXED_COMPONENT_PARTITIONS = {
    "equity.master.cn-a": frozenset({"CN_A_STABLE"}),
    "equity.lifecycle.explicit": frozenset({"SSE", "SZSE", "BSE"}),
    "sector.catalog.raw": frozenset({"eastmoney.industry", "eastmoney.concept"}),
    "sector.membership.release": frozenset({"eastmoney.industry", "eastmoney.concept"}),
}
_REFERENCE_DATASETS = frozenset(
    {*_REFERENCE_FIXED_COMPONENT_COUNTS, "sector.sw2021.membership.snapshot"}
)


@dataclass(frozen=True, slots=True)
class FrozenIdentity:
    """表示创建事务已经冻结的一条 `CONFIRMED` 双时态身份版本。"""

    ordinal: int
    identifier_version_id: UUID
    security_id: int
    instrument_id: UUID
    exchange: str
    symbol: str
    effective_from: date
    effective_to: date | None
    known_from: datetime
    known_to: datetime | None
    effective_date_precision: str

    def active_on(self, value: date) -> bool:
        """判断该代码身份在计划业务日是否有效，采用数据库一致的半开区间。"""
        return self.effective_from <= value and (
            self.effective_to is None or value < self.effective_to
        )

    def last_legal_date(self, as_of: date) -> date:
        """返回不越过身份半开终点的最后合法业务日期。"""
        if self.effective_to is None:
            return as_of
        return min(as_of, self.effective_to - timedelta(days=1))


@dataclass(frozen=True, slots=True)
class FrozenSource:
    """表示一个数据集经人工验证并与控制面实时绑定对齐的来源合同。"""

    dataset_code: str
    publication_dataset_code: str
    source_snapshot: tuple[dict[str, Any], ...]
    source_snapshot_hash: str
    earliest_date: date | None
    earliest_date_method: str
    evidence_ref: str
    evidence_sha256: str
    evidence_observed_at: datetime
    expected_provider_id: str
    expected_capability: str
    expected_upstream_source: str
    expected_adapter_version: str
    expected_schema_fingerprint: str
    supported_exchanges: tuple[str, ...]
    methodology_code: str
    methodology_version: int
    mapping_version: str
    source_contract_hash: str
    source_kind: str = "EXTERNAL_PROVIDER"
    internal_executor_code: str | None = None
    input_contract: tuple[dict[str, Any], ...] = ()
    input_contract_hash: str = _EMPTY_INPUT_CONTRACT_HASH

    def validate(self) -> None:
        """拒绝缺字段、弱摘要和历史能力无来源起点的伪合同。"""
        if self.dataset_code not in _PLANNED_DATASETS:
            raise ValueError(f"unsupported equity backfill dataset: {self.dataset_code}")
        if self.dataset_code in _HISTORICAL_SOURCE_DATASETS and self.earliest_date is None:
            raise ValueError(f"historical source boundary is required: {self.dataset_code}")
        if not self.source_snapshot:
            raise ValueError(f"source snapshot is empty: {self.dataset_code}")
        for field_name, value in (
            ("source_snapshot_hash", self.source_snapshot_hash),
            ("evidence_sha256", self.evidence_sha256),
            ("expected_schema_fingerprint", self.expected_schema_fingerprint),
            ("source_contract_hash", self.source_contract_hash),
        ):
            if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
                raise ValueError(f"{field_name} must be a lowercase SHA-256")
        if self.methodology_version < 1:
            raise ValueError("methodology version must be positive")
        if tuple(sorted(set(self.supported_exchanges))) != self.supported_exchanges or any(
            exchange not in {"SSE", "SZSE", "BSE"} for exchange in self.supported_exchanges
        ):
            raise ValueError(f"supported exchanges are invalid: {self.dataset_code}")
        if self.source_kind not in {"EXTERNAL_PROVIDER", "INTERNAL_EXECUTOR"}:
            raise ValueError(f"source kind is invalid: {self.dataset_code}")
        if self.source_kind == "EXTERNAL_PROVIDER" and self.internal_executor_code is not None:
            raise ValueError("external source cannot freeze an internal executor")
        if self.source_kind == "INTERNAL_EXECUTOR" and (
            self.internal_executor_code is None
            or not self.internal_executor_code.strip()
            or self.expected_provider_id != "platform"
        ):
            raise ValueError("internal source requires a concrete platform executor")
        if _hash_json(list(self.input_contract)) != self.input_contract_hash:
            raise ValueError(f"input contract hash mismatch: {self.dataset_code}")
        required_text = (
            self.publication_dataset_code,
            self.earliest_date_method,
            self.evidence_ref,
            self.expected_provider_id,
            self.expected_capability,
            self.expected_upstream_source,
            self.expected_adapter_version,
            self.methodology_code,
            self.mapping_version,
        )
        if any(not value.strip() for value in required_text):
            raise ValueError(f"source contract contains blank fields: {self.dataset_code}")
        if _hash_json(list(self.source_snapshot)) != self.source_snapshot_hash:
            raise ValueError(f"source snapshot hash mismatch: {self.dataset_code}")
        if source_contract_hash(self) != self.source_contract_hash:
            raise ValueError(f"source contract hash mismatch: {self.dataset_code}")


@dataclass(frozen=True, slots=True)
class FrozenReferenceBundle:
    """表示历史计划创建前已完成并封印的当前态引用 publication bundle。"""

    publication_id: UUID
    data_version: UUID
    release_id: UUID
    snapshot_observed_on: date
    market_as_of: date
    manifest: tuple[dict[str, Any], ...]
    manifest_hash: str

    def validate(self) -> None:
        """拒绝日期混合、组件缺失、弱血缘或摘要不一致的引用 bundle。"""
        if self.market_as_of > self.snapshot_observed_on:
            raise ValueError("reference bundle market date is after snapshot date")
        if _hash_json(list(self.manifest)) != self.manifest_hash:
            raise ValueError("reference bundle manifest hash mismatch")
        counts: dict[str, int] = {}
        partitions: dict[str, set[str]] = {}
        seen_components: set[tuple[str, str]] = set()
        for component in self.manifest:
            if set(component) != {
                "datasetCode",
                "partitionKey",
                "publicationId",
                "dataVersion",
                "releaseId",
                "effectiveAsOf",
                "observedOn",
                "sourceBatchIds",
                "sourceContractHash",
            }:
                raise ValueError("reference bundle component shape is invalid")
            dataset_code = component["datasetCode"]
            partition_key = component["partitionKey"]
            if not isinstance(dataset_code, str) or not isinstance(partition_key, str):
                raise ValueError("reference bundle component key is invalid")
            if dataset_code not in _REFERENCE_DATASETS:
                raise ValueError("reference bundle contains an unknown dataset")
            component_key = (dataset_code, partition_key)
            if component_key in seen_components:
                raise ValueError("reference bundle contains a duplicate component")
            seen_components.add(component_key)
            counts[dataset_code] = counts.get(dataset_code, 0) + 1
            partitions.setdefault(dataset_code, set()).add(partition_key)
            try:
                UUID(str(component["publicationId"]))
                UUID(str(component["dataVersion"]))
                release_id = component["releaseId"]
                if release_id is None:
                    raise TypeError
                UUID(str(release_id))
                effective_value = component["effectiveAsOf"]
                effective_as_of = (
                    None if effective_value is None else date.fromisoformat(str(effective_value))
                )
                observed_on = date.fromisoformat(str(component["observedOn"]))
                raw_source_batch_ids = component["sourceBatchIds"]
                if not isinstance(raw_source_batch_ids, list):
                    raise TypeError
                source_batch_ids = tuple(UUID(str(value)) for value in raw_source_batch_ids)
            except (TypeError, ValueError) as error:
                raise ValueError("reference bundle component identity is invalid") from error
            if not source_batch_ids or len(source_batch_ids) != len(set(source_batch_ids)):
                raise ValueError("reference bundle component has no exact source batch")
            if [str(value) for value in source_batch_ids] != sorted(
                str(value) for value in source_batch_ids
            ):
                raise ValueError("reference bundle source batches are not canonical")
            source_hash = component["sourceContractHash"]
            if (
                not isinstance(source_hash, str)
                or len(source_hash) != 64
                or any(character not in "0123456789abcdef" for character in source_hash)
            ):
                raise ValueError("reference bundle component source contract is invalid")
            expected_date = (
                self.market_as_of
                if dataset_code == "equity.trading_status.1d"
                else self.snapshot_observed_on
            )
            if observed_on != expected_date or (
                effective_as_of is not None and effective_as_of > observed_on
            ):
                raise ValueError("reference bundle component date is mixed")
            if dataset_code == "equity.trading_status.1d" and effective_as_of != self.market_as_of:
                raise ValueError("reference bundle trading-status date is stale")
        for dataset_code, expected_count in _REFERENCE_FIXED_COMPONENT_COUNTS.items():
            if counts.get(dataset_code) != expected_count:
                raise ValueError("reference bundle component coverage is incomplete")
        sw_memberships = partitions.get("sector.sw2021.membership.snapshot", set())
        if counts.get("sector.sw2021.membership.snapshot", 0) < 1 or not sw_memberships:
            raise ValueError("reference bundle SW membership coverage is incomplete")
        if set(counts) != _REFERENCE_DATASETS:
            raise ValueError("reference bundle component coverage is incomplete")
        for dataset_code, expected_partitions in _REFERENCE_FIXED_COMPONENT_PARTITIONS.items():
            if frozenset(partitions.get(dataset_code, set())) != expected_partitions:
                raise ValueError("reference bundle component partitions are incomplete")
        if partitions.get("sector.sw.taxonomy") != {
            f"sw.industry:{self.snapshot_observed_on.isoformat()}"
        }:
            raise ValueError("reference bundle SW taxonomy partition is invalid")
        if partitions.get("equity.trading_status.1d") != {f"date:{self.market_as_of.isoformat()}"}:
            raise ValueError("reference bundle trading-status partition is invalid")
        if any(
            not partition_key.startswith("SW2021:")
            or not partition_key.removeprefix("SW2021:").strip()
            for partition_key in sw_memberships
        ):
            raise ValueError("reference bundle SW membership partition is invalid")


@dataclass(frozen=True, slots=True)
class PlannedChild:
    """表示原子持久化前已完全冻结的一条 control-plane command 规格。"""

    child_id: UUID
    ordinal: int
    phase: str
    requirement: str
    child_key: str
    identity_ordinal: int | None
    window_from: date | None
    window_to: date | None
    targets: tuple[dict[str, Any], ...]
    intents: tuple[dict[str, Any], ...]
    dependency_keys: tuple[str, ...]
    completion_dependency_keys: tuple[str, ...]
    source_hashes: dict[str, str]
    submission_id: UUID
    request_prefix: str


@dataclass(frozen=True, slots=True)
class BackfillTopology:
    """保存完整 child DAG 和不被误记成功的显式排除项。"""

    children: tuple[PlannedChild, ...]
    exclusions: tuple[dict[str, Any], ...]
    reference_bundle: FrozenReferenceBundle


@dataclass(frozen=True, slots=True)
class TopologyPage:
    """表示满足事务预算的连续 child 规格页及其规范摘要。"""

    page_number: int
    first_ordinal: int
    last_ordinal: int
    children: tuple[PlannedChild, ...]
    payload_bytes: int
    page_hash: str


@dataclass(frozen=True, slots=True)
class TopologySeal:
    """表示全部分页完成后才允许 dispatch 的完整性封印。"""

    page_count: int
    child_count: int
    topology_hash: str
    page_roster_hash: str


class _TopologyBuilder:
    """在内存中顺序构造拓扑，并集中维护 child 稳定标识。"""

    def __init__(
        self,
        *,
        plan_id: UUID,
        snapshot_observed_on: date,
        market_as_of: date,
        known_at: datetime,
        roster_hash: str,
        sources: Mapping[str, FrozenSource],
        reference_bundle: FrozenReferenceBundle,
    ) -> None:
        """接收计划边界和完整来源合同，尚不产生任何数据库或网络副作用。"""
        self._plan_id = plan_id
        self._snapshot_observed_on = snapshot_observed_on
        self._market_as_of = market_as_of
        self._known_at = known_at
        self._roster_hash = roster_hash
        self._sources = sources
        self._reference_bundle = reference_bundle
        self._children: list[PlannedChild] = []
        self._exclusions: list[dict[str, Any]] = []

    def add(
        self,
        *,
        phase: str,
        targets: list[dict[str, Any]],
        requirement: str = "OPTIONAL",
        identity: FrozenIdentity | None = None,
        window_from: date | None = None,
        window_to: date | None = None,
        dependency_keys: tuple[str, ...] = (),
        completion_dependency_keys: tuple[str, ...] = (),
        inclusion_reasons: Mapping[str, str] | None = None,
    ) -> PlannedChild:
        """生成稳定 child key、严格私有意图和控制面 submission 身份。"""
        if phase not in PHASES:
            raise ValueError(f"unknown equity backfill phase: {phase}")
        if requirement not in {"BASE_REQUIRED", "OPTIONAL", "FINAL_REQUIRED"}:
            raise ValueError(f"unknown equity backfill requirement: {requirement}")
        dataset_codes = [str(target["datasetCode"]) for target in targets]
        if len(dataset_codes) != len(set(dataset_codes)):
            raise ValueError("child targets must use unique dataset codes")
        source_hashes = {
            dataset_code: self._sources[dataset_code].source_snapshot_hash
            for dataset_code in dataset_codes
        }
        seed = {
            "planId": str(self._plan_id),
            "phase": phase,
            "requirement": requirement,
            "identityOrdinal": None if identity is None else identity.ordinal,
            "windowFrom": None if window_from is None else window_from.isoformat(),
            "windowTo": None if window_to is None else window_to.isoformat(),
            "targets": targets,
            "dependencies": list(dependency_keys),
            "completionDependencies": list(completion_dependency_keys),
            "sourceHashes": source_hashes,
        }
        child_key = _hash_json(seed)
        intents = [
            self._intent(
                child_key=child_key,
                target_index=index,
                dataset_code=dataset_code,
                identity=identity,
                window_from=window_from,
                window_to=window_to,
                inclusion_reason=(inclusion_reasons or {}).get(
                    dataset_code, "FROZEN_PLAN_PREREQUISITE"
                ),
            )
            for index, dataset_code in enumerate(dataset_codes)
        ]
        ordinal = len(self._children) + 1
        request_prefix = f"eqbf:{self._plan_id.hex[:16]}:{ordinal}"
        # 延迟导入避免控制面加载 planner 时与兼容提交模块形成初始化环。
        from service_data_sync.infrastructure.data_operations.legacy_submission import (
            system_command_group_identity,
        )

        _fingerprint, submission_id = system_command_group_identity(
            targets=targets,
            intents=intents,
            request_prefix=request_prefix,
        )
        child = PlannedChild(
            child_id=uuid5(self._plan_id, child_key),
            ordinal=ordinal,
            phase=phase,
            requirement=requirement,
            child_key=child_key,
            identity_ordinal=None if identity is None else identity.ordinal,
            window_from=window_from,
            window_to=window_to,
            targets=tuple(targets),
            intents=tuple(intents),
            dependency_keys=dependency_keys,
            completion_dependency_keys=completion_dependency_keys,
            source_hashes=source_hashes,
            submission_id=submission_id,
            request_prefix=request_prefix,
        )
        self._children.append(child)
        return child

    def exclude(
        self,
        *,
        dataset_code: str,
        reason_code: str,
        identity: FrozenIdentity | None = None,
        detail: str,
    ) -> None:
        """记录不会生成 child 的稳定原因，排除项不会进入成功分母。"""
        self._exclusions.append(
            {
                "datasetCode": dataset_code,
                "reasonCode": reason_code,
                "identifierVersionId": (
                    None if identity is None else str(identity.identifier_version_id)
                ),
                "exchange": None if identity is None else identity.exchange,
                "symbol": None if identity is None else identity.symbol,
                "detail": detail,
            }
        )

    def finish(self) -> BackfillTopology:
        """返回不可变拓扑并执行独立于数据库的完整结构审计。"""
        topology = BackfillTopology(
            tuple(self._children),
            tuple(self._exclusions),
            self._reference_bundle,
        )
        validate_topology(topology, self._sources)
        return topology

    def _intent(
        self,
        *,
        child_key: str,
        target_index: int,
        dataset_code: str,
        identity: FrozenIdentity | None,
        window_from: date | None,
        window_to: date | None,
        inclusion_reason: str,
    ) -> dict[str, Any]:
        """构造只允许股票回填编排器使用的精确执行意图。"""
        source = self._sources[dataset_code]
        intent: dict[str, Any] = {
            "kind": "EQUITY_BACKFILL",
            "planId": str(self._plan_id),
            "childKey": child_key,
            "targetIndex": target_index,
            "sourceSnapshotHash": source.source_snapshot_hash,
            "sourceContractHash": source.source_contract_hash,
            "sourceSupportedExchanges": list(source.supported_exchanges),
            "sourceEarliestDate": (
                None if source.earliest_date is None else source.earliest_date.isoformat()
            ),
            "backfillDateFrom": (None if window_from is None else window_from.isoformat()),
            "backfillDateTo": None if window_to is None else window_to.isoformat(),
            "windowInclusionReason": inclusion_reason,
            "rosterHash": self._roster_hash,
            "referenceBundlePublicationId": str(self._reference_bundle.publication_id),
            "referenceBundleDataVersion": str(self._reference_bundle.data_version),
            "referenceManifestHash": self._reference_bundle.manifest_hash,
            "snapshotObservedOn": self._snapshot_observed_on.isoformat(),
            "marketAsOf": self._market_as_of.isoformat(),
            "knownAt": self._known_at.isoformat(),
            "observationSemantics": _observation_semantics(dataset_code),
        }
        if identity is None:
            intent["identity"] = None
        else:
            intent["identity"] = {
                "ordinal": identity.ordinal,
                "identifierVersionId": str(identity.identifier_version_id),
                "securityId": identity.security_id,
                "instrumentId": str(identity.instrument_id),
                "exchange": identity.exchange,
                "symbol": identity.symbol,
                "effectiveFrom": identity.effective_from.isoformat(),
                "effectiveTo": (
                    None if identity.effective_to is None else identity.effective_to.isoformat()
                ),
                "knownFrom": identity.known_from.isoformat(),
                "knownTo": None if identity.known_to is None else identity.known_to.isoformat(),
            }
        return intent


def source_contract_hash(source: FrozenSource) -> str:
    """计算不含自引用摘要字段的来源合同规范 SHA-256。"""
    return _hash_json(
        {
            "datasetCode": source.dataset_code,
            "publicationDatasetCode": source.publication_dataset_code,
            "sourceSnapshotHash": source.source_snapshot_hash,
            "earliestDate": (
                None if source.earliest_date is None else source.earliest_date.isoformat()
            ),
            "earliestDateMethod": source.earliest_date_method,
            "evidenceRef": source.evidence_ref,
            "evidenceSha256": source.evidence_sha256,
            "evidenceObservedAt": source.evidence_observed_at.isoformat(),
            "expectedProviderId": source.expected_provider_id,
            "expectedCapability": source.expected_capability,
            "expectedUpstreamSource": source.expected_upstream_source,
            "expectedAdapterVersion": source.expected_adapter_version,
            "expectedSchemaFingerprint": source.expected_schema_fingerprint,
            "supportedExchanges": list(source.supported_exchanges),
            "methodologyCode": source.methodology_code,
            "methodologyVersion": source.methodology_version,
            "mappingVersion": source.mapping_version,
            "sourceKind": source.source_kind,
            "internalExecutorCode": source.internal_executor_code,
            "inputContractHash": source.input_contract_hash,
        }
    )


def compute_roster_hash(identities: tuple[FrozenIdentity, ...]) -> str:
    """计算按稳定序号排列的完整确认身份版本名单摘要。"""
    return _hash_json(
        [
            {
                "ordinal": identity.ordinal,
                "identifierVersionId": str(identity.identifier_version_id),
                "securityId": identity.security_id,
                "instrumentId": str(identity.instrument_id),
                "exchange": identity.exchange,
                "symbol": identity.symbol,
                "effectiveFrom": identity.effective_from.isoformat(),
                "effectiveTo": (
                    None if identity.effective_to is None else identity.effective_to.isoformat()
                ),
                "knownFrom": identity.known_from.isoformat(),
                "knownTo": None if identity.known_to is None else identity.known_to.isoformat(),
                "effectiveDatePrecision": identity.effective_date_precision,
            }
            for identity in sorted(identities, key=lambda item: item.ordinal)
        ]
    )


def build_topology(
    *,
    plan_id: UUID,
    snapshot_observed_on: date,
    market_as_of: date,
    known_at: datetime,
    roster_hash: str,
    identities: tuple[FrozenIdentity, ...],
    sources: Mapping[str, FrozenSource],
    reference_bundle: FrozenReferenceBundle,
) -> BackfillTopology:
    """生成覆盖股票中心全部可支持能力的分阶段、可恢复全量计划。"""
    if market_as_of > snapshot_observed_on:
        raise ValueError("market as-of must not be after snapshot observation date")
    if compute_roster_hash(identities) != roster_hash:
        raise ValueError("frozen identity roster hash mismatch")
    reference_bundle.validate()
    if (
        reference_bundle.snapshot_observed_on != snapshot_observed_on
        or reference_bundle.market_as_of != market_as_of
    ):
        raise ValueError("reference bundle dates differ from historical plan boundaries")
    if set(sources) != _PLANNED_DATASETS:
        missing = sorted(_PLANNED_DATASETS - set(sources))
        extra = sorted(set(sources) - _PLANNED_DATASETS)
        raise ValueError(f"source manifest mismatch; missing={missing}, extra={extra}")
    for source in sources.values():
        source.validate()
    builder = _TopologyBuilder(
        plan_id=plan_id,
        snapshot_observed_on=snapshot_observed_on,
        market_as_of=market_as_of,
        known_at=known_at,
        roster_hash=roster_hash,
        sources=sources,
        reference_bundle=reference_bundle,
    )
    for identity in identities:
        _add_historical_market_children(builder, identity, market_as_of, sources)
        _add_corporate_action_children(builder, identity, snapshot_observed_on, sources)
    event_dependencies = _add_event_children(
        builder,
        snapshot_observed_on=snapshot_observed_on,
        market_as_of=market_as_of,
        sources=sources,
    )
    builder.add(
        phase="DISCOVERY_BUILD",
        requirement="FINAL_REQUIRED",
        targets=[
            _target(
                "equity.discovery.eod",
                "OBSERVATION_DATE",
                {"kind": "GLOBAL"},
                market_as_of,
            )
        ],
        completion_dependency_keys=event_dependencies,
    )
    builder.exclude(
        dataset_code=_MONEY_FLOW_DATASET,
        reason_code="UNSUPPORTED_PROVIDER_METHODOLOGY",
        detail="当前资金流来源和供应商方法学未获准用于股票中心，不生成 child 或成功状态。",
    )
    for dataset_code in _CURRENT_REFRESH_DATASETS:
        builder.exclude(
            dataset_code=dataset_code,
            reason_code="CURRENT_SOURCE_SEPARATE_REFRESH",
            detail=(
                "该数据集只能表达执行时当前值，不能伪装为历史计划快照；"
                "由独立当前数据刷新 command 记录真实 observedAt 与 dataVersion。"
            ),
        )
    return builder.finish()


def _add_historical_market_children(
    builder: _TopologyBuilder,
    identity: FrozenIdentity,
    as_of: date,
    sources: Mapping[str, FrozenSource],
) -> None:
    """每身份与数据集只生成一个 child，执行器用持久 checkpoint 内部分窗。"""
    end = identity.last_legal_date(as_of)
    for dataset_code in _DATE_DATASETS:
        source = sources[dataset_code]
        if identity.exchange not in source.supported_exchanges:
            builder.exclude(
                dataset_code=dataset_code,
                reason_code="SOURCE_EXCHANGE_UNAVAILABLE",
                identity=identity,
                detail=(
                    "冻结来源合同未通过该交易所实时能力探测；不调用未验证来源，"
                    "也不把缺失行情记为成功。"
                ),
            )
            continue
        earliest = source.earliest_date
        if earliest is None:
            raise ValueError(f"historical source boundary is required: {dataset_code}")
        start = max(identity.effective_from, earliest)
        if start > end:
            builder.exclude(
                dataset_code=dataset_code,
                reason_code="IDENTITY_OUTSIDE_SOURCE_COVERAGE",
                identity=identity,
                detail="身份有效区间与已证明来源历史区间没有交集。",
            )
            continue
        builder.add(
            phase="RAW_SECURITY",
            targets=[_target(dataset_code, "FULL", _instrument(identity))],
            identity=identity,
            window_from=start,
            window_to=end,
            inclusion_reasons={
                dataset_code: (
                    "IDENTITY_AND_SOURCE_CLIPPED_WINDOW"
                    if start != identity.effective_from
                    else "FULL_LEGAL_IDENTITY_WINDOW"
                )
            },
        )


def _add_corporate_action_children(
    builder: _TopologyBuilder,
    identity: FrozenIdentity,
    as_of: date,
    sources: Mapping[str, FrozenSource],
) -> None:
    """每身份生成一个公司行动 child，由执行器用 1098 日 checkpoint 分窗。"""
    source = sources["equity.corporate_action"]
    if identity.exchange not in source.supported_exchanges:
        builder.exclude(
            dataset_code="equity.corporate_action",
            reason_code="SOURCE_EXCHANGE_UNAVAILABLE",
            identity=identity,
            detail="冻结公司行动来源未证明支持该交易所，不生成执行 child。",
        )
        return
    earliest = source.earliest_date
    if earliest is None:
        raise ValueError("corporate action source boundary is required")
    start = max(identity.effective_from, earliest)
    end = identity.last_legal_date(as_of)
    if start > end:
        builder.exclude(
            dataset_code="equity.corporate_action",
            reason_code="IDENTITY_OUTSIDE_SOURCE_COVERAGE",
            identity=identity,
            detail="身份有效区间与公司行动来源历史区间没有交集。",
        )
        return
    builder.add(
        phase="CORPORATE_ACTION",
        targets=[_target("equity.corporate_action", "FULL", _instrument(identity))],
        identity=identity,
        window_from=start,
        window_to=end,
    )


def _add_event_children(
    builder: _TopologyBuilder,
    *,
    snapshot_observed_on: date,
    market_as_of: date,
    sources: Mapping[str, FrozenSource],
) -> tuple[str, ...]:
    """每事件数据集生成一个 child，由执行器用 31 日 checkpoint 内部分窗。"""
    earliest_dates = {
        dataset_code: sources[dataset_code].earliest_date for dataset_code in _EVENT_DATASETS
    }
    if any(value is None for value in earliest_dates.values()):
        raise ValueError("all event source boundaries are required")
    dataset_ends = {
        "equity.corporate_event.earnings.reported": snapshot_observed_on,
        "equity.dragon_tiger.disclosure.reported": market_as_of,
        "equity.block_trade.execution.reported": market_as_of,
    }
    keys: list[str] = []
    for dataset_code in _EVENT_DATASETS:
        earliest = earliest_dates[dataset_code]
        if earliest is None:
            raise ValueError("event source boundary is required")
        target_to = dataset_ends[dataset_code]
        if earliest > target_to:
            builder.exclude(
                dataset_code=dataset_code,
                reason_code="PLAN_DATE_OUTSIDE_SOURCE_COVERAGE",
                detail="计划日期早于该事件来源已证明的历史起点。",
            )
            continue
        child = builder.add(
            phase="GLOBAL_EVENT",
            targets=[_target(dataset_code, "FULL", _event_selector(dataset_code))],
            window_from=earliest,
            window_to=target_to,
            inclusion_reasons={dataset_code: "FULL_PROVEN_EVENT_WINDOW"},
        )
        keys.append(child.child_key)
    return tuple(keys)


def validate_topology(
    topology: BackfillTopology,
    sources: Mapping[str, FrozenSource],
) -> None:
    """审计目标/意图/来源一一对应、依赖同计划存在且 DAG 无环。"""
    if not topology.children:
        raise ValueError("equity backfill topology must contain children")
    keys = [child.child_key for child in topology.children]
    if len(keys) != len(set(keys)):
        raise ValueError("equity backfill child keys must be unique")
    submissions = [child.submission_id for child in topology.children]
    if len(submissions) != len(set(submissions)):
        raise ValueError("equity backfill submission ids must be unique")
    ordinals = [child.ordinal for child in topology.children]
    if ordinals != list(range(1, len(topology.children) + 1)):
        raise ValueError("equity backfill ordinals must be contiguous")
    known_keys = set(keys)
    phase_rank = {phase: index for index, phase in enumerate(PHASES)}
    child_by_key = {child.child_key: child for child in topology.children}
    for child in topology.children:
        if len(child.targets) != len(child.intents) or len(child.targets) != len(
            child.source_hashes
        ):
            raise ValueError("targets, intents and source hashes must align")
        dataset_codes = [str(target["datasetCode"]) for target in child.targets]
        if len(dataset_codes) != len(set(dataset_codes)):
            raise ValueError("dataset codes must be unique within one command")
        if set(dataset_codes) != set(child.source_hashes):
            raise ValueError("child source hashes do not match targets")
        for index, (target, intent) in enumerate(zip(child.targets, child.intents, strict=True)):
            dataset_code = str(target["datasetCode"])
            if intent.get("targetIndex") != index or intent.get("childKey") != child.child_key:
                raise ValueError("child intent identity does not align with target")
            if intent.get("sourceSnapshotHash") != sources[dataset_code].source_snapshot_hash:
                raise ValueError("child intent source snapshot does not align")
            if (
                intent.get("referenceBundlePublicationId")
                != str(topology.reference_bundle.publication_id)
                or intent.get("referenceBundleDataVersion")
                != str(topology.reference_bundle.data_version)
                or intent.get("referenceManifestHash") != topology.reference_bundle.manifest_hash
            ):
                raise ValueError("child intent reference bundle does not align")
            if intent.get("observationSemantics") != _observation_semantics(dataset_code):
                raise ValueError("child intent observation semantics do not align")
            if intent.get("sourceSupportedExchanges") != list(
                sources[dataset_code].supported_exchanges
            ):
                raise ValueError("child intent source exchange support does not align")
            identity = intent.get("identity")
            if (
                isinstance(identity, dict)
                and identity.get("exchange") not in sources[dataset_code].supported_exchanges
            ):
                raise ValueError("child identity exchange is outside frozen source support")
        if set(child.dependency_keys) & set(child.completion_dependency_keys):
            raise ValueError("success and completion dependencies must not overlap")
        for dependency_key in (*child.dependency_keys, *child.completion_dependency_keys):
            if dependency_key not in known_keys:
                raise ValueError("child dependency does not exist in the same plan")
            dependency = child_by_key[dependency_key]
            if phase_rank[dependency.phase] > phase_rank[child.phase]:
                raise ValueError("child dependency points to a future phase")
    _reject_dependency_cycle(child_by_key)
    money_flow_children = [
        child
        for child in topology.children
        if any(target["datasetCode"] == _MONEY_FLOW_DATASET for target in child.targets)
    ]
    money_flow_exclusions = [
        item
        for item in topology.exclusions
        if item.get("datasetCode") == _MONEY_FLOW_DATASET
        and item.get("reasonCode") == "UNSUPPORTED_PROVIDER_METHODOLOGY"
    ]
    if money_flow_children or len(money_flow_exclusions) != 1:
        raise ValueError("money flow must be one explicit exclusion and never a child")


def iter_topology_pages(
    topology: BackfillTopology,
    *,
    maximum_children: int = 1000,
    maximum_payload_bytes: int = 8 * 1024 * 1024,
) -> Iterator[TopologyPage]:
    """按稳定 ordinal 生成有界事务页，单个 child 超预算时立即失败。"""
    if not 1 <= maximum_children <= 1000:
        raise ValueError("equity backfill page child limit is invalid")
    if not 1 <= maximum_payload_bytes <= 8 * 1024 * 1024:
        raise ValueError("equity backfill page byte limit is invalid")
    page_number = 1
    children: list[PlannedChild] = []
    payloads: list[dict[str, Any]] = []
    page_payload_bytes = 2
    for child in topology.children:
        payload = _child_payload(child)
        payload_bytes = len(_canonical_json(payload))
        single_bytes = payload_bytes + 2
        if single_bytes > maximum_payload_bytes:
            raise ValueError(f"equity backfill child exceeds page budget: {child.child_key}")
        candidate_bytes = page_payload_bytes + payload_bytes + int(bool(children))
        if children and (
            len(children) >= maximum_children or candidate_bytes > maximum_payload_bytes
        ):
            yield _topology_page(page_number, children, payloads)
            page_number += 1
            children = []
            payloads = []
            page_payload_bytes = 2
            candidate_bytes = page_payload_bytes + payload_bytes
        children.append(child)
        payloads.append(payload)
        page_payload_bytes = candidate_bytes
    if children:
        yield _topology_page(page_number, children, payloads)


def compute_topology_seal(topology: BackfillTopology) -> TopologySeal:
    """计算 child/exclusion 与分页 roster 双摘要，恢复时可逐页重算再 seal。"""
    pages = tuple(iter_topology_pages(topology))
    return TopologySeal(
        page_count=len(pages),
        child_count=len(topology.children),
        topology_hash=_hash_json(
            {
                "children": [child.child_key for child in topology.children],
                "exclusions": list(topology.exclusions),
            }
        ),
        page_roster_hash=_hash_json(
            [
                {
                    "pageNumber": page.page_number,
                    "firstOrdinal": page.first_ordinal,
                    "lastOrdinal": page.last_ordinal,
                    "childCount": len(page.children),
                    "payloadBytes": page.payload_bytes,
                    "pageHash": page.page_hash,
                }
                for page in pages
            ]
        ),
    )


def _topology_page(
    page_number: int,
    children: list[PlannedChild],
    payloads: list[dict[str, Any]],
) -> TopologyPage:
    """把已满足预算的连续缓冲区冻结为一个不可变页。"""
    payload = _canonical_json(payloads)
    return TopologyPage(
        page_number=page_number,
        first_ordinal=children[0].ordinal,
        last_ordinal=children[-1].ordinal,
        children=tuple(children),
        payload_bytes=len(payload),
        page_hash=hashlib.sha256(payload).hexdigest(),
    )


def _child_payload(child: PlannedChild) -> dict[str, Any]:
    """投影影响提交语义的全部 child 字段，供分页摘要与数据库复验。"""
    return {
        "childId": str(child.child_id),
        "ordinal": child.ordinal,
        "phase": child.phase,
        "requirement": child.requirement,
        "childKey": child.child_key,
        "identityOrdinal": child.identity_ordinal,
        "windowFrom": None if child.window_from is None else child.window_from.isoformat(),
        "windowTo": None if child.window_to is None else child.window_to.isoformat(),
        "targets": list(child.targets),
        "intents": list(child.intents),
        "dependencies": list(child.dependency_keys),
        "completionDependencies": list(child.completion_dependency_keys),
        "sourceHashes": child.source_hashes,
        "submissionId": str(child.submission_id),
        "requestPrefix": child.request_prefix,
    }


def _canonical_json(value: object) -> bytes:
    """编码与 `_hash_json` 完全一致的规范 JSON 字节。"""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _reject_dependency_cycle(children: Mapping[str, PlannedChild]) -> None:
    """以三色 DFS 拒绝同阶段直接依赖环，避免父编排永久 HELD。"""
    states: dict[str, int] = {}

    def visit(child_key: str) -> None:
        """遍历一条 child 依赖链并在回边处立即失败。"""
        state = states.get(child_key, 0)
        if state == 1:
            raise ValueError("equity backfill dependency graph contains a cycle")
        if state == 2:
            return
        states[child_key] = 1
        for dependency_key in children[child_key].dependency_keys:
            visit(dependency_key)
        states[child_key] = 2

    for child_key in children:
        visit(child_key)


def _target(
    dataset_code: str,
    mode: str,
    selector: dict[str, Any],
    first_date: date | None = None,
    last_date: date | None = None,
) -> dict[str, Any]:
    """构造与 `_validate_targets` 完全一致、包含显式空日期键的目标。"""
    date_from: str | None = None
    date_to: str | None = None
    observation_date: str | None = None
    if mode == "DATE_RANGE":
        if first_date is None or last_date is None:
            raise ValueError("DATE_RANGE target requires both dates")
        date_from = first_date.isoformat()
        date_to = last_date.isoformat()
    elif mode == "OBSERVATION_DATE":
        if first_date is None or last_date is not None:
            raise ValueError("OBSERVATION_DATE target requires one date")
        observation_date = first_date.isoformat()
    elif first_date is not None or last_date is not None:
        raise ValueError("FULL target cannot contain dates")
    return {
        "datasetCode": dataset_code,
        "mode": mode,
        "selector": selector,
        "dateFrom": date_from,
        "dateTo": date_to,
        "observationDate": observation_date,
    }


def _instrument(identity: FrozenIdentity) -> dict[str, Any]:
    """投影控制面允许的公开证券 selector；内部身份只进入私有意图。"""
    return {"kind": "INSTRUMENT", "exchange": identity.exchange, "symbol": identity.symbol}


def _observation_semantics(dataset_code: str) -> str:
    """标记历史回填 target 的冻结或精确派生语义。"""
    if dataset_code == "equity.discovery.eod":
        return "DERIVED_FROM_EXACT_INPUTS"
    return "FROZEN_PLAN_BOUNDARY"


def _event_selector(dataset_code: str) -> dict[str, Any]:
    """返回事件数据集唯一合法的市场级 selector。"""
    if dataset_code == "equity.corporate_event.earnings.reported":
        return {"kind": "GLOBAL"}
    operation = (
        "DRAGON_TIGER"
        if dataset_code == "equity.dragon_tiger.disclosure.reported"
        else "BLOCK_TRADE"
    )
    return {"kind": "TRADING_EVENT", "operation": operation}


def _windows(start: date, end: date, maximum_days: int) -> tuple[tuple[date, date], ...]:
    """把包含端日期范围切成不超过控制面上限的连续窗口。"""
    if start > end or maximum_days < 1:
        return ()
    windows: list[tuple[date, date]] = []
    cursor = start
    while cursor <= end:
        window_end = min(end, cursor + timedelta(days=maximum_days - 1))
        windows.append((cursor, window_end))
        cursor = window_end + timedelta(days=1)
    return tuple(windows)


def _hash_json(value: object) -> str:
    """计算 UTF-8 规范 JSON 的小写 SHA-256。"""
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
