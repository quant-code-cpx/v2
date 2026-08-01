"""数据源无关的应用层端口集合。

端口描述应用服务需要的能力和输入输出。
它们不包含 `SDK`、`HTTP`、`SQL` 或对象存储细节；基础设施实现这些协议，组合根负责装配。
"""

from service_data_sync.application.ports.data_source import (
    DataSourcePort,
    ProviderBatch,
    ProviderError,
    ProviderErrorCode,
    SourceRequest,
)
from service_data_sync.application.ports.dataset_availability import (
    DatasetAvailability,
    DatasetAvailabilityRepository,
)
from service_data_sync.application.ports.index_shadow import IndexShadowRepository
from service_data_sync.application.ports.market_data import (
    EquityMarketDataRepository,
    RawPayloadStore,
)
from service_data_sync.application.ports.sector_market_data import SectorMarketDataRepository
from service_data_sync.application.ports.sector_membership import SectorMembershipRepository
from service_data_sync.application.ports.stock_connect_market_stat_research import (
    StockConnectMarketStatFailureEvidenceStore,
    StockConnectMarketStatResearchRepository,
)

__all__ = [
    "DataSourcePort",
    "DatasetAvailability",
    "DatasetAvailabilityRepository",
    "EquityMarketDataRepository",
    "IndexShadowRepository",
    "ProviderBatch",
    "ProviderError",
    "ProviderErrorCode",
    "RawPayloadStore",
    "SectorMarketDataRepository",
    "SectorMembershipRepository",
    "SourceRequest",
    "StockConnectMarketStatFailureEvidenceStore",
    "StockConnectMarketStatResearchRepository",
]
