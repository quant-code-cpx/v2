"""个股行情与参考数据的受控单证券任务和全市场分发器。

分发器只从已确认证券目录拆出独立消息，不访问行情上游；工作任务才按 `capability`
选择唯一获准适配器，并把抓取、解码或发布失败的证据留存。日、周、月线分别是
独立能力与物理发布路径，复权因子、公司行动和公司概况也不能互相补值。
"""

from __future__ import annotations

from datetime import date, timedelta
from zoneinfo import ZoneInfo

from celery import Celery, Task

from service_data_sync.bootstrap.container import build_container
from service_data_sync.bootstrap.settings import Settings
from service_data_sync.domain.equity import EquityBarPeriod, EquityIdentifier
from service_data_sync.infrastructure.data_operations.control_plane import (
    DataOperationsControlPlane,
    build_catalog,
)
from service_data_sync.infrastructure.data_operations.legacy_submission import submit_system_command
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
            rate_limit=_PROVIDER_RATE_LIMIT,
            acks_late=True,
        )
        def sync_bar(
            task: Task,
            instrument: str,
            period: str,
            start: str,
            end: str,
        ) -> dict[str, object]:
            """把单证券周期任务转换为 command；Celery 重投不会直接调用 canonical 用例。"""
            del task
            return _sync_bar_once(
                settings=settings,
                instrument=instrument,
                period=period,
                start=start,
                end=end,
            )

    if _REFERENCE_TASK in app.tasks:
        return

    @app.task(
        bind=True,
        name=_REFERENCE_TASK,
        shared=False,
        rate_limit=_PROVIDER_RATE_LIMIT,
        acks_late=True,
    )
    def sync_reference(
        task: Task,
        instrument: str,
        capability: str,
        start: str | None,
        end: str | None,
    ) -> dict[str, object]:
        """把参考数据任务转换为 command；真正 retry 由 control-plane run 管理。"""
        del task
        return _sync_reference_once(
            settings=settings,
            instrument=instrument,
            capability=capability,
            start=start,
            end=end,
        )


def _sync_bar_once(
    *,
    settings: Settings,
    instrument: str,
    period: str,
    start: str,
    end: str,
) -> dict[str, object]:
    """把单证券周期参数映射为严格 INSTRUMENT selector 并提交 command。"""
    if not settings.equity_market_enabled:
        raise RuntimeError("equity market sync is disabled")
    selected_period = EquityBarPeriod(period)
    identifier = EquityIdentifier.parse(instrument)
    container = build_container(settings)
    try:
        control_plane = DataOperationsControlPlane(
            database=container.database,
            catalog=build_catalog(settings, container.source_registry),
            source_registry=container.source_registry,
            trading_calendar=container.trading_calendar,
        )
        return submit_system_command(
            control_plane,
            target={
                "datasetCode": selected_period.capability,
                "mode": "DATE_RANGE",
                "selector": {
                    "kind": "INSTRUMENT",
                    "exchange": identifier.exchange.value,
                    "symbol": identifier.symbol,
                },
                "dateFrom": date.fromisoformat(start).isoformat(),
                "dateTo": date.fromisoformat(end).isoformat(),
                "observationDate": None,
            },
            reason="兼容个股行情 Celery 提交",
            request_prefix="legacy-equity-market-task",
        )
    finally:
        container.close()


def _sync_reference_once(
    *,
    settings: Settings,
    instrument: str,
    capability: str,
    start: str | None,
    end: str | None,
) -> dict[str, object]:
    """把参考数据参数映射为严格 INSTRUMENT selector 并提交 command。"""
    if not settings.equity_market_enabled:
        raise RuntimeError("equity market sync is disabled")
    if capability not in _REFERENCE_CAPABILITIES:
        raise ValueError("unsupported equity reference capability")
    identifier = EquityIdentifier.parse(instrument)
    container = build_container(settings)
    try:
        control_plane = DataOperationsControlPlane(
            database=container.database,
            catalog=build_catalog(settings, container.source_registry),
            source_registry=container.source_registry,
            trading_calendar=container.trading_calendar,
        )
        is_profile = capability == "equity.profile"
        return submit_system_command(
            control_plane,
            target={
                "datasetCode": capability,
                "mode": "INCREMENTAL" if is_profile else "DATE_RANGE",
                "selector": {
                    "kind": "INSTRUMENT",
                    "exchange": identifier.exchange.value,
                    "symbol": identifier.symbol,
                },
                "dateFrom": None if is_profile else _required_date(start).isoformat(),
                "dateTo": None if is_profile else _required_date(end).isoformat(),
                "observationDate": None,
            },
            reason="兼容个股参考数据 Celery 提交",
            request_prefix="legacy-equity-reference-task",
        )
    finally:
        container.close()


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
