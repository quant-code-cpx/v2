"""结构化日志配置与递归敏感信息脱敏。

所有进程通过本模块获得一致的服务上下文、时间格式和密钥保护；无论输出 JSON 还是本地
控制台文本，字段都会在最终渲染前经过同一套递归脱敏规则。
"""

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
    """按字段名保守判断日志值是否可能携带凭据或连接密钥。

    这是宁可多遮蔽的安全规则：业务字段偶尔被误判时只损失诊断细节；漏判 token
    或数据库连接串则会把不可撤回的秘密写入日志系统。
    """
    return any(marker in key.lower() for marker in _SENSITIVE_MARKERS)


def _redact(value: Any, key: str | None = None) -> Any:
    """递归遮蔽敏感值，同时保留事件的安全结构供检索和聚合。

    映射、列表和元组都可能嵌套第三方异常或请求参数，因此必须在最终渲染前遍历；
    不敏感字段维持原始类型，避免脱敏处理改变结构化日志消费者的解析契约。
    """
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
    """作为 `structlog` 处理器在渲染前递归执行密钥脱敏。"""
    return cast(structlog.types.EventDict, _redact(event_dict))


def add_service_context(
    _logger: structlog.types.WrappedLogger,
    _method_name: str,
    event_dict: structlog.types.EventDict,
    *,
    settings: Settings,
    process_role: str,
) -> structlog.types.EventDict:
    """为每条事件附加稳定服务名、环境和进程角色，方便跨进程关联排障。"""
    event_dict["service"] = "service-data-sync"
    event_dict["environment"] = settings.environment
    event_dict["process_role"] = process_role
    return event_dict


def configure_logging(settings: Settings, *, process_role: str) -> None:
    """配置输出到标准错误、带固定上下文且必经脱敏的结构化日志链。

    processor 顺序属于安全边界：先补足来源上下文，再递归清除敏感值，最后才序列化为
    JSON 或人类可读文本，确保两种格式都不能绕过脱敏。
    """
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
            # `structlog` 以位置参数调用处理器；先注入运行时上下文，再脱敏并最终渲染。
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
