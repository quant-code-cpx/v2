"""沪深港通 `P0` 官方通道日终统计与活跃榜的应用端口。

端口以通道、方向和交易日分区保存官方披露；不可用字段保留状态解释，活跃证券在写入前经稳定身份解析，避免跨市场代码误绑。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from service_data_sync.domain.stock_connect import (
    StockConnectActiveSecurity,
    StockConnectCalendarDay,
    StockConnectChannel,
    StockConnectChannelStatus,
    StockConnectInstrumentMaster,
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


class StockConnectMarketRepository(
    StockConnectMarketDailyRepository,
    StockConnectActiveSecurityRepository,
    Protocol,
):
    """组合通道统计、活跃榜和港股稳定身份，供单日完整包应用服务使用。"""

    def ensure_hkex_instruments(
        self,
        *,
        records: Sequence[StockConnectInstrumentMaster],
        target_source_codes: set[str],
        source: StockConnectSourceObservation,
    ) -> dict[str, UUID]:
        """以官方稳定证券 ID 维护已跟踪港股身份，并返回本次可解析代码。"""
        ...


@dataclass(frozen=True, slots=True)
class PublishedStockConnectBundle:
    """描述互联互通通道完整包的稳定版本与幂等结果。"""

    bundle_release_id: UUID
    data_version: str
    reused: bool


class StockConnectCenterRepository(Protocol):
    """负责把已发布统计、活跃榜、日历和状态原子组装为完整通道包。"""

    def publish_bundle(
        self,
        *,
        channel: StockConnectChannel,
        overview_generation_id: UUID,
        overview_channels: Sequence[str],
        market_data_version: UUID,
        active_data_version: UUID | None,
        calendar: StockConnectCalendarDay,
        calendar_source_ref: Mapping[str, object],
        calendar_observed_at: datetime,
        status: StockConnectChannelStatus,
        quality_issues: Sequence[Mapping[str, str]],
        source_refs: Sequence[Mapping[str, object]],
    ) -> PublishedStockConnectBundle:
        """在 generation 目标集合内发布一个真实交易日通道包。"""
        ...
