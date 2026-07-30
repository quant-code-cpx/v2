"""资金流旧 `Celery` 任务的安全停用测试。"""

from __future__ import annotations

from typing import cast

import pytest
from celery import Celery

from service_data_sync.application.legacy_entrypoints import (
    LEGACY_ENTRYPOINT_UNAVAILABLE,
    LegacyEntryPointUnavailable,
)
from service_data_sync.bootstrap.settings import Settings
from service_data_sync.infrastructure.messaging import money_flow_tasks


def test_money_flow_tasks_keep_probe_offline_and_reject_partition_execution() -> None:
    """探针不读取来源，旧分区同步任务必须拒绝任意 capability 与参数。"""
    app = Celery("legacy-money-flow-tasks")
    money_flow_tasks.register_money_flow_tasks(app, settings=cast(Settings, object()))
    assert app.tasks[money_flow_tasks._PROBE_TASK].run() == {
        "status": LEGACY_ENTRYPOINT_UNAVAILABLE
    }
    with pytest.raises(LegacyEntryPointUnavailable):
        app.tasks[money_flow_tasks._SYNC_TASK].run("money_flow.daily", {"date": "2026-07-29"})
