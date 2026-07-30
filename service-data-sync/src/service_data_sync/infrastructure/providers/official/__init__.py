"""官方且需显式授权的数据源适配器。"""

from .stock_connect import OfficialStockConnectAdapter, OfficialStockConnectConfig

__all__ = ["OfficialStockConnectAdapter", "OfficialStockConnectConfig"]
