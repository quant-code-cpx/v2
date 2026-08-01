"""持久化股票全量回填长历史分区，并从完整 seal 恢复执行。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from service_data_sync.infrastructure.data_operations.control_plane import ExecutionClaim
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.database.fenced_execution import FencedExecution
from service_data_sync.infrastructure.database.models.canonical import DatasetRelease
from service_data_sync.infrastructure.database.models.equity.backfill import (
    EquityBackfillChildSpec,
    EquityBackfillPartitionCheckpoint,
)
from service_data_sync.infrastructure.database.models.operations import (
    DataOperationCommand,
    DataOperationRun,
)
from service_data_sync.infrastructure.database.models.publication.dataset_publication import (
    DatasetPublication,
)
from service_data_sync.infrastructure.database.models.publication.equity_bar_window_coverage import (  # noqa: E501
    EquityBarWindowCoverage,
)
from service_data_sync.infrastructure.database.models.publication.equity_event_window_coverage import (  # noqa: E501
    EquityEventWindowCoverage,
)

_EVENT_FAMILIES = {
    "equity.corporate_action": ("CORPORATE_ACTION",),
    "equity.corporate_event.earnings.reported": (
        "EARNINGS_FORECAST",
        "EARNINGS_EXPRESS",
    ),
    "equity.dragon_tiger.disclosure.reported": ("DRAGON_TIGER",),
    "equity.block_trade.execution.reported": ("BLOCK_TRADE",),
}


def equity_backfill_partition_key(
    *,
    dataset_code: str,
    exchange: str,
    symbol: str,
    window_from: date,
    window_to: date,
) -> str:
    """生成不受重试 run UUID 影响的证券窗口稳定分区键。"""
    return f"{dataset_code}:{exchange}:{symbol}:{window_from.isoformat()}:{window_to.isoformat()}"


def completed_equity_bar_partitions(
    database: DatabaseClient,
    *,
    claim: ExecutionClaim,
    expected_partition_keys: frozenset[str],
) -> frozenset[str]:
    """读取同一冻结 child target 的成功水位，并拒绝任何额外或跨数据集分区。"""
    binding = _binding(database, claim=claim)
    target_index = _target_index(claim)
    with database.session() as session:
        checkpoints = session.scalars(
            select(EquityBackfillPartitionCheckpoint).where(
                EquityBackfillPartitionCheckpoint.child_id == binding.child_id,
                EquityBackfillPartitionCheckpoint.target_index == target_index,
            )
        ).all()
    actual = {checkpoint.partition_key for checkpoint in checkpoints}
    if actual - expected_partition_keys or any(
        checkpoint.dataset_code != claim.dataset_code for checkpoint in checkpoints
    ):
        raise RuntimeError("equity backfill partition roster differs from frozen child")
    return frozenset(actual)


def record_equity_bar_partition(
    database: DatabaseClient,
    *,
    claim: ExecutionClaim,
    execution: FencedExecution,
    partition_key: str,
    window_from: date,
    window_to: date,
    coverage_version: UUID,
    data_version: UUID,
    source_batch_ids: Sequence[UUID],
    publication_kind: str,
    record_count: int,
) -> None:
    """在独立 fenced 事务中封印一个已提交行情 coverage，崩溃重放只会追加新事实。"""
    binding = _binding(database, claim=claim)
    target_index = _target_index(claim)
    normalized_source_ids = tuple(sorted(set(source_batch_ids), key=str))
    if not normalized_source_ids:
        raise RuntimeError("equity backfill partition has no exact source batch")
    intent = claim.execution_intent
    identity = None if intent is None else intent.get("identity")
    selector = claim.target.get("selector")
    expected_period = {
        "equity.bar.1d.raw": "1d",
        "equity.bar.1w.raw": "1w",
        "equity.bar.1mo.raw": "1mo",
    }.get(claim.dataset_code)
    now = datetime.now(UTC)
    with database.transaction() as session:
        execution.assert_current(session)
        run = session.get(DataOperationRun, claim.run_id, with_for_update=True)
        coverage = session.scalar(
            select(EquityBarWindowCoverage).where(
                EquityBarWindowCoverage.coverage_version == coverage_version
            )
        )
        if (
            run is None
            or run.fencing_token != claim.fencing_token
            or run.dataset_code != claim.dataset_code
            or run.target_index != target_index
            or coverage is None
            or expected_period is None
            or not isinstance(identity, dict)
            or not isinstance(selector, dict)
            or coverage.capability != claim.dataset_code
            or coverage.period != expected_period
            or coverage.security_id != int(identity["securityId"])
            or coverage.identifier_version_id != UUID(str(identity["identifierVersionId"]))
            or selector.get("kind") != "INSTRUMENT"
            or selector.get("exchange") != identity.get("exchange")
            or selector.get("symbol") != identity.get("symbol")
            or coverage.coverage_from != window_from
            or coverage.coverage_to != window_to
        ):
            raise RuntimeError("equity backfill partition publication binding is invalid")
        publication = session.get(DatasetPublication, coverage.publication_id)
        release = (
            None
            if publication is None or publication.release_id is None
            else session.get(DatasetRelease, publication.release_id)
        )
        if (
            publication is None
            or release is None
            or publication.dataset != claim.dataset_code
            or publication.data_version != data_version
            # 不能只相信 `publication_id` 的间接关联：checkpoint 是跨服务读取的不可变
            # 证据，必须在应用层再次证明 coverage 自己冻结的版本与调用结果、publication 相同。
            or coverage.data_version != data_version
            or coverage.data_version != publication.data_version
            or publication.quality_status != "passed"
            or coverage.source_batch_id not in normalized_source_ids
            or coverage.publication_kind != publication_kind
            or coverage.record_count != record_count
        ):
            raise RuntimeError("equity backfill partition output cannot be verified")
        source_batch_values = [str(value) for value in normalized_source_ids]
        output = {
            "datasetCode": claim.dataset_code,
            "partitionKey": partition_key,
            "windowFrom": window_from.isoformat(),
            "windowTo": window_to.isoformat(),
            "publicationId": str(publication.publication_id),
            "dataVersion": str(publication.data_version),
            "releaseId": str(release.release_id),
            "coverageVersion": str(coverage.coverage_version),
            "coverageVersions": [str(coverage.coverage_version)],
            "publicationKind": publication_kind,
            "recordCount": record_count,
            "sourceBatchIds": source_batch_values,
        }
        values = {
            "checkpoint_id": uuid5(
                binding.child_id,
                f"target:{target_index}:partition:{partition_key}",
            ),
            "child_id": binding.child_id,
            "run_id": claim.run_id,
            "command_id": binding.command_id,
            "target_index": target_index,
            "dataset_code": claim.dataset_code,
            "partition_key": partition_key,
            "window_from": window_from,
            "window_to": window_to,
            "checkpoint_kind": "BAR_COVERAGE_VERSION",
            "publication_id": publication.publication_id,
            "data_version": publication.data_version,
            "release_id": release.release_id,
            "coverage_version": coverage.coverage_version,
            "coverage_versions_json": [str(coverage.coverage_version)],
            "publication_kind": publication_kind,
            "record_count": record_count,
            "source_batch_ids_json": source_batch_values,
            "source_batch_hash": _hash(source_batch_values),
            "output_hash": _hash(output),
            "created_at": now,
        }
        existing = session.scalar(
            select(EquityBackfillPartitionCheckpoint).where(
                EquityBackfillPartitionCheckpoint.child_id == binding.child_id,
                EquityBackfillPartitionCheckpoint.target_index == target_index,
                EquityBackfillPartitionCheckpoint.partition_key == partition_key,
            )
        )
        if existing is None:
            session.add(EquityBackfillPartitionCheckpoint(**values))
            return
        # 已成功分区理论上会在 Provider 调用前跳过；只有完全相同的崩溃重放可幂等接受。
        if any(getattr(existing, key) != value for key, value in values.items()):
            raise RuntimeError("equity backfill immutable partition replay differs")


def equity_backfill_event_partition_keys(
    *,
    dataset_code: str,
    window_from: date,
    window_to: date,
) -> tuple[str, ...]:
    """按事件族生成稳定窗口键，使一次双族业绩响应也能逐 publication 完整封印。"""
    families = _EVENT_FAMILIES.get(dataset_code)
    if families is None:
        raise ValueError("equity backfill event dataset is unsupported")
    return tuple(
        (f"{dataset_code}:{family}:{window_from.isoformat()}:{window_to.isoformat()}")
        for family in families
    )


def completed_equity_event_partitions(
    database: DatabaseClient,
    *,
    claim: ExecutionClaim,
    expected_partition_keys: frozenset[str],
) -> frozenset[str]:
    """读取事件 child 的不可变成功窗口，并拒绝额外族、窗口或跨数据集水位。"""
    binding = _binding(database, claim=claim)
    target_index = _target_index(claim)
    with database.session() as session:
        checkpoints = session.scalars(
            select(EquityBackfillPartitionCheckpoint).where(
                EquityBackfillPartitionCheckpoint.child_id == binding.child_id,
                EquityBackfillPartitionCheckpoint.target_index == target_index,
            )
        ).all()
    actual = {checkpoint.partition_key for checkpoint in checkpoints}
    if actual - expected_partition_keys or any(
        checkpoint.dataset_code != claim.dataset_code
        or checkpoint.checkpoint_kind != "EVENT_COVERAGE_VERSION"
        for checkpoint in checkpoints
    ):
        raise RuntimeError("equity backfill event partition roster differs from frozen child")
    return frozenset(actual)


def record_equity_event_partitions(
    database: DatabaseClient,
    *,
    claim: ExecutionClaim,
    execution: FencedExecution,
    window_from: date,
    window_to: date,
    source_batch_ids: Sequence[UUID],
) -> tuple[str, ...]:
    """把一次真实事件响应产生的逐族 coverage publication 封印为可恢复窗口。"""
    binding = _binding(database, claim=claim)
    target_index = _target_index(claim)
    normalized_source_ids = tuple(sorted(set(source_batch_ids), key=str))
    if not normalized_source_ids:
        raise RuntimeError("equity backfill event window has no exact source batch")
    families = _EVENT_FAMILIES.get(claim.dataset_code)
    if families is None:
        raise RuntimeError("equity backfill event checkpoint dataset is unsupported")
    now = datetime.now(UTC)
    partition_keys = equity_backfill_event_partition_keys(
        dataset_code=claim.dataset_code,
        window_from=window_from,
        window_to=window_to,
    )
    with database.transaction() as session:
        execution.assert_current(session)
        run = session.get(DataOperationRun, claim.run_id, with_for_update=True)
        if (
            run is None
            or run.fencing_token != claim.fencing_token
            or run.dataset_code != claim.dataset_code
            or run.target_index != target_index
        ):
            raise RuntimeError("equity backfill event run binding is invalid")
        for family, partition_key in zip(families, partition_keys, strict=True):
            coverages = tuple(
                session.scalars(
                    select(EquityEventWindowCoverage)
                    .where(
                        EquityEventWindowCoverage.dataset == claim.dataset_code,
                        EquityEventWindowCoverage.event_family == family,
                        EquityEventWindowCoverage.coverage_from >= window_from,
                        EquityEventWindowCoverage.coverage_to <= window_to,
                        EquityEventWindowCoverage.source_batch_id.in_(normalized_source_ids),
                        EquityEventWindowCoverage.superseded_at.is_(None),
                    )
                    .order_by(
                        EquityEventWindowCoverage.security_id,
                        EquityEventWindowCoverage.coverage_from,
                        EquityEventWindowCoverage.coverage_to,
                        EquityEventWindowCoverage.coverage_version,
                    )
                ).all()
            )
            if not coverages:
                raise RuntimeError("equity backfill event coverage roster is empty")
            publication_ids = {coverage.publication_id for coverage in coverages}
            coverage_scopes = {coverage.coverage_scope for coverage in coverages}
            universe_hashes = {coverage.universe_hash for coverage in coverages}
            if len(publication_ids) != 1 or len(coverage_scopes) != 1 or len(universe_hashes) != 1:
                raise RuntimeError("equity backfill event coverage roster is inconsistent")
            publication = session.get(
                DatasetPublication,
                next(iter(publication_ids)),
            )
            release = (
                None
                if publication is None or publication.release_id is None
                else session.get(DatasetRelease, publication.release_id)
            )
            if (
                publication is None
                or release is None
                or publication.dataset != claim.dataset_code
                or publication.quality_status != "passed"
                or publication.effective_as_of != window_to
            ):
                raise RuntimeError("equity backfill event publication cannot be verified")
            coverage_versions = sorted({str(coverage.coverage_version) for coverage in coverages})
            aggregate_version = uuid5(
                binding.child_id,
                f"target:{target_index}:event-coverages:{':'.join(coverage_versions)}",
            )
            source_values = [str(value) for value in normalized_source_ids]
            record_count = sum(coverage.record_count for coverage in coverages)
            publication_kind = "ZERO_RECORD_COVERAGE" if record_count == 0 else "DATA"
            output = {
                "datasetCode": claim.dataset_code,
                "partitionKey": partition_key,
                "windowFrom": window_from.isoformat(),
                "windowTo": window_to.isoformat(),
                "publicationId": str(publication.publication_id),
                "dataVersion": str(publication.data_version),
                "releaseId": str(release.release_id),
                "coverageVersion": str(aggregate_version),
                "coverageVersions": coverage_versions,
                "publicationKind": publication_kind,
                "recordCount": record_count,
                "sourceBatchIds": source_values,
            }
            values = {
                "checkpoint_id": uuid5(
                    binding.child_id,
                    f"target:{target_index}:partition:{partition_key}",
                ),
                "child_id": binding.child_id,
                "run_id": claim.run_id,
                "command_id": binding.command_id,
                "target_index": target_index,
                "dataset_code": claim.dataset_code,
                "partition_key": partition_key,
                "window_from": window_from,
                "window_to": window_to,
                "checkpoint_kind": "EVENT_COVERAGE_VERSION",
                "publication_id": publication.publication_id,
                "data_version": publication.data_version,
                "release_id": release.release_id,
                "coverage_version": aggregate_version,
                "coverage_versions_json": coverage_versions,
                "publication_kind": publication_kind,
                "record_count": record_count,
                "source_batch_ids_json": source_values,
                "source_batch_hash": _hash(source_values),
                "output_hash": _hash(output),
                "created_at": now,
            }
            existing = session.scalar(
                select(EquityBackfillPartitionCheckpoint).where(
                    EquityBackfillPartitionCheckpoint.child_id == binding.child_id,
                    EquityBackfillPartitionCheckpoint.target_index == target_index,
                    EquityBackfillPartitionCheckpoint.partition_key == partition_key,
                )
            )
            if existing is None:
                session.add(EquityBackfillPartitionCheckpoint(**values))
            elif any(getattr(existing, key) != value for key, value in values.items()):
                raise RuntimeError("equity backfill immutable event replay differs")
    return partition_keys


def finalize_equity_event_partitions(
    database: DatabaseClient,
    *,
    claim: ExecutionClaim,
    execution: FencedExecution,
    ordered_partition_keys: Sequence[str],
) -> None:
    """要求全部事件族窗口已封印，再以末族 publication 和完整来源集合收敛 run。"""
    if not ordered_partition_keys:
        raise RuntimeError("equity backfill event partition roster is empty")
    binding = _binding(database, claim=claim)
    target_index = _target_index(claim)
    with database.session() as session:
        checkpoints = tuple(
            session.scalars(
                select(EquityBackfillPartitionCheckpoint)
                .where(
                    EquityBackfillPartitionCheckpoint.child_id == binding.child_id,
                    EquityBackfillPartitionCheckpoint.target_index == target_index,
                )
                .order_by(
                    EquityBackfillPartitionCheckpoint.window_from,
                    EquityBackfillPartitionCheckpoint.window_to,
                    EquityBackfillPartitionCheckpoint.partition_key,
                )
            ).all()
        )
    by_key = {checkpoint.partition_key: checkpoint for checkpoint in checkpoints}
    if (
        len(by_key) != len(ordered_partition_keys)
        or set(by_key) != set(ordered_partition_keys)
        or any(checkpoint.dataset_code != claim.dataset_code for checkpoint in checkpoints)
    ):
        raise RuntimeError("equity backfill event partition seal is incomplete")
    all_source_ids = {
        UUID(value) for checkpoint in checkpoints for value in checkpoint.source_batch_ids_json
    }
    for source_batch_id in sorted(all_source_ids, key=str):
        if source_batch_id not in execution.source_batch_ids:
            execution.record_source_batch(source_batch_id)
    final_checkpoint = by_key[ordered_partition_keys[-1]]
    if final_checkpoint.coverage_version is None:
        raise RuntimeError("equity event partition seal has no final coverage")
    execution.completed_partitions = len(checkpoints)
    execution.processed_records = sum(checkpoint.record_count for checkpoint in checkpoints)
    execution.record_checkpoint(
        kind="event-coverage-version",
        position=str(final_checkpoint.coverage_version),
    )
    execution.arm_terminal_write()
    try:
        with database.transaction() as session:
            execution.assert_current(session)
            execution.finalize_if_armed(session)
    except Exception:
        execution.rollback_terminal_write()
        raise
    if not execution.terminal_written:
        raise RuntimeError("equity backfill event partition seal did not finalize its run")


def finalize_equity_bar_partitions(
    database: DatabaseClient,
    *,
    claim: ExecutionClaim,
    execution: FencedExecution,
    ordered_partition_keys: Sequence[str],
) -> None:
    """要求全部期望分区已封印，再用末分区 publication 与完整来源集合收敛 run。"""
    if not ordered_partition_keys:
        raise RuntimeError("equity backfill partition roster is empty")
    binding = _binding(database, claim=claim)
    target_index = _target_index(claim)
    with database.session() as session:
        checkpoints = session.scalars(
            select(EquityBackfillPartitionCheckpoint)
            .where(
                EquityBackfillPartitionCheckpoint.child_id == binding.child_id,
                EquityBackfillPartitionCheckpoint.target_index == target_index,
            )
            .order_by(
                EquityBackfillPartitionCheckpoint.window_from,
                EquityBackfillPartitionCheckpoint.window_to,
            )
        ).all()
    by_key = {checkpoint.partition_key: checkpoint for checkpoint in checkpoints}
    if (
        len(by_key) != len(ordered_partition_keys)
        or tuple(by_key) != tuple(ordered_partition_keys)
        or any(checkpoint.dataset_code != claim.dataset_code for checkpoint in checkpoints)
    ):
        raise RuntimeError("equity backfill partition seal is incomplete")
    all_source_ids = {
        UUID(value) for checkpoint in checkpoints for value in checkpoint.source_batch_ids_json
    }
    for source_batch_id in sorted(all_source_ids, key=str):
        if source_batch_id not in execution.source_batch_ids:
            execution.record_source_batch(source_batch_id)
    final_checkpoint = by_key[ordered_partition_keys[-1]]
    if final_checkpoint.coverage_version is None:
        raise RuntimeError("equity bar partition seal has no final coverage")
    execution.completed_partitions = len(checkpoints)
    execution.processed_records = sum(checkpoint.record_count for checkpoint in checkpoints)
    execution.record_checkpoint(
        kind="bar-coverage-version",
        position=str(final_checkpoint.coverage_version),
    )
    execution.arm_terminal_write()
    try:
        with database.transaction() as session:
            execution.assert_current(session)
            _assert_equity_bar_partition_evidence(
                session,
                claim=claim,
                checkpoints=checkpoints,
            )
            execution.finalize_if_armed(session)
    except Exception:
        # `finalize_if_armed` 在 commit 前写内存标记；提交或质量门失败必须一起清除，
        # 否则 dispatcher 会把已回滚终态误判为成功。
        execution.rollback_terminal_write()
        raise
    if not execution.terminal_written:
        raise RuntimeError("equity backfill partition seal did not finalize its run")


def _assert_equity_bar_partition_evidence(
    session: Session,
    *,
    claim: ExecutionClaim,
    checkpoints: Sequence[EquityBackfillPartitionCheckpoint],
) -> None:
    """在写 child 成功终态前逐项复核 coverage、publication 与 dataVersion 的精确配对。"""
    coverage_versions: list[UUID] = []
    for checkpoint in checkpoints:
        if (
            checkpoint.checkpoint_kind != "BAR_COVERAGE_VERSION"
            or checkpoint.coverage_version is None
        ):
            raise RuntimeError("equity backfill partition final evidence has invalid coverage kind")
        coverage_versions.append(checkpoint.coverage_version)
    if len(set(coverage_versions)) != len(coverage_versions):
        raise RuntimeError("equity backfill partition final evidence reuses a coverage version")

    rows = session.execute(
        select(EquityBarWindowCoverage, DatasetPublication, DatasetRelease)
        .join(
            DatasetPublication,
            DatasetPublication.publication_id == EquityBarWindowCoverage.publication_id,
        )
        .join(
            DatasetRelease,
            DatasetRelease.release_id == DatasetPublication.release_id,
        )
        .where(EquityBarWindowCoverage.coverage_version.in_(coverage_versions))
    ).all()
    evidence_by_version = {
        coverage.coverage_version: (coverage, publication, release)
        for coverage, publication, release in rows
    }
    if len(evidence_by_version) != len(coverage_versions):
        raise RuntimeError("equity backfill partition final evidence is incomplete")

    for checkpoint in checkpoints:
        coverage_version = checkpoint.coverage_version
        if coverage_version is None:
            raise RuntimeError("equity backfill partition final evidence has no coverage version")
        evidence = evidence_by_version.get(coverage_version)
        if evidence is None:
            raise RuntimeError("equity backfill partition final evidence is missing")
        coverage, publication, release = evidence
        try:
            source_batch_ids = {UUID(value) for value in checkpoint.source_batch_ids_json}
        except (TypeError, ValueError) as error:
            raise RuntimeError(
                "equity backfill partition final source batches are invalid"
            ) from error
        if (
            coverage.capability != claim.dataset_code
            or coverage.coverage_from != checkpoint.window_from
            or coverage.coverage_to != checkpoint.window_to
            or coverage.publication_id != checkpoint.publication_id
            or coverage.data_version != checkpoint.data_version
            or coverage.data_version != publication.data_version
            or publication.dataset != claim.dataset_code
            or publication.data_version != checkpoint.data_version
            or publication.release_id != checkpoint.release_id
            or release.release_id != checkpoint.release_id
            or publication.quality_status != "passed"
            or coverage.quality_status != "passed"
            or coverage.source_batch_id not in source_batch_ids
            or coverage.publication_kind != checkpoint.publication_kind
            or coverage.record_count != checkpoint.record_count
        ):
            raise RuntimeError("equity backfill partition final output cannot be verified")


class _Binding:
    """保存一个 claim 到不可变 child 与当前 command 的数据库权威绑定。"""

    def __init__(self, *, child_id: UUID, command_id: UUID) -> None:
        """初始化只读绑定值。"""
        self.child_id = child_id
        self.command_id = command_id


def _binding(database: DatabaseClient, *, claim: ExecutionClaim) -> _Binding:
    """解析私有意图并核对 run/command/child，禁止跨重试或跨 target 复用水位。"""
    intent = claim.execution_intent
    target_index = _target_index(claim)
    if not isinstance(intent, Mapping) or intent.get("kind") != "EQUITY_BACKFILL":
        raise RuntimeError("equity backfill partition requires a private frozen intent")
    try:
        plan_id = UUID(str(intent["planId"]))
        child_key = str(intent["childKey"])
    except (KeyError, TypeError, ValueError) as error:
        raise RuntimeError("equity backfill partition intent is invalid") from error
    with database.session() as session:
        run = session.get(DataOperationRun, claim.run_id)
        child = session.scalar(
            select(EquityBackfillChildSpec).where(
                EquityBackfillChildSpec.plan_id == plan_id,
                EquityBackfillChildSpec.child_key == child_key,
            )
        )
        command = None if run is None else session.get(DataOperationCommand, run.command_id)
        if (
            run is None
            or child is None
            or command is None
            or command.submission_id != child.submission_id
            or run.dataset_code != claim.dataset_code
            or run.target_index != target_index
            or run.execution_intent_json != dict(intent)
        ):
            raise RuntimeError("equity backfill partition binding is invalid")
        return _Binding(child_id=child.child_id, command_id=command.command_id)


def _target_index(claim: ExecutionClaim) -> int:
    """从严格私有意图读取 run target 索引；公开 target 不携带此内部身份。"""
    intent = claim.execution_intent
    value = None if intent is None else intent.get("targetIndex")
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value < 100:
        raise RuntimeError("equity backfill partition target index is invalid")
    return value


def _hash(value: Any) -> str:
    """计算规范 JSON SHA-256，供不可变分区重放逐字段比较。"""
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
