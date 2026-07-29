"""资金流来源探针和显式单分区 Celery 同步任务。"""

from __future__ import annotations

import asyncio

from celery import Celery

from service_data_sync.application.money_flow.sync import MoneyFlowSyncService
from service_data_sync.application.ports.data_source import ProviderError
from service_data_sync.bootstrap.container import build_source_registry
from service_data_sync.bootstrap.settings import Settings
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.object_storage.client import (
    ObjectStorageClient,
)
from service_data_sync.infrastructure.object_storage.raw_payload_store import (
    FailureEvidenceDataSource,
    S3RawPayloadStore,
    retain_failure_evidence,
)
from service_data_sync.infrastructure.persistence.money_flow_repository import (
    SqlAlchemyMoneyFlowRepository,
)
from service_data_sync.infrastructure.persistence.money_flow_run_ledger import (
    SqlAlchemyMoneyFlowRunLedger,
)

_PROBE_TASK = "service_data_sync.money_flow.probe"
_SYNC_TASK = "service_data_sync.money_flow.sync_partition"
_CAPABILITIES = frozenset(
    {
        "money_flow.order_size.daily.equity.raw",
        "money_flow.order_size.daily.sector.raw",
        "money_flow.order_size.daily.market.raw",
        "money_flow.order_size.ranking.equity.raw",
        "money_flow.order_size.ranking.sector.raw",
        "money_flow.trade_direction.ranking.equity.raw",
        "money_flow.trade_direction.ranking.industry.raw",
        "money_flow.trade_direction.ranking.concept.raw",
    }
)


def register_money_flow_tasks(app: Celery, *, settings: Settings) -> None:
    """注册无隐式 beat 的探针和显式分区任务，重复初始化保持幂等。"""
    if _PROBE_TASK not in app.tasks:

        @app.task(name=_PROBE_TASK, shared=False)
        def probe() -> dict[str, object]:
            """只检查配置后的 capability 声明，不触发供应商网络请求。"""
            if not settings.money_flow_enabled:
                return {
                    "status": "disabled",
                    "capabilityCount": 0,
                    "providerCount": 0,
                }
            registry = build_source_registry(settings)
            provider_ids = {
                provider.provider_id
                for capability in _CAPABILITIES
                for provider in registry.for_capability(capability)
            }
            capability_count = sum(
                bool(registry.for_capability(capability)) for capability in _CAPABILITIES
            )
            return {
                "status": (
                    "sync-ready"
                    if capability_count == len(_CAPABILITIES)
                    else "provider-adapter-unavailable"
                ),
                "capabilityCount": capability_count,
                "providerCount": len(provider_ids),
            }

    if _SYNC_TASK in app.tasks:
        return

    @app.task(name=_SYNC_TASK, shared=False)
    def sync_partition(
        capability: str,
        parameters: dict[str, str],
        mode: str = "scheduled",
    ) -> dict[str, object]:
        """同步一个完整显式参数分区；重试使用相同请求键恢复 checkpoint。"""
        if not settings.money_flow_enabled:
            raise RuntimeError("money-flow sync is disabled")
        if capability not in _CAPABILITIES:
            raise ValueError("unsupported money-flow capability")
        parameter_items = tuple(sorted(parameters.items()))
        registry = build_source_registry(settings)
        providers = registry.for_capability(capability)
        if len(providers) != 1:
            raise RuntimeError("exactly one money-flow provider must be enabled")
        database = DatabaseClient.from_settings(settings)
        object_storage = ObjectStorageClient.from_settings(settings)
        ledger = SqlAlchemyMoneyFlowRunLedger(database)
        run = ledger.start(
            capability=capability,
            parameters=parameter_items,
            mode=mode,
        )
        try:
            raw_payload_store = S3RawPayloadStore(object_storage)
            result = retain_failure_evidence(
                raw_payload_store,
                # 任务失败时才把来源响应写入 S3，成功时释放暂存字节。
                lambda: asyncio.run(
                    MoneyFlowSyncService(
                        source=FailureEvidenceDataSource(providers[0], raw_payload_store),
                        repository=SqlAlchemyMoneyFlowRepository(database),
                        raw_payload_store=raw_payload_store,
                    ).sync(
                        capability=capability,
                        parameters=parameter_items,
                        run_id=run.run_id,
                        partition_key=run.partition_key,
                    )
                ),
            )
            ledger.finish(run=run, result=result)
            return {
                "dataVersion": (
                    None
                    if result.publication.data_version is None
                    else str(result.publication.data_version)
                ),
                "published": result.publication.published,
                "qualityStatus": result.publication.quality_status,
                "rawUri": result.raw_uri,
            }
        except ProviderError as error:
            ledger.fail(
                run=run,
                error_code=f"provider-{error.code.value}",
                retryable=error.retryable,
            )
            raise
        except Exception:
            ledger.fail(
                run=run,
                error_code="money-flow-sync-failed",
                retryable=False,
            )
            raise
        finally:
            object_storage.close()
            database.close()


__all__ = ["register_money_flow_tasks"]
