"""财务与估值 dark-launch probe 的无 egress 回归测试。"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from service_data_sync.bootstrap.settings import Settings, load_settings
from service_data_sync.infrastructure.messaging import financial_tasks
from service_data_sync.infrastructure.messaging.celery_app import create_worker_app


@dataclass(frozen=True)
class FakeProvider:
    """提供稳定 adapter 标识，避免测试构造真实来源实现。"""

    provider_id: str


class FakeRegistry:
    """按能力返回预置 adapter，记录 probe 只读取声明能力。"""

    def __init__(self, providers_by_capability: dict[str, tuple[FakeProvider, ...]]) -> None:
        """保存能力到 adapter 的测试映射和请求记录。"""
        self._providers_by_capability = providers_by_capability
        self.requested_capabilities: set[str] = set()

    def for_capability(self, capability: str) -> tuple[FakeProvider, ...]:
        """记录 capability 查询；不提供任何会触发网络访问的 `fetch` 方法。"""
        self.requested_capabilities.add(capability)
        return self._providers_by_capability.get(capability, ())


class CapturedLogger:
    """收集结构化 probe 日志，验证结果不携带来源响应或秘密。"""

    def __init__(self) -> None:
        """初始化空事件列表。"""
        self.events: list[dict[str, object]] = []

    def info(self, event: str, **fields: object) -> None:
        """记录信息级 probe 事件。"""
        self.events.append({"event": event, **fields})


class FakeDatabaseClient:
    """记录同步任务数据库资源是否在成功与失败后关闭。"""

    instances: list[FakeDatabaseClient] = []

    def __init__(self) -> None:
        """创建未关闭的数据库测试资源。"""
        self.closed = False
        self.instances.append(self)

    @classmethod
    def from_settings(cls, settings: Settings) -> FakeDatabaseClient:
        """忽略真实连接配置并返回可观察测试实例。"""
        del settings
        return cls()

    def close(self) -> None:
        """标记数据库连接池已经释放。"""
        self.closed = True


class FakeObjectStorageClient:
    """记录同步任务对象存储资源是否在成功与失败后关闭。"""

    instances: list[FakeObjectStorageClient] = []

    def __init__(self) -> None:
        """创建未关闭的对象存储测试资源。"""
        self.closed = False
        self.instances.append(self)

    @classmethod
    def from_settings(cls, settings: Settings) -> FakeObjectStorageClient:
        """忽略真实对象存储配置并返回可观察测试实例。"""
        del settings
        return cls()

    def close(self) -> None:
        """标记对象存储客户端已经释放。"""
        self.closed = True


class FakeRepository:
    """接受同步任务传入的数据库资源，不执行真实 SQL。"""

    def __init__(self, database: FakeDatabaseClient) -> None:
        """保存数据库资源以证明组合根传参正确。"""
        self.database = database


class FakeRawStore:
    """接受同步任务传入的对象存储资源，不执行真实上传。"""

    def __init__(self, object_storage: FakeObjectStorageClient) -> None:
        """保存对象存储资源以证明组合根传参正确。"""
        self.object_storage = object_storage


class FakeFinancialSyncService:
    """返回确定性三能力摘要，或按测试开关模拟同步失败。"""

    should_fail = False
    calls: list[tuple[str, str]] = []

    def __init__(self, **dependencies: object) -> None:
        """保存任务组合的来源、仓储和 raw store 依赖。"""
        self.dependencies = dependencies

    async def sync_security(self, *, exchange: object, symbol: str) -> SimpleNamespace:
        """记录证券身份，并返回三个独立能力的插入数。"""
        self.calls.append((str(exchange), symbol))
        if self.should_fail:
            raise RuntimeError("canonical publish failed")
        return SimpleNamespace(
            reports=SimpleNamespace(inserted_count=2),
            provider_metrics=SimpleNamespace(inserted_count=3),
            valuations=SimpleNamespace(inserted_count=5),
        )


def test_probe_returns_disabled_without_building_source_registry(
    configured_environment: None,
    monkeypatch,
) -> None:
    """默认关闭时 probe 不得初始化 adapter，更不得触发任何 egress。"""
    app = create_worker_app(load_settings())

    def unexpected_registry(_settings: object) -> object:
        """若默认关闭仍尝试组合来源，立即使测试失败。"""
        raise AssertionError("disabled probe must not build source registry")

    monkeypatch.setattr(financial_tasks, "build_source_registry", unexpected_registry)

    result = app.tasks["service_data_sync.financial.probe"].run()

    assert result == {"status": "disabled", "capabilityCount": 0, "providerCount": 0}


def test_probe_reports_missing_adapter_without_calling_provider(
    configured_environment: None,
    monkeypatch,
) -> None:
    """已配策略但未注册完整 adapter 时，只报告阻断状态且不访问来源。"""
    settings = load_settings().model_copy(
        update={
            "financial_enabled": True,
            "financial_source_policy": "research-policy-pending",
            "financial_max_concurrency": 1,
            "financial_requests_per_minute": 1,
            "financial_request_timeout_seconds": 1,
        }
    )
    app = create_worker_app(settings)
    registry = FakeRegistry({})
    logger = CapturedLogger()

    monkeypatch.setattr(financial_tasks, "build_source_registry", lambda _settings: registry)
    monkeypatch.setattr(financial_tasks, "_LOGGER", logger)

    result = app.tasks["service_data_sync.financial.probe"].run()

    assert result == {
        "status": "provider-adapter-unavailable",
        "capabilityCount": 0,
        "providerCount": 0,
    }
    assert registry.requested_capabilities == {
        "financial.statement.raw",
        "financial.metric.raw",
        "financial.valuation.raw",
    }
    assert logger.events == [
        {
            "event": "financial.dark_launch_probe_completed",
            "status": "provider-adapter-unavailable",
            "source_policy": "research-policy-pending",
            "capability_count": 0,
            "provider_count": 0,
        }
    ]


def test_probe_reports_declared_capabilities_without_selecting_a_source_composition(
    configured_environment: None,
    monkeypatch,
) -> None:
    """probe 只确认三类能力均有 adapter，不选择或合并额外来源。"""
    settings = load_settings().model_copy(
        update={
            "financial_enabled": True,
            "financial_source_policy": "research-policy-pending",
            "financial_max_concurrency": 1,
            "financial_requests_per_minute": 1,
            "financial_request_timeout_seconds": 1,
        }
    )
    app = create_worker_app(settings)
    provider = FakeProvider(provider_id="test-financial")
    registry = FakeRegistry(
        {
            "financial.statement.raw": (provider,),
            "financial.metric.raw": (provider,),
            "financial.valuation.raw": (provider,),
        }
    )

    monkeypatch.setattr(financial_tasks, "build_source_registry", lambda _settings: registry)

    result = app.tasks["service_data_sync.financial.probe"].run()

    assert result == {
        "status": "sync-ready",
        "capabilityCount": 3,
        "providerCount": 1,
    }


def test_sync_task_rejects_disabled_feature_before_building_registry(
    configured_environment: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """开关关闭时同步任务不得构造来源、数据库或对象存储。"""
    app = create_worker_app(load_settings())

    def unexpected_registry(settings: Settings) -> object:
        """若关闭状态仍构造来源注册表则立即失败。"""
        del settings
        raise AssertionError("disabled sync must not build registry")

    monkeypatch.setattr(financial_tasks, "build_source_registry", unexpected_registry)

    with pytest.raises(RuntimeError, match="disabled"):
        app.tasks["service_data_sync.financial.sync_security"].run("SSE", "600519")


@pytest.mark.parametrize("provider_count", [0, 2])
def test_sync_task_requires_exactly_one_financial_provider(
    configured_environment: None,
    monkeypatch: pytest.MonkeyPatch,
    provider_count: int,
) -> None:
    """零个或多个财务 adapter 都不能进入数据库写入，避免来源合并语义不明。"""
    settings = _enabled_settings()
    app = create_worker_app(settings)
    providers = tuple(
        FakeProvider(provider_id=f"provider-{index}") for index in range(provider_count)
    )
    registry = FakeRegistry({"financial.statement.raw": providers})

    def configured_registry(current: Settings) -> FakeRegistry:
        """返回当前测试指定数量的三表来源。"""
        del current
        return registry

    monkeypatch.setattr(financial_tasks, "build_source_registry", configured_registry)

    with pytest.raises(RuntimeError, match="exactly one"):
        app.tasks["service_data_sync.financial.sync_security"].run("SSE", "600519")


def test_sync_task_returns_three_counts_and_always_closes_resources(
    configured_environment: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """成功同步应返回三能力摘要，并在结果形成后释放数据库和对象存储。"""
    _install_sync_task_fakes(monkeypatch)
    FakeFinancialSyncService.should_fail = False
    FakeFinancialSyncService.calls.clear()
    settings = _enabled_settings()
    app = create_worker_app(settings)
    provider = FakeProvider(provider_id="only-provider")

    def configured_registry(current: Settings) -> FakeRegistry:
        """为同步任务返回唯一可选财务来源。"""
        del current
        return FakeRegistry({"financial.statement.raw": (provider,)})

    monkeypatch.setattr(financial_tasks, "build_source_registry", configured_registry)

    result = app.tasks["service_data_sync.financial.sync_security"].run("SSE", "600519")

    assert result == {
        "reportInserted": 2,
        "metricInserted": 3,
        "valuationInserted": 5,
    }
    assert FakeFinancialSyncService.calls == [("SSE", "600519")]
    assert FakeDatabaseClient.instances[-1].closed is True
    assert FakeObjectStorageClient.instances[-1].closed is True


def test_sync_task_closes_resources_when_canonical_publish_fails(
    configured_environment: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同步或 canonical 发布异常不得泄漏连接池，错误仍应向 Celery 调用方传播。"""
    _install_sync_task_fakes(monkeypatch)
    FakeFinancialSyncService.should_fail = True
    settings = _enabled_settings()
    app = create_worker_app(settings)
    provider = FakeProvider(provider_id="only-provider")

    def configured_registry(current: Settings) -> FakeRegistry:
        """为失败路径返回唯一财务来源，使异常发生在资源创建之后。"""
        del current
        return FakeRegistry({"financial.statement.raw": (provider,)})

    monkeypatch.setattr(financial_tasks, "build_source_registry", configured_registry)

    with pytest.raises(RuntimeError, match="canonical publish failed"):
        app.tasks["service_data_sync.financial.sync_security"].run("SSE", "600519")

    assert FakeDatabaseClient.instances[-1].closed is True
    assert FakeObjectStorageClient.instances[-1].closed is True
    FakeFinancialSyncService.should_fail = False


def _enabled_settings() -> Settings:
    """构造允许财务任务运行且不触发真实来源的受控设置。"""
    return load_settings().model_copy(
        update={
            "financial_enabled": True,
            "financial_source_policy": "research-policy-pending",
            "financial_max_concurrency": 1,
            "financial_requests_per_minute": 1,
            "financial_request_timeout_seconds": 1,
        }
    )


def _install_sync_task_fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    """替换任务组合根资源，使成功和异常释放语义可在单元测试观察。"""
    FakeDatabaseClient.instances.clear()
    FakeObjectStorageClient.instances.clear()
    monkeypatch.setattr(financial_tasks, "DatabaseClient", FakeDatabaseClient)
    monkeypatch.setattr(financial_tasks, "ObjectStorageClient", FakeObjectStorageClient)
    monkeypatch.setattr(financial_tasks, "SqlAlchemyFinancialSyncRepository", FakeRepository)
    monkeypatch.setattr(financial_tasks, "S3RawPayloadStore", FakeRawStore)
    monkeypatch.setattr(financial_tasks, "FinancialSyncService", FakeFinancialSyncService)
