"""指数 P0-A 影子 CLI 的参数、来源隔离和机器可读输出测试。"""

from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from service_data_sync.application.index.shadow_sync import IndexShadowSyncResult
from service_data_sync.application.ports.index_shadow import StoredIndexShadowObservation
from service_data_sync.domain.index import IndexAdministrator, IndexCapability, IndexIdentifier
from service_data_sync.entrypoints import index_shadow


class FakeSource:
    """提供管理人绑定的中立来源标识，不访问网络或供应商 SDK。"""

    def __init__(self, provider_id: str) -> None:
        """保存用于断言来源选择规则的稳定 adapter 标识。"""
        self.provider_id = provider_id


class FakeRegistry:
    """记录 CLI 请求的能力，并返回预置的中立来源集合。"""

    def __init__(self, sources: tuple[FakeSource, ...]) -> None:
        """保存允许测试组合来源策略的候选集合。"""
        self._sources = sources
        self.requested_capabilities: list[str] = []

    def for_capability(self, capability: str) -> tuple[FakeSource, ...]:
        """记录请求能力后返回该测试所声明的候选 adapter。"""
        self.requested_capabilities.append(capability)
        return self._sources


class FakeContainer:
    """提供 CLI 所需组合根形状，并记录资源是否在 finally 中释放。"""

    def __init__(self, registry: FakeRegistry) -> None:
        """初始化无外部依赖的数据库、对象存储和来源注册表替身。"""
        self.database = object()
        self.object_storage = object()
        self.source_registry = registry
        self.closed = False

    def close(self) -> None:
        """记录 CLI 在成功或失败后释放组合根。"""
        self.closed = True


class FakeSyncService:
    """记录用例调用，避免单元测试触及 S3、数据库和外部指数来源。"""

    def __init__(self, **kwargs: object) -> None:
        """验证 CLI 只向应用用例注入三个中立依赖。"""
        assert set(kwargs) == {"source", "repository", "raw_payload_store"}

    async def sync_catalog(self, *, administrator: IndexAdministrator) -> IndexShadowSyncResult:
        """返回可预测目录观察，用于校验不带指数代码的调用路径。"""
        assert administrator is IndexAdministrator.CSI
        return _result(IndexCapability.CATALOG_SNAPSHOT)

    async def sync_snapshot(
        self, *, identifier: IndexIdentifier, capability: IndexCapability
    ) -> IndexShadowSyncResult:
        """验证精确身份和能力后返回确定性快照观察。"""
        assert identifier == IndexIdentifier(IndexAdministrator.CNI, "399001")
        assert capability is IndexCapability.WEIGHT_SNAPSHOT
        return _result(capability)


def test_cli_runs_catalog_with_its_administrator_bound_source(monkeypatch, capsys) -> None:
    """中证目录 CLI 应选择中证 adapter、输出研究态摘要并关闭组合根。"""
    source = FakeSource("akshare-csindex-index-snapshot")
    container = _install(monkeypatch, (source,))

    assert (
        index_shadow.main(["--administrator", "CSI", "--capability", "index.catalog.snapshot"]) == 0
    )

    rendered = json.loads(capsys.readouterr().out)
    assert rendered["administrator"] == "CSI"
    assert rendered["publication_created"] is False
    assert container.source_registry.requested_capabilities == ["index.catalog.snapshot"]
    assert container.closed is True


def test_cli_runs_weight_snapshot_with_explicit_index_code(monkeypatch, capsys) -> None:
    """权重同步必须携带单一指数代码，并保持 CNI 的来源隔离。"""
    container = _install(monkeypatch, (FakeSource("akshare-cnindex-index-snapshot"),))

    assert (
        index_shadow.main(
            [
                "--administrator",
                "CNI",
                "--capability",
                "index.weight.snapshot",
                "--index-code",
                "399001",
            ]
        )
        == 0
    )

    rendered = json.loads(capsys.readouterr().out)
    assert rendered["capability"] == "index.weight.snapshot"
    assert rendered["quality_status"] == "warned"
    assert container.closed is True


def test_cli_rejects_missing_index_code_without_creating_container() -> None:
    """成分和权重观察缺少白名单指数代码时必须在组合外失败。"""
    with pytest.raises(SystemExit):
        index_shadow.main(["--administrator", "CSI", "--capability", "index.constituent.snapshot"])


def test_cli_returns_observable_empty_state_for_unregistered_administrator_source(
    monkeypatch, capsys
) -> None:
    """组合策略缺少管理人 adapter 时不回退到其他来源，而是成功返回空研究态。"""
    _install(monkeypatch, (FakeSource("akshare-csindex-index-snapshot"),))

    assert (
        index_shadow.main(
            [
                "--administrator",
                "CNI",
                "--capability",
                "index.weight.snapshot",
                "--index-code",
                "399001",
            ]
        )
        == 0
    )

    rendered = json.loads(capsys.readouterr().out)
    assert rendered["availability"] == "source_unavailable"
    assert rendered["item_count"] == 0


def _install(monkeypatch: pytest.MonkeyPatch, sources: tuple[FakeSource, ...]) -> FakeContainer:
    """注入无 I/O 组合依赖，保留 CLI 参数、来源选择和关闭行为。"""
    container = FakeContainer(FakeRegistry(sources))
    monkeypatch.setattr(index_shadow, "load_settings", lambda: SimpleNamespace())
    monkeypatch.setattr(index_shadow, "configure_logging", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(index_shadow, "build_container", lambda _settings: container)
    monkeypatch.setattr(index_shadow, "SqlAlchemyIndexShadowRepository", lambda _database: object())
    monkeypatch.setattr(
        index_shadow, "SqlAlchemyDatasetAvailabilityRepository", FakeAvailabilityRepository
    )
    monkeypatch.setattr(index_shadow, "S3RawPayloadStore", lambda _storage: object())
    # 入口组合测试不触及对象存储；留证语义由原始载荷存储的专用单元测试覆盖。
    monkeypatch.setattr(
        index_shadow, "retain_failure_evidence", lambda _store, operation: operation()
    )
    monkeypatch.setattr(index_shadow, "IndexShadowSyncService", FakeSyncService)
    return container


class FakeAvailabilityRepository:
    """记录来源缺失观测，避免入口测试连接真实数据库。"""

    def __init__(self, _database: object) -> None:
        """接收但不使用组合根数据库替身。"""

    def record(self, **kwargs: object) -> None:
        """验证入口只记录无敏感信息的来源不可用元数据。"""
        assert kwargs["availability"] == "source_unavailable"


def _result(capability: IndexCapability) -> IndexShadowSyncResult:
    """构造不含 dataVersion 的确定性研究态观察结果。"""
    return IndexShadowSyncResult(
        capability=capability.value,
        observation=StoredIndexShadowObservation(uuid4(), item_count=2, quality_status="warned"),
    )
