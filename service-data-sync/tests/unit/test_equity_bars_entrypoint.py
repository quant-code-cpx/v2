"""个股行情兼容 CLI 的 command 提交边界测试。"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from service_data_sync.entrypoints import equity_bars


class FakeContainer:
    """暴露 CLI 组合根最小形状，并记录资源关闭动作。"""

    def __init__(self) -> None:
        """初始化不连接数据库、对象存储或 Provider 的组合根替身。"""
        self.database = object()
        self.source_registry = object()
        self.trading_calendar = None
        self.closed = False

    def close(self) -> None:
        """记录 CLI 在提交结束后的 finally 资源释放。"""
        self.closed = True


def test_cli_defaults_to_recent_month_and_submits_command(monkeypatch, capsys) -> None:
    """省略开始日时 CLI 只构造 31 天窗口的 command，当前进程不得执行同步。"""
    container = FakeContainer()
    control_plane = object()
    received: dict[str, Any] = {}

    def fake_load_settings() -> SimpleNamespace:
        """返回不含真实环境依赖的设置替身。"""
        return SimpleNamespace()

    def fake_configure_logging(*_args: object, **_kwargs: object) -> None:
        """禁止单元测试配置真实进程日志。"""

    def fake_build_container(_settings: object) -> FakeContainer:
        """返回记录关闭动作的组合根替身。"""
        return container

    def fake_control_plane(**kwargs: object) -> object:
        """验证 CLI 只组合控制面依赖，不构造同步 use case。"""
        assert kwargs["database"] is container.database
        assert kwargs["source_registry"] is container.source_registry
        return control_plane

    def fake_catalog(_settings: object, registry: object) -> dict[str, object]:
        """验证目录仅从组合根注册表构建。"""
        assert registry is container.source_registry
        return {}

    def fake_submit(received_control_plane: object, **kwargs: Any) -> dict[str, str]:
        """记录兼容入口提交的受限 target，并返回稳定受理收据。"""
        assert received_control_plane is control_plane
        received.update(kwargs)
        return {"commandId": "command-bars-1", "status": "QUEUED"}

    monkeypatch.setattr(equity_bars, "load_settings", fake_load_settings)
    monkeypatch.setattr(equity_bars, "configure_logging", fake_configure_logging)
    monkeypatch.setattr(equity_bars, "build_container", fake_build_container)
    monkeypatch.setattr(equity_bars, "DataOperationsControlPlane", fake_control_plane)
    monkeypatch.setattr(equity_bars, "build_catalog", fake_catalog)
    monkeypatch.setattr(equity_bars, "submit_system_command", fake_submit)

    assert equity_bars.main(["--instrument", "SSE.600519", "--end", "2026-07-28"]) == 0

    assert received == {
        "target": {
            "datasetCode": "equity.bar.1d.raw",
            "mode": "DATE_RANGE",
            "selector": {"kind": "INSTRUMENT", "exchange": "SSE", "symbol": "600519"},
            "dateFrom": "2026-06-27",
            "dateTo": "2026-07-28",
            "observationDate": None,
        },
        "reason": "兼容个股行情 CLI 提交",
        "request_prefix": "legacy-equity-bars-cli",
    }
    assert json.loads(capsys.readouterr().out) == {
        "commandId": "command-bars-1",
        "status": "QUEUED",
    }
    assert container.closed is True
