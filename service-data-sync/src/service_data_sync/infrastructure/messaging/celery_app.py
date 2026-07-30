"""创建同步服务唯一的 `Celery` 工作进程与显式调度表。

工作进程只负责把已声明的同步单元投递到队列；任务本身仍通过应用服务、来源中立
端口和仓储发布数据。所有自动调度默认由配置关闭，且时区固定为上海时间，避免把
日线或 EOD 任务误投到错误的自然日。
"""

from __future__ import annotations

from celery import Celery

from service_data_sync.bootstrap.settings import Settings
from service_data_sync.infrastructure.messaging.data_operations_tasks import (
    register_data_operations_tasks,
)


def create_worker_app(settings: Settings) -> Celery:
    """创建受限消息代理工作进程，并注册默认关闭的同步任务和调度表。

    返回的应用不配置结果后端：同步结果由数据库发布版本和结构化日志追溯，避免把
    原始载荷或大结果写入消息系统。
    """
    app = Celery("service_data_sync", broker=settings.broker_url.get_secret_value())
    app.conf.update(
        broker_connection_timeout=5,
        broker_connection_retry_on_startup=True,
        broker_connection_max_retries=5,
        broker_connection_retry_interval_start=0,
        broker_connection_retry_interval_step=1,
        broker_connection_retry_interval_max=3,
        result_backend=None,
        task_ignore_result=True,
        task_send_sent_event=False,
        worker_send_task_events=False,
        # 任务完成后才确认；工作进程崩溃时消息代理可重新投递，发布层再负责幂等。
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        timezone="Asia/Shanghai",
        enable_utc=True,
    )
    register_data_operations_tasks(app, settings=settings)
    # 固定 tick 只唤醒数据库控制面；真正频率、时区、misfire 和 coalesce 保存在计划表。
    app.conf.beat_schedule = {
        "data-operations-dispatch": {
            "task": "service_data_sync.data_operations.dispatch",
            "schedule": 10.0,
        },
        "data-operations-reap": {
            "task": "service_data_sync.data_operations.reap",
            "schedule": 30.0,
        },
        "data-operations-health-dispatch": {
            "task": "service_data_sync.data_operations.health_dispatch",
            "schedule": 10.0,
        },
        "data-operations-scheduler-tick": {
            "task": "service_data_sync.data_operations.scheduler_tick",
            "schedule": 60.0,
        },
    }
    return app
