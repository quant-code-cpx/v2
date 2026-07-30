"""证券目录旧 CLI 的安全停用测试。"""

from __future__ import annotations

import pytest

from service_data_sync.application.legacy_entrypoints import LEGACY_ENTRYPOINT_UNAVAILABLE
from service_data_sync.entrypoints import equity_catalog


def test_equity_catalog_cli_rejects_before_creating_dependencies() -> None:
    """目录 CLI 必须拒绝执行，不能直接写入证券主数据与 publication。"""
    with pytest.raises(SystemExit) as error:
        equity_catalog.main(["--exchange", "SSE"])
    assert str(error.value) == f"{LEGACY_ENTRYPOINT_UNAVAILABLE}: data-sync-equity-catalog"
