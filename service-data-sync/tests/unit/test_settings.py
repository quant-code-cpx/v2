from __future__ import annotations

import pytest

from service_data_sync.bootstrap.errors import ConfigurationError
from service_data_sync.bootstrap.settings import Environment, LogFormat, load_settings


def test_settings_loads_typed_environment(configured_environment: None) -> None:
    settings = load_settings()

    assert settings.environment is Environment.TEST
    assert settings.log_format is LogFormat.JSON
    assert settings.s3_secret_key.get_secret_value() == "test-secret-key"


def test_settings_hides_validation_details(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATA_SYNC_DATABASE_URL", raising=False)

    with pytest.raises(ConfigurationError, match="invalid service-data-sync configuration"):
        load_settings()


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("DATA_SYNC_LOG_LEVEL", "verbose"),
        ("DATA_SYNC_S3_ENDPOINT_URL", "minio.local"),
        ("DATA_SYNC_S3_BUCKET", " "),
    ],
)
def test_settings_rejects_invalid_values(
    configured_environment: None,
    monkeypatch: pytest.MonkeyPatch,
    variable: str,
    value: str,
) -> None:
    monkeypatch.setenv(variable, value)

    with pytest.raises(ConfigurationError):
        load_settings()
