"""融资融券 P0 场所日汇总的应用端口。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from service_data_sync.domain.margin import (
    MarginEligibility,
    MarginMarketDaily,
    MarginSecurityDaily,
    MarginVenue,
)


@dataclass(frozen=True, slots=True)
class MarginSourceObservation:
    """表示两融来源的 raw 与标准载荷已分开归档，满足日后单位和字段审计需要。"""

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
class PublishedMarginMarketDaily:
    """描述场所日汇总分区的发布版本和幂等写入结果。"""

    data_version: UUID
    inserted_count: int
    unchanged_count: int
    venue: MarginVenue


class MarginMarketDailyRepository(Protocol):
    """负责两融市场直报汇总的 revision、来源、质量与独立 publication。"""

    def publish_market_daily(
        self,
        *,
        venue: MarginVenue,
        records: Sequence[MarginMarketDaily],
        source: MarginSourceObservation,
    ) -> PublishedMarginMarketDaily:
        """原子发布场所日汇总，禁止由证券明细回填缺失字段或合并来源。"""
        ...


@dataclass(frozen=True, slots=True)
class PublishedMarginSecurityDaily:
    """描述一个场所证券日明细分区的发布版本与幂等写入计数。"""

    data_version: UUID
    inserted_count: int
    unchanged_count: int
    venue: MarginVenue


class MarginSecurityDailyRepository(Protocol):
    """负责证券日明细的身份解析、revision 与独立 publication。"""

    def publish_security_daily(
        self,
        *,
        venue: MarginVenue,
        records: Sequence[MarginSecurityDaily],
        source: MarginSourceObservation,
    ) -> PublishedMarginSecurityDaily:
        """发布来源直报证券明细；身份不能解析时隔离而不以代码替代内部键。"""
        ...


@dataclass(frozen=True, slots=True)
class PublishedMarginEligibility:
    """描述两融资格集合发布版本与写入计数，资格历史与证券日明细独立。"""

    data_version: UUID
    inserted_count: int
    unchanged_count: int
    venue: MarginVenue


class MarginEligibilityRepository(Protocol):
    """负责两融资格的双时间版本和来源证据，不由当前名单差集推断调出。"""

    def publish_eligibility(
        self,
        *,
        venue: MarginVenue,
        records: Sequence[MarginEligibility],
        source: MarginSourceObservation,
    ) -> PublishedMarginEligibility:
        """原子发布资格记录；观察目录只能建立 observation-based 知识版本。"""
        ...
