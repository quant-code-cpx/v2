"""财务探针与 command 化旧 `Celery` 入口测试。"""

from __future__ import annotations

from dataclasses import dataclass

from celery import Celery

from service_data_sync.bootstrap.settings import Settings, load_settings
from service_data_sync.infrastructure.messaging import financial_tasks


@dataclass(frozen=True)
class FakeProvider:
    """提供不含网络能力的最小 adapter 身份。"""

    provider_id: str


class FakeRegistry:
    """按 capability 返回固定 adapter 集合，并记录探针查询。"""

    def __init__(self, providers: dict[str, tuple[FakeProvider, ...]]) -> None:
        """保存能力映射，测试中绝不提供 `fetch`。"""
        self._providers = providers
        self.requested: set[str] = set()

    def for_capability(self, capability: str) -> tuple[FakeProvider, ...]:
        """记录只读能力查询并返回预置 adapter。"""
        self.requested.add(capability)
        return self._providers.get(capability, ())


class FakeCommandContainer:
    """提供 command 提交所需最小组合根，并记录关闭动作。"""

    def __init__(self) -> None:
        """初始化不连接数据库或对象存储的替身依赖。"""
        self.database = object()
        self.source_registry = object()
        self.trading_calendar = None
        self.closed = False

    def close(self) -> None:
        """记录任务提交结束后释放组合根。"""
        self.closed = True


class FakeControlPlane:
    """记录任务仅构造控制面，不执行同步或发布。"""

    def __init__(self, **dependencies: object) -> None:
        """保存构造依赖，供断言 command 边界使用。"""
        self.dependencies = dependencies


def test_probe_returns_disabled_without_building_source_registry(
    configured_environment: None, monkeypatch
) -> None:
    """关闭财务能力时 probe 不得组合来源，也不能触发任何 egress。"""
    del configured_environment
    app = Celery("financial-probe-disabled")
    financial_tasks.register_financial_tasks(app, settings=load_settings())

    def unexpected_registry(_settings: object) -> object:
        """若停用 probe 仍读取 registry，立即暴露边界回归。"""
        raise AssertionError("disabled probe must not build source registry")

    monkeypatch.setattr(financial_tasks, "build_source_registry", unexpected_registry)
    assert app.tasks[financial_tasks._PROBE_TASK].run() == {
        "status": "disabled",
        "capabilityCount": 0,
        "providerCount": 0,
    }


def test_probe_counts_declared_capabilities_without_fetching(
    configured_environment: None, monkeypatch
) -> None:
    """启用 probe 只统计 adapter 声明能力，不选择来源、不发起同步。"""
    del configured_environment
    settings = _enabled_settings()
    app = Celery("financial-probe-enabled")
    financial_tasks.register_financial_tasks(app, settings=settings)
    provider = FakeProvider(provider_id="approved-financial")
    registry = FakeRegistry(
        {capability: (provider,) for capability in financial_tasks._REQUIRED_CAPABILITIES}
    )
    monkeypatch.setattr(financial_tasks, "build_source_registry", lambda _settings: registry)

    assert app.tasks[financial_tasks._PROBE_TASK].run() == {
        "status": "sync-ready",
        "capabilityCount": 3,
        "providerCount": 1,
    }
    assert registry.requested == financial_tasks._REQUIRED_CAPABILITIES


def test_sync_task_submits_one_financial_command_and_closes_container(
    configured_environment: None, monkeypatch
) -> None:
    """单证券旧任务只能转换为稳定 command，绝不在 `Celery` 进程同步或发布。"""
    del configured_environment
    settings = _enabled_settings()
    app = Celery("financial-command-submission")
    container = FakeCommandContainer()
    received: dict[str, object] = {}
    financial_tasks.register_financial_tasks(app, settings=settings)
    monkeypatch.setattr(financial_tasks, "build_container", lambda _settings: container)
    monkeypatch.setattr(financial_tasks, "build_catalog", lambda _settings, _registry: {})
    monkeypatch.setattr(financial_tasks, "DataOperationsControlPlane", FakeControlPlane)

    def submit(control_plane: FakeControlPlane, **kwargs: object) -> dict[str, str]:
        """记录 command 内容并返回固定受理收据。"""
        received["controlPlane"] = control_plane
        received.update(kwargs)
        return {"commandId": "command-1"}

    monkeypatch.setattr(financial_tasks, "submit_system_command", submit)
    assert app.tasks[financial_tasks._SYNC_TASK].run("SSE", "600519") == {"commandId": "command-1"}
    assert received["target"] == {
        "datasetCode": "financial.report",
        "mode": "INCREMENTAL",
        "selector": {"kind": "INSTRUMENT", "exchange": "SSE", "symbol": "600519"},
        "dateFrom": None,
        "dateTo": None,
        "observationDate": None,
    }
    assert received["reason"] == "兼容财务 Celery 提交"
    assert received["request_prefix"] == "legacy-financial-task"
    assert container.closed is True


def _enabled_settings() -> Settings:
    """构造允许财务 command 提交但不接触真实来源的设置。"""
    return load_settings().model_copy(
        update={
            "financial_enabled": True,
            "financial_source_policy": "research-policy-pending",
            "financial_max_concurrency": 1,
            "financial_requests_per_minute": 1,
            "financial_request_timeout_seconds": 1,
        }
    )
