"""Celery worker 配置与入口的单元测试。"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock
from uuid import uuid4

import pytest
from celery import Task

from service_data_sync.application.ports.data_source import ProviderError, ProviderErrorCode
from service_data_sync.application.ports.market_data import StoredEquityInstrument
from service_data_sync.bootstrap.settings import load_settings
from service_data_sync.domain.equity import EquityBarPeriod, EquityIdentifier
from service_data_sync.entrypoints import worker
from service_data_sync.infrastructure.messaging import equity_market_tasks
from service_data_sync.infrastructure.messaging.celery_app import create_worker_app


def test_worker_app_registers_only_gated_sync_tasks(configured_environment: None) -> None:
    """保证 worker 只注册受审计同步任务，财务 probe 不会创建 beat 调度。"""
    app = create_worker_app(load_settings())
    custom_task_names = {
        task_name for task_name in app.tasks if task_name.startswith("service_data_sync.")
    }

    assert custom_task_names == {
        "service_data_sync.equity_market.dispatch",
        "service_data_sync.equity_market.sync_bar",
        "service_data_sync.equity_market.sync_reference",
        "service_data_sync.equity_lifecycle.probe",
        "service_data_sync.equity_lifecycle.replay_exchange",
        "service_data_sync.equity_lifecycle.sync_exchange",
        "service_data_sync.financial.derive_security",
        "service_data_sync.financial.probe",
        "service_data_sync.financial.sync_security",
        "service_data_sync.money_flow.probe",
        "service_data_sync.money_flow.sync_partition",
        "service_data_sync.sector_eod.dispatch_shadow",
        "service_data_sync.sector_eod.reap",
        "service_data_sync.sector_eod.run",
        "service_data_sync.sw_sector.probe",
        "service_data_sync.sw_sector.replay_snapshot",
        "service_data_sync.sw_sector.sync_current",
    }
    assert app.conf.task_ignore_result is True
    assert app.conf.result_backend is None
    assert app.conf.task_acks_late is True
    assert app.conf.task_reject_on_worker_lost is True
    assert app.conf.beat_schedule == {}
    bar_task = app.tasks["service_data_sync.equity_market.sync_bar"]
    reference_task = app.tasks["service_data_sync.equity_market.sync_reference"]
    assert bar_task.max_retries == 3
    assert reference_task.max_retries == 3
    assert bar_task.rate_limit == "30/m"
    assert reference_task.rate_limit == "30/m"
    assert bar_task.acks_late is True
    assert reference_task.acks_late is True


def test_worker_builds_independent_equity_market_schedules(
    configured_environment: None,
    monkeypatch,
) -> None:
    """开启个股调度后六类能力各自拥有 beat 条目，不存在日线聚合任务。"""
    monkeypatch.setenv("DATA_SYNC_EQUITY_MARKET_ENABLED", "true")
    monkeypatch.setenv("DATA_SYNC_EQUITY_SCHEDULER_ENABLED", "true")

    app = create_worker_app(load_settings())

    assert set(app.conf.beat_schedule) == {
        "equity-daily-bars",
        "equity-weekly-bars",
        "equity-monthly-bars",
        "equity-adjustment-factors",
        "equity-corporate-actions",
        "equity-company-profiles",
    }
    assert app.conf.beat_schedule["equity-weekly-bars"]["args"] == ("equity.bar.1w.raw",)
    assert all("aggregate" not in entry["task"] for entry in app.conf.beat_schedule.values())


def test_disabled_equity_market_tasks_fail_closed_without_dependencies(
    configured_environment: None,
) -> None:
    """关闭能力时分发器不建连接，单证券任务拒绝执行。"""
    app = create_worker_app(load_settings())

    assert app.tasks["service_data_sync.equity_market.dispatch"].run("equity.bar.1w.raw") == {
        "status": "disabled",
        "dispatched": 0,
    }
    with pytest.raises(RuntimeError, match="disabled"):
        app.tasks["service_data_sync.equity_market.sync_bar"].run(
            "SSE.600519",
            "1w",
            "2026-01-01",
            "2026-07-28",
        )
    with pytest.raises(RuntimeError, match="disabled"):
        app.tasks["service_data_sync.equity_market.sync_reference"].run(
            "SSE.600519",
            "equity.profile",
            None,
            None,
        )


def test_enabled_dispatch_sends_independent_bar_and_reference_messages(
    configured_environment: None,
    monkeypatch,
) -> None:
    """启用后分发器跳过 PENDING 身份，并为行情与概况发送不同任务。"""
    del configured_environment
    monkeypatch.setenv("DATA_SYNC_EQUITY_MARKET_ENABLED", "true")
    app = create_worker_app(load_settings())
    listed = StoredEquityInstrument(
        security_id=1,
        instrument_id=uuid4(),
        identifier=EquityIdentifier.parse("SSE.600519"),
        name="贵州茅台",
        listing_status="LISTED",
    )
    pending = StoredEquityInstrument(
        security_id=2,
        instrument_id=uuid4(),
        identifier=EquityIdentifier.parse("SZSE.000001"),
        name=None,
        listing_status="PENDING",
    )
    repository = SimpleNamespace(list_instruments=Mock(return_value=(listed, pending)))
    container = SimpleNamespace(database=object(), close=Mock())
    monkeypatch.setattr(
        equity_market_tasks,
        "build_container",
        Mock(return_value=container),
    )
    monkeypatch.setattr(
        equity_market_tasks,
        "SqlAlchemyEquityMarketDataRepository",
        Mock(return_value=repository),
    )
    send_task = Mock()
    monkeypatch.setattr(app, "send_task", send_task)
    dispatch = app.tasks["service_data_sync.equity_market.dispatch"]

    weekly = dispatch.run("equity.bar.1w.raw")
    profile = dispatch.run("equity.profile")

    assert weekly == {"status": "dispatched", "dispatched": 1}
    assert profile == {"status": "dispatched", "dispatched": 1}
    assert send_task.call_count == 2
    assert send_task.call_args_list[0].kwargs["queue"] == "equity-market"
    assert send_task.call_args_list[1].kwargs["queue"] == "equity-reference"
    assert container.close.call_count == 2
    with pytest.raises(ValueError, match="unsupported"):
        dispatch.run("equity.unknown")


def test_equity_task_windows_are_period_and_capability_specific() -> None:
    """滚动窗口按周期和参考数据语义独立计算。"""
    today = date(2026, 7, 28)

    assert equity_market_tasks._period_for_capability("equity.bar.1mo.raw") is (
        EquityBarPeriod.MONTH_1
    )
    assert equity_market_tasks._period_for_capability("equity.profile") is None
    assert equity_market_tasks._bar_window(
        EquityBarPeriod.DAY_1,
        today=today,
    ) == (date(2026, 7, 14), today)
    assert equity_market_tasks._bar_window(
        EquityBarPeriod.WEEK_1,
        today=today,
    ) == (date(2026, 4, 29), today)
    assert equity_market_tasks._reference_window(
        "equity.adjustment_factor",
        today=today,
    ) == (date(1990, 12, 19), today)
    assert equity_market_tasks._reference_window("equity.profile", today=today) == (None, None)
    assert equity_market_tasks._required_date("2026-07-28") == today
    with pytest.raises(ValueError, match="required"):
        equity_market_tasks._required_date(None)


def test_equity_tasks_retry_only_retryable_provider_failures(monkeypatch) -> None:
    """瞬时上游失败应全抖动重试，schema 失败必须立即终止。"""
    retry = Mock(side_effect=RuntimeError("scheduled retry"))
    task = cast(
        Task,
        SimpleNamespace(
            request=SimpleNamespace(retries=2),
            retry=retry,
        ),
    )
    retryable = ProviderError(
        ProviderErrorCode.UNAVAILABLE,
        "temporary disconnect",
        retryable=True,
    )
    schema_error = ProviderError(
        ProviderErrorCode.SCHEMA,
        "schema drift",
        retryable=False,
    )
    monkeypatch.setattr(equity_market_tasks.random, "randint", Mock(return_value=3))

    with pytest.raises(RuntimeError, match="scheduled retry"):
        equity_market_tasks._retry_provider_error(task, retryable)
    retry.assert_called_once_with(exc=retryable, countdown=3, max_retries=3)

    retry.reset_mock()
    with pytest.raises(ProviderError, match="schema drift"):
        equity_market_tasks._retry_provider_error(task, schema_error)
    retry.assert_not_called()


def test_worker_entrypoint_builds_a_gated_worker(
    configured_environment: None,
    monkeypatch,
) -> None:
    """在必需 worker 与日志级别参数后传递用户提供的 Celery 参数。"""
    calls: list[list[str]] = []

    class FakeWorkerApp:
        """记录入口调用参数的 worker 替身。"""

        def worker_main(self, arguments: list[str]) -> None:
            """捕获 worker 调用参数，供入口断言使用。"""
            calls.append(arguments)

    # 将基础设施初始化替换为空操作和 fixture worker，隔离参数组装逻辑。
    monkeypatch.setattr(worker, "configure_logging", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(worker, "create_worker_app", lambda _settings: FakeWorkerApp())

    assert worker.main(["--without-gossip"]) == 0
    assert calls == [["worker", "--loglevel=info", "--without-gossip"]]
