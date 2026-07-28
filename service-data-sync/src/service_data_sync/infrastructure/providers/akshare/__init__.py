"""AKShare 适配器；上游 API 与字段必须封装在本包内。"""

from service_data_sync.infrastructure.providers.akshare.eastmoney_equity_catalog import (
    AkshareEastmoneyEquityCatalogAdapter,
)
from service_data_sync.infrastructure.providers.akshare.eastmoney_sector_bars import (
    AkshareEastmoneySectorBarsAdapter,
)
from service_data_sync.infrastructure.providers.akshare.eastmoney_sector_eod import (
    AkshareEastmoneySectorEodAdapter,
)
from service_data_sync.infrastructure.providers.akshare.eastmoney_sector_membership import (
    AkshareEastmoneySectorMembershipAdapter,
)
from service_data_sync.infrastructure.providers.akshare.tencent_daily_bars import (
    AkshareTencentDailyBarsAdapter,
)

__all__ = [
    "AkshareEastmoneyEquityCatalogAdapter",
    "AkshareEastmoneySectorBarsAdapter",
    "AkshareEastmoneySectorEodAdapter",
    "AkshareEastmoneySectorMembershipAdapter",
    "AkshareTencentDailyBarsAdapter",
]
