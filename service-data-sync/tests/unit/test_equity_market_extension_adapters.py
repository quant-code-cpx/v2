"""方案 0011 四个 AKShare adapter 的字段、单位与失败隔离测试。"""

from __future__ import annotations

import asyncio
import json
from datetime import date

import pytest

from service_data_sync.application.ports.data_source import (
    ProviderError,
    ProviderErrorCode,
    SourceRequest,
)
from service_data_sync.infrastructure.providers.akshare import (
    cninfo_company_profile,
    eastmoney_corporate_actions,
    eastmoney_equity_period_bars,
    sina_adjustment_factors,
)
from service_data_sync.infrastructure.providers.akshare.cninfo_company_profile import (
    AkshareCninfoCompanyProfileAdapter,
)
from service_data_sync.infrastructure.providers.akshare.eastmoney_corporate_actions import (
    AkshareEastmoneyCorporateActionsAdapter,
)
from service_data_sync.infrastructure.providers.akshare.eastmoney_equity_period_bars import (
    AkshareEastmoneyEquityPeriodBarsAdapter,
)
from service_data_sync.infrastructure.providers.akshare.sina_adjustment_factors import (
    AkshareSinaAdjustmentFactorsAdapter,
)


class FakeFrame:
    """提供 AKShare DataFrame 测试所需的最小接口。"""

    def __init__(self, records: list[dict[str, object]]) -> None:
        """保存上游记录并暴露空值状态。"""
        self.records = records
        self.empty = not records

    def to_dict(self, *, orient: str) -> list[dict[str, object]]:
        """只接受 adapter 使用的 records 方向。"""
        assert orient == "records"
        return self.records


