"""控制面 fencing 执行上下文的进度累计回归测试。"""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import Mock
from uuid import UUID

import pytest
from sqlalchemy import create_engine, event, text

from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.database.fenced_execution import (
    FencedExecution,
    fenced_execution,
)


def _execution() -> FencedExecution:
    """构造不访问数据库的最小 fencing 执行上下文。"""
    return FencedExecution(
        database=cast(DatabaseClient, Mock()),
        run_id=UUID("00000000-0000-0000-0000-000000000001"),
        fencing_token=7,
        finalizer=cast(Any, Mock()),
    )


def test_publication_progress_accumulates_partitions_and_records() -> None:
    """多个 canonical 分区必须在终态写入前累计真实发布记录数。"""
    execution = _execution()

    execution.record_publication_progress(record_count=496)
    execution.record_publication_progress(record_count=3)

    assert execution.completed_partitions == 2
    assert execution.processed_records == 499


def test_publication_progress_rejects_negative_record_count() -> None:
    """负记录数不能污染运行统计或绕过终态字段约束。"""
    execution = _execution()

    with pytest.raises(ValueError, match="record count"):
        execution.record_publication_progress(record_count=-1)

    assert execution.completed_partitions == 0
    assert execution.processed_records == 0


def test_rollback_terminal_write_clears_precommit_written_state() -> None:
    """数据库提交失败时必须同时撤销 armed 与尚未真正提交的 written 标记。"""
    execution = _execution()
    execution.arm_terminal_write()
    execution.terminal_written = True

    execution.rollback_terminal_write()

    assert execution.terminal_armed is False
    assert execution.terminal_written is False


def test_transaction_body_failure_disarms_terminal_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """canonical SQL 异常回滚后，后续失败账本事务不得继承 armed 状态。"""
    database = DatabaseClient(create_engine("sqlite+pysqlite:///:memory:"))
    execution = FencedExecution(
        database=database,
        run_id=UUID("00000000-0000-0000-0000-000000000002"),
        fencing_token=8,
        finalizer=cast(Any, Mock()),
    )
    monkeypatch.setattr(FencedExecution, "assert_current", lambda _self, _session: None)

    try:
        with fenced_execution(execution):
            execution.arm_terminal_write()
            with pytest.raises(Exception, match="missing_fenced_table"):
                with database.transaction() as session:
                    session.execute(text("SELECT * FROM missing_fenced_table"))
    finally:
        database.close()

    assert execution.terminal_armed is False
    assert execution.terminal_written is False


def test_transaction_commit_failure_rolls_back_precommit_written_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """finalizer 已执行但 commit 失败时，dispatcher 仍须走失败终态而非快返成功。"""
    database = DatabaseClient(create_engine("sqlite+pysqlite:///:memory:"))
    finalized: list[UUID] = []

    def finalize(_session: object, execution: FencedExecution) -> None:
        """记录提交前 finalizer 已运行，提交失败应仅撤销内存终态。"""
        finalized.append(execution.run_id)

    execution = FencedExecution(
        database=database,
        run_id=UUID("00000000-0000-0000-0000-000000000003"),
        fencing_token=9,
        finalizer=cast(Any, finalize),
    )
    monkeypatch.setattr(FencedExecution, "assert_current", lambda _self, _session: None)

    def fail_commit(_connection: object) -> None:
        """模拟数据库在 finalizer 之后拒绝提交。"""
        raise RuntimeError("simulated commit failure")

    event.listen(database.engine, "commit", fail_commit)
    try:
        with fenced_execution(execution):
            execution.arm_terminal_write()
            with pytest.raises(RuntimeError, match="simulated commit failure"):
                with database.transaction() as session:
                    session.execute(text("SELECT 1"))
    finally:
        event.remove(database.engine, "commit", fail_commit)
        database.close()

    assert finalized == [execution.run_id]
    assert execution.terminal_armed is False
    assert execution.terminal_written is False
