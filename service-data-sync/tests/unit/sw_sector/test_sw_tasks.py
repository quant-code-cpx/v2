"""申万行业旧 `Celery` 任务的安全停用测试。"""

from __future__ import annotations

from typing import cast

import pytest
from celery import Celery

from service_data_sync.application.legacy_entrypoints import (
    LEGACY_ENTRYPOINT_UNAVAILABLE,
    LegacyEntryPointUnavailable,
)
from service_data_sync.bootstrap.settings import Settings
from service_data_sync.infrastructure.messaging import sw_sector_tasks


def test_sw_sector_tasks_keep_probe_offline_and_reject_sync_and_replay() -> None:
    """探针只返回停用状态，旧同步与重放消息不能触发独立 publication。"""
    app = Celery("legacy-sw-sector-tasks")
    settings = cast(Settings, object())
    sw_sector_tasks.register_sw_sector_tasks(app, settings=settings)
    assert app.tasks[sw_sector_tasks._PROBE_TASK].run() == {"status": LEGACY_ENTRYPOINT_UNAVAILABLE}
    with pytest.raises(LegacyEntryPointUnavailable):
        app.tasks[sw_sector_tasks._SYNC_TASK].run()
    with pytest.raises(LegacyEntryPointUnavailable):
        app.tasks[sw_sector_tasks._REPLAY_TASK].run("2026-07-29")
    assert sw_sector_tasks.sw_sector_beat_schedule(settings=settings) == {}
