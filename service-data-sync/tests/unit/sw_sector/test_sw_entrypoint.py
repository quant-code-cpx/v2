"""申万行业旧 CLI 的安全停用测试。"""

from __future__ import annotations

import pytest

from service_data_sync.application.legacy_entrypoints import LEGACY_ENTRYPOINT_UNAVAILABLE
from service_data_sync.entrypoints import sw_sector


def test_sw_sector_cli_rejects_before_creating_dependencies() -> None:
    """旧申万 CLI 必须拒绝同步与 replay，不能写入独立 publication。"""
    with pytest.raises(SystemExit) as error:
        sw_sector.main(["--snapshot-date", "2026-07-29"])
    assert str(error.value) == f"{LEGACY_ENTRYPOINT_UNAVAILABLE}: data-sync-sw-sector"
