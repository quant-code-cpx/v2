"""启动板块 EOD beat 调度器；默认没有 schedule，直到显式 shadow 开关开启。"""

from __future__ import annotations

from collections.abc import Sequence

from service_data_sync.bootstrap.logging import configure_logging
from service_data_sync.bootstrap.settings import load_settings
from service_data_sync.infrastructure.messaging.celery_app import create_worker_app


def main(argv: Sequence[str] | None = None) -> int:
    """以独立 beat 进程运行 16:20 上海时区调度，避免把 scheduler 混入 worker 生命周期。"""
    settings = load_settings()
    configure_logging(settings, process_role="scheduler")
    app = create_worker_app(settings)
    scheduler_arguments = ["beat", f"--loglevel={settings.log_level.lower()}"]
    if argv is not None:
        scheduler_arguments.extend(argv)
    app.start(scheduler_arguments)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
