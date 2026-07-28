"""申万行业 capability 的独立组合工厂。"""

from __future__ import annotations

from service_data_sync.application.ports.data_source import (
    DataSourcePort,
    ProviderBatch,
    ProviderError,
    ProviderErrorCode,
    SourceRequest,
)
from service_data_sync.application.sector.sw_snapshot_sync import SwSnapshotSyncService
from service_data_sync.bootstrap.settings import Settings
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.object_storage.client import ObjectStorageClient
from service_data_sync.infrastructure.object_storage.raw_payload_store import S3RawPayloadStore
from service_data_sync.infrastructure.persistence.sw_sector_repository import (
    SqlAlchemySwSectorRepository,
)
from service_data_sync.infrastructure.providers.akshare.sw_industry_snapshot import (
    AkshareSwIndustrySnapshotAdapter,
)


def build_sw_source(settings: Settings) -> DataSourcePort:
    """仅在 AKShare、板块和申万专属开关均开启时构造固定版本 adapter。"""
    if (
        not settings.akshare_enabled
        or not settings.sector_enabled
        or not settings.sw_sector_enabled
    ):
        raise RuntimeError("SW sector source policy is disabled")
    return AkshareSwIndustrySnapshotAdapter(
        request_timeout_seconds=settings.akshare_request_timeout_seconds
    )


def build_sw_sync_service(
    settings: Settings,
    *,
    database: DatabaseClient,
    object_storage: ObjectStorageClient,
    replay_only: bool = False,
) -> SwSnapshotSyncService:
    """组合中立来源、canonical 仓储与私有 raw evidence 存储。"""
    return SwSnapshotSyncService(
        source=_ReplayOnlySource() if replay_only else build_sw_source(settings),
        repository=SqlAlchemySwSectorRepository(database),
        raw_payload_store=S3RawPayloadStore(object_storage),
    )


class _ReplayOnlySource:
    """为不访问外网的 checkpoint replay 提供最小中立来源占位。"""

    provider_id = "replay-only"

    def capabilities(self) -> frozenset[str]:
        """replay 不发起新抓取，因此不声明任何可调用来源能力。"""
        return frozenset()

    async def fetch(self, request: SourceRequest) -> ProviderBatch:
        """防御性拒绝任何意外 provider 调用。"""
        raise ProviderError(
            ProviderErrorCode.INVALID_REQUEST,
            f"replay-only source cannot fetch {request.capability}",
            retryable=False,
        )
