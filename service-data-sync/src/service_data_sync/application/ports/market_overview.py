"""市场概览不可变组件、完整包发布与读取端口。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Protocol
from uuid import UUID


@dataclass(frozen=True, slots=True)
class MarketComponentCandidate:
    """承载一个已通过应用层 schema 与质量门的 provider-neutral 组件候选。"""

    data_version: UUID
    dataset_code: str
    partition_key: str
    trade_date: date | None
    payload: dict[str, Any]
    source: dict[str, Any]
    methodology: dict[str, Any]
    quality: dict[str, Any]
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class PublishedMarketBundle:
    """返回原子发布后的完整包身份和首页载荷。"""

    data_version: UUID
    trade_date: date
    published_at: datetime
    payload: dict[str, Any]
    inserted: bool


@dataclass(frozen=True, slots=True)
class StoredMarketBundle:
    """表示内部读端选中的完整包，不暴露数据库主键或组件物理位置。"""

    data_version: UUID
    trade_date: date
    published_at: datetime
    payload: dict[str, Any]
    active_action: str
    active_changed_at: datetime


@dataclass(frozen=True, slots=True)
class StoredMarketComponent:
    """表示一个固定 canonical 组件发布及其完整血缘元数据。"""

    data_version: UUID
    dataset_code: str
    partition_key: str
    trade_date: date | None
    published_at: datetime
    payload: dict[str, Any]
    source: dict[str, Any]
    methodology: dict[str, Any]
    quality: dict[str, Any]


@dataclass(frozen=True, slots=True)
class StoredMarketSnapshot:
    """固定同一数据库快照内选中的 bundle 与其 manifest 组件。"""

    bundle: StoredMarketBundle
    components: tuple[StoredMarketComponent, ...]


@dataclass(frozen=True, slots=True)
class MarketBundlePointerResult:
    """返回受审计交易日指针变更后的公开 bundle 身份。"""

    data_version: UUID
    trade_date: date
    action: str
    changed_at: datetime


class MarketOverviewRepository(Protocol):
    """隔离应用层与 PostgreSQL，实现写时组合、读时原子和固定版本查询。"""

    def publish_complete_bundle(
        self,
        *,
        trade_date: date,
        components: tuple[MarketComponentCandidate, ...],
        overview: dict[str, Any],
    ) -> PublishedMarketBundle:
        """原子保存全部组件、完整包和 current pointer；任一步失败都不推进指针。"""
        ...

    def publish_derivation_inputs(
        self,
        *,
        components: tuple[MarketComponentCandidate, ...],
    ) -> int:
        """幂等保存近期 bootstrap 日线，只供写时派生且不推进任何公开 bundle 指针。"""
        ...

    def get_bundle(self, *, trade_date: date | None) -> StoredMarketBundle | None:
        """按精确交易日或 current pointer 读取完整包，不自动改用其他日期。"""
        ...

    def get_snapshot(self, *, trade_date: date | None) -> StoredMarketSnapshot | None:
        """在一个数据库事务快照内读取 active bundle 与固定 manifest。"""
        ...

    def get_bundle_components(
        self,
        *,
        trade_date: date | None,
    ) -> tuple[StoredMarketComponent, ...]:
        """读取精确 bundle manifest 固定的组件版本，禁止重新解析各数据集 latest。"""
        ...

    def list_components(
        self,
        *,
        dataset_code: str,
        start: date | None,
        end: date | None,
    ) -> tuple[StoredMarketComponent, ...]:
        """读取一个 canonical 数据集的已发布组件历史，供资源式内部 API 投影。"""
        ...

    def list_derivation_inputs(
        self,
        *,
        dataset_code: str,
        start: date,
        end: date,
    ) -> tuple[StoredMarketComponent, ...]:
        """读取内部近期日线 seed；该端口不得用于公开 reader。"""
        ...

    def move_active_bundle(
        self,
        *,
        trade_date: date,
        target_data_version: UUID,
        action: str,
        reason: str,
        actor_ref: str,
    ) -> MarketBundlePointerResult:
        """在锁内回滚或前滚公开指针；不可变 bundle 与组件历史保持可审计。"""
        ...


__all__ = [
    "MarketComponentCandidate",
    "MarketBundlePointerResult",
    "MarketOverviewRepository",
    "PublishedMarketBundle",
    "StoredMarketBundle",
    "StoredMarketComponent",
    "StoredMarketSnapshot",
]
