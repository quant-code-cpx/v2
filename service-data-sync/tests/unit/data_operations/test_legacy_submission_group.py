"""父级回填计划的多数据集 SYSTEM command 提交回归测试。"""

from __future__ import annotations

from typing import Any, cast

import pytest

from service_data_sync.infrastructure.data_operations.control_plane import (
    DataOperationsControlPlane,
)
from service_data_sync.infrastructure.data_operations.legacy_submission import (
    submit_system_command_group,
)


class RecordingControlPlane:
    """记录内部 command 参数，避免纯幂等规则测试依赖数据库。"""

    def __init__(self) -> None:
        """初始化空调用记录。"""
        self.calls: list[dict[str, Any]] = []

    def submit_system_legacy_command(self, **values: Any) -> dict[str, Any]:
        """保存一次提交并返回稳定测试收据。"""
        self.calls.append(values)
        return {"commandId": "accepted"}


def test_group_submission_is_stable_and_keeps_dataset_codes_unique() -> None:
    """相同计划目标重放必须生成相同键和 submissionId，且保持原目标顺序。"""
    recorder = RecordingControlPlane()
    control_plane = cast(DataOperationsControlPlane, recorder)
    targets = [
        {
            "datasetCode": "equity.bar.1d.raw",
            "mode": "FULL",
            "selector": {"kind": "INSTRUMENT", "exchange": "SSE", "symbol": "600519"},
        },
        {
            "datasetCode": "equity.profile",
            "mode": "INCREMENTAL",
            "selector": {"kind": "INSTRUMENT", "exchange": "SSE", "symbol": "600519"},
        },
    ]

    first = submit_system_command_group(
        control_plane,
        targets=targets,
        reason="股票中心全量回填",
        request_prefix="equity-workspace-plan-v1-security-000001",
    )
    second = submit_system_command_group(
        control_plane,
        targets=targets,
        reason="股票中心全量回填",
        request_prefix="equity-workspace-plan-v1-security-000001",
    )

    assert first == second == {"commandId": "accepted"}
    assert recorder.calls[0]["targets"] == targets
    assert recorder.calls[0]["idempotency_key"] == recorder.calls[1]["idempotency_key"]
    assert recorder.calls[0]["submission_id"] == recorder.calls[1]["submission_id"]


def test_group_submission_rejects_repeated_dataset_before_control_plane() -> None:
    """同 command 内重复数据集会违反数据库唯一约束，父计划必须在预检前拒绝。"""
    recorder = RecordingControlPlane()

    with pytest.raises(ValueError, match="dataset codes must be unique"):
        submit_system_command_group(
            cast(DataOperationsControlPlane, recorder),
            targets=[
                {"datasetCode": "equity.bar.1d.raw"},
                {"datasetCode": "equity.bar.1d.raw"},
            ],
            reason="股票中心全量回填",
            request_prefix="equity-workspace-plan-v1-duplicate",
        )

    assert recorder.calls == []
