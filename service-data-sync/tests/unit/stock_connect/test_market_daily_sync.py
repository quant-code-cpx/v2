"""沪深港通 P0 通道统计的制度断点与净买入边界测试。"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from service_data_sync.application.ports.data_source import ProviderError
from service_data_sync.application.stock_connect.market_daily_sync import (
    decode_stock_connect_market_daily_batch,
)
from service_data_sync.domain.stock_connect import StockConnectChannel


def test_decoder_keeps_disclosure_unavailable_distinct_from_zero() -> None:
    """制度未披露的北向买卖字段必须全为空，不能因为通道存在而补成零。"""
    payload = json.dumps(
        {
            "schema": "quant-v2.stock-connect-market-daily.v1",
            "channel": "SH",
            "direction": "NORTHBOUND",
            "records": [
                {
                    "tradeDate": "2026-07-28",
                    "buyAmount": None,
                    "sellAmount": None,
                    "turnoverAmount": None,
                    "netBuyAmount": None,
                    "quotaBalance": None,
                    "currency": "CNY",
                    "availabilityStatus": "DISCLOSURE_UNAVAILABLE",
                    "tradeCount": None,
                    "etfTurnoverAmount": None,
                    "fieldAvailability": {
                        "buyAmount": "NOT_DISCLOSED_BY_REGIME",
                        "sellAmount": "NOT_DISCLOSED_BY_REGIME",
                        "turnoverAmount": "SOURCE_MISSING",
                        "netBuyAmount": "NOT_DISCLOSED_BY_REGIME",
                        "tradeCount": "SOURCE_MISSING",
                        "etfTurnoverAmount": "SOURCE_MISSING",
                    },
                },
                {
                    "tradeDate": "2026-07-29",
                    "buyAmount": "0",
                    "sellAmount": "0",
                    "turnoverAmount": "0",
                    "netBuyAmount": "-2.5",
                    "quotaBalance": "100",
                    "currency": "CNY",
                    "availabilityStatus": "COMPLETE",
                    "tradeCount": None,
                    "etfTurnoverAmount": None,
                    "fieldAvailability": {
                        "buyAmount": "REPORTED",
                        "sellAmount": "REPORTED",
                        "turnoverAmount": "REPORTED",
                        "netBuyAmount": "REPORTED",
                        "tradeCount": "SOURCE_MISSING",
                        "etfTurnoverAmount": "SOURCE_MISSING",
                    },
                },
            ],
        }
    ).encode()

    records = decode_stock_connect_market_daily_batch(
        payload, channel=StockConnectChannel("SH", "NORTHBOUND")
    )

    assert records[0].turnover_amount is None
    assert records[1].buy_amount == Decimal("0")
    assert records[1].net_buy_amount == Decimal("-2.5")


def test_decoder_rejects_non_reported_estimate() -> None:
    """估算资金流和滚动排行不能作为官方通道统计写入 P0。"""
    payload = json.dumps(
        {
            "schema": "quant-v2.stock-connect-market-daily.v1",
            "channel": "SH",
            "direction": "NORTHBOUND",
            "valueKind": "ESTIMATED",
            "records": [],
        }
    ).encode()

    with pytest.raises(ProviderError, match="reported values"):
        decode_stock_connect_market_daily_batch(
            payload, channel=StockConnectChannel("SH", "NORTHBOUND")
        )


def test_decoder_accepts_a_legal_empty_market_window() -> None:
    """交易所未披露该通道窗口时，空数组应直接进入正常空结果路径。"""
    payload = json.dumps(
        {
            "schema": "quant-v2.stock-connect-market-daily.v1",
            "channel": "SH",
            "direction": "NORTHBOUND",
            "valueKind": "REPORTED",
            "records": [],
        }
    ).encode()

    assert (
        decode_stock_connect_market_daily_batch(
            payload, channel=StockConnectChannel("SH", "NORTHBOUND")
        )
        == ()
    )
