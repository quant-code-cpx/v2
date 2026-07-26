from __future__ import annotations

from celery import Celery

from service_data_sync.bootstrap.settings import Settings


def create_worker_app(settings: Settings) -> Celery:
    """Create broker-only worker with bounded startup retries and no result backend."""
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
    )
    return app
