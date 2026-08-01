"""AKShare P0 adapter 的标准载荷映射回归测试。"""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta
from io import BytesIO
from typing import Any
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import httpx
import pandas as pd
import pytest
import requests

from service_data_sync.application.ports.data_source import (
    ProviderError,
    ProviderErrorCode,
    SourceRequest,
)
from service_data_sync.infrastructure.providers.akshare.p0_market_data import (
    AkshareP0MarketDataAdapter,
    _block_trades,
    _derivative_bars,
    _etf_bars,
    _etf_master,
    _etf_nav,
    _etf_status,
    _fetch_corporate_events,
    _fetch_dragon_tiger_bulk,
    _fetch_payload,
    _frame_records,
    _margin_eligibility,
    _margin_market,
    _margin_security,
    _normalize_corporate_events,
    _normalize_dragon_tiger,
    _stock_connect_market,
)


class _FakeResponse:
    """提供 provider HTTP 单测所需的最小响应接口。"""

    def __init__(
        self,
        *,
        content: bytes,
        payload: dict[str, object] | None = None,
        status_code: int = 200,
        content_type: str = "application/json;charset=UTF-8",
    ) -> None:
        """保存固定响应字节、可选 JSON 根对象、状态码和媒体类型。"""
        self.content = content
        self._payload = payload
        self.status_code = status_code
        self.headers = {"Content-Type": content_type}

    def json(self) -> dict[str, object]:
        """返回固定 JSON；未配置时模拟无法解析的响应。"""
        if self._payload is None:
            raise ValueError("测试响应未配置 JSON")
        return self._payload


def _window(**extra: str) -> dict[str, str]:
    """构造所有有界 P0 请求共用的测试日期窗口。"""
    return {"start": "2026-07-28", "end": "2026-07-29", **extra}


def _dragon_head_row(
    *,
    code: object = "000001",
    trade_date: object = "2026-07-28 00:00:00",
    reason: object = "日涨幅偏离值达7%",
    trade_id: object = "1001",
    buy: object = 100,
    sell: object = 50,
    net: object = 50,
    deal: object = 150,
) -> dict[str, object]:
    """构造东财龙虎榜头表冻结字段行，并允许专项测试注入坏字段。"""
    return {
        "SECURITY_CODE": code,
        "TRADE_DATE": trade_date,
        "EXPLANATION": reason,
        "TRADE_ID": trade_id,
        "CLOSE_PRICE": 10,
        "BILLBOARD_NET_AMT": net,
        "BILLBOARD_BUY_AMT": buy,
        "BILLBOARD_SELL_AMT": sell,
        "BILLBOARD_DEAL_AMT": deal,
        "ACCUM_AMOUNT": 1000,
        "DEAL_NET_RATIO": 5,
        "DEAL_AMOUNT_RATIO": 15,
        "TURNOVERRATE": 1,
    }


def _dragon_seat_row(
    *,
    code: object = "000001",
    trade_date: object = "2026-07-28 00:00:00",
    reason: object = "日涨幅偏离值达7%",
    trade_id: object = "1001",
    seat_code: object = "10001",
    seat_name: object = "样本营业部",
    buy: object = 100,
    sell: object = 0,
    net: object = 100,
) -> dict[str, object]:
    """构造东财批量买卖席位冻结字段行，并保留来源金额用于恒等式测试。"""
    return {
        "SECURITY_CODE": code,
        "TRADE_DATE": trade_date,
        "EXPLANATION": reason,
        "TRADE_ID": trade_id,
        "OPERATEDEPT_CODE": seat_code,
        "OPERATEDEPT_NAME": seat_name,
        "BUY": buy,
        "SELL": sell,
        "NET": net,
        "TOTAL_BUYRIO": 0.1,
        "TOTAL_SELLRIO": 0,
    }


def _dragon_page_response(
    *,
    records: list[dict[str, object]],
    pages: int = 1,
    count: int | None = None,
    status_code: int = 200,
) -> httpx.Response:
    """构造带真实请求对象的东财分页 HTTP 响应，供原生异步 transport 单测。"""
    payload: dict[str, object] = {
        "version": "test",
        "result": {
            "pages": pages,
            "data": records,
            "count": len(records) if count is None else count,
        },
        "success": True,
        "message": "ok",
        "code": 0,
    }
    return httpx.Response(
        status_code,
        json=payload,
        headers={"Content-Type": "text/plain;charset=UTF-8"},
        request=httpx.Request("GET", "https://example.test"),
    )


class _DragonTigerPageScript:
    """按报表和页码返回固定 HTTP 结果，并记录真实 timeout 与过滤参数。"""

    def __init__(
        self,
        responses: dict[tuple[str, int], list[httpx.Response | BaseException]],
        *,
        delay_seconds: float = 0,
    ) -> None:
        """复制可变响应队列，避免测试执行消费调用方传入的 fixture。"""
        self._responses = {key: list(values) for key, values in responses.items()}
        self._delay_seconds = delay_seconds
        self.calls: list[tuple[dict[str, str], httpx.Timeout]] = []

    async def __call__(
        self,
        _client: httpx.AsyncClient,
        *,
        params: dict[str, str],
        request_timeout: httpx.Timeout,
    ) -> httpx.Response:
        """返回目标页下一项；异常项原样抛出以验证有限重试分类。"""
        self.calls.append((dict(params), request_timeout))
        if self._delay_seconds > 0:
            await asyncio.sleep(self._delay_seconds)
        key = (params["reportName"], int(params["pageNumber"]))
        values = self._responses[key]
        value = values.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


def _normalize_corporate_fixture(
    parameters: dict[str, str],
    *,
    guidance: pd.DataFrame,
    express: pd.DataFrame,
    report_period: date = date(2026, 6, 30),
) -> dict[str, Any]:
    """把既有中文行 fixture 送入纯标准化器，不再调用旧 AKShare 阻塞 SDK。"""
    return _normalize_corporate_events(
        parameters,
        groups=[
            (
                report_period,
                _frame_records(guidance),
                _frame_records(express),
            )
        ],
    )


def _earnings_guidance_row(
    *,
    code: object = "600519",
    notice_date: object = "2026-07-28 00:00:00",
    report_date: object = "2026-06-30 00:00:00",
) -> dict[str, object]:
    """构造业绩预告冻结英文来源字段，公告日与报告期保持显式分离。"""
    return {
        "SECURITY_CODE": code,
        "SECURITY_NAME_ABBR": "贵州茅台",
        "NOTICE_DATE": notice_date,
        "REPORT_DATE": report_date,
        "PREDICT_FINANCE": "净利润",
        "PREDICT_CONTENT": "预增",
        "FORECAST_JZ": 100,
        "INCREASE_JZ": 10,
        "CHANGE_REASON_EXPLAIN": "样本原因",
        "PREDICT_TYPE": "预增",
        "PREYEAR_SAME_PERIOD": 90,
        "ORG_CODE": "10000001",
        "IS_LATEST": "T",
    }


def _earnings_express_row(
    *,
    code: object = "600519",
    notice_date: object = "2026-07-29 00:00:00",
    report_date: object = "2026-06-30 00:00:00",
) -> dict[str, object]:
    """构造业绩快报冻结英文来源字段，使用 `NOTICE_DATE` 作为公开可用日期。"""
    return {
        "SECURITY_CODE": code,
        "SECURITY_NAME_ABBR": "贵州茅台",
        "NOTICE_DATE": notice_date,
        "REPORT_DATE": report_date,
        "BASIC_EPS": 1,
        "TOTAL_OPERATE_INCOME": 1000,
        "TOTAL_OPERATE_INCOME_SQ": 900,
        "PARENT_NETPROFIT": 100,
        "PARENT_NETPROFIT_SQ": 90,
        "PARENT_BVPS": 10,
        "WEIGHTAVG_ROE": 5,
        "PUBLISHNAME": "饮料制造",
        "ORG_CODE": "10000001",
        "ISNEW": "1",
    }


class _EarningsPageScript:
    """按报表、报告期和页码返回业绩 HTTP fixture，并记录 timeout 与调用顺序。"""

    def __init__(
        self,
        responses: dict[tuple[str, str, int], list[httpx.Response | BaseException]],
        *,
        delay_seconds: float = 0,
    ) -> None:
        """复制响应队列，保证每个测试可独立消费同名页面。"""
        self._responses = {key: list(values) for key, values in responses.items()}
        self._delay_seconds = delay_seconds
        self.calls: list[tuple[dict[str, str], httpx.Timeout]] = []

    async def __call__(
        self,
        _client: httpx.AsyncClient,
        *,
        params: dict[str, str],
        request_timeout: httpx.Timeout,
    ) -> httpx.Response:
        """返回当前业绩页脚本项；异常用于验证 transport 重试边界。"""
        self.calls.append((dict(params), request_timeout))
        if self._delay_seconds > 0:
            await asyncio.sleep(self._delay_seconds)
        period = params["filter"].rsplit("'", maxsplit=2)[1]
        key = (params["reportName"], period, int(params["pageNumber"]))
        value = self._responses[key].pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


def _eastmoney_nav_response() -> _FakeResponse:
    """构造一页同时含净值和申赎状态的真实字段形状。"""
    payload: dict[str, object] = {
        "Data": {
            "LSJZList": [
                {
                    "FSRQ": "2026-07-28",
                    "DWJZ": "1.2",
                    "LJJZ": "1.4",
                    "NAVTYPE": "1",
                    "SGZT": "开放申购",
                    "SHZT": "开放赎回",
                }
            ]
        },
        "ErrCode": 0,
        "ErrMsg": None,
        "TotalCount": 1,
        "PageSize": 100,
        "PageIndex": 1,
    }
    return _FakeResponse(
        content=json.dumps(payload, ensure_ascii=False).encode(),
        payload=payload,
    )


def _xlsx_response(frame: pd.DataFrame) -> _FakeResponse:
    """把给定表格编码为深交所目录的 XLSX 测试响应。"""
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        frame.to_excel(writer, index=False)
    return _FakeResponse(content=output.getvalue())


