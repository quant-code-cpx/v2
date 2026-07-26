"""结构化日志配置与递归敏感信息脱敏。"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, cast

import structlog

from service_data_sync.bootstrap.settings import LogFormat, Settings

_SENSITIVE_MARKERS = (
    "password",
    "secret",
    "token",
    "key",
    "authorization",
    "cookie",
    "database_url",
    "broker_url",
)


def _is_sensitive(key: str) -> bool:
    """判断日志字段名是否可能携带凭证或连接密钥。"""
    return any(marker in key.lower() for marker in _SENSITIVE_MARKERS)


def _redact(value: Any, key: str | None = None) -> Any:
    """递归脱敏敏感映射，同时保留安全的事件结构。"""
    if key is not None and _is_sensitive(key):
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return {
            str(item_key): _redact(item_value, str(item_key))
            for item_key, item_value in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact(item) for item in value)
    return value


def redact_secrets(
    _logger: structlog.types.WrappedLogger,
    _method_name: str,
    event_dict: structlog.types.EventDict,
) -> structlog.types.EventDict:
    """作为 structlog 处理器递归执行密钥脱敏。"""
    return cast(structlog.types.EventDict, _redact(event_dict))


def add_service_context(
    _logger: structlog.types.WrappedLogger,
    _method_name: str,
    event_dict: structlog.types.EventDict,
    *,
    settings: Settings,
    process_role: str,
) -> structlog.types.EventDict:
    """为每条事件附加稳定的服务、环境与进程角色字段。"""
    event_dict["service"] = "service-data-sync"
    event_dict["environment"] = settings.environment
    event_dict["process_role"] = process_role
    return event_dict


def configure_logging(settings: Settings, *, process_role: str) -> None:
    """配置兼容标准错误输出且强制脱敏的结构化日志。"""
    logging.basicConfig(level=settings.log_level, format="%(message)s", force=True)
    renderer: structlog.types.Processor
    if settings.log_format is LogFormat.JSON:
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer()

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.EventRenamer("event"),
            # 回调必须先注入运行时上下文，再脱敏并完成最终渲染。
            lambda logger, method_name, event_dict: add_service_context(
                logger,
                method_name,
                event_dict,
                settings=settings,
                process_role=process_role,
            ),
            redact_secrets,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, settings.log_level)),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
