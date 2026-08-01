"""方案 0011 四个 AKShare adapter 的字段、单位与失败隔离测试。"""

from __future__ import annotations

import asyncio
import json
from datetime import date
from http.client import RemoteDisconnected
from unittest.mock import AsyncMock

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

    def __init__(
        self,
        records: list[dict[str, object]],
        *,
        columns: tuple[str, ...] | None = None,
    ) -> None:
        """保存上游记录、列集合并暴露空值状态。"""
        self.records = records
        self.columns = columns if columns is not None else tuple(records[0]) if records else ()
        self.empty = not records

    def to_dict(self, *, orient: str) -> list[dict[str, object]]:
        """只接受 adapter 使用的 records 方向。"""
        assert orient == "records"
        return self.records


class FakeResponse:
    """提供东财显式空窗复核所需的最小 HTTP 响应。"""

    def __init__(self, payload: bytes) -> None:
        """保存确定性 JSON 原始响应。"""
        self.content = payload

    def raise_for_status(self) -> None:
        """测试响应固定为成功 HTTP 状态。"""

    def json(self) -> object:
        """解析并返回原始 JSON 对象。"""
        return json.loads(self.content)


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
                    "股票代码": "600519",
                    "开盘": 10,
                    "收盘": 11,
                    "最高": 12,
                    "最低": 9,
                    "成交量": 10,
                    "成交额": 10_500,
                    "振幅": 3,
                    "涨跌幅": 1,
                    "涨跌额": 0.1,
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
    assert batch.upstream_source == "eastmoney-stock-kline"
    assert batch.adapter_version == eastmoney_equity_period_bars._ADAPTER_VERSION
    assert batch.schema_fingerprint == eastmoney_equity_period_bars._SCHEMA_FINGERPRINT
    first_timeout = captured["timeout"]
    assert isinstance(first_timeout, float)
    assert 0 < first_timeout <= 2.0
    assert adapter.supported_exchanges == frozenset(
        {
            eastmoney_equity_period_bars.Exchange.SSE,
            eastmoney_equity_period_bars.Exchange.SZSE,
        }
    )


def test_period_adapter_rejects_bse_before_calling_unverified_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """北交所未获本来源合同证明时不触网、不产生可误判为空窗的批次。"""

    def unexpected_history_call(**_kwargs: object) -> None:
        """若北交所请求误入东财端点，则立即暴露来源矩阵绕过。"""
        raise AssertionError("unverified BSE endpoint must not be called")

    monkeypatch.setattr(
        eastmoney_equity_period_bars.ak,
        "stock_zh_a_hist",
        unexpected_history_call,
    )
    adapter = AkshareEastmoneyEquityPeriodBarsAdapter(request_timeout_seconds=2)

    with pytest.raises(ProviderError) as captured:
        asyncio.run(
            adapter.fetch(
                SourceRequest(
                    capability="equity.bar.1w.raw",
                    parameters=(
                        ("instrument", "BSE.835185"),
                        ("period", "1w"),
                        ("start", "2026-07-01"),
                        ("end", "2026-07-28"),
                    ),
                )
            )
        )

    assert captured.value.code is ProviderErrorCode.CURRENTLY_UNSUPPORTED
    assert captured.value.retryable is False


def test_period_adapter_returns_proven_empty_window_and_rejects_schema_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """冻结列齐全时允许零记录周期窗口，缺列时必须失败关闭。"""
    frames = [
        FakeFrame([], columns=eastmoney_equity_period_bars._EXPECTED_COLUMNS),
        FakeFrame([], columns=("日期",)),
    ]

    def fake_history(**_kwargs: object) -> FakeFrame:
        """按顺序返回可证明空窗口与结构漂移空响应。"""
        return frames.pop(0)

    monkeypatch.setattr(eastmoney_equity_period_bars.ak, "stock_zh_a_hist", fake_history)
    adapter = AkshareEastmoneyEquityPeriodBarsAdapter(request_timeout_seconds=2)
    request = SourceRequest(
        capability="equity.bar.1mo.raw",
        parameters=(
            ("instrument", "SSE.600519"),
            ("period", "1mo"),
            ("start", "2026-07-01"),
            ("end", "2026-07-28"),
        ),
    )

    batch = asyncio.run(adapter.fetch(request))
    assert json.loads(batch.payload)["bars"] == []
    assert batch.schema_fingerprint == eastmoney_equity_period_bars._SCHEMA_FINGERPRINT
    with pytest.raises(ProviderError) as captured:
        asyncio.run(adapter.fetch(request))
    assert captured.value.code is ProviderErrorCode.SCHEMA
    assert captured.value.retryable is False


