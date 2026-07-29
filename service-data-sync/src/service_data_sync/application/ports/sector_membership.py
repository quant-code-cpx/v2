"""板块成分观测、发布清单与双向读取的中立端口。

端口保留来源当前集合、身份解析结果和冻结 `release`。
观测区间仅可由完整快照推进，不能从空响应或目录差异推断真实调入调出日期。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol
from uuid import UUID

from service_data_sync.application.ports.sector_market_data import StoredSector
from service_data_sync.domain.equity import Exchange
from service_data_sync.domain.sector import (
    SectorIdentifier,
    SectorMembershipCandidate,
    SectorScheme,
)


@dataclass(frozen=True, slots=True)
class PublishedSectorMembershipSnapshot:
    """描述一板块当前来源集合的已提交观测与是否推进观测区间。"""

    snapshot_id: UUID
    observed_at: datetime
    complete: bool
    inserted_interval_count: int
    closed_interval_count: int
    pending_count: int
    quarantine_count: int


@dataclass(frozen=True, slots=True)
class PublishedSectorMembershipRelease:
    """描述 scheme 级不可变成分 release 及其稳定数据版本。"""

    release_id: UUID
    data_version: UUID
    quality_status: str
    fresh_sector_count: int
    carried_forward_sector_count: int
    published_at: datetime


@dataclass(frozen=True, slots=True)
class SectorMembershipRun:
    """表示一次 scheme 成分任务的 PostgreSQL 权威运行身份与冻结目标市场日。"""

    run_id: UUID
    scheme: SectorScheme
    observation_date: date


@dataclass(frozen=True, slots=True)
class StoredSectorMembershipRelease:
    """表示 internal API 可读取的已发布 scheme 级观测清单上下文。"""

    release_id: UUID
    scheme: SectorScheme
    requested_as_of: datetime | None
    resolved_as_of: datetime
    coverage_start: datetime
    data_version: UUID
    quality_status: str
    carried_forward_sector_count: int
    published_at: datetime


@dataclass(frozen=True, slots=True)
class StoredMembershipConstituent:
    """表示某 release 固定快照内的一条已确认证券成分及其观测区间。"""

    instrument_id: UUID
    exchange: Exchange
    symbol: str
    name: str
    listing_status: str
    observed_from: datetime
    observed_to: datetime | None


@dataclass(frozen=True, slots=True)
class StoredEquityMembership:
    """表示某 release 中一只已确认证券所属的一个板块观测关系。"""

    sector: StoredSector
    observed_from: datetime
    observed_to: datetime | None
    snapshot_observed_at: datetime
    carried_forward: bool


@dataclass(frozen=True, slots=True)
class StoredMembershipEquity:
    """表示在 release 知识视图中可供反向查询的已确认证券身份。"""

    instrument_id: UUID
    exchange: Exchange
    symbol: str
    name: str
    listing_status: str


class SectorMembershipRepository(Protocol):
    """持有板块成分 canonical 观测、区间与 release，不依赖具体来源实现。"""

    def list_active_sectors(self, *, scheme: SectorScheme) -> Sequence[StoredSector]:
        """读取本次 scheme 运行冻结的 ACTIVE 板块集合。"""
        ...

    def publish_snapshot(
        self,
        *,
        sector: StoredSector,
        observation_date: date,
        candidates: Sequence[SectorMembershipCandidate],
        provider_id: str,
        source_payload_sha256: str,
        raw_uri: str,
        observed_at: datetime,
        upstream_source: str | None,
        adapter_version: str,
        schema_fingerprint: str,
        run_id: UUID,
        partition_key: str,
    ) -> PublishedSectorMembershipSnapshot:
        """写入一次来源观测，只有完整已确认快照才更新半开观测区间。"""
        ...

    def start_run(
        self,
        *,
        scheme: SectorScheme,
        observation_date: date,
        sectors: Sequence[StoredSector],
    ) -> SectorMembershipRun:
        """创建或恢复冻结分区任务，并在 PostgreSQL 中获取可回收 partition lease。"""
        ...

    def mark_partition_completed(
        self,
        *,
        run: SectorMembershipRun,
        sector: StoredSector,
        publication: PublishedSectorMembershipSnapshot,
    ) -> None:
        """持久化分区 checkpoint；隔离快照标为 partial，不能伪装成功。"""
        ...

    def mark_partition_failed(
        self,
        *,
        run: SectorMembershipRun,
        sector: StoredSector,
        error_code: str,
    ) -> None:
        """记录终止来源失败并释放 lease，供同一幂等 run 后续恢复。"""
        ...

    def finish_run(self, *, run: SectorMembershipRun, status: str) -> None:
        """以 succeeded、partial 或 failed 结束任务，不覆盖分区审计状态。"""
        ...

    def publish_release(
        self, *, scheme: SectorScheme, observation_date: date
    ) -> PublishedSectorMembershipRelease | None:
        """以完整快照或受限 carry-forward 原子发布 scheme 级固定清单。"""
        ...

    def get_release(
        self, *, scheme: SectorScheme, as_of: datetime | None
    ) -> StoredSectorMembershipRelease | None:
        """选择当前或不晚于请求时刻的已发布清单，不读取未发布快照。"""
        ...

    def get_release_sector(
        self, *, release_id: UUID, identifier: SectorIdentifier
    ) -> tuple[StoredSector, datetime, bool] | None:
        """读取 release 内固定板块快照的观测时刻与 carry-forward 标记。"""
        ...

    def list_constituents(
        self,
        *,
        release_id: UUID,
        identifier: SectorIdentifier,
        after_exchange: Exchange | None,
        after_symbol: str | None,
        limit: int,
    ) -> Sequence[StoredMembershipConstituent]:
        """按交易所和代码稳定分页读取 release 固定快照中的已确认成分。"""
        ...

    def get_release_equity(
        self,
        *,
        release_id: UUID,
        exchange: Exchange,
        symbol: str,
    ) -> StoredMembershipEquity | None:
        """按 release 固定知识视图解析一只已确认证券，用于反向读取。"""
        ...

    def list_equity_memberships(
        self,
        *,
        release_id: UUID,
        instrument_id: UUID,
        after_sector_code: str | None,
        limit: int,
    ) -> Sequence[StoredEquityMembership]:
        """按板块代码稳定分页读取一只证券在 release 中的观测归属。"""
        ...
