"""AKShare 适配器边界处腾讯专有字段标准化的单元测试。"""

from __future__ import annotations

import asyncio
import json
from datetime import date

import pytest

from service_data_sync.application.ports.data_source import ProviderError, SourceRequest
from service_data_sync.infrastructure.providers.akshare import tencent_daily_bars
from service_data_sync.infrastructure.providers.akshare.tencent_daily_bars import (
    AkshareTencentDailyBarsAdapter,
)


class FakeFrame:
    """提供适配器测试所需的最小 DataFrame 接口，不直接导入 pandas。"""

    def __init__(self, records: list[dict[str, object]]) -> None:
        """保存测试转换所需的确定性供应商形记录。"""
        self._records = records

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
                "high": "11.00",
                "low": "9.00",
                "close": "10.50",
                "volume": "10500",
                "amount": "11025000",
            }
        ]
    )
    # 匿名回调固定返回“手”口径样本，验证适配器只在 VWAP 对账成立时换算。
    monkeypatch.setattr(tencent_daily_bars.ak, "stock_zh_a_hist_tx", lambda **_: frame)

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
    assert batch.raw_payload is not None


def test_adapter_rejects_a_response_with_unreconcilable_volume_unit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """“股”和“手”都无法与日内价格区间对账时隔离供应商行。"""
    frame = FakeFrame(
        [
            {
                "date": "2026-06-30",
                "open": "10",
                "high": "11",
                "low": "9",
                "close": "10",
                "volume": "100",
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
