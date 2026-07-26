"""服务配置加载与校验的单元测试。"""

from __future__ import annotations

import pytest

from service_data_sync.bootstrap.errors import ConfigurationError
from service_data_sync.bootstrap.settings import Environment, LogFormat, load_settings


def test_settings_loads_typed_environment(configured_environment: None) -> None:
    """将测试环境加载为带类型的配置和密钥包装对象。"""
    settings = load_settings()

    assert settings.environment is Environment.TEST
    assert settings.log_format is LogFormat.JSON
    assert settings.s3_secret_key.get_secret_value() == "test-secret-key"


def test_settings_hides_validation_details(monkeypatch: pytest.MonkeyPatch) -> None:
    """将原始 Pydantic 错误替换为安全的配置领域错误。"""
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
    """拒绝无效日志级别、端点协议和空桶名称配置。"""
    monkeypatch.setenv(variable, value)

    with pytest.raises(ConfigurationError):
        load_settings()
