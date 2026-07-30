"""ETF v2 typed market-data 查询的服务内单一契约。"""

from __future__ import annotations

import re
from datetime import date
from uuid import UUID

from service_data_sync.application.ports.market_data_access import (
    MarketDataFilter,
    MarketDataQuery,
    MarketDataRequestValidationError,
)

ETF_V2_DATASETS = frozenset(
    {
        "fund.etf.profile.reported",
        "fund.etf.bar.1d.reported",
        "fund.etf.nav.1d.reported",
        "fund.etf.trading_state.reported",
    }
)
ETF_V2_FIELDS = {
    "fund.etf.profile.reported": frozenset(
        {
            "etfEntityRef",
            "exchange",
            "symbol",
            "displayName",
            "etfType",
            "managementMode",
            "managerName",
            "custodianName",
            "listedOn",
            "delistedOn",
            "listingStatus",
            "quoteCurrency",
            "navCurrency",
            "sourceTimePrecision",
        }
    ),
    "fund.etf.bar.1d.reported": frozenset(
        {
            "tradeDate",
            "etfEntityRef",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "volumeUnit",
            "amount",
            "currency",
            "tradeStatus",
            "adjustment",
        }
    ),
    "fund.etf.nav.1d.reported": frozenset(
        {"navDate", "etfEntityRef", "navKind", "nav", "currency", "finality"}
    ),
    "fund.etf.trading_state.reported": frozenset(
        {
            "etfEntityRef",
            "stateDimension",
            "state",
            "effectiveFrom",
            "effectiveTo",
            "reason",
        }
    ),
}
ETF_V2_FILTERS = {
    "fund.etf.profile.reported": {
        "etfEntityRef": frozenset({"EQ", "IN"}),
        "exchange": frozenset({"EQ", "IN"}),
        "symbol": frozenset({"EQ", "PREFIX"}),
        "displayName": frozenset({"CONTAINS"}),
        "listingStatus": frozenset({"EQ", "IN"}),
    },
    "fund.etf.bar.1d.reported": {
        "etfEntityRef": frozenset({"EQ", "IN"}),
    },
    "fund.etf.nav.1d.reported": {
        "etfEntityRef": frozenset({"EQ", "IN"}),
        "navKind": frozenset({"EQ", "IN"}),
    },
    "fund.etf.trading_state.reported": {
        "etfEntityRef": frozenset({"EQ", "IN"}),
        "stateDimension": frozenset({"EQ", "IN"}),
        "state": frozenset({"EQ", "IN"}),
    },
}
ETF_V2_SORT_FIELDS = {
    "fund.etf.profile.reported": frozenset({"symbol", "displayName", "etfEntityRef"}),
    "fund.etf.bar.1d.reported": frozenset({"tradeDate"}),
    "fund.etf.nav.1d.reported": frozenset({"navDate"}),
    "fund.etf.trading_state.reported": frozenset({"effectiveFrom"}),
}


def assert_etf_v2_query_contract(request: MarketDataQuery) -> None:
    """校验 ETF v2 全部可执行边界，避免 HTTP、catalog 与 SQL reader 产生接受差异。"""
    if request.dataset_code not in ETF_V2_DATASETS or request.schema_version != 2:
        return
    if request.business_scope != "ETF":
        raise MarketDataRequestValidationError("ETF v2 requires ETF business scope")
    if request.identity is not None:
        raise MarketDataRequestValidationError("ETF v2 identity must use explicit filters")
    _assert_time(request)
    if request.visibility != {"mode": "CURRENT"}:
        raise MarketDataRequestValidationError("ETF v2 supports CURRENT visibility only")
    _assert_selection(request)
    if (
        not request.fields
        or len(set(request.fields)) != len(request.fields)
        or not set(request.fields) <= ETF_V2_FIELDS[request.dataset_code]
    ):
        raise MarketDataRequestValidationError("ETF v2 fields are invalid")
    filters = _assert_filters(request)
    _assert_required_filters(request, filters)
    if (
        len({field for field, _direction in request.sort}) != len(request.sort)
        or not {field for field, _direction in request.sort}
        <= ETF_V2_SORT_FIELDS[request.dataset_code]
        or any(direction not in {"ASC", "DESC"} for _field, direction in request.sort)
    ):
        raise MarketDataRequestValidationError("ETF v2 sort is invalid")
    maximum = (
        50
        if request.dataset_code == "fund.etf.profile.reported"
        else (
            366
            if request.dataset_code in {"fund.etf.bar.1d.reported", "fund.etf.nav.1d.reported"}
            else 500
        )
    )
    if not 1 <= request.limit <= maximum:
        raise MarketDataRequestValidationError("ETF v2 page limit exceeds dataset maximum")


def _assert_time(request: MarketDataQuery) -> None:
    """校验数据集时间维度、上海时区和一期允许的有界自然日窗口。"""
    if set(request.time) - {"dimension", "from", "to", "timezone"}:
        raise MarketDataRequestValidationError("ETF v2 time is invalid")
    expected_dimension = (
        "TRADE_DATE"
        if request.dataset_code in {"fund.etf.bar.1d.reported", "fund.etf.nav.1d.reported"}
        else "EFFECTIVE_AT"
    )
    if (
        request.time.get("dimension") != expected_dimension
        or request.time.get("timezone", "Asia/Shanghai") != "Asia/Shanghai"
    ):
        raise MarketDataRequestValidationError("ETF v2 time dimension is invalid")
    start_text = request.time.get("from")
    end_text = request.time.get("to")
    if not isinstance(start_text, str) or not isinstance(end_text, str):
        raise MarketDataRequestValidationError("ETF v2 time range is invalid")
    try:
        start = date.fromisoformat(start_text)
        end = date.fromisoformat(end_text)
    except ValueError as error:
        raise MarketDataRequestValidationError("ETF v2 time range is invalid") from error
    if start > end:
        raise MarketDataRequestValidationError("ETF v2 time range is invalid")
    if request.dataset_code == "fund.etf.profile.reported" and start != end:
        raise MarketDataRequestValidationError("ETF v2 profile requires one effective date")
    if request.dataset_code != "fund.etf.profile.reported" and (end - start).days + 1 > 366:
        raise MarketDataRequestValidationError("ETF v2 time range exceeds 366 days")


