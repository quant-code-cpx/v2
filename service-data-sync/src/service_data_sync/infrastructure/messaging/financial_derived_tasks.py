"""平台派生财务指标的显式单证券 Celery 任务。"""

from __future__ import annotations

from uuid import uuid4

from celery import Celery, Task

from service_data_sync.bootstrap.container import build_container
from service_data_sync.bootstrap.financial_derived import run_financial_derivation
from service_data_sync.bootstrap.settings import Settings
from service_data_sync.domain.equity import Exchange

_TASK = "service_data_sync.financial.derive_security"


def register_financial_derived_tasks(app: Celery, *, settings: Settings) -> None:
    """注册无自动调度的单证券派生任务；由调用方按报表发布依赖显式触发。"""
    if _TASK in app.tasks:
        return

    @app.task(bind=True, name=_TASK, shared=False, acks_late=True)
    def derive_security(task: Task, exchange: str, symbol: str) -> dict[str, str | int]:
        """从当前已发布报表计算派生指标；同一 Celery 请求重试复用运行账本。"""
        if not settings.financial_enabled:
            raise RuntimeError("financial sync is disabled")
        task_id = task.request.id or str(uuid4())
        container = build_container(settings)
        try:
            result = run_financial_derivation(
                database=container.database,
                exchange=Exchange(exchange),
                symbol=symbol,
                mode="scheduled",
                request_key=f"celery:{task_id}",
            )
            return {
                "dataVersion": str(result.publication.data_version),
                "computed": result.computed_count,
                "skipped": result.skipped_count,
                "rowCount": result.publication.row_count,
            }
        finally:
            container.close()
