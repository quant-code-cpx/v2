"""已停用的财务派生指标历史 `Celery` 入口。"""

from __future__ import annotations

from celery import Celery

from service_data_sync.application.legacy_entrypoints import reject_legacy_task
from service_data_sync.bootstrap.settings import Settings

_TASK = "service_data_sync.financial.derive_security"


def register_financial_derived_tasks(app: Celery, *, settings: Settings) -> None:
    """注册安全停用的派生任务名，阻止它绕过 command 审计和全局执行槽。"""
    del settings
    if _TASK in app.tasks:
        return

    @app.task(name=_TASK, shared=False)
    def derive_security(exchange: str, symbol: str) -> None:
        """拒绝旧派生计算消息，避免直接读取报表并发布指标版本。"""
        del exchange, symbol
        reject_legacy_task(_TASK)
