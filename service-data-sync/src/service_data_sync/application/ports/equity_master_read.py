"""已发布证券主数据的 provider-neutral 内部读取端口。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal, Protocol
from uuid import UUID

from service_data_sync.domain.equity import Exchange

PublicationScope = Literal["SSE", "SZSE", "BSE", "CN_A_STABLE"]


class EquityMasterReadUnavailable(RuntimeError):
    """表示 canonical 发布存储暂时不可读取，且不能安全返回降级数据。"""


@dataclass(frozen=True, slots=True)
class EquityMasterPublication:
    """描述一次交易所或三所稳定聚合的不可变读取版本。"""

    data_version: UUID
    published_at: datetime
    effective_as_of: date
    knowledge_cutoff: datetime
    publication_scope: PublicationScope


@dataclass(frozen=True, slots=True)
class TemporalEquityIdentifier:
    """描述证券代码在市场有效时间和系统知识时间中的已确认版本。"""

    exchange: Exchange
    symbol: str
    effective_from: date
    effective_to: date | None
    date_precision: str
    known_from: datetime
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class TemporalEquityName:
    """描述证券名称的双时间版本及其来源观测时间。"""

    value: str
    effective_from: date
    effective_to: date | None
    date_precision: str
    known_from: datetime
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class TemporalEquityListing:
    """描述证券上市生命周期在选定双时间切片中的事实。"""

    status: str
    listed_on: date | None
    delisted_on: date | None
    effective_from: date
    effective_to: date | None
    date_precision: str
    known_from: datetime
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class StoredEquityInstrument:
    """保存内部查询所需锚点，并组合一组已发布双时间投影。"""

    security_id: int
    instrument_id: UUID
    identifier: TemporalEquityIdentifier
    name: TemporalEquityName
    listing: TemporalEquityListing


@dataclass(frozen=True, slots=True)
class StoredListingStatusPeriod:
    """保存一个上市状态知识版本及稳定分页所需的内部版本键。"""

    version_id: UUID
    status: str
    effective_from: date
    effective_to: date | None
    effective_date_precision: str
    known_from: datetime
    known_to: datetime | None
    observed_at: datetime


class EquityMasterReadRepository(Protocol):
    """读取已发布证券主数据，不暴露数据库或供应商实现。"""

    def get_current_publication(
        self, *, exchange: Exchange | None
    ) -> EquityMasterPublication | None:
        """返回单所或三所稳定聚合当前发布；未发布时返回空值。"""
        ...

    def list_instruments(
        self,
        *,
        data_version: UUID,
        exchange: Exchange | None,
        statuses: tuple[str, ...],
        query: str | None,
        as_of: date,
        known_at: datetime,
        after_exchange: Exchange | None,
        after_symbol: str | None,
        after_instrument_id: UUID | None,
        limit: int,
    ) -> Sequence[StoredEquityInstrument]:
        """按交易所、代码和内部 UUID 稳定分页读取一个双时间切片。"""
        ...

    def find_instruments(
        self,
        *,
        data_version: UUID,
        exchange: Exchange,
        symbol: str,
        identifier_as_of: date | None,
        projection_as_of: date,
        known_at: datetime,
        limit: int = 2,
    ) -> Sequence[StoredEquityInstrument]:
        """按路径代码解析至多两只证券，使调用方能显式识别身份冲突。"""
        ...

    def list_listing_status_history(
        self,
        *,
        data_version: UUID,
        exchange: Exchange,
        security_id: int,
        known_at: datetime,
        effective_from: date | None,
        effective_to: date | None,
        after_effective_from: date | None,
        after_known_from: datetime | None,
        after_version_id: UUID | None,
        limit: int,
    ) -> Sequence[StoredListingStatusPeriod]:
        """按有效时间、知识时间和版本 UUID 稳定分页读取生命周期修订。"""
        ...
