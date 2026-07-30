"""运行时市场数据目录与安全拒绝的强类型读取器基线实现。

目录把外部请求的 `schema`、实体、日期维度、质量状态和字段白名单映射为受控读取契约；
未登记数据集、未知字段或缺少精确分区的请求一律拒绝。它不直接读取表数据，而是为
`SQLAlchemy` 读取仓储提供可验证描述，防止调用方扩展为全市场扫描或来源字段检索。
"""

from __future__ import annotations

from collections.abc import Sequence

from service_data_sync.application.etf.query_contract import assert_etf_v2_query_contract
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
    filter_operators: tuple[str, ...] = (),
    sortable: bool = False,
) -> MarketDataFieldDescriptor:
    """构造所有 P0 dataset 共享的字段描述，避免字段能力在目录和 reader 间漂移。"""
    return MarketDataFieldDescriptor(
        name=name,
        logical_type=logical_type,
        nullable=nullable,
        selectable=True,
        unit=unit,
        filter_operators=filter_operators or (("EQ", "IN") if filterable else ()),
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
        visibility_modes=("CURRENT",),
        fields=fields,
        filters=filters,
        allowed_sort_fields=tuple(item.name for item in fields if item.sortable),
        sources=(_SOURCE,),
        methodologies=(_METHODOLOGY,),
        max_range_days=max_range_days,
    )


def _etf_v2_descriptor(
    *,
    code: str,
    title: str,
    time_dimension: str,
    fields: tuple[MarketDataFieldDescriptor, ...],
    filters: tuple[MarketDataFilterDescriptor, ...],
    sources: tuple[MarketDataSourceDescriptor, ...],
    max_range_days: int = 366,
) -> MarketDataDatasetDescriptor:
    """构造 ETF 中心真实同步链路使用的 v2 typed 契约，publication 缺失时保持不可读。"""
    return MarketDataDatasetDescriptor(
        code=code,
        schema_version=2,
        title=title,
        domain="ETF",
        priority="P0",
        availability="DISABLED",
        availability_reason="尚无满足质量门禁的当前 publication。",
        allowed_time_dimensions=(time_dimension,),
        visibility_modes=("CURRENT",),
        fields=fields,
        filters=filters,
        allowed_sort_fields=tuple(item.name for item in fields if item.sortable),
        sources=sources,
        methodologies=(
            {"code": "etf-reported-source-contract", "version": "1", "kind": "REPORTED"},
        ),
        max_range_days=max_range_days,
    )


