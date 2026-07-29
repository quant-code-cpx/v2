"""通过 AKShare 提供 ETF、两融、港通、公告、公开交易与衍生品 P0 adapter。"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from math import isnan
from typing import Any
from zoneinfo import ZoneInfo

import akshare as ak

from service_data_sync.application.ports.data_source import (
    ProviderBatch,
    ProviderError,
    ProviderErrorCode,
    SourceRequest,
)

_ETF_MASTER = "fund.etf.master"
_ETF_STATUS = "fund.etf.trading_state"
_ETF_BAR = "fund.etf.bar.1d.raw"
_ETF_NAV = "fund.etf.nav.1d.reported"
_MARGIN_MARKET = "market.margin.market.1d.reported"
_MARGIN_SECURITY = "market.margin.security.1d.reported"
_MARGIN_ELIGIBILITY = "market.margin.eligibility.reported"
_CONNECT_MARKET = "market.stock_connect.market_stat.reported"
_CONNECT_ACTIVE = "market.stock_connect.active_security.snapshot"
_CORPORATE = "corporate.disclosure.earnings.p0"
_DRAGON_TIGER = "market.dragon_tiger.disclosure.1d"
_BLOCK_TRADE = "market.block_trade.execution.1d"
_DERIVATIVE = "derivative.bar.1d.reported"

_CAPABILITIES = frozenset(
    {
        _ETF_MASTER,
        _ETF_STATUS,
        _ETF_BAR,
        _ETF_NAV,
        _MARGIN_MARKET,
        _MARGIN_SECURITY,
        _MARGIN_ELIGIBILITY,
        _CONNECT_MARKET,
        _CONNECT_ACTIVE,
        _CORPORATE,
        _DRAGON_TIGER,
        _BLOCK_TRADE,
        _DERIVATIVE,
    }
)
_SCHEMAS = {
    _ETF_MASTER: "quant-v2.etf-master.v1",
    _ETF_STATUS: "quant-v2.etf-trading-state.v1",
    _ETF_BAR: "quant-v2.etf-daily-bar.v1",
    _ETF_NAV: "quant-v2.etf-nav.v1",
    _MARGIN_MARKET: "quant-v2.margin-market-daily.v1",
    _MARGIN_SECURITY: "quant-v2.margin-security-daily.v1",
    _MARGIN_ELIGIBILITY: "quant-v2.margin-eligibility.v1",
    _CONNECT_MARKET: "quant-v2.stock-connect-market-daily.v1",
    _CONNECT_ACTIVE: "quant-v2.stock-connect-active-security.v1",
    _CORPORATE: "quant-v2.corporate-earnings-events.v1",
    _DRAGON_TIGER: "quant-v2.dragon-tiger-disclosure.v1",
    _BLOCK_TRADE: "quant-v2.block-trade-execution.v1",
    _DERIVATIVE: "quant-v2.derivative-daily-bar.v1",
}
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_ADAPTER_VERSION = "akshare-1.18.78-p0-market-data-v1"


class AkshareP0MarketDataAdapter:
    """将 AKShare P0 可验证字段隔离成一个默认 `akshare` provider。"""

    provider_id = "akshare"

    def __init__(self, *, request_timeout_seconds: int) -> None:
        """保存每次阻塞 SDK 调用可占用的最大墙钟时间。"""
        self._request_timeout_seconds = request_timeout_seconds

    def capabilities(self) -> frozenset[str]:
        """声明全部已完成映射或可安全返回空集的 P0 capability。"""
        return _CAPABILITIES

    async def fetch(self, request: SourceRequest) -> ProviderBatch:
        """调用唯一 AKShare 映射并返回标准 JSON 与仅失败留证所需的内存载荷。"""
        _validate_capability(request.capability)
        parameters = dict(request.parameters)
        try:
            async with asyncio.timeout(self._request_timeout_seconds):
                payload_object, raw_object, upstream_source = await asyncio.to_thread(
                    _fetch_payload,
                    capability=request.capability,
                    parameters=parameters,
                )
        except TimeoutError as error:
            raise ProviderError(
                ProviderErrorCode.UNAVAILABLE,
                "provider request timed out",
                retryable=True,
            ) from error
        except ProviderError:
            raise
        except Exception as error:
            raise ProviderError(
                ProviderErrorCode.UNAVAILABLE,
                "provider request failed",
                retryable=True,
            ) from error
        payload = _json_bytes(payload_object)
        raw_payload = _json_bytes(raw_object)
        return ProviderBatch(
            provider_id=self.provider_id,
            capability=request.capability,
            payload=payload,
            observed_at=datetime.now(UTC),
            content_type=f"application/vnd.{_SCHEMAS[request.capability]}+json",
            raw_payload=raw_payload,
            raw_content_type="application/json",
            upstream_source=upstream_source,
            adapter_version=_ADAPTER_VERSION,
            schema_fingerprint=_schema_fingerprint(raw_object),
        )


def _fetch_payload(
    *, capability: str, parameters: dict[str, str]
) -> tuple[dict[str, Any], dict[str, Any], str]:
    """在 adapter 边界内分派供应商函数，绝不将 AKShare 名称泄漏给应用层。"""
    if capability == _ETF_MASTER:
        return (*_etf_master(parameters), "ths.etf-category")
    if capability == _ETF_STATUS:
        return (*_etf_status(parameters), "eastmoney.etf.nav")
    if capability == _ETF_BAR:
        return (*_etf_bars(parameters), "eastmoney.etf.kline")
    if capability == _ETF_NAV:
        return (*_etf_nav(parameters), "eastmoney.etf.nav")
    if capability == _MARGIN_MARKET:
        return (*_margin_market(parameters), "sse-szse.margin")
    if capability == _MARGIN_SECURITY:
        return (*_margin_security(parameters), "sse-szse.margin")
    if capability == _MARGIN_ELIGIBILITY:
        return (*_margin_eligibility(parameters), "szse.margin-underlying")
    if capability == _CONNECT_MARKET:
        return (*_stock_connect_market(parameters), "eastmoney.stock-connect")
    if capability == _CONNECT_ACTIVE:
        return (*_stock_connect_active(parameters), "akshare.unsupported-stock-connect-active")
    if capability == _CORPORATE:
        return (*_corporate_events(parameters), "eastmoney.earnings")
    if capability == _DRAGON_TIGER:
        return (*_dragon_tiger(parameters), "eastmoney.dragon-tiger")
    if capability == _BLOCK_TRADE:
        return (*_block_trades(parameters), "eastmoney.block-trade")
    return (*_derivative_bars(parameters), "eastmoney.futures")


def _etf_master(parameters: dict[str, str]) -> tuple[dict[str, Any], dict[str, Any]]:
    """读取当前 ETF 行情目录；无历史快照接口时拒绝把当前名单写到过去。"""
    venue = _venue(parameters)
    observation_date = _date_parameter(parameters, "observationDate")
    if observation_date != datetime.now(_SHANGHAI).date():
        return (
            {"schema": _SCHEMAS[_ETF_MASTER], "venue": venue, "profiles": []},
            _raw_empty(
                _ETF_MASTER, parameters, "AKShare ETF category only exposes current snapshot"
            ),
        )
    # `fund_etf_spot_em` 需逐页请求；同花顺 ETF 分类接口一次返回已筛选的 ETF 目录。
    frame = _akshare_frame_or_empty(lambda: ak.fund_etf_category_ths(symbol="ETF"))
    raw_records = _frame_records(frame)
    profiles = []
    for record in raw_records:
        symbol = _security_code(record.get("基金代码") or record.get("代码"))
        if symbol is None or _etf_venue(symbol) != venue:
            continue
        profiles.append(
            {
                "symbol": symbol,
                # AKShare 的目录接口未披露管理方式、管理人和托管人；只保留显式未知。
                "etfType": _optional_text(record.get("基金类型")) or "ETF",
                "managementMode": "UNKNOWN",
                "managerName": None,
                "custodianName": None,
                "establishedOn": None,
                "listedOn": None,
                "delistedOn": None,
                "quoteCurrency": "CNY",
                "navCurrency": "CNY",
                "listingStatus": "LISTED",
                "effectiveFrom": observation_date.isoformat(),
                "sourceTimePrecision": "DATE_ONLY",
            }
        )
    return (
        {"schema": _SCHEMAS[_ETF_MASTER], "venue": venue, "profiles": profiles},
        {"capability": _ETF_MASTER, "parameters": parameters, "records": raw_records},
    )


def _etf_status(parameters: dict[str, str]) -> tuple[dict[str, Any], dict[str, Any]]:
    """从 ETF 历史净值返回申购和赎回状态，交易状态因来源未披露保持缺席。"""
    _, symbol = _etf(parameters)
    start, end = _window(parameters)
    frame = _akshare_frame_or_empty(
        lambda: ak.fund_etf_fund_info_em(
            fund=symbol,
            start_date=start.strftime("%Y%m%d"),
            end_date=end.strftime("%Y%m%d"),
        )
    )
    raw_records = _frame_records(frame)
    statuses: list[dict[str, object]] = []
    for record in raw_records:
        effective_from = _record_date(record, "净值日期")
        if effective_from is None or not start <= effective_from <= end:
            continue
        for field, dimension in (("申购状态", "SUBSCRIPTION"), ("赎回状态", "REDEMPTION")):
            status_code = _optional_text(record.get(field))
            if status_code is not None:
                statuses.append(
                    {
                        "dimension": dimension,
                        "statusCode": status_code,
                        "effectiveFrom": effective_from.isoformat(),
                        "effectiveTo": None,
                        "reason": None,
                    }
                )
    return (
        {"schema": _SCHEMAS[_ETF_STATUS], "etf": _etf_key(parameters), "statuses": statuses},
        {"capability": _ETF_STATUS, "parameters": parameters, "records": raw_records},
    )


def _etf_bars(parameters: dict[str, str]) -> tuple[dict[str, Any], dict[str, Any]]:
    """读取 ETF 未复权日线，成交量保留东财 K 线的手数口径。"""
    _, symbol = _etf(parameters)
    start, end = _window(parameters)
    if parameters.get("priceBasis") != "UNADJUSTED":
        raise _invalid_request("ETF P0 requires UNADJUSTED price basis")
    frame = _akshare_frame_or_empty(
        lambda: ak.fund_etf_hist_em(
            symbol=symbol,
            period="daily",
            start_date=start.strftime("%Y%m%d"),
            end_date=end.strftime("%Y%m%d"),
            adjust="",
        )
    )
    raw_records = _frame_records(frame)
    bars = []
    for record in raw_records:
        trade_date = _record_date(record, "日期")
        if trade_date is None or not start <= trade_date <= end:
            continue
        bars.append(
            {
                "tradeDate": trade_date.isoformat(),
                "open": _required_decimal(record, "开盘"),
                "high": _required_decimal(record, "最高"),
                "low": _required_decimal(record, "最低"),
                "close": _required_decimal(record, "收盘"),
                "volume": _required_decimal(record, "成交量"),
                "volumeUnit": "LOT",
                "amount": _required_decimal(record, "成交额"),
                "currency": "CNY",
                "tradeStatus": None,
            }
        )
    return (
        {
            "schema": _SCHEMAS[_ETF_BAR],
            "etf": _etf_key(parameters),
            "priceBasis": "UNADJUSTED",
            "bars": bars,
        },
        {"capability": _ETF_BAR, "parameters": parameters, "records": raw_records},
    )


def _etf_nav(parameters: dict[str, str]) -> tuple[dict[str, Any], dict[str, Any]]:
    """读取 ETF 单位与累计净值，终态信息未披露时明确写为未知。"""
    _, symbol = _etf(parameters)
    start, end = _window(parameters)
    frame = _akshare_frame_or_empty(
        lambda: ak.fund_etf_fund_info_em(
            fund=symbol,
            start_date=start.strftime("%Y%m%d"),
            end_date=end.strftime("%Y%m%d"),
        )
    )
    raw_records = _frame_records(frame)
    navs: list[dict[str, object]] = []
    for record in raw_records:
        nav_date = _record_date(record, "净值日期")
        if nav_date is None or not start <= nav_date <= end:
            continue
        for field, nav_kind in (("单位净值", "UNIT"), ("累计净值", "ACCUMULATED")):
            value = _decimal_text(record.get(field))
            if value is not None:
                navs.append(
                    {
                        "navDate": nav_date.isoformat(),
                        "navKind": nav_kind,
                        "nav": value,
                        "currency": "CNY",
                        "finality": "UNKNOWN",
                    }
                )
    return (
        {"schema": _SCHEMAS[_ETF_NAV], "etf": _etf_key(parameters), "navs": navs},
        {"capability": _ETF_NAV, "parameters": parameters, "records": raw_records},
    )


def _margin_market(parameters: dict[str, str]) -> tuple[dict[str, Any], dict[str, Any]]:
    """读取沪深场所两融汇总；深市接口按单交易日逐日请求。"""
    venue = _venue(parameters)
    start, end = _window(parameters)
    if venue == "SSE":
        frames = [
            (
                None,
                _akshare_frame_or_empty(
                    lambda: ak.stock_margin_sse(start.strftime("%Y%m%d"), end.strftime("%Y%m%d"))
                ),
            )
        ]
    else:
        frames = [
            (
                day,
                _akshare_frame_or_empty(
                    lambda day=day: ak.stock_margin_szse(day.strftime("%Y%m%d"))
                ),
            )
            for day in _days(start, end)
        ]
    raw_records = _frames_raw(frames)
    records: list[dict[str, object]] = []
    for default_date, frame in frames:
        for row in _frame_records(frame):
            trade_date = _record_date(row, "信用交易日期") or default_date
            if trade_date is None or not start <= trade_date <= end:
                continue
            records.append(
                {
                    "tradeDate": trade_date.isoformat(),
                    "financingBalance": _decimal_text(row.get("融资余额")),
                    "financingBuyAmount": _decimal_text(row.get("融资买入额")),
                    "financingRepaymentAmount": None,
                    "lendingBalanceAmount": _decimal_text(
                        row.get("融券余量金额") or row.get("融券余额")
                    ),
                    "lendingBalanceQty": _decimal_text(row.get("融券余量")),
                    "lendingSellQty": _decimal_text(row.get("融券卖出量")),
                    "lendingRepaymentQty": None,
                    "totalBalance": _decimal_text(row.get("融资融券余额")),
                    "currency": "CNY",
                    "quantityUnit": "SHARES",
                }
            )
    return (
        {
            "schema": _SCHEMAS[_MARGIN_MARKET],
            "venue": venue,
            "valueKind": "REPORTED",
            "records": records,
        },
        {"capability": _MARGIN_MARKET, "parameters": parameters, "records": raw_records},
    )


def _margin_security(parameters: dict[str, str]) -> tuple[dict[str, Any], dict[str, Any]]:
    """读取两融证券明细，深市未披露的融资偿还额保持空值并说明原因。"""
    venue = _venue(parameters)
    start, end = _window(parameters)
    function = ak.stock_margin_detail_sse if venue == "SSE" else ak.stock_margin_detail_szse
    frames = [
        (day, _akshare_frame_or_empty(lambda day=day: function(day.strftime("%Y%m%d"))))
        for day in _days(start, end)
    ]
    raw_records = _frames_raw(frames)
    records: list[dict[str, object]] = []
    for default_date, frame in frames:
        for row in _frame_records(frame):
            security_code = _security_code(row.get("标的证券代码") or row.get("证券代码"))
            trade_date = _record_date(row, "信用交易日期") or default_date
            if security_code is None or trade_date is None:
                continue
            records.append(
                {
                    "securityCode": security_code,
                    "tradeDate": trade_date.isoformat(),
                    "financingBalance": _decimal_text(row.get("融资余额")),
                    "financingBuyAmount": _decimal_text(row.get("融资买入额")),
                    "financingRepaymentReported": (
                        _decimal_text(row.get("融资偿还额")) if venue == "SSE" else None
                    ),
                    "financingRepaymentDerived": None,
                    "lendingBalanceQty": _decimal_text(row.get("融券余量")),
                    "quantityUnit": "SHARES",
                    "currency": "CNY",
                    "nullReason": "NOT_REPORTED_BY_SOURCE" if venue == "SZSE" else None,
                }
            )
    return (
        {
            "schema": _SCHEMAS[_MARGIN_SECURITY],
            "venue": venue,
            "valueKind": "REPORTED",
            "records": records,
        },
        {"capability": _MARGIN_SECURITY, "parameters": parameters, "records": raw_records},
    )


def _margin_eligibility(parameters: dict[str, str]) -> tuple[dict[str, Any], dict[str, Any]]:
    """读取深市当日标的观察名单；AKShare 未提供上交所等价接口时返回空集。"""
    venue = _venue(parameters)
    start, end = _window(parameters)
    if venue == "SSE":
        return (
            {"schema": _SCHEMAS[_MARGIN_ELIGIBILITY], "venue": venue, "records": []},
            _raw_empty(
                _MARGIN_ELIGIBILITY, parameters, "AKShare has no SSE underlying-security endpoint"
            ),
        )
    frames = [
        (
            day,
            _akshare_frame_or_empty(
                lambda day=day: ak.stock_margin_underlying_info_szse(day.strftime("%Y%m%d"))
            ),
        )
        for day in _days(start, end)
    ]
    raw_records = _frames_raw(frames)
    records: list[dict[str, object]] = []
    for observation_date, frame in frames:
        for row in _frame_records(frame):
            security_code = _security_code(row.get("证券代码"))
            if security_code is not None:
                records.append(
                    {
                        "securityCode": security_code,
                        "status": "ELIGIBLE",
                        "effectiveFrom": observation_date.isoformat(),
                        "effectiveTo": None,
                        "announcementOn": None,
                        "evidenceBasis": "OBSERVED_LIST",
                    }
                )
    return (
        {"schema": _SCHEMAS[_MARGIN_ELIGIBILITY], "venue": venue, "records": records},
        {"capability": _MARGIN_ELIGIBILITY, "parameters": parameters, "records": raw_records},
    )


def _stock_connect_market(
    parameters: dict[str, str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """读取通道历史统计；东财返回亿元时统一换算为 canonical CNY。"""
    channel, direction = _channel(parameters)
    start, end = _window(parameters)
    frame = _akshare_frame_or_empty(
        lambda: ak.stock_hsgt_hist_em(symbol=_akshare_connect_symbol(channel, direction))
    )
    raw_records = _frame_records(frame)
    records: list[dict[str, object]] = []
    for row in raw_records:
        trade_date = _record_date(row, "日期")
        if trade_date is None or not start <= trade_date <= end:
            continue
        buy = _cny_from_yi(row.get("买入成交额"))
        sell = _cny_from_yi(row.get("卖出成交额"))
        net = _cny_from_yi(row.get("当日成交净买额"))
        quota = _cny_from_yi(row.get("当日余额"))
        records.append(
            {
                "tradeDate": trade_date.isoformat(),
                "buyAmount": buy,
                "sellAmount": sell,
                # 该接口没有独立的当日成交总额列，禁止由买卖额相加生成。
                "turnoverAmount": None,
                "netBuyAmount": net,
                "quotaBalance": quota,
                "currency": "CNY",
                "availabilityStatus": "COMPLETE" if all((buy, sell, net)) else "PARTIAL",
            }
        )
    return (
        {
            "schema": _SCHEMAS[_CONNECT_MARKET],
            "channel": channel,
            "direction": direction,
            "valueKind": "REPORTED",
            "records": records,
        },
        {"capability": _CONNECT_MARKET, "parameters": parameters, "records": raw_records},
    )


def _stock_connect_active(
    parameters: dict[str, str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """为无官方成交前十来源的 AKShare 保留安全空结果，绝不把持股估算冒充成交榜。"""
    channel, direction = _channel(parameters)
    _window(parameters)
    return (
        {
            "schema": _SCHEMAS[_CONNECT_ACTIVE],
            "channel": channel,
            "direction": direction,
            "valueKind": "REPORTED",
            "records": [],
        },
        _raw_empty(
            _CONNECT_ACTIVE,
            parameters,
            "AKShare holding and estimated-increase rankings are not official active-trading lists",
        ),
    )


def _corporate_events(parameters: dict[str, str]) -> tuple[dict[str, Any], dict[str, Any]]:
    """按公告窗口关联业绩预告和快报；报告期接口仅输出窗口内公告事实。"""
    start, end = _window(parameters)
    documents: list[dict[str, object]] = []
    guidance_metrics: list[dict[str, object]] = []
    express_metrics: list[dict[str, object]] = []
    raw_groups: list[dict[str, object]] = []
    for report_period in _report_periods(start, end):
        period_text = report_period.strftime("%Y%m%d")
        guidance_frame = _akshare_frame_or_empty(
            lambda period_text=period_text: ak.stock_yjyg_em(date=period_text)
        )
        express_frame = _akshare_frame_or_empty(
            lambda period_text=period_text: ak.stock_yjkb_em(date=period_text)
        )
        guidance_rows = _frame_records(guidance_frame)
        express_rows = _frame_records(express_frame)
        raw_groups.extend(
            (
                {"kind": "guidance", "reportPeriod": period_text, "records": guidance_rows},
                {"kind": "express", "reportPeriod": period_text, "records": express_rows},
            )
        )
        for row in guidance_rows:
            announced_on = _record_date(row, "公告日期")
            security_code = _security_code(row.get("股票代码"))
            if announced_on is None or security_code is None or not start <= announced_on <= end:
                continue
            document_id = _corporate_document_id(
                "guidance", security_code, report_period, announced_on
            )
            documents.append(
                _corporate_document(
                    document_id=document_id,
                    security_code=security_code,
                    title=_corporate_title(
                        row=row,
                        security_code=security_code,
                        report_period=report_period,
                        event_name="业绩预告",
                    ),
                    category="EARNINGS_GUIDANCE",
                    report_period=report_period,
                    announced_on=announced_on,
                    source_record=row,
                    page="yjyg",
                )
            )
            metric_code = _guidance_metric_code(_optional_text(row.get("预测指标")))
            amount = _decimal_text(row.get("预测数值"))
            yoy = _decimal_text(row.get("业绩变动幅度"))
            prior = _decimal_text(row.get("上年同期值"))
            if metric_code is not None and any(value is not None for value in (amount, yoy, prior)):
                guidance_metrics.append(
                    {
                        "sourceDocumentId": document_id,
                        "securityCode": security_code,
                        "reportPeriod": report_period.isoformat(),
                        "guidanceType": _optional_text(row.get("预告类型")) or "UNKNOWN",
                        "metricCode": metric_code,
                        "amountLow": amount,
                        "amountHigh": amount,
                        "yoyLow": yoy,
                        "yoyHigh": yoy,
                        "priorPeriodValue": prior,
                        "currency": "CNY",
                    }
                )
        for row in express_rows:
            announced_on = _record_date(row, "公告日期")
            security_code = _security_code(row.get("股票代码"))
            if announced_on is None or security_code is None or not start <= announced_on <= end:
                continue
            document_id = _corporate_document_id(
                "express", security_code, report_period, announced_on
            )
            documents.append(
                _corporate_document(
                    document_id=document_id,
                    security_code=security_code,
                    title=_corporate_title(
                        row=row,
                        security_code=security_code,
                        report_period=report_period,
                        event_name="业绩快报",
                    ),
                    category="EARNINGS_EXPRESS",
                    report_period=report_period,
                    announced_on=announced_on,
                    source_record=row,
                    page="yjkb",
                )
            )
            express_metrics.extend(_express_metrics(document_id, security_code, report_period, row))
    return (
        {
            "schema": _SCHEMAS[_CORPORATE],
            "documents": _deduplicate_documents(documents),
            "guidanceMetrics": guidance_metrics,
            "expressMetrics": express_metrics,
        },
        {"capability": _CORPORATE, "parameters": parameters, "groups": raw_groups},
    )


def _dragon_tiger(parameters: dict[str, str]) -> tuple[dict[str, Any], dict[str, Any]]:
    """读取龙虎榜头和逐证券买卖席位；缺少席位的头记录不伪造聚合席位。"""
    start, end = _window(parameters)
    frame = _akshare_frame_or_empty(
        lambda: ak.stock_lhb_detail_em(start.strftime("%Y%m%d"), end.strftime("%Y%m%d"))
    )
    head_rows = _frame_records(frame)
    raw_groups: list[dict[str, object]] = [{"kind": "heads", "records": head_rows}]
    events: list[dict[str, object]] = []
    for row in head_rows:
        trade_date = _record_date(row, "上榜日")
        security_code = _security_code(row.get("代码"))
        reason_text = _optional_text(row.get("上榜原因"))
        if trade_date is None or security_code is None or reason_text is None:
            continue
        buy_frame = _akshare_frame_or_empty(
            lambda security_code=security_code, trade_date=trade_date: ak.stock_lhb_stock_detail_em(
                symbol=security_code, date=trade_date.strftime("%Y%m%d"), flag="买入"
            )
        )
        sell_frame = _akshare_frame_or_empty(
            lambda security_code=security_code, trade_date=trade_date: ak.stock_lhb_stock_detail_em(
                symbol=security_code, date=trade_date.strftime("%Y%m%d"), flag="卖出"
            )
        )
        buy_rows = _frame_records(buy_frame)
        sell_rows = _frame_records(sell_frame)
        raw_groups.extend(
            (
                {
                    "kind": "buy-seats",
                    "securityCode": security_code,
                    "tradeDate": trade_date.isoformat(),
                    "records": buy_rows,
                },
                {
                    "kind": "sell-seats",
                    "securityCode": security_code,
                    "tradeDate": trade_date.isoformat(),
                    "records": sell_rows,
                },
            )
        )
        seats = _dragon_tiger_seats(buy_rows, "BUY") + _dragon_tiger_seats(sell_rows, "SELL")
        if not seats:
            continue
        buy_amount = _decimal_text(row.get("龙虎榜买入额"))
        sell_amount = _decimal_text(row.get("龙虎榜卖出额"))
        deal_amount = _decimal_text(row.get("龙虎榜成交额"))
        if buy_amount is None or sell_amount is None or deal_amount is None:
            continue
        buy_decimal = Decimal(buy_amount)
        sell_decimal = Decimal(sell_amount)
        deal_decimal = Decimal(deal_amount)
        if abs(deal_decimal - (buy_decimal + sell_decimal)) > Decimal("0.01"):
            continue
        reason_hash = hashlib.sha256(reason_text.encode()).hexdigest()[:16]
        events.append(
            {
                "sourceEventKey": f"{security_code}:{trade_date.isoformat()}:{reason_hash}",
                "securityCode": security_code,
                "tradeDate": trade_date.isoformat(),
                "reasonCode": f"EASTMONEY_{reason_hash}",
                "reasonText": reason_text,
                "closePrice": _decimal_text(row.get("收盘价")),
                "buyAmount": buy_amount,
                "sellAmount": sell_amount,
                "netAmount": str(buy_decimal - sell_decimal),
                "dealAmount": deal_amount,
                "marketTurnoverAmount": _decimal_text(row.get("市场总成交额")),
                "dealRatio": _fraction_text(row.get("成交额占总成交比")),
                "netRatio": _fraction_text(row.get("净买额占总成交比")),
                "turnoverRatio": _fraction_text(row.get("换手率")),
                "sourcePublishedAt": None,
                "visibleTimePrecision": "DATE_ONLY",
                "visibleAt": _conservative_visible_at(trade_date),
                "seats": seats,
            }
        )
    return (
        {"schema": _SCHEMAS[_DRAGON_TIGER], "events": events},
        {"capability": _DRAGON_TIGER, "parameters": parameters, "groups": raw_groups},
    )


def _block_trades(parameters: dict[str, str]) -> tuple[dict[str, Any], dict[str, Any]]:
    """读取 A 股大宗逐笔数据，并以来源行稳定字段保留合法重复成交。"""
    start, end = _window(parameters)
    frame = _akshare_frame_or_empty(
        lambda: ak.stock_dzjy_mrmx(
            symbol="A股", start_date=start.strftime("%Y%m%d"), end_date=end.strftime("%Y%m%d")
        )
    )
    raw_records = _frame_records(frame)
    trades: list[dict[str, object]] = []
    occurrences: dict[str, int] = {}
    for row in raw_records:
        trade_date = _record_date(row, "交易日期")
        security_code = _security_code(row.get("证券代码"))
        price = _decimal_text(row.get("成交价"))
        volume = _decimal_text(row.get("成交量"))
        amount = _decimal_text(row.get("成交额"))
        buyer_name = _optional_text(row.get("买方营业部"))
        seller_name = _optional_text(row.get("卖方营业部"))
        if (
            trade_date is None
            or security_code is None
            or price is None
            or volume is None
            or amount is None
            or buyer_name is None
            or seller_name is None
        ):
            continue
        quantity = _block_trade_quantity(Decimal(price), Decimal(volume), Decimal(amount))
        if quantity is None:
            continue
        source_key = _block_trade_key(
            trade_date=trade_date,
            security_code=security_code,
            price=price,
            quantity=quantity,
            amount=amount,
            buyer_name=buyer_name,
            seller_name=seller_name,
        )
        occurrence = occurrences.get(source_key, 0) + 1
        occurrences[source_key] = occurrence
        trades.append(
            {
                "sourceTradeKey": source_key,
                "securityCode": security_code,
                "tradeDate": trade_date.isoformat(),
                "occurrenceNo": str(occurrence),
                "executionPrice": price,
                "quantityShares": str(quantity),
                "notionalCny": str(Decimal(price) * quantity),
                "buyerSeatCode": None,
                "buyerSeatName": buyer_name,
                "sellerSeatCode": None,
                "sellerSeatName": seller_name,
                "referenceClosePrice": _decimal_text(row.get("收盘价")),
                "premiumDiscountRatio": _fraction_text(row.get("折溢率")),
                "sourceDailyRank": _integer_text(row.get("序号")),
                "sourcePublishedAt": None,
                "visibleTimePrecision": "DATE_ONLY",
                "visibleAt": _conservative_visible_at(trade_date),
            }
        )
    return (
        {"schema": _SCHEMAS[_BLOCK_TRADE], "trades": trades},
        {"capability": _BLOCK_TRADE, "parameters": parameters, "records": raw_records},
    )


def _derivative_bars(parameters: dict[str, str]) -> tuple[dict[str, Any], dict[str, Any]]:
    """读取真实合约日线；东财没有结算列时保持为空，不用收盘价替换。"""
    venue, contract_code = _contract(parameters)
    start, end = _window(parameters)
    frame = _akshare_frame_or_empty(
        lambda: ak.futures_hist_em(
            symbol=contract_code,
            period="daily",
            start_date=start.strftime("%Y%m%d"),
            end_date=end.strftime("%Y%m%d"),
        )
    )
    raw_records = _frame_records(frame)
    bars: list[dict[str, object]] = []
    for row in raw_records:
        trade_date = _record_date(row, "时间")
        if trade_date is None or not start <= trade_date <= end:
            continue
        bars.append(
            {
                "tradeDate": trade_date.isoformat(),
                "open": _required_decimal(row, "开盘"),
                "high": _required_decimal(row, "最高"),
                "low": _required_decimal(row, "最低"),
                "close": _required_decimal(row, "收盘"),
                "preClose": None,
                "settlement": None,
                "preSettlement": None,
                "volume": _required_decimal(row, "成交量"),
                "openInterest": _required_decimal(row, "持仓量"),
                "turnover": _decimal_text(row.get("成交额")),
                "turnoverCurrency": "CNY" if _decimal_text(row.get("成交额")) is not None else None,
                "turnoverUnit": "CNY",
                "tradeStatus": None,
            }
        )
    return (
        {
            "schema": _SCHEMAS[_DERIVATIVE],
            "contract": f"{venue}.{contract_code}",
            "contractKind": "REAL",
            "bars": bars,
        },
        {"capability": _DERIVATIVE, "parameters": parameters, "records": raw_records},
    )


def _validate_capability(capability: str) -> None:
    """拒绝未声明能力，避免任意 AKShare 函数经通用 adapter 暴露。"""
    if capability not in _CAPABILITIES:
        raise _invalid_request("unsupported capability")


def _venue(parameters: dict[str, str]) -> str:
    """读取仅支持沪深的场所参数。"""
    venue = parameters.get("venue")
    if venue not in {"SSE", "SZSE"}:
        raise _invalid_request("venue must be SSE or SZSE")
    return venue


def _window(parameters: dict[str, str]) -> tuple[date, date]:
    """解析包含端日期窗并拒绝倒置窗口。"""
    start = _date_parameter(parameters, "start")
    end = _date_parameter(parameters, "end")
    if start > end:
        raise _invalid_request("start must not be after end")
    return start, end


def _date_parameter(parameters: dict[str, str], key: str) -> date:
    """读取 ISO 日期参数，拒绝 adapter 内隐式日期推断。"""
    try:
        return date.fromisoformat(parameters[key])
    except (KeyError, ValueError) as error:
        raise _invalid_request(f"{key} must be an ISO date") from error


def _etf(parameters: dict[str, str]) -> tuple[str, str]:
    """解析场所限定 ETF 身份并拒绝未声明场所或代码。"""
    value = parameters.get("etf", "")
    venue, separator, symbol = value.partition(".")
    if (
        separator != "."
        or venue not in {"SSE", "SZSE"}
        or len(symbol) != 6
        or not symbol.isdecimal()
    ):
        raise _invalid_request("etf must use SSE.SYMBOL or SZSE.SYMBOL")
    return venue, symbol


def _etf_key(parameters: dict[str, str]) -> str:
    """返回已验证 ETF 的场所限定键。"""
    venue, symbol = _etf(parameters)
    return f"{venue}.{symbol}"


def _etf_venue(symbol: str) -> str | None:
    """按沪深 ETF 代码段识别场所；目录没有独立交易所列，未知段不写入。"""
    if symbol.startswith(("5",)):
        return "SSE"
    if symbol.startswith(("1", "2")):
        return "SZSE"
    return None


def _channel(parameters: dict[str, str]) -> tuple[str, str]:
    """解析通道和方向，四种组合必须由来源显式选择而不能合并。"""
    channel = parameters.get("channel")
    direction = parameters.get("direction")
    if channel not in {"SH", "SZ"} or direction not in {"NORTHBOUND", "SOUTHBOUND"}:
        raise _invalid_request("invalid stock-connect channel or direction")
    return channel, direction


def _akshare_connect_symbol(channel: str, direction: str) -> str:
    """映射标准通道方向到唯一东财历史序列名称。"""
    return {
        ("SH", "NORTHBOUND"): "沪股通",
        ("SZ", "NORTHBOUND"): "深股通",
        ("SH", "SOUTHBOUND"): "港股通沪",
        ("SZ", "SOUTHBOUND"): "港股通深",
    }[(channel, direction)]


def _contract(parameters: dict[str, str]) -> tuple[str, str]:
    """解析真实合约标识，拒绝连续合约名称和未分场所代码。"""
    value = parameters.get("contract", "")
    venue, separator, contract_code = value.partition(".")
    if (
        separator != "."
        or not venue
        or venue != venue.upper()
        or not contract_code
        or contract_code != contract_code.upper()
    ):
        raise _invalid_request("contract must use VENUE.REAL_CONTRACT")
    return venue, contract_code


def _frame_records(frame: Any) -> list[dict[str, object | None]]:
    """安全投影 pandas 表为 JSON 标量记录，阻止 NaN 进入标准载荷。"""
    if isinstance(frame, tuple) or getattr(frame, "empty", False):
        return []
    return [
        {str(key): _json_value(value) for key, value in row.items()}
        for row in frame.to_dict(orient="records")
    ]


def _akshare_frame_or_empty(fetch: Callable[[], Any]) -> Any:
    """把 AKShare 空响应触发的已知 pandas 列重命名异常转成合法空表。"""
    try:
        return fetch()
    except ValueError as error:
        # AKShare 在部分空结果上先构造零列 DataFrame，再硬写固定列名；这不是字段漂移。
        if str(error).startswith("Length mismatch: Expected axis has 0 elements"):
            return ()
        raise


def _frames_raw(frames: Sequence[tuple[date | None, Any]]) -> list[dict[str, object]]:
    """保留每个逐日上游响应及其请求日期，便于失败时定位日期级问题。"""
    return [
        {
            "requestedDate": None if requested is None else requested.isoformat(),
            "records": _frame_records(frame),
        }
        for requested, frame in frames
    ]


def _json_value(value: object) -> object | None:
    """将 pandas、numpy 与日期标量归一为可序列化且不含非有限数值的值。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return None if not value.is_finite() else str(value)
    if isinstance(value, float):
        return None if isnan(value) else str(Decimal(str(value)))
    text = str(value).strip()
    return None if text.lower() in {"", "nan", "nat", "none", "<na>", "--"} else text