def test_period_adapter_calls_weekly_interface_and_converts_lots_to_shares(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """周线必须直接传 `weekly`，并用成交额对账后把手转换为股。"""
    captured: dict[str, object] = {}

    def fake_history(**kwargs: object) -> FakeFrame:
        """记录 AKShare 周期参数并返回一条可通过 VWAP 对账的周线。"""
        captured.update(kwargs)
        return FakeFrame(
            [
                {
                    "日期": date(2026, 7, 24),
                    "开盘": 10,
                    "最高": 12,
                    "最低": 9,
                    "收盘": 11,
                    "成交量": 10,
                    "成交额": 10_500,
                    "换手率": 2,
                }
            ]
        )

    monkeypatch.setattr(eastmoney_equity_period_bars.ak, "stock_zh_a_hist", fake_history)
    adapter = AkshareEastmoneyEquityPeriodBarsAdapter(request_timeout_seconds=2)
    batch = asyncio.run(
        adapter.fetch(
            SourceRequest(
                capability="equity.bar.1w.raw",
                parameters=(
                    ("instrument", "SSE.600519"),
                    ("period", "1w"),
                    ("start", "2026-07-01"),
                    ("end", "2026-07-28"),
                ),
            )
        )
    )
    payload = json.loads(batch.payload)

    assert captured["period"] == "weekly"
    assert captured["adjust"] == ""
    assert payload["bars"][0]["volumeShares"] == "1000"
    assert payload["bars"][0]["turnoverRate"] == "0.02"
    assert batch.schema_fingerprint is not None


def test_factor_adapter_filters_window_and_keeps_positive_exact_factor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """因子 adapter 只保留窗口内生效点，并调用 `hfq-factor`。"""
    captured: dict[str, object] = {}

    def fake_daily(**kwargs: object) -> FakeFrame:
        """记录新浪参数并返回窗口内外两个累计因子。"""
        captured.update(kwargs)
        return FakeFrame(
            [
                {"date": date(2025, 1, 1), "hfq_factor": "1.5"},
                {"date": date(2026, 1, 1), "hfq_factor": "2.5"},
            ]
        )

    monkeypatch.setattr(sina_adjustment_factors.ak, "stock_zh_a_daily", fake_daily)
    batch = asyncio.run(
        AkshareSinaAdjustmentFactorsAdapter(request_timeout_seconds=2).fetch(
            SourceRequest(
                capability="equity.adjustment_factor",
                parameters=(
                    ("instrument", "SSE.600519"),
                    ("start", "2026-01-01"),
                    ("end", "2026-07-28"),
                ),
            )
        )
    )
    payload = json.loads(batch.payload)

    assert captured["symbol"] == "sh600519"
    assert captured["adjust"] == "hfq-factor"
    assert payload["factors"] == [{"effectiveDate": "2026-01-01", "cumulativeFactor": "2.5"}]


def test_action_and_profile_adapters_map_nullable_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """东财事件和巨潮概况必须保留真实空值并输出中立字段。"""

    def fake_actions(**_kwargs: object) -> FakeFrame:
        """返回一条窗口内分红送转方案。"""
        return FakeFrame(
            [
                {
                    "报告期": date(2025, 12, 31),
                    "业绩披露日期": date(2026, 4, 1),
                    "预案公告日": date(2026, 5, 1),
                    "股权登记日": None,
                    "除权除息日": date(2026, 6, 30),
                    "最新公告日期": date(2026, 6, 1),
                    "方案进度": "实施",
                    "现金分红-现金分红比例": 10,
                    "送转股份-送股比例": float("nan"),
                    "送转股份-转股比例": 1,
                }
            ]
        )

    def fake_profile(**_kwargs: object) -> FakeFrame:
        """返回一条包含空字段的公司概况。"""
        return FakeFrame(
            [
                {
                    "公司名称": "贵州茅台酒股份有限公司",
                    "英文名称": "",
                    "所属行业": "白酒",
                    "法人代表": None,
                    "成立日期": "19991120",
                    "官方网站": "https://example.test",
                    "电子邮箱": None,
                    "联系电话": None,
                    "注册地址": "贵州",
                    "办公地址": None,
                    "主营业务": "白酒",
                    "经营范围": None,
                    "机构简介": None,
                }
            ]
        )

    monkeypatch.setattr(
        eastmoney_corporate_actions.ak,
        "stock_fhps_detail_em",
        fake_actions,
    )
    monkeypatch.setattr(
        cninfo_company_profile.ak,
        "stock_profile_cninfo",
        fake_profile,
    )
    action_batch = asyncio.run(
        AkshareEastmoneyCorporateActionsAdapter(request_timeout_seconds=2).fetch(
            SourceRequest(
                capability="equity.corporate_action",
                parameters=(
                    ("instrument", "SSE.600519"),
                    ("start", "2026-01-01"),
                    ("end", "2026-12-31"),
                ),
            )
        )
    )
    profile_batch = asyncio.run(
        AkshareCninfoCompanyProfileAdapter(request_timeout_seconds=2).fetch(
            SourceRequest(
                capability="equity.profile",
                parameters=(("instrument", "SSE.600519"),),
            )
        )
    )

    action = json.loads(action_batch.payload)["actions"][0]
    profile = json.loads(profile_batch.payload)["profile"]
    assert action["bonusSharesPer10"] is None
    assert action["cashDividendPer10"] == "10"
    assert profile["englishName"] is None
    assert profile["establishedOn"] == "1999-11-20"


@pytest.mark.parametrize(
    ("adapter", "source_request"),
    [
        (
            AkshareEastmoneyEquityPeriodBarsAdapter(request_timeout_seconds=1),
            SourceRequest(capability="equity.bar.1d.raw"),
        ),
        (
            AkshareSinaAdjustmentFactorsAdapter(request_timeout_seconds=1),
            SourceRequest(capability="equity.profile"),
        ),
        (
            AkshareEastmoneyCorporateActionsAdapter(request_timeout_seconds=1),
            SourceRequest(capability="equity.profile"),
        ),
        (
            AkshareCninfoCompanyProfileAdapter(request_timeout_seconds=1),
            SourceRequest(capability="equity.profile"),
        ),
    ],
)
def test_adapters_reject_unsupported_or_incomplete_requests(
    adapter: object,
    source_request: SourceRequest,
) -> None:
    """不匹配能力或缺少证券参数的请求必须映射为不可重试参数错误。"""
    with pytest.raises(ProviderError) as captured:
        asyncio.run(adapter.fetch(source_request))  # type: ignore[attr-defined]

    assert captured.value.code is ProviderErrorCode.INVALID_REQUEST
    assert captured.value.retryable is False


def test_period_adapter_maps_provider_failure_to_retryable_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """上游调用异常不得泄漏，必须归类为可重试不可用。"""

    def broken_history(**_kwargs: object) -> FakeFrame:
        """模拟 AKShare 或上游网络失败。"""
        raise RuntimeError("boom")

    monkeypatch.setattr(eastmoney_equity_period_bars.ak, "stock_zh_a_hist", broken_history)
    adapter = AkshareEastmoneyEquityPeriodBarsAdapter(request_timeout_seconds=1)

    with pytest.raises(ProviderError) as captured:
        asyncio.run(
            adapter.fetch(
                SourceRequest(
                    capability="equity.bar.1mo.raw",
                    parameters=(
                        ("instrument", "SSE.600519"),
                        ("period", "1mo"),
                        ("start", "2026-01-01"),
                        ("end", "2026-07-28"),
                    ),
                )
            )
        )

    assert captured.value.code is ProviderErrorCode.UNAVAILABLE
    assert captured.value.retryable is True
