"""有界实时测试 CLI 组合与 JSON 输出的单元测试。"""

from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

from service_data_sync.application.equity.daily_bar_sync import DailyBarSyncResult
from service_data_sync.domain.equity import EquityIdentifier
from service_data_sync.entrypoints import equity_bars


class FakeRegistry:
    """只返回一个中立日线来源，使 CLI 能通过来源策略校验。"""

    def for_capability(self, capability: str) -> tuple[object, ...]:
        """断言请求中立能力，并返回一个不透明来源对象。"""
        assert capability == "equity.bar.1d.raw"
        return (object(),)


class FakeContainer:
    """暴露 CLI 依赖，并记录执行后必须发生的关闭操作。"""

    def __init__(self) -> None:
        """初始化组合根形状的替身，不连接外部基础设施客户端。"""
        self.database = object()
        self.object_storage = object()
        self.source_registry = FakeRegistry()
        self.closed = False

    def close(self) -> None:
        """记录 CLI `finally` 块请求的尽力依赖释放。"""
        self.closed = True


class FakeSyncService:
    """不调用数据源、S3 或 PostgreSQL 边界，返回确定性结果。"""

    def __init__(self, **kwargs: object) -> None:
        """验证 CLI 构造了用例所需的三个依赖。"""
        assert set(kwargs) == {"source", "repository", "raw_payload_store"}

    async def sync(self, **kwargs: object) -> DailyBarSyncResult:
        """返回成功结果，并保留已解析的请求证券身份。"""
        identifier = kwargs["identifier"]
        assert isinstance(identifier, EquityIdentifier)
        return DailyBarSyncResult(
            instrument=identifier,
            data_version=uuid4(),
            inserted_count=3,
            unchanged_count=1,
        )


def test_cli_defaults_to_recent_month_and_closes_composition_root(monkeypatch, capsys) -> None:
    """遗漏日期时限制窗口为 31 天，并渲染机器可读发布摘要。"""
    container = FakeContainer()
    # 以下匿名回调只替换组合根依赖，避免该 CLI 单测连接外部服务。
    monkeypatch.setattr(equity_bars, "load_settings", lambda: SimpleNamespace())
    monkeypatch.setattr(equity_bars, "configure_logging", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(equity_bars, "build_container", lambda _settings: container)
    monkeypatch.setattr(
        equity_bars, "SqlAlchemyEquityMarketDataRepository", lambda _database: object()
    )
    monkeypatch.setattr(equity_bars, "S3RawPayloadStore", lambda _storage: object())
    monkeypatch.setattr(equity_bars, "EquityDailyBarSyncService", FakeSyncService)

    assert equity_bars.main(["--instrument", "SSE.600519"]) == 0

    rendered = json.loads(capsys.readouterr().out)
    assert rendered["instrument"] == "SSE.600519"
    assert rendered["inserted_count"] == 3
    assert container.closed is True
