"""板块 EOD Celery 调度、开关与重试边界测试。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from types import SimpleNamespace
from uuid import uuid4

import pytest
from celery import Celery

from service_data_sync.application.ports.data_source import ProviderError, ProviderErrorCode
from service_data_sync.application.ports.sector_eod import QueuedSectorEodRun
from service_data_sync.bootstrap.settings import load_settings
from service_data_sync.infrastructure.messaging import sector_eod_tasks
from service_data_sync.infrastructure.messaging.celery_app import create_worker_app


@dataclass
class FakeCalendar:
    """返回固定开市状态并记录调度器请求，避免依赖尚未接入的权威日历。"""

    is_open_result: bool | None
    requested_date: date | None = None

    def is_open(self, *, trade_date: date) -> bool | None:
        """记录调度目标并返回测试预置的日历结论。"""
        self.requested_date = trade_date
        return self.is_open_result


class FakeContainer:
    """仅提供 dispatcher 读取日历所需依赖，并记录资源关闭。"""

    def __init__(self, calendar: FakeCalendar) -> None:
        """保存日历替身和关闭标记，不创建数据库、Redis 或对象存储客户端。"""
        self.trading_calendar = calendar
        self.database = object()
        self.source_registry: object = object()
        self.object_storage: object = object()
        self.closed = False

    def close(self) -> None:
        """记录 dispatcher 的 finally 释放行为。"""
        self.closed = True


class CapturedLogger:
    """收集结构化任务事件，断言日志字段不依赖真实日志后端。"""

    def __init__(self) -> None:
        """初始化按级别保存的空事件列表。"""
        self.events: list[dict[str, object]] = []

    def info(self, event: str, **fields: object) -> None:
        """记录一条信息级结构化事件。"""
        self.events.append({"level": "info", "event": event, **fields})

    def warning(self, event: str, **fields: object) -> None:
        """记录一条警告级结构化事件。"""
        self.events.append({"level": "warning", "event": event, **fields})

    def error(self, event: str, **fields: object) -> None:
        """记录一条错误级结构化事件。"""
        self.events.append({"level": "error", "event": event, **fields})


def test_dispatcher_skips_closed_or_unknown_calendar_without_enqueuing(
    configured_environment: None,
    monkeypatch,
) -> None:
    """休市或未知日历必须不投递 EOD，不能把周末或缺失日视为开市日。"""
    settings = load_settings().model_copy(
        update={"sector_eod_enabled": True, "sector_eod_scheduler_enabled": True}
    )
    app = create_worker_app(settings)
    calendar = FakeCalendar(is_open_result=None)
    container = FakeContainer(calendar)
    queued: list[tuple[str, tuple[object, ...]]] = []
    logger = CapturedLogger()

    def build(_settings: object) -> FakeContainer:
        """返回同一容器替身，避免调度测试连接真实基础设施。"""
        return container

    def send(task_name: str, args: tuple[object, ...]) -> None:
        """记录任务名和显式参数，验证 dispatcher 不推断或省略交易日。"""
        queued.append((task_name, args))

    monkeypatch.setattr(sector_eod_tasks, "build_container", build)
    monkeypatch.setattr(app, "send_task", send)
    monkeypatch.setattr(sector_eod_tasks, "_LOGGER", logger)

    result = app.tasks["service_data_sync.sector_eod.dispatch_shadow"].run()

    assert result == {"status": "calendar-unavailable", "queued": 0}
    assert queued == []
    assert logger.events[0]["event"] == "sector_eod.schedule_skipped"
    assert logger.events[0]["reason"] == "calendar-unavailable"
    assert calendar.requested_date is not None
    assert container.closed is True


def test_dispatcher_enqueues_two_schemes_for_one_explicit_open_date(
    configured_environment: None,
    monkeypatch,
) -> None:
    """开市日 dispatcher 必须给行业和概念分别投递相同明确日期，不能合并为隐式全市场任务。"""
    settings = load_settings().model_copy(
        update={"sector_eod_enabled": True, "sector_eod_scheduler_enabled": True}
    )
    app: Celery = create_worker_app(settings)
    calendar = FakeCalendar(is_open_result=True)
    container = FakeContainer(calendar)
    queued: list[tuple[str, tuple[object, ...]]] = []
    logger = CapturedLogger()

    def build(_settings: object) -> FakeContainer:
        """返回开市日容器替身，不建立真实基础设施连接。"""
        return container

    def send(task_name: str, args: tuple[object, ...]) -> None:
        """记录 dispatcher 发送的受控任务与显式 scheme/date 参数。"""
        queued.append((task_name, args))

    monkeypatch.setattr(sector_eod_tasks, "build_container", build)
    monkeypatch.setattr(app, "send_task", send)
    monkeypatch.setattr(sector_eod_tasks, "_LOGGER", logger)

    result = app.tasks["service_data_sync.sector_eod.dispatch_shadow"].run()

    assert result == {"status": "queued", "queued": 2}
    assert {task_name for task_name, _ in queued} == {"service_data_sync.sector_eod.run"}
    assert {args[0] for _, args in queued} == {"eastmoney.industry", "eastmoney.concept"}
    assert len({args[1] for _, args in queued}) == 1
    assert logger.events == [
        {
            "level": "info",
            "event": "sector_eod.schedule_dispatched",
            "trade_date": queued[0][1][1],
            "execution_mode": "shadow",
            "partition_count": 2,
        }
    ]
    assert container.closed is True


def test_run_task_returns_without_provider_when_source_policy_is_disabled(
    configured_environment: None,
) -> None:
    """即使消息被误投递，source policy 关闭时 run task 也不能接触 provider 或发布数据。"""
    app = create_worker_app(load_settings())

    result = app.tasks["service_data_sync.sector_eod.run"].run("eastmoney.industry", "2026-07-27")

    assert result == {"status": "source-policy-disabled", "replayed": False}


def test_scheduler_tasks_return_disabled_before_building_any_dependency(
    configured_environment: None,
) -> None:
    """未开启 scheduler 时，两个后台任务均不得构造容器或访问任何基础设施。"""
    app = create_worker_app(load_settings())

    dispatch_result = app.tasks["service_data_sync.sector_eod.dispatch_shadow"].run()
    reaper_result = app.tasks["service_data_sync.sector_eod.reap"].run()

    assert dispatch_result == {"status": "disabled", "queued": 0}
    assert reaper_result == {"status": "disabled", "requeued": 0}


def test_scheduler_tasks_do_not_enqueue_when_source_policy_is_disabled(
    configured_environment: None,
) -> None:
    """仅打开 scheduler 不能绕过来源准入，dispatcher 和 reaper 都应原地停止。"""
    settings = load_settings().model_copy(update={"sector_eod_scheduler_enabled": True})
    app = create_worker_app(settings)

    dispatch_result = app.tasks["service_data_sync.sector_eod.dispatch_shadow"].run()
    reaper_result = app.tasks["service_data_sync.sector_eod.reap"].run()

    assert dispatch_result == {"status": "source-policy-disabled", "queued": 0}
    assert reaper_result == {"status": "source-policy-disabled", "requeued": 0}


def test_run_task_executes_shadow_sync_and_closes_resources(
    configured_environment: None,
    monkeypatch,
) -> None:
    """来源准入开启后，任务应执行明确分区的 shadow 同步并在结束时释放组合根。"""
    settings = load_settings().model_copy(update={"sector_eod_enabled": True})
    app = create_worker_app(settings)
    container = FakeContainer(FakeCalendar(is_open_result=True))
    logger = CapturedLogger()

    def for_capability(_capability: str) -> tuple[object, ...]:
        """返回唯一测试来源，保持任务选择逻辑与生产一致。"""
        return (object(),)

    container.source_registry = SimpleNamespace(for_capability=for_capability)
    container.object_storage = object()
    sync_calls: list[dict[str, object]] = []

    class FakeRepository:
        """返回未归档分区，迫使任务进入首次同步路径。"""

        def __init__(self, _database: object) -> None:
            """接受生产仓储的数据库参数，避免建立真实连接。"""

        def has_archived_observation(self, **_kwargs: object) -> bool:
            """表明此分区没有可重放的原始观测。"""
            return False

    class FakeSyncService:
        """记录任务传入的执行模式和日期窗，并返回最小成功摘要。"""

        def __init__(self, **_kwargs: object) -> None:
            """接收任务构造的中立依赖。"""

        async def sync(self, **kwargs: object) -> SimpleNamespace:
            """保存首次同步参数，返回候选快照结果。"""
            sync_calls.append(kwargs)
            return SimpleNamespace(
                snapshot=SimpleNamespace(
                    snapshot_id=uuid4(), data_version=uuid4(), quality_status="passed"
                ),
                execution_mode=sector_eod_tasks.SectorEodExecutionMode.SHADOW,
                run_id=uuid4(),
            )

    def build(_settings: object) -> FakeContainer:
        """返回同一无网络组合根替身。"""
        return container

    def raw_payload_store(_storage: object) -> object:
        """返回无网络原始证据端口占位，隔离任务组合逻辑。"""
        return object()

    monkeypatch.setattr(sector_eod_tasks, "build_container", build)
    monkeypatch.setattr(sector_eod_tasks, "SqlAlchemySectorEodRepository", FakeRepository)
    monkeypatch.setattr(sector_eod_tasks, "S3RawPayloadStore", raw_payload_store)
    monkeypatch.setattr(sector_eod_tasks, "SectorEodSnapshotSyncService", FakeSyncService)
    monkeypatch.setattr(sector_eod_tasks, "_LOGGER", logger)

    result = app.tasks["service_data_sync.sector_eod.run"].run("eastmoney.industry", "2026-07-27")

    assert result["status"] == "completed"
    assert result["replayed"] is False
    assert result["executionMode"] == "shadow"
    assert sync_calls[0]["scheme"] is sector_eod_tasks.SectorScheme.EASTMONEY_INDUSTRY
    assert sync_calls[0]["trade_date"] == date(2026, 7, 27)
    assert [event["event"] for event in logger.events] == [
        "sector_eod.run_started",
        "sector_eod.run_completed",
    ]
    assert logger.events[1]["run_id"] is not None
    assert logger.events[1]["partition_key"] == "eastmoney.industry:2026-07-27"
    assert logger.events[1]["quality_status"] == "passed"
    assert container.closed is True


def test_run_task_logs_retryable_provider_failure_with_backoff(
    configured_environment: None,
    monkeypatch,
) -> None:
    """来源临时失败必须记录脱敏事件并将同一分区交给 Celery 的固定退避重试。"""
    settings = load_settings().model_copy(update={"sector_eod_enabled": True})
    app = create_worker_app(settings)
    task = app.tasks["service_data_sync.sector_eod.run"]
    container = FakeContainer(FakeCalendar(is_open_result=True))
    logger = CapturedLogger()
    retries: list[tuple[Exception, int]] = []

    class RetryRequested(Exception):
        """代替 Celery 的控制流异常，便于检查重试参数。"""

    class FakeRepository:
        """将分区标记为首次同步，避免测试读取 PostgreSQL。"""

        def __init__(self, _database: object) -> None:
            """接受生产仓储构造参数。"""

        def has_archived_observation(self, **_kwargs: object) -> bool:
            """表明无可重放 raw，任务应调用同步路径。"""
            return False

    class FakeSyncService:
        """模拟可重试的 provider 超时，不暴露供应商异常文本。"""

        def __init__(self, **_kwargs: object) -> None:
            """接受生产服务所需的中立依赖。"""

        async def sync(self, **_kwargs: object) -> SimpleNamespace:
            """抛出带稳定错误码的可重试来源错误。"""
            raise ProviderError(
                ProviderErrorCode.UNAVAILABLE, "provider secret endpoint", retryable=True
            )

    def for_capability(_capability: str) -> tuple[object, ...]:
        """返回唯一批准来源，使错误发生在同步调用阶段。"""
        return (object(),)

    def build(_settings: object) -> FakeContainer:
        """返回无网络容器替身。"""
        return container

    def raw_payload_store(_storage: object) -> object:
        """返回对象存储占位，避免构造真实 S3 客户端。"""
        return object()

    def retry(*, exc: Exception, countdown: int) -> None:
        """记录 Celery 重试参数并中断当前任务控制流。"""
        retries.append((exc, countdown))
        raise RetryRequested

    def fixed_jitter(_start: int, _end: int) -> int:
        """固定抖动，使重试倒计时断言保持确定性。"""
        return 0

    container.source_registry = SimpleNamespace(for_capability=for_capability)
    monkeypatch.setattr(sector_eod_tasks, "build_container", build)
    monkeypatch.setattr(sector_eod_tasks, "SqlAlchemySectorEodRepository", FakeRepository)
    monkeypatch.setattr(sector_eod_tasks, "S3RawPayloadStore", raw_payload_store)
    monkeypatch.setattr(sector_eod_tasks, "SectorEodSnapshotSyncService", FakeSyncService)
    monkeypatch.setattr(sector_eod_tasks, "_LOGGER", logger)
    monkeypatch.setattr(task, "retry", retry)
    monkeypatch.setattr(sector_eod_tasks.random, "randint", fixed_jitter)

    with pytest.raises(RetryRequested):
        task.run("eastmoney.industry", "2026-07-27")

    assert isinstance(retries[0][0], ProviderError)
    assert retries[0][0].code is ProviderErrorCode.UNAVAILABLE
    assert retries[0][1] == 300
    assert logger.events[-1]["event"] == "sector_eod.run_retry_scheduled"
    assert logger.events[-1]["error_code"] == "unavailable"
    assert container.closed is True


def test_run_task_logs_terminal_provider_configuration_error(
    configured_environment: None,
    monkeypatch,
) -> None:
    """不允许的多来源配置应记录终态错误，不排队重试也不泄露内部异常文本。"""
    settings = load_settings().model_copy(update={"sector_eod_enabled": True})
    app = create_worker_app(settings)
    container = FakeContainer(FakeCalendar(is_open_result=True))
    logger = CapturedLogger()

    def for_capability(_capability: str) -> tuple[object, ...]:
        """返回零来源，触发部署配置错误而非 provider 调用。"""
        return ()

    def build(_settings: object) -> FakeContainer:
        """返回无网络容器替身。"""
        return container

    container.source_registry = SimpleNamespace(for_capability=for_capability)
    monkeypatch.setattr(sector_eod_tasks, "build_container", build)
    monkeypatch.setattr(sector_eod_tasks, "_LOGGER", logger)

    with pytest.raises(RuntimeError, match="exactly one approved"):
        app.tasks["service_data_sync.sector_eod.run"].run("eastmoney.industry", "2026-07-27")

    assert logger.events == [
        {
            "level": "error",
            "event": "sector_eod.run_failed",
            "error_code": "unexpected",
            "partition_key": "eastmoney.industry:2026-07-27",
            "scheme": "eastmoney.industry",
            "trade_date": "2026-07-27",
            "attempt": 1,
            "duration_ms": logger.events[0]["duration_ms"],
        }
    ]
    assert container.closed is True


def test_reaper_requeues_queued_partitions_without_calling_provider(
    configured_environment: None,
    monkeypatch,
) -> None:
    """自动 reaper 只释放 lease 并投递固定分区任务，不读取或调用任何数据源。"""
    settings = load_settings().model_copy(
        update={"sector_eod_enabled": True, "sector_eod_scheduler_enabled": True}
    )
    app = create_worker_app(settings)
    container = FakeContainer(FakeCalendar(is_open_result=True))
    queued: list[tuple[str, tuple[object, ...]]] = []
    logger = CapturedLogger()

    class FakeRepository:
        """提供确定性回收结果和 queued 分区，不连接 PostgreSQL。"""

        def __init__(self, _database: object) -> None:
            """接受生产仓储构造参数，保持 task 注入路径不变。"""

        def requeue_expired_leases(self, *, now: object) -> int:
            """断言 task 传入当前时刻，模拟回收一个过期 owner。"""
            assert now is not None
            return 1

        def list_queued_runs(self) -> tuple[QueuedSectorEodRun, ...]:
            """返回一个明确待恢复分区，验证 worker 接收稳定身份而非 raw。"""
            return (
                QueuedSectorEodRun(
                    scheme=sector_eod_tasks.SectorScheme.EASTMONEY_INDUSTRY,
                    trade_date=date(2026, 7, 27),
                ),
            )

    def build(_settings: object) -> FakeContainer:
        """返回不含来源注册表的容器，证明 reaper 不访问 provider。"""
        return container

    def send(task_name: str, args: tuple[object, ...]) -> None:
        """记录恢复任务的受控名称与显式分区参数。"""
        queued.append((task_name, args))

    monkeypatch.setattr(sector_eod_tasks, "build_container", build)
    monkeypatch.setattr(sector_eod_tasks, "SqlAlchemySectorEodRepository", FakeRepository)
    monkeypatch.setattr(app, "send_task", send)
    monkeypatch.setattr(sector_eod_tasks, "_LOGGER", logger)

    result = app.tasks["service_data_sync.sector_eod.reap"].run()

    assert result == {"status": "requeued", "requeued": 1}
    assert queued == [("service_data_sync.sector_eod.run", ("eastmoney.industry", "2026-07-27"))]
    assert logger.events == [
        {
            "level": "info",
            "event": "sector_eod.reaper_completed",
            "requeued_count": 1,
            "queued_partition_count": 1,
        }
    ]
    assert container.closed is True


def test_registering_sector_eod_tasks_twice_is_idempotent(
    configured_environment: None,
) -> None:
    """重复初始化 worker 时不得覆盖既有任务对象或重复注册同名任务。"""
    settings = load_settings()
    app = Celery("sector-eod-idempotency")
    sector_eod_tasks.register_sector_eod_tasks(app, settings=settings)
    original_task = app.tasks["service_data_sync.sector_eod.run"]

    sector_eod_tasks.register_sector_eod_tasks(app, settings=settings)

    assert app.tasks["service_data_sync.sector_eod.run"] is original_task


def test_retry_countdown_uses_frozen_backoff_with_small_jitter(monkeypatch) -> None:
    """临时失败的重试必须使用 5、15、30 分钟档位，jitter 只用于削峰而不改变策略级别。"""

    def fixed_jitter(_start: int, _end: int) -> int:
        """固定随机抖动，使退避策略断言保持确定性。"""
        return 7

    task = SimpleNamespace(request=SimpleNamespace(retries=0))
    monkeypatch.setattr(sector_eod_tasks.random, "randint", fixed_jitter)

    first = sector_eod_tasks._retry_countdown(task)
    task.request.retries = 1
    second = sector_eod_tasks._retry_countdown(task)
    task.request.retries = 2
    third = sector_eod_tasks._retry_countdown(task)

    assert (first, second, third) == (307, 907, 1807)


def test_task_error_logs_keep_a_stable_non_sensitive_error_code(monkeypatch) -> None:
    """失败日志只输出稳定错误码、分区和耗时，避免供应商异常文本进入结构化索引。"""
    logger = CapturedLogger()
    monkeypatch.setattr(sector_eod_tasks, "_LOGGER", logger)
    provider_error = ProviderError(
        ProviderErrorCode.SCHEMA, "supplier response includes secret", retryable=False
    )

    sector_eod_tasks._log_retry(
        error=provider_error,
        scheme=sector_eod_tasks.SectorScheme.EASTMONEY_INDUSTRY,
        trade_date=date(2026, 7, 27),
        attempt=2,
        countdown=300,
        started_at=0.0,
    )
    sector_eod_tasks._log_failure(
        error=OSError("private endpoint failed"),
        scheme=sector_eod_tasks.SectorScheme.EASTMONEY_INDUSTRY,
        trade_date=date(2026, 7, 27),
        attempt=2,
        started_at=0.0,
    )

    assert logger.events[0]["error_code"] == "schema"
    assert logger.events[0]["retry_in_seconds"] == 300
    assert logger.events[1]["error_code"] == "network"
    assert "supplier response" not in str(logger.events)
