"""沪深港通 P0 活跃证券标准载荷的边界测试。"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from service_data_sync.application.ports.data_source import ProviderError
from service_data_sync.application.stock_connect.active_security_sync import (
    decode_stock_connect_active_security_batch,
)
from service_data_sync.domain.stock_connect import StockConnectChannel


def test_decoder_keeps_active_rank_separate_from_market_statistics() -> None:
    """活跃榜只保留来源证券、名次和直报金额，不从通道汇总反推每只证券数值。"""
    payload = json.dumps(
        {
            "schema": "quant-v2.stock-connect-active-security.v1",
            "channel": "SH",
            "direction": "NORTHBOUND",
            "valueKind": "REPORTED",
            "records": [
                {
                    "instrumentCode": "HK.00700",
                    "tradeDate": "2026-07-28",
                    "rankNo": 1,
                    "buyAmount": "1000",
                    "sellAmount": "900",
                    "turnoverAmount": "1900",
                    "currency": "HKD",
                }
            ],
        }
    ).encode()

    records = decode_stock_connect_active_security_batch(
        payload, channel=StockConnectChannel("SH", "NORTHBOUND")
    )

    assert records[0].source_instrument_code == "HK.00700"
    assert records[0].turnover_amount == Decimal("1900")


def test_decoder_rejects_estimated_active_security_values() -> None:
    """估算资金流属于 P2，活跃榜 P0 只能接受交易所直接披露的 reported 值。"""
    payload = json.dumps(
        {
            "schema": "quant-v2.stock-connect-active-security.v1",
            "channel": "SH",
            "direction": "NORTHBOUND",
            "valueKind": "ESTIMATED",
            "records": [],
        }
    ).encode()

    with pytest.raises(ProviderError, match="reported active securities"):
        decode_stock_connect_active_security_batch(
            payload, channel=StockConnectChannel("SH", "NORTHBOUND")
        )


def test_decoder_accepts_a_legal_empty_active_security_window() -> None:
    """来源没有活跃榜记录时应返回空集，不能触发失败归档。"""
    payload = json.dumps(
        {
            "schema": "quant-v2.stock-connect-active-security.v1",
            "channel": "SH",
            "direction": "NORTHBOUND",
            "valueKind": "REPORTED",
            "records": [],
        }
    ).encode()

    assert (
        decode_stock_connect_active_security_batch(
            payload, channel=StockConnectChannel("SH", "NORTHBOUND")
        )
        == ()
    )
