"""0028 运行时市场数据目录与 fail-closed typed reader 基线实现。"""

from __future__ import annotations

from collections.abc import Sequence

from service_data_sync.application.ports.market_data_access import (
    MarketDataAccessRepository,
    MarketDataAccessUnavailable,
    MarketDataDatasetDescriptor,
    MarketDataDatasetNotFound,
    MarketDataFieldDescriptor,
    MarketDataFilterDescriptor,
    MarketDataQuery,
    MarketDataQueryPage,
    MarketDataRequestValidationError,
    MarketDataSourceDescriptor,
)

_SOURCE = MarketDataSourceDescriptor(
    source_ref="src_pending_qualification",
    publisher="待准入权威来源",
    source_dataset="P0 source contract pending qualification",
    authoritative=True,
    redistribution="UNKNOWN",
    coverage_note="真实来源、许可、连续探针和 shadow 验收尚未完成。",
)
_METHODOLOGY = {"code": "source-contract", "version": "1", "kind": "REPORTED"}
_VISIBILITY = ("CURRENT", "PUBLIC_PIT")


def _field(
    name: str,
    logical_type: str,
    *,
    unit: str | None = None,
    nullable: bool = False,
    filterable: bool = False,
    sortable: bool = False,
) -> MarketDataFieldDescriptor:
    """构造所有 P0 dataset 共享的字段描述，避免字段能力在目录和 reader 间漂移。"""
    return MarketDataFieldDescriptor(
        name=name,
        logical_type=logical_type,
        nullable=nullable,
        selectable=True,
        unit=unit,
        filter_operators=("EQ", "IN") if filterable else (),
        sortable=sortable,
    )


def _descriptor(
    *,
    code: str,
    title: str,
    domain: str,
    time_dimension: str,
    fields: tuple[MarketDataFieldDescriptor, ...],
    filters: tuple[MarketDataFilterDescriptor, ...],
    max_range_days: int = 366,
) -> MarketDataDatasetDescriptor:
    """构造尚未通过真实发布门禁的 P0 描述；可发现不代表可读。"""
    return MarketDataDatasetDescriptor(
        code=code,
        schema_version=1,
        title=title,
        domain=domain,
        priority="P0",
        availability="DISABLED",
        availability_reason="等待来源许可、连续探针、PIT 和 shadow 质量门禁。",
        allowed_time_dimensions=(time_dimension,),
        visibility_modes=_VISIBILITY,
        fields=fields,
        filters=filters,
        allowed_sort_fields=tuple(item.name for item in fields if item.sortable),
        sources=(_SOURCE,),
        methodologies=(_METHODOLOGY,),
        max_range_days=max_range_days,
    )


