"""板块 EOD shadow 调度、受控重试与 raw 优先恢复任务。"""

from __future__ import annotations

import asyncio
import random
from datetime import UTC, date, datetime
from time import perf_counter
from typing import Any

import structlog
from botocore.exceptions import BotoCoreError, ClientError
from celery import Celery
from sqlalchemy.exc import SQLAlchemyError

from service_data_sync.application.ports.data_source import ProviderError
from service_data_sync.application.ports.sector_eod import SectorEodExecutionMode
from service_data_sync.application.sector.eod_schedule import (
    sector_eod_scheduler_target_date,
    sector_eod_source_cutoff_at,
)
from service_data_sync.application.sector.eod_snapshot_sync import SectorEodSnapshotSyncService
from service_data_sync.bootstrap.container import build_container
from service_data_sync.bootstrap.settings import Settings
from service_data_sync.domain.sector import SectorScheme
from service_data_sync.infrastructure.object_storage.raw_payload_store import S3RawPayloadStore
from service_data_sync.infrastructure.persistence.sector_eod_repository import (
    SqlAlchemySectorEodRepository,
)

_CAPABILITY = "sector.quote.eod.snapshot.raw"
_DISPATCH_TASK = "service_data_sync.sector_eod.dispatch_shadow"
_RUN_TASK = "service_data_sync.sector_eod.run"
_REAP_TASK = "service_data_sync.sector_eod.reap"
_RETRY_DELAYS_SECONDS = (5 * 60, 15 * 60, 30 * 60)
_LOGGER = structlog.get_logger(__name__)


def register_sector_eod_tasks(app: Celery, *, settings: Settings) -> None:
    """向 worker 注册固定 EOD dispatch/run 任务；任务本身仍受全部运行开关与日历约束。"""
    if _DISPATCH_TASK in app.tasks or _RUN_TASK in app.tasks or _REAP_TASK in app.tasks:
        return

    @app.task(name=_DISPATCH_TASK, shared=False)
    def dispatch_shadow() -> dict[str, int | str]:
        """在 16:20 调度点读取权威日历，并为两个 scheme 投递同一明确交易日任务。"""
        if not settings.sector_eod_scheduler_enabled:
            return {"status": "disabled", "queued": 0}
        # 调度开关不能绕过来源准入；未获许可时不应产生后续无效消息。
        if not settings.sector_eod_enabled:
            return {"status": "source-policy-disabled", "queued": 0}
        container = build_container(settings)
        try:
            target_date = sector_eod_scheduler_target_date(datetime.now(UTC))
            is_open = container.trading_calendar.is_open(trade_date=target_date)
        finally:
            container.close()
        if is_open is not True:
            reason = "closed" if is_open is False else "calendar-unavailable"
            _LOGGER.warning(
                "sector_eod.schedule_skipped",
                reason=reason,
                trade_date=target_date.isoformat(),
            )
            return {"status": reason, "queued": 0}
        for scheme in SectorScheme:
            app.send_task(_RUN_TASK, args=(scheme.value, target_date.isoformat()))
        _LOGGER.info(
            "sector_eod.schedule_dispatched",
            trade_date=target_date.isoformat(),
            execution_mode=SectorEodExecutionMode.SHADOW.value,
            partition_count=len(SectorScheme),
        )
        return {"status": "queued", "queued": len(SectorScheme)}

    @app.task(name=_REAP_TASK, shared=False)
    def reap_expired_leases() -> dict[str, int | str]:
        """回收崩溃 worker 的 lease，并重投仍 queued 的明确分区；不直接访问 provider。"""
        if not settings.sector_eod_scheduler_enabled:
            return {"status": "disabled", "requeued": 0}
        # 暂停来源准入时保留历史运行记录，但不重投会触发来源访问的任务。
        if not settings.sector_eod_enabled:
            return {"status": "source-policy-disabled", "requeued": 0}
        container = build_container(settings)
        try:
            repository = SqlAlchemySectorEodRepository(container.database)
            requeued = repository.requeue_expired_leases(now=datetime.now(UTC))
            queued_runs = repository.list_queued_runs()
        finally:
            container.close()
        for queued_run in queued_runs:
            app.send_task(
                _RUN_TASK,
                args=(queued_run.scheme.value, queued_run.trade_date.isoformat()),
            )
        _LOGGER.info(
            "sector_eod.reaper_completed",
            requeued_count=requeued,
            queued_partition_count=len(queued_runs),
        )
        return {"status": "requeued", "requeued": requeued}

    @app.task(
        bind=True,
        name=_RUN_TASK,
        shared=False,
        max_retries=len(_RETRY_DELAYS_SECONDS),
        acks_late=True,
        reject_on_worker_lost=True,
    )
    def run_sector_eod(
        task: Any, scheme_value: str, trade_date_value: str
    ) -> dict[str, str | bool]:
        """执行或恢复一个明确分区；临时来源与基础设施失败按 5/15/30 分钟退避。"""
        if not settings.sector_eod_enabled:
            return {"status": "source-policy-disabled", "replayed": False}
        scheme = SectorScheme(scheme_value)
        trade_date = datetime.fromisoformat(f"{trade_date_value}T00:00:00+00:00").date()
        started_at = perf_counter()
        attempt = int(task.request.retries) + 1
        container = build_container(settings)
        try:
            providers = container.source_registry.for_capability(_CAPABILITY)
            if len(providers) != 1:
                raise RuntimeError("exactly one approved sector-eod provider must be enabled")
            repository = SqlAlchemySectorEodRepository(container.database)
            service = SectorEodSnapshotSyncService(
                source=providers[0],
                repository=repository,
                raw_payload_store=S3RawPayloadStore(container.object_storage),
                trading_calendar=container.trading_calendar,
            )
            replayed = repository.has_archived_observation(
                scheme=scheme,
                trade_date=trade_date,
            )
            execution_mode = (
                SectorEodExecutionMode.PUBLISH
                if settings.sector_eod_publish_enabled
                else SectorEodExecutionMode.SHADOW
            )
            _LOGGER.info(
                "sector_eod.run_started",
                partition_key=_partition_key(scheme=scheme, trade_date=trade_date),
                scheme=scheme.value,
                trade_date=trade_date.isoformat(),
                attempt=attempt,
                execution_mode=execution_mode.value,
                replayed=replayed,
            )
            operation = service.replay if replayed else service.sync
            result = asyncio.run(
                operation(
                    scheme=scheme,
                    trade_date=trade_date,
                    source_cutoff_at=sector_eod_source_cutoff_at(trade_date),
                    execution_mode=execution_mode,
                )
            )
            _LOGGER.info(
                "sector_eod.run_completed",
                run_id=None if result.run_id is None else str(result.run_id),
                snapshot_id=str(result.snapshot.snapshot_id),
                data_version=str(result.snapshot.data_version),
                partition_key=_partition_key(scheme=scheme, trade_date=trade_date),
                scheme=scheme.value,
                trade_date=trade_date.isoformat(),
                attempt=attempt,
                execution_mode=result.execution_mode.value,
                quality_status=result.snapshot.quality_status,
                duration_ms=_duration_ms(started_at),
                replayed=replayed,
            )
            return {
                "status": "completed",
                "replayed": replayed,
                "dataVersion": str(result.snapshot.data_version),
                "executionMode": result.execution_mode.value,
            }
        except ProviderError as error:
            if error.retryable:
                countdown = _retry_countdown(task)
                _log_retry(
                    error=error,
                    scheme=scheme,
                    trade_date=trade_date,
                    attempt=attempt,
                    countdown=countdown,
                    started_at=started_at,
                )
                raise task.retry(exc=error, countdown=countdown) from error
            _log_failure(
                error=error,
                scheme=scheme,
                trade_date=trade_date,
                attempt=attempt,
                started_at=started_at,
            )
            raise
        except (BotoCoreError, ClientError, OSError, SQLAlchemyError) as error:
            countdown = _retry_countdown(task)
            _log_retry(
                error=error,
                scheme=scheme,
                trade_date=trade_date,
                attempt=attempt,
                countdown=countdown,
                started_at=started_at,
            )
            raise task.retry(exc=error, countdown=countdown) from error
        except Exception as error:
            _log_failure(
                error=error,
                scheme=scheme,
                trade_date=trade_date,
                attempt=attempt,
                started_at=started_at,
            )
            raise
        finally:
            container.close()


