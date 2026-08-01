"""通过 `AKShare` 提供 `ETF`、两融、港通、公告、公开交易与衍生品 `P0` 适配器。

本模块集中隔离多个已审核的上游接口，但每个 `capability` 仍有独立 `schema`、来源、参数
和单位规则。它绝不因为一个来源缺字段而用另一能力补值：例如 `ETF` 净值不补日线，
期货结算价不以收盘价替代，港通不同通道和方向不合并。标准 JSON 与原始响应一并交给
失败留证包装器，成功路径不在对象存储长期保留供应商字节。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from io import BytesIO, StringIO
from math import isnan
from typing import Any
from zoneinfo import ZoneInfo

import akshare as ak
import httpx
import pandas as pd
import requests

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
_EQUITY_TRADING_STATUS = "equity.trading_status.1d"
_EQUITY_SHARE_CAPITAL = "equity.share_capital.reported"
_SW_MEMBERSHIP = "sector.sw2021.membership.snapshot"

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
        _CORPORATE,
        _DRAGON_TIGER,
        _BLOCK_TRADE,
        _DERIVATIVE,
        _EQUITY_TRADING_STATUS,
        _EQUITY_SHARE_CAPITAL,
        _SW_MEMBERSHIP,
    }
)
_SCHEMAS = {
    _ETF_MASTER: "quant-v2.etf-master.v2",
    _ETF_STATUS: "quant-v2.etf-trading-state.v1",
    _ETF_BAR: "quant-v2.etf-daily-bar.v1",
    _ETF_NAV: "quant-v2.etf-nav.v1",
    _MARGIN_MARKET: "quant-v2.margin-market-daily.v1",
    _MARGIN_SECURITY: "quant-v2.margin-security-daily.v1",
    _MARGIN_ELIGIBILITY: "quant-v2.margin-eligibility.v1",
    _CONNECT_MARKET: "quant-v2.stock-connect-market-daily.v1",
    _CORPORATE: "quant-v2.corporate-earnings-events.v1",
    _DRAGON_TIGER: "quant-v2.dragon-tiger-disclosure.v1",
    _BLOCK_TRADE: "quant-v2.block-trade-execution.v1",
    _DERIVATIVE: "quant-v2.derivative-daily-bar.v1",
    _EQUITY_TRADING_STATUS: "quant-v2.equity-trading-status.v1",
    _EQUITY_SHARE_CAPITAL: "quant-v2.equity-share-capital.v1",
    _SW_MEMBERSHIP: "quant-v2.sw2021-membership.v1",
}
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_ADAPTER_VERSION = "akshare-1.18.81-p0-market-data-v9"
_BSE_MARGIN_ELIGIBILITY_COLUMNS = (
    "证券代码",
    "证券简称",
    "融资标的",
    "融券标的",
    "当日可融资",
    "当日可融券",
)
_BSE_MARGIN_STATUS = {
    ("Y", "Y"): "ELIGIBLE",
    ("Y", "N"): "FINANCING_ONLY",
    ("N", "Y"): "LENDING_ONLY",
    ("N", "N"): "INELIGIBLE",
}
_EVENT_CAPABILITIES = frozenset({_CORPORATE, _DRAGON_TIGER, _BLOCK_TRADE})
_THREADED_EVENT_CAPABILITIES = frozenset({_BLOCK_TRADE})
_EVENT_FETCH_MAX_ATTEMPTS = 3
_EVENT_FETCH_RETRY_BASE_SECONDS = 0.5
# 通用 P0 SDK 调用会遇到交易所网关主动断连；只对未分类的传输异常做有限重试。
_P0_FETCH_MAX_ATTEMPTS = 3
_P0_FETCH_RETRY_BASE_SECONDS = 0.5
_EARNINGS_URL = "https://datacenter.eastmoney.com/securities/api/data/v1/get"
_EARNINGS_PAGE_SIZE = 500
_EARNINGS_MAX_PAGES = 64
_EARNINGS_MAX_RECORDS = 32_000
_EARNINGS_MAX_PAGE_BYTES = 8 * 1024 * 1024
_EARNINGS_REPORT_ATTEMPTS = 3
_EARNINGS_RETRY_BASE_SECONDS = 0.25
_EARNINGS_GUIDANCE_REPORT = "RPT_PUBLIC_OP_NEWPREDICT"
_EARNINGS_EXPRESS_REPORT = "RPT_FCI_PERFORMANCEE"
_EARNINGS_GUIDANCE_FIELDS = (
    "SECURITY_CODE",
    "SECURITY_NAME_ABBR",
    "NOTICE_DATE",
    "REPORT_DATE",
    "PREDICT_FINANCE",
    "PREDICT_CONTENT",
    "FORECAST_JZ",
    "INCREASE_JZ",
    "CHANGE_REASON_EXPLAIN",
    "PREDICT_TYPE",
    "PREYEAR_SAME_PERIOD",
    "ORG_CODE",
    "IS_LATEST",
)
_EARNINGS_EXPRESS_FIELDS = (
    "SECURITY_CODE",
    "SECURITY_NAME_ABBR",
    "NOTICE_DATE",
    "REPORT_DATE",
    "BASIC_EPS",
    "TOTAL_OPERATE_INCOME",
    "TOTAL_OPERATE_INCOME_SQ",
    "PARENT_NETPROFIT",
    "PARENT_NETPROFIT_SQ",
    "PARENT_BVPS",
    "WEIGHTAVG_ROE",
    "PUBLISHNAME",
    "ORG_CODE",
    "ISNEW",
)
_EARNINGS_SORT = (
    ("NOTICE_DATE", "-1"),
    ("SECURITY_CODE", "-1"),
    ("ORG_CODE", "-1"),
)
_DRAGON_TIGER_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
_DRAGON_TIGER_PAGE_SIZE = 500
_DRAGON_TIGER_MAX_PAGES = 128
_DRAGON_TIGER_MAX_RECORDS = 64_000
_DRAGON_TIGER_MAX_PAGE_BYTES = 8 * 1024 * 1024
_DRAGON_TIGER_MAX_WINDOW_DAYS = 31
_DRAGON_TIGER_REPORT_ATTEMPTS = 3
_DRAGON_TIGER_RETRY_BASE_SECONDS = 0.25
_DRAGON_TIGER_ROOT_FIELDS = frozenset({"version", "result", "success", "message", "code"})
_DRAGON_TIGER_RESULT_FIELDS = frozenset({"pages", "data", "count"})
_DRAGON_TIGER_HEAD_REPORT = "RPT_DAILYBILLBOARD_DETAILSNEW"
_DRAGON_TIGER_BUY_REPORT = "RPT_BILLBOARD_DAILYDETAILSBUY"
_DRAGON_TIGER_SELL_REPORT = "RPT_BILLBOARD_DAILYDETAILSSELL"
_DRAGON_TIGER_HEAD_FIELDS = (
    "SECURITY_CODE",
    "TRADE_DATE",
    "EXPLANATION",
    "TRADE_ID",
    "CLOSE_PRICE",
    "BILLBOARD_NET_AMT",
    "BILLBOARD_BUY_AMT",
    "BILLBOARD_SELL_AMT",
    "BILLBOARD_DEAL_AMT",
    "ACCUM_AMOUNT",
    "DEAL_NET_RATIO",
    "DEAL_AMOUNT_RATIO",
    "TURNOVERRATE",
)
_DRAGON_TIGER_SEAT_FIELDS = (
    "SECURITY_CODE",
    "TRADE_DATE",
    "EXPLANATION",
    "TRADE_ID",
    "OPERATEDEPT_CODE",
    "OPERATEDEPT_NAME",
    "BUY",
    "SELL",
    "NET",
    "TOTAL_BUYRIO",
    "TOTAL_SELLRIO",
)
_DRAGON_TIGER_HEAD_SORT = (
    ("TRADE_DATE", "1"),
    ("SECURITY_CODE", "1"),
    ("EXPLANATION", "1"),
    ("TRADE_ID", "1"),
)
_DRAGON_TIGER_BUY_SORT = (
    ("TRADE_DATE", "1"),
    ("SECURITY_CODE", "1"),
    ("EXPLANATION", "1"),
    ("TRADE_ID", "1"),
    ("BUY", "-1"),
    ("SELL", "-1"),
    ("OPERATEDEPT_CODE", "1"),
    ("OPERATEDEPT_NAME", "1"),
)
_DRAGON_TIGER_SELL_SORT = (
    ("TRADE_DATE", "1"),
    ("SECURITY_CODE", "1"),
    ("EXPLANATION", "1"),
    ("TRADE_ID", "1"),
    ("SELL", "-1"),
    ("BUY", "-1"),
    ("OPERATEDEPT_CODE", "1"),
    ("OPERATEDEPT_NAME", "1"),
)
_SSE_FUND_DIRECTORY_URL = "https://query.sse.com.cn/commonQuery.do"
_SSE_FUND_DIRECTORY_REFERER = "https://etf.sse.com.cn/fundlist/"
_SSE_FUND_DIRECTORY_MAX_BYTES = 8 * 1024 * 1024
_SSE_FUND_DIRECTORY_MAX_RECORDS = 5_000
_SSE_FUND_CATEGORY_MAX_RECORDS = 128
_SSE_FUND_DIRECTORY_ATTEMPTS = 2
_SSE_FUND_DIRECTORY_FIELDS = frozenset(
    {
        "COMPANY_NAME",
        "FUND_CODE",
        "CATEGORY",
        "FUND_ABBR",
        "COMPANY_CODE",
        "INDEX_NAME",
        "FUND_EXPANSION_ABBR",
        "SCALE",
        "LISTING_DATE",
    }
)
_SSE_FUND_CATEGORY_FIELDS = frozenset({"CATEGORY_CODE", "CATEGORY_PARENT_CODE", "CATEGORY_NAME"})
_SSE_QUERY_ROOT_FIELDS = frozenset(
    {
        "actionErrors",
        "actionMessages",
        "fieldErrors",
        "isPagination",
        "jsonCallBack",
        "locale",
        "pageHelp",
        "pageNo",
        "pageSize",
        "queryDate",
        "result",
        "securityCode",
        "sqlId",
        "texts",
        "type",
        "validateCode",
    }
)
_SZSE_FUND_DIRECTORY_URL = "https://fund.szse.cn/api/report/ShowReport"
_SZSE_FUND_DIRECTORY_MAX_BYTES = 16 * 1024 * 1024
_SZSE_FUND_DIRECTORY_ATTEMPTS = 2
_LEGULEGU_SW_MEMBERSHIP_URL = "https://legulegu.com/stockdata/index-composition"
_LEGULEGU_SW_MEMBERSHIP_MAX_BYTES = 4 * 1024 * 1024
_SW_MEMBERSHIP_COLUMNS = ("序号", "股票代码", "股票简称", "纳入时间")
_SZSE_FUND_DIRECTORY_FIELDS = frozenset(
    {
        "基金代码",
        "基金简称",
        "基金类别",
        "投资类别",
        "上市日期",
        "当前规模(份)",
        "基金管理人",
        "基金发起人",
        "基金托管人",
        "净值",
    }
)
_EASTMONEY_ETF_NAV_URL = "https://api.fund.eastmoney.com/f10/lsjz"
_EASTMONEY_ETF_NAV_PAGE_SIZE = 100
_EASTMONEY_ETF_NAV_MAX_PAGES = 400
_EASTMONEY_ETF_NAV_MAX_BYTES = 4 * 1024 * 1024
_EASTMONEY_ETF_NAV_ATTEMPTS = 2
_EASTMONEY_ETF_NAV_REQUIRED_FIELDS = frozenset({"FSRQ", "DWJZ", "LJJZ", "NAVTYPE", "SGZT", "SHZT"})


class AkshareP0MarketDataAdapter:
    """将 `AKShare` `P0` 可验证字段隔离成一个默认 `akshare` 数据源。

    适配器只声明已完成且可诚实发布的字段映射；未知请求不会成为任意 SDK 调用。
    """

    provider_id = "akshare"

    def __init__(self, *, request_timeout_seconds: int) -> None:
        """保存每次阻塞 SDK 调用可占用的最大墙钟时间。"""
        self._request_timeout_seconds = request_timeout_seconds

    def capabilities(self) -> frozenset[str]:
        """声明已完成映射且不依赖伪空成功的 P0 capability。"""
        return _CAPABILITIES

    async def fetch(self, request: SourceRequest) -> ProviderBatch:
        """调用唯一 `AKShare` 映射并返回标准 `JSON` 与仅失败留证所需的内存载荷。

        可重试错误只代表上游暂不可用；参数、单位、字段和来源语义问题会以不可重试错误
        停止发布，避免重试把同一错误响应写成多次观察。
        """
        parameters = dict(request.parameters)
        if request.capability == _CONNECT_ACTIVE:
            # 该能力不能以持股或估算排行替代；即使有旧任务直调，也必须失败关闭。
            raise _currently_unsupported(
                capability=_CONNECT_ACTIVE,
                parameters=parameters,
                reason_code="NO_VERIFIED_ACTIVE_SECURITY_SOURCE",
            )
        _validate_capability(request.capability)
        try:
            if request.capability == _CORPORATE:
                payload_object, raw_object = await _fetch_corporate_events(
                    parameters,
                    request_timeout_seconds=self._request_timeout_seconds,
                )
                upstream_source = "eastmoney.earnings"
            elif request.capability == _DRAGON_TIGER:
                payload_object, raw_object = await _fetch_dragon_tiger_bulk(
                    parameters,
                    request_timeout_seconds=self._request_timeout_seconds,
                )
                # 批量化只改变 transport 与 adapter 版本，稳定来源身份不得切断历史窗口连续性。
                upstream_source = "eastmoney.dragon-tiger"
            elif request.capability in _THREADED_EVENT_CAPABILITIES:
                payload_object, raw_object, upstream_source = await _fetch_event_payload_with_retry(
                    capability=request.capability,
                    parameters=parameters,
                    request_timeout_seconds=self._request_timeout_seconds,
                )
            else:
                async with asyncio.timeout(self._request_timeout_seconds):
                    payload_object, raw_object, upstream_source = await _fetch_payload_with_retry(
                        capability=request.capability,
                        parameters=parameters,
                        request_timeout_seconds=self._request_timeout_seconds,
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
            if request.capability in _EVENT_CAPABILITIES:
                retryable = _is_retryable_event_fetch_error(error)
                raise ProviderError(
                    (ProviderErrorCode.UNAVAILABLE if retryable else ProviderErrorCode.SCHEMA),
                    "event provider request failed",
                    retryable=retryable,
                ) from error
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


async def _fetch_payload_with_retry(
    *,
    capability: str,
    parameters: dict[str, str],
    request_timeout_seconds: int,
) -> tuple[dict[str, Any], dict[str, Any], str]:
    """在一个总超时预算内重试短暂的通用 SDK 传输故障。

    `ProviderError` 已携带经过适配器确认的业务语义，不能因重试而把 schema、权限或当前不支持
    的能力误当网络抖动。其余异常沿用既有“来源暂不可用”分类，并只重试两次以避免单个分区长期
    占据全局执行槽。
    """
    for attempt in range(_P0_FETCH_MAX_ATTEMPTS):
        try:
            return await asyncio.to_thread(
                _fetch_payload,
                capability=capability,
                parameters=parameters,
                request_timeout_seconds=request_timeout_seconds,
            )
        except ProviderError:
            raise
        except Exception:
            if attempt + 1 >= _P0_FETCH_MAX_ATTEMPTS:
                raise
            await asyncio.sleep(_P0_FETCH_RETRY_BASE_SECONDS * (2**attempt))
    raise AssertionError("P0 fetch retry loop must return or raise")


async def _fetch_event_payload_with_retry(
    *,
    capability: str,
    parameters: dict[str, str],
    request_timeout_seconds: int,
) -> tuple[dict[str, Any], dict[str, Any], str]:
    """在单一总墙钟预算内重试事件网络、`429` 与 `5xx`，避免分页瞬断卡死整次运行。

    每次重试仍执行同一冻结参数，不改变日期窗或证券选择器。解析、字段和普通 `4xx`
    错误不重试，否则确定性坏响应会挤占供应商预算并延迟真实失败。
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + request_timeout_seconds
    for attempt in range(_EVENT_FETCH_MAX_ATTEMPTS):
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise TimeoutError("event provider total request budget exhausted")
        try:
            async with asyncio.timeout(remaining):
                return await asyncio.to_thread(
                    _fetch_payload,
                    capability=capability,
                    parameters=parameters,
                    request_timeout_seconds=max(1, int(remaining)),
                )
        except Exception as error:
            if (
                isinstance(error, asyncio.CancelledError)
                or not _is_retryable_event_fetch_error(error)
                or attempt + 1 >= _EVENT_FETCH_MAX_ATTEMPTS
            ):
                raise
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError("event provider total request budget exhausted") from error
            delay = min(_EVENT_FETCH_RETRY_BASE_SECONDS * (2**attempt), remaining)
            if delay > 0:
                await asyncio.sleep(delay)
    raise AssertionError("event provider retry loop exhausted without a terminal result")


