"""板块 EOD 横截面同步与版本化读取端口，不依赖供应商或 SQL。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from service_data_sync.domain.sector import (
    SectorEodQuote,
    SectorEodSnapshot,
    SectorEodSort,
    SectorIdentifier,
    SectorScheme,
    SortOrder,
)

# EOD shadow 初始质量阈值的冻结版本；变更必须产生新版本并保留旧快照证据。
SECTOR_EOD_QUALITY_POLICY_VERSION = "sector-eod-shadow-v1"


class SectorEodExecutionMode(StrEnum):
    """区分仅保存候选的 shadow 与可推进消费者版本的 publish。"""

    SHADOW = "shadow"
    PUBLISH = "publish"


@dataclass(frozen=True, slots=True)
class PublishedSectorEodSnapshot:
    """描述一次 EOD 候选或发布写入后新增或复用的不可变结果。"""

    snapshot: SectorEodSnapshot
    inserted: bool


@dataclass(frozen=True, slots=True)
class RankedSectorEodQuote:
    """携带内部板块 UUID、稳定页位置和 competition rank 的 EOD 报价。"""

    sector_id: UUID
    quote: SectorEodQuote
    rank: int | None
    position: int


@dataclass(frozen=True, slots=True)
class SectorEodRun:
    """表示一个 EOD scheme/date 分区当前获得的 PostgreSQL 租约与 fencing token。"""

    run_id: UUID
    lease_token: UUID
    scheme: SectorScheme
    trade_date: date


@dataclass(frozen=True, slots=True)
class QueuedSectorEodRun:
    """表示需要由 worker 重新投递的 EOD 分区，不泄漏 lease 或来源原始字段。"""

    scheme: SectorScheme
    trade_date: date


@dataclass(frozen=True, slots=True)
class ArchivedSectorEodObservation:
    """表示已归档且已登记 source batch 的原始观察，供失败后严格 replay。"""

    source_batch_id: UUID
    raw_uri: str
    provider_id: str
    observed_at: datetime
    adapter_version: str
    schema_fingerprint: str


@dataclass(frozen=True, slots=True)
class SectorEodHistoricalReference:
    """表示目标日前最近已发布快照的稳定摘要，供跨日质量规则只读比对。"""

    trade_date: date
    content_sha256: bytes
    market_values: Mapping[str, Decimal | None]

    def __post_init__(self) -> None:
        """限制摘要长度并冻结最小字段，防止质量应用层读取完整历史 canonical 行。"""
        if len(self.content_sha256) != 32:
            raise ValueError("sector eod historical content digest must be SHA-256 bytes")


@dataclass(frozen=True, slots=True)
class SectorEodQualityResult:
    """表示一次 EOD 发布候选的结构化质量规则结果，不承载原始来源响应。"""

    rule_code: str
    severity: str
    passed: bool
    actual: Mapping[str, int | str]
    threshold: Mapping[str, int | str]

    def __post_init__(self) -> None:
        """限制规则代码、严重级别和轻量标量证据，避免质量表变成 raw 存储。"""
        if not self.rule_code or len(self.rule_code) > 64:
            raise ValueError("sector eod quality rule code is invalid")
        if self.severity not in {"info", "warning", "blocking"}:
            raise ValueError("sector eod quality severity is invalid")


class SectorEodRepository(Protocol):
    """维护 EOD 横截面、质量证据、原子发布和严格版本化读取。"""

    def publish_snapshot(
        self,
        *,
        scheme: SectorScheme,
        trade_date: date,
        source_cutoff_at: datetime,
        observed_at: datetime,
        quotes: Sequence[SectorEodQuote],
        provider_id: str,
        source_payload_sha256: str,
        raw_uri: str,
        adapter_version: str,
        schema_fingerprint: str,
        run: SectorEodRun | None = None,
        source_batch_id: UUID | None = None,
        quality_status: str = "passed",
        quality_results: Sequence[SectorEodQualityResult] = (),
    ) -> PublishedSectorEodSnapshot:
        """以完整 ACTIVE 目录为质量门，写入或复用一个可发布横截面。"""
        ...

    def store_quarantined_snapshot(
        self,
        *,
        scheme: SectorScheme,
        trade_date: date,
        source_cutoff_at: datetime,
        observed_at: datetime,
        quotes: Sequence[SectorEodQuote],
        provider_id: str,
        source_payload_sha256: str,
        raw_uri: str,
        adapter_version: str,
        schema_fingerprint: str,
        run: SectorEodRun,
        source_batch_id: UUID,
        quality_results: Sequence[SectorEodQualityResult],
    ) -> None:
        """持久化完整但阻断质量失败的候选与证据，不替换任何 consumer publication。"""
        ...

    def store_shadow_snapshot(
        self,
        *,
        scheme: SectorScheme,
        trade_date: date,
        source_cutoff_at: datetime,
        observed_at: datetime,
        quotes: Sequence[SectorEodQuote],
        provider_id: str,
        source_payload_sha256: str,
        raw_uri: str,
        adapter_version: str,
        schema_fingerprint: str,
        run: SectorEodRun,
        source_batch_id: UUID,
        quality_status: str,
        quality_results: Sequence[SectorEodQualityResult],
    ) -> PublishedSectorEodSnapshot:
        """保存通过质量门的 shadow candidate，不创建或替换 consumer publication。"""
        ...

    def start_run(
        self, *, scheme: SectorScheme, trade_date: date, reuse_archived_raw: bool
    ) -> SectorEodRun:
        """获取目标分区的可回收租约；replay 仅允许复用已有 raw observation。"""
        ...

    def record_archived_observation(
        self,
        *,
        run: SectorEodRun,
        provider_id: str,
        source_payload_sha256: str,
        raw_uri: str,
        observed_at: datetime,
        adapter_version: str,
        schema_fingerprint: str,
        upstream_source: str | None = None,
    ) -> ArchivedSectorEodObservation:
        """在 raw 已可靠落盘后创建独立 source batch 并推进 checkpoint。"""
        ...

    def get_archived_observation(self, *, run: SectorEodRun) -> ArchivedSectorEodObservation:
        """读取当前 checkpoint 的 raw evidence 元数据，不向 provider 发起第二次请求。"""
        ...

    def has_archived_observation(self, *, scheme: SectorScheme, trade_date: date) -> bool:
        """判断分区是否已有可 replay 的 raw evidence，供可重试任务避免重复抓取来源。"""
        ...

    def get_historical_reference(
        self, *, scheme: SectorScheme, before_trade_date: date
    ) -> SectorEodHistoricalReference | None:
        """读取目标日前最近已发布快照的最小质量参考，不返回未发布候选。"""
        ...

    def mark_normalized(self, *, run: SectorEodRun) -> None:
        """记录标准化已完成；更新必须携带当前 fencing token。"""
        ...

    def mark_fetched(self, *, run: SectorEodRun) -> None:
        """记录 provider 已返回而 raw 尚未归档的阶段；更新必须携带当前 fencing token。"""
        ...

    def renew_lease(self, *, run: SectorEodRun) -> None:
        """延长当前 owner 的短期租约；已被接管或过期时拒绝僵尸 worker 继续执行。"""
        ...

    def requeue_expired_leases(self, *, now: datetime) -> int:
        """由运维 reaper 释放过期分区并保留 checkpoint，返回可安全重新投递的数量。"""
        ...

    def list_queued_runs(self) -> Sequence[QueuedSectorEodRun]:
        """返回已释放且未完成的 EOD 分区，供受控 reaper 重新投递固定任务。"""
        ...

    def mark_failed(self, *, run: SectorEodRun, error_code: str) -> None:
        """持久化稳定失败码并释放租约，保留 raw 和 checkpoint 供后续恢复。"""
        ...

    def get_published_snapshot(
        self, *, scheme: SectorScheme, trade_date: date | None
    ) -> SectorEodSnapshot | None:
        """读取最新或精确交易日的 published 快照，绝不回退指定日期。"""
        ...

    def rollback_published_snapshot(
        self, *, scheme: SectorScheme, trade_date: date, revision: int
    ) -> SectorEodSnapshot:
        """将当前 consumer publication 原子指回指定已 superseded 的通过版本，不删除任何证据。"""
        ...

    def list_ranked_quotes(
        self,
        *,
        snapshot_id: UUID,
        sort: SectorEodSort,
        order: SortOrder,
        after_position: int | None,
        limit: int,
    ) -> Sequence[RankedSectorEodQuote]:
        """在单个不可变快照内重算排行并按稳定位置返回有界页。"""
        ...

    def get_snapshot_quote(
        self, *, snapshot_id: UUID, identifier: SectorIdentifier
    ) -> RankedSectorEodQuote | None:
        """读取一个已发布快照中的单板块报价，不附加排行语义。"""
        ...