def _sse_query_response(
    *,
    sql_id: str,
    result: list[dict[str, object]],
    content_type: str = "application/json;charset=UTF-8",
) -> _FakeResponse:
    """构造上交所 commonQuery 当前目录的真实根 envelope。"""
    payload: dict[str, object] = {
        "actionErrors": [],
        "actionMessages": [],
        "fieldErrors": {},
        "isPagination": "false",
        "jsonCallBack": None,
        "locale": "en",
        "pageHelp": {},
        "pageNo": None,
        "pageSize": None,
        "queryDate": "",
        "result": result,
        "securityCode": "",
        "sqlId": sql_id,
        "texts": None,
        "type": "inParams",
        "validateCode": "",
    }
    return _FakeResponse(
        content=json.dumps(payload, ensure_ascii=False).encode(),
        payload=payload,
        content_type=content_type,
    )


def _sse_category_rows() -> list[dict[str, object]]:
    """返回含股票、货币、LOF 与 REITs 的官方类型树形态，验证只接纳 F100 后代。"""
    return [
        {"CATEGORY_CODE": "F000", "CATEGORY_PARENT_CODE": "-", "CATEGORY_NAME": "基金"},
        {"CATEGORY_CODE": "F100", "CATEGORY_PARENT_CODE": "F000", "CATEGORY_NAME": "ETF"},
        {"CATEGORY_CODE": "F110", "CATEGORY_PARENT_CODE": "F100", "CATEGORY_NAME": "股票ETF"},
        {
            "CATEGORY_CODE": "F111",
            "CATEGORY_PARENT_CODE": "F110",
            "CATEGORY_NAME": "单市场股票（沪）ETF",
        },
        {
            "CATEGORY_CODE": "F150",
            "CATEGORY_PARENT_CODE": "F100",
            "CATEGORY_NAME": "交易型货币基金",
        },
        {"CATEGORY_CODE": "F200", "CATEGORY_PARENT_CODE": "F000", "CATEGORY_NAME": "上证LOF"},
        {"CATEGORY_CODE": "F600", "CATEGORY_PARENT_CODE": "F000", "CATEGORY_NAME": "REITs"},
    ]


def _sse_directory_row(
    *,
    symbol: str,
    category: str,
    name: str,
) -> dict[str, object]:
    """构造上交所当前 ETF 目录一行的精确字段集合；SCALE 仅作为 raw 证据存在。"""
    return {
        "COMPANY_NAME": "测试基金管理有限公司",
        "FUND_CODE": symbol,
        "CATEGORY": category,
        "FUND_ABBR": name,
        "COMPANY_CODE": "900001",
        "INDEX_NAME": "-",
        "FUND_EXPANSION_ABBR": name,
        "SCALE": "1.2345",
        "LISTING_DATE": "2020-01-02",
    }


def test_adapter_retries_unclassified_transport_failure_before_returning_batch() -> None:
    """验证交易所网关短暂断连会在总请求预算内重试，而不会生成伪空批次。"""
    expected_payload = {"schema": "quant-v2.margin-eligibility.v1", "records": []}
    expected_raw = {"records": []}
    request = SourceRequest(
        capability="market.margin.eligibility.reported",
        parameters=(("venue", "BSE"), ("start", "2026-07-31"), ("end", "2026-07-31")),
    )
    with (
        patch(
            "service_data_sync.infrastructure.providers.akshare.p0_market_data._fetch_payload",
            side_effect=[
                requests.ConnectionError("gateway closed"),
                (expected_payload, expected_raw, "bse"),
            ],
        ) as fetch,
        patch(
            "service_data_sync.infrastructure.providers.akshare.p0_market_data.asyncio.sleep",
            new=AsyncMock(),
        ) as sleep,
    ):
        batch = asyncio.run(AkshareP0MarketDataAdapter(request_timeout_seconds=5).fetch(request))

    assert json.loads(batch.payload) == expected_payload
    assert fetch.call_count == 2
    sleep.assert_awaited_once_with(0.5)


def test_adapter_does_not_retry_classified_provider_error() -> None:
    """验证已识别的 schema 失败保持 fail-closed，不被传输重试掩盖。"""
    request = SourceRequest(
        capability="market.margin.eligibility.reported",
        parameters=(("venue", "BSE"), ("start", "2026-07-31"), ("end", "2026-07-31")),
    )
    failure = ProviderError(ProviderErrorCode.SCHEMA, "provider schema changed", retryable=False)
    with patch(
        "service_data_sync.infrastructure.providers.akshare.p0_market_data._fetch_payload",
        side_effect=failure,
    ) as fetch:
        with pytest.raises(ProviderError, match="provider schema changed") as raised:
            asyncio.run(AkshareP0MarketDataAdapter(request_timeout_seconds=5).fetch(request))

    assert raised.value.code is ProviderErrorCode.SCHEMA
    assert fetch.call_count == 1


def test_adapter_declares_only_fifteen_real_p0_capabilities() -> None:
    """验证统一 provider 不再把无真源的港通活跃证券伪装为成功 capability。"""
    adapter = AkshareP0MarketDataAdapter(request_timeout_seconds=5)

    assert adapter.provider_id == "akshare"
    assert len(adapter.capabilities()) == 15
    assert "fund.etf.master" in adapter.capabilities()
    assert "derivative.bar.1d.reported" in adapter.capabilities()
    assert "market.stock_connect.active_security.snapshot" not in adapter.capabilities()


def test_etf_status_nav_and_bars_keep_source_units_and_empty_dimensions() -> None:
    """验证 ETF 状态、净值和腾讯未复权日线保留真实来源单位。"""
    bar_frame = pd.DataFrame(
        [
            {
                "date": date(2026, 7, 28),
                "open": 1.0,
                "high": 1.3,
                "low": 0.9,
                "close": 1.2,
                "volume": 100,
                "turnover": 0.1,
                "amount": 12000,
            }
        ]
    )
    parameters = _window(etf="SSE.510300", priceBasis="UNADJUSTED")

    with (
        patch(
            "service_data_sync.infrastructure.providers.akshare.p0_market_data.requests.get",
            side_effect=[_eastmoney_nav_response(), _eastmoney_nav_response()],
        ),
        patch(
            "service_data_sync.infrastructure.providers.akshare.p0_market_data.ak.stock_zh_a_hist_tx",
            return_value=bar_frame,
        ),
    ):
        statuses, _ = _etf_status(parameters)
        navs, _ = _etf_nav(parameters)
        bars, _ = _etf_bars(parameters)

    assert {item["dimension"] for item in statuses["statuses"]} == {"SUBSCRIPTION", "REDEMPTION"}
    assert "TRADING" not in {item["dimension"] for item in statuses["statuses"]}
    assert {item["effectiveTo"] for item in statuses["statuses"]} == {"2026-07-29"}
    assert {item["navKind"] for item in navs["navs"]} == {"UNIT", "ACCUMULATED"}
    assert bars["priceBasis"] == "UNADJUSTED"
    assert bars["bars"] == [
        {
            "tradeDate": "2026-07-28",
            "open": "1.0",
            "high": "1.3",
            "low": "0.9",
            "close": "1.2",
            "volume": "100",
            "volumeUnit": "SHARE",
            "amount": "12000",
            "currency": "CNY",
            "tradeStatus": None,
        }
    ]


def test_money_market_etf_mixed_navtype_is_currently_unsupported() -> None:
    """159001 实际响应的混合 NAVTYPE 属于货币收益语义，不能删掉 0 后冒充单位/累计 NAV。"""
    records = [
        {
            "FSRQ": (date(2026, 7, 28) - timedelta(days=offset)).isoformat(),
            "DWJZ": "0.3256" if offset == 0 else "0.0000",
            "LJJZ": "1.2090",
            "NAVTYPE": "0" if offset == 1 else "1",
            "SGZT": "开放申购",
            "SHZT": "开放赎回",
        }
        for offset in range(26)
    ]
    payload: dict[str, object] = {
        "Data": {"LSJZList": records},
        "ErrCode": 0,
        "ErrMsg": None,
        "TotalCount": 26,
        "PageSize": 100,
        "PageIndex": 1,
    }
    response = _FakeResponse(
        content=json.dumps(payload, ensure_ascii=False).encode(),
        payload=payload,
    )
    with (
        patch(
            "service_data_sync.infrastructure.providers.akshare.p0_market_data.requests.get",
            return_value=response,
        ),
        pytest.raises(ProviderError) as captured,
    ):
        _etf_nav(
            {
                "etf": "SZSE.159001",
                "start": "2026-07-01",
                "end": "2026-07-28",
            }
        )

    assert captured.value.code is ProviderErrorCode.CURRENTLY_UNSUPPORTED
    assert captured.value.retryable is False
    assert captured.value.failure_evidence is not None
    evidence_text = captured.value.failure_evidence.decode()
    evidence = json.loads(evidence_text)
    assert evidence["provider"] == "eastmoney"
    assert evidence["capability"] == "fund.etf.nav.1d.reported"
    assert evidence["request"]["navTypes"] == ["0", "1"]
    assert evidence["request"]["recordDateFrom"] == "2026-07-03"
    assert evidence["request"]["recordDateTo"] == "2026-07-28"
    assert evidence["request"]["rowCount"] == 26
    assert len(evidence["request"]["symbolFingerprint"]) == 64
    assert len(evidence["request"]["rawPayloadSha256"]) == 64
    assert "159001" not in evidence_text
    assert "http" not in evidence_text.lower()


