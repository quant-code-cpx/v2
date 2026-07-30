"""板块 EOD 旧 CLI 的安全停用测试。"""

from __future__ import annotations

import pytest

from service_data_sync.application.legacy_entrypoints import LEGACY_ENTRYPOINT_UNAVAILABLE
from service_data_sync.entrypoints import sector_eod


def test_sector_eod_cli_rejects_before_creating_dependencies() -> None:
    """EOD CLI 必须拒绝执行，不能绕过控制面的 publication fence。"""
    with pytest.raises(SystemExit) as error:
        sector_eod.main(["--trade-date", "2026-07-29"])
    assert str(error.value) == f"{LEGACY_ENTRYPOINT_UNAVAILABLE}: data-sync-sector-eod"
