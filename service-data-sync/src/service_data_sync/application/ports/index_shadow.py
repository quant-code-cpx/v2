"""指数 P0-A 影子观察的应用端口与中立值对象。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Protocol
from uuid import UUID

from service_data_sync.domain.index import IndexIdentifier


@dataclass(frozen=True, slots=True)
class IndexShadowSourceObservation:
    """表示已归档 raw 与标准载荷的来源观察，尚不意味着可向消费者发布。"""

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
class IndexCatalogObservationEntry:
    """表示一次目录观察中的来源条目，不把代码或名称变化解释为正式生命周期事件。"""

    identifier: IndexIdentifier
    name: str
    full_name: str | None
    base_date: date | None
    base_value: Decimal | None
    published_date: date | None
    constituent_count: int | None


@dataclass(frozen=True, slots=True)
class IndexObservedSnapshotItem:
    """表示当前成分或权重快照的一条来源证券观察，不携带推断出的平台证券身份。"""

    source_symbol: str
    source_name: str
    source_exchange: str | None
    source_industry: str | None
    weight_value: Decimal | None
    weight_kind: str | None


@dataclass(frozen=True, slots=True)
class StoredIndexShadowObservation:
    """描述仓储已提交的研究态观察记录，不含 dataVersion 或 PIT 可见性。"""

    observation_id: UUID
    item_count: int
    quality_status: str


class IndexShadowRepository(Protocol):
    """持有指数 P0-A 的目录、当前成分和权重观察，不承担正式 PIT 或发布职责。"""

    def record_catalog(
        self,
        *,
        administrator: str,
        entries: tuple[IndexCatalogObservationEntry, ...],
        source: IndexShadowSourceObservation,
    ) -> StoredIndexShadowObservation:
        """记录一个管理人的非空目录观察，并保留每次抓取的独立来源证据。"""
        ...

    def record_snapshot(
        self,
        *,
        identifier: IndexIdentifier,
        observation_kind: str,
        source_as_of_date: date | None,
        items: tuple[IndexObservedSnapshotItem, ...],
        source: IndexShadowSourceObservation,
    ) -> StoredIndexShadowObservation:
        """记录一个当前成分或权重观察，未知日期或交易所必须原样保持为空。"""
        ...