def _record_date(record: dict[str, object | None], key: str) -> date | None:
    """解析供应商日期单元，空值保持为空而不使用抓取日补齐。"""
    value = _optional_text(record.get(key))
    if value is None:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _security_code(value: object) -> str | None:
    """保留六位证券代码的前导零，无法确认的非证券文本不进入 P0。"""
    text = _optional_text(value)
    if text is None:
        return None
    if text.endswith(".0") and text[:-2].isdecimal():
        text = text[:-2]
    if text.isdecimal() and len(text) <= 6:
        return text.zfill(6)
    return text if len(text) == 6 and text.isalnum() else None


def _optional_text(value: object) -> str | None:
    """统一来源空值字面量，避免它们污染 canonical 身份和字段。"""
    if value is None:
        return None
    text = str(value).strip()
    return None if text.lower() in {"", "nan", "nat", "none", "<na>", "--"} else text


def _decimal_text(value: object) -> str | None:
    """解析有限十进制为文本，保留真实零值并拒绝非数值供应商占位符。"""
    text = _optional_text(value)
    if text is None:
        return None
    try:
        parsed = Decimal(text.replace(",", ""))
    except InvalidOperation:
        return None
    return str(parsed) if parsed.is_finite() else None


def _required_decimal(record: dict[str, object | None], key: str) -> str:
    """读取标准载荷必填数值，缺失表明供应商 schema 或响应质量发生变化。"""
    value = _decimal_text(record.get(key))
    if value is None:
        raise ProviderError(
            ProviderErrorCode.SCHEMA, f"missing numeric field: {key}", retryable=False
        )
    return value


