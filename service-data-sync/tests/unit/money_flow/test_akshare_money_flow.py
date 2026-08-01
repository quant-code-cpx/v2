"""固定 AKShare 1.18.78 资金流 adapter 的签名、字段与单位测试。"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import date

import pandas as pd
import pytest
import requests

from service_data_sync.application.ports.data_source import (
    ProviderError,
    ProviderErrorCode,
    SourceRequest,
)
from service_data_sync.infrastructure.providers.akshare import money_flow as money_flow_provider
from service_data_sync.infrastructure.providers.akshare.money_flow import (
    AkshareEastmoneyMoneyFlowAdapter,
    AkshareThsMoneyFlowAdapter,
)


def _daily_frame() -> pd.DataFrame:
    """构造东财五分桶日序列冻结表头。"""
    row: dict[str, object] = {"日期": date(2026, 7, 24)}
    for label in ("主力", "超大单", "大单", "中单", "小单"):
        row[f"{label}净流入-净额"] = 1
        row[f"{label}净流入-净占比"] = 1
    return pd.DataFrame([row])


def _order_size_ranking_row(*, indicator: str, name: str) -> dict[str, object]:
    """构造东财 supplier ranking 的五分桶冻结表头。"""
    row: dict[str, object] = {"名称": name}
    for label in ("主力", "超大单", "大单", "中单", "小单"):
        row[f"{indicator}{label}净流入-净额"] = 1
        row[f"{indicator}{label}净流入-净占比"] = 1
    return row


def test_eastmoney_equity_daily_uses_exact_signature_and_converts_ratio() -> None:
    """验证 stock/market 精确参数、百分数换算和 raw NaN 可归档。"""
    calls: list[dict[str, str]] = []

    def fetcher(**kwargs: str) -> pd.DataFrame:
        """记录固定 SDK 函数关键字，并返回冻结表头 fixture。"""
        calls.append(kwargs)
        return pd.DataFrame(
            [
                {
                    "日期": date(2026, 7, 24),
                    "主力净流入-净额": 100.0,
                    "主力净流入-净占比": 2.5,
                    "超大单净流入-净额": float("nan"),
                    "超大单净流入-净占比": float("nan"),
                    "大单净流入-净额": 30.0,
                    "大单净流入-净占比": 0.5,
                    "中单净流入-净额": -20.0,
                    "中单净流入-净占比": -0.25,
                    "小单净流入-净额": -10.0,
                    "小单净流入-净占比": -0.1,
                }
            ]
        )

    adapter = AkshareEastmoneyMoneyFlowAdapter(
        request_timeout_seconds=5,
        equity_daily_fetcher=fetcher,
    )
    batch = asyncio.run(
        adapter.fetch(
            SourceRequest(
                capability="money_flow.order_size.daily.equity.raw",
                parameters=(("exchange", "SSE"), ("symbol", "600000")),
            )
        )
    )

    assert calls == [{"stock": "600000", "market": "sh"}]
    payload = json.loads(batch.payload)
    assert payload["observations"][0]["netRatio"] == "0.025"
    assert payload["methodologyKey"] == "eastmoney-order-size"
    assert batch.raw_payload is not None
    assert json.loads(batch.raw_payload)["records"][0]["超大单净流入-净额"] is None


def test_eastmoney_ranking_is_fail_closed_without_verified_upstream_total() -> None:
    """验证 SDK 合并 DataFrame 不会伪装成已验证完整分页。"""

    def fetcher(**kwargs: str) -> pd.DataFrame:
        """返回一条今日个股排行 fixture。"""
        assert kwargs == {"indicator": "今日"}
        row: dict[str, object] = {"序号": 1, "代码": "600000", "名称": "浦发银行"}
        for label in ("主力", "超大单", "大单", "中单", "小单"):
            row[f"今日{label}净流入-净额"] = 1
            row[f"今日{label}净流入-净占比"] = 1
        return pd.DataFrame([row])

    adapter = AkshareEastmoneyMoneyFlowAdapter(
        request_timeout_seconds=5,
        equity_ranking_fetcher=fetcher,
    )
    batch = asyncio.run(
        adapter.fetch(
            SourceRequest(
                capability="money_flow.order_size.ranking.equity.raw",
                parameters=(("indicator", "今日"), ("targetDate", "2026-07-24")),
            )
        )
    )

    payload = json.loads(batch.payload)
    assert payload["isComplete"] is False
    assert payload["completenessBasis"] == "sdk_returned"
    assert payload["items"][0]["supplierPosition"] == 1


def test_eastmoney_ranking_excludes_rows_without_primary_measure() -> None:
    """来源缺主力桶不得替代排序，空侧桶也不能伪造可消费指标。"""
    empty_row = _order_size_ranking_row(indicator="今日", name="无主力资金流证券")
    empty_row.update({"序号": 1, "代码": "688836"})
    for label in ("主力", "超大单", "大单", "中单", "小单"):
        empty_row[f"今日{label}净流入-净额"] = float("nan")
        empty_row[f"今日{label}净流入-净占比"] = float("nan")
    empty_row["今日超大单净流入-净额"] = 1
    empty_row["今日超大单净流入-净占比"] = 1
    measured_row = _order_size_ranking_row(indicator="今日", name="浦发银行")
    measured_row.update({"序号": 2, "代码": "600000"})
    for label in ("超大单", "大单", "中单", "小单"):
        measured_row[f"今日{label}净流入-净额"] = float("nan")
        measured_row[f"今日{label}净流入-净占比"] = float("nan")

    def fetcher(**kwargs: str) -> pd.DataFrame:
        """返回一个缺主力排行依据行和一个仅主力可消费行。"""
        assert kwargs == {"indicator": "今日"}
        return pd.DataFrame([empty_row, measured_row])

    adapter = AkshareEastmoneyMoneyFlowAdapter(
        request_timeout_seconds=5,
        equity_ranking_fetcher=fetcher,
    )
    batch = asyncio.run(
        adapter.fetch(
            SourceRequest(
                capability="money_flow.order_size.ranking.equity.raw",
                parameters=(("indicator", "今日"), ("targetDate", "2026-07-24")),
            )
        )
    )

    items = json.loads(batch.payload)["items"]
    assert [item["supplierPosition"] for item in items] == [2]
    assert [metric["bucket"] for metric in items[0]["metrics"]] == ["main"]


@pytest.mark.parametrize(
    ("configured_timeout_seconds", "expected_equity_total_timeout_seconds"),
    ((30, 180), (240, 240)),
)
def test_eastmoney_equity_ranking_reserves_a_bounded_full_scan_deadline(
    configured_timeout_seconds: int,
    expected_equity_total_timeout_seconds: int,
) -> None:
    """东财个股排行跨页读取时，整批 deadline 不能误用每页网络预算。"""
    adapter = AkshareEastmoneyMoneyFlowAdapter(
        request_timeout_seconds=configured_timeout_seconds,
    )

    assert (
        adapter._total_timeout_seconds("money_flow.order_size.ranking.equity.raw")
        == expected_equity_total_timeout_seconds
    )
    assert (
        adapter._total_timeout_seconds("money_flow.order_size.daily.equity.raw")
        == configured_timeout_seconds
    )


def test_eastmoney_equity_ranking_first_success_does_not_rescan() -> None:
    """个股排行首轮成功时不得额外扫描上游全部页面。"""
    calls: list[dict[str, str]] = []
    row = _order_size_ranking_row(indicator="今日", name="浦发银行")
    row.update({"序号": 1, "代码": "600000"})

    def fetcher(**kwargs: str) -> pd.DataFrame:
        """记录扫描次数并在首轮返回完整 fixture。"""
        calls.append(kwargs)
        return pd.DataFrame([row])

    adapter = AkshareEastmoneyMoneyFlowAdapter(
        request_timeout_seconds=5,
        equity_ranking_fetcher=fetcher,
    )
    asyncio.run(
        adapter.fetch(
            SourceRequest(
                capability="money_flow.order_size.ranking.equity.raw",
                parameters=(("indicator", "今日"), ("targetDate", "2026-07-24")),
            )
        )
    )

    assert calls == [{"indicator": "今日"}]


def test_eastmoney_equity_ranking_retries_one_full_scan_after_transport_failure() -> None:
    """首轮传输中断后只重扫一次完整个股排行，并使用第二轮真实结果。"""
    calls: list[dict[str, str]] = []
    row = _order_size_ranking_row(indicator="今日", name="浦发银行")
    row.update({"序号": 1, "代码": "600000"})

    def fetcher(**kwargs: str) -> pd.DataFrame:
        """第一次模拟 SSL 断连，第二次返回固定 SDK 结果。"""
        calls.append(kwargs)
        if len(calls) == 1:
            raise requests.exceptions.SSLError("temporary upstream disconnect")
        return pd.DataFrame([row])

    adapter = AkshareEastmoneyMoneyFlowAdapter(
        request_timeout_seconds=5,
        equity_ranking_fetcher=fetcher,
    )
    batch = asyncio.run(
        adapter.fetch(
            SourceRequest(
                capability="money_flow.order_size.ranking.equity.raw",
                parameters=(("indicator", "今日"), ("targetDate", "2026-07-24")),
            )
        )
    )

    assert calls == [{"indicator": "今日"}, {"indicator": "今日"}]
    assert json.loads(batch.payload)["items"][0]["scope"]["sourceSymbol"] == "600000"


def test_eastmoney_equity_ranking_returns_retryable_error_after_second_transport_failure() -> None:
    """两次完整扫描都发生传输失败时必须返回真实可重试失败。"""
    calls = 0

    def fetcher(**_: str) -> pd.DataFrame:
        """每次扫描均模拟上游 SSL 断连。"""
        nonlocal calls
        calls += 1
        raise requests.exceptions.SSLError("persistent upstream disconnect")

    adapter = AkshareEastmoneyMoneyFlowAdapter(
        request_timeout_seconds=5,
        equity_ranking_fetcher=fetcher,
    )
    with pytest.raises(ProviderError) as captured:
        asyncio.run(
            adapter.fetch(
                SourceRequest(
                    capability="money_flow.order_size.ranking.equity.raw",
                    parameters=(("indicator", "今日"), ("targetDate", "2026-07-24")),
                )
            )
        )

    assert calls == 2
    assert captured.value.code is ProviderErrorCode.UNAVAILABLE
    assert captured.value.retryable is True


def test_eastmoney_equity_ranking_timeout_remains_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """单一整批 deadline 到期时不泄露部分页，并映射为可重试失败。"""
    calls = 0
    row = _order_size_ranking_row(indicator="今日", name="浦发银行")
    row.update({"序号": 1, "代码": "600000"})

    def fetcher(**_: str) -> pd.DataFrame:
        """模拟超过测试专用整批预算的阻塞 SDK 扫描。"""
        nonlocal calls
        calls += 1
        time.sleep(0.05)
        return pd.DataFrame([row])

    monkeypatch.setattr(
        money_flow_provider,
        "_EASTMONEY_EQUITY_RANKING_MIN_TOTAL_TIMEOUT_SECONDS",
        0,
    )
    adapter = AkshareEastmoneyMoneyFlowAdapter(
        request_timeout_seconds=0.001,
        equity_ranking_fetcher=fetcher,
    )
    with pytest.raises(ProviderError) as captured:
        asyncio.run(
            adapter.fetch(
                SourceRequest(
                    capability="money_flow.order_size.ranking.equity.raw",
                    parameters=(("indicator", "今日"), ("targetDate", "2026-07-24")),
                )
            )
        )

    assert calls == 1
    assert captured.value.code is ProviderErrorCode.UNAVAILABLE
    assert captured.value.retryable is True


def test_ths_instant_ranking_converts_wan_and_yi_to_cny() -> None:
    """验证同花顺即时交易方向金额按版本化万/亿规则换算。"""

    def fetcher(**kwargs: str) -> pd.DataFrame:
        """返回带中文倍率单位的同花顺个股排行 fixture。"""
        assert kwargs == {"symbol": "即时"}
        return pd.DataFrame(
            [
                {
                    "序号": 1,
                    "股票代码": "000001",
                    "股票简称": "平安银行",
                    "流入资金": "1.5亿",
                    "流出资金": "20万",
                    "净额": "1.498亿",
                }
            ]
        )

    adapter = AkshareThsMoneyFlowAdapter(
        request_timeout_seconds=5,
        equity_fetcher=fetcher,
    )
    batch = asyncio.run(
        adapter.fetch(
            SourceRequest(
                capability="money_flow.trade_direction.ranking.equity.raw",
                parameters=(("indicator", "即时"), ("targetDate", "2026-07-24")),
            )
        )
    )

    metric = json.loads(batch.payload)["items"][0]["metrics"][0]
    assert metric["grossInflow"] == "150000000.0"
    assert metric["grossOutflow"] == "200000"
    assert metric["netAmount"] == "149800000.000"


@pytest.mark.parametrize(
    ("configured_timeout_seconds", "expected_equity_total_timeout_seconds"),
    ((30, 180), (240, 240)),
)
def test_ths_equity_ranking_reserves_a_bounded_full_scan_deadline(
    configured_timeout_seconds: int,
    expected_equity_total_timeout_seconds: int,
) -> None:
    """个股 SDK 必须有足够页扫描时间；配置更大时仍不得被固定值截断。"""
    adapter = AkshareThsMoneyFlowAdapter(
        request_timeout_seconds=configured_timeout_seconds,
    )

    assert (
        adapter._total_timeout_seconds("money_flow.trade_direction.ranking.equity.raw")
        == expected_equity_total_timeout_seconds
    )
    assert (
        adapter._total_timeout_seconds("money_flow.trade_direction.ranking.industry.raw")
        == configured_timeout_seconds
    )


def test_eastmoney_sector_and_market_daily_use_distinct_upstream_scopes() -> None:
    """板块和市场日序列必须调用各自上游接口，不能由证券聚合。"""
    sector_calls: list[dict[str, str]] = []
    market_calls = 0

    def sector_fetcher(**kwargs: str) -> pd.DataFrame:
        """记录东财板块历史接口真实关键字。"""
        sector_calls.append(kwargs)
        return _daily_frame()

    def market_fetcher() -> pd.DataFrame:
        """记录东财全市场历史接口无参数签名。"""
        nonlocal market_calls
        market_calls += 1
        return _daily_frame()

    adapter = AkshareEastmoneyMoneyFlowAdapter(
        request_timeout_seconds=5,
        sector_daily_fetcher=sector_fetcher,
        market_daily_fetcher=market_fetcher,
    )
    sector_batch = asyncio.run(
        adapter.fetch(
            SourceRequest(
                capability="money_flow.order_size.daily.sector.raw",
                parameters=(
                    ("scheme", "eastmoney.industry"),
                    ("sectorCode", "BK0475"),
                    ("sectorName", "银行"),
                ),
            )
        )
    )
    market_batch = asyncio.run(
        adapter.fetch(
            SourceRequest(
                capability="money_flow.order_size.daily.market.raw",
                parameters=(("marketCode", "cn-a"),),
            )
        )
    )

    assert sector_calls == [{"symbol": "银行"}]
    assert market_calls == 1
    assert json.loads(sector_batch.payload)["scope"]["sectorCode"] == "BK0475"
    assert json.loads(market_batch.payload)["scope"]["marketCode"] == "cn-a"


def test_eastmoney_sector_ranking_preserves_fallback_position_and_scheme() -> None:
    """来源缺序号时使用稳定页序，并保留地域板块独立分类体系。"""
    row = _order_size_ranking_row(indicator="5日", name="上海板块")

    def fetcher(**kwargs: str) -> pd.DataFrame:
        """验证板块排行接口的 indicator 与 sector_type 参数。"""
        assert kwargs == {"indicator": "5日", "sector_type": "地域资金流"}
        return pd.DataFrame([row])

    adapter = AkshareEastmoneyMoneyFlowAdapter(
        request_timeout_seconds=5,
        sector_ranking_fetcher=fetcher,
    )
    batch = asyncio.run(
        adapter.fetch(
            SourceRequest(
                capability="money_flow.order_size.ranking.sector.raw",
                parameters=(
                    ("indicator", "5日"),
                    ("sectorType", "地域资金流"),
                    ("targetDate", "2026-07-24"),
                ),
            )
        )
    )
    payload = json.loads(batch.payload)

    assert payload["windowType"] == "supplier_rolling"
    assert payload["items"][0]["supplierPosition"] == 1
    assert payload["items"][0]["scope"]["scheme"] == "eastmoney.region"


def test_ths_rolling_sector_ranking_keeps_supplier_window_semantics() -> None:
    """同花顺滚动板块排行只保留净额，不能伪装为逐日序列。"""

    def fetcher(**kwargs: str) -> pd.DataFrame:
        """返回带缺失序号的三日行业排行。"""
        assert kwargs == {"symbol": "3日排行"}
        return pd.DataFrame([{"序号": None, "行业": "银行", "净额": "2万"}])

    adapter = AkshareThsMoneyFlowAdapter(
        request_timeout_seconds=5,
        industry_fetcher=fetcher,
    )
    batch = asyncio.run(
        adapter.fetch(
            SourceRequest(
                capability="money_flow.trade_direction.ranking.industry.raw",
                parameters=(("indicator", "3日排行"), ("targetDate", "2026-07-24")),
            )
        )
    )
    payload = json.loads(batch.payload)
    metric = payload["items"][0]["metrics"][0]

    assert payload["windowType"] == "supplier_rolling"
    assert payload["windowSize"] == 3
    assert payload["items"][0]["supplierPosition"] == 1
    assert metric == {
        "bucket": "all",
        "grossInflow": None,
        "grossOutflow": None,
        "netAmount": "20000",
        "netRatio": None,
    }


def test_adapters_map_request_and_schema_failures_to_provider_contract() -> None:
    """非法参数不可重试，来源失败可重试，空表和字段漂移不可重试。"""
    eastmoney = AkshareEastmoneyMoneyFlowAdapter(request_timeout_seconds=5)
    invalid_requests = (
        SourceRequest(capability="unknown"),
        SourceRequest(
            capability="money_flow.order_size.daily.equity.raw",
            parameters=(("exchange", "HKEX"), ("symbol", "600000")),
        ),
        SourceRequest(
            capability="money_flow.order_size.daily.sector.raw",
            parameters=(
                ("scheme", "other"),
                ("sectorCode", "BK"),
                ("sectorName", "银行"),
            ),
        ),
        SourceRequest(
            capability="money_flow.order_size.daily.market.raw",
            parameters=(("marketCode", "cn-b"),),
        ),
        SourceRequest(
            capability="money_flow.order_size.ranking.equity.raw",
            parameters=(("indicator", "20日"),),
        ),
        SourceRequest(
            capability="money_flow.order_size.ranking.sector.raw",
            parameters=(("indicator", "3日"), ("sectorType", "行业资金流")),
        ),
    )
    for request in invalid_requests:
        with pytest.raises(ProviderError) as captured:
            asyncio.run(eastmoney.fetch(request))
        assert captured.value.code is ProviderErrorCode.INVALID_REQUEST
        assert captured.value.retryable is False

    def failed_fetcher(**_: str) -> pd.DataFrame:
        """模拟来源 HTTP 失败。"""
        raise ConnectionError("offline")

    failed = AkshareEastmoneyMoneyFlowAdapter(
        request_timeout_seconds=5,
        equity_daily_fetcher=failed_fetcher,
    )
    with pytest.raises(ProviderError) as unavailable:
        asyncio.run(
            failed.fetch(
                SourceRequest(
                    capability="money_flow.order_size.daily.equity.raw",
                    parameters=(("exchange", "SSE"), ("symbol", "600000")),
                )
            )
        )
    assert unavailable.value.code is ProviderErrorCode.UNAVAILABLE
    assert unavailable.value.retryable is True

    def empty_fetcher(**_: str) -> pd.DataFrame:
        """模拟供应商合法调用却返回空表。"""
        return pd.DataFrame()

    empty = AkshareEastmoneyMoneyFlowAdapter(
        request_timeout_seconds=5,
        equity_daily_fetcher=empty_fetcher,
    )
    with pytest.raises(ProviderError) as schema:
        asyncio.run(
            empty.fetch(
                SourceRequest(
                    capability="money_flow.order_size.daily.equity.raw",
                    parameters=(("exchange", "SSE"), ("symbol", "600000")),
                )
            )
        )
    assert schema.value.code is ProviderErrorCode.SCHEMA
    assert schema.value.retryable is False


def test_ths_rejects_unknown_capability_indicator_and_amount_suffix() -> None:
    """同花顺 adapter 必须拒绝错误能力、窗口和无法复验的金额倍率。"""
    adapter = AkshareThsMoneyFlowAdapter(request_timeout_seconds=5)
    invalid_requests = (
        SourceRequest(capability="unknown"),
        SourceRequest(
            capability="money_flow.trade_direction.ranking.equity.raw",
            parameters=(("indicator", "今日"),),
        ),
    )
    for request in invalid_requests:
        with pytest.raises(ProviderError) as captured:
            asyncio.run(adapter.fetch(request))
        assert captured.value.code is ProviderErrorCode.INVALID_REQUEST

    def suffix_fetcher(**_: str) -> pd.DataFrame:
        """返回供应商新增但尚未定义换算规则的金额后缀。"""
        return pd.DataFrame(
            [
                {
                    "序号": 1,
                    "股票代码": "000001",
                    "股票简称": "平安银行",
                    "流入资金": "1千",
                    "流出资金": "1万",
                    "净额": "0",
                }
            ]
        )

    suffix_adapter = AkshareThsMoneyFlowAdapter(
        request_timeout_seconds=5,
        equity_fetcher=suffix_fetcher,
    )
    with pytest.raises(ProviderError) as schema:
        asyncio.run(
            suffix_adapter.fetch(
                SourceRequest(
                    capability="money_flow.trade_direction.ranking.equity.raw",
                    parameters=(("indicator", "即时"), ("targetDate", "2026-07-24")),
                )
            )
        )
    assert schema.value.code is ProviderErrorCode.SCHEMA
