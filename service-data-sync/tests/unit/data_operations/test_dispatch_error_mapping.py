"""数据运维 dispatcher 失败分类回归测试。"""

from __future__ import annotations

from datetime import UTC, datetime
from threading import Event, Thread
from typing import Any, cast
from uuid import UUID

from service_data_sync.application.ports.data_source import ProviderError, ProviderErrorCode
from service_data_sync.infrastructure.data_operations.control_plane import (
    DataOperationsControlPlane,
    ExecutionClaim,
    ExecutionOutcome,
)
from service_data_sync.infrastructure.persistence.event_window_coverage import (
    EquityWindowIdentityUnavailable,
)


class _NoopThread(Thread):
    """提供 dispatcher `finally` 所需的线程接口，而不启动真实心跳。"""

    def join(self, timeout: float | None = None) -> None:
        """测试不创建后台线程，因此无需等待。"""
        del timeout


def _claim() -> ExecutionClaim:
    """构造一个已取得 fencing token 的最小日线 run。"""
    return ExecutionClaim(
        run_id=UUID("00000000-0000-0000-0000-000000000021"),
        dataset_code="equity.bar.1d.raw",
        fencing_token=21,
        target={},
        source_snapshot=[],
    )


def _dispatch_with_failure(error: Exception) -> ExecutionOutcome:
    """让真实 `dispatch_once` 接收指定异常，并返回它写入的安全失败结果。"""
    control_plane = object.__new__(DataOperationsControlPlane)
    control_plane._now = lambda: datetime(2026, 8, 1, 14, tzinfo=UTC)
    claim = _claim()
    seen: dict[str, object] = {}

    def failing_executor(_claim_value: ExecutionClaim) -> ExecutionOutcome:
        """模拟已进入 fenced executor 的确定性失败。"""
        del _claim_value
        raise error

    def claim_next_run(worker_id: str) -> ExecutionClaim | None:
        """固定返回最小 run，使测试只覆盖 dispatcher 的异常分类。"""
        del worker_id
        return claim

    def start_heartbeat(claim: ExecutionClaim) -> tuple[Event, Thread]:
        """替换真实心跳线程，避免测试连接数据库。"""
        del claim
        return Event(), _NoopThread()

    def complete_run(**kwargs: object) -> bool:
        """捕获 dispatcher 准备写入的失败终态，不访问数据库。"""
        seen["outcome"] = kwargs["outcome"]
        return True

    control_plane._database = cast(Any, object())
    control_plane._executors = {claim.dataset_code: failing_executor}
    control_plane.claim_next_run = claim_next_run
    control_plane._start_heartbeat = start_heartbeat
    control_plane.complete_run = complete_run

    assert control_plane.dispatch_once("error-mapping-test") is True
    return cast(ExecutionOutcome, seen["outcome"])


def test_dispatch_maps_identity_window_gap_to_non_retryable_precondition() -> None:
    """历史身份缺口不能被泛化为可重试 `PERSIST` 或来源不可用。"""
    outcome = _dispatch_with_failure(
        EquityWindowIdentityUnavailable("fixture identity does not cover requested window")
    )

    assert outcome.status == "FAILED"
    assert outcome.error == {
        "code": "equity-identity-window-unavailable",
        "stage": "PRECONDITION",
        "retryable": False,
        "message": "Equity identity does not cover requested window",
    }


def test_dispatch_preserves_provider_error_as_provider_fetch() -> None:
    """adapter 抛出的中立来源错误仍必须保留 `PROVIDER_FETCH` 与重试语义。"""
    outcome = _dispatch_with_failure(
        ProviderError(ProviderErrorCode.UNAVAILABLE, "fixture provider unavailable", retryable=True)
    )

    assert outcome.status == "FAILED"
    assert outcome.error == {
        "code": "source-unavailable",
        "stage": "PROVIDER_FETCH",
        "retryable": True,
        "message": "Data source is unavailable",
    }
