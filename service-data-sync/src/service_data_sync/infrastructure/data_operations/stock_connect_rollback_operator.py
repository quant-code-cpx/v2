"""通过数据运维全局槽执行可恢复、可幂等重放的互联互通 bundle 回滚。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import NoReturn
from uuid import UUID, uuid4, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from service_data_sync.application.source_registry import SourceRegistry
from service_data_sync.domain.stock_connect import StockConnectChannel
from service_data_sync.infrastructure.data_operations.control_plane import (
    DataOperationsControlPlane,
)
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.database.fenced_execution import (
    FencedExecution,
    fenced_execution,
)
from service_data_sync.infrastructure.database.models.market import (
    StockConnectBundlePublication,
    StockConnectBundleRollbackAudit,
)
from service_data_sync.infrastructure.database.models.operations import (
    DataOperationCommand,
    DataOperationEvent,
    DataOperationExecutionSlot,
    DataOperationRun,
)
from service_data_sync.infrastructure.persistence.stock_connect_rollback_repository import (
    RolledBackStockConnectBundle,
    SqlAlchemyStockConnectRollbackRepository,
    StockConnectBundleRollbackRejected,
)

_DATASET_CODE = "market.stock_connect.overview.bundle"
_MODE = "ROLLBACK"
_LEASE_DURATION = timedelta(minutes=5)
_RUN_NAMESPACE_NAME = "quant-v2.stock-connect-bundle-rollback.v1"
_TERMINAL_RUNS = frozenset(
    {"SUCCEEDED", "PARTIAL", "FAILED", "CANCELLED", "INTERRUPTED", "SKIPPED"}
)


class StockConnectRollbackOperationRejected(ValueError):
    """携带稳定原因码表示操作幂等身份、执行槽或历史目标被拒绝。"""

    def __init__(self, code: str, message: str) -> None:
        """保存不泄漏数据库或来源细节的低基数拒绝码。"""
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class StockConnectRollbackOperation:
    """冻结一次运维回滚的永久幂等身份、目标和审计主体。"""

    operation_id: UUID
    channel: StockConnectChannel
    trade_date: date
    target_bundle_release_id: UUID
    actor_ref: str
    reason: str
    request_id: str


@dataclass(frozen=True, slots=True)
class StockConnectRollbackOperationResult:
    """返回控制面身份与仓储原子回滚结果。"""

    operation_id: UUID
    run_id: UUID
    channel: str
    trade_date: date
    rollback: RolledBackStockConnectBundle


@dataclass(frozen=True, slots=True)
class _RollbackLease:
    """保存本次进程真正取得的 run 与 fencing token。"""

    operation_id: UUID
    run_id: UUID
    fencing_token: int


class StockConnectRollbackOperator:
    """将紧急回滚接入权威 command/run、全局 slot、fencing 与不可变审计。"""

    def __init__(
        self,
        database: DatabaseClient,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        """保存数据库与可测试时钟；不创建 provider、网络或对象存储客户端。"""
        self._database = database
        self._now = now or (lambda: datetime.now(UTC))
        self._repository = SqlAlchemyStockConnectRollbackRepository(database)

    def execute(
        self,
        operation: StockConnectRollbackOperation,
    ) -> StockConnectRollbackOperationResult:
        """取得或恢复同一 operation run，并在 publication 事务内写成功终态。"""
        normalized = _validate_operation(operation)
        # 先使用既有控制面语义回收真正过期的 worker；活跃任务仍由后续行锁拒绝抢占。
        DataOperationsControlPlane(
            database=self._database,
            catalog={},
            source_registry=SourceRegistry(),
            now=self._now,
        ).reap_expired_slots()
        lease, replay = self._acquire(normalized)
        if replay is not None:
            return replay
        if lease is None:
            raise RuntimeError("stock-connect rollback lease was not resolved")
        execution = FencedExecution(
            database=self._database,
            run_id=lease.run_id,
            fencing_token=lease.fencing_token,
            finalizer=self._finalize_success,
        )
        execution.record_publication_progress(record_count=1)
        execution.arm_terminal_write()
        try:
            with fenced_execution(execution):
                rollback = self._repository.rollback_bundle(
                    channel=normalized.channel,
                    trade_date=normalized.trade_date,
                    target_bundle_release_id=normalized.target_bundle_release_id,
                    actor_ref=normalized.actor_ref,
                    reason=normalized.reason,
                )
        except StockConnectBundleRollbackRejected as error:
            execution.disarm_terminal_write()
            self._finalize_failure(
                lease,
                code=error.code,
                message="Stock-connect bundle rollback was rejected",
                retryable=False,
            )
            raise StockConnectRollbackOperationRejected(error.code, str(error)) from error
        except Exception:
            execution.disarm_terminal_write()
            self._finalize_failure(
                lease,
                code="rollback-execution-failed",
                message="Stock-connect bundle rollback execution failed",
                retryable=True,
            )
            raise
        if not execution.terminal_written:
            raise RuntimeError("stock-connect rollback did not write its atomic terminal state")
        return StockConnectRollbackOperationResult(
            operation_id=normalized.operation_id,
            run_id=lease.run_id,
            channel=_channel_code(normalized.channel),
            trade_date=normalized.trade_date,
            rollback=rollback,
        )

    def _acquire(
        self,
        operation: StockConnectRollbackOperation,
    ) -> tuple[_RollbackLease | None, StockConnectRollbackOperationResult | None]:
        """创建或恢复确定性 run；成功 operation 直接从不可变审计重放。"""
        run_id = uuid5(operation.operation_id, _RUN_NAMESPACE_NAME)
        target = _target(operation)
        now = _aware(self._now())
        with self._database.transaction() as session:
            slot = _locked_slot(session)
            command = session.get(
                DataOperationCommand,
                operation.operation_id,
                with_for_update=True,
            )
            run = session.get(DataOperationRun, run_id, with_for_update=True)
            if command is None:
                if run is not None:
                    _reject(
                        "rollback-operation-id-conflict",
                        "deterministic rollback run already belongs to another command",
                    )
                command = DataOperationCommand(
                    command_id=operation.operation_id,
                    submission_id=None,
                    status="QUEUED",
                    actor_ref=operation.actor_ref,
                    actor_role="DATA_OPERATOR",
                    reason=operation.reason,
                    request_id=operation.request_id,
                    retry_of_command_id=None,
                    error_json=None,
                    requested_at=now,
                    started_at=None,
                    finished_at=None,
                )
                run = DataOperationRun(
                    run_id=run_id,
                    command_id=operation.operation_id,
                    target_index=0,
                    dataset_code=_DATASET_CODE,
                    mode=_MODE,
                    target_json=target,
                    source_snapshot=[],
                    execution_intent_json=None,
                    status="QUEUED",
                    queue_position=None,
                    attempt=0,
                    recovery_attempts=0,
                    completed_partitions=0,
                    total_partitions=1,
                    processed_records=0,
                    estimated_records=1,
                    fencing_token=None,
                    cancel_requested=False,
                    error_json=None,
                    quality_gate_json={
                        "disposition": "ROLLBACK",
                        "rules": ["HISTORICAL_BUNDLE_AND_OVERVIEW_GRAPH_VALIDATED"],
                    },
                    requested_at=now,
                    started_at=None,
                    finished_at=None,
                )
                session.add_all([command, run])
                session.flush()
            else:
                _assert_same_operation(
                    command=command,
                    run=run,
                    operation=operation,
                    expected_run_id=run_id,
                    target=target,
                )
                assert run is not None
                if run.status == "SUCCEEDED":
                    return None, _replay_result(
                        session,
                        operation_id=operation.operation_id,
                        run_id=run_id,
                    )
                if run.status in _TERMINAL_RUNS:
                    _reject(
                        "rollback-operation-terminal",
                        "rollback operation already has a non-success terminal state",
                    )
            assert run is not None
            if slot.state != "IDLE":
                _reject(
                    "rollback-execution-slot-busy",
                    "data operation execution slot is occupied",
                )
            fencing_token = int(slot.fencing_token) + 1
            command.status = "RUNNING"
            command.started_at = command.started_at or now
            command.finished_at = None
            command.error_json = None
            run.status = "RUNNING"
            run.attempt += 1
            run.fencing_token = fencing_token
            run.started_at = run.started_at or now
            run.finished_at = None
            run.error_json = None
            slot.state = "RUNNING"
            slot.run_id = run_id
            slot.dataset_code = _DATASET_CODE
            slot.lease_until = now + _LEASE_DURATION
            slot.heartbeat_at = now
            slot.fencing_token = fencing_token
            session.add(
                _event(
                    resource_type="RUN",
                    resource_id=run_id,
                    action="STOCK_CONNECT_ROLLBACK_ACQUIRE",
                    result="ACCEPTED",
                    actor_ref=operation.actor_ref,
                    request_id=operation.request_id,
                    occurred_at=now,
                    error_json=None,
                )
            )
            return (
                _RollbackLease(
                    operation_id=operation.operation_id,
                    run_id=run_id,
                    fencing_token=fencing_token,
                ),
                None,
            )

    def _finalize_success(
        self,
        session: Session,
        execution: FencedExecution,
    ) -> None:
        """与审计和 publication 指针同事务写成功 run/command 并释放全局槽。"""
        now = _aware(self._now())
        run = session.get(DataOperationRun, execution.run_id, with_for_update=True)
        if run is None or run.status != "RUNNING" or run.fencing_token != execution.fencing_token:
            raise RuntimeError("stock-connect rollback run lost its fencing identity")
        command = session.get(DataOperationCommand, run.command_id, with_for_update=True)
        slot = session.get(DataOperationExecutionSlot, "global", with_for_update=True)
        if command is None or slot is None or slot.run_id != run.run_id:
            raise RuntimeError("stock-connect rollback control-plane state is incomplete")
        run.status = "SUCCEEDED"
        run.completed_partitions = 1
        run.processed_records = 1
        run.quality_gate_json = {
            "disposition": "ROLLBACK",
            "rules": ["HISTORICAL_BUNDLE_AND_OVERVIEW_GRAPH_VALIDATED"],
        }
        run.finished_at = now
        command.status = "SUCCEEDED"
        command.finished_at = now
        _release_slot(slot)
        session.add(
            _event(
                resource_type="RUN",
                resource_id=run.run_id,
                action="STOCK_CONNECT_ROLLBACK_COMPLETE",
                result="SUCCEEDED",
                actor_ref=command.actor_ref,
                request_id=command.request_id,
                occurred_at=now,
                error_json=None,
            )
        )

    def _finalize_failure(
        self,
        lease: _RollbackLease,
        *,
        code: str,
        message: str,
        retryable: bool,
    ) -> None:
        """在回滚事务失败后只写脱敏失败终态；publication 与审计已整体回滚。"""
        now = _aware(self._now())
        error = {
            "code": code,
            "stage": "ROLLBACK",
            "retryable": retryable,
            "message": message,
        }
        with self._database.transaction() as session:
            slot = session.get(DataOperationExecutionSlot, "global", with_for_update=True)
            run = session.get(DataOperationRun, lease.run_id, with_for_update=True)
            if (
                slot is None
                or run is None
                or slot.run_id != lease.run_id
                or run.fencing_token != lease.fencing_token
            ):
                return
            command = session.get(DataOperationCommand, run.command_id, with_for_update=True)
            if command is None:
                raise RuntimeError("stock-connect rollback command is unavailable")
            run.status = "FAILED"
            run.error_json = error
            run.finished_at = now
            command.status = "FAILED"
            command.error_json = error
            command.finished_at = now
            _release_slot(slot)
            session.add(
                _event(
                    resource_type="RUN",
                    resource_id=run.run_id,
                    action="STOCK_CONNECT_ROLLBACK_COMPLETE",
                    result="FAILED",
                    actor_ref=command.actor_ref,
                    request_id=command.request_id,
                    occurred_at=now,
                    error_json=error,
                )
            )


def stock_connect_rollback_result_view(
    result: StockConnectRollbackOperationResult,
) -> dict[str, object]:
    """投影不含数据库细节的稳定机器结果，供 CLI 与验收脚本复用。"""
    rollback = result.rollback
    return {
        "schema": "quant-v2.stock-connect-bundle-rollback-result.v1",
        "operationId": str(result.operation_id),
        "runId": str(result.run_id),
        "channel": result.channel,
        "tradeDate": result.trade_date.isoformat(),
        "rollbackId": str(rollback.rollback_id),
        "fromBundleReleaseId": str(rollback.from_bundle_release_id),
        "toBundleReleaseId": str(rollback.to_bundle_release_id),
        "targetDataVersion": rollback.target_data_version,
        "overviewReleases": [
            {"channelSet": channel_set, "overviewReleaseId": str(release_id)}
            for channel_set, release_id in rollback.overview_release_ids
        ],
        "reused": rollback.reused,
    }


def _validate_operation(
    operation: StockConnectRollbackOperation,
) -> StockConnectRollbackOperation:
    """校验所有公开运维字段有界且无控制字符，避免污染审计与日志。"""
    actor_ref = _bounded_text(operation.actor_ref, minimum=1, maximum=128, label="actor")
    reason = _bounded_text(operation.reason, minimum=8, maximum=2000, label="reason")
    request_id = _bounded_text(
        operation.request_id,
        minimum=1,
        maximum=128,
        label="request id",
    )
    return StockConnectRollbackOperation(
        operation_id=operation.operation_id,
        channel=operation.channel,
        trade_date=operation.trade_date,
        target_bundle_release_id=operation.target_bundle_release_id,
        actor_ref=actor_ref,
        reason=reason,
        request_id=request_id,
    )


def _target(operation: StockConnectRollbackOperation) -> dict[str, object]:
    """构造由 operation UUID 钉住且可做幂等冲突检测的完整目标。"""
    return {
        "date": operation.trade_date.isoformat(),
        "channel": _channel_code(operation.channel),
        "targetBundleReleaseId": str(operation.target_bundle_release_id),
    }


def _assert_same_operation(
    *,
    command: DataOperationCommand,
    run: DataOperationRun | None,
    operation: StockConnectRollbackOperation,
    expected_run_id: UUID,
    target: dict[str, object],
) -> None:
    """拒绝 operation UUID 被不同主体、原因、请求链或回滚目标复用。"""
    if (
        run is None
        or run.run_id != expected_run_id
        or run.command_id != command.command_id
        or run.dataset_code != _DATASET_CODE
        or run.mode != _MODE
        or run.target_json != target
        or command.actor_ref != operation.actor_ref
        or command.reason != operation.reason
        or command.request_id != operation.request_id
    ):
        _reject(
            "rollback-operation-id-conflict",
            "rollback operation UUID was already used with different immutable fields",
        )


def _replay_result(
    session: Session,
    *,
    operation_id: UUID,
    run_id: UUID,
) -> StockConnectRollbackOperationResult:
    """从成功 run 的不可变审计重建结果，处理输出丢失后的安全重放。"""
    audit = session.scalar(
        select(StockConnectBundleRollbackAudit).where(
            StockConnectBundleRollbackAudit.operation_run_id == run_id
        )
    )
    if audit is None:
        raise RuntimeError("successful stock-connect rollback has no immutable audit")
    target = session.get(StockConnectBundlePublication, audit.to_bundle_release_id)
    if target is None:
        raise RuntimeError("stock-connect rollback audit target is unavailable")
    overview_ids = tuple(
        (channel_set, UUID(release_id))
        for channel_set, release_id in sorted(audit.to_overview_release_ids.items())
    )
    return StockConnectRollbackOperationResult(
        operation_id=operation_id,
        run_id=run_id,
        channel=f"{audit.channel}_{audit.direction}",
        trade_date=audit.trade_date,
        rollback=RolledBackStockConnectBundle(
            rollback_id=UUID(str(audit.rollback_id)),
            from_bundle_release_id=UUID(str(audit.from_bundle_release_id)),
            to_bundle_release_id=UUID(str(audit.to_bundle_release_id)),
            target_data_version=target.data_version,
            overview_release_ids=overview_ids,
            reused=True,
        ),
    )


def _locked_slot(session: Session) -> DataOperationExecutionSlot:
    """锁定全局槽；空库首次运行时以 IDLE 和零 token 初始化。"""
    slot = session.get(DataOperationExecutionSlot, "global", with_for_update=True)
    if slot is not None:
        return slot
    slot = DataOperationExecutionSlot(
        slot_key="global",
        state="IDLE",
        run_id=None,
        dataset_code=None,
        lease_until=None,
        heartbeat_at=None,
        fencing_token=0,
    )
    session.add(slot)
    session.flush()
    return slot


def _release_slot(slot: DataOperationExecutionSlot) -> None:
    """释放占用字段但保留单调 fencing token，旧进程永远不能重新提交。"""
    slot.state = "IDLE"
    slot.run_id = None
    slot.dataset_code = None
    slot.lease_until = None
    slot.heartbeat_at = None


def _channel_code(channel: StockConnectChannel) -> str:
    """把领域通道映射为 API、CLI 与审计共用的稳定公开代码。"""
    return f"{channel.channel}_{channel.direction}"


def _event(
    *,
    resource_type: str,
    resource_id: UUID,
    action: str,
    result: str,
    actor_ref: str,
    request_id: str,
    occurred_at: datetime,
    error_json: dict[str, object] | None,
) -> DataOperationEvent:
    """构造一条不可变、无供应商原文的控制面事件。"""
    return DataOperationEvent(
        event_id=uuid4(),
        resource_type=resource_type,
        resource_id=resource_id,
        action=action,
        result=result,
        actor_ref=actor_ref,
        request_id=request_id,
        error_json=error_json,
        occurred_at=occurred_at,
    )


def _bounded_text(value: str, *, minimum: int, maximum: int, label: str) -> str:
    """裁剪外侧空白并拒绝控制字符、空值或超长审计文本。"""
    normalized = value.strip()
    if not minimum <= len(normalized) <= maximum or any(
        ord(character) < 32 or ord(character) == 127 for character in normalized
    ):
        raise ValueError(f"stock-connect rollback {label} is invalid")
    return normalized


def _aware(value: datetime) -> datetime:
    """要求操作时钟带时区并规范到 UTC，防止租约使用本地无时区时间。"""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("stock-connect rollback clock must include timezone")
    return value.astimezone(UTC)


def _reject(code: str, message: str) -> NoReturn:
    """抛出带稳定原因码的运维拒绝。"""
    raise StockConnectRollbackOperationRejected(code, message)
