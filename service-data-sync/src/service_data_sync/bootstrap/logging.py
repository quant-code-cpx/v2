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
    return any(marker in key.lower() for marker in _SENSITIVE_MARKERS)


def _redact(value: Any, key: str | None = None) -> Any:
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
    return cast(structlog.types.EventDict, _redact(event_dict))


def add_service_context(
    _logger: structlog.types.WrappedLogger,
    _method_name: str,
    event_dict: structlog.types.EventDict,
    *,
    settings: Settings,
    process_role: str,
) -> structlog.types.EventDict:
    event_dict["service"] = "service-data-sync"
    event_dict["environment"] = settings.environment
    event_dict["process_role"] = process_role
    return event_dict


def configure_logging(settings: Settings, *, process_role: str) -> None:
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