_DATASETS: tuple[MarketDataDatasetDescriptor, ...] = (
    _descriptor(
        code="index.constituent.membership.reported",
        title="指数成分正式生效关系",
        domain="INDEX",
        time_dimension="EFFECTIVE_AT",
        fields=(
            _field("indexEntityRef", "ENTITY_REF", filterable=True, sortable=True),
            _field("constituentEntityRef", "ENTITY_REF", filterable=True, sortable=True),
            _field("effectiveFrom", "DATE", sortable=True),
            _field("effectiveTo", "DATE", nullable=True),
        ),
        filters=(
            MarketDataFilterDescriptor("indexEntityRef", ("EQ", "IN")),
            MarketDataFilterDescriptor("constituentEntityRef", ("EQ", "IN")),
        ),
    ),
    _descriptor(
        code="fund.etf.profile.reported",
        title="ETF 主数据和上市生命周期",
        domain="ETF",
        time_dimension="EFFECTIVE_AT",
        fields=(
            _field("etfEntityRef", "ENTITY_REF", filterable=True, sortable=True),
            _field("exchange", "CODE", filterable=True),
            _field("symbol", "CODE", filterable=True, sortable=True),
            _field("listingStatus", "CODE", filterable=True),
        ),
        filters=(
            MarketDataFilterDescriptor("etfEntityRef", ("EQ", "IN")),
            MarketDataFilterDescriptor("exchange", ("EQ", "IN")),
        ),
    ),
    _descriptor(
        code="fund.etf.bar.1d.reported",
        title="ETF 未复权日行情",
        domain="ETF",
        time_dimension="TRADE_DATE",
        fields=(
            _field("tradeDate", "DATE", filterable=True, sortable=True),
            _field("etfEntityRef", "ENTITY_REF", filterable=True, sortable=True),
            _field("close", "DECIMAL_STRING", unit="CNY"),
            _field("volume", "DECIMAL_STRING", unit="SHARE"),
        ),
        filters=(
            MarketDataFilterDescriptor("tradeDate", ("EQ", "GTE", "LTE", "RANGE")),
            MarketDataFilterDescriptor("etfEntityRef", ("EQ", "IN")),
        ),
    ),
    _descriptor(
        code="fund.etf.nav.1d.reported",
        title="ETF 单位净值",
        domain="ETF",
        time_dimension="TRADE_DATE",
        fields=(
            _field("navDate", "DATE", filterable=True, sortable=True),
            _field("etfEntityRef", "ENTITY_REF", filterable=True, sortable=True),
            _field("navKind", "CODE", filterable=True),
            _field("nav", "DECIMAL_STRING", unit="CNY"),
        ),
        filters=(
            MarketDataFilterDescriptor("navDate", ("EQ", "GTE", "LTE", "RANGE")),
            MarketDataFilterDescriptor("etfEntityRef", ("EQ", "IN")),
        ),
    ),
    _descriptor(
        code="fund.etf.trading_state.reported",
        title="ETF 日级交易、申购和赎回状态",
        domain="ETF",
        time_dimension="EFFECTIVE_AT",
        fields=(
            _field("etfEntityRef", "ENTITY_REF", filterable=True, sortable=True),
            _field("stateDimension", "CODE", filterable=True),
            _field("state", "CODE", filterable=True),
            _field("effectiveFrom", "DATE", sortable=True),
        ),
        filters=(
            MarketDataFilterDescriptor("etfEntityRef", ("EQ", "IN")),
            MarketDataFilterDescriptor("stateDimension", ("EQ", "IN")),
        ),
    ),
    _descriptor(
        code="market.margin.market.1d.reported",
        title="融资融券市场汇总",
        domain="MARGIN",
        time_dimension="TRADE_DATE",
        fields=(
            _field("tradeDate", "DATE", filterable=True, sortable=True),
            _field("venueEntityRef", "ENTITY_REF", filterable=True, sortable=True),
            _field("marginBalance", "DECIMAL_STRING", unit="CNY"),
            _field("shortBalance", "DECIMAL_STRING", unit="CNY"),
        ),
        filters=(
            MarketDataFilterDescriptor("tradeDate", ("EQ", "GTE", "LTE", "RANGE")),
            MarketDataFilterDescriptor("venueEntityRef", ("EQ", "IN")),
        ),
    ),
    _descriptor(
        code="market.margin.security.1d.reported",
        title="融资融券证券日明细",
        domain="MARGIN",
        time_dimension="TRADE_DATE",
        fields=(
            _field("tradeDate", "DATE", filterable=True, sortable=True),
            _field("equityEntityRef", "ENTITY_REF", filterable=True, sortable=True),
            _field("financingBalance", "DECIMAL_STRING", unit="CNY", nullable=True),
            _field("lendingBalanceQty", "DECIMAL_STRING", nullable=True),
        ),
        filters=(
            MarketDataFilterDescriptor("tradeDate", ("EQ", "GTE", "LTE", "RANGE")),
            MarketDataFilterDescriptor("venueEntityRef", ("EQ", "IN")),
            MarketDataFilterDescriptor("equityEntityRef", ("EQ", "IN")),
        ),
    ),
    _descriptor(
        code="market.margin.eligibility.reported",
        title="融资融券标的资格",
        domain="MARGIN",
        time_dimension="EFFECTIVE_AT",
        fields=(
            _field("equityEntityRef", "ENTITY_REF", filterable=True, sortable=True),
            _field("eligibilityStatus", "CODE", filterable=True),
            _field("effectiveFrom", "DATE", filterable=True, sortable=True),
            _field("effectiveTo", "DATE", nullable=True),
        ),
        filters=(
            MarketDataFilterDescriptor("venueEntityRef", ("EQ", "IN")),
            MarketDataFilterDescriptor("equityEntityRef", ("EQ", "IN")),
            MarketDataFilterDescriptor("eligibilityStatus", ("EQ", "IN")),
        ),
    ),
    _descriptor(
        code="market.stock_connect.market_stat.reported",
        title="沪深港通官方日终通道统计",
        domain="STOCK_CONNECT",
        time_dimension="TRADE_DATE",
        fields=(
            _field("tradeDate", "DATE", filterable=True, sortable=True),
            _field("channel", "CODE", filterable=True, sortable=True),
            _field("direction", "CODE", filterable=True, sortable=True),
            _field("turnover", "DECIMAL_STRING", unit="CNY"),
            _field("netBuy", "DECIMAL_STRING", unit="CNY", nullable=True),
        ),
        filters=(
            MarketDataFilterDescriptor("tradeDate", ("EQ", "GTE", "LTE", "RANGE")),
            MarketDataFilterDescriptor("channel", ("EQ", "IN")),
            MarketDataFilterDescriptor("direction", ("EQ", "IN")),
        ),
    ),
    _descriptor(
        code="market.stock_connect.active_security.snapshot",
        title="沪深港通日终活跃证券",
        domain="STOCK_CONNECT",
        time_dimension="TRADE_DATE",
        fields=(
            _field("tradeDate", "DATE", filterable=True, sortable=True),
            _field("channel", "CODE", filterable=True, sortable=True),
            _field("direction", "CODE", filterable=True, sortable=True),
            _field("instrumentEntityRef", "ENTITY_REF", filterable=True, sortable=True),
            _field("rank", "INTEGER", sortable=True),
            _field("turnover", "DECIMAL_STRING", unit="CNY", nullable=True),
        ),
        filters=(
            MarketDataFilterDescriptor("tradeDate", ("EQ", "GTE", "LTE", "RANGE")),
            MarketDataFilterDescriptor("channel", ("EQ", "IN")),
            MarketDataFilterDescriptor("direction", ("EQ", "IN")),
            MarketDataFilterDescriptor("instrumentEntityRef", ("EQ", "IN")),
        ),
    ),
    _descriptor(
        code="equity.corporate_disclosure.document.reported",
        title="上市公司公告目录",
        domain="CORPORATE_EVENT",
        time_dimension="EVENT_DATE",
        fields=(
            _field("documentRef", "CODE", filterable=True, sortable=True),
            _field("equityEntityRef", "ENTITY_REF", filterable=True, sortable=True),
            _field("announcedOn", "DATE", sortable=True),
            _field("category", "CODE", filterable=True),
        ),
        filters=(
            MarketDataFilterDescriptor("equityEntityRef", ("EQ", "IN")),
            MarketDataFilterDescriptor("category", ("EQ", "IN")),
        ),
    ),
    _descriptor(
        code="equity.corporate_event.earnings.reported",
        title="上市公司业绩预告与快报",
        domain="CORPORATE_EVENT",
        time_dimension="EVENT_DATE",
        fields=(
            _field("eventRef", "CODE", filterable=True, sortable=True),
            _field("equityEntityRef", "ENTITY_REF", filterable=True, sortable=True),
            _field("announcementAt", "DATE", sortable=True),
            _field("eventKind", "CODE", filterable=True),
        ),
        filters=(
            MarketDataFilterDescriptor("equityEntityRef", ("EQ", "IN")),
            MarketDataFilterDescriptor("eventKind", ("EQ", "IN")),
        ),
    ),
    _descriptor(
        code="equity.block_trade.execution.reported",
        title="大宗交易逐笔明细",
        domain="TRADING_EVENT",
        time_dimension="TRADE_DATE",
        fields=(
            _field("tradeDate", "DATE", filterable=True, sortable=True),
            _field("equityEntityRef", "ENTITY_REF", filterable=True, sortable=True),
            _field("price", "DECIMAL_STRING", unit="CNY"),
            _field("quantity", "DECIMAL_STRING", unit="SHARE"),
        ),
        filters=(
            MarketDataFilterDescriptor("tradeDate", ("EQ", "GTE", "LTE", "RANGE")),
            MarketDataFilterDescriptor("equityEntityRef", ("EQ", "IN")),
        ),
    ),
    _descriptor(
        code="equity.dragon_tiger.disclosure.reported",
        title="龙虎榜披露与席位明细",
        domain="TRADING_EVENT",
        time_dimension="TRADE_DATE",
        fields=(
            _field("tradeDate", "DATE", filterable=True, sortable=True),
            _field("equityEntityRef", "ENTITY_REF", filterable=True, sortable=True),
            _field("reasonCode", "CODE", filterable=True),
            _field("netAmount", "DECIMAL_STRING", unit="CNY"),
        ),
        filters=(
            MarketDataFilterDescriptor("tradeDate", ("EQ", "GTE", "LTE", "RANGE")),
            MarketDataFilterDescriptor("equityEntityRef", ("EQ", "IN")),
            MarketDataFilterDescriptor("reasonCode", ("EQ", "IN")),
        ),
    ),
    _descriptor(
        code="derivative.bar.1d.reported",
        title="真实衍生品合约日行情",
        domain="DERIVATIVE",
        time_dimension="TRADE_DATE",
        fields=(
            _field("tradeDate", "DATE", filterable=True, sortable=True),
            _field("contractEntityRef", "ENTITY_REF", filterable=True, sortable=True),
            _field("close", "DECIMAL_STRING"),
            _field("settlement", "DECIMAL_STRING", nullable=True),
        ),
        filters=(
            MarketDataFilterDescriptor("tradeDate", ("EQ", "GTE", "LTE", "RANGE")),
            MarketDataFilterDescriptor("contractEntityRef", ("EQ", "IN")),
        ),
    ),
)