def _fraction_text(value: object) -> str | None:
    """将东财百分数转换为 canonical 小数比例；未披露比例保持为空。"""
    text = _decimal_text(value)
    return None if text is None else str(Decimal(text) / Decimal("100"))


def _cny_from_yi(value: object) -> str | None:
    """将东财港通历史序列的亿元展示值换算为 CNY，避免单位穿透 adapter。"""
    text = _decimal_text(value)
    return None if text is None else str(Decimal(text) * Decimal("100000000"))


def _integer_text(value: object) -> str | None:
    """读取来源正整数定位字段；非整数值不作为稳定排行写入。"""
    text = _decimal_text(value)
    if text is None:
        return None
    parsed = Decimal(text)
    return str(int(parsed)) if parsed == parsed.to_integral_value() else None


def _days(start: date, end: date) -> tuple[date, ...]:
    """生成包含端日历日期，交易所接口将非交易日空响应保留为无事实。"""
    return tuple(start + timedelta(days=offset) for offset in range((end - start).days + 1))


def _report_periods(start: date, end: date) -> tuple[date, ...]:
    """生成可能在公告窗口出现的季报期，额外包含前一年年报以覆盖次年披露。"""
    periods: list[date] = []
    for year in range(start.year - 1, end.year + 1):
        periods.extend(
            (date(year, 3, 31), date(year, 6, 30), date(year, 9, 30), date(year, 12, 31))
        )
    return tuple(periods)