def test_etf_master_uses_official_sse_current_directory_and_category_tree() -> None:
    """上交所当前 F100 目录按官方类别树区分股票与交易型货币基金，不混入 LOF/REITs。"""
    category = _sse_query_response(
        sql_id="COMMON_JJZWZ_JJLB_JJLX_C",
        result=_sse_category_rows(),
    )
    directory = _sse_query_response(
        sql_id="COMMON_JJZWZ_JJLB_L",
        result=[
            _sse_directory_row(symbol="510300", category="F111", name="沪深300ETF"),
            _sse_directory_row(symbol="511600", category="F150", name="货币ETF"),
        ],
    )
    with (
        patch(
            "service_data_sync.infrastructure.providers.akshare.p0_market_data.requests.get",
            side_effect=[category, directory],
        ) as request,
        patch(
            "service_data_sync.infrastructure.providers.akshare.p0_market_data.ak.fund_etf_scale_sse"
        ) as dated_scale,
    ):
        payload, raw, upstream_source = _fetch_payload(
            capability="fund.etf.master",
            parameters={
                "venue": "SSE",
                "observationDate": datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat(),
            },
            request_timeout_seconds=15,
        )

    assert [item["symbol"] for item in payload["profiles"]] == ["510300", "511600"]
    assert payload["profiles"][0]["etfType"] == "单市场股票（沪）ETF"
    assert payload["profiles"][1]["etfType"] == "交易型货币基金"
    assert payload["profiles"][0]["displayName"] == "沪深300ETF"
    assert payload["profiles"][0]["listingStatus"] == "UNKNOWN"
    assert payload["profiles"][0]["sourceTimePrecision"] == "UNKNOWN"
    assert all("scale" not in {key.lower() for key in item} for item in payload["profiles"])
    assert raw["source"] == "sse.official-current-etf-directory"
    assert upstream_source == "sse.official-current-etf-directory"
    assert raw["sourceDataDate"] is None
    assert raw["publicationLagDays"] is None
    assert request.call_count == 2
    assert dated_scale.call_count == 0


def test_szse_master_accepts_only_explicit_etf_rows_from_official_xlsx() -> None:
    """验证深交所官方混合基金目录只发布精确 ETF 类别，不混入 LOF 与 REITs。"""
    frame = pd.DataFrame(
        [
            {
                "基金代码": "159919",
                "基金简称": "沪深300ETF",
                "基金类别": " ETF ",
                "投资类别": "股票型",
                "上市日期": "2012-05-28",
            },
            {
                "基金代码": "160119",
                "基金简称": "南方500LOF",
                "基金类别": "LOF",
                "投资类别": "股票型",
                "上市日期": "2009-10-14",
            },
            {
                "基金代码": "180101",
                "基金简称": "不动产基金",
                "基金类别": "不动产基金",
                "投资类别": "REITs",
                "上市日期": "2021-06-21",
            },
        ]
    )
    with patch(
        "service_data_sync.infrastructure.providers.akshare.p0_market_data.requests.get",
        return_value=_xlsx_response(frame),
    ):
        payload, raw = _etf_master(
            {
                "venue": "SZSE",
                "observationDate": datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat(),
            }
        )

    assert [item["symbol"] for item in payload["profiles"]] == ["159919"]
    assert payload["profiles"][0]["listingStatus"] == "UNKNOWN"
    assert len(raw["records"]) == 3


def test_etf_master_rejects_historical_dates_for_both_current_only_directories() -> None:
    """沪深官方目录都只有当前快照，历史 observationDate 必须在网络请求前失败关闭。"""
    historical_date = (
        datetime.now(ZoneInfo("Asia/Shanghai")).date() - timedelta(days=1)
    ).isoformat()
    with patch(
        "service_data_sync.infrastructure.providers.akshare.p0_market_data.requests.get"
    ) as request:
        for venue in ("SSE", "SZSE"):
            with pytest.raises(ProviderError) as captured:
                _etf_master({"venue": venue, "observationDate": historical_date})
            assert captured.value.code is ProviderErrorCode.INVALID_REQUEST
            assert captured.value.retryable is False
            assert captured.value.failure_evidence is not None
            evidence = json.loads(captured.value.failure_evidence)
            assert evidence["request"] == {
                "venue": venue,
                "requestedObservationDate": historical_date,
                "availableObservationDate": (
                    datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
                ),
            }

    assert request.call_count == 0


def test_szse_explicit_etf_with_invalid_identity_fails_closed() -> None:
    """来源明确标记 ETF 的行缺代码或名称时必须阻断目录，不得静默缩小全集。"""
    frame = pd.DataFrame([{"基金代码": "159919", "基金简称": None, "基金类别": "ETF"}])
    with (
        patch(
            "service_data_sync.infrastructure.providers.akshare.p0_market_data.requests.get",
            return_value=_xlsx_response(frame),
        ),
        pytest.raises(ProviderError) as captured,
    ):
        _etf_master(
            {
                "venue": "SZSE",
                "observationDate": datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat(),
            }
        )

    assert captured.value.code is ProviderErrorCode.SCHEMA
    assert captured.value.retryable is False


def test_szse_directory_retries_one_transport_failure_then_decodes_xlsx() -> None:
    """验证深交所目录网络错误只做有限重试且成功后停止。"""
    frame = pd.DataFrame([{"基金代码": "159919", "基金简称": "沪深300ETF", "基金类别": "ETF"}])
    with patch(
        "service_data_sync.infrastructure.providers.akshare.p0_market_data.requests.get",
        side_effect=[requests.ConnectionError("临时断连"), _xlsx_response(frame)],
    ) as request:
        payload, _ = _etf_master(
            {
                "venue": "SZSE",
                "observationDate": datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat(),
            }
        )

    assert [item["symbol"] for item in payload["profiles"]] == ["159919"]
    assert request.call_count == 2


def test_sse_current_directory_empty_fails_closed_without_dated_scale_fallback() -> None:
    """上交所当前官方目录为空时返回可重试来源失败，不回退份额统计或解释为零产品。"""
    observation_date = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    category = _sse_query_response(
        sql_id="COMMON_JJZWZ_JJLB_JJLX_C",
        result=_sse_category_rows(),
    )
    empty = _sse_query_response(sql_id="COMMON_JJZWZ_JJLB_L", result=[])
    with (
        patch(
            "service_data_sync.infrastructure.providers.akshare.p0_market_data.requests.get",
            side_effect=[category, empty],
        ),
        patch(
            "service_data_sync.infrastructure.providers.akshare.p0_market_data.ak.fund_etf_scale_sse"
        ) as dated_scale,
        pytest.raises(ProviderError) as captured,
    ):
        _etf_master({"venue": "SSE", "observationDate": observation_date})

    assert captured.value.code is ProviderErrorCode.UNAVAILABLE
    assert captured.value.retryable is True
    assert dated_scale.call_count == 0


def test_sse_current_directory_rejects_unknown_fields_and_non_json_media_type() -> None:
    """上交所当前目录字段漂移或非 JSON 媒体类型必须失败关闭。"""
    observation_date = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    category = _sse_query_response(
        sql_id="COMMON_JJZWZ_JJLB_JJLX_C",
        result=_sse_category_rows(),
    )
    invalid_row = {
        **_sse_directory_row(symbol="510300", category="F111", name="沪深300ETF"),
        "UNREVIEWED": "value",
    }
    with (
        patch(
            "service_data_sync.infrastructure.providers.akshare.p0_market_data.requests.get",
            side_effect=[
                category,
                _sse_query_response(
                    sql_id="COMMON_JJZWZ_JJLB_L",
                    result=[invalid_row],
                ),
            ],
        ),
        pytest.raises(ProviderError) as field_error,
    ):
        _etf_master({"venue": "SSE", "observationDate": observation_date})
    with (
        patch(
            "service_data_sync.infrastructure.providers.akshare.p0_market_data.requests.get",
            return_value=_sse_query_response(
                sql_id="COMMON_JJZWZ_JJLB_JJLX_C",
                result=_sse_category_rows(),
                content_type="text/html",
            ),
        ),
        pytest.raises(ProviderError) as media_error,
    ):
        _etf_master({"venue": "SSE", "observationDate": observation_date})

    assert field_error.value.code is ProviderErrorCode.SCHEMA
    assert media_error.value.code is ProviderErrorCode.SCHEMA


@pytest.mark.parametrize(
    "category_rows",
    [
        [
            {"CATEGORY_CODE": "F000", "CATEGORY_PARENT_CODE": "-", "CATEGORY_NAME": "基金"},
            {"CATEGORY_CODE": "F100", "CATEGORY_PARENT_CODE": "F000", "CATEGORY_NAME": "ETF"},
            {
                "CATEGORY_CODE": "F111",
                "CATEGORY_PARENT_CODE": "MISSING",
                "CATEGORY_NAME": "股票ETF",
            },
        ],
        [
            {"CATEGORY_CODE": "F000", "CATEGORY_PARENT_CODE": "-", "CATEGORY_NAME": "基金"},
            {"CATEGORY_CODE": "F100", "CATEGORY_PARENT_CODE": "F000", "CATEGORY_NAME": "ETF"},
            {
                "CATEGORY_CODE": "F110",
                "CATEGORY_PARENT_CODE": "F111",
                "CATEGORY_NAME": "股票ETF",
            },
            {
                "CATEGORY_CODE": "F111",
                "CATEGORY_PARENT_CODE": "F110",
                "CATEGORY_NAME": "单市场股票ETF",
            },
        ],
    ],
)
def test_sse_category_tree_rejects_unknown_parent_and_cycle(
    category_rows: list[dict[str, object]],
) -> None:
    """官方类别树父节点缺失或成环时必须阻断目录，不能靠简称补判 ETF。"""
    with (
        patch(
            "service_data_sync.infrastructure.providers.akshare.p0_market_data.requests.get",
            side_effect=[
                _sse_query_response(
                    sql_id="COMMON_JJZWZ_JJLB_JJLX_C",
                    result=category_rows,
                ),
                _sse_query_response(
                    sql_id="COMMON_JJZWZ_JJLB_L",
                    result=[
                        _sse_directory_row(
                            symbol="510300",
                            category="F111",
                            name="沪深300ETF",
                        )
                    ],
                ),
            ],
        ),
        pytest.raises(ProviderError) as captured,
    ):
        _etf_master(
            {
                "venue": "SSE",
                "observationDate": datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat(),
            }
        )

    assert captured.value.code is ProviderErrorCode.SCHEMA


