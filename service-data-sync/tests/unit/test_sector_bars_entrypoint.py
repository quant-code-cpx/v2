"""板块行情旧 CLI 的安全停用测试。"""

from __future__ import annotations

import pytest

from service_data_sync.application.legacy_entrypoints import LEGACY_ENTRYPOINT_UNAVAILABLE
from service_data_sync.entrypoints import sector_bars


def test_sector_bars_cli_rejects_before_creating_dependencies() -> None:
    """行情 CLI 必须拒绝执行，不能直接调用板块行情同步用例。"""
    with pytest.raises(SystemExit) as error:
        sector_bars.main(["--sector", "BK0475"])
    assert str(error.value) == f"{LEGACY_ENTRYPOINT_UNAVAILABLE}: data-sync-sector-bars"
