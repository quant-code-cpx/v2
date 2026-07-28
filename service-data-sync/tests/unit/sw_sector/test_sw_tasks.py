"""申万 Celery 任务注册、探针与发布 cadence 的单元测试。"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest
from celery import Celery

from service_data_sync.application.ports.data_source import (
    ProviderError,
    ProviderErrorCode,
)
from service_data_sync.bootstrap.settings import Settings
from service_data_sync.infrastructure.messaging import sw_sector_tasks
from service_data_sync.infrastructure.messaging.sw_sector_tasks import (
    register_sw_sector_tasks,
    sw_sector_beat_schedule,
)

_DATE = date(2026, 7, 28)


class ReadySource:
    """提供无网络副作用的申万能力声明。"""

    provider_id = "ready-sw"

    def capabilities(self) -> frozenset[str]:
        """声明一个完整快照能力。"""
        return frozenset({"sector.sw.snapshot.raw"})


class FakeResource:
    """记录短生命周期基础设施是否被释放。"""

    def __init__(self) -> None:
        """初始化未关闭状态。"""
        self.closed = False

    def close(self) -> None:
        """记录资源释放。"""
        self.closed = True


class FakeSyncService:
    """返回确定性双发布版本并记录同步或 replay 日期。"""

    def __init__(self) -> None:
        """初始化调用记录。"""
        self.synced_date: date | None = None
        self.replayed_date: date | None = None

    async def sync(self, *, snapshot_date: date) -> SimpleNamespace:
        """记录同步日期并返回任务所需发布形状。"""
        self.synced_date = snapshot_date
        return _sync_result()

    def replay(self, *, snapshot_date: date) -> SimpleNamespace:
        """记录 replay 日期并返回任务所需发布形状。"""
        self.replayed_date = snapshot_date
        return _sync_result()


def test_sw_tasks_register_idempotently_and_disabled_probe_stays_offline() -> None:
    """重复装配不得覆盖任务，关闭来源策略的探针也不得访问 AKShare。"""
    app = Celery("sw-sector-task-test")
    settings = _settings(enabled=False)

    register_sw_sector_tasks(app, settings=settings)
    probe = app.tasks["service_data_sync.sw_sector.probe"]
    first_task = app.tasks["service_data_sync.sw_sector.sync_current"]
    register_sw_sector_tasks(app, settings=settings)

    assert probe.run() == {"status": "disabled", "capabilityCount": 0}
    assert app.tasks["service_data_sync.sw_sector.sync_current"] is first_task
    assert "service_data_sync.sw_sector.replay_snapshot" in app.tasks


def test_sw_probe_reports_enabled_provider_without_fetching(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """启用探针只读取 adapter 能力与 provider 身份，不运行同步。"""
    app = Celery("sw-sector-ready-probe-test")

    def build_source(_settings: Settings) -> ReadySource:
        """返回无网络的就绪来源。"""
        return ReadySource()

    monkeypatch.setattr(sw_sector_tasks, "build_sw_source", build_source)
    register_sw_sector_tasks(app, settings=_settings(enabled=True))

    assert app.tasks["service_data_sync.sw_sector.probe"].run() == {
        "status": "sync-ready",
        "capabilityCount": 1,
        "providerId": "ready-sw",
    }


def test_sw_sync_task_retries_only_retryable_provider_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同步任务应透传永久错误，并对瞬时错误按重试次数指数退避。"""
    app = Celery("sw-sector-retry-test")
    register_sw_sector_tasks(app, settings=_settings(enabled=True))
    task = app.tasks["service_data_sync.sw_sector.sync_current"]

    def fail_permanently(_settings: Settings, *, snapshot_date: date) -> dict[str, object]:
        """模拟不可重试 schema 错误。"""
        del snapshot_date
        raise ProviderError(ProviderErrorCode.SCHEMA, "schema", retryable=False)

    monkeypatch.setattr(sw_sector_tasks, "_run_sync", fail_permanently)
    with pytest.raises(ProviderError) as permanent:
        task.run()
    assert permanent.value.retryable is False

    def fail_temporarily(_settings: Settings, *, snapshot_date: date) -> dict[str, object]:
        """模拟可重试网络错误。"""
        del snapshot_date
        raise ProviderError(ProviderErrorCode.UNAVAILABLE, "network", retryable=True)

    captured: dict[str, object] = {}

    def retry(*, exc: BaseException, countdown: int) -> RuntimeError:
        """捕获退避参数并返回 Celery 将抛出的重试异常替身。"""
        captured.update({"error": exc, "countdown": countdown})
        return RuntimeError("retry")

    monkeypatch.setattr(sw_sector_tasks, "_run_sync", fail_temporarily)
    monkeypatch.setattr(task, "retry", retry)
    with pytest.raises(RuntimeError, match="retry"):
        task.run()

    assert isinstance(captured["error"], ProviderError)
    assert captured["countdown"] == 2


