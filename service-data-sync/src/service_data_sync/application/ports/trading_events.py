"""龙虎榜与大宗交易 `P0` 的应用端口。

端口分别接收严格解码后的事件和逐笔成交及其来源观察，仓储按各自身份和重复规则发布，不能把两类披露混为同一数据集。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol
from uuid import UUID

from service_data_sync.domain.equity import EquityIdentifier
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
    excluded_count: int = 0


class DragonTigerRepository(Protocol):
    """负责龙虎榜事件头、席位和来源血缘的独立 revision/publication。"""

    def publish_dragon_tiger(
        self,
        *,
        events: Sequence[DragonTigerEvent],
        source: TradingEventsSourceObservation,
        start: date,
        end: date,
        identifier: EquityIdentifier | None = None,
    ) -> PublishedTradingEvents:
        """发布有界龙虎榜窗口及逐证券覆盖；合法空窗同样产生零记录 manifest。"""
        ...


class BlockTradeRepository(Protocol):
    """负责大宗交易逐笔事实和来源血缘的独立 revision/publication。"""

    def publish_block_trades(
        self,
        *,
        trades: Sequence[BlockTrade],
        source: TradingEventsSourceObservation,
        start: date,
        end: date,
        identifier: EquityIdentifier | None = None,
    ) -> PublishedTradingEvents:
        """发布大宗逐笔窗口及逐证券覆盖；单证券任务必须同时约束交易所身份。"""
        ...