def test_period_adapter_requires_identity_bound_raw_proof_for_sdk_no_columns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """固定 SDK 无列空响应只有经原始成功响应回显证券身份后才能成为合法空窗。"""
    frame = FakeFrame([], columns=())
    responses = [
        b'{"rc":0,"data":{"market":1,"code":"600519","klines":[]}}',
        b'{"rc":0,"data":null}',
    ]
    captured: dict[str, object] = {}

    def fake_history(**_kwargs: object) -> FakeFrame:
        """模拟固定 SDK 在无 `klines` 时返回的无列 DataFrame。"""
        return frame

    def fake_get(url: str, **kwargs: object) -> FakeResponse:
        """返回一次身份绑定空列表和一次无法证明空窗的响应。"""
        captured["url"] = url
        captured.update(kwargs)
        return FakeResponse(responses.pop(0))

    monkeypatch.setattr(eastmoney_equity_period_bars.ak, "stock_zh_a_hist", fake_history)
    monkeypatch.setattr(eastmoney_equity_period_bars.requests, "get", fake_get)
    adapter = AkshareEastmoneyEquityPeriodBarsAdapter(request_timeout_seconds=2)
    request = SourceRequest(
        capability="equity.bar.1w.raw",
        parameters=(
            ("instrument", "SSE.600519"),
            ("period", "1w"),
            ("start", "2026-07-25"),
            ("end", "2026-07-26"),
        ),
    )

    batch = asyncio.run(adapter.fetch(request))

    assert batch.raw_payload == b'{"rc":0,"data":{"market":1,"code":"600519","klines":[]}}'
    assert json.loads(batch.payload)["bars"] == []
    assert captured["url"] == eastmoney_equity_period_bars._EMPTY_PROOF_URL
    assert captured["params"] == {
        "fields1": eastmoney_equity_period_bars._EMPTY_PROOF_FIELDS_1,
        "fields2": eastmoney_equity_period_bars._EMPTY_PROOF_FIELDS_2,
        "ut": eastmoney_equity_period_bars._EMPTY_PROOF_TOKEN,
        "klt": "102",
        "fqt": "0",
        "secid": "1.600519",
        "beg": "20260725",
        "end": "20260726",
    }
    proof_timeout = captured["timeout"]
    assert isinstance(proof_timeout, float)
    assert 0 < proof_timeout <= 2.0

    with pytest.raises(ProviderError) as failed:
        asyncio.run(adapter.fetch(request))
    assert failed.value.code is ProviderErrorCode.SCHEMA
    assert failed.value.retryable is False


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


def test_factor_adapter_preserves_proven_sparse_empty_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """结构正常但没有窗口内生效点时必须输出空批次，而非把稀疏语义误判为漂移。"""

    def fake_daily(**_kwargs: object) -> FakeFrame:
        """返回窗口外的两个有效因子，模拟本次没有除权生效点。"""
        return FakeFrame(
            [
                {"date": date(2026, 7, 31), "hfq_factor": "1.5"},
                {"date": date(2026, 8, 3), "hfq_factor": "1.5"},
            ]
        )

    monkeypatch.setattr(sina_adjustment_factors.ak, "stock_zh_a_daily", fake_daily)
    batch = asyncio.run(
        AkshareSinaAdjustmentFactorsAdapter(request_timeout_seconds=2).fetch(
            SourceRequest(
                capability="equity.adjustment_factor",
                parameters=(
                    ("instrument", "SSE.600519"),
                    ("start", "2026-08-01"),
                    ("end", "2026-08-01"),
                ),
            )
        )
    )

    assert json.loads(batch.payload)["factors"] == []


def test_factor_adapter_keeps_empty_provider_response_as_schema_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """来源本身无行不等于已证明稀疏空窗，仍必须按 schema 异常隔离。"""

    def fake_daily(**_kwargs: object) -> FakeFrame:
        """模拟无法证明字段结构或窗口语义的空 SDK 响应。"""
        return FakeFrame([])

    monkeypatch.setattr(sina_adjustment_factors.ak, "stock_zh_a_daily", fake_daily)

    with pytest.raises(ProviderError) as captured:
        asyncio.run(
            AkshareSinaAdjustmentFactorsAdapter(request_timeout_seconds=2).fetch(
                SourceRequest(
                    capability="equity.adjustment_factor",
                    parameters=(
                        ("instrument", "SSE.600519"),
                        ("start", "2026-08-01"),
                        ("end", "2026-08-01"),
                    ),
                )
            )
        )

    assert captured.value.code is ProviderErrorCode.SCHEMA
    assert captured.value.retryable is False


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


