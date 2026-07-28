"""启动注册受控 EOD 任务的 Celery worker 进程。"""

from __future__ import annotations

from collections.abc import Sequence

from service_data_sync.bootstrap.logging import configure_logging
from service_data_sync.bootstrap.settings import load_settings
from service_data_sync.infrastructure.messaging.celery_app import create_worker_app


def main(argv: Sequence[str] | None = None) -> int:
    """配置并启动 worker；任务执行仍受 source、scheduler、publish 与日历门控。"""
    settings = load_settings()
    configure_logging(settings, process_role="worker")
    app = create_worker_app(settings)
    worker_arguments = ["worker", f"--loglevel={settings.log_level.lower()}"]
    if argv is not None:
        worker_arguments.extend(argv)
    app.worker_main(worker_arguments)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
