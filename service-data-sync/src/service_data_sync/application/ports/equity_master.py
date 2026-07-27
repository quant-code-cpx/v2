"""证券目录主数据的持久化端口，不依赖供应商或 SQL 实现。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol
from uuid import UUID

from service_data_sync.domain.equity import Exchange
from service_data_sync.domain.equity_master import (
    EquityCatalogEntry,
    EquityIdentityResolution,
)


@dataclass(frozen=True, slots=True)
class PublishedEquityCatalog:
    """描述一个交易所目录快照及其可见数据版本。"""

    snapshot_id: UUID
    data_version: UUID
    inserted_count: int
    unchanged_count: int


@dataclass(frozen=True, slots=True)
class PublishedCnAAggregate:
    """描述三所交易所一致 child version 组成的稳定全市场发布。"""

    data_version: UUID
    published_at: datetime


class EquityMasterRepository(Protocol):
    """以完整快照方式发布已确认证券目录和其版本化身份事实。"""

    def publish_catalog(
        self,
        *,
        exchange: Exchange,
        target_date: date,
        entries: tuple[EquityCatalogEntry, ...],
        provider_id: str,
        source_payload_sha256: str,
        raw_uri: str,
        observed_at: datetime,
        upstream_source: str | None,
        adapter_version: str,
        schema_fingerprint: str,
    ) -> PublishedEquityCatalog:
        """保存完整目录证据、确认身份，并仅在业务内容变化时推进版本。"""
        ...

    def publish_cn_a_aggregate(self) -> PublishedCnAAggregate:
        """原子发布当前三所交易所的稳定版本组合，缺任一 child 时拒绝聚合。"""
        ...


class EquityIdentityResolver(Protocol):
    """按双时间标识历史解析证券，供所有事实写入者共享。"""

    def resolve(
        self,
        *,
        exchange: Exchange,
        symbol: str,
        fact_date: date,
        known_at: datetime,
    ) -> EquityIdentityResolution:
        """解析事实日期和知识时间下的唯一身份，返回确定结果而不猜测。"""
        ...

    def resolve_current_open(self, *, exchange: Exchange, symbol: str) -> EquityIdentityResolution:
        """仅解析当前开放且已确认标识，专供当前快照读取而非历史写入。"""
        ...
