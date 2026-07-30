"""已停用的证券生命周期历史 `Celery` 入口。"""

from __future__ import annotations

from celery import Celery

from service_data_sync.application.legacy_entrypoints import (
    LEGACY_ENTRYPOINT_UNAVAILABLE,
    reject_legacy_task,
)
from service_data_sync.bootstrap.settings import Settings

_PROBE_TASK = "service_data_sync.equity_lifecycle.probe"
_SYNC_TASK = "service_data_sync.equity_lifecycle.sync_exchange"
_REPLAY_TASK = "service_data_sync.equity_lifecycle.replay_exchange"


def register_equity_lifecycle_tasks(app: Celery, *, settings: Settings) -> None:
    """注册安全停用的生命周期任务名，避免旧 checkpoint 恢复绕过全局 slot。"""
    del settings
    if _PROBE_TASK not in app.tasks:

        @app.task(name=_PROBE_TASK, shared=False)
        def probe() -> dict[str, str]:
            """返回稳定停用状态，不枚举来源 adapter 或账户配置。"""
            return {"status": LEGACY_ENTRYPOINT_UNAVAILABLE}

    if _SYNC_TASK not in app.tasks:

        @app.task(name=_SYNC_TASK, shared=False)
        def sync_exchange(exchange: str, target_date: str) -> None:
            """拒绝旧生命周期同步，避免消息直接写入 canonical 事实和 checkpoint。"""
            del exchange, target_date
            reject_legacy_task(_SYNC_TASK)

    if _REPLAY_TASK in app.tasks:
        return

    @app.task(name=_REPLAY_TASK, shared=False)
    def replay_exchange(exchange: str) -> None:
        """拒绝旧 checkpoint 重放，恢复必须由 command dispatcher 和当前 fence 管理。"""
        del exchange
        reject_legacy_task(_REPLAY_TASK)