def _is_retryable_event_fetch_error(error: BaseException) -> bool:
    """仅把传输故障、限流和服务端错误归为可重试，普通 `4xx` 与解析异常直接失败。"""
    if isinstance(error, ProviderError):
        return error.retryable
    if isinstance(error, (TimeoutError, ConnectionError)):
        return True
    if isinstance(error, requests.HTTPError):
        response = error.response
        return response is not None and (response.status_code == 429 or response.status_code >= 500)
    return isinstance(error, requests.RequestException)


def _fetch_payload(
    *,
    capability: str,
    parameters: dict[str, str],
    request_timeout_seconds: int,
) -> tuple[dict[str, Any], dict[str, Any], str]:
    """在适配器边界内分派供应商函数，绝不将 `AKShare` 名称泄漏给应用层。

    返回的 `upstream_source` 是血缘字段，而非可自由选择的路由参数；每个分支固定对应
    一个经过审核的上游数据集。
    """
    if capability == _ETF_MASTER:
        upstream_source = (
            "sse.official-current-etf-directory"
            if parameters.get("venue") == "SSE"
            else "szse.official-fund-directory"
        )
        return (
            *_etf_master(
                parameters,
                request_timeout_seconds=request_timeout_seconds,
            ),
            upstream_source,
        )
    if capability == _ETF_STATUS:
        return (
            *_etf_status(
                parameters,
                request_timeout_seconds=request_timeout_seconds,
            ),
            "eastmoney.etf.nav-json",
        )
    if capability == _ETF_BAR:
        return (
            *_etf_bars(
                parameters,
                request_timeout_seconds=request_timeout_seconds,
            ),
            "tencent.etf-kline",
        )
    if capability == _ETF_NAV:
        return (
            *_etf_nav(
                parameters,
                request_timeout_seconds=request_timeout_seconds,
            ),
            "eastmoney.etf.nav-json",
        )
    if capability == _MARGIN_MARKET:
        return (*_margin_market(parameters), "sse-szse.margin")
    if capability == _MARGIN_SECURITY:
        return (*_margin_security(parameters), "sse-szse.margin")
    if capability == _MARGIN_ELIGIBILITY:
        payload, raw = _margin_eligibility(parameters)
        return payload, raw, _margin_eligibility_upstream_source(parameters)
    if capability == _CONNECT_MARKET:
        return (*_stock_connect_market(parameters), "eastmoney.stock-connect")
    if capability == _CONNECT_ACTIVE:
        raise _currently_unsupported(
            capability=_CONNECT_ACTIVE,
            parameters=parameters,
            reason_code="NO_VERIFIED_ACTIVE_SECURITY_SOURCE",
        )
    if capability == _CORPORATE:
        raise AssertionError("earnings fetch must use its cancellable async transport")
    if capability == _DRAGON_TIGER:
        raise AssertionError("dragon-tiger bulk fetch must use its cancellable async transport")
    if capability == _BLOCK_TRADE:
        return (*_block_trades(parameters), "eastmoney.block-trade")
    if capability == _EQUITY_TRADING_STATUS:
        return (*_equity_trading_status(parameters), "eastmoney.trading-suspension")
    if capability == _EQUITY_SHARE_CAPITAL:
        return (*_equity_share_capital(parameters), "eastmoney.share-capital")
    if capability == _SW_MEMBERSHIP:
        return (*_sw_membership(parameters), "legulegu.sw-index-composition")
    return (*_derivative_bars(parameters), "eastmoney.futures")


