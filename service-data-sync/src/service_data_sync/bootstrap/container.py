from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass

from service_data_sync.application.source_registry import SourceRegistry
from service_data_sync.bootstrap.settings import Settings
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.messaging.redis_client import RedisClient
from service_data_sync.infrastructure.object_storage.client import ObjectStorageClient


@dataclass
class ServiceContainer:
    settings: Settings
    database: DatabaseClient
    broker: RedisClient
    object_storage: ObjectStorageClient
    source_registry: SourceRegistry

    def close(self) -> None:
        for dependency in (self.object_storage, self.broker, self.database):
            with suppress(Exception):
                dependency.close()


def build_container(settings: Settings) -> ServiceContainer:
    return ServiceContainer(
        settings=settings,
        database=DatabaseClient.from_settings(settings),
        broker=RedisClient.from_settings(settings),
        object_storage=ObjectStorageClient.from_settings(settings),
        source_registry=SourceRegistry(),
    )