def _assert_selection(request: MarketDataQuery) -> None:
    """校验 current publication 的质量门和可选精确数据版本。"""
    if set(request.selection) - {"qualityStatuses", "dataVersion"}:
        raise MarketDataRequestValidationError("ETF v2 selection is not allowed")
    statuses = request.selection.get("qualityStatuses")
    if (
        not isinstance(statuses, (list, tuple))
        or not 1 <= len(statuses) <= 2
        or not all(isinstance(status, str) for status in statuses)
        or not set(statuses) <= {"PASSED", "WARNED"}
        or len(set(statuses)) != len(statuses)
    ):
        raise MarketDataRequestValidationError("ETF v2 quality statuses are invalid")
    if "dataVersion" in request.selection:
        try:
            UUID(str(request.selection["dataVersion"]))
        except ValueError as error:
            raise MarketDataRequestValidationError("ETF v2 dataVersion is invalid") from error


def _assert_filters(request: MarketDataQuery) -> dict[str, MarketDataFilter]:
    """校验过滤字段唯一性、运算符、值数量和业务标量，返回按字段索引的过滤器。"""
    filters: dict[str, MarketDataFilter] = {}
    allowed = ETF_V2_FILTERS[request.dataset_code]
    for item in request.filters:
        if item.field in filters:
            raise MarketDataRequestValidationError("ETF v2 filter fields must be unique")
        if item.field not in allowed or item.operator not in allowed[item.field]:
            raise MarketDataRequestValidationError("ETF v2 filter is unsupported")
        if (
            item.operator in {"EQ", "GTE", "LTE", "PREFIX", "CONTAINS"} and len(item.values) != 1
        ) or (item.operator == "RANGE" and len(item.values) != 2):
            raise MarketDataRequestValidationError("ETF v2 filter value count is invalid")
        if (
            not item.values
            or len(item.values) > 500
            or len(set(item.values)) != len(item.values)
            or not all(isinstance(value, str) for value in item.values)
        ):
            raise MarketDataRequestValidationError("ETF v2 filter values must be strings")
        _assert_filter_values(item)
        filters[item.field] = item
    return filters


def _assert_filter_values(item: MarketDataFilter) -> None:
    """按过滤字段校验 UUID、场所、代码、日期和封闭枚举，不从代码前缀推断类别。"""
    values = tuple(str(value) for value in item.values)
    if item.field == "etfEntityRef":
        try:
            tuple(UUID(value) for value in values)
        except ValueError as error:
            raise MarketDataRequestValidationError("ETF entity filter is invalid") from error
    if item.field == "exchange" and not set(values) <= {"SSE", "SZSE"}:
        raise MarketDataRequestValidationError("ETF exchange filter is invalid")
    if item.field == "symbol" and any(
        re.fullmatch(r"[0-9]{6}", value) is None
        if item.operator == "EQ"
        else re.fullmatch(r"[0-9]{1,6}", value) is None
        for value in values
    ):
        raise MarketDataRequestValidationError("ETF symbol filter is invalid")
    if item.field == "displayName" and any(not value.strip() for value in values):
        raise MarketDataRequestValidationError("ETF displayName filter is invalid")
    if item.field == "listingStatus" and not set(values) <= {
        "LISTED",
        "SUSPENDED",
        "DELISTED",
        "UNKNOWN",
    }:
        raise MarketDataRequestValidationError("ETF listingStatus filter is invalid")
    if item.field in {"tradeDate", "navDate"}:
        try:
            tuple(date.fromisoformat(value) for value in values)
        except ValueError as error:
            raise MarketDataRequestValidationError("ETF date filter is invalid") from error
    if item.field == "navKind" and not set(values) <= {"UNIT", "ACCUMULATED"}:
        raise MarketDataRequestValidationError("ETF navKind filter is invalid")
    if item.field == "stateDimension" and not set(values) <= {
        "TRADING",
        "SUBSCRIPTION",
        "REDEMPTION",
    }:
        raise MarketDataRequestValidationError("ETF stateDimension filter is invalid")


def _assert_required_filters(
    request: MarketDataQuery,
    filters: dict[str, MarketDataFilter],
) -> None:
    """要求目录固定单一场所、详情固定单一实体，并让 NAV 显式选择净值类型。"""
    if request.dataset_code == "fund.etf.profile.reported":
        exchange = filters.get("exchange")
        if exchange is None or exchange.operator != "EQ" or len(exchange.values) != 1:
            raise MarketDataRequestValidationError("ETF profile requires one exchange EQ filter")
        return
    entity = filters.get("etfEntityRef")
    if entity is None or entity.operator != "EQ" or len(entity.values) != 1:
        raise MarketDataRequestValidationError("ETF detail requires one etfEntityRef EQ filter")
    if request.dataset_code == "fund.etf.nav.1d.reported" and "navKind" not in filters:
        raise MarketDataRequestValidationError("ETF NAV requires an explicit navKind filter")
