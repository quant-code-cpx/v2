"""按配置组合基础设施客户端与获准数据源适配器。"""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass

from service_data_sync.application.ports.trading_calendar import TradingCalendarPort
from service_data_sync.application.source_registry import SourceRegistry
from service_data_sync.bootstrap.settings import Settings
from service_data_sync.infrastructure.calendar.sse_szse_a_share_2026 import (
    SseSzseAshare2026TradingCalendar,
)
from service_data_sync.infrastructure.calendar.unavailable_trading_calendar import (
    UnavailableTradingCalendar,
)
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.messaging.redis_client import RedisClient
from service_data_sync.infrastructure.object_storage.client import ObjectStorageClient
from service_data_sync.infrastructure.providers.akshare import (
    AkshareCnindexIndexSnapshotAdapter,
    AkshareCninfoCompanyProfileAdapter,
    AkshareCsindexIndexSnapshotAdapter,
    AkshareEastmoneyCorporateActionsAdapter,
    AkshareEastmoneyEquityCatalogAdapter,
    AkshareEastmoneyEquityPeriodBarsAdapter,
    AkshareEastmoneyFinancialAdapter,
    AkshareEastmoneyMoneyFlowAdapter,
    AkshareEastmoneySectorBarsAdapter,
    AkshareEastmoneySectorEodAdapter,
    AkshareEastmoneySectorMembershipAdapter,
    AkshareExchangeEquityLifecycleAdapter,
    AkshareP0MarketDataAdapter,
    AkshareSinaAdjustmentFactorsAdapter,
    AkshareTencentDailyBarsAdapter,
    AkshareThsMoneyFlowAdapter,
)


@dataclass
class ServiceContainer:
    """承载一个进程共享的基础设施依赖与获准来源注册表。

    容器只负责组装，不承载业务状态；CLI、HTTP 进程和 worker 在入口处创建一个
    容器，并在退出时统一关闭网络客户端。来源注册表保留的是 provider-neutral
    port，因此应用层不会直接依赖某个供应商 SDK。
    """

    settings: Settings
    database: DatabaseClient
    broker: RedisClient
    object_storage: ObjectStorageClient
    source_registry: SourceRegistry
    trading_calendar: TradingCalendarPort

    def close(self) -> None:
        """在进程退出时按反向使用顺序尽力关闭依赖。

        关闭操作是资源回收，不应遮蔽已经产生的业务异常；因此即使某个驱动已断连，
        其余连接仍会获得释放机会。
        """
        # 单个驱动关闭失败不能阻止其余连接释放，避免进程退出时遗留资源。
        for dependency in (self.object_storage, self.broker, self.database):
            with suppress(Exception):
                dependency.close()


def build_container(settings: Settings) -> ServiceContainer:
    """根据已校验策略创建进程所需客户端和获准 adapter。

    此处不探测外部数据源、不建表，也不启动后台任务；延迟这些副作用能让诊断、CLI
    和测试复用同一组装规则，同时在真正执行前保留清晰的失败边界。
    """
    registry = build_source_registry(settings)
    return ServiceContainer(
        settings=settings,
        database=DatabaseClient.from_settings(settings),
        broker=RedisClient.from_settings(settings),
        object_storage=ObjectStorageClient.from_settings(settings),
        source_registry=registry,
        trading_calendar=_trading_calendar_for_settings(settings),
    )


def build_source_registry(settings: Settings) -> SourceRegistry:
    """只按批准的能力开关组合来源 adapter，不创建其他基础设施客户端。

    注册与使用分离：这里仅声明某来源可被选择，具体任务仍须按 capability 精确挑选
    adapter。这样关闭某个实验性能力时，不会影响已批准的独立数据集。
    """
    registry = SourceRegistry()
    if settings.akshare_enabled:
        # P0 CLI 默认精确选择 `provider_id=akshare`；统一 adapter 避免同名 provider 注册歧义。
        registry.register(
            AkshareP0MarketDataAdapter(
                request_timeout_seconds=settings.akshare_request_timeout_seconds
            )
        )
        registry.register(
            AkshareTencentDailyBarsAdapter(
                request_timeout_seconds=settings.akshare_request_timeout_seconds
            )
        )
        registry.register(
            AkshareEastmoneyEquityCatalogAdapter(
                request_timeout_seconds=settings.akshare_request_timeout_seconds
            )
        )
        registry.register(
            AkshareExchangeEquityLifecycleAdapter(
                request_timeout_seconds=settings.akshare_request_timeout_seconds
            )
        )
        if settings.equity_market_enabled:
            registry.register(
                AkshareEastmoneyEquityPeriodBarsAdapter(
                    request_timeout_seconds=settings.akshare_request_timeout_seconds
                )
            )
            registry.register(
                AkshareSinaAdjustmentFactorsAdapter(
                    request_timeout_seconds=settings.akshare_request_timeout_seconds
                )
            )
            registry.register(
                AkshareEastmoneyCorporateActionsAdapter(
                    request_timeout_seconds=settings.akshare_request_timeout_seconds
                )
            )
            registry.register(
                AkshareCninfoCompanyProfileAdapter(
                    request_timeout_seconds=settings.akshare_request_timeout_seconds
                )
            )
        # 财务 source policy 必须精确指向东财 adapter，其他策略不允许静默复用该来源。
        if settings.financial_enabled and settings.financial_source_policy == "akshare-eastmoney":
            registry.register(
                AkshareEastmoneyFinancialAdapter(
                    request_timeout_seconds=(
                        settings.financial_request_timeout_seconds
                        or settings.akshare_request_timeout_seconds
                    )
                )
            )
        if settings.money_flow_enabled:
            registry.register(
                AkshareEastmoneyMoneyFlowAdapter(
                    request_timeout_seconds=settings.akshare_request_timeout_seconds
                )
            )
            registry.register(
                AkshareThsMoneyFlowAdapter(
                    request_timeout_seconds=settings.akshare_request_timeout_seconds
                )
            )
        if settings.index_enabled:
            if settings.index_source_policy in {"akshare-csindex", "akshare-csindex-cnindex"}:
                registry.register(
                    AkshareCsindexIndexSnapshotAdapter(
                        request_timeout_seconds=settings.akshare_request_timeout_seconds
                    )
                )
            if settings.index_source_policy in {"akshare-cnindex", "akshare-csindex-cnindex"}:
                registry.register(
                    AkshareCnindexIndexSnapshotAdapter(
                        request_timeout_seconds=settings.akshare_request_timeout_seconds
                    )
                )
        if settings.sector_enabled:
            registry.register(
                AkshareEastmoneySectorBarsAdapter(
                    request_timeout_seconds=settings.akshare_request_timeout_seconds
                )
            )
            if settings.sector_eod_enabled:
                registry.register(
                    AkshareEastmoneySectorEodAdapter(
                        request_timeout_seconds=settings.akshare_request_timeout_seconds
                    )
                )
            if settings.sector_membership_enabled:
                registry.register(
                    AkshareEastmoneySectorMembershipAdapter(
                        request_timeout_seconds=settings.akshare_request_timeout_seconds
                    )
                )
    return registry


def _trading_calendar_for_settings(settings: Settings) -> TradingCalendarPort:
    """按开关返回已发布交易日历或显式未知日历。

    EOD 横截面只能在已验证年份判断交易日；未启用时返回拒绝型实现，而不是猜测
    周末或节假日，从而防止在错误日期发布不完整快照。
    """
    if settings.trading_calendar_enabled:
        return SseSzseAshare2026TradingCalendar()
    return UnavailableTradingCalendar()
