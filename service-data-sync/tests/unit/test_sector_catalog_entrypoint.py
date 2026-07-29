"""板块目录 CLI 的组合、关闭和机器可读输出单元测试。"""

from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

from service_data_sync.application.sector.catalog_sync import SectorCatalogSyncResult
from service_data_sync.domain.sector import SectorScheme
from service_data_sync.entrypoints import sector_catalog


class FakeRegistry:
    """为目录 CLI 返回一个匹配目录能力的中立来源对象。"""

    def for_capability(self, capability: str) -> tuple[object, ...]:
        """断言 CLI 直接请求目录能力而不是任意行情能力。"""
        assert capability == "sector.catalog.raw"
        return (object(),)


class FakeContainer:
    """模拟组合根必要依赖和 finally 关闭状态。"""

    def __init__(self) -> None:
        """初始化无网络依赖和未关闭标记。"""
        self.database = object()
        self.object_storage = object()
        self.source_registry = FakeRegistry()
        self.closed = False

    def close(self) -> None:
        """记录 CLI 在成功后释放组合根资源。"""
        self.closed = True


class FakeSyncService:
    """避免外部 I/O，返回给定分类体系的确定性目录发布摘要。"""

    def __init__(self, **kwargs: object) -> None:
        """验证 CLI 构造目录用例所需的三个中立依赖。"""
        assert set(kwargs) == {"source", "repository", "raw_payload_store"}

    async def sync(self, *, scheme: SectorScheme) -> SectorCatalogSyncResult:
        """返回行业目录的确定性发布计数。"""
        assert scheme is SectorScheme.EASTMONEY_INDUSTRY
        return SectorCatalogSyncResult(scheme, uuid4(), inserted_count=3, unchanged_count=1)


def test_cli_syncs_one_explicit_scheme_and_closes_composition_root(monkeypatch, capsys) -> None:
    """传入行业分类体系时应渲染目录发布摘要，并始终关闭组合根。"""
    container = FakeContainer()
    # 以下替身只隔离组合根外部依赖，保留 CLI 的参数和资源管理行为。
    monkeypatch.setattr(sector_catalog, "load_settings", lambda: SimpleNamespace())
    monkeypatch.setattr(sector_catalog, "configure_logging", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(sector_catalog, "build_container", lambda _settings: container)
    monkeypatch.setattr(
        sector_catalog, "SqlAlchemySectorMarketDataRepository", lambda _database: object()
    )
    monkeypatch.setattr(sector_catalog, "S3RawPayloadStore", lambda _storage: object())
    # 入口组合测试不触及对象存储；留证语义由原始载荷存储的专用单元测试覆盖。
    monkeypatch.setattr(
        sector_catalog, "retain_failure_evidence", lambda _store, operation: operation()
    )
    monkeypatch.setattr(sector_catalog, "SectorCatalogSyncService", FakeSyncService)

    assert sector_catalog.main(["--scheme", "eastmoney.industry"]) == 0

    rendered = json.loads(capsys.readouterr().out)
    assert rendered["sector_scheme"] == "eastmoney.industry"
    assert rendered["inserted_count"] == 3
    assert container.closed is True
