"""创建同步服务唯一的 `Celery` 工作进程与显式调度表。

工作进程只负责把已声明的同步单元投递到队列；任务本身仍通过应用服务、来源中立
端口和仓储发布数据。所有自动调度默认由配置关闭，且时区固定为上海时间，避免把
日线或 EOD 任务误投到错误的自然日。
"""

from __future__ import annotations

from celery import Celery
from celery.schedules import crontab

from service_data_sync.bootstrap.settings import Settings
from service_data_sync.infrastructure.messaging.equity_lifecycle_tasks import (
    register_equity_lifecycle_tasks,
)
from service_data_sync.infrastructure.messaging.equity_market_tasks import (
    register_equity_market_tasks,
)
from service_data_sync.infrastructure.messaging.financial_derived_tasks import (
    register_financial_derived_tasks,
)
from service_data_sync.infrastructure.messaging.financial_tasks import register_financial_tasks
from service_data_sync.infrastructure.messaging.money_flow_tasks import register_money_flow_tasks
from service_data_sync.infrastructure.messaging.sector_eod_tasks import register_sector_eod_tasks
from service_data_sync.infrastructure.messaging.sw_sector_tasks import (
    register_sw_sector_tasks,
    sw_sector_beat_schedule,
)

_SECTOR_EOD_DISPATCH_TASK = "service_data_sync.sector_eod.dispatch_shadow"
_SECTOR_EOD_REAP_TASK = "service_data_sync.sector_eod.reap"


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
    register_financial_tasks(app, settings=settings)
    register_financial_derived_tasks(app, settings=settings)
    register_equity_lifecycle_tasks(app, settings=settings)
    register_equity_market_tasks(app, settings=settings)
    register_money_flow_tasks(app, settings=settings)
    register_sw_sector_tasks(app, settings=settings)
    register_sector_eod_tasks(app, settings=settings)
    beat_schedule: dict[str, object] = dict(sw_sector_beat_schedule(settings=settings))
    if settings.equity_scheduler_enabled:
        # 每个周期和参考数据能力独立分发，日线失败不会阻断或触发周/月派生。
        beat_schedule.update(
            {
                "equity-daily-bars": {
                    "task": "service_data_sync.equity_market.dispatch",
                    "schedule": crontab(hour=16, minute=10),
                    "args": ("equity.bar.1d.raw",),
                },
                "equity-weekly-bars": {
                    "task": "service_data_sync.equity_market.dispatch",
                    "schedule": crontab(hour=18, minute=0, day_of_week="friday"),
                    "args": ("equity.bar.1w.raw",),
                },
                "equity-monthly-bars": {
                    "task": "service_data_sync.equity_market.dispatch",
                    "schedule": crontab(hour=8, minute=0, day_of_month="1"),
                    "args": ("equity.bar.1mo.raw",),
                },
                "equity-adjustment-factors": {
                    "task": "service_data_sync.equity_market.dispatch",
                    "schedule": crontab(hour=8, minute=30, day_of_week="saturday"),
                    "args": ("equity.adjustment_factor",),
                },
                "equity-corporate-actions": {
                    "task": "service_data_sync.equity_market.dispatch",
                    "schedule": crontab(hour=19, minute=0),
                    "args": ("equity.corporate_action",),
                },
                "equity-company-profiles": {
                    "task": "service_data_sync.equity_market.dispatch",
                    "schedule": crontab(hour=7, minute=0, day_of_month="1"),
                    "args": ("equity.profile",),
                },
            }
        )
    if settings.sector_eod_scheduler_enabled:
        # scheduler 只投递明确日期，再由任务用权威日历复核，避免跨日或休市误跑。
        beat_schedule.update(
            {
                "sector-eod-shadow-dispatch": {
                    "task": _SECTOR_EOD_DISPATCH_TASK,
                    "schedule": crontab(hour=16, minute=20),
                },
                "sector-eod-lease-reaper": {
                    "task": _SECTOR_EOD_REAP_TASK,
                    "schedule": 5 * 60,
                },
            }
        )
    if beat_schedule:
        app.conf.beat_schedule = beat_schedule
    return app
