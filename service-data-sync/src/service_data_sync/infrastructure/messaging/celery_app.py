"""Celery worker 的受限 broker 配置。"""

from __future__ import annotations

from celery import Celery
from celery.schedules import crontab

from service_data_sync.bootstrap.settings import Settings
from service_data_sync.infrastructure.messaging.sector_eod_tasks import register_sector_eod_tasks

_SECTOR_EOD_DISPATCH_TASK = "service_data_sync.sector_eod.dispatch_shadow"
_SECTOR_EOD_REAP_TASK = "service_data_sync.sector_eod.reap"


def create_worker_app(settings: Settings) -> Celery:
    """创建受限 broker worker，并注册默认关闭的 EOD shadow 任务和调度表。"""
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
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        timezone="Asia/Shanghai",
        enable_utc=True,
    )
    register_sector_eod_tasks(app, settings=settings)
    if settings.sector_eod_scheduler_enabled:
        # scheduler 只投递明确日期，再由任务用权威日历复核，避免跨日或休市误跑。
        app.conf.beat_schedule = {
            "sector-eod-shadow-dispatch": {
                "task": _SECTOR_EOD_DISPATCH_TASK,
                "schedule": crontab(hour=16, minute=20),
            },
            "sector-eod-lease-reaper": {
                "task": _SECTOR_EOD_REAP_TASK,
                "schedule": 5 * 60,
            },
        }
    return app
