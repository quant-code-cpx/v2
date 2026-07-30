"""启动板块 EOD beat 调度器；默认没有 schedule，直到显式 shadow 开关开启。

调度进程只按配置投递受控任务和租约回收任务，不承担数据抓取或发布；真正的来源、交易
日、质量和生产发布门禁仍在 worker 任务中二次验证，避免单点配置误触发消费版本变化。
"""

from __future__ import annotations

from collections.abc import Sequence

from service_data_sync.bootstrap.logging import configure_logging
from service_data_sync.bootstrap.settings import load_settings
from service_data_sync.infrastructure.messaging.celery_app import create_worker_app


def main(argv: Sequence[str] | None = None) -> int:
    """以独立 beat 进程运行上海时区调度，避免把 scheduler 混入 worker 生命周期。

    Celery 配置会在能力、日历和 shadow 开关未同时满足时保留空 schedule；因此启动
    beat 进程本身不会绕过 EOD 发布门控，调度职责也能独立于执行 worker 扩缩容。
    """
    settings = load_settings()
    configure_logging(settings, process_role="scheduler")
    app = create_worker_app(settings)
    # `beat` 的状态可从权威命令重建，固定写入 tmpfs 才能兼容容器只读根文件系统。
    scheduler_arguments = [
        "beat",
        f"--loglevel={settings.log_level.lower()}",
        "--schedule=/tmp/celerybeat-schedule",
    ]
    if argv is not None:
        scheduler_arguments.extend(argv)
    app.start(scheduler_arguments)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
