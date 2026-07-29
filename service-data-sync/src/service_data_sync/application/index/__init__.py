"""指数目录、成分和权重的影子同步应用用例。

影子同步只保留研究态来源观察、质量和血缘，用于评估供应商能力。
它不会创建生产 `publication` 或历史 `PIT` 事实。
"""

from .shadow_sync import IndexShadowSyncResult, IndexShadowSyncService

__all__ = ["IndexShadowSyncResult", "IndexShadowSyncService"]
