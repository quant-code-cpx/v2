"""个股行情与参考数据的受控单证券任务和全市场分发器。

分发器只从已确认证券目录拆出独立消息，不访问行情上游；工作任务才按 `capability`
选择唯一获准适配器，并把抓取、解码或发布失败的证据留存。日、周、月线分别是
独立能力与物理发布路径，复权因子、公司行动和公司概况也不能互相补值。
"""

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
from service_data_sync.infrastructure.object_storage.raw_payload_store import (
    FailureEvidenceDataSource,
    S3RawPayloadStore,
    retain_failure_evidence,
)
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
    """注册调度分发与单证券任务；重复创建 worker 时保持幂等。

    每个同步消息携带完整证券、能力和日期窗口，使重试不依赖“当前默认日期”。
    """
    if _DISPATCH_TASK not in app.tasks:

        @app.task(name=_DISPATCH_TASK, shared=False)
        def dispatch(capability: str) -> dict[str, int | str]:
            """把一个能力分发为已确认证券的独立消息，不在分发器内访问上游。

            目录中的 ``PENDING`` 身份尚未被主数据确认，不能把它们的代码交给 provider
            并写回可能属于另一只历史证券的行情。
            """
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
                    # 身份未确认时宁可稍后分发，也不能以代码复用风险换取更高覆盖率。
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
            """同步一个明确证券和周期窗口，三个周期使用各自数据源 `capability`。

            ``acks_late`` 允许工作进程中断后重投相同窗口；发布仓储据此把内容未变化的
            重试计为 `unchanged`，而不是生成新的 `revision`。
            """
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
        """同步一个证券的因子、公司行动或概况。

        三类参考数据有不同修订与空值语义，按 `capability` 单独执行，绝不在任务层融合。
        """
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
    """执行一次单证券周期行情同步，并确保外部客户端在失败前关闭。

    每个周期必须精确匹配自己的 `capability`；周月数据不从日线聚合，以保留供应商原生
    K 线口径和后续修订。
    """
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
            # 日线与周/月线的仓储和质量规则不同，选择对应应用服务而非共用转换分支。
            result = retain_failure_evidence(
                raw_store,
                # 任务失败时才把来源响应写入 S3，成功时释放暂存字节。
                lambda: asyncio.run(
                    EquityDailyBarSyncService(
                        source=FailureEvidenceDataSource(providers[0], raw_store),
                        repository=repository,
                        raw_payload_store=raw_store,
                    ).sync(identifier=identifier, start=start_date, end=end_date)
                ),
            )
        else:
            result = retain_failure_evidence(
                raw_store,
                # 任务失败时才把来源响应写入 S3，成功时释放暂存字节。
                lambda: asyncio.run(
                    EquityPeriodBarSyncService(
                        source=FailureEvidenceDataSource(providers[0], raw_store),
                        repository=repository,
                        raw_payload_store=raw_store,
                    ).sync(
                        identifier=identifier,
                        period=selected_period,
                        start=start_date,
                        end=end_date,
                    )
                ),
            )
    finally:
        object_storage.close()
        database.close()
    return {
        "capability": selected_period.capability,
        "inserted": result.inserted_count,
        "unchanged": result.unchanged_count,
        "availability": result.availability,
    }


def _sync_reference_once(
    *,
    settings: Settings,
    instrument: str,
    capability: str,
    start: str | None,
    end: str | None,
) -> dict[str, int | str]:
    """执行一次单证券参考数据同步，并按 capability 选择独立用例。

    只有一个数据源声明此 `capability` 时才可执行；任务层不能在多个来源之间任选其一。
    """
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
            # 累计因子会影响后续复权序列，应用服务需要完整指定窗口而非默认最近几天。
            result = retain_failure_evidence(
                raw_store,
                # 任务失败时才把来源响应写入 S3，成功时释放暂存字节。
                lambda: asyncio.run(
                    EquityAdjustmentFactorSyncService(
                        source=FailureEvidenceDataSource(providers[0], raw_store),
                        repository=repository,
                        raw_payload_store=raw_store,
                    ).sync(
                        identifier=identifier,
                        start=_required_date(start),
                        end=_required_date(end),
                    )
                ),
            )
        elif capability == "equity.corporate_action":
            result = retain_failure_evidence(
                raw_store,
                # 任务失败时才把来源响应写入 S3，成功时释放暂存字节。
                lambda: asyncio.run(
                    EquityCorporateActionSyncService(
                        source=FailureEvidenceDataSource(providers[0], raw_store),
                        repository=repository,
                        raw_payload_store=raw_store,
                    ).sync(
                        identifier=identifier,
                        start=_required_date(start),
                        end=_required_date(end),
                    )
                ),
            )
        else:
            result = retain_failure_evidence(
                raw_store,
                # 任务失败时才把来源响应写入 S3，成功时释放暂存字节。
                lambda: asyncio.run(
                    EquityCompanyProfileSyncService(
                        source=FailureEvidenceDataSource(providers[0], raw_store),
                        repository=repository,
                        raw_payload_store=raw_store,
                    ).sync(identifier=identifier)
                ),
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
    """仅重试适配器明确标记的瞬时失败，并使用有上限的指数全抖动。

    全抖动从零到当前上界随机选择，避免大量证券在同一指数退避时刻再次触发限流。
    """
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
    """按周期返回包含端滚动修订窗口。

    窗口覆盖供应商常见的迟到修订但保持有限；`canonical` 仓储再以内容哈希识别真实变化。
    """
    lookback_days = {
        EquityBarPeriod.DAY_1: 14,
        EquityBarPeriod.WEEK_1: 90,
        EquityBarPeriod.MONTH_1: 400,
    }[period]
    return today - timedelta(days=lookback_days), today


def _reference_window(capability: str, *, today: date) -> tuple[date | None, date | None]:
    """因子每次比较完整序列，公司行动滚动三年，概况无需日期。

    累计因子的早期修订会影响之后所有复权计算，故不能仅抓取最近窗口；概况则是当前
    状态快照，不接受虚构日期参数。
    """
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
