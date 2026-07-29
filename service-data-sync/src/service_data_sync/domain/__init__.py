"""数据源无关的核心领域类型与业务不变量。

领域层只描述“什么事实可以成立”：例如证券身份、行情、板块层级和披露事件；
它不访问供应商、数据库或网络。应用层和基础设施层都应复用这些值对象，避免各自解释同一业务口径。
"""

from service_data_sync.domain.sector import (
    SectorBar,
    SectorIdentifier,
    SectorPeriod,
    SectorScheme,
)

__all__ = ["SectorBar", "SectorIdentifier", "SectorPeriod", "SectorScheme"]
