"""创建、封印并执行股票中心真实全量回填父计划。

编排器先用统一控制面执行小范围真实来源探测，再冻结引用 bundle、双时态身份名单、
adapter/schema/mapping 合同和完整 child DAG。计划页与 child 状态全部以 PostgreSQL 为
权威；进程退出后复用同一 campaign key 即可从缺页、SUBMITTING、重试或阶段水位恢复。
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import func, or_, select, text

from service_data_sync.infrastructure.data_operations.control_plane import (
    DataOperationsControlPlane,
    OperationProblem,
)
from service_data_sync.infrastructure.data_operations.equity_backfill import (
    PHASES,
    BackfillTopology,
    FrozenIdentity,
    FrozenReferenceBundle,
    FrozenSource,
    build_topology,
    compute_roster_hash,
    compute_topology_seal,
    iter_topology_pages,
    source_contract_hash,
)
from service_data_sync.infrastructure.data_operations.equity_reference_bundle import (
    EquityReferenceBundleOrchestrator,
)
from service_data_sync.infrastructure.data_operations.legacy_submission import (
    submit_system_command_group,
)
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.database.models.equity.backfill import (
    EquityBackfillChildSpec,
    EquityBackfillChildState,
    EquityBackfillPlan,
    EquityBackfillPlanIdentity,
    EquityBackfillPlanPage,
    EquityBackfillPlanSeal,
    EquityBackfillPlanSource,
    EquityBackfillPlanState,
)
from service_data_sync.infrastructure.database.models.equity.identity.equity_identifier_version import (  # noqa: E501
    EquityIdentifierVersion,
)
from service_data_sync.infrastructure.database.models.equity.identity.equity_instrument import (
    EquityInstrument,
)
from service_data_sync.infrastructure.database.models.operations import (
    DataOperationExecutionSlot,
    DataOperationRun,
    DataOperationRunSourceBatch,
)
from service_data_sync.infrastructure.database.models.provenance.source_batch import (
    SourceBatch,
)
from service_data_sync.infrastructure.database.models.publication.dataset_publication import (
    DatasetPublication,
)
from service_data_sync.infrastructure.database.models.publication.dataset_publication_component import (  # noqa: E501
    DatasetPublicationComponent,
)

_HISTORY_START = date(1990, 12, 19)
_MAX_RETRIES = 3
_TERMINAL_COMMANDS = frozenset({"SUCCEEDED", "PARTIAL", "FAILED", "CANCELLED", "REJECTED"})
_TERMINAL_CHILDREN = frozenset({"SUCCEEDED", "PARTIAL", "FAILED", "CANCELLED", "BLOCKED"})
_INSTRUMENT_SOURCE_DATASETS = (
    "equity.bar.1d.raw",
    "equity.bar.1w.raw",
    "equity.bar.1mo.raw",
    "equity.adjustment_factor",
    "equity.corporate_action",
)
_GLOBAL_EVENT_DATASETS = (
    "equity.corporate_event.earnings.reported",
    "equity.dragon_tiger.disclosure.reported",
    "equity.block_trade.execution.reported",
)
_INTERNAL_DATASETS = ("equity.discovery.eod",)
_PLANNED_DATASETS = frozenset(
    (*_INSTRUMENT_SOURCE_DATASETS, *_GLOBAL_EVENT_DATASETS, *_INTERNAL_DATASETS)
)
_HISTORICAL_DATASETS = frozenset(
    (
        "equity.bar.1d.raw",
        "equity.bar.1w.raw",
        "equity.bar.1mo.raw",
        "equity.adjustment_factor",
        "equity.corporate_action",
        *_GLOBAL_EVENT_DATASETS,
    )
)
_PUBLICATION_DATASETS: dict[str, str] = {}


def _utc_now() -> datetime:
    """返回带时区 UTC 当前时间，供生产编排默认使用。"""
    return datetime.now(UTC)


class EquityBackfillOrchestrationError(RuntimeError):
    """表示计划无法安全创建、恢复或推进，数据库保留最后一个权威水位。"""


class EquityBackfillPending(EquityBackfillOrchestrationError):
    """表示本次墙钟预算耗尽，使用同一 campaign key 可继续执行。"""


@dataclass(frozen=True, slots=True)
class _ProbeObservation:
    """保存一个数据集/交易所探测 run 的真实来源事实或稳定失败。"""

    dataset_code: str
    exchange: str | None
    command_id: UUID
    run_id: UUID
    source_snapshot: tuple[dict[str, Any], ...]
    source_batches: tuple[SourceBatch, ...]
    error: dict[str, Any] | None

    @property
    def succeeded(self) -> bool:
        """只有存在真实 SourceBatch 的无错误 run 才构成成功来源证明。"""
        return self.error is None and bool(self.source_batches)


@dataclass(frozen=True, slots=True)
class _ReferenceInputs:
    """保存引用 bundle 解出的主目录、生命周期和身份冻结输入。"""

    aggregate: DatasetPublication
    aggregate_components: tuple[dict[str, Any], ...]
    lifecycle_publications: tuple[dict[str, Any], ...]
    known_at: datetime
    identities: tuple[FrozenIdentity, ...]


class EquityBackfillOrchestrator:
    """用数据库不可变规格和统一控制面串行打通股票中心全量数据。"""

    def __init__(
        self,
        *,
        database: DatabaseClient,
        control_plane: DataOperationsControlPlane,
        reference_bundle_orchestrator: EquityReferenceBundleOrchestrator | None = None,
        now: Callable[[], datetime] | None = None,
        poll_interval_seconds: float = 0.25,
    ) -> None:
        """保存权威数据库、控制面、可选自动引用生成器与可测试时钟。"""
        self._database = database
        self._control_plane = control_plane
        self._reference_bundle_orchestrator = reference_bundle_orchestrator
        self._now = now or _utc_now
        self._poll_interval_seconds = max(0.0, poll_interval_seconds)

    def find_plan(self, campaign_key: str) -> UUID | None:
        """按稳定 campaign key 查找已有父计划，不创建引用或 Provider 请求。"""
        normalized = _campaign_key(campaign_key)
        with self._database.session() as session:
            value = session.scalar(
                select(EquityBackfillPlan.plan_id).where(
                    EquityBackfillPlan.campaign_key == normalized
                )
            )
        return None if value is None else UUID(str(value))

    def create_or_resume_plan(
        self,
        *,
        campaign_key: str,
        reference_bundle: FrozenReferenceBundle | None,
        worker_id: str,
        instrument_scope: tuple[str, str] | None = None,
        max_wait_seconds: float = 7200,
    ) -> UUID:
        """自动封印引用后创建历史计划，或从已有不可变输入补齐缺页并封印。

        调用方不传入 `reference_bundle` 时，必须注入引用生成器。新计划先完成当前态
        引用 bundle，才会探测可重放历史来源并落入父计划；已有计划绝不再次调用任何
        current-only 适配器，因此可跨上海自然日继续恢复。`instrument_scope` 只供真实
        端到端烟测缩小历史 child roster：引用 bundle 仍完整生成，且 scope 会进入独立
        campaign key，绝不与全市场计划或另一只证券混用。
        """
        normalized_scope = _instrument_scope(instrument_scope)
        normalized = _scoped_campaign_key(
            _campaign_key(campaign_key),
            instrument_scope=normalized_scope,
        )
        deadline = time.monotonic() + max(1.0, max_wait_seconds)
        existing = self.find_plan(normalized)
        if existing is not None:
            topology = self._rehydrate_topology(existing)
            self._persist_pages_and_seal(existing, topology)
            return existing
        if reference_bundle is None:
            reference_bundle = self._create_reference_bundle(
                campaign_key=normalized,
                worker_id=worker_id,
                deadline=deadline,
            )
        reference_bundle.validate()
        inputs = self._reference_inputs(reference_bundle)
        if normalized_scope is not None:
            inputs = _scoped_reference_inputs(inputs, instrument_scope=normalized_scope)
        roster_hash = compute_roster_hash(inputs.identities)
        plan_id = uuid5(
            NAMESPACE_URL,
            f"quant-v2:equity-backfill-plan:{normalized}",
        )
        observations = self._prove_external_sources(
            campaign_key=normalized,
            identities=inputs.identities,
            snapshot_observed_on=reference_bundle.snapshot_observed_on,
            market_as_of=reference_bundle.market_as_of,
            worker_id=worker_id,
            deadline=deadline,
        )
        snapshots = self._control_plane.system_source_snapshots(sorted(_PLANNED_DATASETS))
        sources = self._freeze_sources(
            observations=observations,
            snapshots=snapshots,
            identities=inputs.identities,
        )
        topology = build_topology(
            plan_id=plan_id,
            snapshot_observed_on=reference_bundle.snapshot_observed_on,
            market_as_of=reference_bundle.market_as_of,
            known_at=inputs.known_at,
            roster_hash=roster_hash,
            identities=inputs.identities,
            sources=sources,
            reference_bundle=reference_bundle,
        )
        self._persist_plan_header(
            plan_id=plan_id,
            campaign_key=normalized,
            reference_bundle=reference_bundle,
            inputs=inputs,
            roster_hash=roster_hash,
            sources=sources,
            topology=topology,
        )
        self._persist_pages_and_seal(plan_id, topology)
        return plan_id

    def _create_reference_bundle(
        self,
        *,
        campaign_key: str,
        worker_id: str,
        deadline: float,
    ) -> FrozenReferenceBundle:
        """经七步 current-only 生成器获得已封印 bundle，禁止历史计划自行补当前输入。"""
        if self._reference_bundle_orchestrator is None:
            raise EquityBackfillOrchestrationError(
                "new equity backfill plan requires an automatic reference bundle orchestrator"
            )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise EquityBackfillPending(
                "equity backfill budget expired before reference bundle sealing"
            )
        return self._reference_bundle_orchestrator.run_until_sealed(
            campaign_key=_reference_campaign_key(campaign_key),
            worker_id=f"{worker_id}:reference",
            max_wait_seconds=remaining,
        )

    def run_until_terminal(
        self,
        *,
        plan_id: UUID,
        worker_id: str,
        max_wait_seconds: float = 86400,
        maximum_inflight_children: int = 16,
    ) -> dict[str, Any]:
        """恢复并推进全部阶段；超时只返回可恢复 Pending，不改写冻结规格。"""
        if not 1 <= maximum_inflight_children <= 100:
            raise ValueError("equity backfill inflight child limit is invalid")
        deadline = time.monotonic() + max(1.0, max_wait_seconds)
        self._activate_when_idle(plan_id=plan_id, worker_id=worker_id, deadline=deadline)
        while True:
            summary = self.plan_summary(plan_id)
            if summary["status"] in {"SUCCEEDED", "PARTIAL", "FAILED", "BLOCKED"}:
                return summary
            if time.monotonic() >= deadline:
                raise EquityBackfillPending(f"equity backfill plan remains {summary['status']}")
            self._recover_submitting(plan_id, maximum_inflight_children)
            self._retry_children(plan_id)
            self._prepare_phase_children(plan_id, maximum_inflight_children)
            self._recover_submitting(plan_id, maximum_inflight_children)
            if self._advance_phase_if_terminal(plan_id):
                continue
            progressed = self._control_plane.dispatch_once(
                f"{worker_id}:equity-backfill:{str(plan_id)[:12]}"
            )
            if not progressed:
                self._control_plane.reap_expired_slots()
                if self._poll_interval_seconds:
                    time.sleep(self._poll_interval_seconds)

    def plan_summary(self, plan_id: UUID) -> dict[str, Any]:
        """读取父计划与 child 状态计数，作为 CLI 和恢复调用方的机器摘要。"""
        with self._database.session() as session:
            plan = session.get(EquityBackfillPlan, plan_id)
            state = session.get(EquityBackfillPlanState, plan_id)
            if plan is None or state is None:
                raise EquityBackfillOrchestrationError("equity backfill plan is missing")
            counts = {
                str(status): int(count)
                for status, count in session.execute(
                    select(
                        EquityBackfillChildState.status,
                        func.count(),
                    )
                    .join(
                        EquityBackfillChildSpec,
                        EquityBackfillChildSpec.child_id == EquityBackfillChildState.child_id,
                    )
                    .where(EquityBackfillChildSpec.plan_id == plan_id)
                    .group_by(EquityBackfillChildState.status)
                ).all()
            }
            return {
                "planId": str(plan.plan_id),
                "campaignKey": plan.campaign_key,
                "status": state.status,
                "currentPhase": state.current_phase,
                "childCount": plan.child_count,
                "childStatusCounts": counts,
                "rosterCount": plan.roster_count,
                "snapshotObservedOn": plan.snapshot_observed_on.isoformat(),
                "marketAsOf": plan.market_as_of.isoformat(),
                "referenceBundleDataVersion": str(plan.reference_bundle_data_version),
                "sourceEvidenceHash": plan.source_evidence_hash,
                "auditSummary": state.audit_summary_json,
                "lastError": state.last_error_json,
            }

    def _reference_inputs(self, bundle: FrozenReferenceBundle) -> _ReferenceInputs:
        """从 bundle 精确解析主目录、生命周期和知识时点可见的确认身份。"""
        master_components = [
            component
            for component in bundle.manifest
            if component["datasetCode"] == "equity.master.cn-a"
        ]
        lifecycle_components = [
            component
            for component in bundle.manifest
            if component["datasetCode"] == "equity.lifecycle.explicit"
        ]
        if len(master_components) != 1 or len(lifecycle_components) != 3:
            raise EquityBackfillOrchestrationError(
                "reference bundle has no complete master and lifecycle inputs"
            )
        with self._database.session() as session:
            aggregate = session.get(
                DatasetPublication,
                UUID(str(master_components[0]["publicationId"])),
            )
            if (
                aggregate is None
                or aggregate.dataset != "equity.master.cn-a"
                or aggregate.partition_key != "CN_A_STABLE"
                or aggregate.data_version != UUID(str(master_components[0]["dataVersion"]))
                or aggregate.quality_status != "passed"
            ):
                raise EquityBackfillOrchestrationError(
                    "reference master aggregate publication is invalid"
                )
            component_rows = tuple(
                session.scalars(
                    select(DatasetPublicationComponent)
                    .where(
                        DatasetPublicationComponent.aggregate_publication_id
                        == aggregate.publication_id
                    )
                    .order_by(DatasetPublicationComponent.component_partition_key)
                ).all()
            )
            if {row.component_partition_key for row in component_rows} != {
                "SSE",
                "SZSE",
                "BSE",
            }:
                raise EquityBackfillOrchestrationError(
                    "master aggregate component roster is incomplete"
                )
            aggregate_components: list[dict[str, Any]] = []
            for row in component_rows:
                publication = session.scalar(
                    select(DatasetPublication).where(
                        DatasetPublication.dataset == "equity.master.catalog",
                        DatasetPublication.partition_key == row.component_partition_key,
                        DatasetPublication.data_version == row.component_data_version,
                    )
                )
                if publication is None:
                    raise EquityBackfillOrchestrationError(
                        "master child publication is unavailable"
                    )
                aggregate_components.append(
                    _publication_manifest(
                        publication,
                        exchange=row.component_partition_key,
                    )
                )
            lifecycle_publications: list[dict[str, Any]] = []
            for component in sorted(
                lifecycle_components,
                key=lambda item: str(item["partitionKey"]),
            ):
                publication = session.get(
                    DatasetPublication,
                    UUID(str(component["publicationId"])),
                )
                if (
                    publication is None
                    or publication.dataset != "equity.lifecycle.explicit"
                    or publication.partition_key != component["partitionKey"]
                    or publication.data_version != UUID(str(component["dataVersion"]))
                    or publication.quality_status != "passed"
                ):
                    raise EquityBackfillOrchestrationError("lifecycle publication is invalid")
                lifecycle_publications.append(
                    _publication_manifest(
                        publication,
                        exchange=publication.partition_key,
                    )
                )
            known_at = aggregate.knowledge_cutoff or aggregate.published_at
            identity_rows = tuple(
                session.execute(
                    select(EquityIdentifierVersion, EquityInstrument.instrument_id)
                    .join(
                        EquityInstrument,
                        EquityInstrument.security_id == EquityIdentifierVersion.security_id,
                    )
                    .where(
                        EquityIdentifierVersion.identity_state == "CONFIRMED",
                        EquityIdentifierVersion.known_from <= known_at,
                        or_(
                            EquityIdentifierVersion.known_to.is_(None),
                            EquityIdentifierVersion.known_to > known_at,
                        ),
                        EquityInstrument.master_confirmed_at.is_not(None),
                        EquityInstrument.master_confirmed_at <= known_at,
                    )
                    .order_by(
                        EquityIdentifierVersion.exchange,
                        EquityIdentifierVersion.symbol,
                        EquityIdentifierVersion.effective_from,
                        EquityIdentifierVersion.version_id,
                    )
                ).all()
            )
        identities = tuple(
            FrozenIdentity(
                ordinal=ordinal,
                identifier_version_id=row.version_id,
                security_id=int(row.security_id),
                instrument_id=instrument_id,
                exchange=row.exchange,
                symbol=row.symbol,
                effective_from=row.effective_from,
                effective_to=row.effective_to,
                known_from=row.known_from,
                known_to=row.known_to,
                effective_date_precision=row.effective_date_precision,
            )
            for ordinal, (row, instrument_id) in enumerate(identity_rows, start=1)
        )
        if not identities:
            raise EquityBackfillOrchestrationError(
                "reference master contains no confirmed identity roster"
            )
        return _ReferenceInputs(
            aggregate=aggregate,
            aggregate_components=tuple(aggregate_components),
            lifecycle_publications=tuple(lifecycle_publications),
            known_at=known_at,
            identities=identities,
        )

    def _prove_external_sources(
        self,
        *,
        campaign_key: str,
        identities: tuple[FrozenIdentity, ...],
        snapshot_observed_on: date,
        market_as_of: date,
        worker_id: str,
        deadline: float,
    ) -> tuple[_ProbeObservation, ...]:
        """通过真实 Provider run 证明数据集 schema，并逐交易所记录可用或稳定不可用。"""
        representatives: dict[str, FrozenIdentity] = {}
        for exchange in ("SSE", "SZSE", "BSE"):
            candidates = [
                identity
                for identity in identities
                if identity.exchange == exchange and identity.active_on(snapshot_observed_on)
            ]
            if candidates:
                representatives[exchange] = min(
                    candidates,
                    key=lambda item: (
                        item.effective_from,
                        item.symbol,
                        str(item.identifier_version_id),
                    ),
                )
        if not representatives:
            raise EquityBackfillOrchestrationError(
                "source proof has no active representative identity"
            )
        observations: list[_ProbeObservation] = []
        for exchange, identity in sorted(representatives.items()):
            targets = [
                _instrument_probe_target(
                    dataset_code,
                    identity=identity,
                    market_as_of=market_as_of,
                )
                for dataset_code in _INSTRUMENT_SOURCE_DATASETS
            ]
            observations.extend(
                self._run_probe_group(
                    campaign_key=campaign_key,
                    group_key=f"instrument:{exchange}",
                    exchange=exchange,
                    targets=targets,
                    worker_id=worker_id,
                    deadline=deadline,
                )
            )
        event_targets = [
            _event_probe_target(dataset_code, market_as_of=market_as_of)
            for dataset_code in _GLOBAL_EVENT_DATASETS
        ]
        event_observations = self._run_probe_group(
            campaign_key=campaign_key,
            group_key="global-events",
            exchange=None,
            targets=event_targets,
            worker_id=worker_id,
            deadline=deadline,
        )
        if any(not observation.succeeded for observation in event_observations):
            raise EquityBackfillOrchestrationError(
                "global event source proof did not produce real SourceBatch evidence"
            )
        observations.extend(event_observations)
        return tuple(observations)

    def _run_probe_group(
        self,
        *,
        campaign_key: str,
        group_key: str,
        exchange: str | None,
        targets: list[dict[str, Any]],
        worker_id: str,
        deadline: float,
    ) -> tuple[_ProbeObservation, ...]:
        """幂等执行一个探测组；可重试失败复制新 run，成功结果不会被假重跑覆盖。"""
        request_prefix = f"eqbf-proof:{_short_hash(campaign_key)}:{_short_hash(group_key)}"
        receipt = submit_system_command_group(
            self._control_plane,
            targets=targets,
            reason="股票中心全量回填真实来源能力探测",
            request_prefix=request_prefix,
        )
        command_id = UUID(str(receipt["commandId"]))
        retry_no = 0
        results: dict[str, _ProbeObservation] = {}
        while True:
            detail = self._control_plane.command_detail(command_id)
            status = str(detail["status"])
            if status in _TERMINAL_COMMANDS:
                child_runs = detail["childRuns"]
                if not isinstance(child_runs, list) or not child_runs:
                    raise EquityBackfillOrchestrationError("source proof command has no child runs")
                retryable_failure = False
                for item in child_runs:
                    dataset_code = str(item["datasetCode"])
                    run_status = str(item["status"])
                    run_id = UUID(str(item["runId"]))
                    if run_status == "SUCCEEDED":
                        results[dataset_code] = self._probe_observation(
                            dataset_code=dataset_code,
                            exchange=exchange,
                            command_id=command_id,
                            run_id=run_id,
                            error=None,
                        )
                        continue
                    error = item.get("error")
                    normalized_error = (
                        dict(error)
                        if isinstance(error, Mapping)
                        else {
                            "code": "source-proof-failed",
                            "stage": "PROVIDER_FETCH",
                            "retryable": False,
                        }
                    )
                    if normalized_error.get("retryable") is True:
                        retryable_failure = True
                        continue
                    results[dataset_code] = _ProbeObservation(
                        dataset_code=dataset_code,
                        exchange=exchange,
                        command_id=command_id,
                        run_id=run_id,
                        source_snapshot=(),
                        source_batches=(),
                        error=normalized_error,
                    )
                if retryable_failure:
                    if retry_no >= _MAX_RETRIES:
                        raise EquityBackfillOrchestrationError(
                            "source proof exhausted retryable Provider failures"
                        )
                    retry_no += 1
                    retry_seed = f"{campaign_key}:{group_key}:{command_id}:{retry_no}"
                    digest = _hash_json(retry_seed)
                    retry_receipt = self._control_plane.retry_command(
                        request={
                            "submissionId": str(
                                uuid5(
                                    NAMESPACE_URL,
                                    f"quant-v2:eqbf-source-proof:{retry_seed}",
                                )
                            ),
                            "target": {
                                "resourceType": "COMMAND",
                                "resourceId": str(command_id),
                            },
                            "actor": {
                                "actorRef": "system:equity-backfill-source-proof",
                                "role": "SYSTEM",
                                "reason": "股票中心来源能力探测自动重试",
                            },
                        },
                        idempotency_key=f"eqbf-proof-retry:{digest}",
                        request_id=f"eqbf-proof-retry:{digest[:24]}",
                    )
                    command_id = UUID(str(retry_receipt["commandId"]))
                    continue
                expected_codes = {str(target["datasetCode"]) for target in targets}
                if set(results) != expected_codes:
                    raise EquityBackfillOrchestrationError(
                        "source proof result roster is incomplete"
                    )
                return tuple(results[code] for code in sorted(results))
            if time.monotonic() >= deadline:
                raise EquityBackfillPending(f"source proof command remains {status}: {group_key}")
            progressed = self._control_plane.dispatch_once(
                f"{worker_id}:source-proof:{_short_hash(group_key)}"
            )
            if not progressed:
                self._control_plane.reap_expired_slots()
                if self._poll_interval_seconds:
                    time.sleep(self._poll_interval_seconds)

    def _probe_observation(
        self,
        *,
        dataset_code: str,
        exchange: str | None,
        command_id: UUID,
        run_id: UUID,
        error: dict[str, Any] | None,
    ) -> _ProbeObservation:
        """读取成功 run 的实际 SourceBatch、来源快照和完整 schema 身份。"""
        with self._database.session() as session:
            run = session.get(DataOperationRun, run_id)
            source_ids = tuple(
                session.scalars(
                    select(DataOperationRunSourceBatch.source_batch_id)
                    .where(DataOperationRunSourceBatch.run_id == run_id)
                    .order_by(DataOperationRunSourceBatch.source_batch_id)
                ).all()
            )
            batches = tuple(
                session.scalars(
                    select(SourceBatch)
                    .where(SourceBatch.source_batch_id.in_(source_ids))
                    .order_by(SourceBatch.source_batch_id)
                ).all()
            )
            if (
                run is None
                or run.command_id != command_id
                or run.dataset_code != dataset_code
                or run.status != "SUCCEEDED"
                or not source_ids
                or len(batches) != len(source_ids)
            ):
                raise EquityBackfillOrchestrationError(
                    "successful source proof lacks actual SourceBatch linkage"
                )
            return _ProbeObservation(
                dataset_code=dataset_code,
                exchange=exchange,
                command_id=command_id,
                run_id=run_id,
                source_snapshot=tuple(run.source_snapshot),
                source_batches=batches,
                error=error,
            )

    def _freeze_sources(
        self,
        *,
        observations: tuple[_ProbeObservation, ...],
        snapshots: Mapping[str, list[dict[str, Any]]],
        identities: tuple[FrozenIdentity, ...],
    ) -> dict[str, FrozenSource]:
        """把真实探测事实和内部 executor 代码身份转换为完整来源合同。"""
        result: dict[str, FrozenSource] = {}
        grouped = {
            dataset_code: [
                observation
                for observation in observations
                if observation.dataset_code == dataset_code
            ]
            for dataset_code in (*_INSTRUMENT_SOURCE_DATASETS, *_GLOBAL_EVENT_DATASETS)
        }
        for dataset_code, dataset_observations in grouped.items():
            successful = [
                observation for observation in dataset_observations if observation.succeeded
            ]
            if not successful:
                raise EquityBackfillOrchestrationError(
                    f"source proof has no real success for {dataset_code}"
                )
            current_snapshot = tuple(snapshots[dataset_code])
            if any(observation.source_snapshot != current_snapshot for observation in successful):
                raise EquityBackfillOrchestrationError(
                    f"source binding changed during proof: {dataset_code}"
                )
            batches = tuple(
                batch for observation in successful for batch in observation.source_batches
            )
            signatures = {
                (
                    batch.provider_id,
                    batch.capability,
                    batch.upstream_source,
                    batch.adapter_version,
                    batch.schema_fingerprint,
                )
                for batch in batches
            }
            if len(signatures) != 1:
                raise EquityBackfillOrchestrationError(
                    f"source proof has no uniform adapter/schema contract: {dataset_code}"
                )
            (
                provider_id,
                capability,
                upstream_source,
                adapter_version,
                schema_fingerprint,
            ) = next(iter(signatures))
            binding = _matching_binding(
                current_snapshot,
                provider_id=provider_id,
                upstream_source=upstream_source,
                capability=capability,
            )
            evidence = {
                "datasetCode": dataset_code,
                "successfulRuns": [
                    {
                        "exchange": observation.exchange,
                        "commandId": str(observation.command_id),
                        "runId": str(observation.run_id),
                        "sourceBatches": [
                            {
                                "sourceBatchId": str(batch.source_batch_id),
                                "payloadSha256": batch.payload_sha256,
                                "observedAt": batch.observed_at.isoformat(),
                                "providerId": batch.provider_id,
                                "capability": batch.capability,
                                "upstreamSource": batch.upstream_source,
                                "adapterVersion": batch.adapter_version,
                                "schemaFingerprint": batch.schema_fingerprint,
                            }
                            for batch in observation.source_batches
                        ],
                    }
                    for observation in successful
                ],
                "unavailableExchanges": sorted(
                    observation.exchange
                    for observation in dataset_observations
                    if not observation.succeeded and observation.exchange is not None
                ),
            }
            source = FrozenSource(
                dataset_code=dataset_code,
                publication_dataset_code=_PUBLICATION_DATASETS.get(dataset_code, dataset_code),
                source_snapshot=current_snapshot,
                source_snapshot_hash=_hash_json(list(current_snapshot)),
                earliest_date=(_HISTORY_START if dataset_code in _HISTORICAL_DATASETS else None),
                earliest_date_method=(
                    "CONTROLLED_EXECUTOR_REQUEST_FLOOR_V1"
                    if dataset_code in _HISTORICAL_DATASETS
                    else "CURRENT_SNAPSHOT_ONLY"
                ),
                evidence_ref=(
                    f"data-operation-source-proof:{dataset_code}:{_short_hash(evidence)}"
                ),
                evidence_sha256=_hash_json(evidence),
                evidence_observed_at=max(batch.observed_at for batch in batches),
                expected_provider_id=provider_id,
                expected_capability=capability,
                expected_upstream_source=upstream_source,
                expected_adapter_version=adapter_version,
                expected_schema_fingerprint=schema_fingerprint,
                supported_exchanges=(
                    tuple(
                        sorted(
                            {
                                str(observation.exchange)
                                for observation in successful
                                if observation.exchange is not None
                            }
                        )
                    )
                    if dataset_code in _INSTRUMENT_SOURCE_DATASETS
                    else ()
                ),
                methodology_code=str(binding["methodologyCode"]),
                methodology_version=int(binding["methodologyVersion"]),
                mapping_version=str(binding["mappingCodeSha256"]),
                source_contract_hash="0" * 64,
            )
            result[dataset_code] = replace(
                source,
                source_contract_hash=source_contract_hash(source),
            )
        supported_internal_exchanges = tuple(sorted({identity.exchange for identity in identities}))
        for dataset_code in _INTERNAL_DATASETS:
            source_snapshot = tuple(snapshots[dataset_code])
            if len(source_snapshot) != 1:
                raise EquityBackfillOrchestrationError(
                    f"internal executor identity is ambiguous: {dataset_code}"
                )
            binding = source_snapshot[0]
            executor_code = str(binding.get("adapterId", "")).strip()
            code_hash = str(binding.get("codeSha256", "")).strip()
            if (
                binding.get("sourceKind") != "INTERNAL_EXECUTOR"
                or not executor_code
                or len(code_hash) != 64
            ):
                raise EquityBackfillOrchestrationError(
                    f"internal executor identity is invalid: {dataset_code}"
                )
            input_contract = (
                {
                    "binding": "ALL_TERMINAL_CHILD_RESULTS",
                    "referenceBundle": "EXACT_PLAN_BUNDLE",
                },
            )
            evidence = {
                "datasetCode": dataset_code,
                "executorCode": executor_code,
                "codeSha256": code_hash,
                "sourceSnapshot": list(source_snapshot),
            }
            source = FrozenSource(
                dataset_code=dataset_code,
                publication_dataset_code=dataset_code,
                source_snapshot=source_snapshot,
                source_snapshot_hash=_hash_json(list(source_snapshot)),
                earliest_date=None,
                earliest_date_method="INTERNAL_EXACT_INPUTS_ONLY",
                evidence_ref=(
                    f"internal-executor-source-proof:{dataset_code}:{_short_hash(evidence)}"
                ),
                evidence_sha256=_hash_json(evidence),
                evidence_observed_at=self._now(),
                expected_provider_id="platform",
                expected_capability=dataset_code,
                expected_upstream_source="platform-derived",
                expected_adapter_version=code_hash,
                expected_schema_fingerprint=_hash_json(
                    {
                        "datasetCode": dataset_code,
                        "executorCode": executor_code,
                        "codeSha256": code_hash,
                        "contractVersion": 1,
                    }
                ),
                supported_exchanges=supported_internal_exchanges,
                methodology_code=str(binding["methodologyCode"]),
                methodology_version=int(binding["methodologyVersion"]),
                mapping_version=str(binding["mappingCodeSha256"]),
                source_contract_hash="0" * 64,
                source_kind="INTERNAL_EXECUTOR",
                internal_executor_code=executor_code,
                input_contract=input_contract,
                input_contract_hash=_hash_json(list(input_contract)),
            )
            result[dataset_code] = replace(
                source,
                source_contract_hash=source_contract_hash(source),
            )
        if set(result) != _PLANNED_DATASETS:
            raise EquityBackfillOrchestrationError("frozen source manifest is incomplete")
        for source in result.values():
            source.validate()
        return result

    def _persist_plan_header(
        self,
        *,
        plan_id: UUID,
        campaign_key: str,
        reference_bundle: FrozenReferenceBundle,
        inputs: _ReferenceInputs,
        roster_hash: str,
        sources: Mapping[str, FrozenSource],
        topology: BackfillTopology,
    ) -> None:
        """先原子冻结父计划、身份和来源；child 分页随后可独立崩溃恢复。"""
        now = self._now()
        request_hash = _hash_json(
            {
                "campaignKey": campaign_key,
                "planVersion": 1,
                "referenceBundlePublicationId": str(reference_bundle.publication_id),
                "referenceBundleDataVersion": str(reference_bundle.data_version),
                "referenceManifestHash": reference_bundle.manifest_hash,
                "snapshotObservedOn": reference_bundle.snapshot_observed_on.isoformat(),
                "marketAsOf": reference_bundle.market_as_of.isoformat(),
            }
        )
        source_evidence_hash = _hash_json(
            [
                {
                    "datasetCode": code,
                    "sourceContractHash": sources[code].source_contract_hash,
                    "evidenceSha256": sources[code].evidence_sha256,
                }
                for code in sorted(sources)
            ]
        )
        with self._database.transaction() as session:
            session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": f"equity-backfill-plan:{campaign_key}"},
            )
            existing = session.scalar(
                select(EquityBackfillPlan).where(EquityBackfillPlan.campaign_key == campaign_key)
            )
            if existing is not None:
                if existing.plan_id != plan_id or existing.request_hash != request_hash:
                    raise EquityBackfillOrchestrationError(
                        "campaign key is already bound to another frozen request"
                    )
                return
            session.add(
                EquityBackfillPlan(
                    plan_id=plan_id,
                    campaign_key=campaign_key,
                    plan_version=1,
                    request_hash=request_hash,
                    aggregate_publication_id=inputs.aggregate.publication_id,
                    aggregate_data_version=inputs.aggregate.data_version,
                    aggregate_components_json=list(inputs.aggregate_components),
                    lifecycle_publications_json=list(inputs.lifecycle_publications),
                    reference_bundle_publication_id=reference_bundle.publication_id,
                    reference_bundle_data_version=reference_bundle.data_version,
                    reference_manifest_json=list(reference_bundle.manifest),
                    reference_manifest_hash=reference_bundle.manifest_hash,
                    snapshot_observed_on=reference_bundle.snapshot_observed_on,
                    market_as_of=reference_bundle.market_as_of,
                    known_at=inputs.known_at,
                    roster_hash=roster_hash,
                    roster_count=len(inputs.identities),
                    source_evidence_hash=source_evidence_hash,
                    exclusions_json=list(topology.exclusions),
                    child_count=len(topology.children),
                    created_at=now,
                )
            )
            session.add(
                EquityBackfillPlanState(
                    plan_id=plan_id,
                    status="BUILDING",
                    current_phase=None,
                    revision=1,
                    last_error_json=None,
                    audit_summary_json=None,
                    updated_at=now,
                    completed_at=None,
                )
            )
            session.add_all(
                [
                    EquityBackfillPlanIdentity(
                        plan_id=plan_id,
                        ordinal=identity.ordinal,
                        identifier_version_id=identity.identifier_version_id,
                        security_id=identity.security_id,
                        instrument_id=identity.instrument_id,
                        exchange=identity.exchange,
                        symbol=identity.symbol,
                        effective_from=identity.effective_from,
                        effective_to=identity.effective_to,
                        known_from=identity.known_from,
                        known_to=identity.known_to,
                        effective_date_precision=identity.effective_date_precision,
                    )
                    for identity in inputs.identities
                ]
            )
            session.add_all(
                [_source_row(plan_id=plan_id, source=sources[code]) for code in sorted(sources)]
            )

    def _persist_pages_and_seal(
        self,
        plan_id: UUID,
        topology: BackfillTopology,
    ) -> None:
        """逐页追加 child 与 HELD 状态，最后才写 seal 并把父状态推进到 HELD。"""
        pages = tuple(iter_topology_pages(topology))
        now = self._now()
        for page in pages:
            with self._database.transaction() as session:
                existing = session.get(
                    EquityBackfillPlanPage,
                    {
                        "plan_id": plan_id,
                        "page_number": page.page_number,
                    },
                )
                if existing is not None:
                    if (
                        existing.first_ordinal != page.first_ordinal
                        or existing.last_ordinal != page.last_ordinal
                        or existing.child_count != len(page.children)
                        or existing.payload_bytes != page.payload_bytes
                        or existing.page_hash != page.page_hash
                    ):
                        raise EquityBackfillOrchestrationError(
                            "persisted equity backfill page differs from topology"
                        )
                    continue
                existing_children = int(
                    session.scalar(
                        select(func.count())
                        .select_from(EquityBackfillChildSpec)
                        .where(
                            EquityBackfillChildSpec.plan_id == plan_id,
                            EquityBackfillChildSpec.ordinal >= page.first_ordinal,
                            EquityBackfillChildSpec.ordinal <= page.last_ordinal,
                        )
                    )
                    or 0
                )
                if existing_children:
                    raise EquityBackfillOrchestrationError(
                        "equity backfill page has orphan child specifications"
                    )
                session.add_all(
                    [
                        EquityBackfillChildSpec(
                            child_id=child.child_id,
                            plan_id=plan_id,
                            ordinal=child.ordinal,
                            phase=child.phase,
                            requirement=child.requirement,
                            child_key=child.child_key,
                            identity_ordinal=child.identity_ordinal,
                            window_from=child.window_from,
                            window_to=child.window_to,
                            targets_json=list(child.targets),
                            intents_json=list(child.intents),
                            dependency_keys_json=list(child.dependency_keys),
                            completion_dependency_keys_json=list(child.completion_dependency_keys),
                            source_hashes_json=child.source_hashes,
                            submission_id=child.submission_id,
                            request_prefix=child.request_prefix,
                            target_count=len(child.targets),
                            created_at=now,
                        )
                        for child in page.children
                    ]
                )
                session.add_all(
                    [
                        EquityBackfillChildState(
                            child_id=child.child_id,
                            status="HELD",
                            command_id=None,
                            resume_count=0,
                            last_error_json=None,
                            audit_json=None,
                            submitted_at=None,
                            finished_at=None,
                            updated_at=now,
                        )
                        for child in page.children
                    ]
                )
                session.add(
                    EquityBackfillPlanPage(
                        plan_id=plan_id,
                        page_number=page.page_number,
                        first_ordinal=page.first_ordinal,
                        last_ordinal=page.last_ordinal,
                        child_count=len(page.children),
                        payload_bytes=page.payload_bytes,
                        page_hash=page.page_hash,
                        created_at=now,
                    )
                )
        seal_value = compute_topology_seal(topology)
        with self._database.transaction() as session:
            plan = session.get(EquityBackfillPlan, plan_id)
            state = session.get(
                EquityBackfillPlanState,
                plan_id,
                with_for_update=True,
            )
            if plan is None or state is None:
                raise EquityBackfillOrchestrationError(
                    "equity backfill header disappeared before seal"
                )
            existing_seal = session.get(EquityBackfillPlanSeal, plan_id)
            if existing_seal is None:
                session.add(
                    EquityBackfillPlanSeal(
                        plan_id=plan_id,
                        page_count=seal_value.page_count,
                        child_count=seal_value.child_count,
                        topology_hash=seal_value.topology_hash,
                        page_roster_hash=seal_value.page_roster_hash,
                        sealed_at=self._now(),
                    )
                )
            elif (
                existing_seal.page_count != seal_value.page_count
                or existing_seal.child_count != seal_value.child_count
                or existing_seal.topology_hash != seal_value.topology_hash
                or existing_seal.page_roster_hash != seal_value.page_roster_hash
            ):
                raise EquityBackfillOrchestrationError(
                    "persisted equity backfill seal differs from topology"
                )
            if state.status == "BUILDING":
                state.status = "HELD"
                state.current_phase = None
                state.revision += 1
                state.updated_at = self._now()

    def _rehydrate_topology(self, plan_id: UUID) -> BackfillTopology:
        """从冻结 header、身份和来源重建确定性拓扑，供缺页恢复与 seal 复验。"""
        with self._database.session() as session:
            plan = session.get(EquityBackfillPlan, plan_id)
            if plan is None:
                raise EquityBackfillOrchestrationError("equity backfill plan is missing")
            bundle_publication = session.get(
                DatasetPublication,
                plan.reference_bundle_publication_id,
            )
            if bundle_publication is None or bundle_publication.release_id is None:
                raise EquityBackfillOrchestrationError("frozen reference bundle release is missing")
            bundle = FrozenReferenceBundle(
                publication_id=plan.reference_bundle_publication_id,
                data_version=plan.reference_bundle_data_version,
                release_id=bundle_publication.release_id,
                snapshot_observed_on=plan.snapshot_observed_on,
                market_as_of=plan.market_as_of,
                manifest=tuple(plan.reference_manifest_json),
                manifest_hash=plan.reference_manifest_hash,
            )
            identity_rows = tuple(
                session.scalars(
                    select(EquityBackfillPlanIdentity)
                    .where(EquityBackfillPlanIdentity.plan_id == plan_id)
                    .order_by(EquityBackfillPlanIdentity.ordinal)
                ).all()
            )
            source_rows = tuple(
                session.scalars(
                    select(EquityBackfillPlanSource)
                    .where(EquityBackfillPlanSource.plan_id == plan_id)
                    .order_by(EquityBackfillPlanSource.dataset_code)
                ).all()
            )
            identities = tuple(_frozen_identity(row) for row in identity_rows)
            sources = {row.dataset_code: _frozen_source(row) for row in source_rows}
            if (
                len(identities) != plan.roster_count
                or compute_roster_hash(identities) != plan.roster_hash
                or _hash_json(
                    [
                        {
                            "datasetCode": code,
                            "sourceContractHash": sources[code].source_contract_hash,
                            "evidenceSha256": sources[code].evidence_sha256,
                        }
                        for code in sorted(sources)
                    ]
                )
                != plan.source_evidence_hash
            ):
                raise EquityBackfillOrchestrationError(
                    "frozen equity backfill header evidence changed"
                )
        return build_topology(
            plan_id=plan_id,
            snapshot_observed_on=plan.snapshot_observed_on,
            market_as_of=plan.market_as_of,
            known_at=plan.known_at,
            roster_hash=plan.roster_hash,
            identities=identities,
            sources=sources,
            reference_bundle=bundle,
        )

    def _activate_when_idle(
        self,
        *,
        plan_id: UUID,
        worker_id: str,
        deadline: float,
    ) -> None:
        """等待当前持槽 run 完成，再原子激活独占计划；普通排队命令不会在期间执行。"""
        while True:
            with self._database.transaction() as session:
                state = session.get(
                    EquityBackfillPlanState,
                    plan_id,
                    with_for_update=True,
                )
                if state is None:
                    raise EquityBackfillOrchestrationError("equity backfill state is missing")
                if state.status in {
                    "RUNNING",
                    "SUCCEEDED",
                    "PARTIAL",
                    "FAILED",
                    "BLOCKED",
                }:
                    return
                if state.status != "HELD":
                    raise EquityBackfillOrchestrationError("equity backfill plan is not sealed")
                other = session.scalar(
                    select(EquityBackfillPlanState.plan_id).where(
                        EquityBackfillPlanState.status == "RUNNING",
                        EquityBackfillPlanState.plan_id != plan_id,
                    )
                )
                if other is not None:
                    raise EquityBackfillPending(
                        "another equity backfill plan owns the exclusive publication window"
                    )
                slot = session.get(
                    DataOperationExecutionSlot,
                    "global",
                    with_for_update=True,
                )
                active_runs = int(
                    session.scalar(
                        select(func.count())
                        .select_from(DataOperationRun)
                        .where(DataOperationRun.status.in_(("RUNNING", "CANCEL_REQUESTED")))
                    )
                    or 0
                )
                if (slot is None or slot.state == "IDLE") and active_runs == 0:
                    state.status = "RUNNING"
                    state.current_phase = PHASES[0]
                    state.revision += 1
                    state.updated_at = self._now()
                    return
            if time.monotonic() >= deadline:
                raise EquityBackfillPending("equity backfill waits for the current fenced run")
            progressed = self._control_plane.dispatch_once(f"{worker_id}:equity-backfill-drain")
            if not progressed:
                self._control_plane.reap_expired_slots()
                if self._poll_interval_seconds:
                    time.sleep(self._poll_interval_seconds)

    def _recover_submitting(self, plan_id: UUID, limit: int) -> None:
        """提交已标为 SUBMITTING 的稳定 child，网络未知结果由幂等键原样恢复。"""
        with self._database.session() as session:
            state = session.get(EquityBackfillPlanState, plan_id)
            if state is None or state.status != "RUNNING" or state.current_phase is None:
                return
            children = tuple(
                session.scalars(
                    select(EquityBackfillChildSpec)
                    .join(
                        EquityBackfillChildState,
                        EquityBackfillChildState.child_id == EquityBackfillChildSpec.child_id,
                    )
                    .where(
                        EquityBackfillChildSpec.plan_id == plan_id,
                        EquityBackfillChildSpec.phase == state.current_phase,
                        EquityBackfillChildState.status == "SUBMITTING",
                    )
                    .order_by(EquityBackfillChildSpec.ordinal)
                    .limit(limit)
                ).all()
            )
        for child in children:
            try:
                submit_system_command_group(
                    self._control_plane,
                    targets=child.targets_json,
                    intents=child.intents_json,
                    reason="股票中心冻结父计划分阶段全量回填",
                    request_prefix=child.request_prefix,
                )
            except OperationProblem as error:
                self._block_submission_problem(child.child_id, error)

    def _prepare_phase_children(self, plan_id: UUID, maximum_inflight: int) -> None:
        """在当前阶段有界选择 HELD child，并按直接依赖决定 SUBMITTING 或 BLOCKED。"""
        now = self._now()
        with self._database.transaction() as session:
            plan_state = session.get(
                EquityBackfillPlanState,
                plan_id,
                with_for_update=True,
            )
            if (
                plan_state is None
                or plan_state.status != "RUNNING"
                or plan_state.current_phase is None
            ):
                return
            active_count = int(
                session.scalar(
                    select(func.count())
                    .select_from(EquityBackfillChildState)
                    .join(
                        EquityBackfillChildSpec,
                        EquityBackfillChildSpec.child_id == EquityBackfillChildState.child_id,
                    )
                    .where(
                        EquityBackfillChildSpec.plan_id == plan_id,
                        EquityBackfillChildSpec.phase == plan_state.current_phase,
                        EquityBackfillChildState.status.in_(("SUBMITTING", "SUBMITTED", "RUNNING")),
                    )
                )
                or 0
            )
            capacity = max(0, maximum_inflight - active_count)
            if capacity == 0:
                return
            children = tuple(
                session.scalars(
                    select(EquityBackfillChildSpec)
                    .join(
                        EquityBackfillChildState,
                        EquityBackfillChildState.child_id == EquityBackfillChildSpec.child_id,
                    )
                    .where(
                        EquityBackfillChildSpec.plan_id == plan_id,
                        EquityBackfillChildSpec.phase == plan_state.current_phase,
                        EquityBackfillChildState.status == "HELD",
                    )
                    .order_by(EquityBackfillChildSpec.ordinal)
                    .limit(capacity)
                    .with_for_update(skip_locked=True)
                ).all()
            )
            dependency_keys = {key for child in children for key in child.dependency_keys_json}
            dependency_states = (
                {
                    child_key: status
                    for child_key, status in session.execute(
                        select(
                            EquityBackfillChildSpec.child_key,
                            EquityBackfillChildState.status,
                        )
                        .join(
                            EquityBackfillChildState,
                            EquityBackfillChildState.child_id == EquityBackfillChildSpec.child_id,
                        )
                        .where(
                            EquityBackfillChildSpec.plan_id == plan_id,
                            EquityBackfillChildSpec.child_key.in_(dependency_keys),
                        )
                    ).all()
                }
                if dependency_keys
                else {}
            )
            for child in children:
                child_state = session.get(
                    EquityBackfillChildState,
                    child.child_id,
                    with_for_update=True,
                )
                if child_state is None or child_state.status != "HELD":
                    continue
                failed_dependencies = [
                    key
                    for key in child.dependency_keys_json
                    if dependency_states.get(key) != "SUCCEEDED"
                ]
                if failed_dependencies:
                    child_state.status = "BLOCKED"
                    child_state.last_error_json = {
                        "code": "equity-backfill-dependency-unsatisfied",
                        "stage": "DEPENDENCY",
                        "retryable": False,
                        "dependencyKeys": sorted(failed_dependencies),
                    }
                    child_state.finished_at = now
                else:
                    child_state.status = "SUBMITTING"
                child_state.updated_at = now

    def _retry_children(self, plan_id: UUID) -> None:
        """只重试当前阶段全部失败 run 明确标记 retryable 的 child，最多三次。"""
        with self._database.session() as session:
            plan_state = session.get(EquityBackfillPlanState, plan_id)
            if (
                plan_state is None
                or plan_state.status != "RUNNING"
                or plan_state.current_phase is None
            ):
                return
            values = tuple(
                session.execute(
                    select(EquityBackfillChildSpec, EquityBackfillChildState)
                    .join(
                        EquityBackfillChildState,
                        EquityBackfillChildState.child_id == EquityBackfillChildSpec.child_id,
                    )
                    .where(
                        EquityBackfillChildSpec.plan_id == plan_id,
                        EquityBackfillChildSpec.phase == plan_state.current_phase,
                        EquityBackfillChildState.status.in_(("PARTIAL", "FAILED", "CANCELLED")),
                        EquityBackfillChildState.resume_count < _MAX_RETRIES,
                    )
                    .order_by(EquityBackfillChildSpec.ordinal)
                    .limit(16)
                ).all()
            )
        for child, state in values:
            if state.command_id is None or not _retryable_child_error(state.last_error_json):
                continue
            retry_no = state.resume_count + 1
            digest = _hash_json(
                {
                    "childId": str(child.child_id),
                    "commandId": str(state.command_id),
                    "retryNo": retry_no,
                }
            )
            try:
                self._control_plane.retry_command(
                    request={
                        "submissionId": str(child.submission_id),
                        "target": {
                            "resourceType": "COMMAND",
                            "resourceId": str(state.command_id),
                        },
                        "actor": {
                            "actorRef": "system:equity-backfill",
                            "role": "SYSTEM",
                            "reason": "股票中心全量回填自动重试",
                        },
                    },
                    idempotency_key=f"eqbf-retry:{digest}",
                    request_id=f"eqbf-retry:{digest[:24]}",
                )
            except OperationProblem as error:
                self._block_submission_problem(child.child_id, error)

    def _advance_phase_if_terminal(self, plan_id: UUID) -> bool:
        """当前阶段全部终止时推进下一阶段，或按硬门与可选失败形成真实父终态。"""
        now = self._now()
        with self._database.transaction() as session:
            plan = session.get(EquityBackfillPlan, plan_id)
            state = session.get(
                EquityBackfillPlanState,
                plan_id,
                with_for_update=True,
            )
            if (
                plan is None
                or state is None
                or state.status != "RUNNING"
                or state.current_phase is None
            ):
                return False
            rows = tuple(
                session.execute(
                    select(EquityBackfillChildSpec, EquityBackfillChildState)
                    .join(
                        EquityBackfillChildState,
                        EquityBackfillChildState.child_id == EquityBackfillChildSpec.child_id,
                    )
                    .where(
                        EquityBackfillChildSpec.plan_id == plan_id,
                        EquityBackfillChildSpec.phase == state.current_phase,
                    )
                    .order_by(EquityBackfillChildSpec.ordinal)
                ).all()
            )
            if any(child_state.status not in _TERMINAL_CHILDREN for _, child_state in rows):
                return False
            hard_failures = [
                (spec, child_state)
                for spec, child_state in rows
                if spec.requirement in {"BASE_REQUIRED", "FINAL_REQUIRED"}
                and child_state.status != "SUCCEEDED"
            ]
            if hard_failures:
                state.status = "FAILED"
                state.last_error_json = {
                    "code": "equity-backfill-required-child-failed",
                    "stage": state.current_phase,
                    "retryable": False,
                    "childKeys": [spec.child_key for spec, _ in hard_failures],
                }
                state.audit_summary_json = self._audit_summary(session, plan)
                state.completed_at = now
                state.updated_at = now
                state.revision += 1
                return True
            phase_index = PHASES.index(state.current_phase)
            if phase_index + 1 < len(PHASES):
                state.current_phase = PHASES[phase_index + 1]
                state.revision += 1
                state.updated_at = now
                return True
            all_rows = tuple(
                session.execute(
                    select(EquityBackfillChildSpec, EquityBackfillChildState)
                    .join(
                        EquityBackfillChildState,
                        EquityBackfillChildState.child_id == EquityBackfillChildSpec.child_id,
                    )
                    .where(EquityBackfillChildSpec.plan_id == plan_id)
                ).all()
            )
            optional_failures = [
                child_state
                for spec, child_state in all_rows
                if spec.requirement == "OPTIONAL" and child_state.status != "SUCCEEDED"
            ]
            state.status = "PARTIAL" if optional_failures else "SUCCEEDED"
            state.current_phase = None
            state.last_error_json = None
            state.audit_summary_json = self._audit_summary(session, plan)
            state.completed_at = now
            state.updated_at = now
            state.revision += 1
            return True

    def _audit_summary(
        self,
        session: Any,
        plan: EquityBackfillPlan,
    ) -> dict[str, Any]:
        """聚合 child 终态、来源合同和显式排除数量，不把排除项计作成功。"""
        counts = {
            str(status): int(count)
            for status, count in session.execute(
                select(EquityBackfillChildState.status, func.count())
                .join(
                    EquityBackfillChildSpec,
                    EquityBackfillChildSpec.child_id == EquityBackfillChildState.child_id,
                )
                .where(EquityBackfillChildSpec.plan_id == plan.plan_id)
                .group_by(EquityBackfillChildState.status)
            ).all()
        }
        return {
            "childCount": plan.child_count,
            "childStatusCounts": counts,
            "exclusionCount": len(plan.exclusions_json),
            "rosterCount": plan.roster_count,
            "rosterHash": plan.roster_hash,
            "referenceManifestHash": plan.reference_manifest_hash,
            "sourceEvidenceHash": plan.source_evidence_hash,
        }

    def _block_submission_problem(
        self,
        child_id: UUID,
        error: OperationProblem,
    ) -> None:
        """把不可受理 child 明确收敛为 BLOCKED，使可选能力不会永久卡住阶段。"""
        now = self._now()
        with self._database.transaction() as session:
            state = session.get(
                EquityBackfillChildState,
                child_id,
                with_for_update=True,
            )
            if state is None or state.status in _TERMINAL_CHILDREN:
                return
            state.status = "BLOCKED"
            state.last_error_json = {
                "code": error.code,
                "stage": "SUBMISSION",
                "retryable": False,
                "status": error.status,
            }
            state.finished_at = now
            state.updated_at = now


def _campaign_key(value: str) -> str:
    """规范并校验运维稳定批次键。"""
    normalized = value.strip()
    if not normalized or len(normalized) > 128:
        raise ValueError("equity backfill campaign key is invalid")
    return normalized


def _instrument_scope(value: tuple[str, str] | None) -> tuple[str, str] | None:
    """校验真实烟测的交易所和六位证券代码，不接受可被 Provider 重解释的自由文本。"""
    if value is None:
        return None
    if not isinstance(value, tuple) or len(value) != 2:
        raise ValueError("equity backfill instrument scope is invalid")
    exchange, symbol = (str(part).strip().upper() for part in value)
    if exchange not in {"SSE", "SZSE", "BSE"} or (
        len(symbol) != 6 or not symbol.isascii() or not symbol.isdecimal()
    ):
        raise ValueError("equity backfill instrument scope is invalid")
    return exchange, symbol


def _scoped_campaign_key(
    campaign_key: str,
    *,
    instrument_scope: tuple[str, str] | None,
) -> str:
    """为真实单证券烟测派生独立稳定 campaign key，防止复用全市场计划。"""
    if instrument_scope is None:
        return campaign_key
    exchange, symbol = instrument_scope
    suffix = f":smoke:{exchange}.{symbol}"
    candidate = f"{campaign_key}{suffix}"
    if len(candidate) <= 128:
        return candidate
    digest = hashlib.sha256(candidate.encode()).hexdigest()[:24]
    return f"{campaign_key[:95]}:smoke:{digest}"


def _reference_campaign_key(campaign_key: str) -> str:
    """派生不超过账本上限的引用生成批次键，长全市场键也可自动恢复。"""
    suffix = ":reference"
    candidate = f"{campaign_key}{suffix}"
    if len(candidate) <= 128:
        return candidate
    digest = hashlib.sha256(candidate.encode()).hexdigest()[:24]
    return f"{campaign_key[:93]}:ref:{digest}"


def _scoped_reference_inputs(
    inputs: _ReferenceInputs,
    *,
    instrument_scope: tuple[str, str],
) -> _ReferenceInputs:
    """只从已封存主目录挑选一个精确身份，重新编号后生成最小真实历史 roster。"""
    exchange, symbol = instrument_scope
    matched = [
        identity
        for identity in inputs.identities
        if identity.exchange == exchange and identity.symbol == symbol
    ]
    if len(matched) != 1:
        raise EquityBackfillOrchestrationError(
            "instrument scope is not exactly one confirmed identity in the sealed master"
        )
    return replace(inputs, identities=(replace(matched[0], ordinal=1),))


def _instrument_probe_target(
    dataset_code: str,
    *,
    identity: FrozenIdentity,
    market_as_of: date,
) -> dict[str, Any]:
    """构造只产生真实数据/空覆盖的单证券小范围来源探测目标。"""
    mode = (
        "FULL"
        if dataset_code
        in {
            "equity.adjustment_factor",
            "equity.profile",
            "equity.share_capital.reported",
            "financial.report",
            "financial.provider-metric",
            "financial.valuation",
        }
        else "DATE_RANGE"
    )
    return {
        "datasetCode": dataset_code,
        "mode": mode,
        "selector": {
            "kind": "INSTRUMENT",
            "exchange": identity.exchange,
            "symbol": identity.symbol,
        },
        "dateFrom": market_as_of.isoformat() if mode == "DATE_RANGE" else None,
        "dateTo": market_as_of.isoformat() if mode == "DATE_RANGE" else None,
        "observationDate": None,
    }


def _event_probe_target(
    dataset_code: str,
    *,
    market_as_of: date,
) -> dict[str, Any]:
    """构造一天全市场事件探测；合法空窗仍必须形成真实 coverage publication。"""
    selector = (
        {"kind": "GLOBAL"}
        if dataset_code == "equity.corporate_event.earnings.reported"
        else {
            "kind": "TRADING_EVENT",
            "operation": (
                "DRAGON_TIGER"
                if dataset_code == "equity.dragon_tiger.disclosure.reported"
                else "BLOCK_TRADE"
            ),
        }
    )
    return {
        "datasetCode": dataset_code,
        "mode": "DATE_RANGE",
        "selector": selector,
        "dateFrom": market_as_of.isoformat(),
        "dateTo": market_as_of.isoformat(),
        "observationDate": None,
    }


def _matching_binding(
    snapshot: tuple[dict[str, Any], ...],
    *,
    provider_id: str,
    upstream_source: str,
    capability: str,
) -> dict[str, Any]:
    """定位与实际 SourceBatch 完全一致的唯一有效控制面来源绑定。"""
    values = [
        binding
        for binding in snapshot
        if binding.get("providerId") == provider_id
        and binding.get("upstreamSource") == upstream_source
        and binding.get("sourceDataset") == capability
        and binding.get("effective") is True
        and isinstance(binding.get("mappingCodeSha256"), str)
    ]
    if len(values) != 1:
        raise EquityBackfillOrchestrationError(
            "actual SourceBatch has no unique effective control-plane binding"
        )
    return values[0]


def _publication_manifest(
    publication: DatasetPublication,
    *,
    exchange: str,
) -> dict[str, Any]:
    """投影计划需要的 publication 身份、业务日期和知识截止。"""
    return {
        "exchange": exchange,
        "publicationId": str(publication.publication_id),
        "dataVersion": str(publication.data_version),
        "effectiveAsOf": (
            None if publication.effective_as_of is None else publication.effective_as_of.isoformat()
        ),
        "knowledgeCutoff": (
            None
            if publication.knowledge_cutoff is None
            else publication.knowledge_cutoff.isoformat()
        ),
    }


def _source_row(
    *,
    plan_id: UUID,
    source: FrozenSource,
) -> EquityBackfillPlanSource:
    """把强类型来源合同逐字段写入不可变 ORM 行。"""
    return EquityBackfillPlanSource(
        plan_id=plan_id,
        dataset_code=source.dataset_code,
        source_kind=source.source_kind,
        publication_dataset_code=source.publication_dataset_code,
        source_snapshot_json=list(source.source_snapshot),
        source_snapshot_hash=source.source_snapshot_hash,
        earliest_date=source.earliest_date,
        earliest_date_method=source.earliest_date_method,
        evidence_ref=source.evidence_ref,
        evidence_sha256=source.evidence_sha256,
        source_contract_hash=source.source_contract_hash,
        evidence_observed_at=source.evidence_observed_at,
        expected_provider_id=source.expected_provider_id,
        expected_capability=source.expected_capability,
        expected_upstream_source=source.expected_upstream_source,
        expected_adapter_version=source.expected_adapter_version,
        expected_schema_fingerprint=source.expected_schema_fingerprint,
        supported_exchanges_json=list(source.supported_exchanges),
        internal_executor_code=source.internal_executor_code,
        input_contract_json=list(source.input_contract),
        input_contract_hash=source.input_contract_hash,
        methodology_code=source.methodology_code,
        methodology_version=source.methodology_version,
        mapping_version=source.mapping_version,
    )


def _frozen_identity(row: EquityBackfillPlanIdentity) -> FrozenIdentity:
    """把不可变身份 ORM 行恢复为拓扑值对象。"""
    return FrozenIdentity(
        ordinal=row.ordinal,
        identifier_version_id=row.identifier_version_id,
        security_id=row.security_id,
        instrument_id=row.instrument_id,
        exchange=row.exchange,
        symbol=row.symbol,
        effective_from=row.effective_from,
        effective_to=row.effective_to,
        known_from=row.known_from,
        known_to=row.known_to,
        effective_date_precision=row.effective_date_precision,
    )


def _frozen_source(row: EquityBackfillPlanSource) -> FrozenSource:
    """把不可变来源 ORM 行恢复为共享校验使用的强类型合同。"""
    source = FrozenSource(
        dataset_code=row.dataset_code,
        publication_dataset_code=row.publication_dataset_code,
        source_snapshot=tuple(row.source_snapshot_json),
        source_snapshot_hash=row.source_snapshot_hash,
        earliest_date=row.earliest_date,
        earliest_date_method=row.earliest_date_method,
        evidence_ref=row.evidence_ref,
        evidence_sha256=row.evidence_sha256,
        evidence_observed_at=row.evidence_observed_at,
        expected_provider_id=row.expected_provider_id,
        expected_capability=row.expected_capability,
        expected_upstream_source=row.expected_upstream_source,
        expected_adapter_version=row.expected_adapter_version,
        expected_schema_fingerprint=row.expected_schema_fingerprint,
        supported_exchanges=tuple(row.supported_exchanges_json),
        methodology_code=row.methodology_code,
        methodology_version=row.methodology_version,
        mapping_version=row.mapping_version,
        source_contract_hash=row.source_contract_hash,
        source_kind=row.source_kind,
        internal_executor_code=row.internal_executor_code,
        input_contract=tuple(row.input_contract_json),
        input_contract_hash=row.input_contract_hash,
    )
    source.validate()
    return source


def _retryable_child_error(value: dict[str, Any] | None) -> bool:
    """只有全部失败 run 都显式标记 retryable 时才自动复制完整 child。"""
    if not isinstance(value, dict):
        return False
    run_errors = value.get("runErrors")
    return (
        isinstance(run_errors, list)
        and bool(run_errors)
        and all(
            isinstance(item, dict)
            and isinstance(item.get("error"), dict)
            and item["error"].get("retryable") is True
            for item in run_errors
        )
    )


def _short_hash(value: object) -> str:
    """返回稳定十二位摘要，限制控制面请求前缀长度。"""
    return _hash_json(value)[:12]


def _hash_json(value: object) -> str:
    """计算 UTF-8 规范 JSON SHA-256。"""
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
