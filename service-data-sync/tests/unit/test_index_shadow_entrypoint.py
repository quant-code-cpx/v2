"""指数影子观测旧 CLI 的安全停用测试。"""

from __future__ import annotations

import pytest

from service_data_sync.application.legacy_entrypoints import LEGACY_ENTRYPOINT_UNAVAILABLE
from service_data_sync.entrypoints import index_shadow


def test_index_shadow_cli_rejects_before_creating_dependencies() -> None:
    """影子 CLI 必须拒绝执行，不能在 command 外写入 research 观察。"""
    with pytest.raises(SystemExit) as error:
        index_shadow.main(["--administrator", "CSI"])
    assert str(error.value) == f"{LEGACY_ENTRYPOINT_UNAVAILABLE}: data-sync-index-shadow"