def _corporate_document_id(
    kind: str, security_code: str, report_period: date, announced_on: date
) -> str:
    """由供应商聚合行的不可变键组合文档标识，避免把标题 hash 当作唯一身份。"""
    return (
        f"eastmoney:{kind}:{security_code}:{report_period.isoformat()}:{announced_on.isoformat()}"
    )


def _corporate_title(
    *,
    row: dict[str, object | None],
    security_code: str,
    report_period: date,
    event_name: str,
) -> str:
    """构造来源展示标题；名称缺失时稳定回退至证券代码而不阻断文档证据。"""
    name = _optional_text(row.get("股票简称")) or security_code
    return f"{name} {report_period.isoformat()} {event_name}"


def _corporate_document(
    *,
    document_id: str,
    security_code: str,
    title: str,
    category: str,
    report_period: date,
    announced_on: date,
    source_record: dict[str, object | None],
    page: str,
) -> dict[str, object]:
    """构造聚合披露目录项；来源无单文档 URL 时保留可追溯的报告期查询页。"""
    content_material = _json_bytes({"documentId": document_id, "record": source_record})
    return {
        "sourceDocumentId": document_id,
        "securityCode": security_code,
        "title": title,
        "category": category,
        "officialUrl": f"https://data.eastmoney.com/bbsj/{report_period:%Y%m}/{page}.html",
        "announcedOn": announced_on.isoformat(),
        "sourceVisibleAt": None,
        "visibleTimePrecision": "DATE_ONLY",
        "publicUsableAt": _conservative_visible_at(announced_on),
        "contentSha256": hashlib.sha256(content_material).hexdigest(),
    }


