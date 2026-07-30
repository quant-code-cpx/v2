"""证券生命周期旧 CLI 的安全停用测试。"""

from __future__ import annotations

import pytest

from service_data_sync.application.legacy_entrypoints import LEGACY_ENTRYPOINT_UNAVAILABLE
from service_data_sync.entrypoints import equity_lifecycle


def test_equity_lifecycle_cli_rejects_before_creating_dependencies() -> None:
    """生命周期 CLI 必须拒绝执行，不能直接推进 checkpoint 或终态。"""
    with pytest.raises(SystemExit) as error:
        equity_lifecycle.main(["--exchange", "SSE"])
    assert str(error.value) == f"{LEGACY_ENTRYPOINT_UNAVAILABLE}: data-sync-equity-lifecycle"
