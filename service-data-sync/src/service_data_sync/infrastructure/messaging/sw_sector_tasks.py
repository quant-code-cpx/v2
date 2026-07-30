"""已停用的申万行业历史 `Celery` 入口。"""

from __future__ import annotations

from celery import Celery

from service_data_sync.application.legacy_entrypoints import (
    LEGACY_ENTRYPOINT_UNAVAILABLE,
    reject_legacy_task,
)
from service_data_sync.bootstrap.settings import Settings

_PROBE_TASK = "service_data_sync.sw_sector.probe"
_SYNC_TASK = "service_data_sync.sw_sector.sync_current"
_REPLAY_TASK = "service_data_sync.sw_sector.replay_snapshot"


def register_sw_sector_tasks(app: Celery, *, settings: Settings) -> None:
    """注册安全停用的旧任务名，阻止遗留消息直写 taxonomy 或 publication。"""
    del settings
    if _PROBE_TASK not in app.tasks:

        @app.task(name=_PROBE_TASK, shared=False)
        def probe() -> dict[str, str]:
            """返回稳定停用状态，不读取 adapter、数据库或对象存储。"""
            return {"status": LEGACY_ENTRYPOINT_UNAVAILABLE}

    if _SYNC_TASK not in app.tasks:

        @app.task(name=_SYNC_TASK, shared=False)
        def sync_current() -> None:
            """拒绝旧当天同步消息，直到存在对应的 fenced command 执行器。"""
            reject_legacy_task(_SYNC_TASK)

    if _REPLAY_TASK in app.tasks:
        return

    @app.task(name=_REPLAY_TASK, shared=False)
    def replay_snapshot(snapshot_date: str) -> None:
        """拒绝旧重放消息，避免历史 checkpoint 绕过全局执行槽。"""
        del snapshot_date
        reject_legacy_task(_REPLAY_TASK)


def sw_sector_beat_schedule(*, settings: Settings) -> dict[str, dict[str, object]]:
    """停用遗留 beat；自动计划只能由 data-operations scheduler 持久化并投递 command。"""
    del settings
    return {}
