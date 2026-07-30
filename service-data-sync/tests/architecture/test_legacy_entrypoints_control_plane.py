"""验证遗留同步入口不能绕过 data-operations command 控制面。"""

from __future__ import annotations

from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_CLI_FILES = (
    "sector_catalog.py",
    "sector_bars.py",
    "sector_membership.py",
    "sector_eod.py",
    "sector_eod_reaper.py",
    "sector_eod_rollback.py",
    "equity_catalog.py",
    "equity_lifecycle.py",
    "financial_derived.py",
    "money_flow.py",
    "index_shadow.py",
    "derivative_daily_bars.py",
    "etf.py",
    "margin.py",
    "stock_connect.py",
    "corporate_events.py",
    "trading_events.py",
    "sw_sector.py",
)
_TASK_FILES = (
    "sw_sector_tasks.py",
    "money_flow_tasks.py",
    "sector_eod_tasks.py",
    "equity_lifecycle_tasks.py",
    "financial_derived_tasks.py",
)
_FORBIDDEN = (
    "SyncService",
    ".sync(",
    ".replay(",
    ".rollback_published_snapshot(",
    ".requeue_expired_leases(",
    "run_financial_derivation",
    ".publish_",
    "service_data_sync.infrastructure.persistence",
    "service_data_sync.bootstrap.container",
    "service_data_sync.infrastructure.object_storage",
)


@pytest.mark.parametrize("filename", _CLI_FILES)
def test_legacy_cli_has_no_direct_canonical_execution(filename: str) -> None:
    """每个旧 CLI 只能调用统一拒绝器，不能导入或调用历史同步用例。"""
    source = _read("src/service_data_sync/entrypoints", filename)
    assert "reject_legacy_cli" in source
    _assert_no_bypass(source, filename)


@pytest.mark.parametrize("filename", _TASK_FILES)
def test_legacy_celery_task_has_no_direct_canonical_execution(filename: str) -> None:
    """每个旧 Celery 模块只能安全拒绝，不能同步、重放、回滚或回收旧租约。"""
    source = _read("src/service_data_sync/infrastructure/messaging", filename)
    assert "reject_legacy_task" in source
    _assert_no_bypass(source, filename)


def test_worker_does_not_register_legacy_tasks_or_legacy_beat() -> None:
    """组合根不能重新暴露旧任务注册或 beat，避免停用模块被间接唤醒。"""
    source = _read("src/service_data_sync/infrastructure/messaging", "celery_app.py")
    forbidden_registrations = (
        "register_sw_sector_tasks",
        "register_money_flow_tasks",
        "register_sector_eod_tasks",
        "register_equity_lifecycle_tasks",
        "register_financial_derived_tasks",
        "sw_sector_beat_schedule",
    )
    assert all(name not in source for name in forbidden_registrations)


def _read(directory: str, filename: str) -> str:
    """读取受审计生产文件，确保测试只检查源码而不触发导入副作用。"""
    return (_ROOT / directory / filename).read_text(encoding="utf-8")


def _assert_no_bypass(source: str, filename: str) -> None:
    """拒绝任何可直接写 canonical、重放或回收旧租约的调用形态。"""
    found = [token for token in _FORBIDDEN if token in source]
    assert not found, f"{filename} bypasses data operations: {', '.join(found)}"