def _guidance_metric_code(value: str | None) -> str | None:
    """把 AKShare 预告指标名称映射到 P0 白名单，未知指标仅保留其文档。"""
    if value is None:
        return None
    if "扣除" in value and "净利润" in value:
        return "DEDUCTED_NET_PROFIT"
    return "NET_PROFIT" if "净利润" in value else None


def _express_metrics(
    document_id: str, security_code: str, report_period: date, row: dict[str, object | None]
) -> list[dict[str, object]]:
    """将快报有限字段投影为已定义单位，百分数 ROE 显式换算为小数比例。"""
    mappings = (
        ("每股收益", "EPS", "CNY_PER_SHARE", "CNY", "每股收益", None),
        ("营业收入-营业收入", "REVENUE", "CNY", "CNY", "营业收入-去年同期", None),
        ("净利润-净利润", "NET_PROFIT", "CNY", "CNY", "净利润-去年同期", None),
        ("每股净资产", "BOOK_VALUE_PER_SHARE", "CNY_PER_SHARE", "CNY", None, None),
        ("净资产收益率", "ROE", "FRACTION", None, None, _fraction_text),
    )
    metrics: list[dict[str, object]] = []
    for current_field, code, unit, currency, prior_field, transform in mappings:
        current = _decimal_text(row.get(current_field))
        if transform is not None:
            current = transform(row.get(current_field))
        if current is None:
            continue
        metrics.append(
            {
                "sourceDocumentId": document_id,
                "securityCode": security_code,
                "reportPeriod": report_period.isoformat(),
                "metricCode": code,
                "currentValue": current,
                "priorValue": None if prior_field is None else _decimal_text(row.get(prior_field)),
                "unit": unit,
                "currency": currency,
                "preliminaryStatus": "PRELIMINARY",
            }
        )
    return metrics


