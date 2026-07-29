"""日频资金流同步、解码与方法学门禁用例。

每个同步结果必须绑定明确方法学版本；未完成完整性与质量验证的来源只能保留研究态，不能进入生产读取。
"""

from .sync import MoneyFlowSyncResult, MoneyFlowSyncService

__all__ = ["MoneyFlowSyncResult", "MoneyFlowSyncService"]
