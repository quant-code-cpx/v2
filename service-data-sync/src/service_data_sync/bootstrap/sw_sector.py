"""申万行业 capability 的独立组合工厂。

申万 taxonomy 与估值使用固定方法学和专属开关，本模块集中连接中立来源、canonical
仓储与失败证据包装器；重放路径另行使用拒绝网络的来源，保证修复不会新增外部观察。
"""

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
from service_data_sync.infrastructure.object_storage.raw_payload_store import (
    FailureEvidenceDataSource,
    S3RawPayloadStore,
)
from service_data_sync.infrastructure.persistence.sw_sector_repository import (
    SqlAlchemySwSectorRepository,
)
from service_data_sync.infrastructure.providers.akshare.sw_industry_snapshot import (
    AkshareSwIndustrySnapshotAdapter,
)


def build_sw_source(settings: Settings) -> DataSourcePort:
    """仅在 AKShare、板块和申万专属开关均开启时构造固定版本 adapter。

    三个开关分别表达通用来源许可、板块域许可和申万方法学许可；缺少任一项即拒绝，
    以免调用方把通用板块来源误当作已批准的申万 taxonomy 数据。
    """
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
    raw_payload_store: S3RawPayloadStore | None = None,
) -> SwSnapshotSyncService:
    """组合中立来源、canonical 仓储与单次失败证据暂存区。

    正常模式只让包装器在失败路径保留来源字节；replay 模式则安装拒绝网络的占位来源，
    迫使同步服务只读取既有 checkpoint 证据，避免“重放”意外变成再次抓取。
    """
    store = raw_payload_store or S3RawPayloadStore(object_storage)
    return SwSnapshotSyncService(
        source=(
            _ReplayOnlySource()
            if replay_only
            else FailureEvidenceDataSource(build_sw_source(settings), store)
        ),
        repository=SqlAlchemySwSectorRepository(database),
        raw_payload_store=store,
    )


class _ReplayOnlySource:
    """为不访问外网的 checkpoint replay 提供最小 provider-neutral 来源占位。

    同步服务仍依赖 `DataSourcePort`，因此使用此对象保留类型和组合方式；它故意不
    声明 capability，并在任何 `fetch` 调用时失败，形成重放模式的最后一道网络护栏。
    """

    provider_id = "replay-only"

    def capabilities(self) -> frozenset[str]:
        """返回空集合，明确 replay 不允许把任何来源能力视为可抓取。"""
        return frozenset()

    async def fetch(self, request: SourceRequest) -> ProviderBatch:
        """防御性拒绝任何意外 provider 调用，保住无网络重放承诺。"""
        raise ProviderError(
            ProviderErrorCode.INVALID_REQUEST,
            f"replay-only source cannot fetch {request.capability}",
            retryable=False,
        )