def test_margin_adapter_maps_sse_and_szse_missing_fields_without_derivation() -> None:
    """验证两融 adapter 保留深市未披露偿还字段为空，并映射深市观察名单。"""
    market_frame = pd.DataFrame(
        [
            {
                "信用交易日期": date(2026, 7, 28),
                "融资余额": 10,
                "融资买入额": 2,
                "融券余量": 3,
                "融券余量金额": 4,
                "融券卖出量": 5,
                "融资融券余额": 14,
            }
        ]
    )
    security_frame = pd.DataFrame(
        [{"证券代码": "000001", "融资余额": 10, "融资买入额": 2, "融券余量": 3}]
    )
    eligibility_frame = pd.DataFrame([{"证券代码": "000001"}])
    with (
        patch(
            "service_data_sync.infrastructure.providers.akshare.p0_market_data.ak.stock_margin_sse",
            return_value=market_frame,
        ),
        patch(
            "service_data_sync.infrastructure.providers.akshare.p0_market_data.ak.stock_margin_detail_szse",
            return_value=security_frame,
        ),
        patch(
            "service_data_sync.infrastructure.providers.akshare.p0_market_data.ak.stock_margin_underlying_info_szse",
            return_value=eligibility_frame,
        ),
    ):
        market, _ = _margin_market(_window(venue="SSE"))
        security, _ = _margin_security(_window(venue="SZSE"))
        eligibility, _ = _margin_eligibility(_window(venue="SZSE"))

    assert market["records"][0]["financingBalance"] == "10"
    assert security["records"][0]["financingRepaymentReported"] is None
    assert security["records"][0]["nullReason"] == "NOT_REPORTED_BY_SOURCE"
    assert eligibility["records"][0]["evidenceBasis"] == "OBSERVED_LIST"


def test_margin_adapter_maps_bse_underlying_flags_without_using_daily_availability() -> None:
    """验证北交所标的列精确映射四种资格，日内可用列只保留在 raw 证据。"""
    frame = pd.DataFrame(
        [
            {
                "证券代码": "920000",
                "证券简称": "全资格",
                "融资标的": "Y",
                "融券标的": "Y",
                "当日可融资": "N",
                "当日可融券": "N",
            },
            {
                "证券代码": "920001",
                "证券简称": "仅融资",
                "融资标的": "Y",
                "融券标的": "N",
                "当日可融资": "Y",
                "当日可融券": "N",
            },
            {
                "证券代码": "920002",
                "证券简称": "仅融券",
                "融资标的": "N",
                "融券标的": "Y",
                "当日可融资": "N",
                "当日可融券": "Y",
            },
            {
                "证券代码": "920003",
                "证券简称": "非标的",
                "融资标的": "N",
                "融券标的": "N",
                "当日可融资": "Y",
                "当日可融券": "Y",
            },
        ]
    )
    parameters = {"venue": "BSE", "start": "2026-07-28", "end": "2026-07-28"}
    with patch(
        "service_data_sync.infrastructure.providers.akshare.p0_market_data.ak.stock_margin_underlying_info_bse",
        return_value=frame,
    ) as fetch:
        payload, raw, upstream_source = _fetch_payload(
            capability="market.margin.eligibility.reported",
            parameters=parameters,
            request_timeout_seconds=5,
        )

    assert fetch.call_args.args == ("20260728",)
    assert upstream_source == "bse.margin-underlying"
    assert [record["status"] for record in payload["records"]] == [
        "ELIGIBLE",
        "FINANCING_ONLY",
        "LENDING_ONLY",
        "INELIGIBLE",
    ]
    assert {"当日可融资", "当日可融券"}.isdisjoint(payload["records"][0])
    assert raw["records"][0]["records"][0]["当日可融资"] == "N"
    assert raw["records"][0]["records"][0]["当日可融券"] == "N"


@pytest.mark.parametrize(
    ("frame", "message"),
    [
        (pd.DataFrame([{"证券代码": "920000"}]), "column contract changed"),
        (
            pd.DataFrame(
                [
                    {
                        "证券代码": "920000",
                        "证券简称": "坏标志",
                        "融资标的": "MAYBE",
                        "融券标的": "Y",
                        "当日可融资": "Y",
                        "当日可融券": "Y",
                    }
                ]
            ),
            "must be Y or N",
        ),
        (
            pd.DataFrame(
                [
                    {
                        "证券代码": "ABC123",
                        "证券简称": "坏代码",
                        "融资标的": "Y",
                        "融券标的": "Y",
                        "当日可融资": "Y",
                        "当日可融券": "Y",
                    }
                ]
            ),
            "identity is invalid",
        ),
    ],
)
def test_margin_adapter_rejects_bse_schema_or_flag_drift(frame: pd.DataFrame, message: str) -> None:
    """北交所中文列或任一 Y/N 标志漂移必须隔离，不能降级为空或猜测。"""
    with (
        patch(
            "service_data_sync.infrastructure.providers.akshare.p0_market_data.ak.stock_margin_underlying_info_bse",
            return_value=frame,
        ),
        pytest.raises(ProviderError, match=message) as captured,
    ):
        _margin_eligibility({"venue": "BSE", "start": "2026-07-28", "end": "2026-07-28"})

    assert captured.value.code is ProviderErrorCode.SCHEMA


def test_akshare_empty_dataframe_construction_error_becomes_a_normal_empty_batch() -> None:
    """验证 AKShare 已知空响应异常不会让非交易日被误判为来源不可用。"""
    with patch(
        "service_data_sync.infrastructure.providers.akshare.p0_market_data.ak.stock_margin_sse",
        side_effect=ValueError(
            "Length mismatch: Expected axis has 0 elements, new values have 13 elements"
        ),
    ):
        payload, raw = _margin_market(_window(venue="SSE"))

    assert payload["records"] == []
    assert raw["records"] == [{"requestedDate": None, "records": []}]


def test_stock_connect_maps_history_to_cny_without_deriving_turnover() -> None:
    """验证港通金额换算为 CNY，接口未给总成交额时保持为空。"""
    frame = pd.DataFrame(
        [
            {
                "日期": date(2026, 7, 28),
                "买入成交额": 12.5,
                "卖出成交额": 10.0,
                "当日成交净买额": 2.5,
                "当日余额": 100.0,
            }
        ]
    )
    parameters = _window(channel="SH", direction="NORTHBOUND")
    with patch(
        "service_data_sync.infrastructure.providers.akshare.p0_market_data.ak.stock_hsgt_hist_em",
        return_value=frame,
    ):
        market, _ = _stock_connect_market(parameters)

    assert market["records"][0]["buyAmount"] == "1250000000.0"
    assert market["records"][0]["turnoverAmount"] is None


def test_adapter_rejects_stock_connect_active_direct_request_with_desensitized_evidence() -> None:
    """无可验证 active-security 真源时，直调必须失败关闭而非返回伪空成功。"""
    adapter = AkshareP0MarketDataAdapter(request_timeout_seconds=5)
    request = SourceRequest(
        capability="market.stock_connect.active_security.snapshot",
        parameters=(
            ("channel", "SH"),
            ("direction", "NORTHBOUND"),
            ("start", "2026-07-28"),
            ("end", "2026-07-28"),
        ),
    )

    assert request.capability not in adapter.capabilities()
    with pytest.raises(ProviderError) as captured:
        asyncio.run(adapter.fetch(request))

    assert captured.value.code is ProviderErrorCode.CURRENTLY_UNSUPPORTED
    assert captured.value.retryable is False
    assert captured.value.failure_evidence is not None
    evidence_text = captured.value.failure_evidence.decode()
    evidence = json.loads(evidence_text)
    assert evidence["reasonCode"] == "NO_VERIFIED_ACTIVE_SECURITY_SOURCE"
    assert evidence["request"]["parameterNames"] == ["channel", "direction", "end", "start"]
    assert len(evidence["request"]["requestFingerprint"]) == 64
    assert "NORTHBOUND" not in evidence_text
    assert "2026-07-28" not in evidence_text


@pytest.mark.parametrize(
    ("capability", "venue", "reason_code"),
    [
        (
            "market.margin.eligibility.reported",
            "SSE",
            "SSE_MARGIN_ELIGIBILITY_NO_UNDERLYING_ENDPOINT",
        ),
        ("market.margin.market.1d.reported", "BSE", "BSE_MARGIN_MARKET_NOT_MAPPED"),
        ("market.margin.security.1d.reported", "BSE", "BSE_MARGIN_SECURITY_NOT_MAPPED"),
    ],
)
def test_adapter_rejects_margin_requests_without_a_mapped_true_source(
    capability: str, venue: str, reason_code: str
) -> None:
    """上交所资格和北交所余额/明细均必须显式不支持，不能被伪空成功掩盖。"""
    adapter = AkshareP0MarketDataAdapter(request_timeout_seconds=5)
    request = SourceRequest(
        capability=capability,
        parameters=(
            ("venue", venue),
            ("start", "2026-07-28"),
            ("end", "2026-07-28"),
        ),
    )

    with pytest.raises(ProviderError) as captured:
        asyncio.run(adapter.fetch(request))

    assert captured.value.code is ProviderErrorCode.CURRENTLY_UNSUPPORTED
    assert captured.value.retryable is False
    assert captured.value.failure_evidence is not None
    evidence = json.loads(captured.value.failure_evidence)
    assert evidence["capability"] == capability
    assert evidence["reasonCode"] == reason_code
    assert evidence["request"]["parameterNames"] == ["end", "start", "venue"]


def test_corporate_adapter_keeps_documents_when_some_metrics_are_unavailable() -> None:
    """验证业绩预告和快报均能产生日期级文档，未知指标不会阻断整批同步。"""
    guidance = pd.DataFrame(
        [
            {
                "股票代码": "000001",
                "股票简称": "样本",
                "公告日期": date(2026, 7, 28),
                "预测指标": "净利润",
                "预测数值": 100,
                "业绩变动幅度": 10,
                "预告类型": "预增",
                "上年同期值": 90,
            }
        ]
    )
    express = pd.DataFrame(
        [
            {
                "股票代码": "000001",
                "股票简称": "样本",
                "公告日期": date(2026, 7, 28),
                "每股收益": 1.0,
                "营业收入-营业收入": 1000,
                "营业收入-去年同期": 900,
                "净利润-净利润": 100,
                "净利润-去年同期": 90,
                "每股净资产": 10,
                "净资产收益率": 5,
            }
        ]
    )
    with (
        patch(
            "service_data_sync.infrastructure.providers.akshare.p0_market_data.ak.stock_yjyg_em",
            return_value=guidance,
        ),
        patch(
            "service_data_sync.infrastructure.providers.akshare.p0_market_data.ak.stock_yjkb_em",
            return_value=express,
        ),
    ):
        payload = _normalize_corporate_fixture(
            _window(),
            guidance=guidance,
            express=express,
        )

    assert payload["documents"]
    assert payload["guidanceMetrics"][0]["metricCode"] == "NET_PROFIT"
    assert any(metric["metricCode"] == "ROE" for metric in payload["expressMetrics"])