_ETF_DIRECTORY_SOURCES = (
    MarketDataSourceDescriptor(
        source_ref="src_sse_etf_directory",
        publisher="上海证券交易所",
        source_dataset="ETF 专用目录",
        authoritative=True,
        redistribution="INTERNAL_ONLY",
        coverage_note="仅当前快照；历史 observationDate 返回显式不可用，不回退或冒充历史目录。",
    ),
    MarketDataSourceDescriptor(
        source_ref="src_szse_fund_directory",
        publisher="深圳证券交易所",
        source_dataset="基金产品目录",
        authoritative=True,
        redistribution="INTERNAL_ONLY",
        coverage_note="仅当前快照，且只接收来源字段明确标记为 ETF 的产品。",
    ),
)
_ETF_TENCENT_BAR_SOURCE = (
    MarketDataSourceDescriptor(
        source_ref="src_tencent_etf_kline",
        publisher="腾讯证券",
        source_dataset="证券未复权日线",
        authoritative=False,
        redistribution="INTERNAL_ONLY",
        coverage_note="成交量口径为股，成交额口径为人民币元。",
    ),
)
_ETF_EASTMONEY_NAV_SOURCE = (
    MarketDataSourceDescriptor(
        source_ref="src_eastmoney_etf_nav",
        publisher="东方财富",
        source_dataset="基金历史净值与申赎状态",
        authoritative=False,
        redistribution="INTERNAL_ONLY",
        coverage_note="净值终态未披露；交易状态未披露，不从申赎状态推断。",
    ),
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
    _etf_v2_descriptor(
        code="fund.etf.profile.reported",
        title="ETF 产品目录与上市资料",
        time_dimension="EFFECTIVE_AT",
        fields=(
            _field("etfEntityRef", "ENTITY_REF", filterable=True, sortable=True),
            _field("exchange", "CODE", filterable=True),
            _field(
                "symbol",
                "CODE",
                filter_operators=("EQ", "PREFIX"),
                sortable=True,
            ),
            _field(
                "displayName",
                "STRING",
                filter_operators=("CONTAINS",),
                sortable=True,
            ),
            _field("etfType", "CODE"),
            _field("managementMode", "CODE"),
            _field("managerName", "STRING", nullable=True),
            _field("custodianName", "STRING", nullable=True),
            _field("listedOn", "DATE", nullable=True),
            _field("delistedOn", "DATE", nullable=True),
            _field("listingStatus", "CODE", filterable=True),
            _field("quoteCurrency", "CODE"),
            _field("navCurrency", "CODE"),
            _field("sourceTimePrecision", "CODE"),
        ),
        filters=(
            MarketDataFilterDescriptor("etfEntityRef", ("EQ", "IN"), max_values=500),
            MarketDataFilterDescriptor("exchange", ("EQ", "IN"), max_values=1),
            MarketDataFilterDescriptor("symbol", ("EQ", "PREFIX"), max_values=1),
            MarketDataFilterDescriptor("displayName", ("CONTAINS",), max_values=1),
            MarketDataFilterDescriptor("listingStatus", ("EQ", "IN")),
        ),
        sources=_ETF_DIRECTORY_SOURCES,
    ),
    _etf_v2_descriptor(
        code="fund.etf.bar.1d.reported",
        title="ETF 未复权日行情",
        time_dimension="TRADE_DATE",
        fields=(
            _field("tradeDate", "DATE", sortable=True),
            _field("etfEntityRef", "ENTITY_REF", filterable=True),
            _field("open", "DECIMAL_STRING", unit="CNY"),
            _field("high", "DECIMAL_STRING", unit="CNY"),
            _field("low", "DECIMAL_STRING", unit="CNY"),
            _field("close", "DECIMAL_STRING", unit="CNY"),
            _field("volume", "DECIMAL_STRING", unit="SHARE"),
            _field("volumeUnit", "CODE"),
            _field("amount", "DECIMAL_STRING", unit="CNY"),
            _field("currency", "CODE"),
            _field("tradeStatus", "CODE", nullable=True),
            _field("adjustment", "CODE"),
        ),
        filters=(MarketDataFilterDescriptor("etfEntityRef", ("EQ", "IN")),),
        sources=_ETF_TENCENT_BAR_SOURCE,
    ),
    _etf_v2_descriptor(
        code="fund.etf.nav.1d.reported",
        title="ETF 来源报告净值",
        time_dimension="TRADE_DATE",
        fields=(
            _field("navDate", "DATE", sortable=True),
            _field("etfEntityRef", "ENTITY_REF", filterable=True),
            _field("navKind", "CODE", filterable=True),
            _field("nav", "DECIMAL_STRING", unit="CNY"),
            _field("currency", "CODE"),
            _field("finality", "CODE"),
        ),
        filters=(
            MarketDataFilterDescriptor("etfEntityRef", ("EQ", "IN")),
            MarketDataFilterDescriptor("navKind", ("EQ", "IN"), max_values=500),
        ),
        sources=_ETF_EASTMONEY_NAV_SOURCE,
    ),
    _etf_v2_descriptor(
        code="fund.etf.trading_state.reported",
        title="ETF 申购与赎回状态",
        time_dimension="EFFECTIVE_AT",
        fields=(
            _field("etfEntityRef", "ENTITY_REF", filterable=True),
            _field("stateDimension", "CODE", filterable=True),
            _field("state", "CODE", filterable=True),
            _field("effectiveFrom", "DATE", sortable=True),
            _field("effectiveTo", "DATE", nullable=True),
            _field("reason", "STRING", nullable=True),
        ),
        filters=(
            MarketDataFilterDescriptor("etfEntityRef", ("EQ", "IN")),
            MarketDataFilterDescriptor("stateDimension", ("EQ", "IN"), max_values=500),
            MarketDataFilterDescriptor("state", ("EQ", "IN"), max_values=500),
        ),
        sources=_ETF_EASTMONEY_NAV_SOURCE,
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
        if descriptor.domain == "ETF" and descriptor.schema_version == 2:
            _assert_etf_v2_boundary(request)
        selected = {field.name for field in descriptor.fields if field.selectable}
        if not set(request.fields) <= selected:
            raise MarketDataRequestValidationError("requested field is not selectable")
        allowed_filters = {item.field: item for item in descriptor.filters}
        for item in request.filters:
            if (
                item.field not in allowed_filters
                or item.operator not in allowed_filters[item.field].operators
                or len(item.values) > allowed_filters[item.field].max_values
            ):
                raise MarketDataRequestValidationError("filter is not allowed for dataset")
        if not set(field for field, _direction in request.sort) <= set(
            descriptor.allowed_sort_fields
        ) or any(direction not in {"ASC", "DESC"} for _field, direction in request.sort):
            raise MarketDataRequestValidationError("sort is not allowed for dataset")
        if request.time.get("dimension") not in descriptor.allowed_time_dimensions:
            raise MarketDataRequestValidationError("time dimension is not allowed for dataset")
        if request.visibility.get("mode") not in descriptor.visibility_modes:
            raise MarketDataRequestValidationError("visibility mode is not allowed for dataset")


def _assert_etf_v2_boundary(request: MarketDataQuery) -> None:
    """调用服务内单一 ETF v2 查询契约，保持 catalog 与 HTTP 入口一致。"""
    assert_etf_v2_query_contract(request)
