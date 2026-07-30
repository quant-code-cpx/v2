"""验证独立调度入口在只读容器中的启动参数。"""

from unittest.mock import Mock, patch

from service_data_sync.entrypoints import scheduler


def test_main_writes_scheduler_state_to_tmpfs() -> None:
    """验证 Celery beat 默认把可重建状态写入可写的 `/tmp`。"""
    settings = Mock(log_level="INFO")
    worker_app = Mock()

    with (
        patch.object(scheduler, "load_settings", return_value=settings),
        patch.object(scheduler, "configure_logging"),
        patch.object(scheduler, "create_worker_app", return_value=worker_app),
    ):
        assert scheduler.main() == 0

    worker_app.start.assert_called_once_with(
        ["beat", "--loglevel=info", "--schedule=/tmp/celerybeat-schedule"]
    )