def test_dragon_tiger_and_block_trade_require_reconcilable_source_rows() -> None:
    """验证公开交易 adapter 只发布可由金额恒等验证的头、席位和大宗逐笔。"""
    head = [_dragon_head_row()]
    buy_seats = [_dragon_seat_row()]
    sell_seats = [
        _dragon_seat_row(
            seat_code="20001",
            seat_name="样本卖方营业部",
            buy=0,
            sell=50,
            net=-50,
        )
    ]
    trades = pd.DataFrame(
        [
            {
                "交易日期": date(2026, 7, 28),
                "证券代码": "000001",
                "成交价": 10,
                "成交量": 2,
                "成交额": 200000,
                "买方营业部": "买方",
                "卖方营业部": "卖方",
                "收盘价": 10.2,
                "折溢率": -1,
                "序号": 1,
            }
        ]
    )
    with (
        patch(
            "service_data_sync.infrastructure.providers.akshare.p0_market_data.ak.stock_dzjy_mrmx",
            return_value=trades,
        ),
    ):
        dragon_tiger = _normalize_dragon_tiger(
            _window(),
            head_rows=head,
            buy_rows=buy_seats,
            sell_rows=sell_seats,
        )
        block_trades, _ = _block_trades(_window())

    assert dragon_tiger["events"][0]["seats"][0]["listSide"] == "BUY"
    assert dragon_tiger["events"][0]["netAmount"] == "50"
    assert block_trades["trades"][0]["quantityShares"] == "20000"
    assert block_trades["trades"][0]["notionalCny"] == "200000"


def test_event_adapters_keep_only_provider_true_empty_as_empty() -> None:
    """来源确实返回零候选时，三类事件能力仍可形成合法空批次。"""
    empty = pd.DataFrame()
    with (
        patch(
            "service_data_sync.infrastructure.providers.akshare.p0_market_data.ak.stock_yjyg_em",
            return_value=empty,
        ),
        patch(
            "service_data_sync.infrastructure.providers.akshare.p0_market_data.ak.stock_yjkb_em",
            return_value=empty,
        ),
        patch(
            "service_data_sync.infrastructure.providers.akshare.p0_market_data.ak.stock_dzjy_mrmx",
            return_value=empty,
        ),
    ):
        corporate = _normalize_corporate_fixture(
            _window(),
            guidance=empty,
            express=empty,
        )
        dragon_tiger = _normalize_dragon_tiger(
            _window(),
            head_rows=[],
            buy_rows=[],
            sell_rows=[],
        )
        block_trades, _ = _block_trades(_window())

    assert corporate["documents"] == []
    assert corporate["guidanceMetrics"] == []
    assert corporate["expressMetrics"] == []
    assert dragon_tiger["events"] == []
    assert block_trades["trades"] == []


@pytest.mark.parametrize("bad_family", ("guidance", "express"))
def test_corporate_target_bad_identity_fails_closed(bad_family: str) -> None:
    """业绩预告或快报目标行日期坏掉时，禁止把原始候选降级为空覆盖。"""
    bad = pd.DataFrame([{"股票代码": "000001", "公告日期": "不是日期"}])
    empty = pd.DataFrame()
    with (
        patch(
            "service_data_sync.infrastructure.providers.akshare.p0_market_data.ak.stock_yjyg_em",
            return_value=bad if bad_family == "guidance" else empty,
        ),
        patch(
            "service_data_sync.infrastructure.providers.akshare.p0_market_data.ak.stock_yjkb_em",
            return_value=bad if bad_family == "express" else empty,
        ),
        pytest.raises(ProviderError) as captured,
    ):
        _normalize_corporate_fixture(
            _window(instrument="SZSE.000001"),
            guidance=bad if bad_family == "guidance" else empty,
            express=bad if bad_family == "express" else empty,
        )

    assert captured.value.code is ProviderErrorCode.SCHEMA
    assert captured.value.retryable is False
    assert captured.value.failure_evidence is not None
    evidence = json.loads(captured.value.failure_evidence)
    assert evidence["failureKind"] == "MALFORMED_EVENT_CANDIDATE"


@pytest.mark.parametrize("bad_family", ("guidance", "express"))
def test_corporate_target_without_supported_metric_fails_closed(bad_family: str) -> None:
    """目标预告或快报没有任何可映射指标时，禁止只写文档后宣称事件完整。"""
    bad = pd.DataFrame(
        [
            {
                "股票代码": "000001",
                "公告日期": date(2026, 7, 28),
                "预测指标": "未治理指标",
            }
        ]
    )
    empty = pd.DataFrame()
    with (
        patch(
            "service_data_sync.infrastructure.providers.akshare.p0_market_data.ak.stock_yjyg_em",
            return_value=bad if bad_family == "guidance" else empty,
        ),
        patch(
            "service_data_sync.infrastructure.providers.akshare.p0_market_data.ak.stock_yjkb_em",
            return_value=bad if bad_family == "express" else empty,
        ),
        pytest.raises(ProviderError) as captured,
    ):
        _normalize_corporate_fixture(
            _window(instrument="SZSE.000001"),
            guidance=bad if bad_family == "guidance" else empty,
            express=bad if bad_family == "express" else empty,
        )

    assert captured.value.code is ProviderErrorCode.SCHEMA
    assert captured.value.retryable is False
    assert "no supported earnings metric" in str(captured.value)


def test_corporate_global_bad_identity_fails_closed() -> None:
    """全市场业绩来源存在无法归属证券的行时，不得宣称全证券窗口完整。"""
    bad = pd.DataFrame([{"股票代码": None, "公告日期": date(2026, 7, 28)}])
    empty = pd.DataFrame()
    with (
        patch(
            "service_data_sync.infrastructure.providers.akshare.p0_market_data.ak.stock_yjyg_em",
            return_value=bad,
        ),
        patch(
            "service_data_sync.infrastructure.providers.akshare.p0_market_data.ak.stock_yjkb_em",
            return_value=empty,
        ),
        pytest.raises(ProviderError) as captured,
    ):
        _normalize_corporate_fixture(
            _window(),
            guidance=bad,
            express=empty,
        )

    assert captured.value.code is ProviderErrorCode.SCHEMA
    assert captured.value.retryable is False


def test_instrument_request_ignores_proven_non_target_event_rows() -> None:
    """证券限定请求可忽略代码明确不同的行，即使该非目标行其他字段不可解析。"""
    non_target = pd.DataFrame([{"股票代码": "000002", "公告日期": "不是日期"}])
    empty = pd.DataFrame()
    with (
        patch(
            "service_data_sync.infrastructure.providers.akshare.p0_market_data.ak.stock_yjyg_em",
            return_value=non_target,
        ),
        patch(
            "service_data_sync.infrastructure.providers.akshare.p0_market_data.ak.stock_yjkb_em",
            return_value=empty,
        ),
    ):
        payload = _normalize_corporate_fixture(
            _window(instrument="SZSE.000001"),
            guidance=non_target,
            express=empty,
        )

    assert payload["documents"] == []


def test_dragon_tiger_target_bad_head_fails_closed() -> None:
    """目标龙虎榜头不能对账时必须报错，不能吞行后发布空事件集。"""
    head = [_dragon_head_row(deal=149)]
    buy_seats = [_dragon_seat_row()]
    sell_seats = [
        _dragon_seat_row(
            seat_code="20001",
            seat_name="样本卖方营业部",
            buy=0,
            sell=50,
            net=-50,
        )
    ]
    with (
        pytest.raises(ProviderError) as captured,
    ):
        _normalize_dragon_tiger(
            _window(instrument="SZSE.000001"),
            head_rows=head,
            buy_rows=buy_seats,
            sell_rows=sell_seats,
        )

    assert captured.value.code is ProviderErrorCode.SCHEMA
    assert captured.value.retryable is False
    assert "cannot be reconciled" in str(captured.value)


def test_dragon_tiger_target_bad_seat_fails_closed() -> None:
    """目标龙虎榜席位候选缺必填金额时必须报错，不能只保留其余席位。"""
    head = [_dragon_head_row()]
    bad_buy_seats = [_dragon_seat_row(buy=None, sell=0, net=100)]
    sell_seats = [
        _dragon_seat_row(
            seat_code="20001",
            seat_name="样本卖方营业部",
            buy=0,
            sell=50,
            net=-50,
        )
    ]
    with (
        pytest.raises(ProviderError) as captured,
    ):
        _normalize_dragon_tiger(
            _window(instrument="SZSE.000001"),
            head_rows=head,
            buy_rows=bad_buy_seats,
            sell_rows=sell_seats,
        )

    assert captured.value.code is ProviderErrorCode.SCHEMA
    assert captured.value.retryable is False
    assert "DAILYDETAILSBUY" in str(captured.value)


def test_dragon_tiger_global_bad_identity_fails_closed() -> None:
    """全市场龙虎榜头无法确认证券时必须阻断，不能形成虚假的完整空覆盖。"""
    head = [_dragon_head_row(code=None)]
    with pytest.raises(ProviderError) as captured:
        _normalize_dragon_tiger(
            _window(),
            head_rows=head,
            buy_rows=[],
            sell_rows=[],
        )

    assert captured.value.code is ProviderErrorCode.SCHEMA
    assert captured.value.retryable is False