def default_market_data_descriptors() -> tuple[MarketDataDatasetDescriptor, ...]:
    """返回冻结的 P0 目录副本，读库实现可据真实 publication 动态调整可用状态。"""
    return _DATASETS


class CatalogMarketDataAccessRepository(MarketDataAccessRepository):
    """提供 P0 运行时白名单，并在任何未发布 capability 上严格返回不可用。"""

    def __init__(self, descriptors: Sequence[MarketDataDatasetDescriptor] = _DATASETS) -> None:
        """接收不可变目录，测试可注入小集合而生产使用冻结的 P0 默认集合。"""
        self._descriptors = tuple(sorted(descriptors, key=lambda item: (item.domain, item.code)))
        self._by_key = {(item.code, item.schema_version): item for item in self._descriptors}
        if len(self._by_key) != len(self._descriptors):
            raise ValueError("market-data dataset catalog has duplicate code and schema version")

    def search_datasets(
        self,
        *,
        priorities: frozenset[str],
        availability: frozenset[str],
        query: str | None,
    ) -> Sequence[MarketDataDatasetDescriptor]:
        """按显式筛选返回稳定排序目录；已禁用项仍可发现以支持调用方降级决策。"""
        normalized = query.strip().lower() if query is not None else None
        return tuple(
            descriptor
            for descriptor in self._descriptors
            if (not priorities or descriptor.priority in priorities)
            and (not availability or descriptor.availability in availability)
            and (
                normalized is None
                or normalized in descriptor.code
                or normalized in descriptor.title.lower()
            )
        )

    def query(self, *, request: MarketDataQuery, after: str | None) -> MarketDataQueryPage:
        """先校验所有字段和排序都在目录内，再因未通过 publication 门禁而 fail-closed。"""
        del after
        descriptor = self._by_key.get((request.dataset_code, request.schema_version))
        if descriptor is None:
            raise MarketDataDatasetNotFound(request.dataset_code)
        self._assert_contract(descriptor, request)
        raise MarketDataAccessUnavailable("dataset has no qualified publication")

    @staticmethod
    def _assert_contract(descriptor: MarketDataDatasetDescriptor, request: MarketDataQuery) -> None:
        """执行无数据库依赖的 typed allowlist 校验，避免未来 reader 漏做外层防护。"""
        selected = {field.name for field in descriptor.fields if field.selectable}
        if not set(request.fields) <= selected:
            raise MarketDataRequestValidationError("requested field is not selectable")
        allowed_filters = {item.field: set(item.operators) for item in descriptor.filters}
        for item in request.filters:
            if (
                item.field not in allowed_filters
                or item.operator not in allowed_filters[item.field]
            ):
                raise MarketDataRequestValidationError("filter is not allowed for dataset")
        if not set(field for field, _direction in request.sort) <= set(
            descriptor.allowed_sort_fields
        ):
            raise MarketDataRequestValidationError("sort is not allowed for dataset")
        if request.time.get("dimension") not in descriptor.allowed_time_dimensions:
            raise MarketDataRequestValidationError("time dimension is not allowed for dataset")
        if request.visibility.get("mode") not in descriptor.visibility_modes:
            raise MarketDataRequestValidationError("visibility mode is not allowed for dataset")
