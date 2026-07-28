"""资金流 Celery 探针、显式分区同步和失败 checkpoint 测试。"""

from __future__ import annotations

from uuid import UUID

import pytest
from celery import Celery

from service_data_sync.application.money_flow.sync import MoneyFlowSyncResult
from service_data_sync.application.ports.data_source import (
    ProviderError,
    ProviderErrorCode,
)
from service_data_sync.application.ports.money_flow import PublishedMoneyFlow
from service_data_sync.bootstrap.settings import Settings
from service_data_sync.infrastructure.messaging import money_flow_tasks
from service_data_sync.infrastructure.persistence.money_flow_run_ledger import (
    MoneyFlowRun,
)


class FakeProvider:
    """只暴露探针去重所需的 provider 身份。"""

    provider_id = "fixture-money-flow"


class FakeRegistry:
    """为全部能力返回同一个唯一 provider。"""

    def __init__(self, providers: tuple[FakeProvider, ...] = (FakeProvider(),)) -> None:
        """保存每种 capability 的受控 provider 集。"""
        self.providers = providers

    def for_capability(self, _: str) -> tuple[FakeProvider, ...]:
        """返回固定 provider 集。"""
        return self.providers


class FakeClient:
    """记录数据库或对象存储客户端是否关闭。"""

    instances: list[FakeClient] = []

    def __init__(self) -> None:
        """注册新客户端实例。"""
        self.closed = False
        self.instances.append(self)

    def close(self) -> None:
        """记录资源释放。"""
        self.closed = True


class FakeLedger:
    """记录任务写入的开始、完成和失败 checkpoint。"""

    instances: list[FakeLedger] = []

    def __init__(self, _: object) -> None:
        """注册账本并初始化调用记录。"""
        self.started: list[dict[str, object]] = []
        self.finished: list[MoneyFlowSyncResult] = []
        self.failed: list[tuple[str, bool]] = []
        self.instances.append(self)

    def start(self, **kwargs: object) -> MoneyFlowRun:
        """返回固定 fencing run。"""
        self.started.append(kwargs)
        return MoneyFlowRun(
            run_id=UUID("00000000-0000-4000-8000-000000000110"),
            partition_key="request:fixture",
            lease_owner="money-flow:fixture",
            attempt=1,
        )

    def finish(self, *, run: MoneyFlowRun, result: MoneyFlowSyncResult) -> None:
        """记录成功或部分完成结果。"""
        assert run.partition_key == "request:fixture"
        self.finished.append(result)

    def fail(
        self,
        *,
        run: MoneyFlowRun,
        error_code: str,
        retryable: bool,
    ) -> None:
        """记录稳定错误码与重试策略。"""
        assert run.partition_key == "request:fixture"
        self.failed.append((error_code, retryable))


class FakeSyncService:
    """返回固定同步结果，或按测试注入异常。"""

    failure: Exception | None = None

    def __init__(self, **_: object) -> None:
        """接受生产装配参数但不访问外部系统。"""

    async def sync(self, **_: object) -> MoneyFlowSyncResult:
        """执行受控成功或失败路径。"""
        if self.failure is not None:
            raise self.failure
        return MoneyFlowSyncResult(
            capability="money_flow.order_size.daily.market.raw",
            source_payload_sha256="a" * 64,
            raw_uri="s3://private/raw/evidence.json",
            publication=PublishedMoneyFlow(
                data_version=UUID("00000000-0000-4000-8000-000000000111"),
                inserted_count=1,
                revised_count=0,
                unchanged_count=0,
                published=True,
                quality_status="passed",
            ),
        )


def _settings(*, enabled: bool) -> Settings:
    """构造任务模块实际读取字段的受控配置。"""
    return Settings.model_construct(
        money_flow_enabled=enabled,
    )


def _patch_runtime(
    monkeypatch: pytest.MonkeyPatch,
    *,
    registry: FakeRegistry,
) -> None:
    """替换任务外围依赖，同时保留真实注册和错误分支。"""

    def build_registry(_: Settings) -> FakeRegistry:
        """返回受控来源注册表。"""
        return registry

    def build_client(_: Settings) -> FakeClient:
        """创建可观察关闭状态的客户端。"""
        return FakeClient()

    def build_repository(_: object) -> object:
        """返回无需数据库的仓储占位。"""
        return object()

    def build_raw_store(_: object) -> object:
        """返回无需对象存储的 raw 仓储占位。"""
        return object()

    monkeypatch.setattr(money_flow_tasks, "build_source_registry", build_registry)
    monkeypatch.setattr(
        money_flow_tasks.DatabaseClient,
        "from_settings",
        staticmethod(build_client),
    )
    monkeypatch.setattr(
        money_flow_tasks.ObjectStorageClient,
        "from_settings",
        staticmethod(build_client),
    )
    monkeypatch.setattr(money_flow_tasks, "SqlAlchemyMoneyFlowRunLedger", FakeLedger)
    monkeypatch.setattr(money_flow_tasks, "MoneyFlowSyncService", FakeSyncService)
    monkeypatch.setattr(
        money_flow_tasks,
        "SqlAlchemyMoneyFlowRepository",
        build_repository,
    )
    monkeypatch.setattr(money_flow_tasks, "S3RawPayloadStore", build_raw_store)


