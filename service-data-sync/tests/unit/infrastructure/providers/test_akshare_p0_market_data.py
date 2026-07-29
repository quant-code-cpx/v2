"""AKShare P0 adapter 的标准载荷映射回归测试。"""

from __future__ import annotations

from datetime import date, datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pandas as pd

from service_data_sync.application.ports.data_source import SourceRequest
from service_data_sync.infrastructure.providers.akshare.p0_market_data import (
    AkshareP0MarketDataAdapter,
    _block_trades,
    _corporate_events,
    _derivative_bars,
    _dragon_tiger,
    _etf_bars,
    _etf_master,
    _etf_nav,
    _etf_status,
    _margin_eligibility,
    _margin_market,
    _margin_security,
    _stock_connect_active,
    _stock_connect_market,
)


def _window(**extra: str) -> dict[str, str]:
    """构造所有有界 P0 请求共用的测试日期窗口。"""
    return {"start": "2026-07-28", "end": "2026-07-29", **extra}


def test_adapter_declares_all_thirteen_p0_capabilities() -> None:
    """验证统一 provider 恰好覆盖用户要求补齐的十三个 capability。"""
    adapter = AkshareP0MarketDataAdapter(request_timeout_seconds=5)

    assert adapter.provider_id == "akshare"
    assert len(adapter.capabilities()) == 13
    assert "fund.etf.master" in adapter.capabilities()
    assert "derivative.bar.1d.reported" in adapter.capabilities()


def test_etf_status_nav_and_bars_keep_source_units_and_empty_dimensions() -> None:
    """验证 ETF 净值状态与未复权日线分别使用对应 AKShare 响应。"""
    nav_frame = pd.DataFrame(
        [
            {
                "净值日期": date(2026, 7, 28),
                "单位净值": 1.2,
                "累计净值": 1.4,
                "申购状态": "开放申购",
                "赎回状态": "开放赎回",
            }
        ]
    )
    bar_frame = pd.DataFrame(
        [
            {
                "日期": date(2026, 7, 28),
                "开盘": 1.0,
                "最高": 1.3,
                "最低": 0.9,
                "收盘": 1.2,
                "成交量": 100,
                "成交额": 12000,
            }
        ]
    )
    parameters = _window(etf="SSE.510300", priceBasis="UNADJUSTED")

    with (
        patch(
            "service_data_sync.infrastructure.providers.akshare.p0_market_data.ak.fund_etf_fund_info_em",
            return_value=nav_frame,
        ),
        patch(
            "service_data_sync.infrastructure.providers.akshare.p0_market_data.ak.fund_etf_hist_em",
            return_value=bar_frame,
        ),
    ):
        statuses, _ = _etf_status(parameters)
        navs, _ = _etf_nav(parameters)
        bars, _ = _etf_bars(parameters)

    assert {item["dimension"] for item in statuses["statuses"]} == {"SUBSCRIPTION", "REDEMPTION"}
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
            "volumeUnit": "LOT",
            "amount": "12000",
            "currency": "CNY",
            "tradeStatus": None,
        }
    ]


def test_etf_master_uses_single_response_directory() -> None:
    """验证 ETF 目录不走慢速分页现货接口，直接使用已筛选 ETF 分类响应。"""
    frame = pd.DataFrame(
        [
            {"基金代码": "510300", "基金类型": "股票型"},
            {"基金代码": "159001", "基金类型": "混合型"},
        ]
    )
    with patch(
        "service_data_sync.infrastructure.providers.akshare.p0_market_data.ak.fund_etf_category_ths",
        return_value=frame,
    ):
        payload, _ = _etf_master(
            {
                "venue": "SSE",
                "observationDate": datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat(),
            }
        )

    assert [item["symbol"] for item in payload["profiles"]] == ["510300"]
    assert payload["profiles"][0]["etfType"] == "股票型"


def test_margin_adapter_maps_sse_and_szse_missing_fields_without_derivation() -> None:
    """验证两融 adapter 保留深市未披露偿还字段为空，并只为深市映射名单。"""
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
        sse_eligibility, _ = _margin_eligibility(_window(venue="SSE"))

    assert market["records"][0]["financingBalance"] == "10"
    assert security["records"][0]["financingRepaymentReported"] is None
    assert security["records"][0]["nullReason"] == "NOT_REPORTED_BY_SOURCE"
    assert eligibility["records"][0]["evidenceBasis"] == "OBSERVED_LIST"
    assert sse_eligibility["records"] == []


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


def test_stock_connect_maps_history_and_rejects_estimated_holding_ranking() -> None:
    """验证港通金额换算为 CNY，成交活跃榜不会误用持股估算排行。"""
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
    active, raw = _stock_connect_active(parameters)

    assert market["records"][0]["buyAmount"] == "1250000000.0"
    assert market["records"][0]["turnoverAmount"] is None
    assert active["records"] == []
    assert "not official active-trading" in str(raw["reason"])


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
        payload, _ = _corporate_events(_window())

    assert payload["documents"]
    assert payload["guidanceMetrics"][0]["metricCode"] == "NET_PROFIT"
    assert any(metric["metricCode"] == "ROE" for metric in payload["expressMetrics"])


def test_dragon_tiger_and_block_trade_require_reconcilable_source_rows() -> None:
    """验证公开交易 adapter 只发布可由金额恒等验证的头、席位和大宗逐笔。"""
    head = pd.DataFrame(
        [
            {
                "上榜日": date(2026, 7, 28),
                "代码": "000001",
                "上榜原因": "日涨幅偏离值达7%",
                "收盘价": 10,
                "龙虎榜买入额": 100,
                "龙虎榜卖出额": 50,
                "龙虎榜成交额": 150,
                "市场总成交额": 1000,
                "成交额占总成交比": 15,
                "净买额占总成交比": 5,
                "换手率": 1,
            }
        ]
    )
    seats = pd.DataFrame(
        [
            {
                "交易营业部名称": "样本营业部",
                "买入金额": 100,
                "卖出金额": 0,
                "买入金额-占总成交比例": 10,
                "卖出金额-占总成交比例": 0,
            }
        ]
    )
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
            "service_data_sync.infrastructure.providers.akshare.p0_market_data.ak.stock_lhb_detail_em",
            return_value=head,
        ),
        patch(
            "service_data_sync.infrastructure.providers.akshare.p0_market_data.ak.stock_lhb_stock_detail_em",
            return_value=seats,
        ),
        patch(
            "service_data_sync.infrastructure.providers.akshare.p0_market_data.ak.stock_dzjy_mrmx",
            return_value=trades,
        ),
    ):
        dragon_tiger, _ = _dragon_tiger(_window())
        block_trades, _ = _block_trades(_window())

    assert dragon_tiger["events"][0]["seats"][0]["listSide"] == "BUY"
    assert dragon_tiger["events"][0]["netAmount"] == "50"
    assert block_trades["trades"][0]["quantityShares"] == "20000"
    assert block_trades["trades"][0]["notionalCny"] == "200000"


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
