"""沪深港通 P0 官方通道日终统计的应用端口。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from service_data_sync.domain.stock_connect import (
    StockConnectActiveSecurity,
    StockConnectChannel,
    StockConnectMarketDaily,
)


@dataclass(frozen=True, slots=True)
class StockConnectSourceObservation:
    """表示已归档的官方原始对象和 adapter 标准对象，不暗示字段在历史上持续可得。"""

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


@dataclass(frozen=True, slots=True)
class PublishedStockConnectMarketDaily:
    """描述一个通道方向分区发布的稳定版本与本次幂等计数。"""

    data_version: UUID
    inserted_count: int
    unchanged_count: int
    channel: StockConnectChannel


class StockConnectMarketDailyRepository(Protocol):
    """负责官方通道统计、披露制度和独立 publication，禁止以估算资金流填补字段。"""

    def publish_market_daily(
        self,
        *,
        channel: StockConnectChannel,
        records: Sequence[StockConnectMarketDaily],
        source: StockConnectSourceObservation,
    ) -> PublishedStockConnectMarketDaily:
        """原子写入一个通道方向的日频统计，制度断点必须随记录版本保留。"""
        ...


@dataclass(frozen=True, slots=True)
class PublishedStockConnectActiveSecurities:
    """描述一个通道方向活跃证券集合的发布版本与幂等变更统计。"""

    data_version: UUID
    inserted_count: int
    unchanged_count: int
    channel: StockConnectChannel


class StockConnectActiveSecurityRepository(Protocol):
    """负责活跃证券集合、跨市场工具身份解析和独立 publication。"""

    def publish_active_securities(
        self,
        *,
        channel: StockConnectChannel,
        records: Sequence[StockConnectActiveSecurity],
        source: StockConnectSourceObservation,
    ) -> PublishedStockConnectActiveSecurities:
        """发布日终活跃榜；无法精确解析 A/H 或跨市场工具时隔离而不按代码猜测。"""
        ...