def test_earnings_native_pages_use_notice_date_and_independent_timeout() -> None:
    """业绩两报表逐请求 timeout，累计时长可超单页预算且公告日期固定取 `NOTICE_DATE`。"""
    period = date(2026, 6, 30)
    script = _EarningsPageScript(
        {
            ("RPT_PUBLIC_OP_NEWPREDICT", period.isoformat(), 1): [
                _dragon_page_response(records=[_earnings_guidance_row()])
            ],
            ("RPT_FCI_PERFORMANCEE", period.isoformat(), 1): [
                _dragon_page_response(records=[_earnings_express_row()])
            ],
        },
        delay_seconds=0.55,
    )
    adapter = AkshareP0MarketDataAdapter(request_timeout_seconds=1)
    request = SourceRequest(
        capability="corporate.disclosure.earnings.p0",
        parameters=(
            ("start", "2026-07-28"),
            ("end", "2026-07-29"),
            ("instrument", "SSE.600519"),
        ),
    )
    with (
        patch(
            "service_data_sync.infrastructure.providers.akshare.p0_market_data._report_periods",
            return_value=(period,),
        ),
        patch(
            "service_data_sync.infrastructure.providers.akshare.p0_market_data._earnings_http_get",
            new=script,
        ),
    ):
        batch = asyncio.run(adapter.fetch(request))

    payload = json.loads(batch.payload)
    raw = json.loads(batch.raw_payload or b"")
    assert {document["announcedOn"] for document in payload["documents"]} == {
        "2026-07-28",
        "2026-07-29",
    }
    assert batch.upstream_source == "eastmoney.earnings"
    assert batch.adapter_version == "akshare-1.18.81-p0-market-data-v9"
    assert raw["announcementField"] == "NOTICE_DATE"
    assert len(script.calls) == 2
    assert all(request_timeout.read == 1 for _, request_timeout in script.calls)
    assert all("600519" not in params["filter"] for params, _ in script.calls)


def test_earnings_native_page_retries_protocol_error_then_succeeds() -> None:
    """业绩原生协议瞬断只重试当前页，成功后才继续下一报表。"""
    period = date(2026, 6, 30)
    protocol_error = httpx.RemoteProtocolError(
        "peer closed connection",
        request=httpx.Request("GET", "https://example.test"),
    )
    script = _EarningsPageScript(
        {
            ("RPT_PUBLIC_OP_NEWPREDICT", period.isoformat(), 1): [
                protocol_error,
                _dragon_page_response(records=[_earnings_guidance_row()]),
            ],
            ("RPT_FCI_PERFORMANCEE", period.isoformat(), 1): [
                _dragon_page_response(records=[_earnings_express_row()])
            ],
        }
    )
    with (
        patch(
            "service_data_sync.infrastructure.providers.akshare.p0_market_data._report_periods",
            return_value=(period,),
        ),
        patch(
            "service_data_sync.infrastructure.providers.akshare.p0_market_data."
            "_EARNINGS_RETRY_BASE_SECONDS",
            0,
        ),
        patch(
            "service_data_sync.infrastructure.providers.akshare.p0_market_data._earnings_http_get",
            new=script,
        ),
    ):
        payload, _ = asyncio.run(
            _fetch_corporate_events(
                _window(instrument="SSE.600519"),
                request_timeout_seconds=1,
            )
        )

    assert len(payload["documents"]) == 2
    assert [params["reportName"] for params, _ in script.calls] == [
        "RPT_PUBLIC_OP_NEWPREDICT",
        "RPT_PUBLIC_OP_NEWPREDICT",
        "RPT_FCI_PERFORMANCEE",
    ]


def test_earnings_native_failure_stops_later_requests_and_keeps_hashes() -> None:
    """业绩首页 `5xx` 三次即停止，失败证据只保留响应哈希且不继续快报。"""
    period = date(2026, 6, 30)
    unavailable = _dragon_page_response(records=[], status_code=503)
    script = _EarningsPageScript(
        {
            ("RPT_PUBLIC_OP_NEWPREDICT", period.isoformat(), 1): [
                unavailable,
                unavailable,
                unavailable,
            ]
        }
    )
    with (
        patch(
            "service_data_sync.infrastructure.providers.akshare.p0_market_data._report_periods",
            return_value=(period,),
        ),
        patch(
            "service_data_sync.infrastructure.providers.akshare.p0_market_data."
            "_EARNINGS_RETRY_BASE_SECONDS",
            0,
        ),
        patch(
            "service_data_sync.infrastructure.providers.akshare.p0_market_data._earnings_http_get",
            new=script,
        ),
        pytest.raises(ProviderError) as captured,
    ):
        asyncio.run(
            _fetch_corporate_events(
                _window(),
                request_timeout_seconds=1,
            )
        )

    evidence = json.loads(captured.value.failure_evidence or b"")
    assert captured.value.retryable is True
    assert [params["reportName"] for params, _ in script.calls] == [
        "RPT_PUBLIC_OP_NEWPREDICT",
        "RPT_PUBLIC_OP_NEWPREDICT",
        "RPT_PUBLIC_OP_NEWPREDICT",
    ]
    assert len(evidence["fetchedPages"]) == 3
    assert all(
        "response" not in page and "records" not in page for page in evidence["fetchedPages"]
    )


def test_earnings_native_plain_4xx_is_not_retried() -> None:
    """业绩普通 `4xx` 是确定性请求或 schema 错误，只调用一次且不继续快报。"""
    period = date(2026, 6, 30)
    script = _EarningsPageScript(
        {
            ("RPT_PUBLIC_OP_NEWPREDICT", period.isoformat(), 1): [
                _dragon_page_response(records=[], status_code=400)
            ]
        }
    )
    with (
        patch(
            "service_data_sync.infrastructure.providers.akshare.p0_market_data._report_periods",
            return_value=(period,),
        ),
        patch(
            "service_data_sync.infrastructure.providers.akshare.p0_market_data._earnings_http_get",
            new=script,
        ),
        pytest.raises(ProviderError) as captured,
    ):
        asyncio.run(_fetch_corporate_events(_window(), request_timeout_seconds=1))

    assert len(script.calls) == 1
    assert captured.value.code is ProviderErrorCode.SCHEMA
    assert captured.value.retryable is False


def test_earnings_native_cancellation_stops_before_next_request() -> None:
    """取消在途业绩首页后立即传播，且不再调度快报或后续报告期。"""

    async def scenario() -> list[dict[str, str]]:
        """运行阻塞的业绩首页并在确认调用后取消任务。"""
        period = date(2026, 6, 30)
        started = asyncio.Event()
        calls: list[dict[str, str]] = []

        async def blocking_request(
            _client: httpx.AsyncClient,
            *,
            params: dict[str, str],
            request_timeout: httpx.Timeout,
        ) -> httpx.Response:
            """记录真实 timeout 后阻塞，依赖原生任务取消退出。"""
            assert request_timeout.read == 1
            calls.append(dict(params))
            started.set()
            await asyncio.Event().wait()
            raise AssertionError("cancelled earnings request resumed unexpectedly")

        with (
            patch(
                "service_data_sync.infrastructure.providers.akshare.p0_market_data._report_periods",
                return_value=(period,),
            ),
            patch(
                "service_data_sync.infrastructure.providers.akshare.p0_market_data."
                "_earnings_http_get",
                new=blocking_request,
            ),
        ):
            task = asyncio.create_task(
                _fetch_corporate_events(
                    _window(),
                    request_timeout_seconds=1,
                )
            )
            await started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        return calls

    calls = asyncio.run(scenario())
    assert [(call["reportName"], call["pageNumber"]) for call in calls] == [
        ("RPT_PUBLIC_OP_NEWPREDICT", "1")
    ]


