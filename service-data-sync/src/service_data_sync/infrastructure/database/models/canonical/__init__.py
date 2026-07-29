"""跨数据域复用的 canonical 生命周期、来源血缘、质量和发布支撑模型。

这些表把“来源观察 → 规范化候选 → 质量判定 → 不可变 release → 消费者 publication”分开保存，
使重跑、失败隔离和回滚不会覆盖历史事实。它们不替代各数据域自己的强类型事实表。
"""

from .dataset import CanonicalDataset
from .lifecycle import (
    NormalizationRun,
    NormalizedRecordManifest,
    RawPayloadManifest,
)
from .methodology import MethodologyVersion
from .quality import QualityEvaluation, QualityResult, QuarantineRecord
from .release import CanonicalCheckpoint, CanonicalRecordLineage, DatasetRelease
from .source import DataSource, SourceDataset

__all__ = [
    "CanonicalCheckpoint",
    "CanonicalDataset",
    "CanonicalRecordLineage",
    "DataSource",
    "DatasetRelease",
    "MethodologyVersion",
    "NormalizationRun",
    "NormalizedRecordManifest",
    "QualityEvaluation",
    "QualityResult",
    "QuarantineRecord",
    "RawPayloadManifest",
    "SourceDataset",
]
