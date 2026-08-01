"""融资融券 P0 市场汇总标准载荷的边界测试。"""

from __future__ import annotations

import asyncio
import json
from datetime import date
from decimal import Decimal
from typing import cast

import pytest

from service_data_sync.application.margin.market_daily_sync import (
    MarginMarketDailySyncService,
    decode_margin_market_daily_batch,
)
from service_data_sync.application.ports.data_source import (
    DataSourcePort,
    ProviderBatch,
    ProviderError,
    ProviderErrorCode,
    SourceRequest,
)
from service_data_sync.application.ports.margin_market import MarginMarketDailyRepository
from service_data_sync.application.ports.market_data import RawPayloadStore
from service_data_sync.domain.margin import MarginVenue


class _NoCallMarginSource:
    """验证北交所非资格请求在应用层被拒绝，不会触发任何来源调用。"""

    provider_id = "no-call-margin-source"

    def __init__(self) -> None:
        """初始化可观测调用计数，供失败关闭边界断言。"""
        self.capabilities_calls = 0
        self.fetch_calls = 0

    def capabilities(self) -> frozenset[str]:
        """记录 capability 查询；此测试预期应用层在此之前返回不支持。"""
        self.capabilities_calls += 1
        return frozenset({"market.margin.market.1d.reported"})

    async def fetch(self, request: SourceRequest) -> ProviderBatch:
        """禁止测试路径发起来源调用，若触发即说明场所门禁失效。"""
        self.fetch_calls += 1
        raise AssertionError(f"unexpected source request: {request.capability}")


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


def test_market_daily_service_rejects_bse_before_calling_the_source() -> None:
    """北交所当前只有资格清单，市场日汇总服务必须明确失败并不尝试伪造来源。"""
    source = _NoCallMarginSource()
    service = MarginMarketDailySyncService(
        source=cast(DataSourcePort, source),
        repository=cast(MarginMarketDailyRepository, object()),
        raw_payload_store=cast(RawPayloadStore, object()),
    )

    with pytest.raises(ProviderError) as captured:
        asyncio.run(
            service.sync(
                venue=MarginVenue("BSE"),
                start=date(2026, 7, 31),
                end=date(2026, 7, 31),
            )
        )

    assert captured.value.code is ProviderErrorCode.CURRENTLY_UNSUPPORTED
    assert source.capabilities_calls == 0
    assert source.fetch_calls == 0
