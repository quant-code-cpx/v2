"""Celery worker 配置与入口的单元测试。"""

from __future__ import annotations

from service_data_sync.bootstrap.settings import load_settings
from service_data_sync.entrypoints import worker
from service_data_sync.infrastructure.messaging.celery_app import create_worker_app


def test_worker_app_has_no_business_tasks(configured_environment: None) -> None:
    """保证基础 worker 不含未经评审的业务任务注册。"""
    app = create_worker_app(load_settings())
    custom_task_names = {
        task_name for task_name in app.tasks if task_name.startswith("service_data_sync.")
    }

    assert custom_task_names == set()
    assert app.conf.task_ignore_result is True
    assert app.conf.result_backend is None


def test_worker_entrypoint_builds_an_empty_worker(
    configured_environment: None,
    monkeypatch,
) -> None:
    """在必需 worker 与日志级别参数后传递用户提供的 Celery 参数。"""
    calls: list[list[str]] = []

    class FakeWorkerApp:
        """记录入口调用参数的 worker 替身。"""

        def worker_main(self, arguments: list[str]) -> None:
            """捕获 worker 调用参数，供入口断言使用。"""
            calls.append(arguments)

    # 将基础设施初始化替换为空操作和 fixture worker，隔离参数组装逻辑。
    monkeypatch.setattr(worker, "configure_logging", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(worker, "create_worker_app", lambda _settings: FakeWorkerApp())

    assert worker.main(["--without-gossip"]) == 0
    assert calls == [["worker", "--loglevel=info", "--without-gossip"]]
