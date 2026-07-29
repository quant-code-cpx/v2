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
    """承载单进程依赖，并负责在退出时按反向使用顺序关闭资源。"""

    settings: Settings
    database: DatabaseClient
    broker: RedisClient
    object_storage: ObjectStorageClient
    source_registry: SourceRegistry
    trading_calendar: TradingCalendarPort

    def close(self) -> None:
        """在进程退出时按反向使用顺序尽力关闭依赖。"""
        # 单个驱动关闭失败不能阻止其余连接释放，避免进程退出时遗留资源。
        for dependency in (self.object_storage, self.broker, self.database):
            with suppress(Exception):
                dependency.close()


def build_container(settings: Settings) -> ServiceContainer:
    """根据策略配置组合基础设施客户端和获准适配器。"""
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
    """只按开关组合来源适配器，不创建数据库、消息或对象存储客户端。"""
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
    """仅在显式开关开启时提供已发布的年度日历，其余场景保持未知并阻止 EOD。"""
    if settings.trading_calendar_enabled:
        return SseSzseAshare2026TradingCalendar()
    return UnavailableTradingCalendar()