def _deduplicate_documents(documents: list[dict[str, object]]) -> list[dict[str, object]]:
    """按来源文档标识去重，重复供应商行不产生冲突的同批证据。"""
    unique: dict[str, dict[str, object]] = {}
    for document in documents:
        unique[str(document["sourceDocumentId"])] = document
    return [unique[key] for key in sorted(unique)]


def _dragon_tiger_seats(rows: list[dict[str, object | None]], side: str) -> list[dict[str, object]]:
    """转换一个买卖榜单的真实营业部行；金额恒等不成立的行不进入 canonical。"""
    seats: list[dict[str, object]] = []
    for rank, row in enumerate(rows, start=1):
        seat_name = _optional_text(row.get("交易营业部名称"))
        buy = _decimal_text(row.get("买入金额"))
        sell = _decimal_text(row.get("卖出金额"))
        if seat_name is None or buy is None or sell is None:
            continue
        seats.append(
            {
                "listSide": side,
                "rank": str(rank),
                "seatCode": None,
                "seatName": seat_name,
                "buyAmount": buy,
                "sellAmount": sell,
                "netAmount": str(Decimal(buy) - Decimal(sell)),
                "buyRatio": _fraction_text(row.get("买入金额-占总成交比例")),
                "sellRatio": _fraction_text(row.get("卖出金额-占总成交比例")),
            }
        )
    return seats


