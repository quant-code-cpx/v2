"""Tushare 市场完整包 adapter 单位、数值与失败证据测试。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

import pytest

from service_data_sync.application.ports.data_source import (
    ProviderError,
    ProviderErrorCode,
    SourceRequest,
)
from service_data_sync.infrastructure.providers.tushare.market_overview import (
    TushareMarketOverviewAdapter,
    _finite_decimal,
    _optional_finite_decimal,
)

_TOKEN = "test-tushare-token-that-is-never-persisted-000001"


def test_index_and_sw_units_preserve_volume_semantics_and_convert_amount_to_cny() -> None:
    """指数保留手、申万保留供应商原始量，而两类成交额都准确换算为 CNY。"""
    adapter = _adapter(_successful_transport)

    index_batch = asyncio.run(
        adapter.fetch(
            SourceRequest(
                capability="index.bar.1d",
                parameters=(("start", "2026-07-28"), ("end", "2026-07-28")),
            )
        )
    )
    sw_batch = asyncio.run(
        adapter.fetch(
            SourceRequest(
                capability="sw.market-data",
                parameters=(("tradeDate", "2026-07-28"),),
            )
        )
    )
    index_payload = json.loads(index_batch.payload)
    sw_payload = json.loads(sw_batch.payload)

    assert index_payload["volumeUnit"] == "lot"
    assert index_payload["amountRawUnit"] == "thousand_CNY"
    assert index_payload["amountUnit"] == "CNY"
    assert {row["volume"] for row in index_payload["records"]} == {"123"}
    assert {row["amountCny"] for row in index_payload["records"]} == {"12000"}
    assert sw_payload["volumeUnit"] == "provider_native"
    assert sw_payload["amountRawUnit"] == "ten_thousand_CNY"
    assert sw_payload["amountUnit"] == "CNY"
    assert sw_payload["records"][0]["volume"] == "456"
    assert sw_payload["records"][0]["amountCny"] == "120000"


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_provider_decimals_fail_closed(value: str) -> None:
    """任何非有限来源数值都不能进入 final canonical JSON 或被吞成空值。"""
    with pytest.raises(ValueError, match="must be finite"):
        _finite_decimal(value)
    with pytest.raises(ValueError, match="must be finite"):
        _optional_finite_decimal(value)


def test_provider_rejection_attaches_token_free_reproducible_failure_evidence() -> None:
    """供应商非零 code 留下请求指纹与 raw 摘要，但不保存 token、消息或原始响应。"""

    def rejected_transport(_body: bytes, _timeout: int) -> bytes:
        """返回含敏感供应商消息的拒绝响应。"""
        return json.dumps(
            {
                "code": 2002,
                "msg": "account secret and entitlement detail",
                "data": None,
            }
        ).encode()

    adapter = _adapter(rejected_transport)

    with pytest.raises(ProviderError) as captured:
        asyncio.run(
            adapter.fetch(
                SourceRequest(
                    capability="index.bar.1d",
                    parameters=(("start", "2026-07-28"), ("end", "2026-07-28")),
                )
            )
        )

    error = captured.value
    assert error.code is ProviderErrorCode.AUTHENTICATION
    assert error.failure_evidence is not None
    evidence_text = error.failure_evidence.decode()
    evidence = json.loads(evidence_text)
    request = evidence["requests"][0]
    assert request["endpoint"] == "index_daily"
    assert request["outcome"] == "provider_rejected"
    assert request["providerCode"] == 2002
    assert len(request["requestFingerprint"]) == 64
    assert len(request["rawSha256"]) == 64
    assert _TOKEN not in evidence_text
    assert "account secret" not in evidence_text
    assert "msg" not in evidence_text


@pytest.mark.parametrize("truncated_status", ["L", "D", "P"])
def test_equity_catalog_rejects_each_stock_basic_status_at_provider_row_limit(
    truncated_status: str,
) -> None:
    """上市、退市和暂停上市任一分区恰达上限都必须阻断全量目录发布。"""
    queried_statuses: list[str] = []

    def truncated_transport(body: bytes, _timeout: int) -> bytes:
        """让指定 `stock_basic` 状态分区恰好返回供应商六千行上限。"""
        request = json.loads(body)
        assert request["api_name"] == "stock_basic"
        status = str(request["params"]["list_status"])
        queried_statuses.append(status)
        fields = str(request["fields"]).split(",")
        values = {
            "ts_code": "600000.SH",
            "symbol": "600000",
            "name": "测试证券",
            "area": "上海",
            "industry": "测试",
            "market": "主板",
            "exchange": "SSE",
            "list_status": status,
            "list_date": "19991110",
            "delist_date": None,
        }
        count = 6000 if status == truncated_status else 1
        return _protocol_response(fields, [[values[field] for field in fields]] * count)

    adapter = _adapter(truncated_transport, response_row_limit=10_000)

    with pytest.raises(ProviderError, match="stock_basic.*row limit") as captured:
        asyncio.run(adapter.fetch(SourceRequest(capability="equity.catalog")))

    assert captured.value.code is ProviderErrorCode.SCHEMA
    assert queried_statuses[-1] == truncated_status


def test_preflight_probes_listed_stock_basic_and_rejects_silent_truncation() -> None:
    """提交前必须在线验证目录权限与 schema，并在 L 分区六千行时拒绝排队。"""
    endpoints: list[str] = []

    def preflight_transport(body: bytes, _timeout: int) -> bytes:
        """依次返回合法日历、千行行情与恰达上限的上市目录。"""
        request = json.loads(body)
        api_name = str(request["api_name"])
        endpoints.append(api_name)
        fields = str(request["fields"]).split(",")
        if api_name == "trade_cal":
            values = {
                "exchange": "SSE",
                "cal_date": "20260728",
                "is_open": 1,
                "pretrade_date": "20260727",
            }
            return _protocol_response(fields, [[values[field] for field in fields]])
        if api_name == "daily":
            values = {
                "ts_code": "600000.SH",
                "trade_date": "20260728",
                "open": "10",
                "high": "11",
                "low": "9",
                "close": "10",
                "pre_close": "10",
                "change": "0",
                "pct_chg": "0",
                "vol": "1",
                "amount": "1",
            }
            return _protocol_response(
                fields,
                [[values[field] for field in fields]] * 1000,
            )
        if api_name == "stock_basic":
            values = {
                "ts_code": "600000.SH",
                "symbol": "600000",
                "name": "测试证券",
                "area": "上海",
                "industry": "测试",
                "market": "主板",
                "exchange": "SSE",
                "list_status": "L",
                "list_date": "19991110",
                "delist_date": None,
            }
            return _protocol_response(
                fields,
                [[values[field] for field in fields]] * 6000,
            )
        raise AssertionError(f"unexpected preflight endpoint: {api_name}")

    adapter = _adapter(preflight_transport, response_row_limit=10_000)

    with pytest.raises(ProviderError, match="stock_basic.*row limit") as captured:
        asyncio.run(
            adapter.fetch(
                SourceRequest(
                    capability="market.source.preflight",
                    parameters=(("tradeDate", "2026-07-28"),),
                )
            )
        )

    assert captured.value.code is ProviderErrorCode.SCHEMA
    assert endpoints == ["trade_cal", "daily", "stock_basic"]


def _adapter(
    transport: Callable[[bytes, int], bytes],
    *,
    response_row_limit: int = 5000,
) -> TushareMarketOverviewAdapter:
    """构造禁用重试等待的确定性测试 adapter。"""
    return TushareMarketOverviewAdapter(
        token=_TOKEN,
        timeout_seconds=2,
        response_row_limit=response_row_limit,
        license_scope="personal-research",
        transport=transport,
        max_retries=0,
    )


def _protocol_response(fields: list[str], items: list[list[Any]]) -> bytes:
    """编码测试所需的最小 Tushare 成功协议响应。"""
    return json.dumps(
        {
            "code": 0,
            "msg": None,
            "data": {"fields": fields, "items": items},
        }
    ).encode()


def _successful_transport(body: bytes, _timeout: int) -> bytes:
    """按请求端点和字段返回最小合法 Tushare 协议响应。"""
    request = json.loads(body)
    api_name = request["api_name"]
    fields = str(request["fields"]).split(",")
    values: dict[str, Any]
    if api_name == "index_daily":
        values = {
            "ts_code": request["params"]["ts_code"],
            "trade_date": "20260728",
            "close": "3500",
            "open": "3480",
            "high": "3510",
            "low": "3470",
            "pre_close": "3475",
            "change": "25",
            "pct_chg": "0.72",
            "vol": "123",
            "amount": "12",
        }
    elif api_name == "sw_daily":
        values = {
            "ts_code": "801010.SI",
            "trade_date": "20260728",
            "name": "农林牧渔",
            "open": "1000",
            "high": "1020",
            "low": "990",
            "close": "1010",
            "change": "10",
            "pct_change": "1",
            "vol": "456",
            "amount": "12",
            "pe": "18",
            "pb": "2",
            "float_mv": "100",
            "total_mv": "200",
        }
    else:
        raise AssertionError(f"unexpected test endpoint: {api_name}")
    return json.dumps(
        {
            "code": 0,
            "msg": None,
            "data": {
                "fields": fields,
                "items": [[values[field] for field in fields]],
            },
        }
    ).encode()