def test_tasks_register_idempotently_and_disabled_probe_stays_offline() -> None:
    """关闭资金流时探针不装配 provider，同步任务稳定拒绝。"""
    app = Celery("money-flow-disabled")
    settings = _settings(enabled=False)
    money_flow_tasks.register_money_flow_tasks(app, settings=settings)
    first_sync = app.tasks["service_data_sync.money_flow.sync_partition"]
    money_flow_tasks.register_money_flow_tasks(app, settings=settings)

    assert app.tasks["service_data_sync.money_flow.probe"].run() == {
        "status": "disabled",
        "capabilityCount": 0,
        "providerCount": 0,
    }
    assert app.tasks["service_data_sync.money_flow.sync_partition"] is first_sync
    with pytest.raises(RuntimeError, match="disabled"):
        first_sync.run("money_flow.order_size.daily.market.raw", {"marketCode": "cn-a"})


def test_probe_reports_all_capabilities_and_single_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """探针仅检查本地 capability 声明，并按 provider_id 去重。"""
    _patch_runtime(monkeypatch, registry=FakeRegistry())
    app = Celery("money-flow-probe")
    money_flow_tasks.register_money_flow_tasks(app, settings=_settings(enabled=True))

    assert app.tasks["service_data_sync.money_flow.probe"].run() == {
        "status": "sync-ready",
        "capabilityCount": 8,
        "providerCount": 1,
    }


def test_sync_task_finishes_checkpoint_and_closes_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """成功同步写入 publication checkpoint，并在返回前关闭两类客户端。"""
    FakeLedger.instances.clear()
    FakeClient.instances.clear()
    FakeSyncService.failure = None
    _patch_runtime(monkeypatch, registry=FakeRegistry())
    app = Celery("money-flow-success")
    money_flow_tasks.register_money_flow_tasks(app, settings=_settings(enabled=True))

    result = app.tasks["service_data_sync.money_flow.sync_partition"].run(
        "money_flow.order_size.daily.market.raw",
        {"marketCode": "cn-a"},
        "backfill",
    )

    assert result == {
        "dataVersion": "00000000-0000-4000-8000-000000000111",
        "published": True,
        "qualityStatus": "passed",
        "rawUri": "s3://private/raw/evidence.json",
    }
    assert FakeLedger.instances[-1].started[0]["parameters"] == (("marketCode", "cn-a"),)
    assert len(FakeLedger.instances[-1].finished) == 1
    assert all(client.closed for client in FakeClient.instances)


def test_sync_task_maps_provider_and_unexpected_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ProviderError 保留可重试性，未知异常写入终止错误码。"""
    _patch_runtime(monkeypatch, registry=FakeRegistry())
    app = Celery("money-flow-failures")
    money_flow_tasks.register_money_flow_tasks(app, settings=_settings(enabled=True))
    task = app.tasks["service_data_sync.money_flow.sync_partition"]

    FakeLedger.instances.clear()
    FakeSyncService.failure = ProviderError(
        ProviderErrorCode.UNAVAILABLE,
        "offline",
        retryable=True,
    )
    with pytest.raises(ProviderError):
        task.run(
            "money_flow.order_size.daily.market.raw",
            {"marketCode": "cn-a"},
        )
    assert FakeLedger.instances[-1].failed == [("provider-unavailable", True)]

    FakeLedger.instances.clear()
    FakeSyncService.failure = RuntimeError("broken")
    with pytest.raises(RuntimeError, match="broken"):
        task.run(
            "money_flow.order_size.daily.market.raw",
            {"marketCode": "cn-a"},
        )
    assert FakeLedger.instances[-1].failed == [("money-flow-sync-failed", False)]
    FakeSyncService.failure = None


def test_sync_task_rejects_unknown_capability_or_ambiguous_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """任务只接受八种受控能力，且每种能力必须唯一选择 provider。"""
    _patch_runtime(monkeypatch, registry=FakeRegistry(providers=()))
    app = Celery("money-flow-invalid")
    money_flow_tasks.register_money_flow_tasks(app, settings=_settings(enabled=True))
    task = app.tasks["service_data_sync.money_flow.sync_partition"]

    with pytest.raises(ValueError, match="unsupported"):
        task.run("unknown", {})
    with pytest.raises(RuntimeError, match="exactly one"):
        task.run(
            "money_flow.order_size.daily.market.raw",
            {"marketCode": "cn-a"},
        )
