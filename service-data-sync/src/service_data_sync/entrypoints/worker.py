from __future__ import annotations

from collections.abc import Sequence

from service_data_sync.bootstrap.logging import configure_logging
from service_data_sync.bootstrap.settings import load_settings
from service_data_sync.infrastructure.messaging.celery_app import create_worker_app


def main(argv: Sequence[str] | None = None) -> int:
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
