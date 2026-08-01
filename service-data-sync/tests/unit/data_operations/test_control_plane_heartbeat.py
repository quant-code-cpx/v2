"""控制面心跳遇到数据库故障时的回归测试。"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from threading import Event, Thread
from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy.exc import OperationalError, SQLAlchemyError, TimeoutError

from service_data_sync.infrastructure.data_operations.control_plane import (
    DataOperationsControlPlane,
)


class _DatabaseWithHeartbeatFailure:
    """模拟心跳事务在连接 PostgreSQL 时失败，且不产生任何可确认写入。"""

    def __init__(self, failure: SQLAlchemyError) -> None:
        """保存本次连接或连接池失败，并初始化可断言的事务调用计数。"""
        self._failure = failure
        self.transaction_calls = 0

    @contextmanager
    def transaction(self) -> Iterator[Any]:
        """在进入事务边界时抛出可重试的数据库连接错误。"""
        self.transaction_calls += 1
        raise self._failure
        yield None


class _StopAfterOneHeartbeat:
    """让心跳循环立即执行一次，再在意外继续时停止，避免测试等待真实 lease 周期。"""

    def __init__(self) -> None:
        """记录等待次数，供断言确认失败后没有继续续租。"""
        self.wait_calls = 0

    def wait(self, timeout: float | None = None) -> bool:
        """第一次放行心跳，第二次返回停止信号。"""
        del timeout
        self.wait_calls += 1
        return self.wait_calls > 1


def _control_plane(database: _DatabaseWithHeartbeatFailure) -> DataOperationsControlPlane:
    """构造只含心跳依赖的最小控制面，避免测试触及真实 PostgreSQL。"""
    control_plane = object.__new__(DataOperationsControlPlane)
    control_plane._database = cast(Any, database)
    control_plane._now = lambda: datetime(2099, 1, 1, tzinfo=UTC)
    return control_plane


def _operational_failure() -> SQLAlchemyError:
    """构造 PostgreSQL 连接中断时由 SQLAlchemy 包装的 `OperationalError`。"""
    return OperationalError(
        "UPDATE data_operation_execution_slot",
        {},
        ConnectionError("postgres connection lost"),
    )


def _pool_timeout_failure() -> SQLAlchemyError:
    """构造连接池等待超时，覆盖 PostgreSQL 不可达时的另一条常见失败路径。"""
    return TimeoutError("postgres connection timeout")


def test_heartbeat_surfaces_database_failure_without_returning_success() -> None:
    """直接心跳保留数据库失败语义，不能把未提交续租伪造成 `True`。"""
    database = _DatabaseWithHeartbeatFailure(_operational_failure())
    control_plane = _control_plane(database)

    with pytest.raises(SQLAlchemyError):
        control_plane.heartbeat(
            run_id=UUID("00000000-0000-0000-0000-000000000001"),
            fencing_token=1,
        )

    assert database.transaction_calls == 1


@pytest.mark.parametrize(
    "failure_factory",
    [_operational_failure, _pool_timeout_failure],
    ids=["operational-error", "pool-timeout"],
)
def test_heartbeat_thread_stops_cleanly_after_database_failure(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    failure_factory: Callable[[], SQLAlchemyError],
) -> None:
    """后台续租吞掉数据库异常并停止，保留 lease 到期和 fencing 恢复语义。"""
    database = _DatabaseWithHeartbeatFailure(failure_factory())
    control_plane = _control_plane(database)
    stop = _StopAfterOneHeartbeat()
    uncaught: list[BaseException] = []
    caplog.set_level(logging.WARNING)

    def record_uncaught_thread_error(args: threading.ExceptHookArgs) -> None:
        """捕获线程逃逸异常，确保数据库故障不会打印未处理 traceback。"""
        if args.exc_value is not None:
            uncaught.append(args.exc_value)

    monkeypatch.setattr(threading, "excepthook", record_uncaught_thread_error)
    thread = Thread(
        target=control_plane._heartbeat_until_stopped,
        args=(UUID("00000000-0000-0000-0000-000000000002"), 2, cast(Event, stop)),
    )

    thread.start()
    thread.join(timeout=1)

    assert thread.is_alive() is False
    assert uncaught == []
    assert database.transaction_calls == 1
    assert stop.wait_calls == 1
    assert any("数据运维心跳数据库事务失败" in message for message in caplog.messages)
    record = caplog.records[-1]
    assert record.__dict__["data_operation_run_id"] == "00000000-0000-0000-0000-000000000002"
    assert record.__dict__["fencing_token"] == 2
