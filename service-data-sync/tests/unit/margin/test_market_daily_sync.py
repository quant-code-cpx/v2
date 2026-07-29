"""融资融券 P0 市场汇总标准载荷的边界测试。"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from service_data_sync.application.margin.market_daily_sync import (
    decode_margin_market_daily_batch,
)
from service_data_sync.application.ports.data_source import ProviderError
from service_data_sync.domain.margin import MarginVenue


def test_decoder_preserves_zero_and_null_without_derived_repayment() -> None:
    """官方直报零值与未披露空值必须分开，且不接受派生偿还作为 P0 市场汇总。"""
    payload = json.dumps(
        {
            "schema": "quant-v2.margin-market-daily.v1",
            "venue": "SSE",
            "valueKind": "REPORTED",
            "records": [
                {
                    "tradeDate": "2026-07-28",
                    "financingBalance": "0",
                    "financingBuyAmount": None,
                    "totalBalance": "123456.78",
                    "currency": "CNY",
                    "quantityUnit": None,
                }
            ],
        }
    ).encode()

    records = decode_margin_market_daily_batch(payload, venue=MarginVenue("SSE"))

    assert records[0].financing_balance == Decimal("0")
    assert records[0].financing_buy_amount is None
    assert records[0].total_balance == Decimal("123456.78")


def test_decoder_rejects_derived_market_values() -> None:
    """证券明细或公式派生的值必须进入独立版本，不能伪装成交易所市场直报。"""
    payload = json.dumps(
        {
            "schema": "quant-v2.margin-market-daily.v1",
            "venue": "SSE",
            "valueKind": "DERIVED",
            "records": [],
        }
    ).encode()

    with pytest.raises(ProviderError, match="reported values"):
        decode_margin_market_daily_batch(payload, venue=MarginVenue("SSE"))


def test_decoder_accepts_a_legal_empty_market_window() -> None:
    """来源以正确 schema 返回空数组时，窗口无两融汇总是正常结果而非 schema 失败。"""
    payload = json.dumps(
        {
            "schema": "quant-v2.margin-market-daily.v1",
            "venue": "SSE",
            "valueKind": "REPORTED",
            "records": [],
        }
    ).encode()

    assert decode_margin_market_daily_batch(payload, venue=MarginVenue("SSE")) == ()
