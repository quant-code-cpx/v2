"""个股行情与参考数据的受控单证券任务和全市场调度分发器。"""

from __future__ import annotations

import asyncio
import random
from datetime import date, timedelta
from typing import NoReturn
from zoneinfo import ZoneInfo

from celery import Celery, Task

from service_data_sync.application.equity.daily_bar_sync import EquityDailyBarSyncService
from service_data_sync.application.equity.market_extension_sync import (
    EquityAdjustmentFactorSyncService,
    EquityCompanyProfileSyncService,
    EquityCorporateActionSyncService,
    EquityPeriodBarSyncService,
)
from service_data_sync.application.ports.data_source import ProviderError
from service_data_sync.bootstrap.container import build_container, build_source_registry
from service_data_sync.bootstrap.settings import Settings
from service_data_sync.domain.equity import EquityBarPeriod, EquityIdentifier
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.object_storage.client import ObjectStorageClient
from service_data_sync.infrastructure.object_storage.raw_payload_store import S3RawPayloadStore
from service_data_sync.infrastructure.persistence.equity_market_data_repository import (
    SqlAlchemyEquityMarketDataRepository,
)

_DISPATCH_TASK = "service_data_sync.equity_market.dispatch"
_BAR_TASK = "service_data_sync.equity_market.sync_bar"
_REFERENCE_TASK = "service_data_sync.equity_market.sync_reference"
_REFERENCE_CAPABILITIES = frozenset(
    {"equity.adjustment_factor", "equity.corporate_action", "equity.profile"}
)
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_HISTORY_START = date(1990, 12, 19)
_PROVIDER_MAX_RETRIES = 3
_PROVIDER_MAX_BACKOFF_SECONDS = 60
_PROVIDER_RATE_LIMIT = "30/m"


def register_equity_market_tasks(app: Celery, *, settings: Settings) -> None:
    """注册调度分发与单证券任务；重复创建 worker 时保持幂等。"""
    if _DISPATCH_TASK not in app.tasks:

        @app.task(name=_DISPATCH_TASK, shared=False)
        def dispatch(capability: str) -> dict[str, int | str]:
            """把一个能力分发为已确认证券的独立消息，不在分发器内访问上游。"""
            if not settings.equity_market_enabled:
                return {"status": "disabled", "dispatched": 0}
            period = _period_for_capability(capability)
            if period is None and capability not in _REFERENCE_CAPABILITIES:
                raise ValueError("unsupported equity market capability")
            container = build_container(settings)
            try:
                instruments = SqlAlchemyEquityMarketDataRepository(
                    container.database
                ).list_instruments(query=None, limit=100_000)
            finally:
                container.close()
            today = date.today()
            dispatched = 0
            for instrument in instruments:
                if instrument.listing_status == "PENDING":
                    continue
                if period is not None:
                    start, end = _bar_window(period, today=today)
                    app.send_task(
                        _BAR_TASK,
                        args=(
                            instrument.identifier.qualified_symbol,
                            period.value,
                            start.isoformat(),
                            end.isoformat(),
                        ),
                        queue="equity-market",
                    )
                else:
                    start, end = _reference_window(capability, today=today)
                    app.send_task(
                        _REFERENCE_TASK,
                        args=(
                            instrument.identifier.qualified_symbol,
                            capability,
                            None if start is None else start.isoformat(),
                            None if end is None else end.isoformat(),
                        ),
                        queue="equity-reference",
                    )
                dispatched += 1
            return {"status": "dispatched", "dispatched": dispatched}

    if _BAR_TASK not in app.tasks:

        @app.task(
            bind=True,
            name=_BAR_TASK,
            shared=False,
            max_retries=_PROVIDER_MAX_RETRIES,
            rate_limit=_PROVIDER_RATE_LIMIT,
            acks_late=True,
        )
        def sync_bar(
            task: Task,
            instrument: str,
            period: str,
            start: str,
            end: str,
        ) -> dict[str, int | str]:
            """同步一个明确证券和周期窗口，三个周期使用各自 provider capability。"""
            try:
                return _sync_bar_once(
                    settings=settings,
                    instrument=instrument,
                    period=period,
                    start=start,
                    end=end,
                )
            except ProviderError as error:
                _retry_provider_error(task, error)

    if _REFERENCE_TASK in app.tasks:
        return

    @app.task(
        bind=True,
        name=_REFERENCE_TASK,
        shared=False,
        max_retries=_PROVIDER_MAX_RETRIES,
        rate_limit=_PROVIDER_RATE_LIMIT,
        acks_late=True,
    )
    def sync_reference(
        task: Task,
        instrument: str,
        capability: str,
        start: str | None,
        end: str | None,
    ) -> dict[str, int | str]:
        """同步一个证券的因子、公司行动或概况。"""
        try:
            return _sync_reference_once(
                settings=settings,
                instrument=instrument,
                capability=capability,
                start=start,
                end=end,
            )
        except ProviderError as error:
            _retry_provider_error(task, error)


