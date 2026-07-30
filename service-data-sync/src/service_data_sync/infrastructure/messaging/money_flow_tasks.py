"""已停用的资金流历史 `Celery` 入口。"""

from __future__ import annotations

from celery import Celery

from service_data_sync.application.legacy_entrypoints import (
    LEGACY_ENTRYPOINT_UNAVAILABLE,
    reject_legacy_task,
)
from service_data_sync.bootstrap.settings import Settings

_PROBE_TASK = "service_data_sync.money_flow.probe"
_SYNC_TASK = "service_data_sync.money_flow.sync_partition"


def register_money_flow_tasks(app: Celery, *, settings: Settings) -> None:
    """注册安全停用的旧资金流任务名，不解释任意 capability 或参数。"""
    del settings
    if _PROBE_TASK not in app.tasks:

        @app.task(name=_PROBE_TASK, shared=False)
        def probe() -> dict[str, str]:
            """返回稳定停用状态，不泄漏可用 provider 或来源策略。"""
            return {"status": LEGACY_ENTRYPOINT_UNAVAILABLE}

    if _SYNC_TASK in app.tasks:
        return

    @app.task(name=_SYNC_TASK, shared=False)
    def sync_partition(
        capability: str, parameters: dict[str, str], mode: str = "scheduled"
    ) -> None:
        """拒绝旧分区消息，避免参数直接进入 canonical 资金流同步用例。"""
        del capability, parameters, mode
        reject_legacy_task(_SYNC_TASK)
