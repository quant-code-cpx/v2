from __future__ import annotations

from collections.abc import Iterator

import pytest


@pytest.fixture
def configured_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    values = {
        "DATA_SYNC_ENV": "test",
        "DATA_SYNC_LOG_LEVEL": "INFO",
        "DATA_SYNC_LOG_FORMAT": "json",
        "DATA_SYNC_DATABASE_URL": "postgresql+psycopg://data_sync:test@127.0.0.1:15432/data_sync",
        "DATA_SYNC_BROKER_URL": "redis://:test@127.0.0.1:16379/0",
        "DATA_SYNC_S3_ENDPOINT_URL": "http://127.0.0.1:19000",
        "DATA_SYNC_S3_ACCESS_KEY": "test-access-key",
        "DATA_SYNC_S3_SECRET_KEY": "test-secret-key",
        "DATA_SYNC_S3_BUCKET": "quant-data-sync-test",
        "DATA_SYNC_S3_REGION": "us-east-1",
        "DATA_SYNC_DIAGNOSTICS_TIMEOUT_SECONDS": "10",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    yield
