"""按配置组合基础设施客户端与获准数据源适配器。"""

from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass

from service_data_sync.application.source_registry import SourceRegistry
from service_data_sync.bootstrap.settings import Settings
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.messaging.redis_client import RedisClient
from service_data_sync.infrastructure.object_storage.client import ObjectStorageClient
from service_data_sync.infrastructure.providers.akshare import (
    AkshareEastmoneyEquityCatalogAdapter,
    AkshareEastmoneySectorBarsAdapter,
    AkshareEastmoneySectorMembershipAdapter,
    AkshareTencentDailyBarsAdapter,
)


@dataclass
class ServiceContainer:
    """承载单进程依赖，并负责在退出时按反向使用顺序关闭资源。"""

    settings: Settings
    database: DatabaseClient
    broker: RedisClient
    object_storage: ObjectStorageClient
    source_registry: SourceRegistry

    def close(self) -> None:
        """在进程退出时按反向使用顺序尽力关闭依赖。"""
        # 单个驱动关闭失败不能阻止其余连接释放，避免进程退出时遗留资源。
        for dependency in (self.object_storage, self.broker, self.database):
            with suppress(Exception):
                dependency.close()


def build_container(settings: Settings) -> ServiceContainer:
    """根据策略配置组合基础设施客户端和获准适配器。"""
    registry = SourceRegistry()
    if settings.akshare_enabled:
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
        if settings.sector_enabled:
            registry.register(
                AkshareEastmoneySectorBarsAdapter(
                    request_timeout_seconds=settings.akshare_request_timeout_seconds
                )
            )
            if settings.sector_membership_enabled:
                registry.register(
                    AkshareEastmoneySectorMembershipAdapter(
                        request_timeout_seconds=settings.akshare_request_timeout_seconds
                    )
                )
    return ServiceContainer(
        settings=settings,
        database=DatabaseClient.from_settings(settings),
        broker=RedisClient.from_settings(settings),
        object_storage=ObjectStorageClient.from_settings(settings),
        source_registry=registry,
    )
