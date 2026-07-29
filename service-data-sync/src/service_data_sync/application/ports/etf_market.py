"""ETF P0 未复权日行情的应用持久化端口。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from service_data_sync.domain.etf import (
    EtfDailyBar,
    EtfDailyStatus,
    EtfIdentifier,
    EtfNav,
    EtfProfile,
)


@dataclass(frozen=True, slots=True)
class EtfSourceObservation:
    """表示已独立归档上游 raw 与 adapter 标准 JSON 的单次来源观察。"""

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
class PublishedEtfDailyBars:
    """描述一个 ETF 日线分区成功发布或同内容重放的稳定结果。"""

    data_version: UUID
    inserted_count: int
    unchanged_count: int
    etf: EtfIdentifier


class EtfDailyBarRepository(Protocol):
    """负责 ETF 未复权日线 revision、血缘、质量和 publication 的原子写入。"""

    def publish_daily_bars(
        self,
        *,
        etf: EtfIdentifier,
        bars: Sequence[EtfDailyBar],
        source: EtfSourceObservation,
    ) -> PublishedEtfDailyBars:
        """基于双载荷来源观察写入日线，不允许静默改为复权价格或推断状态。"""
        ...


@dataclass(frozen=True, slots=True)
class PublishedEtfNavs:
    """描述一个 ETF NAV 分区的 immutable data version 与幂等写入计数。"""

    data_version: UUID
    inserted_count: int
    unchanged_count: int
    etf: EtfIdentifier


class EtfNavRepository(Protocol):
    """负责 ETF 单位/累计 NAV 的 revision 和发布，不接收 IOPV 或折溢价派生值。"""

    def publish_navs(
        self,
        *,
        etf: EtfIdentifier,
        navs: Sequence[EtfNav],
        source: EtfSourceObservation,
    ) -> PublishedEtfNavs:
        """依据双载荷来源观察原子发布不同 NAV 类型，未知发布时间必须保守标记。"""
        ...


@dataclass(frozen=True, slots=True)
class PublishedEtfReference:
    """描述 ETF 主数据或状态分区的发布版本和幂等变更统计。"""

    data_version: UUID
    inserted_count: int
    unchanged_count: int
    etf: EtfIdentifier | None


class EtfReferenceRepository(Protocol):
    """负责 ETF 产品资料及三个独立状态维度的双时间发布。"""

    def publish_profiles(
        self,
        *,
        profiles: Sequence[EtfProfile],
        source: EtfSourceObservation,
    ) -> PublishedEtfReference:
        """按交易所独立发布产品资料；目录差集不能自动产生摘牌版本。"""
        ...

    def publish_statuses(
        self,
        *,
        etf: EtfIdentifier,
        statuses: Sequence[EtfDailyStatus],
        source: EtfSourceObservation,
    ) -> PublishedEtfReference:
        """发布日级状态维度，停牌不能自动等同于申购或赎回暂停。"""
        ...