def _retry_countdown(task: Any) -> int:
    """按当前重试次数选择冻结退避档位并加入小抖动，避免两个 scheme 同时重压来源。"""
    retries = int(task.request.retries)
    delay = _RETRY_DELAYS_SECONDS[min(retries, len(_RETRY_DELAYS_SECONDS) - 1)]
    return delay + random.randint(0, 30)


def _partition_key(*, scheme: SectorScheme, trade_date: date) -> str:
    """生成稳定分区标识供日志关联；它不进入 metrics label，避免产生高基数时序。"""
    return f"{scheme.value}:{trade_date.isoformat()}"


def _duration_ms(started_at: float) -> int:
    """以单调时钟记录本进程任务耗时，避免系统校时影响探针分析。"""
    return round((perf_counter() - started_at) * 1000)


def _log_retry(
    *,
    error: Exception,
    scheme: SectorScheme,
    trade_date: date,
    attempt: int,
    countdown: int,
    started_at: float,
) -> None:
    """记录可重试失败的稳定错误分类与退避，供 shadow 探针聚合失败率。"""
    _LOGGER.warning(
        "sector_eod.run_retry_scheduled",
        error_code=_error_code(error),
        partition_key=_partition_key(scheme=scheme, trade_date=trade_date),
        scheme=scheme.value,
        trade_date=trade_date.isoformat(),
        attempt=attempt,
        retry_in_seconds=countdown,
        duration_ms=_duration_ms(started_at),
    )


def _log_failure(
    *,
    error: Exception,
    scheme: SectorScheme,
    trade_date: date,
    attempt: int,
    started_at: float,
) -> None:
    """记录终态失败但不输出异常消息、原始载荷或配置，防止日志泄露来源细节。"""
    _LOGGER.error(
        "sector_eod.run_failed",
        error_code=_error_code(error),
        partition_key=_partition_key(scheme=scheme, trade_date=trade_date),
        scheme=scheme.value,
        trade_date=trade_date.isoformat(),
        attempt=attempt,
        duration_ms=_duration_ms(started_at),
    )


def _error_code(error: Exception) -> str:
    """将异常归类为低基数错误码，避免异常文本进入日志索引或 metrics 维度。"""
    if isinstance(error, ProviderError):
        return error.code.value
    if isinstance(error, (BotoCoreError, ClientError)):
        return "object-storage"
    if isinstance(error, SQLAlchemyError):
        return "persistence"
    if isinstance(error, OSError):
        return "network"
    return "unexpected"
