"""申万行业同步、发布、恢复与内部读取的 provider-neutral 端口。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal, Protocol
from uuid import UUID

from service_data_sync.domain.sw_sector import (
    SwIndustryNode,
    SwIndustrySnapshot,
    SwIndustryValuation,
    SwMethodology,
)

SwCapability = Literal["sector.sw.taxonomy", "sector.sw.valuation"]


@dataclass(frozen=True, slots=True)
class SwSourceObservation:
    """描述 raw 与中立重放载荷均已可靠归档的来源观察。"""

    provider_id: str
    capability: str
    source_payload_sha256: str
    raw_uri: str
    normalized_payload_sha256: str
    normalized_uri: str
    observed_at: datetime
    upstream_source: str
    adapter_version: str
    schema_fingerprint: str


@dataclass(frozen=True, slots=True)
class SwPublishedCapability:
    """描述一个申万 capability 的不可变消费者发布。"""

    capability: SwCapability
    data_version: UUID
    snapshot_date: date
    published_at: datetime
    inserted_count: int
    unchanged_count: int
    row_count: int
    content_sha256: str


@dataclass(frozen=True, slots=True)
class SwPublishResult:
    """组合同一次上游快照的 taxonomy 与估值独立发布结果。"""

    taxonomy: SwPublishedCapability
    valuation: SwPublishedCapability


@dataclass(frozen=True, slots=True)
class SwPublication:
    """提供内部 API 所需的当前发布元数据和方法学。"""

    capability: SwCapability
    data_version: UUID
    snapshot_date: date
    published_at: datetime
    quality_status: str
    row_count: int
    content_sha256: str
    methodology: SwMethodology


@dataclass(frozen=True, slots=True)
class SwCheckpoint:
    """保存某日可恢复标准载荷及其原始来源血缘。"""

    snapshot_date: date
    summary_sha256: str
    raw_sha256: str
    raw_uri: str
    normalized_uri: str
    provider_id: str
    upstream_source: str
    adapter_version: str
    schema_fingerprint: str
    observed_at: datetime
    last_data_version: UUID


@dataclass(frozen=True, slots=True)
class SwStoredNode:
    """组合一个 taxonomy 节点与当前知识 revision。"""

    node: SwIndustryNode
    revision: int


@dataclass(frozen=True, slots=True)
class SwStoredValuation:
    """组合一个行业估值观察、显示身份和当前知识 revision。"""

    node: SwIndustryNode
    valuation: SwIndustryValuation
    revision: int


class SwSectorRepository(Protocol):
    """负责申万快照双时间修订、闭包、发布、checkpoint 与读取。"""

    def publish_snapshot(
        self, *, snapshot: SwIndustrySnapshot, source: SwSourceObservation
    ) -> SwPublishResult:
        """在一个事务内发布 taxonomy、闭包、估值、质量与 checkpoint。"""
        ...

    def get_checkpoint(self, *, snapshot_date: date) -> SwCheckpoint | None:
        """读取指定观测日最近成功发布的可恢复标准载荷。"""
        ...

    def get_publication(
        self, *, capability: SwCapability, snapshot_date: date | None
    ) -> SwPublication | None:
        """读取指定日期或最新日期的消费者发布。"""
        ...

    def list_nodes(
        self,
        *,
        snapshot_date: date,
        level: int | None,
        parent_code: str | None,
        after_level: int | None,
        after_code: str | None,
        limit: int,
    ) -> Sequence[SwStoredNode]:
        """按层级和代码稳定分页读取一个 taxonomy 发布。"""
        ...

    def get_node(self, *, snapshot_date: date, code: str) -> SwStoredNode | None:
        """读取指定发布日期中的一个申万行业节点。"""
        ...

    def list_ancestors(
        self, *, data_version: UUID, snapshot_date: date, descendant_code: str
    ) -> Sequence[SwStoredNode]:
        """按根到直接父级顺序读取已发布父级闭包。"""
        ...

    def list_valuations(
        self,
        *,
        snapshot_date: date,
        level: int | None,
        after_code: str | None,
        limit: int,
    ) -> Sequence[SwStoredValuation]:
        """按代码稳定分页读取指定日期的行业估值观察。"""
        ...
