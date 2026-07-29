"""龙虎榜与大宗交易 P0 标准载荷的边界测试。"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from service_data_sync.application.ports.data_source import ProviderError
from service_data_sync.application.trading_events.sync import (
    decode_block_trade_batch,
    decode_dragon_tiger_batch,
)


def test_dragon_tiger_decoder_keeps_buy_and_sell_seats_separate() -> None:
    """同一排名的买卖席位可以共存，净额和成交额必须由来源同一事件头对账。"""
    payload = json.dumps(
        {
            "schema": "quant-v2.dragon-tiger-disclosure.v1",
            "events": [
                {
                    "sourceEventKey": "trade-20260728-000001-reason-a",
                    "securityCode": "SSE.600000",
                    "tradeDate": "2026-07-28",
                    "reasonCode": "DAILY_TOP_GAIN",
                    "reasonText": "日涨幅偏离值达到7%的前五只证券",
                    "closePrice": "12.34",
                    "buyAmount": "120.00",
                    "sellAmount": "100.00",
                    "netAmount": "20.00",
                    "dealAmount": "220.00",
                    "marketTurnoverAmount": "1000.00",
                    "dealRatio": "0.22",
                    "netRatio": "0.02",
                    "turnoverRatio": "0.03",
                    "sourcePublishedAt": None,
                    "visibleTimePrecision": "DATE_ONLY",
                    "visibleAt": "2026-07-29T09:30:00+08:00",
                    "seats": [
                        {
                            "listSide": "BUY",
                            "rank": 1,
                            "seatCode": None,
                            "seatName": "机构专用",
                            "buyAmount": "120.00",
                            "sellAmount": "0",
                            "netAmount": "120.00",
                            "buyRatio": "1",
                            "sellRatio": "0",
                        },
                        {
                            "listSide": "SELL",
                            "rank": 1,
                            "seatCode": "001",
                            "seatName": "示例营业部",
                            "buyAmount": "0",
                            "sellAmount": "100.00",
                            "netAmount": "-100.00",
                            "buyRatio": "0",
                            "sellRatio": "1",
                        },
                    ],
                }
            ],
        }
    ).encode()

    events = decode_dragon_tiger_batch(payload)

    assert events[0].net_amount == Decimal("20.00")
    assert [(item.list_side, item.rank) for item in events[0].seats] == [("BUY", 1), ("SELL", 1)]


def test_dragon_tiger_decoder_accepts_negative_reported_net_ratio() -> None:
    """卖出大于买入时净买占比可为负，不能因字段名含 ratio 被错误拒绝。"""
    payload = json.dumps(
        {
            "schema": "quant-v2.dragon-tiger-disclosure.v1",
            "events": [
                {
                    "sourceEventKey": "trade-20260728-000001-reason-b",
                    "securityCode": "SSE.600000",
                    "tradeDate": "2026-07-28",
                    "reasonCode": "DAILY_TOP_LOSS",
                    "reasonText": "日跌幅偏离值达到7%的前五只证券",
                    "closePrice": "12.34",
                    "buyAmount": "100.00",
                    "sellAmount": "120.00",
                    "netAmount": "-20.00",
                    "dealAmount": "220.00",
                    "marketTurnoverAmount": "1000.00",
                    "dealRatio": "0.22",
                    "netRatio": "-0.02",
                    "turnoverRatio": "0.03",
                    "sourcePublishedAt": None,
                    "visibleTimePrecision": "DATE_ONLY",
                    "visibleAt": "2026-07-29T09:30:00+08:00",
                    "seats": [
                        {
                            "listSide": "BUY",
                            "rank": 1,
                            "seatCode": None,
                            "seatName": "机构专用",
                            "buyAmount": "100.00",
                            "sellAmount": "0",
                            "netAmount": "100.00",
                            "buyRatio": "1",
                            "sellRatio": "0",
                        }
                    ],
                }
            ],
        }
    ).encode()

    events = decode_dragon_tiger_batch(payload)

    assert events[0].net_ratio == Decimal("-0.02")


def test_block_trade_decoder_keeps_identical_economic_trades_by_occurrence() -> None:
    """完全相同的经济字段可代表两笔真实成交，不能在解码时按字段 hash 合并。"""
    template = {
        "securityCode": "SZSE.000001",
        "tradeDate": "2026-07-28",
        "executionPrice": "10.00",
        "quantityShares": "100",
        "notionalCny": "1000.00",
        "buyerSeatCode": None,
        "buyerSeatName": "机构专用",
        "sellerSeatCode": None,
        "sellerSeatName": "机构专用",
        "referenceClosePrice": "10.20",
        "premiumDiscountRatio": "-0.0196078431",
        "sourceDailyRank": None,
        "sourcePublishedAt": None,
        "visibleTimePrecision": "DATE_ONLY",
        "visibleAt": "2026-07-29T09:30:00+08:00",
    }
    payload = json.dumps(
        {
            "schema": "quant-v2.block-trade-execution.v1",
            "trades": [
                {**template, "sourceTradeKey": "page-1-row-1", "occurrenceNo": 1},
                {**template, "sourceTradeKey": "page-1-row-2", "occurrenceNo": 1},
            ],
        }
    ).encode()

    trades = decode_block_trade_batch(payload)

    assert len(trades) == 2
    assert trades[0].notional_cny == Decimal("1000.00")


def test_decoder_rejects_post_event_performance_column() -> None:
    """后续涨跌幅和供应商成功率属于 P2，必须让 schema 审查失败而非被忽略。"""
    payload = json.dumps(
        {
            "schema": "quant-v2.block-trade-execution.v1",
            "trades": [
                {
                    "sourceTradeKey": "one",
                    "securityCode": "SSE.600000",
                    "tradeDate": "2026-07-28",
                    "occurrenceNo": 1,
                    "executionPrice": "1",
                    "quantityShares": 1,
                    "notionalCny": "1",
                    "buyerSeatCode": None,
                    "buyerSeatName": "买方",
                    "sellerSeatCode": None,
                    "sellerSeatName": "卖方",
                    "referenceClosePrice": None,
                    "premiumDiscountRatio": None,
                    "sourceDailyRank": None,
                    "sourcePublishedAt": None,
                    "visibleTimePrecision": "DATE_ONLY",
                    "visibleAt": "2026-07-29T09:30:00+08:00",
                    "returnAfterFiveDays": "0.1",
                }
            ],
        }
    ).encode()

    with pytest.raises(ProviderError, match="value is invalid"):
        decode_block_trade_batch(payload)


def test_decoders_accept_legal_empty_trading_event_windows() -> None:
    """没有龙虎榜或大宗成交是正常空集，不应被转换为 schema 异常。"""
    dragon_payload = json.dumps(
        {"schema": "quant-v2.dragon-tiger-disclosure.v1", "events": []}
    ).encode()
    block_payload = json.dumps(
        {"schema": "quant-v2.block-trade-execution.v1", "trades": []}
    ).encode()

    assert decode_dragon_tiger_batch(dragon_payload) == ()
    assert decode_block_trade_batch(block_payload) == ()
