"""板块成分旧 CLI 的安全停用测试。"""

from __future__ import annotations

import pytest

from service_data_sync.application.legacy_entrypoints import LEGACY_ENTRYPOINT_UNAVAILABLE
from service_data_sync.entrypoints import sector_membership


def test_sector_membership_cli_rejects_before_creating_dependencies() -> None:
    """成分 CLI 必须拒绝执行，不能创建独立 run ledger 或 release。"""
    with pytest.raises(SystemExit) as error:
        sector_membership.main(["--observation-date", "2026-07-29"])
    assert str(error.value) == f"{LEGACY_ENTRYPOINT_UNAVAILABLE}: data-sync-sector-membership"