def _etf_master(
    parameters: dict[str, str],
    *,
    request_timeout_seconds: int = 15,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """按来源明确的场所目录读取 ETF，不通过代码前缀或目录差集推断类别与状态。"""
    venue = _venue(parameters)
    observation_date = _date_parameter(parameters, "observationDate")
    current_date = datetime.now(_SHANGHAI).date()
    if observation_date != current_date:
        error = ProviderError(
            ProviderErrorCode.INVALID_REQUEST,
            f"{venue} official ETF directory only exposes the current snapshot",
            retryable=False,
        )
        error.attach_failure_evidence(
            json.dumps(
                {
                    "schema": "quant-v2.provider-failure-evidence.v1",
                    "provider": venue.lower(),
                    "capability": _ETF_MASTER,
                    "errorCode": ProviderErrorCode.INVALID_REQUEST.value,
                    "retryable": False,
                    "request": {
                        "venue": venue,
                        "requestedObservationDate": observation_date.isoformat(),
                        "availableObservationDate": current_date.isoformat(),
                    },
                    "rawResponseRetention": "not_requested",
                },
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        )
        raise error
    if venue == "SSE":
        raw_records, category_names, raw_object = _sse_etf_directory(
            request_timeout_seconds=request_timeout_seconds
        )
        profiles = []
        symbols: set[str] = set()
        for record in raw_records:
            symbol = _security_code(record.get("FUND_CODE"))
            display_name = _optional_text(record.get("FUND_EXPANSION_ABBR")) or _optional_text(
                record.get("FUND_ABBR")
            )
            category = _optional_text(record.get("CATEGORY"))
            if symbol is None or display_name is None or category not in category_names:
                raise ProviderError(
                    ProviderErrorCode.SCHEMA,
                    "SSE ETF directory contains an invalid required field",
                    retryable=False,
                )
            if symbol in symbols:
                raise ProviderError(
                    ProviderErrorCode.SCHEMA,
                    "SSE ETF directory contains a duplicate symbol",
                    retryable=False,
                )
            symbols.add(symbol)
            profiles.append(
                {
                    "symbol": symbol,
                    "displayName": display_name,
                    # ETF 类型取自同一官方接口的 CATEGORY 树，不按代码、简称或指数名称推断。
                    "etfType": category_names[category],
                    "managementMode": "UNKNOWN",
                    "managerName": _optional_text(record.get("COMPANY_NAME")),
                    "custodianName": None,
                    "establishedOn": None,
                    "listedOn": _optional_iso_date(record.get("LISTING_DATE")),
                    "delistedOn": None,
                    "quoteCurrency": "CNY",
                    "navCurrency": "CNY",
                    "listingStatus": "UNKNOWN",
                    # 当前目录没有来源业务日期，使用请求观察日并显式标记时间精度未知。
                    "effectiveFrom": observation_date.isoformat(),
                    "sourceTimePrecision": "UNKNOWN",
                }
            )
        return (
            {"schema": _SCHEMAS[_ETF_MASTER], "venue": venue, "profiles": profiles},
            {
                "capability": _ETF_MASTER,
                "parameters": parameters,
                "requestedObservationDate": observation_date.isoformat(),
                "sourceDataDate": None,
                "sourceTimePrecision": "UNKNOWN",
                "publicationLagDays": None,
                **raw_object,
            },
        )

    frame = _szse_fund_directory(request_timeout_seconds=request_timeout_seconds)
    raw_records = _frame_records(frame)
    profiles = []
    symbols = set()
    for record in raw_records:
        if _optional_text(record.get("基金类别")) != "ETF":
            # 深交所目录同时包含 ETF、LOF 和 REITs，只接受来源明确标记为 ETF 的行。
            continue
        symbol = _security_code(record.get("基金代码"))
        display_name = _optional_text(record.get("基金简称"))
        if symbol is None or display_name is None:
            raise ProviderError(
                ProviderErrorCode.SCHEMA,
                "SZSE explicit ETF row contains an invalid required field",
                retryable=False,
            )
        if symbol in symbols:
            raise ProviderError(
                ProviderErrorCode.SCHEMA,
                "SZSE ETF directory contains a duplicate symbol",
                retryable=False,
            )
        symbols.add(symbol)
        effective_from = observation_date
        listed_on = _record_date(record, "上市日期")
        profiles.append(
            {
                "symbol": symbol,
                "displayName": display_name,
                "etfType": _optional_text(record.get("投资类别")) or "ETF",
                "managementMode": "UNKNOWN",
                "managerName": _optional_text(record.get("基金管理人")),
                "custodianName": _optional_text(record.get("基金托管人")),
                "establishedOn": None,
                "listedOn": listed_on.isoformat() if listed_on is not None else None,
                "delistedOn": None,
                "quoteCurrency": "CNY",
                "navCurrency": "CNY",
                # 被目录观察到不等于来源明确披露“上市中”，因此保持未知。
                "listingStatus": "UNKNOWN",
                "effectiveFrom": effective_from.isoformat(),
                "sourceTimePrecision": "DATE_ONLY",
            }
        )
    return (
        {"schema": _SCHEMAS[_ETF_MASTER], "venue": venue, "profiles": profiles},
        {"capability": _ETF_MASTER, "parameters": parameters, "records": raw_records},
    )


def _sse_etf_directory(
    *,
    request_timeout_seconds: int,
) -> tuple[list[dict[str, object]], dict[str, str], dict[str, object]]:
    """读取上交所官方当前 ETF 目录及 CATEGORY 树，空集、未知类别或超界响应一律失败关闭。"""
    category_payload = _sse_json_query(
        {
            "sqlId": "COMMON_JJZWZ_JJLB_JJLX_C",
            "PARENT": "F100",
            "type": "inParams",
        },
        request_timeout_seconds=request_timeout_seconds,
    )
    directory_payload = _sse_json_query(
        {
            "sqlId": "COMMON_JJZWZ_JJLB_L",
            "CATEGORY": "F100",
            "type": "inParams",
            "CATEGORY_ASC": "1",
            "FUND_CODE": "",
            "FUND_ABBR": "",
        },
        request_timeout_seconds=request_timeout_seconds,
    )
    categories = _sse_category_names(category_payload)
    raw_records = _sse_result_records(
        directory_payload,
        expected_sql_id="COMMON_JJZWZ_JJLB_L",
        fields=_SSE_FUND_DIRECTORY_FIELDS,
    )
    if not raw_records or len(raw_records) > _SSE_FUND_DIRECTORY_MAX_RECORDS:
        raise ProviderError(
            ProviderErrorCode.UNAVAILABLE if not raw_records else ProviderErrorCode.SCHEMA,
            "SSE official ETF directory is empty or exceeds the bounded record limit",
            retryable=not raw_records,
        )
    if any(_optional_text(record.get("CATEGORY")) not in categories for record in raw_records):
        raise ProviderError(
            ProviderErrorCode.SCHEMA,
            "SSE official ETF directory references an unknown CATEGORY",
            retryable=False,
        )
    return (
        raw_records,
        categories,
        {
            "source": "sse.official-current-etf-directory",
            "categoryTree": category_payload["result"],
            "records": raw_records,
        },
    )


def _sse_json_query(
    parameters: dict[str, str],
    *,
    request_timeout_seconds: int,
) -> dict[str, object]:
    """对上交所受控 SQL 标识执行有限重试，并严格校验 HTTP、媒体类型、体积与 JSON 根结构。"""
    timeout = max(1, request_timeout_seconds)
    headers = {
        "Referer": _SSE_FUND_DIRECTORY_REFERER,
        "Accept": "application/json,text/javascript;q=0.9",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36"
        ),
    }
    for attempt in range(_SSE_FUND_DIRECTORY_ATTEMPTS):
        try:
            response = requests.get(
                _SSE_FUND_DIRECTORY_URL,
                params=parameters,
                headers=headers,
                timeout=(min(5, timeout), timeout),
            )
        except requests.RequestException as error:
            if attempt + 1 < _SSE_FUND_DIRECTORY_ATTEMPTS:
                continue
            raise ProviderError(
                ProviderErrorCode.UNAVAILABLE,
                "SSE official ETF directory request failed after bounded retry",
                retryable=True,
            ) from error
        if response.status_code == 429:
            if attempt + 1 < _SSE_FUND_DIRECTORY_ATTEMPTS:
                continue
            raise ProviderError(
                ProviderErrorCode.RATE_LIMITED,
                "SSE official ETF directory rate limited",
                retryable=True,
            )
        if response.status_code >= 500:
            if attempt + 1 < _SSE_FUND_DIRECTORY_ATTEMPTS:
                continue
            raise ProviderError(
                ProviderErrorCode.UNAVAILABLE,
                "SSE official ETF directory upstream unavailable",
                retryable=True,
            )
        if response.status_code != 200:
            raise ProviderError(
                ProviderErrorCode.INVALID_REQUEST,
                f"SSE official ETF directory returned HTTP {response.status_code}",
                retryable=False,
            )
        content_type = str(response.headers.get("Content-Type", "")).lower()
        if (
            "json" not in content_type
            or not response.content
            or len(response.content) > _SSE_FUND_DIRECTORY_MAX_BYTES
        ):
            raise ProviderError(
                ProviderErrorCode.SCHEMA,
                "SSE official ETF directory response media type or size is invalid",
                retryable=False,
            )
        try:
            payload = response.json()
        except ValueError as error:
            raise ProviderError(
                ProviderErrorCode.SCHEMA,
                "SSE official ETF directory response is not JSON",
                retryable=False,
            ) from error
        if not isinstance(payload, dict) or set(payload) != _SSE_QUERY_ROOT_FIELDS:
            raise ProviderError(
                ProviderErrorCode.SCHEMA,
                "SSE official ETF directory root contract changed",
                retryable=False,
            )
        if (
            payload.get("sqlId") != parameters["sqlId"]
            or payload.get("isPagination") != "false"
            or payload.get("actionErrors") != []
            or payload.get("fieldErrors") != {}
        ):
            raise ProviderError(
                ProviderErrorCode.SCHEMA,
                "SSE official ETF directory query returned an invalid envelope",
                retryable=False,
            )
        return payload
    raise AssertionError("bounded SSE ETF directory retry loop did not return")


def _sse_category_names(payload: dict[str, object]) -> dict[str, str]:
    """从官方类型树解析 F100 后代叶类名称，禁止自行维护代码到基金类型的猜测表。"""
    rows = _sse_result_records(
        payload,
        expected_sql_id="COMMON_JJZWZ_JJLB_JJLX_C",
        fields=_SSE_FUND_CATEGORY_FIELDS,
    )
    if not rows or len(rows) > _SSE_FUND_CATEGORY_MAX_RECORDS:
        raise ProviderError(
            ProviderErrorCode.SCHEMA,
            "SSE ETF CATEGORY tree is empty or exceeds the bounded node limit",
            retryable=False,
        )
    parents: dict[str, str] = {}
    names: dict[str, str] = {}
    for row in rows:
        code = _optional_text(row.get("CATEGORY_CODE"))
        parent = _optional_text(row.get("CATEGORY_PARENT_CODE"))
        name = _optional_text(row.get("CATEGORY_NAME"))
        if code is None or parent is None or name is None or code in names:
            raise ProviderError(
                ProviderErrorCode.SCHEMA,
                "SSE ETF CATEGORY tree contains an invalid node",
                retryable=False,
            )
        parents[code] = parent
        names[code] = name
    if (
        names.get("F000") != "基金"
        or parents.get("F000") != "-"
        or names.get("F100") != "ETF"
        or parents.get("F100") != "F000"
    ):
        raise ProviderError(
            ProviderErrorCode.SCHEMA,
            "SSE ETF CATEGORY root is unavailable",
            retryable=False,
        )
    _validate_sse_category_graph(parents)
    descendants = {
        code: name
        for code, name in names.items()
        if code != "F100" and _sse_category_descends_from(code, parents=parents, root="F100")
    }
    if not descendants:
        raise ProviderError(
            ProviderErrorCode.SCHEMA,
            "SSE ETF CATEGORY tree has no ETF descendants",
            retryable=False,
        )
    return descendants


def _validate_sse_category_graph(parents: dict[str, str]) -> None:
    """验证官方类型树的全部父节点和无环不变量，避免残缺树把其他基金误收为 ETF。"""
    for code, parent in parents.items():
        if code == "F000":
            continue
        if parent not in parents:
            raise ProviderError(
                ProviderErrorCode.SCHEMA,
                "SSE ETF CATEGORY tree references an unknown parent",
                retryable=False,
            )
        visited: set[str] = set()
        current = code
        while current != "F000":
            if current in visited:
                raise ProviderError(
                    ProviderErrorCode.SCHEMA,
                    "SSE ETF CATEGORY tree contains a cycle",
                    retryable=False,
                )
            visited.add(current)
            current = parents[current]


def _sse_category_descends_from(
    code: str,
    *,
    parents: dict[str, str],
    root: str,
) -> bool:
    """沿官方父节点链确认类别属于 ETF 根，遇到环或缺失父节点时失败关闭。"""
    visited: set[str] = set()
    current = code
    while current not in visited and current in parents:
        visited.add(current)
        current = parents[current]
        if current == root:
            return True
    return False


def _sse_result_records(
    payload: dict[str, object],
    *,
    expected_sql_id: str,
    fields: frozenset[str],
) -> list[dict[str, object]]:
    """读取上交所查询结果并要求每行字段集合精确匹配冻结合同。"""
    raw_records = payload.get("result")
    if payload.get("sqlId") != expected_sql_id or not isinstance(raw_records, list):
        raise ProviderError(
            ProviderErrorCode.SCHEMA,
            "SSE official ETF directory result contract changed",
            retryable=False,
        )
    records: list[dict[str, object]] = []
    for record in raw_records:
        if not isinstance(record, dict) or set(record) != fields:
            raise ProviderError(
                ProviderErrorCode.SCHEMA,
                "SSE official ETF directory row contract changed",
                retryable=False,
            )
        records.append({str(key): value for key, value in record.items()})
    return records


def _optional_iso_date(value: object) -> str | None:
    """把官方可选 ISO 日期保留为日期字符串，空值不以观察日补齐。"""
    normalized = _optional_text(value)
    if normalized is None:
        return None
    return date.fromisoformat(normalized).isoformat()


def _szse_fund_directory(*, request_timeout_seconds: int) -> pd.DataFrame:
    """兼容读取深交所官方 XLSX，并对网络、状态、体积和文件格式设置有限边界。

    AKShare 1.18.81 将响应 `bytes` 直接传给新版 pandas，触发 `TypeError`。这里仅在
    provider adapter 内用 `BytesIO` 修复，不修改全局 pandas/AKShare 行为。
    """
    timeout = max(1, request_timeout_seconds)
    params = {
        "SHOWTYPE": "xlsx",
        "CATALOGID": "1000_lf",
        "TABKEY": "tab1",
        "random": "0.07610353191740105",
    }
    headers = {
        "Referer": "https://fund.szse.cn/marketdata/fundslist/index.html",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept": (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,"
            "application/octet-stream;q=0.9"
        ),
    }
    for attempt in range(_SZSE_FUND_DIRECTORY_ATTEMPTS):
        try:
            response = requests.get(
                _SZSE_FUND_DIRECTORY_URL,
                params=params,
                headers=headers,
                timeout=(min(5, timeout), timeout),
            )
        except requests.RequestException as error:
            if attempt + 1 < _SZSE_FUND_DIRECTORY_ATTEMPTS:
                continue
            raise ProviderError(
                ProviderErrorCode.UNAVAILABLE,
                "SZSE fund directory request failed after bounded retry",
                retryable=True,
            ) from error
        if response.status_code == 429:
            if attempt + 1 < _SZSE_FUND_DIRECTORY_ATTEMPTS:
                continue
            raise ProviderError(
                ProviderErrorCode.RATE_LIMITED,
                "SZSE fund directory rate limited",
                retryable=True,
            )
        if response.status_code >= 500:
            if attempt + 1 < _SZSE_FUND_DIRECTORY_ATTEMPTS:
                continue
            raise ProviderError(
                ProviderErrorCode.UNAVAILABLE,
                "SZSE fund directory upstream unavailable",
                retryable=True,
            )
        if response.status_code != 200:
            raise ProviderError(
                ProviderErrorCode.INVALID_REQUEST,
                f"SZSE fund directory returned HTTP {response.status_code}",
                retryable=False,
            )
        content = response.content
        if (
            not content
            or len(content) > _SZSE_FUND_DIRECTORY_MAX_BYTES
            or not content.startswith(b"PK")
        ):
            raise ProviderError(
                ProviderErrorCode.SCHEMA,
                "SZSE fund directory response is not a bounded XLSX file",
                retryable=False,
            )
        try:
            frame = pd.read_excel(
                BytesIO(content),
                engine="openpyxl",
                dtype={"基金代码": str},
            )
        except (TypeError, ValueError) as error:
            raise ProviderError(
                ProviderErrorCode.SCHEMA,
                "SZSE fund directory XLSX cannot be decoded",
                retryable=False,
            ) from error
        columns = {str(column).strip() for column in frame.columns}
        required = {"基金代码", "基金简称", "基金类别"}
        if not required.issubset(columns) or not columns.issubset(_SZSE_FUND_DIRECTORY_FIELDS):
            raise ProviderError(
                ProviderErrorCode.SCHEMA,
                "SZSE fund directory column contract changed",
                retryable=False,
            )
        return frame
    raise AssertionError("bounded SZSE fund directory retry loop did not return")