def test_dragon_tiger_bulk_pages_full_window_before_instrument_filter() -> None:
    """两页三报表先完整对账再筛证券，且累计耗时可超过单页 timeout 而不误杀整窗。"""
    heads = [
        _dragon_head_row(code="000001", trade_id="1001"),
        _dragon_head_row(code="600519", trade_id="1002"),
    ]
    buys = [
        _dragon_seat_row(code="000001", trade_id="1001", seat_code="buy-1"),
        _dragon_seat_row(code="600519", trade_id="1002", seat_code="buy-2"),
    ]
    sells = [
        _dragon_seat_row(
            code="000001",
            trade_id="1001",
            seat_code="sell-1",
            buy=0,
            sell=50,
            net=-50,
        ),
        _dragon_seat_row(
            code="600519",
            trade_id="1002",
            seat_code="sell-2",
            buy=0,
            sell=50,
            net=-50,
        ),
    ]
    script = _DragonTigerPageScript(
        {
            ("RPT_DAILYBILLBOARD_DETAILSNEW", 1): [
                _dragon_page_response(records=[heads[0]], pages=2, count=2)
            ],
            ("RPT_DAILYBILLBOARD_DETAILSNEW", 2): [
                _dragon_page_response(records=[heads[1]], pages=2, count=2)
            ],
            ("RPT_BILLBOARD_DAILYDETAILSBUY", 1): [
                _dragon_page_response(records=[buys[0]], pages=2, count=2)
            ],
            ("RPT_BILLBOARD_DAILYDETAILSBUY", 2): [
                _dragon_page_response(records=[buys[1]], pages=2, count=2)
            ],
            ("RPT_BILLBOARD_DAILYDETAILSSELL", 1): [
                _dragon_page_response(records=[sells[0]], pages=2, count=2)
            ],
            ("RPT_BILLBOARD_DAILYDETAILSSELL", 2): [
                _dragon_page_response(records=[sells[1]], pages=2, count=2)
            ],
        },
        delay_seconds=0.55,
    )
    request = SourceRequest(
        capability="market.dragon_tiger.disclosure.1d",
        parameters=(
            ("start", "2026-07-28"),
            ("end", "2026-07-29"),
            ("instrument", "SSE.600519"),
        ),
    )
    adapter = AkshareP0MarketDataAdapter(request_timeout_seconds=1)
    with (
        patch(
            "service_data_sync.infrastructure.providers.akshare.p0_market_data."
            "_DRAGON_TIGER_PAGE_SIZE",
            1,
        ),
        patch(
            "service_data_sync.infrastructure.providers.akshare.p0_market_data."
            "_dragon_tiger_http_get",
            new=script,
        ),
    ):
        batch = asyncio.run(adapter.fetch(request))

    payload = json.loads(batch.payload)
    raw = json.loads(batch.raw_payload or b"")
    assert [event["securityCode"] for event in payload["events"]] == ["600519"]
    assert payload["events"][0]["netAmount"] == "50"
    assert payload["events"][0]["seats"][0]["seatCode"] == "buy-2"
    assert batch.upstream_source == "eastmoney.dragon-tiger"
    assert len(script.calls) == 6
    assert all(
        call[0]["filter"] == "(TRADE_DATE<='2026-07-29')(TRADE_DATE>='2026-07-28')"
        for call in script.calls
    )
    assert all("SECURITY_CODE" not in call[0]["filter"] for call in script.calls)
    assert all(call[1].read == 1 for call in script.calls)
    assert {
        (params["reportName"], params["sortColumns"], params["sortTypes"])
        for params, _ in script.calls
        if params["pageNumber"] == "1"
    } == {
        (
            "RPT_DAILYBILLBOARD_DETAILSNEW",
            "TRADE_DATE,SECURITY_CODE,EXPLANATION,TRADE_ID",
            "1,1,1,1",
        ),
        (
            "RPT_BILLBOARD_DAILYDETAILSBUY",
            (
                "TRADE_DATE,SECURITY_CODE,EXPLANATION,TRADE_ID,"
                "BUY,SELL,OPERATEDEPT_CODE,OPERATEDEPT_NAME"
            ),
            "1,1,1,1,-1,-1,1,1",
        ),
        (
            "RPT_BILLBOARD_DAILYDETAILSSELL",
            (
                "TRADE_DATE,SECURITY_CODE,EXPLANATION,TRADE_ID,"
                "SELL,BUY,OPERATEDEPT_CODE,OPERATEDEPT_NAME"
            ),
            "1,1,1,1,-1,-1,1,1",
        ),
    }
    assert raw["methodology"] == {
        "headAmounts": "EASTMONEY_REPORTED_AGGREGATES",
        "seatLists": "EASTMONEY_TOP_BUY_AND_TOP_SELL_DISCLOSURES",
        "reconciliation": "HEAD_INTERNAL_AND_SEAT_INTERNAL_ONLY",
    }
    assert [len(report["rawPages"]) for report in raw["reports"]] == [2, 2, 2]


def test_dragon_tiger_bulk_retries_native_protocol_error_then_succeeds() -> None:
    """原生协议瞬断可在同一页有限重试，解析或普通 `4xx` 不共享这条重试路径。"""
    head = _dragon_head_row()
    buy = _dragon_seat_row()
    sell = _dragon_seat_row(
        seat_code="sell-1",
        buy=0,
        sell=50,
        net=-50,
    )
    protocol_error = httpx.RemoteProtocolError(
        "peer closed connection",
        request=httpx.Request("GET", "https://example.test"),
    )
    script = _DragonTigerPageScript(
        {
            ("RPT_DAILYBILLBOARD_DETAILSNEW", 1): [
                protocol_error,
                _dragon_page_response(records=[head]),
            ],
            ("RPT_BILLBOARD_DAILYDETAILSBUY", 1): [_dragon_page_response(records=[buy])],
            ("RPT_BILLBOARD_DAILYDETAILSSELL", 1): [_dragon_page_response(records=[sell])],
        }
    )
    with (
        patch(
            "service_data_sync.infrastructure.providers.akshare.p0_market_data."
            "_DRAGON_TIGER_RETRY_BASE_SECONDS",
            0,
        ),
        patch(
            "service_data_sync.infrastructure.providers.akshare.p0_market_data."
            "_dragon_tiger_http_get",
            new=script,
        ),
    ):
        payload, _ = asyncio.run(
            _fetch_dragon_tiger_bulk(
                _window(),
                request_timeout_seconds=1,
            )
        )

    assert len(payload["events"]) == 1
    assert (
        len(
            [
                params
                for params, _ in script.calls
                if params["reportName"] == "RPT_DAILYBILLBOARD_DETAILSNEW"
            ]
        )
        == 2
    )


def test_dragon_tiger_bulk_retries_5xx_and_attaches_page_hash_evidence() -> None:
    """单页 `5xx` 只重试三次；失败证据仅含已抓页哈希清单而不复制原始响应。"""
    unavailable = _dragon_page_response(records=[], status_code=503)
    script = _DragonTigerPageScript(
        {
            ("RPT_DAILYBILLBOARD_DETAILSNEW", 1): [unavailable, unavailable, unavailable],
            ("RPT_BILLBOARD_DAILYDETAILSBUY", 1): [
                unavailable,
                unavailable,
                unavailable,
            ],
            ("RPT_BILLBOARD_DAILYDETAILSSELL", 1): [
                unavailable,
                unavailable,
                unavailable,
            ],
        }
    )
    with (
        patch(
            "service_data_sync.infrastructure.providers.akshare.p0_market_data."
            "_DRAGON_TIGER_RETRY_BASE_SECONDS",
            0,
        ),
        patch(
            "service_data_sync.infrastructure.providers.akshare.p0_market_data."
            "_dragon_tiger_http_get",
            new=script,
        ),
        pytest.raises(ProviderError) as captured,
    ):
        asyncio.run(
            _fetch_dragon_tiger_bulk(
                _window(),
                request_timeout_seconds=1,
            )
        )

    evidence = json.loads(captured.value.failure_evidence or b"")
    assert captured.value.retryable is True
    assert evidence["failureKind"] == "BULK_REPORT_FAILED"
    assert evidence["fetchedPages"]
    assert all(
        set(page)
        == {
            "reportName",
            "pageNumber",
            "attempt",
            "statusCode",
            "byteSize",
            "sha256",
        }
        for page in evidence["fetchedPages"]
    )
    assert all(
        "records" not in page and "response" not in page for page in evidence["fetchedPages"]
    )
    head_calls = [
        params
        for params, _ in script.calls
        if params["reportName"] == "RPT_DAILYBILLBOARD_DETAILSNEW"
    ]
    assert len(head_calls) == 3


def test_dragon_tiger_bulk_cancellation_stops_before_next_page_or_report() -> None:
    """任务取消原生异步页请求后立即传播，且不会再调度下页或买卖报表。"""

    async def scenario() -> list[dict[str, str]]:
        """启动一个阻塞首页，在确认已发请求后取消并返回调用清单。"""
        started = asyncio.Event()
        calls: list[dict[str, str]] = []

        async def blocking_request(
            _client: httpx.AsyncClient,
            *,
            params: dict[str, str],
            request_timeout: httpx.Timeout,
        ) -> httpx.Response:
            """模拟可被原生取消的在途 HTTP，不吞掉 `CancelledError`。"""
            assert request_timeout.read == 1
            calls.append(dict(params))
            started.set()
            await asyncio.Event().wait()
            raise AssertionError("cancelled request resumed unexpectedly")

        with patch(
            "service_data_sync.infrastructure.providers.akshare.p0_market_data."
            "_dragon_tiger_http_get",
            new=blocking_request,
        ):
            task = asyncio.create_task(
                _fetch_dragon_tiger_bulk(
                    _window(),
                    request_timeout_seconds=1,
                )
            )
            await started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        return calls

    calls = asyncio.run(scenario())
    assert [(call["reportName"], call["pageNumber"]) for call in calls] == [
        ("RPT_DAILYBILLBOARD_DETAILSNEW", "1")
    ]


def test_dragon_tiger_bulk_rejects_pagination_upper_bound() -> None:
    """来源声明页数或总量超过冻结上界时，首页即失败且不继续请求。"""
    script = _DragonTigerPageScript(
        {
            ("RPT_DAILYBILLBOARD_DETAILSNEW", 1): [
                _dragon_page_response(
                    records=[_dragon_head_row()],
                    pages=129,
                    count=64_001,
                )
            ]
        }
    )
    with (
        patch(
            "service_data_sync.infrastructure.providers.akshare.p0_market_data."
            "_dragon_tiger_http_get",
            new=script,
        ),
        pytest.raises(ProviderError) as captured,
    ):
        asyncio.run(_fetch_dragon_tiger_bulk(_window(), request_timeout_seconds=1))

    assert captured.value.code is ProviderErrorCode.SCHEMA
    assert len(script.calls) == 1


def test_dragon_tiger_bulk_rejects_cross_page_duplicate_and_changed_total() -> None:
    """跨页重复或冻结总量变化任一出现，都不得形成可发布的窗口批次。"""
    head = _dragon_head_row()
    buy = _dragon_seat_row()
    sell = _dragon_seat_row(
        seat_code="sell-1",
        buy=0,
        sell=50,
        net=-50,
    )
    for second_head in (
        _dragon_page_response(records=[head], pages=2, count=2),
        _dragon_page_response(
            records=[_dragon_head_row(code="600519", trade_id="1002")],
            pages=3,
            count=3,
        ),
    ):
        script = _DragonTigerPageScript(
            {
                ("RPT_DAILYBILLBOARD_DETAILSNEW", 1): [
                    _dragon_page_response(records=[head], pages=2, count=2)
                ],
                ("RPT_DAILYBILLBOARD_DETAILSNEW", 2): [second_head],
                ("RPT_BILLBOARD_DAILYDETAILSBUY", 1): [_dragon_page_response(records=[buy])],
                ("RPT_BILLBOARD_DAILYDETAILSSELL", 1): [_dragon_page_response(records=[sell])],
            }
        )
        with (
            patch(
                "service_data_sync.infrastructure.providers.akshare.p0_market_data."
                "_DRAGON_TIGER_PAGE_SIZE",
                1,
            ),
            patch(
                "service_data_sync.infrastructure.providers.akshare.p0_market_data."
                "_dragon_tiger_http_get",
                new=script,
            ),
            pytest.raises(ProviderError) as captured,
        ):
            asyncio.run(_fetch_dragon_tiger_bulk(_window(), request_timeout_seconds=1))

        assert captured.value.code is ProviderErrorCode.SCHEMA


