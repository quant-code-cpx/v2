"""日频资金流同步与已发布读取的 provider-neutral 端口。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol
from uuid import UUID

from service_data_sync.domain.money_flow import (
    MoneyFlowDailyObservation,
    MoneyFlowMethodology,
    MoneyFlowRankingSnapshot,
    MoneyFlowScope,
    MoneyFlowScopeType,
    MoneyFlowWindowType,
)


@dataclass(frozen=True, slots=True)
class MoneyFlowSourceObservation:
    """携带 canonical 写入必须保留的 raw evidence 血缘。"""

    provider_id: str
    capability: str
    source_payload_sha256: str
    raw_uri: str
    observed_at: datetime
    upstream_source: str
    adapter_version: str
    schema_fingerprint: str


@dataclass(frozen=True, slots=True)
class PublishedMoneyFlow:
    """返回一次日序列或排行写入的发布、修订和 no-op 摘要。"""

    data_version: UUID | None
    inserted_count: int
    revised_count: int
    unchanged_count: int
    published: bool
    quality_status: str


@dataclass(frozen=True, slots=True)
class MoneyFlowMethodologyPage:
    """封装内部方法学目录的不可变 publication 和游标页。"""

    data_version: UUID
    published_at: datetime
    items: tuple[dict[str, object], ...]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class MoneyFlowDailyPage:
    """封装一个强身份日序列页和 point-in-time 元数据。"""

    series_id: UUID
    data_version: UUID
    published_at: datetime
    methodology: dict[str, object]
    scope: dict[str, object]
    universe: str
    bucket: str
    known_at_applied: datetime | None
    items: tuple[dict[str, object], ...]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class MoneyFlowRankingPage:
    """封装一份不可变供应商排行快照页。"""

    data_version: UUID
    published_at: datetime
    methodology: dict[str, object]
    snapshot: dict[str, object]
    items: tuple[dict[str, object], ...]
    next_cursor: str | None


class MoneyFlowRepository(Protocol):
    """发布方法学、日序列和供应商排行，不向应用层泄漏 ORM。"""

    def publish_daily(
        self,
        *,
        methodology: MoneyFlowMethodology,
        observations: Sequence[MoneyFlowDailyObservation],
        source: MoneyFlowSourceObservation,
        run_id: UUID | None = None,
        partition_key: str | None = None,
    ) -> PublishedMoneyFlow:
        """按交易日解析身份并原子追加有变化的知识修订。"""
        ...

    def publish_ranking(
        self,
        *,
        methodology: MoneyFlowMethodology,
        snapshot: MoneyFlowRankingSnapshot,
        source: MoneyFlowSourceObservation,
        run_id: UUID | None = None,
        partition_key: str | None = None,
    ) -> PublishedMoneyFlow:
        """完整性和身份质量通过后原子发布供应商快照。"""
        ...


class MoneyFlowReadRepository(Protocol):
    """只读取 production-enabled publication，不访问 provider 或 raw。"""

    def list_methodologies(
        self,
        *,
        semantic_family: str | None,
        methodology_status: str | None,
        scope_type: MoneyFlowScopeType | None,
        cursor: str | None,
        limit: int,
    ) -> MoneyFlowMethodologyPage | None:
        """读取公开候选方法学目录；无 publication 时返回空。"""
        ...

    def list_daily(
        self,
        *,
        methodology_id: str,
        methodology_version: str,
        scope: MoneyFlowScope,
        bucket: str,
        start: date,
        end: date,
        known_at: datetime | None,
        cursor: str | None,
        limit: int,
    ) -> MoneyFlowDailyPage | None:
        """读取一个方法学、scope 与 bucket 的当前或历史知识视图。"""
        ...

    def list_ranking(
        self,
        *,
        methodology_id: str,
        methodology_version: str,
        scope_type: MoneyFlowScopeType,
        universe: str,
        window_type: MoneyFlowWindowType,
        window_size: int,
        bucket: str,
        trade_date: date | None,
        cursor: str | None,
        limit: int,
    ) -> MoneyFlowRankingPage | None:
        """读取 exact 或 latest 的不可变供应商排行 publication。"""
        ...
