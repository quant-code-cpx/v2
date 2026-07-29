"""从已发布财务事实计算平台派生指标的显式单证券 `Celery` 任务。

派生计算不调用供应商；它只读取当前通过质量检查的报表版本，写入带输入血缘的新
发布版本。任务没有自动调度，调用方应在确认所依赖报表已发布后显式投递。
"""

from __future__ import annotations

from uuid import uuid4

from celery import Celery, Task

from service_data_sync.bootstrap.container import build_container
from service_data_sync.bootstrap.financial_derived import run_financial_derivation
from service_data_sync.bootstrap.settings import Settings
from service_data_sync.domain.equity import Exchange

_TASK = "service_data_sync.financial.derive_security"


def register_financial_derived_tasks(app: Celery, *, settings: Settings) -> None:
    """注册无自动调度的单证券派生任务；由调用方按报表发布依赖显式触发。

    同名任务已存在时不重新注册，防止嵌入式 worker 初始化改变已运行应用的任务定义。
    """
    if _TASK in app.tasks:
        return

    @app.task(bind=True, name=_TASK, shared=False, acks_late=True)
    def derive_security(task: Task, exchange: str, symbol: str) -> dict[str, str | int]:
        """从当前已发布报表计算派生指标；同一 Celery 请求重试复用运行账本。

        ``request_key`` 以 `Celery` 任务 ID 构造，使消息重复投递可关联同一运行而不会把
        相同输入误记成多次独立计算。
        """
        if not settings.financial_enabled:
            raise RuntimeError("financial sync is disabled")
        # `Celery` 在极端故障时可能没有 ID，随机 UUID 只用于本次无法关联的独立执行。
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
