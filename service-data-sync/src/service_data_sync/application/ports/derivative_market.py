"""衍生品 `P0` 标准日行情的持久化端口。

端口只接收真实合约、上游直报日线和来源观察；它把同步服务与实际数据库隔离，也拒绝连续合约或合成行情混入该发布链路。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from service_data_sync.domain.derivative import DerivativeContractIdentifier, DerivativeDailyBar


@dataclass(frozen=True, slots=True)
class PublishedDerivativeDailyBars:
    """描述一个真实合约日线分区的不可变发布和幂等写入结果。"""

    data_version: UUID
    inserted_count: int
    unchanged_count: int
    contract: DerivativeContractIdentifier


@dataclass(frozen=True, slots=True)
class DerivativeSourceObservation:
    """表示同一 fetch 已分别归档的上游 raw 与 adapter 标准载荷，供血缘和 replay 使用。"""

    provider_id: str
    capability: str
    raw_payload_sha256: str
    raw_uri: str
    raw_content_type: str
    raw_byte_size: int
    normalized_payload_sha256: str
    normalized_uri: str
    normalized_content_type: str
    normalized_byte_size: int
    observed_at: datetime
    upstream_source: str
    adapter_version: str
    schema_fingerprint: str


class DerivativeDailyBarRepository(Protocol):
    """负责真实合约日行情写入；连续或主力序列由独立 P2 端口处理。"""

    def publish_daily_bars(
        self,
        *,
        contract: DerivativeContractIdentifier,
        bars: Sequence[DerivativeDailyBar],
        source: DerivativeSourceObservation,
    ) -> PublishedDerivativeDailyBars:
        """依据双载荷来源观察，原子写入并发布一个真实合约日行情版本。"""
        ...