def _sync_bar_once(
    *,
    settings: Settings,
    instrument: str,
    period: str,
    start: str,
    end: str,
) -> dict[str, int | str]:
    """执行一次单证券周期行情同步，并确保外部客户端在失败前关闭。"""
    if not settings.equity_market_enabled:
        raise RuntimeError("equity market sync is disabled")
    selected_period = EquityBarPeriod(period)
    identifier = EquityIdentifier.parse(instrument)
    start_date = date.fromisoformat(start)
    end_date = date.fromisoformat(end)
    registry = build_source_registry(settings)
    providers = registry.for_capability(selected_period.capability)
    if len(providers) != 1:
        raise RuntimeError("exactly one equity bar provider must be enabled")
    database = DatabaseClient.from_settings(settings)
    object_storage = ObjectStorageClient.from_settings(settings)
    try:
        repository = SqlAlchemyEquityMarketDataRepository(database)
        raw_store = S3RawPayloadStore(object_storage)
        if selected_period is EquityBarPeriod.DAY_1:
            result = asyncio.run(
                EquityDailyBarSyncService(
                    source=providers[0],
                    repository=repository,
                    raw_payload_store=raw_store,
                ).sync(identifier=identifier, start=start_date, end=end_date)
            )
        else:
            result = asyncio.run(
                EquityPeriodBarSyncService(
                    source=providers[0],
                    repository=repository,
                    raw_payload_store=raw_store,
                ).sync(
                    identifier=identifier,
                    period=selected_period,
                    start=start_date,
                    end=end_date,
                )
            )
    finally:
        object_storage.close()
        database.close()
    return {
        "capability": selected_period.capability,
        "inserted": result.inserted_count,
        "unchanged": result.unchanged_count,
    }


def _sync_reference_once(
    *,
    settings: Settings,
    instrument: str,
    capability: str,
    start: str | None,
    end: str | None,
) -> dict[str, int | str]:
    """执行一次单证券参考数据同步，并按 capability 选择独立用例。"""
    if not settings.equity_market_enabled:
        raise RuntimeError("equity market sync is disabled")
    if capability not in _REFERENCE_CAPABILITIES:
        raise ValueError("unsupported equity reference capability")
    identifier = EquityIdentifier.parse(instrument)
    registry = build_source_registry(settings)
    providers = registry.for_capability(capability)
    if len(providers) != 1:
        raise RuntimeError("exactly one equity reference provider must be enabled")
    database = DatabaseClient.from_settings(settings)
    object_storage = ObjectStorageClient.from_settings(settings)
    try:
        repository = SqlAlchemyEquityMarketDataRepository(database)
        raw_store = S3RawPayloadStore(object_storage)
        if capability == "equity.adjustment_factor":
            result = asyncio.run(
                EquityAdjustmentFactorSyncService(
                    source=providers[0],
                    repository=repository,
                    raw_payload_store=raw_store,
                ).sync(
                    identifier=identifier,
                    start=_required_date(start),
                    end=_required_date(end),
                )
            )
        elif capability == "equity.corporate_action":
            result = asyncio.run(
                EquityCorporateActionSyncService(
                    source=providers[0],
                    repository=repository,
                    raw_payload_store=raw_store,
                ).sync(
                    identifier=identifier,
                    start=_required_date(start),
                    end=_required_date(end),
                )
            )
        else:
            result = asyncio.run(
                EquityCompanyProfileSyncService(
                    source=providers[0],
                    repository=repository,
                    raw_payload_store=raw_store,
                ).sync(identifier=identifier)
            )
    finally:
        object_storage.close()
        database.close()
    return {
        "capability": capability,
        "inserted": result.inserted_count,
        "unchanged": result.unchanged_count,
    }


def _retry_provider_error(task: Task, error: ProviderError) -> NoReturn:
    """仅重试适配器明确标记的瞬时失败，并使用有上限的指数全抖动。"""
    if not error.retryable:
        raise error
    retry_index = int(task.request.retries)
    ceiling = min(_PROVIDER_MAX_BACKOFF_SECONDS, 2**retry_index)
    countdown = random.randint(0, ceiling)
    raise task.retry(
        exc=error,
        countdown=countdown,
        max_retries=_PROVIDER_MAX_RETRIES,
    )


def _period_for_capability(capability: str) -> EquityBarPeriod | None:
    """把独立行情能力解析为周期，其余能力返回空。"""
    for period in EquityBarPeriod:
        if period.capability == capability:
            return period
    return None


def _bar_window(period: EquityBarPeriod, *, today: date) -> tuple[date, date]:
    """按周期返回包含端滚动修订窗口。"""
    lookback_days = {
        EquityBarPeriod.DAY_1: 14,
        EquityBarPeriod.WEEK_1: 90,
        EquityBarPeriod.MONTH_1: 400,
    }[period]
    return today - timedelta(days=lookback_days), today


def _reference_window(capability: str, *, today: date) -> tuple[date | None, date | None]:
    """因子每次比较完整序列，公司行动滚动三年，概况无需日期。"""
    if capability == "equity.adjustment_factor":
        return _HISTORY_START, today
    if capability == "equity.corporate_action":
        return today - timedelta(days=3 * 366), today
    return None, None


def _required_date(value: str | None) -> date:
    """解析任务必须携带的 ISO 日期。"""
    if value is None:
        raise ValueError("task date is required")
    return date.fromisoformat(value)
