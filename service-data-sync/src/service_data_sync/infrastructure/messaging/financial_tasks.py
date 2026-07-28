"""财务来源探针和受控单证券同步任务；本模块不直接调用 provider 或数据库。"""

from __future__ import annotations

import asyncio

import structlog
from celery import Celery

from service_data_sync.application.financial.sync import FinancialSyncService
from service_data_sync.application.source_registry import SourceRegistry
from service_data_sync.bootstrap.container import build_source_registry
from service_data_sync.bootstrap.settings import Settings
from service_data_sync.domain.equity import Exchange
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.object_storage.client import ObjectStorageClient
from service_data_sync.infrastructure.object_storage.raw_payload_store import S3RawPayloadStore
from service_data_sync.infrastructure.persistence.financial_sync_repository import (
    SqlAlchemyFinancialSyncRepository,
)

_PROBE_TASK = "service_data_sync.financial.probe"
_SYNC_TASK = "service_data_sync.financial.sync_security"
_REQUIRED_CAPABILITIES = frozenset(
    {
        "financial.statement.raw",
        "financial.metric.raw",
        "financial.valuation.raw",
    }
)
_LOGGER = structlog.get_logger(__name__)


def register_financial_tasks(app: Celery, *, settings: Settings) -> None:
    """注册无 beat schedule 的探针和单证券同步任务；重复初始化保持幂等。"""
    if _PROBE_TASK not in app.tasks:

        @app.task(name=_PROBE_TASK, shared=False)
        def probe() -> dict[str, str | int]:
            """报告来源和同步代码是否可用；探针只读取 adapter 声明能力，绝不调用 `fetch`。"""
            if not settings.financial_enabled:
                return _probe_result(
                    status="disabled",
                    capability_count=0,
                    provider_count=0,
                    settings=settings,
                )

            registry = build_source_registry(settings)
            capability_count, provider_count = _financial_adapter_summary(registry)
            status = (
                "sync-ready"
                if capability_count == len(_REQUIRED_CAPABILITIES)
                else "provider-adapter-unavailable"
            )
            return _probe_result(
                status=status,
                capability_count=capability_count,
                provider_count=provider_count,
                settings=settings,
            )

    if _SYNC_TASK in app.tasks:
        return

    @app.task(name=_SYNC_TASK, shared=False)
    def sync_security(exchange: str, symbol: str) -> dict[str, str | int]:
        """同步一个明确证券；没有 beat 触发器，调用方必须显式提供交易所和六位代码。"""
        if not settings.financial_enabled:
            raise RuntimeError("financial sync is disabled")
        registry = build_source_registry(settings)
        providers = registry.for_capability("financial.statement.raw")
        if len(providers) != 1:
            raise RuntimeError("exactly one financial provider must be enabled")
        database = DatabaseClient.from_settings(settings)
        object_storage = ObjectStorageClient.from_settings(settings)
        try:
            result = asyncio.run(
                FinancialSyncService(
                    source=providers[0],
                    repository=SqlAlchemyFinancialSyncRepository(database),
                    raw_payload_store=S3RawPayloadStore(object_storage),
                ).sync_security(exchange=Exchange(exchange), symbol=symbol)
            )
        finally:
            object_storage.close()
            database.close()
        return {
            "reportInserted": result.reports.inserted_count,
            "metricInserted": result.provider_metrics.inserted_count,
            "valuationInserted": result.valuations.inserted_count,
        }


def _financial_adapter_summary(registry: SourceRegistry) -> tuple[int, int]:
    """统计已声明的财务能力与 adapter 数；不在 probe 中决定来源组合或数据合并。"""
    provider_ids: set[str] = set()
    available_capability_count = 0
    for capability in _REQUIRED_CAPABILITIES:
        capability_providers = {
            provider.provider_id for provider in registry.for_capability(capability)
        }
        if capability_providers:
            available_capability_count += 1
        provider_ids.update(capability_providers)
    return available_capability_count, len(provider_ids)


def _probe_result(
    *, status: str, capability_count: int, provider_count: int, settings: Settings
) -> dict[str, str | int]:
    """记录低基数准入结论，并返回可由受控运维任务读取的最小摘要。"""
    _LOGGER.info(
        "financial.dark_launch_probe_completed",
        status=status,
        source_policy=settings.financial_source_policy,
        capability_count=capability_count,
        provider_count=provider_count,
    )
    return {
        "status": status,
        "capabilityCount": capability_count,
        "providerCount": provider_count,
    }
