"""板块 EOD 旧 reaper CLI 的安全停用测试。"""

from __future__ import annotations

import pytest

from service_data_sync.application.legacy_entrypoints import LEGACY_ENTRYPOINT_UNAVAILABLE
from service_data_sync.entrypoints import sector_eod_reaper


def test_sector_eod_reaper_cli_rejects_before_creating_dependencies() -> None:
    """旧 reaper 必须拒绝执行，恢复只允许 data-operations reaper 处理。"""
    with pytest.raises(SystemExit) as error:
        sector_eod_reaper.main([])
    assert str(error.value) == f"{LEGACY_ENTRYPOINT_UNAVAILABLE}: data-sync-sector-eod-reaper"
