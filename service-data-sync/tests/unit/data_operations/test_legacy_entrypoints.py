"""历史同步入口在未接入 fenced executor 时的安全停用测试。"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from types import ModuleType
from typing import cast

import pytest
from celery import Celery

from service_data_sync.application.legacy_entrypoints import (
    LEGACY_ENTRYPOINT_UNAVAILABLE,
    LegacyEntryPointUnavailable,
)

_CLI_ENTRYPOINTS = (
    ("service_data_sync.entrypoints.sector_catalog", "data-sync-sector-catalog"),
    ("service_data_sync.entrypoints.sector_bars", "data-sync-sector-bars"),
    ("service_data_sync.entrypoints.sector_membership", "data-sync-sector-membership"),
    ("service_data_sync.entrypoints.sector_eod", "data-sync-sector-eod"),
    ("service_data_sync.entrypoints.sector_eod_reaper", "data-sync-sector-eod-reaper"),
    ("service_data_sync.entrypoints.sector_eod_rollback", "data-sync-sector-eod-rollback"),
    ("service_data_sync.entrypoints.equity_catalog", "data-sync-equity-catalog"),
    ("service_data_sync.entrypoints.equity_lifecycle", "data-sync-equity-lifecycle"),
    ("service_data_sync.entrypoints.financial_derived", "data-sync-financial-derived"),
    ("service_data_sync.entrypoints.money_flow", "data-sync-money-flow"),
    ("service_data_sync.entrypoints.index_shadow", "data-sync-index-shadow"),
    ("service_data_sync.entrypoints.derivative_daily_bars", "data-sync-derivative-bars"),
    ("service_data_sync.entrypoints.etf", "data-sync-etf"),
    ("service_data_sync.entrypoints.margin", "data-sync-margin"),
    ("service_data_sync.entrypoints.stock_connect", "data-sync-stock-connect"),
    ("service_data_sync.entrypoints.corporate_events", "data-sync-corporate-events"),
    ("service_data_sync.entrypoints.trading_events", "data-sync-trading-events"),
    ("service_data_sync.entrypoints.sw_sector", "data-sync-sw-sector"),
)


@pytest.mark.parametrize(("module_name", "entrypoint"), _CLI_ENTRYPOINTS)
def test_legacy_cli_keeps_help_and_rejects_execution(module_name: str, entrypoint: str) -> None:
    """旧 CLI 可展示帮助，但任何执行参数都会在建立依赖前以稳定错误码退出。"""
    module = importlib.import_module(module_name)
    main = _main(module)

    with pytest.raises(SystemExit) as help_exit:
        main(["--help"])
    assert help_exit.value.code == 0

    with pytest.raises(SystemExit) as execution_exit:
        main(["--legacy-argument", "value"])
    assert str(execution_exit.value) == f"{LEGACY_ENTRYPOINT_UNAVAILABLE}: {entrypoint}"


def test_legacy_celery_tasks_reject_sync_replay_recovery_and_keep_probe_safe() -> None:
    """旧 Celery 同步、重放和恢复消息必须失败，探针只返回无敏感信息的停用状态。"""
    task_cases = (
        (
            "service_data_sync.infrastructure.messaging.sw_sector_tasks",
            "register_sw_sector_tasks",
            (
                ("service_data_sync.sw_sector.probe", (), False),
                ("service_data_sync.sw_sector.sync_current", (), True),
                ("service_data_sync.sw_sector.replay_snapshot", ("2026-07-29",), True),
            ),
        ),
        (
            "service_data_sync.infrastructure.messaging.money_flow_tasks",
            "register_money_flow_tasks",
            (
                ("service_data_sync.money_flow.probe", (), False),
                (
                    "service_data_sync.money_flow.sync_partition",
                    ("money_flow.daily", {"date": "2026-07-29"}),
                    True,
                ),
            ),
        ),
        (
            "service_data_sync.infrastructure.messaging.sector_eod_tasks",
            "register_sector_eod_tasks",
            (
                ("service_data_sync.sector_eod.dispatch_shadow", (), True),
                ("service_data_sync.sector_eod.reap", (), True),
                ("service_data_sync.sector_eod.run", ("eastmoney.industry", "2026-07-29"), True),
            ),
        ),
        (
            "service_data_sync.infrastructure.messaging.equity_lifecycle_tasks",
            "register_equity_lifecycle_tasks",
            (
                ("service_data_sync.equity_lifecycle.probe", (), False),
                ("service_data_sync.equity_lifecycle.sync_exchange", ("SSE", "2026-07-29"), True),
                ("service_data_sync.equity_lifecycle.replay_exchange", ("SSE",), True),
            ),
        ),
        (
            "service_data_sync.infrastructure.messaging.financial_derived_tasks",
            "register_financial_derived_tasks",
            (("service_data_sync.financial.derive_security", ("SSE", "600519"), True),),
        ),
    )
    for module_name, register_name, tasks in task_cases:
        module = importlib.import_module(module_name)
        app = Celery(f"legacy-entrypoint-{register_name}")
        register = _register(module, register_name)
        register(app, settings=object())
        registered = {name: app.tasks[name] for name, _, _ in tasks}
        register(app, settings=object())
        assert {name: app.tasks[name] for name, _, _ in tasks} == registered
        for task_name, arguments, must_reject in tasks:
            task = app.tasks[task_name]
            if not must_reject:
                assert task.run(*arguments) == {"status": LEGACY_ENTRYPOINT_UNAVAILABLE}
                continue
            with pytest.raises(LegacyEntryPointUnavailable) as error:
                task.run(*arguments)
            assert error.value.code == LEGACY_ENTRYPOINT_UNAVAILABLE
            assert error.value.entrypoint == task_name


def test_legacy_sw_sector_beat_schedule_is_empty() -> None:
    """旧 beat 不能自行投递同步消息，计划只由 data-operations scheduler 维护。"""
    module = importlib.import_module("service_data_sync.infrastructure.messaging.sw_sector_tasks")
    assert module.sw_sector_beat_schedule(settings=object()) == {}


def _main(module: ModuleType) -> Callable[[list[str]], int]:
    """取得 CLI 主函数，并在测试失败时给出明确的模块契约错误。"""
    candidate = getattr(module, "main", None)
    if not callable(candidate):
        raise AssertionError(f"{module.__name__} has no callable main")
    return cast(Callable[[list[str]], int], candidate)


def _register(module: ModuleType, name: str) -> Callable[..., None]:
    """取得幂等任务注册函数，不容许测试静默跳过错误模块。"""
    candidate = getattr(module, name, None)
    if not callable(candidate):
        raise AssertionError(f"{module.__name__} has no callable {name}")
    return cast(Callable[..., None], candidate)
