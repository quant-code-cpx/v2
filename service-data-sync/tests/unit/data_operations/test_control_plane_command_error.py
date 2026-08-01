"""数据运维 command 聚合错误与整批重试可见性的单元回归。"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

from service_data_sync.infrastructure.data_operations.control_plane import (
    DataOperationsControlPlane,
)


class _FakeScalarResult:
    """模拟控制面刷新状态时按 target 顺序读取的 child run 集合。"""

    def __init__(self, runs: list[SimpleNamespace]) -> None:
        """保存已按提交顺序构造的 child run 替身。"""
        self._runs = runs

    def all(self) -> list[SimpleNamespace]:
        """返回完整 child run 集合，不连接数据库。"""
        return self._runs


class _FakeSession:
    """提供 `_refresh_command_status` 所需的最小 SQLAlchemy Session 表面。"""

    def __init__(self, runs: list[SimpleNamespace]) -> None:
        """保存本次聚合应读取的 child run 集合。"""
        self._runs = runs

    def scalars(self, _statement: object) -> _FakeScalarResult:
        """忽略 ORM 查询表达式并返回固定 child run 结果。"""
        return _FakeScalarResult(self._runs)


def test_command_error_prefers_retryable_child_error_for_command_retry() -> None:
    """混合失败时 command 必须展示最早可重试错误，避免 Web 错误隐藏整批重试。"""
    command = _command(error={"code": "stale", "retryable": False})
    non_retryable = _run(
        status="FAILED",
        error={
            "code": "schema-changed",
            "stage": "NORMALIZE",
            "retryable": False,
            "message": "Schema changed",
        },
    )
    retryable = _run(
        status="FAILED",
        error={
            "code": "source-unavailable",
            "stage": "PROVIDER_FETCH",
            "retryable": True,
            "message": "Data source is unavailable",
        },
    )

    _refresh(command, [non_retryable, retryable])

    assert command.status == "FAILED"
    assert command.error_json == retryable.error_json
    assert command.error_json is not retryable.error_json


def test_command_error_uses_first_error_when_no_child_is_retryable() -> None:
    """全部不可重试时保留最早失败摘要，供审计而不伪造可重试语义。"""
    command = _command(error=None)
    first = _run(
        status="FAILED",
        error={
            "code": "schema-changed",
            "stage": "NORMALIZE",
            "retryable": False,
            "message": "Schema changed",
        },
    )
    second = _run(
        status="FAILED",
        error={
            "code": "quality-blocked",
            "stage": "QUALITY_GATE",
            "retryable": False,
            "message": "Quality gate blocked publication",
        },
    )

    _refresh(command, [first, second])

    assert command.status == "FAILED"
    assert command.error_json == first.error_json


def test_command_error_clears_for_running_success_and_cancelled_states() -> None:
    """重试后的排队运行及成功、取消终态都不能残留旧 command 错误。"""
    for status, initial_status, expected_status in (
        ("QUEUED", "FAILED", "QUEUED"),
        ("RUNNING", "FAILED", "RUNNING"),
        ("SUCCEEDED", "FAILED", "SUCCEEDED"),
        ("CANCELLED", "CANCEL_REQUESTED", "CANCELLED"),
    ):
        command = _command(error={"code": "old-error", "retryable": True}, status=initial_status)

        _refresh(command, [_run(status=status, error=None)])

        assert command.status == expected_status
        assert command.error_json is None


def _refresh(command: SimpleNamespace, runs: list[SimpleNamespace]) -> None:
    """调用真实聚合逻辑，避免测试自行重建 command 状态机。"""
    control_plane = object.__new__(DataOperationsControlPlane)
    control_plane._refresh_command_status(
        cast(Any, _FakeSession(runs)),
        cast(Any, command),
        datetime(2026, 8, 1, tzinfo=UTC),
    )


def _command(*, error: dict[str, Any] | None, status: str = "RUNNING") -> SimpleNamespace:
    """构造控制面状态聚合需要的最小 command 实体。"""
    return SimpleNamespace(
        command_id=uuid4(),
        status=status,
        error_json=error,
        finished_at=None,
    )


def _run(*, status: str, error: dict[str, Any] | None) -> SimpleNamespace:
    """构造按调用方顺序排列的最小 child run 实体。"""
    return SimpleNamespace(status=status, error_json=error)
