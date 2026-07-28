"""AKShare 适配器；上游 API 与字段必须封装在本包内。"""

from service_data_sync.infrastructure.providers.akshare.cninfo_company_profile import (
    AkshareCninfoCompanyProfileAdapter,
)
from service_data_sync.infrastructure.providers.akshare.eastmoney_corporate_actions import (
    AkshareEastmoneyCorporateActionsAdapter,
)
from service_data_sync.infrastructure.providers.akshare.eastmoney_equity_catalog import (
    AkshareEastmoneyEquityCatalogAdapter,
)
from service_data_sync.infrastructure.providers.akshare.eastmoney_equity_period_bars import (
    AkshareEastmoneyEquityPeriodBarsAdapter,
)
from service_data_sync.infrastructure.providers.akshare.eastmoney_financial import (
    AkshareEastmoneyFinancialAdapter,
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
from service_data_sync.infrastructure.providers.akshare.exchange_equity_lifecycle import (
    AkshareExchangeEquityLifecycleAdapter,
)
from service_data_sync.infrastructure.providers.akshare.money_flow import (
    AkshareEastmoneyMoneyFlowAdapter,
    AkshareThsMoneyFlowAdapter,
)
from service_data_sync.infrastructure.providers.akshare.sina_adjustment_factors import (
    AkshareSinaAdjustmentFactorsAdapter,
)
from service_data_sync.infrastructure.providers.akshare.sw_industry_snapshot import (
    AkshareSwIndustrySnapshotAdapter,
)
from service_data_sync.infrastructure.providers.akshare.tencent_daily_bars import (
    AkshareTencentDailyBarsAdapter,
)

__all__ = [
    "AkshareCninfoCompanyProfileAdapter",
    "AkshareEastmoneyCorporateActionsAdapter",
    "AkshareEastmoneyEquityCatalogAdapter",
    "AkshareEastmoneyEquityPeriodBarsAdapter",
    "AkshareEastmoneyFinancialAdapter",
    "AkshareEastmoneyMoneyFlowAdapter",
    "AkshareEastmoneySectorBarsAdapter",
    "AkshareEastmoneySectorEodAdapter",
    "AkshareEastmoneySectorMembershipAdapter",
    "AkshareExchangeEquityLifecycleAdapter",
    "AkshareSinaAdjustmentFactorsAdapter",
    "AkshareSwIndustrySnapshotAdapter",
    "AkshareThsMoneyFlowAdapter",
    "AkshareTencentDailyBarsAdapter",
]