def test_profile_adapter_rejects_multiple_current_records(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """巨潮多行响应没有稳定当前事实时必须 schema 隔离，不能任取首行发布。"""

    def fake_profile(**_kwargs: object) -> FakeFrame:
        """返回两条看似有效但无法判定当前性的公司资料。"""
        return FakeFrame(
            [
                {"公司名称": "甲公司"},
                {"公司名称": "乙公司"},
            ]
        )

    monkeypatch.setattr(cninfo_company_profile.ak, "stock_profile_cninfo", fake_profile)

    with pytest.raises(ProviderError) as captured:
        asyncio.run(
            AkshareCninfoCompanyProfileAdapter(request_timeout_seconds=2).fetch(
                SourceRequest(
                    capability="equity.profile",
                    parameters=(("instrument", "SSE.600519"),),
                )
            )
        )

    assert captured.value.code is ProviderErrorCode.SCHEMA
    assert captured.value.retryable is False


@pytest.mark.parametrize("candidate_date", (None, "不是日期"))
def test_action_adapter_rejects_target_candidate_without_reconcilable_date(
    monkeypatch: pytest.MonkeyPatch,
    candidate_date: object,
) -> None:
    """目标证券公司行动候选没有可解析日期时必须失败，不能发布合法空覆盖。"""

    def fake_actions(**_kwargs: object) -> FakeFrame:
        """返回一条无法证明位于请求窗口外的目标证券坏候选。"""
        return FakeFrame(
            [
                {
                    "报告期": candidate_date,
                    "业绩披露日期": None,
                    "预案公告日": None,
                    "股权登记日": None,
                    "除权除息日": None,
                    "最新公告日期": None,
                    "方案进度": "实施",
                }
            ]
        )

    monkeypatch.setattr(
        eastmoney_corporate_actions.ak,
        "stock_fhps_detail_em",
        fake_actions,
    )
    with pytest.raises(ProviderError) as captured:
        asyncio.run(
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

    assert captured.value.code is ProviderErrorCode.SCHEMA
    assert captured.value.retryable is False


@pytest.mark.parametrize(
    "records",
    (
        [],
        [
            {
                "报告期": date(2024, 12, 31),
                "业绩披露日期": date(2025, 4, 1),
                "预案公告日": None,
                "股权登记日": None,
                "除权除息日": None,
                "最新公告日期": None,
                "方案进度": "实施",
            }
        ],
    ),
)
def test_action_adapter_keeps_proven_empty_window_as_empty(
    monkeypatch: pytest.MonkeyPatch,
    records: list[dict[str, object]],
) -> None:
    """供应商真空集或日期明确在窗外时，仍允许产生可验证的空事件批次。"""

    def fake_actions(**_kwargs: object) -> FakeFrame:
        """返回参数化的真实空或可明确排除候选。"""
        return FakeFrame(records)

    monkeypatch.setattr(
        eastmoney_corporate_actions.ak,
        "stock_fhps_detail_em",
        fake_actions,
    )
    batch = asyncio.run(
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

    assert json.loads(batch.payload)["actions"] == []


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


def test_period_adapter_retries_remote_disconnect_before_returning_weekly_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """东财主动断连只重试同一周线请求，成功后仍保留原生周线口径。"""
    calls = 0

    def transient_history(**_kwargs: object) -> FakeFrame:
        """首次模拟东财断连，第二次返回冻结列顺序的有效周线。"""
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RemoteDisconnected("Remote end closed connection without response")
        return FakeFrame(
            [
                {
                    "日期": date(2026, 7, 24),
                    "股票代码": "600519",
                    "开盘": 10,
                    "收盘": 11,
                    "最高": 12,
                    "最低": 9,
                    "成交量": 10,
                    "成交额": 10_500,
                    "振幅": 3,
                    "涨跌幅": 1,
                    "涨跌额": 0.1,
                    "换手率": 2,
                }
            ]
        )

    sleep = AsyncMock()
    monkeypatch.setattr(eastmoney_equity_period_bars.ak, "stock_zh_a_hist", transient_history)
    monkeypatch.setattr(eastmoney_equity_period_bars.asyncio, "sleep", sleep)
    batch = asyncio.run(
        AkshareEastmoneyEquityPeriodBarsAdapter(request_timeout_seconds=5).fetch(
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

    assert calls == 2
    sleep.assert_awaited_once_with(0.5)
    assert json.loads(batch.payload)["period"] == "1w"


def test_period_adapter_does_not_retry_classified_provider_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """已识别的 schema 失败必须直接退出，不能被传输重试掩盖。"""
    calls = 0
    failure = ProviderError(ProviderErrorCode.SCHEMA, "provider schema changed", retryable=False)

    def classified_failure(**_kwargs: object) -> FakeFrame:
        """模拟适配器下游已判定的不可重试 schema 失败。"""
        nonlocal calls
        calls += 1
        raise failure

    monkeypatch.setattr(
        eastmoney_equity_period_bars.ak,
        "stock_zh_a_hist",
        classified_failure,
    )
    adapter = AkshareEastmoneyEquityPeriodBarsAdapter(request_timeout_seconds=5)

    with pytest.raises(ProviderError, match="provider schema changed") as captured:
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

    assert calls == 1
    assert captured.value.code is ProviderErrorCode.SCHEMA
    assert captured.value.retryable is False
