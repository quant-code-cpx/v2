"""龙虎榜与大宗交易 P0 的应用端口。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from service_data_sync.domain.trading_events import BlockTrade, DragonTigerEvent


@dataclass(frozen=True, slots=True)
class TradingEventsSourceObservation:
    """表示交易披露来源的 raw 与标准 JSON 已分别归档。"""

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
class PublishedTradingEvents:
    """描述一个交易披露 dataset 的原子发布版本及幂等变更统计。"""

    data_version: UUID
    inserted_count: int
    unchanged_count: int


class DragonTigerRepository(Protocol):
    """负责龙虎榜事件头、席位和来源血缘的独立 revision/publication。"""

    def publish_dragon_tiger(
        self,
        *,
        events: Sequence[DragonTigerEvent],
        source: TradingEventsSourceObservation,
    ) -> PublishedTradingEvents:
        """发布一个有界龙虎榜窗口，禁止将机构聚合或事后排行混入原始事件。"""
        ...


class BlockTradeRepository(Protocol):
    """负责大宗交易逐笔事实和来源血缘的独立 revision/publication。"""

    def publish_block_trades(
        self,
        *,
        trades: Sequence[BlockTrade],
        source: TradingEventsSourceObservation,
    ) -> PublishedTradingEvents:
        """发布大宗逐笔成交，保留同经济字段但 occurrence 不同的合法重复成交。"""
        ...
