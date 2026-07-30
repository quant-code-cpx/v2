"""板块目录旧 CLI 的安全停用测试。"""

from __future__ import annotations

import pytest

from service_data_sync.application.legacy_entrypoints import LEGACY_ENTRYPOINT_UNAVAILABLE
from service_data_sync.entrypoints import sector_catalog


def test_sector_catalog_cli_rejects_before_creating_dependencies() -> None:
    """目录 CLI 必须返回稳定停用码，不能创建容器或访问来源。"""
    with pytest.raises(SystemExit) as error:
        sector_catalog.main(["--scheme", "eastmoney.industry"])
    assert str(error.value) == f"{LEGACY_ENTRYPOINT_UNAVAILABLE}: data-sync-sector-catalog"
