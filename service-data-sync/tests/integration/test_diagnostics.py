from __future__ import annotations

import os

import pytest

from service_data_sync.bootstrap.settings import load_settings
from service_data_sync.entrypoints.diagnostics import DiagnosticsExitCode, diagnose


@pytest.mark.integration
def test_diagnostics_reaches_local_infrastructure() -> None:
    """Probe opt-in local Compose infrastructure through real diagnostics clients."""
    if os.environ.get("DATA_SYNC_RUN_INTEGRATION") != "1":
        pytest.skip("set DATA_SYNC_RUN_INTEGRATION=1 after starting local infrastructure")

    report = diagnose(load_settings())

    assert report.exit_code is DiagnosticsExitCode.OK
