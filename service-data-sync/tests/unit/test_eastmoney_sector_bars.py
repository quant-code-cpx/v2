"""AKShare 东财板块历史 adapter 的字段、周期和来源边界单元测试。"""

from __future__ import annotations

import asyncio
import json
from datetime import date
from typing import Any

import pytest

from service_data_sync.application.ports.data_source import SourceRequest
from service_data_sync.infrastructure.providers.akshare import eastmoney_sector_bars
from service_data_sync.infrastructure.providers.akshare.eastmoney_sector_bars import (
    AkshareEastmoneySectorBarsAdapter,
)


class FakeFrame:
    """提供 adapter 转换所需的最小 DataFrame 行接口。"""

    def __init__(self, records: list[dict[str, object]]) -> None:
        """保存确定性的供应商响应行。"""
        self._records = records

    @property
    def empty(self) -> bool:
        """报告响应是否没有任何历史行。"""
        return not self._records

    def to_dict(self, *, orient: str) -> list[dict[str, object]]:
        """仅接受 adapter 使用的 records 转换方式。"""
        assert orient == "records"
        return self._records


@pytest.mark.parametrize(
    ("scheme", "period", "function_name", "expected_period"),
    [
        ("eastmoney.industry", "1d", "stock_board_industry_hist_em", "日k"),
        ("eastmoney.industry", "1w", "stock_board_industry_hist_em", "周k"),
        ("eastmoney.industry", "1mo", "stock_board_industry_hist_em", "月k"),
        ("eastmoney.concept", "1d", "stock_board_concept_hist_em", "daily"),
        ("eastmoney.concept", "1w", "stock_board_concept_hist_em", "weekly"),
        ("eastmoney.concept", "1mo", "stock_board_concept_hist_em", "monthly"),
    ],
)
def test_adapter_uses_matching_upstream_period_without_daily_derivation(
    monkeypatch: pytest.MonkeyPatch,
    scheme: str,
    period: str,
    function_name: str,
    expected_period: str,
) -> None:
    """行业和概念的三个周期均应调用对应上游函数和原生周期字面量。"""
    captured: dict[str, Any] = {}
    frame = FakeFrame(
        [
            {
                "日期": date(2026, 6, 30),
                "开盘": "10",
                "最高": "11",
                "最低": "9",
                "收盘": "10.5",
                "成交量": "1000",
                "成交额": "10500",
                "振幅": "20",
                "涨跌幅": "5",
                "涨跌额": "0.5",
                "换手率": "3",
            }
        ]
    )

    def fake_history(**kwargs: object) -> FakeFrame:
        """记录 adapter 传给 SDK 的参数，并返回固定供应商形数据。"""
        captured.update(kwargs)
        return frame

    monkeypatch.setattr(eastmoney_sector_bars.ak, function_name, fake_history)
    batch = asyncio.run(
        AkshareEastmoneySectorBarsAdapter(request_timeout_seconds=5).fetch(
            SourceRequest(
                capability={
                    "1d": "sector.bar.1d.raw",
                    "1w": "sector.bar.1w.raw",
                    "1mo": "sector.bar.1mo.raw",
                }[period],
                parameters=(
                    ("sectorScheme", scheme),
                    ("sector", "BK0475"),
                    ("period", period),
                    ("start", "2026-06-01"),
                    ("end", "2026-06-30"),
                ),
            )
        )
    )

    payload = json.loads(batch.payload)
    assert captured["period"] == expected_period
    assert captured["adjust"] == ""
    assert payload["period"] == period
    assert payload["bars"][0]["volumeUnit"] == "provider_native"
    assert batch.raw_payload is not None


@pytest.mark.parametrize(
    ("scheme", "function_name"),
    [
        ("eastmoney.industry", "stock_board_industry_name_em"),
        ("eastmoney.concept", "stock_board_concept_name_em"),
    ],
)
def test_adapter_reads_catalog_with_scheme_specific_upstream_function(
    monkeypatch: pytest.MonkeyPatch, scheme: str, function_name: str
) -> None:
    """行业和概念目录均应通过各自 SDK 函数映射为中立代码和名称载荷。"""
    frame = FakeFrame([{"板块代码": "BK0475", "板块名称": "证券"}])

    def fake_catalog() -> FakeFrame:
        """返回确定性的单条东财目录记录。"""
        return frame

    monkeypatch.setattr(eastmoney_sector_bars.ak, function_name, fake_catalog)
    batch = asyncio.run(
        AkshareEastmoneySectorBarsAdapter(request_timeout_seconds=5).fetch(
            SourceRequest(capability="sector.catalog.raw", parameters=(("sectorScheme", scheme),))
        )
    )

    payload = json.loads(batch.payload)
    assert payload["schema"] == "quant-v2.sector-catalog.v1"
    assert payload["sectorScheme"] == scheme
    assert payload["sectors"] == [{"code": "BK0475", "name": "证券"}]
    assert batch.raw_payload is not None
