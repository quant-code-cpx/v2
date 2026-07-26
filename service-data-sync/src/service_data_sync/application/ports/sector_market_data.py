"""板块行情的持久化端口；不依赖具体数据源或 SQL 实现。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol
from uuid import UUID

from service_data_sync.domain.sector import (
    SectorBar,
    SectorCatalogEntry,
    SectorIdentifier,
    SectorPeriod,
    SectorScheme,
)


@dataclass(frozen=True, slots=True)
class StoredSector:
    """表示仓储中已有或由行情同步创建的稳定板块身份。"""

    sector_key: int
    sector_id: UUID
    identifier: SectorIdentifier
    name: str | None
    status: str


@dataclass(frozen=True, slots=True)
class PublishedSectorBars:
    """描述一次指定板块和周期的已提交发布。"""

    data_version: UUID
    inserted_count: int
    unchanged_count: int
    sector: StoredSector


@dataclass(frozen=True, slots=True)
class PublishedSectorCatalog:
    """描述一次分类体系目录发布及其可观察的变更计数。"""

    data_version: UUID
    inserted_count: int
    unchanged_count: int


@dataclass(frozen=True, slots=True)
class DatasetPublication:
    """表示一个数据集分区当前且可供读取的发布快照。"""

    data_version: UUID
    published_at: datetime


class SectorMarketDataRepository(Protocol):
    """负责板块日、周、月行情的追加修订、发布和内部读取。"""

    def publish_bars(
        self,
        *,
        identifier: SectorIdentifier,
        period: SectorPeriod,
        bars: Sequence[SectorBar],
        provider_id: str,
        source_payload_sha256: str,
        raw_uri: str,
        observed_at: datetime,
    ) -> PublishedSectorBars:
        """在原始证据已可靠保存后，写入一个周期的不可变行情修订。"""
        ...

    def publish_catalog(
        self,
        *,
        scheme: SectorScheme,
        entries: Sequence[SectorCatalogEntry],
        provider_id: str,
        source_payload_sha256: str,
        raw_uri: str,
        observed_at: datetime,
    ) -> PublishedSectorCatalog:
        """发布一个分类体系的已验证目录，并把条目激活为可读身份。"""
        ...

    def get_sector(self, sector_id: UUID) -> StoredSector | None:
        """按公开 UUID 读取一个板块身份。"""
        ...

    def get_sector_by_identifier(self, identifier: SectorIdentifier) -> StoredSector | None:
        """按分类体系和代码读取一个稳定板块身份。"""
        ...

    def list_active_sectors(
        self,
        *,
        scheme: SectorScheme,
        query: str | None,
        after_code: str | None,
        after_sector_id: UUID | None,
        limit: int,
    ) -> Sequence[StoredSector]:
        """以代码和稳定 UUID 游标顺序读取已发布的板块目录页。"""
        ...

    def get_current_publication(
        self, *, dataset: str, partition_key: str
    ) -> DatasetPublication | None:
        """读取一个数据集分区未被替代的当前发布版本。"""
        ...

    def list_bars(
        self,
        *,
        sector_id: UUID,
        period: SectorPeriod,
        start: date,
        end: date,
    ) -> Sequence[tuple[SectorBar, int, bool]]:
        """读取有界窗口内指定物理周期表的当前修订。"""
        ...
