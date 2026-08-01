"""已发布证券主数据的中立内部读取端口。

查询只能返回已冻结 `publication` 中的证券、名称和生命周期切片。
它以事实时间和知识时间表达双时间语义，不泄露数据库键或供应商字段。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Literal, Protocol
from uuid import UUID

from service_data_sync.domain.equity import Exchange

PublicationScope = Literal["SSE", "SZSE", "BSE", "CN_A_STABLE"]


class EquityMasterReadUnavailable(RuntimeError):
    """表示 canonical 发布存储暂时不可读取，且不能安全返回降级数据。"""


@dataclass(frozen=True, slots=True)
class EquitySourceAttribution:
    """描述一条已发布字段版本的脱敏来源锚点。

    `source_batch_id` 是可复验的不可变观察标识，但不携带 raw URI、Cookie 或供应商原始字段。
    因此内部和业务 API 可以核对来源一致性，而不会越过 adapter 与证据存储边界。
    """

    source_batch_id: UUID | None = None
    provider_id: str = "unknown"
    upstream_source: str = "unknown"


@dataclass(frozen=True, slots=True)
class EquityPublicationComponent:
    """描述 resolved 主数据 publication 固定采用的一个输入版本。

    目录与生命周期各自的 `knowledge_cutoff` 必须独立保留；调用方不得把它们压缩成
    一个看似统一、实际并不存在的知识时间。
    """

    component_key: str
    dataset: str
    partition_key: str
    data_version: UUID
    published_at: datetime
    effective_as_of: date
    knowledge_cutoff: datetime
    quality_status: str


@dataclass(frozen=True, slots=True)
class EquityMasterPublication:
    """描述一次 resolved 主数据读取版本及其固定的输入血缘。"""

    data_version: UUID
    published_at: datetime
    effective_as_of: date
    publication_scope: PublicationScope
    components: tuple[EquityPublicationComponent, ...]


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
    source: EquitySourceAttribution = field(default_factory=EquitySourceAttribution)
    quality_status: str = "passed"


@dataclass(frozen=True, slots=True)
class TemporalEquityName:
    """描述证券名称的双时间版本及其来源观测时间。"""

    value: str
    effective_from: date
    effective_to: date | None
    date_precision: str
    known_from: datetime
    observed_at: datetime
    source: EquitySourceAttribution = field(default_factory=EquitySourceAttribution)
    quality_status: str = "passed"


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
    evidence_kind: str = "CATALOG"
    source: EquitySourceAttribution = field(default_factory=EquitySourceAttribution)
    quality_status: str = "passed"


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
    evidence_kind: str = "CATALOG"
    source: EquitySourceAttribution = field(default_factory=EquitySourceAttribution)
    quality_status: str = "passed"


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
