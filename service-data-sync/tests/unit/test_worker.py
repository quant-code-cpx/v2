from __future__ import annotations

from service_data_sync.bootstrap.settings import load_settings
from service_data_sync.entrypoints import worker
from service_data_sync.infrastructure.messaging.celery_app import create_worker_app


def test_worker_app_has_no_business_tasks(configured_environment: None) -> None:
    """Keep foundation worker free of unreviewed business task registrations."""
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
    """Pass user-supplied Celery flags after mandatory worker and log-level arguments."""
    calls: list[list[str]] = []

    class FakeWorkerApp:
        def worker_main(self, arguments: list[str]) -> None:
            """Capture worker invocation arguments for entrypoint assertion."""
            calls.append(arguments)

    # Replace infrastructure setup with no-op and fixture worker to isolate argument assembly.
    monkeypatch.setattr(worker, "configure_logging", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(worker, "create_worker_app", lambda _settings: FakeWorkerApp())

    assert worker.main(["--without-gossip"]) == 0
    assert calls == [["worker", "--loglevel=info", "--without-gossip"]]
