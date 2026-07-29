"""证券上市生命周期的探针、显式同步与检查点恢复 `Celery` 任务。

生命周期只能来自交易所的明确上市、退市或更正证据，任务不会从目录缺席推断状态。
同步任务使用晚确认与有限退避，数据库发布层负责重复投递后的幂等；恢复任务只重放
最后成功的标准批次，避免把历史修复变成新的外部抓取。
"""

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
from service_data_sync.infrastructure.object_storage.raw_payload_store import (
    FailureEvidenceDataSource,
    S3RawPayloadStore,
    retain_failure_evidence,
)
from service_data_sync.infrastructure.persistence.equity_lifecycle_repository import (
    SqlAlchemyEquityLifecycleRepository,
)

_CAPABILITY = "equity.lifecycle.explicit"
_PROBE_TASK = "service_data_sync.equity_lifecycle.probe"
_SYNC_TASK = "service_data_sync.equity_lifecycle.sync_exchange"
_REPLAY_TASK = "service_data_sync.equity_lifecycle.replay_exchange"
_MAX_RETRIES = 3


def register_equity_lifecycle_tasks(app: Celery, *, settings: Settings) -> None:
    """注册无自动调度的生命周期任务；重复初始化工作进程时保持幂等。

    交易所和目标日都由受控调用方显式传入，避免通用调度在错误日期或错误市场执行。
    """
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
            """同步一所显式生命周期全量历史；可重试错误遵循有限指数退避。

            ``acks_late`` 可能使工作进程丢失后的消息重新投递，仓储因而必须把相同来源
            证据识别为未变化，而不能依赖任务仅执行一次。
            """
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
    """构造短生命周期依赖并执行一次来源同步。

    同一 `capability` 必须恰好选择一个数据源；多个来源的生命周期证据不得在任务层
    临时合并，以免掩盖冲突或破坏可追溯的来源边界。
    """
    container = build_container(settings)
    try:
        providers = container.source_registry.for_capability(_CAPABILITY)
        if len(providers) != 1:
            raise RuntimeError("exactly one equity lifecycle provider must be enabled")
        raw_payload_store = S3RawPayloadStore(container.object_storage)
        result = retain_failure_evidence(
            raw_payload_store,
            # 任务失败时才把来源响应写入 S3，成功时释放暂存字节。
            lambda: asyncio.run(
                EquityLifecycleSyncService(
                    source=FailureEvidenceDataSource(providers[0], raw_payload_store),
                    repository=SqlAlchemyEquityLifecycleRepository(container.database),
                    raw_payload_store=raw_payload_store,
                ).sync(exchange=exchange, target_date=target_date)
            ),
        )
        return _task_result(result)
    finally:
        container.close()


def _run_replay(*, settings: Settings, exchange: Exchange) -> dict[str, str | int]:
    """构造只需存储依赖的恢复服务并重放最后成功标准批次。

    ``source=None`` 是刻意的保护：重放路径没有网络来源，误接入数据源会改变历史证据。
    """
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
    """只重试适配器标记为可重试的错误，并限制退避上界。

    少量随机抖动让并发交易所任务不会在同一秒再次压向受限上游。
    """
    if not error.retryable:
        raise error
    retries = int(task.request.retries)
    # 60 秒上界让故障可观察，避免指数退避把人工恢复延后到不可预期的时间。
    backoff = min(60, 2 ** (retries + 1)) + random.randint(0, 3)
    raise task.retry(exc=error, countdown=backoff)
