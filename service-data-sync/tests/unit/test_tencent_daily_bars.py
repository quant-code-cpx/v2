"""AKShare 适配器边界处腾讯专有字段标准化的单元测试。"""

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
from service_data_sync.infrastructure.providers.akshare import tencent_daily_bars
from service_data_sync.infrastructure.providers.akshare.tencent_daily_bars import (
    AkshareTencentDailyBarsAdapter,
)


class FakeFrame:
    """提供适配器测试所需的最小 DataFrame 接口，不直接导入 pandas。"""

    def __init__(
        self,
        records: list[dict[str, object]],
        *,
        columns: tuple[str, ...] = tencent_daily_bars._EXPECTED_COLUMNS,
    ) -> None:
        """保存测试转换所需的确定性供应商形记录与冻结列集合。"""
        self._records = records
        self.columns = columns

    @property
    def empty(self) -> bool:
        """报告数据源是否返回任何行。"""
        return not self._records

    def to_dict(self, *, orient: str) -> list[dict[str, object]]:
        """仅在适配器支持的 DataFrame 转换模式下返回记录。"""
        assert orient == "records"
        return self._records


def test_adapter_corrects_lot_volume_only_when_vwap_proves_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """将腾讯按“手”返回的个股响应规范为实际成交股数。"""
    frame = FakeFrame(
        [
            {
                "date": date(2026, 6, 30),
                "open": "10.00",
                "close": "10.50",
                "high": "11.00",
                "low": "9.00",
                "volume": "10500",
                "turnover": "0.02",
                "amount": "11025000",
            }
        ]
    )
    captured_kwargs: dict[str, object] = {}

    def fetch_frame(**kwargs: object) -> FakeFrame:
        """捕获 SDK 调用参数并返回“手”口径样本。"""
        captured_kwargs.update(kwargs)
        return frame

    monkeypatch.setattr(tencent_daily_bars.ak, "stock_zh_a_hist_tx", fetch_frame)

    batch = asyncio.run(
        AkshareTencentDailyBarsAdapter(request_timeout_seconds=5).fetch(
            SourceRequest(
                capability="equity.bar.1d.raw",
                parameters=(
                    ("instrument", "SZSE.000001"),
                    ("start", "2026-06-01"),
                    ("end", "2026-06-30"),
                ),
            )
        )
    )

    payload = json.loads(batch.payload)
    assert payload["instrument"] == "SZSE.000001"
    assert payload["bars"][0]["volumeShares"] == "1050000"
    assert payload["bars"][0]["turnoverRate"] == "0.02"
    assert batch.raw_payload is not None
    assert batch.upstream_source == "tencent-stock-kline"
    assert batch.adapter_version == tencent_daily_bars._ADAPTER_VERSION
    assert batch.schema_fingerprint == tencent_daily_bars._SCHEMA_FINGERPRINT
    assert captured_kwargs["timeout"] == 5.0


def test_adapter_rejects_a_response_with_unreconcilable_volume_unit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """“股”和“手”都无法与日内价格区间对账时隔离供应商行。"""
    frame = FakeFrame(
        [
            {
                "date": "2026-06-30",
                "open": "10",
                "close": "10",
                "high": "11",
                "low": "9",
                "volume": "100",
                "turnover": "0.01",
                "amount": "100000000",
            }
        ]
    )
    # 匿名回调固定返回不可对账样本，验证适配器将其标记为结构错误。
    monkeypatch.setattr(tencent_daily_bars.ak, "stock_zh_a_hist_tx", lambda **_: frame)

    with pytest.raises(ProviderError, match="schema changed") as error:
        asyncio.run(
            AkshareTencentDailyBarsAdapter(request_timeout_seconds=5).fetch(
                SourceRequest(
                    capability="equity.bar.1d.raw",
                    parameters=(
                        ("instrument", "SSE.600519"),
                        ("start", "2026-06-01"),
                        ("end", "2026-06-30"),
                    ),
                )
            )
        )

    assert error.value.retryable is False


def test_adapter_returns_a_valid_empty_batch_when_akshare_has_no_daily_bars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AKShare 空 DataFrame 是业务空集，不是供应商结构漂移。"""
    frame = FakeFrame([])
    # 匿名回调固定返回空窗口，验证适配器交给应用层记录空观测。
    monkeypatch.setattr(tencent_daily_bars.ak, "stock_zh_a_hist_tx", lambda **_: frame)

    batch = asyncio.run(
        AkshareTencentDailyBarsAdapter(request_timeout_seconds=5).fetch(
            SourceRequest(
                capability="equity.bar.1d.raw",
                parameters=(
                    ("instrument", "SSE.600519"),
                    ("start", "2026-07-01"),
                    ("end", "2026-07-29"),
                ),
            )
        )
    )

    assert json.loads(batch.payload) == {
        "schema": "quant-v2.equity-daily-bar.v1",
        "instrument": "SSE.600519",
        "bars": [],
    }
    assert batch.raw_payload == b'{"instrument":"SSE.600519","records":[]}'
    assert batch.schema_fingerprint == tencent_daily_bars._SCHEMA_FINGERPRINT


def test_adapter_rejects_empty_frame_without_frozen_columns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """无冻结列集合的空响应不能证明合法空窗口，必须作为 schema 漂移失败。"""
    frame = FakeFrame([], columns=())
    # 匿名回调返回无列空响应，验证它不能伪装成 passed 零记录 coverage。
    monkeypatch.setattr(tencent_daily_bars.ak, "stock_zh_a_hist_tx", lambda **_: frame)

    with pytest.raises(ProviderError, match="schema changed") as error:
        asyncio.run(
            AkshareTencentDailyBarsAdapter(request_timeout_seconds=5).fetch(
                SourceRequest(
                    capability="equity.bar.1d.raw",
                    parameters=(
                        ("instrument", "SSE.600519"),
                        ("start", "2026-07-01"),
                        ("end", "2026-07-29"),
                    ),
                )
            )
        )

    assert error.value.retryable is False


def test_adapter_rejects_bse_before_calling_unsupported_tencent_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """北交所不在已验证来源范围内，不能把端点 KeyError 转成合法空窗口。"""

    def unexpected_call(**_kwargs: object) -> None:
        """若适配器错误访问未支持端点则立即暴露。"""
        raise AssertionError("unsupported BSE endpoint must not be called")

    monkeypatch.setattr(tencent_daily_bars.ak, "stock_zh_a_hist_tx", unexpected_call)
    adapter = AkshareTencentDailyBarsAdapter(request_timeout_seconds=5)

    with pytest.raises(ProviderError) as captured:
        asyncio.run(
            adapter.fetch(
                SourceRequest(
                    capability="equity.bar.1d.raw",
                    parameters=(
                        ("instrument", "BSE.835185"),
                        ("start", "2026-07-01"),
                        ("end", "2026-07-29"),
                    ),
                )
            )
        )

    assert adapter.supported_exchanges == frozenset(
        {tencent_daily_bars.Exchange.SSE, tencent_daily_bars.Exchange.SZSE}
    )
    assert captured.value.code is ProviderErrorCode.CURRENTLY_UNSUPPORTED
    assert captured.value.retryable is False
