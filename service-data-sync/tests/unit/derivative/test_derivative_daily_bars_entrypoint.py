"""衍生品日线旧 CLI 的安全停用测试。"""

from __future__ import annotations

import pytest

from service_data_sync.application.legacy_entrypoints import LEGACY_ENTRYPOINT_UNAVAILABLE
from service_data_sync.entrypoints import derivative_daily_bars


def test_derivative_daily_bars_cli_rejects_before_creating_dependencies() -> None:
    """合约日线 CLI 必须拒绝执行，不能直接访问来源或发布 canonical 数据。"""
    with pytest.raises(SystemExit) as error:
        derivative_daily_bars.main(["--contract", "CFFEX.IF2608"])
    assert str(error.value) == f"{LEGACY_ENTRYPOINT_UNAVAILABLE}: data-sync-derivative-bars"
