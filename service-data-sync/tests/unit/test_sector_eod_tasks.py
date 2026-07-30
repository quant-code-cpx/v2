"""板块 EOD 旧 `Celery` 任务的安全停用测试。"""

from __future__ import annotations

from typing import cast

import pytest
from celery import Celery

from service_data_sync.application.legacy_entrypoints import LegacyEntryPointUnavailable
from service_data_sync.bootstrap.settings import Settings
from service_data_sync.infrastructure.messaging import sector_eod_tasks


def test_sector_eod_tasks_are_idempotent_and_reject_all_legacy_execution() -> None:
    """旧分发、执行和 reaper 消息必须全部拒绝，且重复注册不替换任务对象。"""
    app = Celery("legacy-sector-eod-tasks")
    settings = cast(Settings, object())
    sector_eod_tasks.register_sector_eod_tasks(app, settings=settings)
    names = (
        sector_eod_tasks._DISPATCH_TASK,
        sector_eod_tasks._RUN_TASK,
        sector_eod_tasks._REAP_TASK,
    )
    first = {name: app.tasks[name] for name in names}
    sector_eod_tasks.register_sector_eod_tasks(app, settings=settings)
    assert {name: app.tasks[name] for name in names} == first
    with pytest.raises(LegacyEntryPointUnavailable):
        app.tasks[sector_eod_tasks._DISPATCH_TASK].run()
    with pytest.raises(LegacyEntryPointUnavailable):
        app.tasks[sector_eod_tasks._RUN_TASK].run("eastmoney.industry", "2026-07-29")
    with pytest.raises(LegacyEntryPointUnavailable):
        app.tasks[sector_eod_tasks._REAP_TASK].run()
