"""证券上市生命周期探针、同步与检查点恢复任务。"""

from __future__ import annotations

import asyncio
import random
from datetime import date
from typing import NoReturn

from celery import Celery, Task

from service_data_sync.application.equity.lifecycle_sync import (
    EquityLifecycleSyncResult,
    EquityLifecycleSyncService,
)
from service_data_sync.application.ports.data_source import ProviderError
from service_data_sync.bootstrap.container import build_container
from service_data_sync.bootstrap.settings import Settings
from service_data_sync.domain.equity import Exchange
from service_data_sync.infrastructure.object_storage.raw_payload_store import S3RawPayloadStore
from service_data_sync.infrastructure.persistence.equity_lifecycle_repository import (
    SqlAlchemyEquityLifecycleRepository,
)

_CAPABILITY = "equity.lifecycle.explicit"
_PROBE_TASK = "service_data_sync.equity_lifecycle.probe"
_SYNC_TASK = "service_data_sync.equity_lifecycle.sync_exchange"
_REPLAY_TASK = "service_data_sync.equity_lifecycle.replay_exchange"
_MAX_RETRIES = 3


def register_equity_lifecycle_tasks(app: Celery, *, settings: Settings) -> None:
    """注册无自动调度的生命周期任务；重复初始化 worker 时保持幂等。"""
    if _PROBE_TASK not in app.tasks:

        @app.task(name=_PROBE_TASK, shared=False)
        def probe() -> dict[str, str | int]:
            """只读取来源声明，报告 adapter 是否已接线而不触发外部请求。"""
            container = build_container(settings)
            try:
                providers = container.source_registry.for_capability(_CAPABILITY)
                return {
                    "status": "sync-ready" if len(providers) == 1 else "provider-unavailable",
                    "providerCount": len(providers),
                }
            finally:
                container.close()

    if _SYNC_TASK not in app.tasks:

        @app.task(
            bind=True,
            name=_SYNC_TASK,
            shared=False,
            max_retries=_MAX_RETRIES,
            acks_late=True,
        )
        def sync_exchange(task: Task, exchange: str, target_date: str) -> dict[str, str | int]:
            """同步一所显式生命周期全量历史；可重试错误遵循有限指数退避。"""
            try:
                return _run_sync(
                    settings=settings,
                    exchange=Exchange(exchange),
                    target_date=date.fromisoformat(target_date),
                )
            except ProviderError as error:
                _retry_provider_error(task, error)

    if _REPLAY_TASK in app.tasks:
        return

    @app.task(name=_REPLAY_TASK, shared=False, acks_late=True)
    def replay_exchange(exchange: str) -> dict[str, str | int]:
        """从一所最后成功检查点恢复，不访问 AKShare 或交易所。"""
        return _run_replay(settings=settings, exchange=Exchange(exchange))


def _run_sync(*, settings: Settings, exchange: Exchange, target_date: date) -> dict[str, str | int]:
    """构造短生命周期依赖并执行一次来源同步。"""
    container = build_container(settings)
    try:
        providers = container.source_registry.for_capability(_CAPABILITY)
        if len(providers) != 1:
            raise RuntimeError("exactly one equity lifecycle provider must be enabled")
        result = asyncio.run(
            EquityLifecycleSyncService(
                source=providers[0],
                repository=SqlAlchemyEquityLifecycleRepository(container.database),
                raw_payload_store=S3RawPayloadStore(container.object_storage),
            ).sync(exchange=exchange, target_date=target_date)
        )
        return _task_result(result)
    finally:
        container.close()


def _run_replay(*, settings: Settings, exchange: Exchange) -> dict[str, str | int]:
    """构造只需存储依赖的恢复服务并重放最后成功标准批次。"""
    container = build_container(settings)
    try:
        result = asyncio.run(
            EquityLifecycleSyncService(
                source=None,
                repository=SqlAlchemyEquityLifecycleRepository(container.database),
                raw_payload_store=S3RawPayloadStore(container.object_storage),
            ).replay_last(exchange=exchange)
        )
        return _task_result(result)
    finally:
        container.close()


def _task_result(result: EquityLifecycleSyncResult) -> dict[str, str | int]:
    """把应用结果裁剪为 Celery 序列化稳定的低基数摘要。"""
    return {
        "exchange": result.exchange.value,
        "snapshotId": str(result.snapshot_id),
        "dataVersion": str(result.data_version),
        "inserted": result.inserted_count,
        "unchanged": result.unchanged_count,
    }


def _retry_provider_error(task: Task, error: ProviderError) -> NoReturn:
    """只重试 adapter 标记为可重试的错误，并限制退避上界。"""
    if not error.retryable:
        raise error
    retries = int(task.request.retries)
    backoff = min(60, 2 ** (retries + 1)) + random.randint(0, 3)
    raise task.retry(exc=error, countdown=backoff)