def _etf_status(
    parameters: dict[str, str],
    *,
    request_timeout_seconds: int = 15,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """从 ETF 历史净值返回申购和赎回状态，交易状态因来源未披露保持缺席。"""
    _, symbol = _etf(parameters)
    start, end = _window(parameters)
    raw_records, raw_object = _eastmoney_etf_nav_records(
        symbol=symbol,
        start=start,
        end=end,
        request_timeout_seconds=request_timeout_seconds,
    )
    statuses: list[dict[str, object]] = []
    for record in raw_records:
        effective_from = _record_date(record, "FSRQ")
        if effective_from is None or not start <= effective_from <= end:
            continue
        for field, dimension in (("SGZT", "SUBSCRIPTION"), ("SHZT", "REDEMPTION")):
            status_code = _optional_text(record.get(field))
            if status_code is not None:
                statuses.append(
                    {
                        "dimension": dimension,
                        "statusCode": status_code,
                        "effectiveFrom": effective_from.isoformat(),
                        # 东财只报告该 NAV 日期的状态，不得把单日观察夸大为持续有效。
                        "effectiveTo": (effective_from + timedelta(days=1)).isoformat(),
                        "reason": None,
                    }
                )
    return (
        {"schema": _SCHEMAS[_ETF_STATUS], "etf": _etf_key(parameters), "statuses": statuses},
        {
            "capability": _ETF_STATUS,
            "parameters": parameters,
            **raw_object,
        },
    )


def _etf_bars(
    parameters: dict[str, str],
    *,
    request_timeout_seconds: int = 15,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """读取腾讯 ETF 未复权日线，成交量统一为股、成交额统一为人民币元。"""
    venue, symbol = _etf(parameters)
    start, end = _window(parameters)
    if parameters.get("priceBasis") != "UNADJUSTED":
        raise _invalid_request("ETF P0 requires UNADJUSTED price basis")
    provider_prefix = {"SSE": "sh", "SZSE": "sz"}[venue]
    # 此回调只执行由已验证交易所身份映射出的腾讯未复权日线请求。
    frame = _akshare_frame_or_empty(
        lambda: ak.stock_zh_a_hist_tx(
            symbol=f"{provider_prefix}{symbol}",
            start_date=start.strftime("%Y%m%d"),
            end_date=end.strftime("%Y%m%d"),
            adjust="",
            timeout=request_timeout_seconds,
        )
    )
    raw_records = _frame_records(frame)
    bars = []
    for record in raw_records:
        trade_date = _record_date(record, "date")
        if trade_date is None or not start <= trade_date <= end:
            continue
        bars.append(
            {
                "tradeDate": trade_date.isoformat(),
                "open": _required_decimal(record, "open"),
                "high": _required_decimal(record, "high"),
                "low": _required_decimal(record, "low"),
                "close": _required_decimal(record, "close"),
                "volume": _required_decimal(record, "volume"),
                "volumeUnit": "SHARE",
                "amount": _required_decimal(record, "amount"),
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


def _etf_nav(
    parameters: dict[str, str],
    *,
    request_timeout_seconds: int = 15,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """读取 ETF 单位与累计净值，终态信息未披露时明确写为未知。"""
    _, symbol = _etf(parameters)
    start, end = _window(parameters)
    raw_records, raw_object = _eastmoney_etf_nav_records(
        symbol=symbol,
        start=start,
        end=end,
        request_timeout_seconds=request_timeout_seconds,
    )
    navs: list[dict[str, object]] = []
    for record in raw_records:
        nav_date = _record_date(record, "FSRQ")
        if nav_date is None or not start <= nav_date <= end:
            continue
        if _optional_text(record.get("NAVTYPE")) != "1":
            error = ProviderError(
                ProviderErrorCode.CURRENTLY_UNSUPPORTED,
                "Eastmoney ETF response uses a non-NAV yield semantic",
                retryable=False,
            )
            error.attach_failure_evidence(
                _etf_nav_semantics_failure_evidence(
                    symbol=symbol,
                    records=raw_records,
                    raw_object=raw_object,
                )
            )
            raise error
        for field, nav_kind in (("DWJZ", "UNIT"), ("LJJZ", "ACCUMULATED")):
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
        {
            "capability": _ETF_NAV,
            "parameters": parameters,
            **raw_object,
        },
    )


def _etf_nav_semantics_failure_evidence(
    *,
    symbol: str,
    records: list[dict[str, object | None]],
    raw_object: dict[str, object],
) -> bytes:
    """生成货币 ETF NAV 语义冲突的有界脱敏证据，不保留代码、URL 或来源正文。"""
    nav_types = sorted({_optional_text(record.get("NAVTYPE")) or "MISSING" for record in records})
    record_dates = sorted(
        value for record in records if (value := _optional_text(record.get("FSRQ"))) is not None
    )
    canonical_raw = json.dumps(
        raw_object,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return json.dumps(
        {
            "schema": "quant-v2.provider-failure-evidence.v1",
            "provider": "eastmoney",
            "capability": _ETF_NAV,
            "errorCode": ProviderErrorCode.CURRENTLY_UNSUPPORTED.value,
            "retryable": False,
            "request": {
                "symbolFingerprint": hashlib.sha256(symbol.encode()).hexdigest(),
                "navTypes": nav_types,
                "recordDateFrom": record_dates[0] if record_dates else None,
                "recordDateTo": record_dates[-1] if record_dates else None,
                "rowCount": len(records),
                "rawPayloadSha256": hashlib.sha256(canonical_raw).hexdigest(),
            },
            "rawResponseRetention": "hash_only",
        },
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _eastmoney_etf_nav_records(
    *,
    symbol: str,
    start: date,
    end: date,
    request_timeout_seconds: int,
) -> tuple[list[dict[str, object | None]], dict[str, object]]:
    """有限分页读取东财 ETF 净值 JSON，并冻结状态与净值共用的来源字段契约。"""
    timeout = max(1, request_timeout_seconds)
    headers = {
        "Referer": f"https://fundf10.eastmoney.com/jjjz_{symbol}.html",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json,text/plain,*/*",
    }
    records: list[dict[str, object | None]] = []
    pages: list[dict[str, object]] = []
    expected_total: int | None = None
    page_index = 1
    while page_index <= _EASTMONEY_ETF_NAV_MAX_PAGES:
        params = {
            "fundCode": symbol,
            "pageIndex": str(page_index),
            "pageSize": str(_EASTMONEY_ETF_NAV_PAGE_SIZE),
            "startDate": start.isoformat(),
            "endDate": end.isoformat(),
        }
        payload = _eastmoney_etf_nav_page(
            params=params,
            headers=headers,
            timeout=timeout,
        )
        if payload.get("ErrCode") != 0:
            raise ProviderError(
                ProviderErrorCode.SCHEMA,
                "Eastmoney ETF NAV returned a non-success application code",
                retryable=False,
            )
        data = payload.get("Data")
        page_records = data.get("LSJZList") if isinstance(data, dict) else None
        total_count = payload.get("TotalCount")
        response_page = payload.get("PageIndex")
        if (
            not isinstance(page_records, list)
            or not isinstance(total_count, int)
            or total_count < 0
            or response_page != page_index
        ):
            raise ProviderError(
                ProviderErrorCode.SCHEMA,
                "Eastmoney ETF NAV pagination contract changed",
                retryable=False,
            )
        if expected_total is None:
            expected_total = total_count
        elif total_count != expected_total:
            raise ProviderError(
                ProviderErrorCode.SCHEMA,
                "Eastmoney ETF NAV total changed during pagination",
                retryable=False,
            )
        normalized_page: list[dict[str, object | None]] = []
        for record in page_records:
            if not isinstance(record, dict) or not _EASTMONEY_ETF_NAV_REQUIRED_FIELDS.issubset(
                {str(key) for key in record}
            ):
                raise ProviderError(
                    ProviderErrorCode.SCHEMA,
                    "Eastmoney ETF NAV required fields are missing",
                    retryable=False,
                )
            normalized_page.append({str(key): _json_value(value) for key, value in record.items()})
        records.extend(normalized_page)
        pages.append({"pageIndex": page_index, "response": payload})
        if len(records) >= total_count:
            break
        if not normalized_page:
            raise ProviderError(
                ProviderErrorCode.SCHEMA,
                "Eastmoney ETF NAV pagination ended before TotalCount",
                retryable=False,
            )
        page_index += 1
    if expected_total is None or len(records) != expected_total:
        raise ProviderError(
            ProviderErrorCode.SCHEMA,
            "Eastmoney ETF NAV exceeded pagination bound or count mismatched",
            retryable=False,
        )
    field_set = sorted({key for record in records for key in record})
    return records, {"records": records, "pages": pages, "fieldSet": field_set}


def _eastmoney_etf_nav_page(
    *,
    params: dict[str, str],
    headers: dict[str, str],
    timeout: int,
) -> dict[str, object]:
    """以有限重试读取一页东财 ETF 净值 JSON，限制状态码、体积和根对象类型。"""
    for attempt in range(_EASTMONEY_ETF_NAV_ATTEMPTS):
        try:
            response = requests.get(
                _EASTMONEY_ETF_NAV_URL,
                params=params,
                headers=headers,
                timeout=(min(5, timeout), timeout),
            )
        except requests.RequestException as error:
            if attempt + 1 < _EASTMONEY_ETF_NAV_ATTEMPTS:
                continue
            raise ProviderError(
                ProviderErrorCode.UNAVAILABLE,
                "Eastmoney ETF NAV request failed after bounded retry",
                retryable=True,
            ) from error
        if response.status_code == 429:
            if attempt + 1 < _EASTMONEY_ETF_NAV_ATTEMPTS:
                continue
            raise ProviderError(
                ProviderErrorCode.RATE_LIMITED,
                "Eastmoney ETF NAV rate limited",
                retryable=True,
            )
        if response.status_code >= 500:
            if attempt + 1 < _EASTMONEY_ETF_NAV_ATTEMPTS:
                continue
            raise ProviderError(
                ProviderErrorCode.UNAVAILABLE,
                "Eastmoney ETF NAV upstream unavailable",
                retryable=True,
            )
        if response.status_code != 200:
            raise ProviderError(
                ProviderErrorCode.INVALID_REQUEST,
                f"Eastmoney ETF NAV returned HTTP {response.status_code}",
                retryable=False,
            )
        if not response.content or len(response.content) > _EASTMONEY_ETF_NAV_MAX_BYTES:
            raise ProviderError(
                ProviderErrorCode.SCHEMA,
                "Eastmoney ETF NAV response is empty or exceeds the size bound",
                retryable=False,
            )
        try:
            payload = response.json()
        except ValueError as error:
            raise ProviderError(
                ProviderErrorCode.SCHEMA,
                "Eastmoney ETF NAV response is not JSON",
                retryable=False,
            ) from error
        if not isinstance(payload, dict):
            raise ProviderError(
                ProviderErrorCode.SCHEMA,
                "Eastmoney ETF NAV response root is not an object",
                retryable=False,
            )
        return {str(key): value for key, value in payload.items()}
    raise AssertionError("bounded Eastmoney ETF NAV retry loop did not return")


def _margin_market(parameters: dict[str, str]) -> tuple[dict[str, Any], dict[str, Any]]:
    """读取沪深场所两融汇总；北交所资格清单不能被误作汇总数据。"""
    venue = _margin_venue(parameters)
    start, end = _window(parameters)
    if venue == "BSE":
        raise _currently_unsupported(
            capability=_MARGIN_MARKET,
            parameters=parameters,
            reason_code="BSE_MARGIN_MARKET_NOT_MAPPED",
        )
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
    """读取沪深证券明细；北交所资格清单不能被误作证券余额明细。"""
    venue = _margin_venue(parameters)
    start, end = _window(parameters)
    if venue == "BSE":
        raise _currently_unsupported(
            capability=_MARGIN_SECURITY,
            parameters=parameters,
            reason_code="BSE_MARGIN_SECURITY_NOT_MAPPED",
        )
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
    """读取沪深北当日资格观察名单，只将标的资格列映射为 canonical 资格状态。"""
    venue = _margin_venue(parameters)
    start, end = _window(parameters)
    if venue == "SSE":
        raise _currently_unsupported(
            capability=_MARGIN_ELIGIBILITY,
            parameters=parameters,
            reason_code="SSE_MARGIN_ELIGIBILITY_NO_UNDERLYING_ENDPOINT",
        )
    fetch = (
        (lambda day: ak.stock_margin_underlying_info_szse(day.strftime("%Y%m%d")))
        if venue == "SZSE"
        else (lambda day: ak.stock_margin_underlying_info_bse(day.strftime("%Y%m%d")))
    )
    frames = [
        (
            day,
            _akshare_frame_or_empty(lambda day=day: fetch(day)),
        )
        for day in _days(start, end)
    ]
    raw_records = _frames_raw(frames)
    records: list[dict[str, object]] = []
    for observation_date, frame in frames:
        if venue == "BSE":
            records.extend(_bse_margin_eligibility_records(observation_date, frame))
            continue
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


def _margin_eligibility_upstream_source(parameters: dict[str, str]) -> str:
    """返回已证实资格名单的上游身份；上交所请求应已在获取前失败关闭。"""
    venue = _margin_venue(parameters)
    if venue == "SZSE":
        return "szse.margin-underlying"
    if venue == "BSE":
        return "bse.margin-underlying"
    raise _currently_unsupported(
        capability=_MARGIN_ELIGIBILITY,
        parameters=parameters,
        reason_code="SSE_MARGIN_ELIGIBILITY_NO_UNDERLYING_ENDPOINT",
    )


def _bse_margin_eligibility_records(observation_date: date, frame: Any) -> list[dict[str, object]]:
    """严格映射北交所标的列；当日可用列仅保留 raw，绝不改变资格结论。"""
    if isinstance(frame, tuple):
        # AKShare 对非交易日的已知零列表构造异常已在公共包装器转成空观察。
        return []
    columns = tuple(str(column).strip() for column in frame.columns)
    if columns != _BSE_MARGIN_ELIGIBILITY_COLUMNS:
        raise ProviderError(
            ProviderErrorCode.SCHEMA,
            "BSE margin underlying column contract changed",
            retryable=False,
        )
    records: list[dict[str, object]] = []
    for row_index, row in enumerate(_frame_records(frame)):
        security_code = _security_code(row.get("证券代码"))
        security_name = _optional_text(row.get("证券简称"))
        if security_code is None or not security_code.isdecimal() or security_name is None:
            raise ProviderError(
                ProviderErrorCode.SCHEMA,
                f"BSE margin underlying identity is invalid at row {row_index}",
                retryable=False,
            )
        financing_underlying = _bse_margin_flag(row, "融资标的", row_index)
        lending_underlying = _bse_margin_flag(row, "融券标的", row_index)
        # 这两个列反映当日可用性，不是标的资格；只校验并保留在 raw source evidence。
        _bse_margin_flag(row, "当日可融资", row_index)
        _bse_margin_flag(row, "当日可融券", row_index)
        records.append(
            {
                "securityCode": security_code,
                "status": _BSE_MARGIN_STATUS[(financing_underlying, lending_underlying)],
                "effectiveFrom": observation_date.isoformat(),
                "effectiveTo": None,
                "announcementOn": None,
                "evidenceBasis": "OBSERVED_LIST",
            }
        )
    return records


def _bse_margin_flag(row: dict[str, object | None], field: str, row_index: int) -> str:
    """读取北交所明确的 `Y`/`N` 标识；未知值不能被降级、填补或猜测。"""
    value = _optional_text(row.get(field))
    if value not in {"Y", "N"}:
        raise ProviderError(
            ProviderErrorCode.SCHEMA,
            f"BSE margin underlying {field} must be Y or N at row {row_index}",
            retryable=False,
        )
    return value


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


async def _fetch_corporate_events(
    parameters: dict[str, str],
    *,
    request_timeout_seconds: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """逐请求读取业绩预告与快报分页，并在完整报告期集合后按公告窗标准化。

    东财接口按报告期检索，公告日期必须取来源 `NOTICE_DATE`，不能用报告期或后续
    `UPDATE_DATE` 替代。所有请求原生异步且有独立 timeout；任一页失败或取消后不会
    调度下页、下一报表或下一报告期。
    """
    start, end = _window(parameters)
    _event_instrument_symbol(parameters)
    if (end - start).days + 1 > _DRAGON_TIGER_MAX_WINDOW_DAYS:
        raise _invalid_request("earnings event window must not exceed 31 calendar days")
    timeout_seconds = max(1, request_timeout_seconds)
    page_evidence: list[dict[str, object]] = []
    groups: list[
        tuple[
            date,
            list[dict[str, object | None]],
            list[dict[str, object | None]],
        ]
    ] = []
    raw_reports: list[dict[str, object]] = []
    try:
        async with httpx.AsyncClient(follow_redirects=False, timeout=None) as client:
            for report_period in _report_periods(start, end):
                guidance_rows, guidance_raw = await _fetch_earnings_report(
                    client=client,
                    report_name=_EARNINGS_GUIDANCE_REPORT,
                    fields=_EARNINGS_GUIDANCE_FIELDS,
                    report_period=report_period,
                    request_timeout_seconds=timeout_seconds,
                    page_evidence=page_evidence,
                )
                express_rows, express_raw = await _fetch_earnings_report(
                    client=client,
                    report_name=_EARNINGS_EXPRESS_REPORT,
                    fields=_EARNINGS_EXPRESS_FIELDS,
                    report_period=report_period,
                    request_timeout_seconds=timeout_seconds,
                    page_evidence=page_evidence,
                )
                groups.append(
                    (
                        report_period,
                        _map_earnings_rows(
                            _EARNINGS_GUIDANCE_REPORT,
                            report_period,
                            guidance_rows,
                        ),
                        _map_earnings_rows(
                            _EARNINGS_EXPRESS_REPORT,
                            report_period,
                            express_rows,
                        ),
                    )
                )
                raw_reports.extend((guidance_raw, express_raw))
    except asyncio.CancelledError:
        raise
    except ProviderError as error:
        _attach_earnings_failure_evidence(
            error,
            parameters=parameters,
            page_evidence=page_evidence,
            failure_kind="EARNINGS_REPORT_FAILED",
        )
        raise
    try:
        normalized = _normalize_corporate_events(parameters, groups=groups)
    except ProviderError as error:
        _attach_earnings_failure_evidence(
            error,
            parameters=parameters,
            page_evidence=page_evidence,
            failure_kind="EARNINGS_RECONCILIATION_FAILED",
        )
        raise
    return (
        normalized,
        {
            "capability": _CORPORATE,
            "parameters": parameters,
            "announcementField": "NOTICE_DATE",
            "reports": raw_reports,
        },
    )


async def _fetch_earnings_report(
    *,
    client: httpx.AsyncClient,
    report_name: str,
    fields: tuple[str, ...],
    report_period: date,
    request_timeout_seconds: int,
    page_evidence: list[dict[str, object]],
) -> tuple[list[dict[str, object | None]], dict[str, object]]:
    """顺序抓取一个报告期的一类业绩报表，并冻结页数、总量和跨页唯一性。"""
    records: list[dict[str, object | None]] = []
    raw_pages: list[dict[str, object]] = []
    expected_pages: int | None = None
    expected_count: int | None = None
    page_number = 1
    while expected_pages is None or page_number <= expected_pages:
        body, decoded = await _request_earnings_page(
            client=client,
            report_name=report_name,
            fields=fields,
            report_period=report_period,
            page_number=page_number,
            request_timeout_seconds=request_timeout_seconds,
            page_evidence=page_evidence,
        )
        pages, count, page_records = _decode_earnings_page(
            report_name=report_name,
            fields=fields,
            page_number=page_number,
            body=body,
            decoded=decoded,
        )
        if expected_pages is None:
            expected_pages = pages
            expected_count = count
            if pages == 0:
                raw_pages.append({"pageNumber": page_number, "response": decoded})
                break
        elif pages != expected_pages or count != expected_count:
            raise _earnings_schema_error(
                f"{report_name} {report_period.isoformat()} pagination totals changed"
            )
        assert expected_count is not None
        expected_length = min(
            _EARNINGS_PAGE_SIZE,
            expected_count - (page_number - 1) * _EARNINGS_PAGE_SIZE,
        )
        if len(page_records) != expected_length:
            raise _earnings_schema_error(
                f"{report_name} {report_period.isoformat()} page {page_number} is incomplete"
            )
        records.extend(page_records)
        raw_pages.append({"pageNumber": page_number, "response": decoded})
        page_number += 1
    assert expected_pages is not None
    assert expected_count is not None
    if len(records) != expected_count:
        raise _earnings_schema_error(
            f"{report_name} {report_period.isoformat()} total count is inconsistent"
        )
    identities = [hashlib.sha256(_json_bytes(row)).hexdigest() for row in records]
    if len(set(identities)) != expected_count:
        raise _earnings_schema_error(
            f"{report_name} {report_period.isoformat()} contains duplicate rows"
        )
    return (
        records,
        {
            "reportName": report_name,
            "reportPeriod": report_period.isoformat(),
            "filter": _earnings_filter(report_name, report_period),
            "pageSize": _EARNINGS_PAGE_SIZE,
            "sortColumns": ",".join(field for field, _ in _EARNINGS_SORT),
            "sortTypes": ",".join(direction for _, direction in _EARNINGS_SORT),
            "pages": expected_pages,
            "count": expected_count,
            "rawPages": raw_pages,
        },
    )


async def _request_earnings_page(
    *,
    client: httpx.AsyncClient,
    report_name: str,
    fields: tuple[str, ...],
    report_period: date,
    page_number: int,
    request_timeout_seconds: int,
    page_evidence: list[dict[str, object]],
) -> tuple[bytes, dict[str, object]]:
    """为一页业绩报表执行真实 timeout 与有限重试，只重试 transport、`429` 和 `5xx`。"""
    params = {
        "reportName": report_name,
        "columns": ",".join(fields),
        "filter": _earnings_filter(report_name, report_period),
        "pageNumber": str(page_number),
        "pageSize": str(_EARNINGS_PAGE_SIZE),
        "sortColumns": ",".join(field for field, _ in _EARNINGS_SORT),
        "sortTypes": ",".join(direction for _, direction in _EARNINGS_SORT),
    }
    for attempt in range(1, _EARNINGS_REPORT_ATTEMPTS + 1):
        try:
            response = await _earnings_http_get(
                client,
                params=params,
                request_timeout=httpx.Timeout(
                    request_timeout_seconds,
                    connect=min(5.0, request_timeout_seconds),
                ),
            )
        except httpx.TransportError as error:
            if attempt >= _EARNINGS_REPORT_ATTEMPTS:
                raise ProviderError(
                    ProviderErrorCode.UNAVAILABLE,
                    f"{report_name} {report_period.isoformat()} page {page_number} failed",
                    retryable=True,
                ) from error
            await _earnings_retry_delay(attempt=attempt)
            continue
        body = response.content
        page_evidence.append(
            {
                "reportName": report_name,
                "reportPeriod": report_period.isoformat(),
                "pageNumber": page_number,
                "attempt": attempt,
                "statusCode": response.status_code,
                "byteSize": len(body),
                "sha256": hashlib.sha256(body).hexdigest(),
            }
        )
        if len(body) > _EARNINGS_MAX_PAGE_BYTES:
            raise _earnings_schema_error(f"{report_name} page exceeds the byte limit")
        if response.status_code == 429 or response.status_code >= 500:
            if attempt < _EARNINGS_REPORT_ATTEMPTS:
                await _earnings_retry_delay(attempt=attempt)
                continue
            code = (
                ProviderErrorCode.RATE_LIMITED
                if response.status_code == 429
                else ProviderErrorCode.UNAVAILABLE
            )
            raise ProviderError(
                code,
                f"{report_name} upstream is temporarily unavailable",
                retryable=True,
            )
        if response.status_code != 200:
            raise _earnings_schema_error(
                f"{report_name} page {page_number} returned HTTP {response.status_code}"
            )
        media_type = response.headers.get("Content-Type", "").partition(";")[0].strip().lower()
        if media_type not in {"application/json", "text/plain"}:
            raise _earnings_schema_error(f"{report_name} returned an unexpected media type")
        try:
            decoded = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise _earnings_schema_error(f"{report_name} page is not valid JSON") from error
        if not isinstance(decoded, dict):
            raise _earnings_schema_error(f"{report_name} response root is not an object")
        return body, decoded
    raise AssertionError("earnings page retry loop exhausted without a terminal result")


async def _earnings_http_get(
    client: httpx.AsyncClient,
    *,
    params: dict[str, str],
    request_timeout: httpx.Timeout,
) -> httpx.Response:
    """执行一页业绩原生异步 HTTP；独立边界用于失败、timeout 与取消回归。"""
    return await client.get(_EARNINGS_URL, params=params, timeout=request_timeout)


async def _earnings_retry_delay(*, attempt: int) -> None:
    """在业绩页级重试之间短退避，任务取消会立即打断等待。"""
    delay = _EARNINGS_RETRY_BASE_SECONDS * (2 ** (attempt - 1))
    if delay > 0:
        await asyncio.sleep(delay)


def _decode_earnings_page(
    *,
    report_name: str,
    fields: tuple[str, ...],
    page_number: int,
    body: bytes,
    decoded: dict[str, object],
) -> tuple[int, int, list[dict[str, object | None]]]:
    """解析业绩分页 envelope，严格区分来源明确空集与错误响应。"""
    if set(decoded) != _DRAGON_TIGER_ROOT_FIELDS:
        raise _earnings_schema_error(f"{report_name} response root fields changed")
    if (
        decoded.get("success") is False
        and decoded.get("code") == 9201
        and decoded.get("message") == "返回数据为空"
        and decoded.get("result") is None
    ):
        if page_number != 1:
            raise _earnings_schema_error(f"{report_name} became empty after page one")
        return 0, 0, []
    if (
        decoded.get("success") is not True
        or decoded.get("code") != 0
        or decoded.get("message") != "ok"
    ):
        raise _earnings_schema_error(f"{report_name} returned an unsuccessful envelope")
    result = decoded.get("result")
    if not isinstance(result, dict) or set(result) != _DRAGON_TIGER_RESULT_FIELDS:
        raise _earnings_schema_error(f"{report_name} result fields changed")
    pages = result.get("pages")
    count = result.get("count")
    data = result.get("data")
    if (
        isinstance(pages, bool)
        or not isinstance(pages, int)
        or isinstance(count, bool)
        or not isinstance(count, int)
        or not isinstance(data, list)
        or pages < 1
        or pages > _EARNINGS_MAX_PAGES
        or count < 1
        or count > _EARNINGS_MAX_RECORDS
        or pages != (count + _EARNINGS_PAGE_SIZE - 1) // _EARNINGS_PAGE_SIZE
    ):
        raise _earnings_schema_error(f"{report_name} pagination bounds are invalid")
    expected_fields = set(fields)
    records: list[dict[str, object | None]] = []
    for row_index, value in enumerate(data):
        if not isinstance(value, dict) or set(value) != expected_fields:
            raise _earnings_schema_error(f"{report_name} row {row_index} fields changed")
        records.append({str(key): _json_value(item) for key, item in value.items()})
    if not body:
        raise _earnings_schema_error(f"{report_name} page body is empty")
    return pages, count, records


def _earnings_filter(report_name: str, report_period: date) -> str:
    """构造固定报告期过滤式；证券和公告窗只在完整批次标准化后应用。"""
    period = report_period.isoformat()
    if report_name == _EARNINGS_GUIDANCE_REPORT:
        return f"(REPORT_DATE='{period}')"
    return (
        '(SECURITY_TYPE_CODE in ("058001001","058001008"))'
        '(TRADE_MARKET_CODE!="069001017")'
        f"(REPORT_DATE='{period}')"
    )


def _map_earnings_rows(
    report_name: str,
    report_period: date,
    rows: list[dict[str, object | None]],
) -> list[dict[str, object | None]]:
    """把冻结英文供应商字段映射为既有标准化输入，并校验报告期不漂移。"""
    mapped: list[dict[str, object | None]] = []
    for row_index, row in enumerate(rows):
        source_period = _record_date(row, "REPORT_DATE")
        if source_period != report_period:
            raise _malformed_event_candidate(
                capability=_CORPORATE,
                row_kind=report_name,
                row_index=row_index,
                reason="REPORT_DATE does not match requested report period",
            )
        if report_name == _EARNINGS_GUIDANCE_REPORT:
            mapped.append(
                {
                    "股票代码": row.get("SECURITY_CODE"),
                    "股票简称": row.get("SECURITY_NAME_ABBR"),
                    "公告日期": row.get("NOTICE_DATE"),
                    "预测指标": row.get("PREDICT_FINANCE"),
                    "业绩变动": row.get("PREDICT_CONTENT"),
                    "预测数值": row.get("FORECAST_JZ"),
                    "业绩变动幅度": row.get("INCREASE_JZ"),
                    "业绩变动原因": row.get("CHANGE_REASON_EXPLAIN"),
                    "预告类型": row.get("PREDICT_TYPE"),
                    "上年同期值": row.get("PREYEAR_SAME_PERIOD"),
                }
            )
        else:
            mapped.append(
                {
                    "股票代码": row.get("SECURITY_CODE"),
                    "股票简称": row.get("SECURITY_NAME_ABBR"),
                    "公告日期": row.get("NOTICE_DATE"),
                    "每股收益": row.get("BASIC_EPS"),
                    "营业收入-营业收入": row.get("TOTAL_OPERATE_INCOME"),
                    "营业收入-去年同期": row.get("TOTAL_OPERATE_INCOME_SQ"),
                    "净利润-净利润": row.get("PARENT_NETPROFIT"),
                    "净利润-去年同期": row.get("PARENT_NETPROFIT_SQ"),
                    "每股净资产": row.get("PARENT_BVPS"),
                    "净资产收益率": row.get("WEIGHTAVG_ROE"),
                    "所处行业": row.get("PUBLISHNAME"),
                }
            )
    return mapped


def _normalize_corporate_events(
    parameters: dict[str, str],
    *,
    groups: list[
        tuple[
            date,
            list[dict[str, object | None]],
            list[dict[str, object | None]],
        ]
    ],
) -> dict[str, Any]:
    """按公告窗口关联已完整抓取的业绩预告和快报，不以报告期代替可见日期。"""
    start, end = _window(parameters)
    instrument_symbol = _event_instrument_symbol(parameters)
    documents: list[dict[str, object]] = []
    guidance_metrics: list[dict[str, object]] = []
    express_metrics: list[dict[str, object]] = []
    for report_period, guidance_rows, express_rows in groups:
        period_text = report_period.strftime("%Y%m%d")
        for row_index, row in enumerate(guidance_rows):
            identity = _event_candidate_identity(
                row,
                capability=_CORPORATE,
                row_kind=f"guidance:{period_text}",
                row_index=row_index,
                code_key="股票代码",
                date_key="公告日期",
                instrument_symbol=instrument_symbol,
                start=start,
                end=end,
            )
            if identity is None:
                continue
            security_code, announced_on = identity
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
            if metric_code is None or not any(value is not None for value in (amount, yoy, prior)):
                raise _malformed_event_candidate(
                    capability=_CORPORATE,
                    row_kind=f"guidance:{period_text}",
                    row_index=row_index,
                    reason="no supported earnings metric can be normalized",
                )
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
        for row_index, row in enumerate(express_rows):
            identity = _event_candidate_identity(
                row,
                capability=_CORPORATE,
                row_kind=f"express:{period_text}",
                row_index=row_index,
                code_key="股票代码",
                date_key="公告日期",
                instrument_symbol=instrument_symbol,
                start=start,
                end=end,
            )
            if identity is None:
                continue
            security_code, announced_on = identity
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
            normalized_metrics = _express_metrics(document_id, security_code, report_period, row)
            if not normalized_metrics:
                raise _malformed_event_candidate(
                    capability=_CORPORATE,
                    row_kind=f"express:{period_text}",
                    row_index=row_index,
                    reason="no supported earnings metric can be normalized",
                )
            express_metrics.extend(normalized_metrics)
    return {
        "schema": _SCHEMAS[_CORPORATE],
        "documents": _deduplicate_documents(documents),
        "guidanceMetrics": guidance_metrics,
        "expressMetrics": express_metrics,
    }


def _attach_earnings_failure_evidence(
    error: ProviderError,
    *,
    parameters: dict[str, str],
    page_evidence: list[dict[str, object]],
    failure_kind: str,
) -> None:
    """附加已收到业绩页的脱敏哈希，不把供应商响应原文复制进异常。"""
    error.attach_failure_evidence(
        _json_bytes(
            {
                "schema": "quant-v2.provider-failure-evidence.v1",
                "provider": "akshare",
                "capability": _CORPORATE,
                "errorCode": error.code.value,
                "retryable": error.retryable,
                "failureKind": failure_kind,
                "request": {
                    "start": parameters.get("start"),
                    "end": parameters.get("end"),
                    "instrument": parameters.get("instrument"),
                },
                "fetchedPages": sorted(
                    page_evidence,
                    # 此回调只按安全定位字段稳定失败证据，不读取或复制供应商业务原文。
                    key=lambda item: (
                        str(item["reportPeriod"]),
                        str(item["reportName"]),
                        str(item["pageNumber"]).zfill(8),
                        str(item["attempt"]).zfill(4),
                    ),
                ),
            }
        )
    )


def _earnings_schema_error(message: str) -> ProviderError:
    """构造不可重试的业绩分页或字段一致性错误。"""
    return ProviderError(ProviderErrorCode.SCHEMA, message, retryable=False)


async def _fetch_dragon_tiger_bulk(
    parameters: dict[str, str],
    *,
    request_timeout_seconds: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """顺序读取三个全市场批量报表，再在完整对账后按证券选择器投影。

    整窗只发日期过滤，不把 `instrument` 下推为 `SECURITY_CODE`，因此全市场和单证券请求
    共享同一来源快照。没有供应商并发配额依据时保持单连接顺序分页；任一页失败或任务
    取消都不会继续下一个页面或报表，也不会留下无法取消的整窗线程。
    """
    start, end = _window(parameters)
    _event_instrument_symbol(parameters)
    if (end - start).days + 1 > _DRAGON_TIGER_MAX_WINDOW_DAYS:
        raise _invalid_request("dragon-tiger window must not exceed 31 calendar days")
    page_evidence: list[dict[str, object]] = []
    timeout_seconds = max(1, request_timeout_seconds)
    specs = (
        (_DRAGON_TIGER_HEAD_REPORT, _DRAGON_TIGER_HEAD_FIELDS, _DRAGON_TIGER_HEAD_SORT),
        (_DRAGON_TIGER_BUY_REPORT, _DRAGON_TIGER_SEAT_FIELDS, _DRAGON_TIGER_BUY_SORT),
        (_DRAGON_TIGER_SELL_REPORT, _DRAGON_TIGER_SEAT_FIELDS, _DRAGON_TIGER_SELL_SORT),
    )
    try:
        async with httpx.AsyncClient(follow_redirects=False, timeout=None) as client:
            reports = []
            for report_name, fields, sort in specs:
                reports.append(
                    await _fetch_dragon_tiger_report(
                        client=client,
                        report_name=report_name,
                        fields=fields,
                        sort=sort,
                        start=start,
                        end=end,
                        request_timeout_seconds=timeout_seconds,
                        page_evidence=page_evidence,
                    )
                )
    except asyncio.CancelledError:
        raise
    except ProviderError as error:
        _attach_dragon_tiger_failure_evidence(
            error,
            parameters=parameters,
            page_evidence=page_evidence,
            failure_kind="BULK_REPORT_FAILED",
        )
        raise
    head_rows, head_raw = reports[0]
    buy_rows, buy_raw = reports[1]
    sell_rows, sell_raw = reports[2]
    try:
        normalized = _normalize_dragon_tiger(
            parameters,
            head_rows=head_rows,
            buy_rows=buy_rows,
            sell_rows=sell_rows,
        )
    except ProviderError as error:
        _attach_dragon_tiger_failure_evidence(
            error,
            parameters=parameters,
            page_evidence=page_evidence,
            failure_kind="CROSS_REPORT_RECONCILIATION_FAILED",
        )
        raise
    return (
        normalized,
        {
            "capability": _DRAGON_TIGER,
            "parameters": parameters,
            "methodology": {
                "headAmounts": "EASTMONEY_REPORTED_AGGREGATES",
                "seatLists": "EASTMONEY_TOP_BUY_AND_TOP_SELL_DISCLOSURES",
                "reconciliation": "HEAD_INTERNAL_AND_SEAT_INTERNAL_ONLY",
            },
            "reports": [head_raw, buy_raw, sell_raw],
        },
    )


async def _fetch_dragon_tiger_report(
    *,
    client: httpx.AsyncClient,
    report_name: str,
    fields: tuple[str, ...],
    sort: tuple[tuple[str, str], ...],
    start: date,
    end: date,
    request_timeout_seconds: int,
    page_evidence: list[dict[str, object]],
) -> tuple[list[dict[str, object | None]], dict[str, object]]:
    """按冻结页数顺序抓取一个报表，并验证页长、总量、字段和跨页唯一性。"""
    raw_pages: list[dict[str, object]] = []
    records: list[dict[str, object | None]] = []
    expected_pages: int | None = None
    expected_count: int | None = None
    page_number = 1
    while expected_pages is None or page_number <= expected_pages:
        body, decoded = await _request_dragon_tiger_page(
            client=client,
            report_name=report_name,
            fields=fields,
            sort=sort,
            start=start,
            end=end,
            page_number=page_number,
            request_timeout_seconds=request_timeout_seconds,
            page_evidence=page_evidence,
        )
        pages, count, page_records = _decode_dragon_tiger_page(
            report_name=report_name,
            fields=fields,
            page_number=page_number,
            body=body,
            decoded=decoded,
        )
        if expected_pages is None:
            expected_pages = pages
            expected_count = count
            if pages == 0:
                raw_pages.append({"pageNumber": page_number, "response": decoded})
                break
        elif pages != expected_pages or count != expected_count:
            raise _dragon_tiger_schema_error(
                f"{report_name} pagination totals changed after page one"
            )
        assert expected_count is not None
        expected_page_length = min(
            _DRAGON_TIGER_PAGE_SIZE,
            expected_count - (page_number - 1) * _DRAGON_TIGER_PAGE_SIZE,
        )
        if len(page_records) != expected_page_length:
            raise _dragon_tiger_schema_error(
                f"{report_name} page {page_number} has an incomplete record count"
            )
        records.extend(page_records)
        raw_pages.append({"pageNumber": page_number, "response": decoded})
        page_number += 1
    assert expected_pages is not None
    assert expected_count is not None
    if len(records) != expected_count:
        raise _dragon_tiger_schema_error(f"{report_name} total record count is inconsistent")
    identities = [
        _dragon_tiger_raw_identity(report_name, row, row_index)
        for row_index, row in enumerate(records)
    ]
    if len(set(identities)) != expected_count:
        raise _dragon_tiger_schema_error(
            f"{report_name} contains duplicate rows across one or more pages"
        )
    return (
        records,
        {
            "reportName": report_name,
            "filter": _dragon_tiger_filter(start, end),
            "pageSize": _DRAGON_TIGER_PAGE_SIZE,
            "sortColumns": ",".join(field for field, _ in sort),
            "sortTypes": ",".join(direction for _, direction in sort),
            "pages": expected_pages,
            "count": expected_count,
            "rawPages": raw_pages,
        },
    )


async def _request_dragon_tiger_page(
    *,
    client: httpx.AsyncClient,
    report_name: str,
    fields: tuple[str, ...],
    sort: tuple[tuple[str, str], ...],
    start: date,
    end: date,
    page_number: int,
    request_timeout_seconds: int,
    page_evidence: list[dict[str, object]],
) -> tuple[bytes, dict[str, object]]:
    """为单页执行真实 HTTP timeout 与有限重试，仅重试网络、`429` 和 `5xx`。"""
    params = {
        "reportName": report_name,
        "columns": ",".join(fields),
        "filter": _dragon_tiger_filter(start, end),
        "pageNumber": str(page_number),
        "pageSize": str(_DRAGON_TIGER_PAGE_SIZE),
        "sortColumns": ",".join(field for field, _ in sort),
        "sortTypes": ",".join(direction for _, direction in sort),
        "source": "WEB",
        "client": "WEB",
    }
    for attempt in range(1, _DRAGON_TIGER_REPORT_ATTEMPTS + 1):
        try:
            response = await _dragon_tiger_http_get(
                client,
                params=params,
                request_timeout=httpx.Timeout(
                    request_timeout_seconds,
                    connect=min(5.0, request_timeout_seconds),
                ),
            )
        except httpx.TransportError as error:
            if attempt >= _DRAGON_TIGER_REPORT_ATTEMPTS:
                raise ProviderError(
                    ProviderErrorCode.UNAVAILABLE,
                    f"{report_name} page {page_number} network request failed",
                    retryable=True,
                ) from error
            await _dragon_tiger_retry_delay(attempt=attempt)
            continue
        body = response.content
        page_evidence.append(
            {
                "reportName": report_name,
                "pageNumber": page_number,
                "attempt": attempt,
                "statusCode": response.status_code,
                "byteSize": len(body),
                "sha256": hashlib.sha256(body).hexdigest(),
            }
        )
        if len(body) > _DRAGON_TIGER_MAX_PAGE_BYTES:
            raise _dragon_tiger_schema_error(f"{report_name} page exceeds the byte limit")
        if response.status_code == 429 or response.status_code >= 500:
            if attempt < _DRAGON_TIGER_REPORT_ATTEMPTS:
                await _dragon_tiger_retry_delay(attempt=attempt)
                continue
            code = (
                ProviderErrorCode.RATE_LIMITED
                if response.status_code == 429
                else ProviderErrorCode.UNAVAILABLE
            )
            raise ProviderError(
                code,
                f"{report_name} page {page_number} upstream is temporarily unavailable",
                retryable=True,
            )
        if response.status_code != 200:
            raise _dragon_tiger_schema_error(
                f"{report_name} page {page_number} returned HTTP {response.status_code}"
            )
        media_type = response.headers.get("Content-Type", "").partition(";")[0].strip().lower()
        if media_type not in {"application/json", "text/plain"}:
            raise _dragon_tiger_schema_error(
                f"{report_name} page {page_number} returned an unexpected media type"
            )
        try:
            decoded = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise _dragon_tiger_schema_error(
                f"{report_name} page {page_number} is not valid JSON"
            ) from error
        if not isinstance(decoded, dict):
            raise _dragon_tiger_schema_error(
                f"{report_name} page {page_number} JSON root is not an object"
            )
        return body, decoded
    raise AssertionError("dragon-tiger page retry loop exhausted without a terminal result")


async def _dragon_tiger_http_get(
    client: httpx.AsyncClient,
    *,
    params: dict[str, str],
    request_timeout: httpx.Timeout,
) -> httpx.Response:
    """执行一页原生异步 HTTP 请求；独立函数用于注入传输失败与取消回归。"""
    return await client.get(_DRAGON_TIGER_URL, params=params, timeout=request_timeout)


async def _dragon_tiger_retry_delay(*, attempt: int) -> None:
    """在页级有限重试之间短退避；任务取消会直接打断等待并阻止下一次请求。"""
    delay = _DRAGON_TIGER_RETRY_BASE_SECONDS * (2 ** (attempt - 1))
    if delay > 0:
        await asyncio.sleep(delay)


def _decode_dragon_tiger_page(
    *,
    report_name: str,
    fields: tuple[str, ...],
    page_number: int,
    body: bytes,
    decoded: dict[str, object],
) -> tuple[int, int, list[dict[str, object | None]]]:
    """严格解析东财分页 envelope，并只接受首页明确的合法空结果。"""
    if set(decoded) != _DRAGON_TIGER_ROOT_FIELDS:
        raise _dragon_tiger_schema_error(f"{report_name} response root fields changed")
    if (
        decoded.get("success") is False
        and decoded.get("code") == 9201
        and decoded.get("message") == "返回数据为空"
        and decoded.get("result") is None
    ):
        if page_number != 1:
            raise _dragon_tiger_schema_error(f"{report_name} became empty after page one")
        return 0, 0, []
    if (
        decoded.get("success") is not True
        or decoded.get("code") != 0
        or decoded.get("message") != "ok"
    ):
        raise _dragon_tiger_schema_error(f"{report_name} returned an unsuccessful envelope")
    result = decoded.get("result")
    if not isinstance(result, dict) or set(result) != _DRAGON_TIGER_RESULT_FIELDS:
        raise _dragon_tiger_schema_error(f"{report_name} result envelope fields changed")
    pages = result.get("pages")
    count = result.get("count")
    data = result.get("data")
    if (
        isinstance(pages, bool)
        or not isinstance(pages, int)
        or isinstance(count, bool)
        or not isinstance(count, int)
        or not isinstance(data, list)
        or pages < 1
        or pages > _DRAGON_TIGER_MAX_PAGES
        or count < 1
        or count > _DRAGON_TIGER_MAX_RECORDS
        or pages != (count + _DRAGON_TIGER_PAGE_SIZE - 1) // _DRAGON_TIGER_PAGE_SIZE
    ):
        raise _dragon_tiger_schema_error(f"{report_name} pagination bounds are invalid")
    expected_fields = set(fields)
    records: list[dict[str, object | None]] = []
    for row_index, value in enumerate(data):
        if not isinstance(value, dict) or set(value) != expected_fields:
            raise _dragon_tiger_schema_error(
                f"{report_name} page {page_number} row {row_index} fields changed"
            )
        records.append({str(key): _json_value(item) for key, item in value.items()})
    if not body:
        raise _dragon_tiger_schema_error(f"{report_name} page {page_number} body is empty")
    return pages, count, records


def _dragon_tiger_filter(start: date, end: date) -> str:
    """构造仅含显式日期窗的冻结过滤式，证券选择器只能在完整批次对账后应用。"""
    return f"(TRADE_DATE<='{end.isoformat()}')(TRADE_DATE>='{start.isoformat()}')"


def _dragon_tiger_raw_identity(
    report_name: str,
    row: dict[str, object | None],
    row_index: int,
) -> tuple[str, ...]:
    """构造报表内唯一行键；相同页或跨页重复都视为不完整分页。"""
    event_key = _dragon_tiger_event_key(row, row_kind=report_name, row_index=row_index)
    if report_name == _DRAGON_TIGER_HEAD_REPORT:
        return event_key
    seat_name = _optional_text(row.get("OPERATEDEPT_NAME"))
    seat_code = _optional_text(row.get("OPERATEDEPT_CODE"))
    buy, sell, net = _dragon_tiger_source_seat_amounts(
        row,
        row_kind=report_name,
        row_index=row_index,
    )
    if seat_name is None or seat_code is None:
        raise _malformed_event_candidate(
            capability=_DRAGON_TIGER,
            row_kind=report_name,
            row_index=row_index,
            reason="missing seat code or name",
        )
    return (*event_key, seat_code, seat_name, buy, sell, net)


def _normalize_dragon_tiger(
    parameters: dict[str, str],
    *,
    head_rows: list[dict[str, object | None]],
    buy_rows: list[dict[str, object | None]],
    sell_rows: list[dict[str, object | None]],
) -> dict[str, Any]:
    """按事件键联接三份批量报表，完整对账后再输出目标证券事件。"""
    start, end = _window(parameters)
    instrument_symbol = _event_instrument_symbol(parameters)
    heads: dict[tuple[str, ...], tuple[int, dict[str, object | None]]] = {}
    for row_index, row in enumerate(head_rows):
        key = _dragon_tiger_event_key(row, row_kind="head", row_index=row_index)
        _dragon_tiger_key_in_window(key, start=start, end=end, row_kind="head", row_index=row_index)
        if key in heads:
            raise _malformed_event_candidate(
                capability=_DRAGON_TIGER,
                row_kind="head",
                row_index=row_index,
                reason="duplicate event key",
            )
        heads[key] = (row_index, row)
    grouped_buy = _group_dragon_tiger_seats(buy_rows, side="BUY", start=start, end=end)
    grouped_sell = _group_dragon_tiger_seats(sell_rows, side="SELL", start=start, end=end)
    head_keys = set(heads)
    orphan_buy = set(grouped_buy) - head_keys
    orphan_sell = set(grouped_sell) - head_keys
    if orphan_buy or orphan_sell:
        raise _dragon_tiger_schema_error("dragon-tiger seat report contains an orphan event")
    events: list[dict[str, object]] = []
    for key in sorted(heads, key=_dragon_tiger_event_sort_key):
        row_index, row = heads[key]
        buy_group = grouped_buy.get(key)
        sell_group = grouped_sell.get(key)
        if not buy_group or not sell_group:
            raise _malformed_event_candidate(
                capability=_DRAGON_TIGER,
                row_kind="head",
                row_index=row_index,
                reason="disclosed head has an empty buy or sell seat list",
            )
        security_code, trade_date_text, reason_text, trade_id = key
        trade_date = date.fromisoformat(trade_date_text)
        buy_amount = _decimal_text(row.get("BILLBOARD_BUY_AMT"))
        sell_amount = _decimal_text(row.get("BILLBOARD_SELL_AMT"))
        net_amount = _decimal_text(row.get("BILLBOARD_NET_AMT"))
        deal_amount = _decimal_text(row.get("BILLBOARD_DEAL_AMT"))
        if None in {buy_amount, sell_amount, net_amount, deal_amount}:
            raise _malformed_event_candidate(
                capability=_DRAGON_TIGER,
                row_kind="head",
                row_index=row_index,
                reason="missing required source aggregate amount",
            )
        assert buy_amount is not None
        assert sell_amount is not None
        assert net_amount is not None
        assert deal_amount is not None
        buy_decimal = Decimal(buy_amount)
        sell_decimal = Decimal(sell_amount)
        if abs(Decimal(net_amount) - (buy_decimal - sell_decimal)) > Decimal("0.01") or abs(
            Decimal(deal_amount) - (buy_decimal + sell_decimal)
        ) > Decimal("0.01"):
            raise _malformed_event_candidate(
                capability=_DRAGON_TIGER,
                row_kind="head",
                row_index=row_index,
                reason="source aggregate amounts do not reconcile",
            )
        # 特殊异常波动事件的头表金额可能采用全市场聚合口径；买卖报表只披露各自前五席位，
        # 两者不得跨口径强求总和相等。这里只校验头表自身及下游每条席位自身的金额恒等式。
        if instrument_symbol is not None and security_code != instrument_symbol:
            continue
        reason_hash = hashlib.sha256(reason_text.encode()).hexdigest()[:16]
        events.append(
            {
                "sourceEventKey": (
                    f"{security_code}:{trade_date.isoformat()}:{trade_id}:{reason_hash}"
                ),
                "securityCode": security_code,
                "tradeDate": trade_date.isoformat(),
                "reasonCode": f"EASTMONEY_{reason_hash}",
                "reasonText": reason_text,
                "closePrice": _decimal_text(row.get("CLOSE_PRICE")),
                "buyAmount": buy_amount,
                "sellAmount": sell_amount,
                "netAmount": net_amount,
                "dealAmount": deal_amount,
                "marketTurnoverAmount": _decimal_text(row.get("ACCUM_AMOUNT")),
                # 头表三个比例字段原生为百分数，统一显式换算为小数比例。
                "dealRatio": _fraction_text(row.get("DEAL_AMOUNT_RATIO")),
                "netRatio": _fraction_text(row.get("DEAL_NET_RATIO")),
                "turnoverRatio": _fraction_text(row.get("TURNOVERRATE")),
                "sourcePublishedAt": None,
                "visibleTimePrecision": "DATE_ONLY",
                "visibleAt": _conservative_visible_at(trade_date),
                "seats": _dragon_tiger_seats(
                    buy_group,
                    "BUY",
                    head_row_index=row_index,
                )
                + _dragon_tiger_seats(
                    sell_group,
                    "SELL",
                    head_row_index=row_index,
                ),
            }
        )
    return {"schema": _SCHEMAS[_DRAGON_TIGER], "events": events}


def _dragon_tiger_event_key(
    row: dict[str, object | None],
    *,
    row_kind: str,
    row_index: int,
) -> tuple[str, str, str, str]:
    """读取代码、日期、原因和 `TRADE_ID` 四元归属键，禁止模糊联接。"""
    security_code = _security_code(row.get("SECURITY_CODE"))
    trade_date = _record_date(row, "TRADE_DATE")
    reason = _optional_text(row.get("EXPLANATION"))
    trade_id = _optional_text(row.get("TRADE_ID"))
    if (
        security_code is None
        or not security_code.isdecimal()
        or trade_date is None
        or reason is None
        or trade_id is None
    ):
        raise _malformed_event_candidate(
            capability=_DRAGON_TIGER,
            row_kind=row_kind,
            row_index=row_index,
            reason="invalid code, date, EXPLANATION, or TRADE_ID",
        )
    return security_code, trade_date.isoformat(), reason, trade_id


def _dragon_tiger_key_in_window(
    key: tuple[str, ...],
    *,
    start: date,
    end: date,
    row_kind: str,
    row_index: int,
) -> None:
    """拒绝供应商在显式过滤后返回的窗外事实，避免覆盖清单夸大日期边界。"""
    trade_date = date.fromisoformat(key[1])
    if not start <= trade_date <= end:
        raise _malformed_event_candidate(
            capability=_DRAGON_TIGER,
            row_kind=row_kind,
            row_index=row_index,
            reason="event date falls outside the requested window",
        )


def _group_dragon_tiger_seats(
    rows: list[dict[str, object | None]],
    *,
    side: str,
    start: date,
    end: date,
) -> dict[tuple[str, ...], list[dict[str, object | None]]]:
    """按严格事件键聚合买榜或卖榜，并保持来源排序作为榜单名次。"""
    groups: dict[tuple[str, ...], list[dict[str, object | None]]] = {}
    identities: set[tuple[str, ...]] = set()
    for row_index, row in enumerate(rows):
        key = _dragon_tiger_event_key(row, row_kind=f"{side.lower()}-seat", row_index=row_index)
        _dragon_tiger_key_in_window(
            key,
            start=start,
            end=end,
            row_kind=f"{side.lower()}-seat",
            row_index=row_index,
        )
        identity = _dragon_tiger_raw_identity(
            _DRAGON_TIGER_BUY_REPORT if side == "BUY" else _DRAGON_TIGER_SELL_REPORT,
            row,
            row_index,
        )
        if identity in identities:
            raise _malformed_event_candidate(
                capability=_DRAGON_TIGER,
                row_kind=f"{side.lower()}-seat",
                row_index=row_index,
                reason="duplicate seat row",
            )
        identities.add(identity)
        groups.setdefault(key, []).append(row)
    return groups


def _dragon_tiger_event_sort_key(key: tuple[str, ...]) -> tuple[str, ...]:
    """按日期、代码、原因和来源交易标识生成稳定事件顺序。"""
    return key[1], key[0], key[2], key[3]


def _dragon_tiger_source_seat_amounts(
    row: dict[str, object | None],
    *,
    row_kind: str,
    row_index: int,
) -> tuple[str, str, str]:
    """保留席位来源金额，并只在 `NET` 恒等式唯一证明时把来源空侧解释为零。"""
    buy = _decimal_text(row.get("BUY"))
    sell = _decimal_text(row.get("SELL"))
    net = _decimal_text(row.get("NET"))
    if net is None or (buy is None and sell is None):
        raise _malformed_event_candidate(
            capability=_DRAGON_TIGER,
            row_kind=row_kind,
            row_index=row_index,
            reason="missing seat amount or NET",
        )
    net_decimal = Decimal(net)
    if buy is None and sell is not None and abs(net_decimal + Decimal(sell)) <= Decimal("0.01"):
        buy = "0"
    if sell is None and buy is not None and abs(net_decimal - Decimal(buy)) <= Decimal("0.01"):
        sell = "0"
    if (
        buy is None
        or sell is None
        or abs(net_decimal - (Decimal(buy) - Decimal(sell))) > Decimal("0.01")
    ):
        raise _malformed_event_candidate(
            capability=_DRAGON_TIGER,
            row_kind=row_kind,
            row_index=row_index,
            reason="seat BUY, SELL, and NET do not reconcile",
        )
    return buy, sell, net


def _attach_dragon_tiger_failure_evidence(
    error: ProviderError,
    *,
    parameters: dict[str, str],
    page_evidence: list[dict[str, object]],
    failure_kind: str,
) -> None:
    """把已收到页面的脱敏哈希清单附到失败，既可定位又不复制供应商原文。"""
    evidence = {
        "schema": "quant-v2.provider-failure-evidence.v1",
        "provider": "akshare",
        "capability": _DRAGON_TIGER,
        "errorCode": error.code.value,
        "retryable": error.retryable,
        "failureKind": failure_kind,
        "request": {
            "start": parameters.get("start"),
            "end": parameters.get("end"),
            "instrument": parameters.get("instrument"),
        },
        "fetchedPages": sorted(
            page_evidence,
            # 此回调只稳定哈希清单顺序，避免并发或重试时序改变失败证据摘要。
            key=lambda item: (
                str(item["reportName"]),
                str(item["pageNumber"]).zfill(8),
                str(item["attempt"]).zfill(4),
            ),
        ),
    }
    error.attach_failure_evidence(_json_bytes(evidence))


def _dragon_tiger_schema_error(message: str) -> ProviderError:
    """构造不可重试的龙虎榜批量 schema 或跨报表一致性错误。"""
    return ProviderError(ProviderErrorCode.SCHEMA, message, retryable=False)


def _block_trades(parameters: dict[str, str]) -> tuple[dict[str, Any], dict[str, Any]]:
    """读取 A 股大宗逐笔数据，并以来源行稳定字段保留合法重复成交。"""
    start, end = _window(parameters)
    instrument_symbol = _event_instrument_symbol(parameters)
    frame = _akshare_frame_or_empty(
        lambda: ak.stock_dzjy_mrmx(
            symbol="A股", start_date=start.strftime("%Y%m%d"), end_date=end.strftime("%Y%m%d")
        )
    )
    raw_records = _frame_records(frame)
    trades: list[dict[str, object]] = []
    occurrences: dict[str, int] = {}
    for row_index, row in enumerate(raw_records):
        identity = _event_candidate_identity(
            row,
            capability=_BLOCK_TRADE,
            row_kind="trade",
            row_index=row_index,
            code_key="证券代码",
            date_key="交易日期",
            instrument_symbol=instrument_symbol,
            start=start,
            end=end,
        )
        if identity is None:
            continue
        security_code, trade_date = identity
        price = _decimal_text(row.get("成交价"))
        volume = _decimal_text(row.get("成交量"))
        amount = _decimal_text(row.get("成交额"))
        buyer_name = _optional_text(row.get("买方营业部"))
        seller_name = _optional_text(row.get("卖方营业部"))
        if (
            price is None
            or volume is None
            or amount is None
            or buyer_name is None
            or seller_name is None
        ):
            raise _malformed_event_candidate(
                capability=_BLOCK_TRADE,
                row_kind="trade",
                row_index=row_index,
                reason="missing required price, volume, amount, buyer, or seller",
            )
        quantity = _block_trade_quantity(Decimal(price), Decimal(volume), Decimal(amount))
        if quantity is None:
            raise _malformed_event_candidate(
                capability=_BLOCK_TRADE,
                row_kind="trade",
                row_index=row_index,
                reason="price, volume, and amount cannot be reconciled",
            )
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
                # 量价乘积只用于确认数量口径；成交额必须保留来源报告值及其舍入结果。
                "notionalCny": amount,
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


def _equity_trading_status(
    parameters: dict[str, str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """读取指定观察日明确披露的普通停牌证券，不以响应缺席推导成交状态。"""
    observation_date = _date_parameter(parameters, "observationDate")
    _reject_extra_parameters(parameters, {"observationDate"})
    frame = _akshare_frame_or_empty(
        lambda: ak.stock_tfp_em(date=observation_date.strftime("%Y%m%d"))
    )
    raw_records = _frame_records(frame)
    statuses: list[dict[str, object]] = []
    for record in raw_records:
        symbol = _security_code(record.get("代码"))
        market = _equity_market(record.get("所属市场"))
        if symbol is None or market is None:
            continue
        suspended_on = _record_date(record, "停牌时间")
        expected_resume_on = _record_date(record, "预计复牌时间")
        statuses.append(
            {
                "symbol": symbol,
                "market": market,
                "status": "SUSPENDED",
                "suspendedOn": None if suspended_on is None else suspended_on.isoformat(),
                "expectedResumeOn": (
                    None if expected_resume_on is None else expected_resume_on.isoformat()
                ),
                "reason": _optional_text(record.get("停牌原因")),
            }
        )
    return (
        {
            "schema": _SCHEMAS[_EQUITY_TRADING_STATUS],
            "observationDate": observation_date.isoformat(),
            "statuses": statuses,
        },
        {
            "capability": _EQUITY_TRADING_STATUS,
            "parameters": parameters,
            "records": raw_records,
        },
    )


def _equity_share_capital(
    parameters: dict[str, str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """读取一只 A 股来源报告的完整股本历史，不由行情或供应商市值反推股数。"""
    venue, symbol = _equity(parameters)
    _reject_extra_parameters(parameters, {"instrument"})
    provider_symbol = f"{symbol}.{_eastmoney_venue_suffix(venue)}"
    frame = _akshare_frame_or_empty(lambda: ak.stock_zh_a_gbjg_em(symbol=provider_symbol))
    raw_records = _frame_records(frame)
    structures: list[dict[str, object]] = []
    for record in raw_records:
        effective_on = _record_date(record, "变更日期")
        total_shares = _decimal_text(record.get("总股本"))
        if effective_on is None or total_shares is None or Decimal(total_shares) <= 0:
            continue
        listed_a = _decimal_text(record.get("已上市流通A股"))
        restricted = _decimal_text(record.get("流通受限股份"))
        structures.append(
            {
                "effectiveOn": effective_on.isoformat(),
                "totalShares": total_shares,
                "listedTradableAShares": listed_a,
                "restrictedShares": restricted,
                "changeReason": _optional_text(record.get("变动原因")),
            }
        )
    return (
        {
            "schema": _SCHEMAS[_EQUITY_SHARE_CAPITAL],
            "instrument": {"exchange": venue, "symbol": symbol},
            "structures": structures,
        },
        {
            "capability": _EQUITY_SHARE_CAPITAL,
            "parameters": parameters,
            "records": raw_records,
        },
    )


def _sw_membership(parameters: dict[str, str]) -> tuple[dict[str, Any], dict[str, Any]]:
    """读取一个申万三级节点的当前完整成分；历史观察日请求必须失败而非回填当前值。"""
    observation_date = _date_parameter(parameters, "observationDate")
    node_code = parameters.get("nodeCode", "")
    _reject_extra_parameters(parameters, {"nodeCode", "observationDate"})
    if len(node_code) != 6 or not node_code.isdecimal():
        raise _invalid_request("nodeCode must be a six-digit SW third-level code")
    if observation_date != datetime.now(_SHANGHAI).date():
        raise _invalid_request("SW membership provider only exposes the current snapshot")
    frame, source_html = _sw_membership_frame(node_code)
    raw_records = _frame_records(frame)
    memberships: list[dict[str, object]] = []
    for record in raw_records:
        symbol = _security_code(record.get("股票代码"))
        name = _optional_text(record.get("股票简称"))
        if symbol is None or name is None:
            raise ProviderError(
                ProviderErrorCode.SCHEMA,
                "SW membership contains an invalid security identity",
                retryable=False,
            )
        included_on = _record_date(record, "纳入时间")
        memberships.append(
            {
                "symbol": symbol,
                "name": name,
                "sourceIncludedOn": None if included_on is None else included_on.isoformat(),
                "level1Name": None,
                "level2Name": None,
                "level3Name": None,
            }
        )
    if len({str(item["symbol"]) for item in memberships}) != len(memberships):
        raise ProviderError(
            ProviderErrorCode.SCHEMA,
            "SW membership contains duplicate security codes",
            retryable=False,
        )
    return (
        {
            "schema": _SCHEMAS[_SW_MEMBERSHIP],
            "schemeVersion": "SW2021",
            "nodeCode": node_code,
            "observationDate": observation_date.isoformat(),
            "memberships": memberships,
        },
        {
            "capability": _SW_MEMBERSHIP,
            "parameters": parameters,
            "selectedColumns": list(_SW_MEMBERSHIP_COLUMNS),
            "selectedRecords": raw_records,
            "sourceHtml": source_html,
        },
    )


def _sw_membership_frame(node_code: str) -> tuple[pd.DataFrame, str]:
    """直读真实页面并只按四个明确语义列投影，拒绝表头污染影响身份字段。

    当前 AKShare 1.18.81 会用固定 17 列覆盖已漂移的 18 列页面并抛出异常。这里不按位置
    猜测或修补分析列，只要求 capability 所需的四个命名列唯一存在；完整 HTML 留在失败
    证据载荷中，任何身份列漂移都会以不可重试 schema 错误停止发布。
    """
    try:
        response = requests.get(
            _LEGULEGU_SW_MEMBERSHIP_URL,
            params={"industryCode": f"{node_code}.SI"},
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36"
                )
            },
            timeout=30,
        )
    except requests.RequestException as error:
        raise ProviderError(
            ProviderErrorCode.UNAVAILABLE,
            "SW membership page request failed",
            retryable=True,
        ) from error
    if response.status_code == 429:
        raise ProviderError(
            ProviderErrorCode.RATE_LIMITED,
            "SW membership page is rate limited",
            retryable=True,
        )
    if response.status_code >= 500:
        raise ProviderError(
            ProviderErrorCode.UNAVAILABLE,
            "SW membership page is unavailable",
            retryable=True,
        )
    content = response.content
    if (
        response.status_code != 200
        or not content
        or len(content) > _LEGULEGU_SW_MEMBERSHIP_MAX_BYTES
    ):
        raise ProviderError(
            ProviderErrorCode.SCHEMA,
            "SW membership page response is not a bounded HTML document",
            retryable=False,
        )
    try:
        source_html = content.decode(response.encoding or "utf-8")
        tables = pd.read_html(StringIO(source_html))
    except (UnicodeDecodeError, ValueError) as error:
        raise ProviderError(
            ProviderErrorCode.SCHEMA,
            "SW membership HTML cannot be decoded",
            retryable=False,
        ) from error
    if len(tables) != 1:
        raise ProviderError(
            ProviderErrorCode.SCHEMA,
            "SW membership page must contain exactly one table",
            retryable=False,
        )
    frame = tables[0]
    columns = [str(value).strip() for value in frame.columns]
    if any(columns.count(required) != 1 for required in _SW_MEMBERSHIP_COLUMNS):
        raise ProviderError(
            ProviderErrorCode.SCHEMA,
            "SW membership identity columns are missing or duplicated",
            retryable=False,
        )
    selected = frame.loc[:, list(_SW_MEMBERSHIP_COLUMNS)].copy()
    if selected.empty:
        raise ProviderError(
            ProviderErrorCode.SCHEMA,
            "SW membership table contains no securities",
            retryable=False,
        )
    return selected, source_html


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


def _margin_venue(parameters: dict[str, str]) -> str:
    """读取两融场所；北交所仅由资格名单分支进一步允许。"""
    venue = parameters.get("venue")
    if venue not in {"SSE", "SZSE", "BSE"}:
        raise _invalid_request("margin venue must be SSE, SZSE, or BSE")
    return venue


def _equity(parameters: dict[str, str]) -> tuple[str, str]:
    """解析场所限定 A 股身份，阻止代码前缀猜测交易所。"""
    value = parameters.get("instrument", "")
    venue, separator, symbol = value.partition(".")
    if (
        separator != "."
        or venue not in {"SSE", "SZSE", "BSE"}
        or len(symbol) != 6
        or not symbol.isdecimal()
    ):
        raise _invalid_request("instrument must use SSE.SYMBOL, SZSE.SYMBOL or BSE.SYMBOL")
    return venue, symbol


def _eastmoney_venue_suffix(venue: str) -> str:
    """将平台交易所代码映射为东财股本接口接受的场所后缀。"""
    return {"SSE": "SH", "SZSE": "SZ", "BSE": "BJ"}[venue]


def _equity_market(value: object) -> str | None:
    """只接受来源明确市场标签；未知文本不能以证券代码前缀补全。"""
    text = _optional_text(value)
    if text is None:
        return None
    return {
        "沪市": "SSE",
        "上海证券交易所": "SSE",
        "深市": "SZSE",
        "深圳证券交易所": "SZSE",
        "京市": "BSE",
        "北京证券交易所": "BSE",
    }.get(text)


def _reject_extra_parameters(parameters: dict[str, str], allowed: set[str]) -> None:
    """拒绝未冻结参数，避免调用方误以为 Provider 已支持历史或额外过滤能力。"""
    unexpected = sorted(set(parameters) - allowed)
    if unexpected:
        raise _invalid_request(f"unsupported parameters: {','.join(unexpected)}")


def _window(parameters: dict[str, str]) -> tuple[date, date]:
    """解析包含端日期窗并拒绝倒置窗口。"""
    start = _date_parameter(parameters, "start")
    end = _date_parameter(parameters, "end")
    if start > end:
        raise _invalid_request("start must not be after end")
    return start, end


def _event_instrument_symbol(parameters: dict[str, str]) -> str | None:
    """解析事件能力可选的交易所限定证券；过滤仍在标准化边界内完成。"""
    _reject_extra_parameters(parameters, {"start", "end", "instrument"})
    value = parameters.get("instrument")
    if value is None:
        return None
    exchange, separator, symbol = value.partition(".")
    if (
        separator != "."
        or exchange not in {"SSE", "SZSE", "BSE"}
        or len(symbol) != 6
        or not symbol.isdecimal()
    ):
        raise _invalid_request("instrument must use SSE.SYMBOL, SZSE.SYMBOL or BSE.SYMBOL")
    return symbol


def _event_candidate_identity(
    row: dict[str, object | None],
    *,
    capability: str,
    row_kind: str,
    row_index: int,
    code_key: str,
    date_key: str,
    instrument_symbol: str | None,
    start: date,
    end: date,
) -> tuple[str, date] | None:
    """确认一行是否属于请求窗口；无法证明为非目标的坏身份必须失败关闭。

    只有代码明确不同或日期明确在窗外时才能排除候选；其余坏代码、坏日期都可能代表目标
    事实。全市场请求也遵循相同边界，防止供应商坏行被误写成完整空集。
    """
    normalized_code = _security_code(row.get(code_key))
    security_code = (
        normalized_code if normalized_code is not None and normalized_code.isdecimal() else None
    )
    if (
        instrument_symbol is not None
        and security_code is not None
        and security_code != instrument_symbol
    ):
        return None
    event_date = _record_date(row, date_key)
    if event_date is not None and not start <= event_date <= end:
        return None
    if security_code is None:
        raise _malformed_event_candidate(
            capability=capability,
            row_kind=row_kind,
            row_index=row_index,
            reason=f"invalid security field {code_key}",
        )
    if event_date is None:
        raise _malformed_event_candidate(
            capability=capability,
            row_kind=row_kind,
            row_index=row_index,
            reason=f"invalid date field {date_key}",
        )
    return security_code, event_date


def _malformed_event_candidate(
    *,
    capability: str,
    row_kind: str,
    row_index: int,
    reason: str,
) -> ProviderError:
    """构造不可重试的事件坏行错误，并附带不含供应商原文的定位证据。"""
    error = ProviderError(
        ProviderErrorCode.SCHEMA,
        f"{capability} {row_kind} row {row_index} cannot be reconciled: {reason}",
        retryable=False,
    )
    error.attach_failure_evidence(
        _json_bytes(
            {
                "schema": "quant-v2.provider-failure-evidence.v1",
                "provider": "akshare",
                "capability": capability,
                "errorCode": ProviderErrorCode.SCHEMA.value,
                "retryable": False,
                "failureKind": "MALFORMED_EVENT_CANDIDATE",
                "rowKind": row_kind,
                "rowIndex": row_index,
                "reason": reason,
            }
        )
    )
    return error


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


def _dragon_tiger_seats(
    rows: list[dict[str, object | None]],
    side: str,
    *,
    head_row_index: int,
) -> list[dict[str, object]]:
    """转换一个批量买卖榜单；任一候选坏行都阻断完整事件发布。"""
    seats: list[dict[str, object]] = []
    for rank, row in enumerate(rows, start=1):
        seat_code = _optional_text(row.get("OPERATEDEPT_CODE"))
        seat_name = _optional_text(row.get("OPERATEDEPT_NAME"))
        buy, sell, net = _dragon_tiger_source_seat_amounts(
            row,
            row_kind=f"{side.lower()}-seat-for-head-{head_row_index}",
            row_index=rank - 1,
        )
        if seat_code is None or seat_name is None:
            raise _malformed_event_candidate(
                capability=_DRAGON_TIGER,
                row_kind=f"{side.lower()}-seat-for-head-{head_row_index}",
                row_index=rank - 1,
                reason="missing seat code or name",
            )
        seats.append(
            {
                "listSide": side,
                "rank": str(rank),
                "seatCode": seat_code,
                "seatName": seat_name,
                "buyAmount": buy,
                "sellAmount": sell,
                "netAmount": net,
                # 东财批量报表的 `TOTAL_*RIO` 已是小数比例，不可再除以 100。
                "buyRatio": _decimal_text(row.get("TOTAL_BUYRIO")),
                "sellRatio": _decimal_text(row.get("TOTAL_SELLRIO")),
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


def _currently_unsupported(
    *, capability: str, parameters: dict[str, str], reason_code: str
) -> ProviderError:
    """构造带脱敏请求摘要的当前不支持错误，禁止以成功空数组替代来源缺口。"""
    error = ProviderError(
        ProviderErrorCode.CURRENTLY_UNSUPPORTED,
        f"{capability} is currently unsupported: {reason_code}",
        retryable=False,
    )
    request_material = _json_bytes(
        {
            "capability": capability,
            "parameters": sorted(parameters.items()),
        }
    )
    error.attach_failure_evidence(
        _json_bytes(
            {
                "schema": "quant-v2.provider-failure-evidence.v1",
                "provider": "akshare",
                "capability": capability,
                "errorCode": ProviderErrorCode.CURRENTLY_UNSUPPORTED.value,
                "retryable": False,
                "reasonCode": reason_code,
                # 仅暴露参数名与稳定摘要，避免把证券、日期或调用方选择器复制到失败证据。
                "request": {
                    "parameterNames": sorted(parameters),
                    "requestFingerprint": hashlib.sha256(request_material).hexdigest(),
                },
            }
        )
    )
    return error
