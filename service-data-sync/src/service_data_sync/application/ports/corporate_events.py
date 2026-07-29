"""公司公告与业绩 `P0` 的应用端口。

端口要求仓储将公告证据、业绩预告和业绩快报作为同一可追溯发布单元处理，避免脱离公告证据写入指标。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from service_data_sync.domain.corporate import (
    DisclosureDocument,
    EarningsExpressMetric,
    EarningsGuidanceMetric,
)


@dataclass(frozen=True, slots=True)
class CorporateSourceObservation:
    """表示公告来源 raw 与标准化载荷均已归档，可支持文档和字段审计。"""

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
class PublishedCorporateEvents:
    """描述公告域一次原子发布的版本和按事实类型统计的变更数。"""

    data_version: UUID
    inserted_count: int
    unchanged_count: int


class CorporateEventsRepository(Protocol):
    """负责公告文档、预告和快报修订及其来源血缘的原子发布。"""

    def publish(
        self,
        *,
        documents: Sequence[DisclosureDocument],
        guidance_metrics: Sequence[EarningsGuidanceMetric],
        express_metrics: Sequence[EarningsExpressMetric],
        source: CorporateSourceObservation,
    ) -> PublishedCorporateEvents:
        """发布文档证据及其 P0 指标，禁止以聚合当前值覆盖旧公告版本。"""
        ...
