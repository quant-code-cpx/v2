"""结构化日志脱敏与上下文字段的单元测试。"""

from __future__ import annotations

from service_data_sync.bootstrap.logging import (
    add_service_context,
    configure_logging,
    redact_secrets,
)
from service_data_sync.bootstrap.settings import load_settings


def test_redact_secrets_redacts_nested_sensitive_fields() -> None:
    """递归脱敏敏感键，同时保留安全的嵌套字段。"""
    rendered = redact_secrets(
        None,
        "info",
        {
            "event": "configuration.loaded",
            "database_url": "postgresql://name:password@example.test/db",
            "nested": {"authorization": "Bearer token"},
            "safe": "visible",
        },
    )

    assert rendered["database_url"] == "[REDACTED]"
    assert rendered["nested"]["authorization"] == "[REDACTED]"
    assert rendered["safe"] == "visible"


def test_logging_context_is_attached_without_exposing_secrets(
    configured_environment: None,
) -> None:
    """在已配置渲染器消费事件前附加服务身份字段。"""
    settings = load_settings()
    event = add_service_context(
        None, "info", {"event": "worker.ready"}, settings=settings, process_role="worker"
    )

    configure_logging(settings, process_role="worker")

    assert event["service"] == "service-data-sync"
    assert event["environment"] == "test"
    assert event["process_role"] == "worker"
