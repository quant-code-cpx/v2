"""已停用的板块 EOD 历史 `Celery` 入口。"""

from __future__ import annotations

from celery import Celery

from service_data_sync.application.legacy_entrypoints import reject_legacy_task
from service_data_sync.bootstrap.settings import Settings

_DISPATCH_TASK = "service_data_sync.sector_eod.dispatch_shadow"
_RUN_TASK = "service_data_sync.sector_eod.run"
_REAP_TASK = "service_data_sync.sector_eod.reap"


def register_sector_eod_tasks(app: Celery, *, settings: Settings) -> None:
    """注册安全停用的旧 EOD 分发、执行和 reaper 任务名。"""
    del settings
    if _DISPATCH_TASK not in app.tasks:

        @app.task(name=_DISPATCH_TASK, shared=False)
        def dispatch_shadow() -> None:
            """拒绝旧 shadow 分发，调度只能提交受持久化计划治理的 command。"""
            reject_legacy_task(_DISPATCH_TASK)

    if _REAP_TASK not in app.tasks:

        @app.task(name=_REAP_TASK, shared=False)
        def reap_expired_leases() -> None:
            """拒绝旧租约回收，run 恢复只能由控制面 reaper 持有当前 fence 执行。"""
            reject_legacy_task(_REAP_TASK)

    if _RUN_TASK in app.tasks:
        return

    @app.task(name=_RUN_TASK, shared=False)
    def run_sector_eod(scheme: str, trade_date: str) -> None:
        """拒绝旧 EOD 执行消息，防止 source、publication 和终态脱离 command。"""
        del scheme, trade_date
        reject_legacy_task(_RUN_TASK)
