"""申万行业快照的探针、当天同步、重放任务与显式调度。

任务层只决定何时执行和如何报告低基数结果，不解析供应商字段或直接写数据库。当天
同步通过失败证据包装器保护排障信息；重放则只读取已成功检查点，不能借由历史日期
再次请求会变化的上游快照。
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from celery import Celery
from celery.schedules import crontab

from service_data_sync.application.ports.data_source import ProviderError
from service_data_sync.bootstrap.settings import Settings
from service_data_sync.bootstrap.sw_sector import build_sw_source, build_sw_sync_service
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.object_storage.client import ObjectStorageClient
from service_data_sync.infrastructure.object_storage.raw_payload_store import (
    S3RawPayloadStore,
    retain_failure_evidence,
)

_PROBE_TASK = "service_data_sync.sw_sector.probe"
_SYNC_TASK = "service_data_sync.sw_sector.sync_current"
_REPLAY_TASK = "service_data_sync.sw_sector.replay_snapshot"
_SHANGHAI = ZoneInfo("Asia/Shanghai")


def register_sw_sector_tasks(app: Celery, *, settings: Settings) -> None:
    """幂等注册探针、当天同步和指定日期重放，不修改其他任务配置。

    多个 worker 进程可重复调用本函数；已有同名任务时不替换其实现或调度设置。
    """
    if _PROBE_TASK not in app.tasks:

        @app.task(name=_PROBE_TASK, shared=False)
        def probe() -> dict[str, object]:
            """只检查开关和 adapter 声明能力，绝不访问外部来源。"""
            try:
                source = build_sw_source(settings)
            except RuntimeError:
                return {"status": "disabled", "capabilityCount": 0}
            return {
                "status": "sync-ready",
                "capabilityCount": len(source.capabilities()),
                "providerId": source.provider_id,
            }

    if _SYNC_TASK not in app.tasks:

        @app.task(name=_SYNC_TASK, bind=True, shared=False, max_retries=3)
        def sync_current(task: Any) -> dict[str, object]:
            """同步上海当天完整快照；仅对 adapter 标记的瞬时失败指数退避。"""
            try:
                return _run_sync(settings, snapshot_date=datetime.now(_SHANGHAI).date())
            except ProviderError as error:
                if not error.retryable:
                    raise
                # 数据结构和请求错误不能因重复调用变好；只有来源暂不可用才允许重试。
                raise task.retry(exc=error, countdown=2 ** (task.request.retries + 1)) from error

    if _REPLAY_TASK in app.tasks:
        return

    @app.task(name=_REPLAY_TASK, shared=False)
    def replay_snapshot(snapshot_date: str) -> dict[str, object]:
        """从精确日期 checkpoint 重放，不调用 provider 或跨日期补洞。"""
        parsed_date = date.fromisoformat(snapshot_date)
        database = DatabaseClient.from_settings(settings)
        object_storage = ObjectStorageClient.from_settings(settings)
        try:
            raw_payload_store = S3RawPayloadStore(object_storage)
            service = build_sw_sync_service(
                settings,
                database=database,
                object_storage=object_storage,
                replay_only=True,
                raw_payload_store=raw_payload_store,
            )
            result = retain_failure_evidence(
                raw_payload_store,
                # replay 不会新增来源字节；包装器统一释放单次执行资源。
                lambda: service.replay(snapshot_date=parsed_date),
            )
            return _task_result(
                result.publications.taxonomy.data_version,
                result.publications.valuation.data_version,
            )
        finally:
            object_storage.close()
            database.close()


def sw_sector_beat_schedule(*, settings: Settings) -> dict[str, dict[str, object]]:
    """在显式开关开启时返回上海时间 18:30 的单一发布调度项。

    函数只生成配置，不投递消息；因此部署时可先审阅调度表再启用实际同步。
    """
    if not settings.sw_sector_enabled:
        return {}
    return {
        "sw-sector-daily-sync": {
            "task": _SYNC_TASK,
            "schedule": crontab(hour=18, minute=30),
            "options": {"queue": "data-sync"},
        }
    }


def _run_sync(settings: Settings, *, snapshot_date: date) -> dict[str, object]:
    """为一个明确日期创建短生命周期基础设施并运行同步。"""
    database = DatabaseClient.from_settings(settings)
    object_storage = ObjectStorageClient.from_settings(settings)
    try:
        raw_payload_store = S3RawPayloadStore(object_storage)
        service = build_sw_sync_service(
            settings,
            database=database,
            object_storage=object_storage,
            raw_payload_store=raw_payload_store,
        )
        result = retain_failure_evidence(
            raw_payload_store,
            # 成功释放来源字节；同步或解码失败时才归档本次申万响应。
            lambda: asyncio.run(service.sync(snapshot_date=snapshot_date)),
        )
        return _task_result(
            result.publications.taxonomy.data_version,
            result.publications.valuation.data_version,
        )
    finally:
        object_storage.close()
        database.close()


def _task_result(taxonomy_version: object, valuation_version: object) -> dict[str, object]:
    """返回低基数任务摘要，避免日志携带 raw URI 或全量代码列表。"""
    return {
        "taxonomyDataVersion": str(taxonomy_version),
        "valuationDataVersion": str(valuation_version),
    }
