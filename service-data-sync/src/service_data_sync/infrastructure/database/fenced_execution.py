"""把控制面 fencing token 传播到 canonical 仓储事务。

同步 worker 在抓取外部来源时不持有数据库锁；只有准备写入 publication、checkpoint 或
可用性观测时，数据库事务才重新锁定全局 ExecutionSlot 并验证 token。这样过期 worker
即使恢复，也无法提交旧结果。
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

if TYPE_CHECKING:
    from service_data_sync.infrastructure.database.connection import DatabaseClient


class FencingTokenLost(RuntimeError):
    """表示 worker 已失去当前全局槽，调用方必须放弃写入而不是重试发布。"""


FencedFinalizer = Callable[[Session, "FencedExecution"], None]
_CURRENT_FENCED_EXECUTION: ContextVar[FencedExecution | None] = ContextVar(
    "service_data_sync_fenced_execution",
    default=None,
)


@dataclass(slots=True)
class FencedExecution:
    """保存一个 dispatcher 执行期间的 run、token 与同事务终态回调。"""

    database: DatabaseClient
    run_id: UUID
    fencing_token: int
    finalizer: FencedFinalizer
    terminal_armed: bool = False
    terminal_written: bool = False
    checkpoint_kind: str | None = None
    checkpoint_position: str | None = None
    completed_partitions: int = 0
    processed_records: int = 0
    source_batch_ids: list[UUID] = field(default_factory=list)

    def assert_current(self, session: Session) -> None:
        """在即将写 canonical 数据的事务内锁槽并验证 lease、run 与 token。"""
        row = (
            session.execute(
                text(
                    "SELECT state, run_id, fencing_token, lease_until "
                    "FROM data_operation_execution_slot "
                    "WHERE slot_key = 'global' FOR UPDATE"
                )
            )
            .mappings()
            .one_or_none()
        )
        now = datetime.now(UTC)
        if (
            row is None
            or row["state"] != "RUNNING"
            or str(row["run_id"]) != str(self.run_id)
            or int(row["fencing_token"]) != self.fencing_token
            or row["lease_until"] is None
            or row["lease_until"] <= now
        ):
            raise FencingTokenLost("data operation fencing token is no longer current")
        run = (
            session.execute(
                text(
                    "SELECT status, cancel_requested, fencing_token "
                    "FROM data_operation_run WHERE run_id = :run_id FOR UPDATE"
                ),
                {"run_id": str(self.run_id)},
            )
            .mappings()
            .one_or_none()
        )
        if (
            run is None
            or run["fencing_token"] is None
            or int(run["fencing_token"]) != self.fencing_token
            or run["status"] == "CANCEL_REQUESTED"
            or bool(run["cancel_requested"])
        ):
            raise FencingTokenLost(
                "data operation was cancelled or fencing token is no longer current"
            )

    def arm_terminal_write(self) -> None:
        """声明下一个 canonical 写事务应在提交前同时写入 run 终态并释放槽。"""
        self.terminal_armed = True

    def disarm_terminal_write(self) -> None:
        """在末次 canonical 写回滚后撤销终态回调，避免失败处理事务被误判为成功发布。"""
        self.terminal_armed = False

    def rollback_terminal_write(self) -> None:
        """在 canonical 事务回滚后撤销尚未提交的终态内存标记。

        `finalize_if_armed` 必须在数据库提交前执行，因此提交本身失败时
        `terminal_written` 可能已经在内存中变为真。回滚路径同时清除两个标记，
        使 dispatcher 继续通过独立失败事务收敛 run，而不是把已回滚发布误判为成功。
        """
        self.terminal_armed = False
        self.terminal_written = False

    def record_checkpoint(self, *, kind: str, position: str) -> None:
        """记录仅供同事务终态摘要使用的 opaque checkpoint，绝不向调用方暴露原值。"""
        self.checkpoint_kind = kind
        self.checkpoint_position = position

    def record_publication_progress(self, *, record_count: int) -> None:
        """累计已成功准备发布的分区和记录数，供同事务终态写入准确进度。"""
        if record_count < 0:
            raise ValueError("canonical publication record count must be non-negative")
        self.completed_partitions += 1
        self.processed_records += record_count

    def record_source_batch(self, source_batch_id: UUID) -> None:
        """记录本 fenced run 实际登记的来源批次，供提交前精确版本审计。"""
        if source_batch_id in self.source_batch_ids:
            raise ValueError("source batch is already attached to fenced execution")
        self.source_batch_ids.append(source_batch_id)

    def is_cancel_requested(self, session: Session) -> bool:
        """在已持有 slot 锁的事务内读取 run 取消标志，供长批次在分区间及时停止。"""
        row = (
            session.execute(
                text(
                    "SELECT status, cancel_requested FROM data_operation_run WHERE run_id = :run_id"
                ),
                {"run_id": str(self.run_id)},
            )
            .mappings()
            .one_or_none()
        )
        return row is None or row["status"] == "CANCEL_REQUESTED" or bool(row["cancel_requested"])

    def finalize_if_armed(self, session: Session) -> None:
        """在 canonical 写完成后、同一事务提交前运行控制面终态回调。"""
        if self.terminal_armed and not self.terminal_written:
            self.finalizer(session, self)
            self.terminal_written = True


@contextmanager
def fenced_execution(execution: FencedExecution) -> Iterator[FencedExecution]:
    """在当前线程传播 fencing 上下文，退出时必定清理避免污染后续任务。"""
    token = _CURRENT_FENCED_EXECUTION.set(execution)
    try:
        yield execution
    finally:
        _CURRENT_FENCED_EXECUTION.reset(token)


def current_fenced_execution() -> FencedExecution | None:
    """返回当前同步写入可见的 fencing 上下文；普通读写始终得到空值。"""
    return _CURRENT_FENCED_EXECUTION.get()