def _block_trade_quantity(price: Decimal, volume: Decimal, amount: Decimal) -> int | None:
    """仅在价格和成交额可对账时识别股、手或万股口径，避免猜测成交数量。"""
    if price <= 0 or volume <= 0 or amount <= 0:
        return None
    for multiplier in (1, 100, 10_000):
        quantity = volume * multiplier
        if quantity != quantity.to_integral_value():
            continue
        if abs(price * quantity - amount) <= Decimal("0.01"):
            return int(quantity)
    return None


def _block_trade_key(
    *,
    trade_date: date,
    security_code: str,
    price: str,
    quantity: int,
    amount: str,
    buyer_name: str,
    seller_name: str,
) -> str:
    """构造来源稳定成交键；重复同键由 occurrence 保留而非错误去重。"""
    material = "|".join(
        (
            trade_date.isoformat(),
            security_code,
            price,
            str(quantity),
            amount,
            buyer_name,
            seller_name,
        )
    )
    return hashlib.sha256(material.encode()).hexdigest()


def _conservative_visible_at(day: date) -> str:
    """为只有日期的来源设置上海收盘后的保守可用时刻，绝不伪造精确发布时间。"""
    return datetime(day.year, day.month, day.day, 20, tzinfo=_SHANGHAI).isoformat()


def _raw_empty(capability: str, parameters: dict[str, str], reason: str) -> dict[str, object]:
    """记录语义不兼容的安全空结果原因；字节仍只会在失败路径落盘。"""
    return {"capability": capability, "parameters": parameters, "records": [], "reason": reason}


def _json_bytes(value: object) -> bytes:
    """序列化标准或原始内存对象，禁止 NaN 以免对象存储失败证据失真。"""
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()


def _schema_fingerprint(raw_object: dict[str, object]) -> str:
    """对本次来源对象结构生成稳定指纹，供 schema 漂移排障使用。"""
    return hashlib.sha256(_json_bytes(_shape(raw_object))).hexdigest()


def _shape(value: object) -> object:
    """递归提取对象键和数组元素形状，不将业务数值纳入 schema 指纹。"""
    if isinstance(value, dict):
        return {key: _shape(item) for key, item in sorted(value.items())}
    if isinstance(value, list):
        return [] if not value else [_shape(value[0])]
    return type(value).__name__


def _invalid_request(message: str) -> ProviderError:
    """构造不可重试请求错误，调用方可安全投影为 source_unavailable。"""
    return ProviderError(ProviderErrorCode.INVALID_REQUEST, message, retryable=False)
