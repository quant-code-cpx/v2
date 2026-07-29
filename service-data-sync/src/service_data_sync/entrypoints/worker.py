"""启动注册受控 EOD 任务的 Celery worker 进程。

worker 从消息队列取得已注册任务，但每个任务仍按来源策略、交易日历、质量门和发布开关
自行校验；因此队列中存在消息不等于数据会被抓取或提升为消费者可见版本。
"""

from __future__ import annotations

from collections.abc import Sequence

from service_data_sync.bootstrap.logging import configure_logging
from service_data_sync.bootstrap.settings import load_settings
from service_data_sync.infrastructure.messaging.celery_app import create_worker_app


def main(argv: Sequence[str] | None = None) -> int:
    """配置并启动 worker；任务执行仍受来源、调度、发布与日历门控。

    worker 只执行已经注册的任务；它既不自行开启实验性能力，也不因收到队列消息而
    绕过任务内部的来源策略和交易日校验，因此可安全地与独立 beat 进程分别部署。
    """
    settings = load_settings()
    configure_logging(settings, process_role="worker")
    app = create_worker_app(settings)
    # 以明确子命令进入 Celery，同时允许部署层追加并发、队列等运行参数。
    worker_arguments = ["worker", f"--loglevel={settings.log_level.lower()}"]
    if argv is not None:
        worker_arguments.extend(argv)
    app.worker_main(worker_arguments)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