def test_sw_run_and_replay_close_short_lived_infrastructure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同步和 replay 无论经何入口都应投影双版本并释放数据库与对象存储。"""
    settings = _settings(enabled=True)
    resources: list[FakeResource] = []
    services: list[FakeSyncService] = []

    def database_from_settings(_settings: Settings) -> FakeResource:
        """创建并记录数据库替身。"""
        resource = FakeResource()
        resources.append(resource)
        return resource

    def object_storage_from_settings(_settings: Settings) -> FakeResource:
        """创建并记录对象存储替身。"""
        resource = FakeResource()
        resources.append(resource)
        return resource

    def build_service(
        _settings: Settings,
        *,
        database: object,
        object_storage: object,
        replay_only: bool = False,
    ) -> FakeSyncService:
        """断言资源已注入并返回可同步或 replay 的服务。"""
        assert database in resources and object_storage in resources
        assert replay_only in {True, False}
        service = FakeSyncService()
        services.append(service)
        return service

    monkeypatch.setattr(
        sw_sector_tasks.DatabaseClient,
        "from_settings",
        staticmethod(database_from_settings),
    )
    monkeypatch.setattr(
        sw_sector_tasks.ObjectStorageClient,
        "from_settings",
        staticmethod(object_storage_from_settings),
    )
    monkeypatch.setattr(sw_sector_tasks, "build_sw_sync_service", build_service)

    live = sw_sector_tasks._run_sync(settings, snapshot_date=_DATE)
    app = Celery("sw-sector-replay-test")
    register_sw_sector_tasks(app, settings=settings)
    replayed = app.tasks["service_data_sync.sw_sector.replay_snapshot"].run(_DATE.isoformat())

    assert live == {
        "taxonomyDataVersion": "taxonomy-version",
        "valuationDataVersion": "valuation-version",
    }
    assert replayed == live
    assert services[0].synced_date == _DATE
    assert services[1].replayed_date == _DATE
    assert len(resources) == 4
    assert all(resource.closed for resource in resources)


def test_sw_schedule_requires_flag_and_uses_fixed_shanghai_cadence() -> None:
    """发布调度仅在专属开关开启时出现，并固定到每日 18:30。"""
    assert sw_sector_beat_schedule(settings=_settings(enabled=False)) == {}

    schedule = sw_sector_beat_schedule(settings=_settings(enabled=True))

    assert schedule["sw-sector-daily-sync"]["task"] == ("service_data_sync.sw_sector.sync_current")
    assert str(schedule["sw-sector-daily-sync"]["schedule"]) == (
        "<crontab: 30 18 * * * (m/h/dM/MY/d)>"
    )


def _settings(*, enabled: bool) -> Settings:
    """构造仅包含本任务模块读取字段的受控设置对象。"""
    return Settings.model_construct(
        akshare_enabled=enabled,
        sector_enabled=enabled,
        sw_sector_enabled=enabled,
        akshare_request_timeout_seconds=30,
    )


def _sync_result() -> SimpleNamespace:
    """构造仅含 Celery 投影所需字段的同步结果。"""
    return SimpleNamespace(
        publications=SimpleNamespace(
            taxonomy=SimpleNamespace(data_version="taxonomy-version"),
            valuation=SimpleNamespace(data_version="valuation-version"),
        )
    )
