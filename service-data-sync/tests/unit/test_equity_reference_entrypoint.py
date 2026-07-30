"""个股参考数据兼容 CLI 的 command 提交测试。"""

from __future__ import annotations

import json
from typing import Any

import pytest

from service_data_sync.entrypoints import equity_reference


class FakeContainer:
    """提供参考数据 CLI 所需最小控制面组合根，并记录关闭状态。"""

    def __init__(self) -> None:
        """初始化不触发外部数据源、数据库或对象存储访问的替身。"""
        self.source_registry = object()
        self.database = object()
        self.trading_calendar = None
        self.closed = False

    def close(self) -> None:
        """记录 CLI 完成提交后必经的容器释放。"""
        self.closed = True


@pytest.mark.parametrize(
    "capability",
    ["equity.adjustment_factor", "equity.corporate_action", "equity.profile"],
)
def test_reference_cli_submits_each_capability_and_closes_container(
    configured_environment: None,
    monkeypatch,
    capsys,
    capability: str,
) -> None:
    """三个参考能力都只提交严格 command，并按能力保留合法同步模式。"""
    del configured_environment
    container = FakeContainer()
    control_plane = object()
    received: dict[str, Any] = {}

    def fake_configure_logging(*_args: object, **_kwargs: object) -> None:
        """屏蔽测试环境中的真实日志配置。"""

    def fake_build_container(_settings: object) -> FakeContainer:
        """返回不会建立基础设施连接的组合根替身。"""
        return container

    def fake_control_plane(**kwargs: object) -> object:
        """验证兼容 CLI 只装配 command 控制面。"""
        assert kwargs["database"] is container.database
        assert kwargs["source_registry"] is container.source_registry
        return control_plane

    def fake_catalog(_settings: object, registry: object) -> dict[str, object]:
        """验证目录构建读取同一来源注册表。"""
        assert registry is container.source_registry
        return {}

    def fake_submit(received_control_plane: object, **kwargs: Any) -> dict[str, str]:
        """捕获受限 target，证明 CLI 不调用参考数据同步 use case。"""
        assert received_control_plane is control_plane
        received.update(kwargs)
        return {"commandId": f"command-{capability}", "status": "QUEUED"}

    monkeypatch.setattr(equity_reference, "configure_logging", fake_configure_logging)
    monkeypatch.setattr(equity_reference, "build_container", fake_build_container)
    monkeypatch.setattr(equity_reference, "DataOperationsControlPlane", fake_control_plane)
    monkeypatch.setattr(equity_reference, "build_catalog", fake_catalog)
    monkeypatch.setattr(equity_reference, "submit_system_command", fake_submit)

    arguments = [
        "--instrument",
        "SSE.600519",
        "--capability",
        capability,
        "--end",
        "2026-07-28",
        "--full-history",
    ]
    assert equity_reference.main(arguments) == 0

    expected_mode = "INCREMENTAL" if capability == "equity.profile" else "FULL"
    assert received == {
        "target": {
            "datasetCode": capability,
            "mode": expected_mode,
            "selector": {"kind": "INSTRUMENT", "exchange": "SSE", "symbol": "600519"},
            "dateFrom": None,
            "dateTo": None,
            "observationDate": None,
        },
        "reason": "兼容个股参考数据 CLI 提交",
        "request_prefix": "legacy-equity-reference-cli",
    }
    assert json.loads(capsys.readouterr().out) == {
        "commandId": f"command-{capability}",
        "status": "QUEUED",
    }
    assert container.closed is True


def test_reference_cli_rejects_conflicting_or_reversed_windows() -> None:
    """CLI 在创建组合根前拒绝冲突回填参数和倒置日期窗口。"""
    with pytest.raises(SystemExit):
        equity_reference.main(
            [
                "--instrument",
                "SSE.600519",
                "--capability",
                "equity.profile",
                "--start",
                "2026-01-01",
                "--full-history",
            ]
        )
    with pytest.raises(SystemExit, match="start must not be after"):
        equity_reference.main(
            [
                "--instrument",
                "SSE.600519",
                "--capability",
                "equity.profile",
                "--start",
                "2026-07-28",
                "--end",
                "2026-01-01",
            ]
        )
