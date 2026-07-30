"""资金流旧 CLI 的安全停用测试。"""

from __future__ import annotations

import pytest

from service_data_sync.application.legacy_entrypoints import LEGACY_ENTRYPOINT_UNAVAILABLE
from service_data_sync.entrypoints import money_flow


def test_money_flow_cli_rejects_before_creating_dependencies() -> None:
    """旧资金流 CLI 必须拒绝任意 capability 与参数，不能直连 canonical 用例。"""
    with pytest.raises(SystemExit) as error:
        money_flow.main(["--capability", "money_flow.daily"])
    assert str(error.value) == f"{LEGACY_ENTRYPOINT_UNAVAILABLE}: data-sync-money-flow"
