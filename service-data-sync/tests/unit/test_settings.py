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
    assert settings.trading_calendar_enabled is False


def test_settings_loads_explicit_calendar_enablement(
    configured_environment: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """只有显式环境变量才能启用已发布的交易日历适配器。"""
    monkeypatch.setenv("DATA_SYNC_TRADING_CALENDAR_ENABLED", "true")

    assert load_settings().trading_calendar_enabled is True


def test_equity_scheduler_requires_market_capability_enablement(
    configured_environment: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """定时调度不能在个股市场能力关闭时单独启动。"""
    monkeypatch.setenv("DATA_SYNC_EQUITY_SCHEDULER_ENABLED", "true")

    with pytest.raises(ConfigurationError):
        load_settings()


def test_equity_market_and_scheduler_load_together(
    configured_environment: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """显式同时开启个股市场能力与调度时配置可用。"""
    monkeypatch.setenv("DATA_SYNC_EQUITY_MARKET_ENABLED", "true")
    monkeypatch.setenv("DATA_SYNC_EQUITY_SCHEDULER_ENABLED", "true")

    settings = load_settings()

    assert settings.equity_market_enabled is True
    assert settings.equity_scheduler_enabled is True


def test_financial_dark_launch_stays_disabled_by_default(configured_environment: None) -> None:
    """默认财务开关和来源策略必须共同保持关闭，避免意外访问未获批来源。"""
    settings = load_settings()

    assert settings.financial_enabled is False
    assert settings.financial_source_policy == "disabled"
    assert settings.financial_max_concurrency is None


def test_financial_dark_launch_requires_policy_and_budgets_when_enabled(
    configured_environment: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """启用财务能力缺少任一来源策略或预算时，启动必须 fail closed。"""
    monkeypatch.setenv("DATA_SYNC_FINANCIAL_ENABLED", "true")

    with pytest.raises(ConfigurationError, match="invalid service-data-sync configuration"):
        load_settings()


def test_financial_dark_launch_loads_explicit_policy_and_budgets(
    configured_environment: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """只在显式给出策略名和全部预算后接受 dark-launch 配置。"""
    monkeypatch.setenv("DATA_SYNC_FINANCIAL_ENABLED", "true")
    monkeypatch.setenv("DATA_SYNC_FINANCIAL_SOURCE_POLICY", "research-policy-pending")
    monkeypatch.setenv("DATA_SYNC_FINANCIAL_MAX_CONCURRENCY", "1")
    monkeypatch.setenv("DATA_SYNC_FINANCIAL_REQUESTS_PER_MINUTE", "1")
    monkeypatch.setenv("DATA_SYNC_FINANCIAL_REQUEST_TIMEOUT_SECONDS", "1")

    settings = load_settings()

    assert settings.financial_enabled is True
    assert settings.financial_source_policy == "research-policy-pending"
    assert settings.financial_max_concurrency == 1
    assert settings.financial_requests_per_minute == 1
    assert settings.financial_request_timeout_seconds == 1


def test_index_dark_launch_requires_akshare_and_exact_source_policy(
    configured_environment: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """指数影子探测必须显式依赖 AKShare 和已审核的中证来源策略。"""
    monkeypatch.setenv("DATA_SYNC_INDEX_ENABLED", "true")

    with pytest.raises(ConfigurationError, match="invalid service-data-sync configuration"):
        load_settings()

    monkeypatch.setenv("DATA_SYNC_AKSHARE_ENABLED", "true")
    monkeypatch.setenv("DATA_SYNC_INDEX_SOURCE_POLICY", "another-source")

    with pytest.raises(ConfigurationError, match="invalid service-data-sync configuration"):
        load_settings()


def test_index_dark_launch_loads_only_explicit_approved_policy(
    configured_environment: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """固定中证与国证 adapter 策略通过时，指数能力仍由独立开关控制。"""
    monkeypatch.setenv("DATA_SYNC_AKSHARE_ENABLED", "true")
    monkeypatch.setenv("DATA_SYNC_INDEX_ENABLED", "true")
    monkeypatch.setenv("DATA_SYNC_INDEX_SOURCE_POLICY", "AKSHARE-CSINDEX-CNINDEX")

    settings = load_settings()

    assert settings.index_enabled is True
    assert settings.index_source_policy == "akshare-csindex-cnindex"


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
