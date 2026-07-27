"""证券目录 CLI 的组合根、来源策略和 JSON 输出单元测试。"""

from __future__ import annotations

import json
from datetime import date, datetime
from types import SimpleNamespace
from uuid import uuid4

from service_data_sync.application.equity.master_catalog_sync import EquityCatalogSyncResult
from service_data_sync.application.ports.equity_master import PublishedCnAAggregate
from service_data_sync.domain.equity import Exchange
from service_data_sync.entrypoints import equity_catalog


class FakeRegistry:
    """只提供一个目录来源，使 CLI 可验证其来源选择规则。"""

    def for_capability(self, capability: str) -> tuple[object, ...]:
        """断言中立能力名称后返回不透明来源对象。"""
        assert capability == "equity.master.catalog"
        return (object(),)


class FakeContainer:
    """提供目录 CLI 所需的组合根形状并记录关闭调用。"""

    def __init__(self) -> None:
        """初始化不会连接外部服务的最小依赖替身。"""
        self.database = object()
        self.object_storage = object()
        self.source_registry = FakeRegistry()
        self.closed = False

    def close(self) -> None:
        """记录 `finally` 块释放组合根资源的行为。"""
        self.closed = True


class FakeSyncService:
    """跳过真实数据源和数据库，返回可预测的目录发布结果。"""

    def __init__(self, **kwargs: object) -> None:
        """验证 CLI 组装了标准用例的三个依赖。"""
        assert set(kwargs) == {"source", "repository", "raw_payload_store"}

    async def sync(self, **kwargs: object) -> EquityCatalogSyncResult:
        """校验解析后的交易所和日期，并返回确定性成功摘要。"""
        exchange = kwargs["exchange"]
        assert isinstance(exchange, Exchange)
        assert kwargs["target_date"] == date(2026, 7, 27)
        return EquityCatalogSyncResult(
            exchange=exchange,
            snapshot_id=uuid4(),
            data_version=uuid4(),
            inserted_count=3,
            unchanged_count=1,
        )


class FakeRepository:
    """为全市场 CLI 测试提供稳定 aggregate 发布返回值。"""

    def __init__(self, _database: object) -> None:
        """接收组合根数据库替身，但不执行实际持久化。"""
        pass

    def publish_cn_a_aggregate(self) -> PublishedCnAAggregate:
        """返回确定性形状的稳定全市场发布。"""
        return PublishedCnAAggregate(data_version=uuid4(), published_at=datetime.now())


def test_cli_runs_one_exchange_catalog_and_closes_composition_root(monkeypatch, capsys) -> None:
    """CLI 应渲染机器可读摘要，并无论结果如何在 finally 中关闭依赖。"""
    container = FakeContainer()
    # 匿名替身只替换组合根依赖，不允许该单测触及 S3、PostgreSQL 或 AKShare。
    monkeypatch.setattr(equity_catalog, "load_settings", lambda: SimpleNamespace())
    monkeypatch.setattr(equity_catalog, "configure_logging", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(equity_catalog, "build_container", lambda _settings: container)
    monkeypatch.setattr(equity_catalog, "SqlAlchemyEquityMasterRepository", FakeRepository)
    monkeypatch.setattr(equity_catalog, "S3RawPayloadStore", lambda _storage: object())
    monkeypatch.setattr(equity_catalog, "EquityCatalogSyncService", FakeSyncService)

    assert equity_catalog.main(["--exchange", "SSE", "--target-date", "2026-07-27"]) == 0

    rendered = json.loads(capsys.readouterr().out)
    assert rendered["items"][0]["exchange"] == "SSE"
    assert rendered["items"][0]["inserted_count"] == 3
    assert rendered["aggregate_data_version"] is None
    assert container.closed is True


def test_cli_syncs_three_exchanges_before_publishing_aggregate(monkeypatch, capsys) -> None:
    """全市场模式必须完成三个 child 同步后才调用稳定 aggregate 发布。"""
    container = FakeContainer()
    monkeypatch.setattr(equity_catalog, "load_settings", lambda: SimpleNamespace())
    monkeypatch.setattr(equity_catalog, "configure_logging", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(equity_catalog, "build_container", lambda _settings: container)
    monkeypatch.setattr(equity_catalog, "SqlAlchemyEquityMasterRepository", FakeRepository)
    monkeypatch.setattr(equity_catalog, "S3RawPayloadStore", lambda _storage: object())
    monkeypatch.setattr(equity_catalog, "EquityCatalogSyncService", FakeSyncService)

    assert equity_catalog.main(["--all-exchanges", "--target-date", "2026-07-27"]) == 0

    rendered = json.loads(capsys.readouterr().out)
    assert {item["exchange"] for item in rendered["items"]} == {"SSE", "SZSE", "BSE"}
    assert rendered["aggregate_data_version"] is not None
