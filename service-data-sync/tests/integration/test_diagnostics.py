"""本地 Compose 基础设施诊断的集成测试。"""

from __future__ import annotations

import os

import pytest

from service_data_sync.bootstrap.settings import load_settings
from service_data_sync.entrypoints.diagnostics import DiagnosticsExitCode, diagnose


@pytest.mark.integration
def test_diagnostics_reaches_local_infrastructure() -> None:
    """通过真实诊断客户端探测显式启用的本地 Compose 基础设施。"""
    if os.environ.get("DATA_SYNC_RUN_INTEGRATION") != "1":
        pytest.skip("set DATA_SYNC_RUN_INTEGRATION=1 after starting local infrastructure")

    report = diagnose(load_settings())

    assert report.exit_code is DiagnosticsExitCode.OK
