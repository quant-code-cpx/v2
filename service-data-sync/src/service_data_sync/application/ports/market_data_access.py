"""0028 内部市场数据访问的运行时目录与强类型查询端口。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True, slots=True)
class MarketDataFieldDescriptor:
    """描述一个可投影业务字段及其固定逻辑类型、单位和筛选能力。"""

    name: str
    logical_type: str
    nullable: bool
    selectable: bool
    unit: str | None
    filter_operators: tuple[str, ...] = ()
    sortable: bool = False


@dataclass(frozen=True, slots=True)
class MarketDataFilterDescriptor:
    """描述一个可过滤字段允许的有限运算符，防止请求成为自由表达式。"""

    field: str
    operators: tuple[str, ...]
    max_values: int = 500


@dataclass(frozen=True, slots=True)
class MarketDataSourceDescriptor:
    """描述发布态记录可引用的来源，而不暴露 Adapter、凭据或原始对象地址。"""

    source_ref: str
    publisher: str
    source_dataset: str
    authoritative: bool
    redistribution: str
    coverage_note: str | None = None


@dataclass(frozen=True, slots=True)
class MarketDataDatasetDescriptor:
    """描述可发现的稳定 dataset 契约，不向消费者泄漏物理表或 Provider 实现。"""

    code: str
    schema_version: int
    title: str
    domain: str
    priority: str
    availability: str
    allowed_time_dimensions: tuple[str, ...]
    visibility_modes: tuple[str, ...]
    fields: tuple[MarketDataFieldDescriptor, ...]
    filters: tuple[MarketDataFilterDescriptor, ...]
    allowed_sort_fields: tuple[str, ...]
    sources: tuple[MarketDataSourceDescriptor, ...]
    methodologies: tuple[Mapping[str, str], ...]
    max_range_days: int | None = None
    max_identifiers: int = 100
    availability_reason: str | None = None

    def __post_init__(self) -> None:
        """校验目录元数据的封闭枚举，避免错误的描述被注册为可消费契约。"""
        if not self.code.strip() or self.schema_version <= 0 or not self.title.strip():
            raise ValueError("market dataset identity is invalid")
        if self.domain not in {
            "INDEX",
            "ETF",
            "MARGIN",
            "STOCK_CONNECT",
            "BUSINESS_COMPOSITION",
            "CORPORATE_EVENT",
            "TRADING_EVENT",
            "DERIVATIVE",
            "UNKNOWN",
        }:
            raise ValueError("market dataset domain is invalid")
        if self.priority not in {"P0", "P1", "P2"}:
            raise ValueError("market dataset priority is invalid")
        if self.availability not in {"AVAILABLE", "DEGRADED", "DISABLED", "UNKNOWN"}:
            raise ValueError("market dataset availability is invalid")
        if not self.allowed_time_dimensions or not self.visibility_modes:
            raise ValueError("market dataset temporal contract is required")
        if not self.fields or not self.sources or not self.methodologies:
            raise ValueError("market dataset descriptor is incomplete")
        if self.max_range_days is not None and self.max_range_days <= 0:
            raise ValueError("market dataset range limit is invalid")
        if not 1 <= self.max_identifiers <= 100:
            raise ValueError("market dataset identifier limit is invalid")


@dataclass(frozen=True, slots=True)
class MarketDataFilter:
    """表示一个经过协议校验的字段过滤器，而非可拼接到 SQL 的表达式。"""

    field: str
    operator: str
    values: tuple[str | int | bool, ...]


@dataclass(frozen=True, slots=True)
class MarketDataQuery:
    """表示一次只读取一个 dataset 的规范化、可签名消费者请求。"""

    dataset_code: str
    schema_version: int
    business_scope: str
    identity: Mapping[str, object] | None
    time: Mapping[str, object]
    visibility: Mapping[str, object]
    selection: Mapping[str, object]
    fields: tuple[str, ...]
    filters: tuple[MarketDataFilter, ...]
    sort: tuple[tuple[str, str], ...]
    limit: int
    request_fingerprint: str


@dataclass(frozen=True, slots=True)
class MarketDataQueryPage:
    """表示同一 immutable data version 中的一页 typed record 与发布元数据。"""

    data_version: UUID
    published_at: datetime
    knowledge_cutoff: datetime
    public_usable_at: datetime
    quality_status: str
    completeness: str
    items: tuple[Mapping[str, object], ...]
    next_position: str | None
    methodology: Mapping[str, str] = field(
        default_factory=lambda: {"code": "unknown", "version": "1", "kind": "UNKNOWN"}
    )
    sources: tuple[MarketDataSourceDescriptor, ...] = ()
    effective_from: datetime | None = None
    effective_to: datetime | None = None
    coverage: Mapping[str, object] = field(
        default_factory=lambda: {"from": None, "to": None, "pitCoverage": "UNKNOWN", "gaps": []}
    )
    warnings: tuple[str, ...] = ()
    disclaimers: tuple[str, ...] = ()


class MarketDataAccessUnavailable(RuntimeError):
    """表示 dataset 尚无可消费的合格 publication，调用方必须 fail-closed。"""


class MarketDataDatasetNotFound(LookupError):
    """表示代码或 schema version 不在运行时目录，不能用 503 隐藏调用错误。"""


class MarketDataRequestValidationError(ValueError):
    """表示请求虽为 JSON 但违反某个 dataset 的已冻结 typed 约束。"""


class MarketDataAccessRepository(Protocol):
    """服务端只经此端口读取发布态 dataset，绝不读取 raw、隔离或候选记录。"""

    def search_datasets(
        self,
        *,
        priorities: frozenset[str],
        availability: frozenset[str],
        query: str | None,
    ) -> Sequence[MarketDataDatasetDescriptor]:
        """返回调用方可发现的目录描述，顺序必须稳定且不依赖物理表名。"""
        ...

    def query(self, *, request: MarketDataQuery, after: str | None) -> MarketDataQueryPage:
        """按 typed reader 查询一页，返回固定 data version 及可续页位置。"""
        ...