def test_dragon_tiger_bulk_rejects_orphan_and_proves_null_side_is_zero() -> None:
    """孤儿席位阻断整窗；来源空侧只有被 `NET` 唯一证明为零时才允许标准化。"""
    head = [_dragon_head_row(buy=100, sell=50, net=50, deal=150)]
    proven_buy = [_dragon_seat_row(sell=None, net=100)]
    proven_sell = [
        _dragon_seat_row(
            seat_code="sell-1",
            buy=None,
            sell=50,
            net=-50,
        )
    ]
    payload = _normalize_dragon_tiger(
        _window(),
        head_rows=head,
        buy_rows=proven_buy,
        sell_rows=proven_sell,
    )
    assert payload["events"][0]["seats"][0]["sellAmount"] == "0"
    assert payload["events"][0]["seats"][1]["buyAmount"] == "0"

    orphan = [_dragon_seat_row(code="600519", trade_id="orphan")]
    with pytest.raises(ProviderError) as captured:
        _normalize_dragon_tiger(
            _window(),
            head_rows=head,
            buy_rows=proven_buy + orphan,
            sell_rows=proven_sell,
        )
    assert captured.value.code is ProviderErrorCode.SCHEMA
    assert "orphan" in str(captured.value)


def test_dragon_tiger_special_event_does_not_mix_head_and_top_five_amount_methods() -> None:
    """冻结真实 605180 特殊事件口径：头表全市场聚合额不能与前五席位金额强求相等。"""
    head = [
        _dragon_head_row(
            code="605180",
            trade_date="2024-04-02 00:00:00",
            reason="连续10个交易日内4次出现同正向异常波动的证券",
            trade_id="100016651",
            buy=1_049_645_500,
            sell=1_049_645_500,
            net=0,
            deal=2_099_291_000,
        )
    ]
    buy = [
        _dragon_seat_row(
            code="605180",
            trade_date="2024-04-02 00:00:00",
            reason="连续10个交易日内4次出现同正向异常波动的证券",
            trade_id="100016651",
            buy=12_000_000,
            sell=0,
            net=12_000_000,
        )
    ]
    sell = [
        _dragon_seat_row(
            code="605180",
            trade_date="2024-04-02 00:00:00",
            reason="连续10个交易日内4次出现同正向异常波动的证券",
            trade_id="100016651",
            seat_code="sell-605180",
            buy=0,
            sell=9_000_000,
            net=-9_000_000,
        )
    ]

    payload = _normalize_dragon_tiger(
        {"start": "2024-04-02", "end": "2024-04-02"},
        head_rows=head,
        buy_rows=buy,
        sell_rows=sell,
    )

    assert payload["events"][0]["buyAmount"] == "1049645500"
    assert payload["events"][0]["dealAmount"] == "2099291000"
    assert payload["events"][0]["seats"][0]["buyAmount"] == "12000000"


def test_block_trade_target_bad_reconciliation_fails_closed() -> None:
    """目标大宗交易量价金额无法对账时必须报错，不能吞行后发布空成交集。"""
    bad = pd.DataFrame(
        [
            {
                "交易日期": date(2026, 7, 28),
                "证券代码": "000001",
                "成交价": 10,
                "成交量": 2,
                "成交额": 199999,
                "买方营业部": "买方",
                "卖方营业部": "卖方",
            }
        ]
    )
    with (
        patch(
            "service_data_sync.infrastructure.providers.akshare.p0_market_data.ak.stock_dzjy_mrmx",
            return_value=bad,
        ),
        pytest.raises(ProviderError) as captured,
    ):
        _block_trades(_window(instrument="SZSE.000001"))

    assert captured.value.code is ProviderErrorCode.SCHEMA
    assert captured.value.retryable is False


def test_block_trade_keeps_reported_amount_with_one_cent_rounding() -> None:
    """大宗成交额在一分容差内仍保留来源值，不用量价乘积改写供应商舍入。"""
    reported = pd.DataFrame(
        [
            {
                "交易日期": date(2026, 7, 28),
                "证券代码": "000001",
                "成交价": "10.01",
                "成交量": "100",
                "成交额": "1001.01",
                "买方营业部": "买方",
                "卖方营业部": "卖方",
            }
        ]
    )
    with patch(
        "service_data_sync.infrastructure.providers.akshare.p0_market_data.ak.stock_dzjy_mrmx",
        return_value=reported,
    ):
        payload, _ = _block_trades(_window(instrument="SZSE.000001"))

    assert payload["trades"][0]["quantityShares"] == "100"
    assert payload["trades"][0]["notionalCny"] == "1001.01"


def test_block_trade_global_bad_identity_fails_closed() -> None:
    """全市场大宗行无法确认日期时必须阻断，不能形成虚假的完整空覆盖。"""
    bad = pd.DataFrame(
        [
            {
                "交易日期": "不是日期",
                "证券代码": "000001",
                "成交价": 10,
                "成交量": 2,
                "成交额": 200000,
                "买方营业部": "买方",
                "卖方营业部": "卖方",
            }
        ]
    )
    with (
        patch(
            "service_data_sync.infrastructure.providers.akshare.p0_market_data.ak.stock_dzjy_mrmx",
            return_value=bad,
        ),
        pytest.raises(ProviderError) as captured,
    ):
        _block_trades(_window())

    assert captured.value.code is ProviderErrorCode.SCHEMA
    assert captured.value.retryable is False


def test_derivative_adapter_does_not_invent_settlement_prices() -> None:
    """验证东财真实合约日线缺少结算字段时以空值表达，而不是用收盘价替代。"""
    frame = pd.DataFrame(
        [
            {
                "时间": date(2026, 7, 28),
                "开盘": 10,
                "最高": 12,
                "最低": 9,
                "收盘": 11,
                "成交量": 100,
                "成交额": 10000,
                "持仓量": 200,
            }
        ]
    )
    with patch(
        "service_data_sync.infrastructure.providers.akshare.p0_market_data.ak.futures_hist_em",
        return_value=frame,
    ):
        payload, _ = _derivative_bars(_window(contract="CFFEX.IF2608"))

    assert payload["bars"][0]["settlement"] is None
    assert payload["bars"][0]["openInterest"] == "200"


def test_adapter_fetch_rejects_unknown_capability() -> None:
    """验证 adapter 不会把未治理请求转发给 AKShare。"""
    adapter = AkshareP0MarketDataAdapter(request_timeout_seconds=5)
    request = SourceRequest(capability="unknown.capability")

    assert request.capability not in adapter.capabilities()


def test_event_fetch_retries_transport_failure_within_same_request_budget() -> None:
    """事件来源遇到一次 TLS 瞬断后重试同一冻结请求，成功响应只发布一次。"""
    adapter = AkshareP0MarketDataAdapter(request_timeout_seconds=5)
    request = SourceRequest(
        capability="market.block_trade.execution.1d",
        parameters=(
            ("start", "2026-07-28"),
            ("end", "2026-07-28"),
            ("instrument", "SSE.600519"),
        ),
    )
    success = (
        {
            "schema": "quant-v2.block-trade-execution.v1",
            "trades": [],
        },
        {"groups": []},
        "eastmoney.block-trade",
    )
    with (
        patch(
            "service_data_sync.infrastructure.providers.akshare.p0_market_data."
            "_EVENT_FETCH_RETRY_BASE_SECONDS",
            0,
        ),
        patch(
            "service_data_sync.infrastructure.providers.akshare.p0_market_data._fetch_payload",
            side_effect=(requests.exceptions.SSLError("TLS EOF"), success),
        ) as fetch_payload,
    ):
        result = asyncio.run(adapter.fetch(request))

    assert fetch_payload.call_count == 2
    assert result.upstream_source == "eastmoney.block-trade"


def test_event_fetch_stops_after_three_retryable_failures() -> None:
    """事件来源的网络重试上限固定为三次，避免持续分页故障无限占用 worker。"""
    adapter = AkshareP0MarketDataAdapter(request_timeout_seconds=5)
    request = SourceRequest(
        capability="market.block_trade.execution.1d",
        parameters=(
            ("start", "2026-07-28"),
            ("end", "2026-07-28"),
            ("instrument", "SSE.600519"),
        ),
    )
    with (
        patch(
            "service_data_sync.infrastructure.providers.akshare.p0_market_data."
            "_EVENT_FETCH_RETRY_BASE_SECONDS",
            0,
        ),
        patch(
            "service_data_sync.infrastructure.providers.akshare.p0_market_data._fetch_payload",
            side_effect=requests.exceptions.ConnectionError("connection reset"),
        ) as fetch_payload,
        pytest.raises(ProviderError) as raised,
    ):
        asyncio.run(adapter.fetch(request))

    assert fetch_payload.call_count == 3
    assert raised.value.code == ProviderErrorCode.UNAVAILABLE
    assert raised.value.retryable is True


def test_event_fetch_does_not_retry_plain_http_4xx() -> None:
    """普通 `4xx` 是确定性请求失败，不得以重试掩盖或放大供应商调用。"""
    adapter = AkshareP0MarketDataAdapter(request_timeout_seconds=5)
    request = SourceRequest(
        capability="market.dragon_tiger.disclosure.1d",
        parameters=(
            ("start", "2026-07-28"),
            ("end", "2026-07-28"),
            ("instrument", "SSE.600519"),
        ),
    )
    with (
        patch(
            "service_data_sync.infrastructure.providers.akshare.p0_market_data."
            "_dragon_tiger_http_get",
            new_callable=AsyncMock,
            return_value=_dragon_page_response(records=[], status_code=400),
        ) as page_request,
        pytest.raises(ProviderError) as raised,
    ):
        asyncio.run(adapter.fetch(request))

    assert page_request.call_count == 1
    assert {call.kwargs["params"]["reportName"] for call in page_request.call_args_list} == {
        "RPT_DAILYBILLBOARD_DETAILSNEW"
    }
    assert raised.value.code == ProviderErrorCode.SCHEMA
    assert raised.value.retryable is False
