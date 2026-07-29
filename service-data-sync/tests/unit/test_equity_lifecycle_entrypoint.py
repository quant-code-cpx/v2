"""显式上市生命周期 CLI 的组合根、来源策略和 JSON 输出测试。"""

from __future__ import annotations

import json
from datetime import date, datetime
from types import SimpleNamespace
from uuid import uuid4

from service_data_sync.application.equity.lifecycle_sync import EquityLifecycleSyncResult
from service_data_sync.application.ports.equity_master import PublishedCnAAggregate
from service_data_sync.domain.equity import Exchange
from service_data_sync.entrypoints import equity_lifecycle


class FakeRegistry:
    """仅返回一个显式生命周期来源，验证 CLI 不会猜测多来源优先级。"""

    def for_capability(self, capability: str) -> tuple[object, ...]:
        """断言中立能力名称后返回不透明来源对象。"""
        assert capability == "equity.lifecycle.explicit"
        return (object(),)


class FakeContainer:
    """提供生命周期 CLI 组合根的最小可关闭形状。"""

    def __init__(self) -> None:
        """初始化不连接外部服务的依赖替身。"""
        self.database = object()
        self.object_storage = object()
        self.source_registry = FakeRegistry()
        self.closed = False

    def close(self) -> None:
        """记录 main 的 finally 块是否释放组合根资源。"""
        self.closed = True


class FakeSyncService:
    """替代真实来源和数据库，返回确定性的生命周期发布摘要。"""

    def __init__(self, **kwargs: object) -> None:
        """验证 CLI 只组装中立来源、仓储和原始证据存储。"""
        assert set(kwargs) == {"source", "repository", "raw_payload_store"}

    async def sync(self, **kwargs: object) -> EquityLifecycleSyncResult:
        """校验 CLI 传入的交易所和目标日，并返回成功结果。"""
        exchange = kwargs["exchange"]
        assert isinstance(exchange, Exchange)
        assert kwargs["target_date"] == date(2026, 7, 27)
        return EquityLifecycleSyncResult(
            exchange=exchange,
            snapshot_id=uuid4(),
            data_version=uuid4(),
            inserted_count=1,
            unchanged_count=0,
        )


class FakeLifecycleRepository:
    """接收数据库替身，防止 CLI 测试接触真实 PostgreSQL。"""

    def __init__(self, _database: object) -> None:
        """验证生命周期仓储从组合根取得数据库。"""


class FakeMasterRepository:
    """为全市场模式返回稳定聚合发布结果。"""

    def __init__(self, _database: object) -> None:
        """验证聚合仓储使用同一组合根数据库。"""

    def publish_cn_a_aggregate(self) -> PublishedCnAAggregate:
        """返回确定性的三所稳定聚合版本。"""
        return PublishedCnAAggregate(data_version=uuid4(), published_at=datetime.now())


def test_cli_runs_one_exchange_lifecycle_and_closes_composition_root(monkeypatch, capsys) -> None:
    """单所 CLI 必须输出机器可读摘要，并在 finally 中关闭依赖。"""
    container = FakeContainer()
    monkeypatch.setattr(equity_lifecycle, "load_settings", lambda: SimpleNamespace())
    monkeypatch.setattr(equity_lifecycle, "configure_logging", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(equity_lifecycle, "build_container", lambda _settings: container)
    monkeypatch.setattr(
        equity_lifecycle,
        "SqlAlchemyEquityLifecycleRepository",
        FakeLifecycleRepository,
    )
    monkeypatch.setattr(equity_lifecycle, "SqlAlchemyEquityMasterRepository", FakeMasterRepository)
    monkeypatch.setattr(equity_lifecycle, "S3RawPayloadStore", lambda _storage: object())
    # 入口组合测试不触及对象存储；留证语义由原始载荷存储的专用单元测试覆盖。
    monkeypatch.setattr(
        equity_lifecycle, "retain_failure_evidence", lambda _store, operation: operation()
    )
    monkeypatch.setattr(equity_lifecycle, "EquityLifecycleSyncService", FakeSyncService)

    assert equity_lifecycle.main(["--exchange", "SSE", "--target-date", "2026-07-27"]) == 0

    rendered = json.loads(capsys.readouterr().out)
    assert rendered["items"][0]["exchange"] == "SSE"
    assert rendered["aggregate_data_version"] is None
    assert container.closed is True


def test_cli_syncs_three_exchanges_before_publishing_aggregate(monkeypatch, capsys) -> None:
    """全市场模式必须等三所生命周期 child 全部完成后才更新稳定聚合。"""
    container = FakeContainer()
    monkeypatch.setattr(equity_lifecycle, "load_settings", lambda: SimpleNamespace())
    monkeypatch.setattr(equity_lifecycle, "configure_logging", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(equity_lifecycle, "build_container", lambda _settings: container)
    monkeypatch.setattr(
        equity_lifecycle,
        "SqlAlchemyEquityLifecycleRepository",
        FakeLifecycleRepository,
    )
    monkeypatch.setattr(equity_lifecycle, "SqlAlchemyEquityMasterRepository", FakeMasterRepository)
    monkeypatch.setattr(equity_lifecycle, "S3RawPayloadStore", lambda _storage: object())
    # 入口组合测试不触及对象存储；留证语义由原始载荷存储的专用单元测试覆盖。
    monkeypatch.setattr(
        equity_lifecycle, "retain_failure_evidence", lambda _store, operation: operation()
    )
    monkeypatch.setattr(equity_lifecycle, "EquityLifecycleSyncService", FakeSyncService)

    assert equity_lifecycle.main(["--all-exchanges", "--target-date", "2026-07-27"]) == 0

    rendered = json.loads(capsys.readouterr().out)
    assert {item["exchange"] for item in rendered["items"]} == {"SSE", "SZSE", "BSE"}
    assert rendered["aggregate_data_version"] is not None
