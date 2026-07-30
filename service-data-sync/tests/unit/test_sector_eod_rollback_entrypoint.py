"""板块 EOD 旧 rollback CLI 的安全停用测试。"""

from __future__ import annotations

import pytest

from service_data_sync.application.legacy_entrypoints import LEGACY_ENTRYPOINT_UNAVAILABLE
from service_data_sync.entrypoints import sector_eod_rollback


def test_sector_eod_rollback_cli_rejects_before_creating_dependencies() -> None:
    """旧 rollback 必须拒绝执行，不能在没有当前 fencing token 时变更发布版本。"""
    with pytest.raises(SystemExit) as error:
        sector_eod_rollback.main(["--revision", "1"])
    assert str(error.value) == f"{LEGACY_ENTRYPOINT_UNAVAILABLE}: data-sync-sector-eod-rollback"
