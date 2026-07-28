"""东财板块 EOD 批量 adapter 的字段隔离与 schema 漂移测试。"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from service_data_sync.application.ports.data_source import ProviderError, SourceRequest
from service_data_sync.infrastructure.providers.akshare import eastmoney_sector_eod
from service_data_sync.infrastructure.providers.akshare.eastmoney_sector_eod import (
    AkshareEastmoneySectorEodAdapter,
)


class FakeFrame:
    """提供 EOD adapter 所需列名、空状态和 records 转换的最小 DataFrame 替身。"""

    def __init__(self, records: list[dict[str, object]]) -> None:
        """保存确定性供应商行，并从首行构造稳定列集合。"""
        self._records = records
        self.columns = tuple(records[0]) if records else ()

    @property
    def empty(self) -> bool:
        """报告供应商是否返回空横截面。"""
        return not self._records

    def to_dict(self, *, orient: str) -> list[dict[str, object]]:
        """仅支持 adapter 使用的 records 输出方式。"""
        assert orient == "records"
        return self._records


def test_adapter_uses_one_industry_batch_and_hides_vendor_rank(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """行业 EOD 应只调一次 name 接口，标准载荷不得保留供应商默认排名字段。"""
    frame = FakeFrame([_record()])
    calls = 0

    def fake_fetch() -> FakeFrame:
        """记录批量调用次数并返回一条完整供应商记录。"""
        nonlocal calls
        calls += 1
        return frame

    monkeypatch.setattr(eastmoney_sector_eod.ak, "stock_board_industry_name_em", fake_fetch)
    batch = asyncio.run(
        AkshareEastmoneySectorEodAdapter(request_timeout_seconds=5).fetch(
            SourceRequest(
                capability="sector.quote.eod.snapshot.raw",
                parameters=(("sectorScheme", "eastmoney.industry"), ("tradeDate", "2026-07-27")),
            )
        )
    )

    payload = json.loads(batch.payload)
    assert calls == 1
    assert payload["tradeDate"] == "2026-07-27"
    assert payload["quotes"] == [
        {
            "code": "BK0475",
            "name": "证券",
            "latestValue": "1000",
            "changeValue": "10",
            "changePercent": "1.01",
            "marketValue": "123456789",
            "turnoverPercent": "3.2",
            "advancers": 10,
            "decliners": 3,
            "leaderName": "示例证券",
            "leaderChangePercent": "5.2",
        }
    ]
    assert "排名" not in payload["quotes"][0]
    assert batch.raw_payload is not None
    assert batch.schema_fingerprint is not None


def test_adapter_quarantines_missing_required_vendor_column(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """任何必需列缺失都必须成为 schema 错误，不能用 partial 横截面继续发布。"""
    incomplete = _record()
    del incomplete["领涨股票-涨跌幅"]

    def fake_fetch() -> FakeFrame:
        """返回缺少必填列的供应商数据。"""
        return FakeFrame([incomplete])

    monkeypatch.setattr(eastmoney_sector_eod.ak, "stock_board_concept_name_em", fake_fetch)

    with pytest.raises(ProviderError, match="schema changed"):
        asyncio.run(
            AkshareEastmoneySectorEodAdapter(request_timeout_seconds=5).fetch(
                SourceRequest(
                    capability="sector.quote.eod.snapshot.raw",
                    parameters=(("sectorScheme", "eastmoney.concept"), ("tradeDate", "2026-07-27")),
                )
            )
        )


def test_adapter_quarantines_unapproved_extra_vendor_column(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """供应商新增列会改变 schema fingerprint；未显式认证前不得静默继续发布。"""
    changed = _record()
    changed["未批准字段"] = "drift"

    def fake_fetch() -> FakeFrame:
        """返回字段集合发生漂移的完整行。"""
        return FakeFrame([changed])

    monkeypatch.setattr(eastmoney_sector_eod.ak, "stock_board_industry_name_em", fake_fetch)

    with pytest.raises(ProviderError, match="schema changed"):
        asyncio.run(
            AkshareEastmoneySectorEodAdapter(request_timeout_seconds=5).fetch(
                SourceRequest(
                    capability="sector.quote.eod.snapshot.raw",
                    parameters=(
                        ("sectorScheme", "eastmoney.industry"),
                        ("tradeDate", "2026-07-27"),
                    ),
                )
            )
        )


def test_adapter_rejects_unrecognized_capability_before_calling_sdk() -> None:
    """错误 capability 必须在 adapter 边界被拒绝，不能触发任何未知上游请求。"""
    adapter = AkshareEastmoneySectorEodAdapter(request_timeout_seconds=5)

    with pytest.raises(ProviderError, match="unsupported"):
        asyncio.run(adapter.fetch(SourceRequest(capability="sector.bar.1d.raw")))


def test_adapter_quarantines_empty_batch_before_normalization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """空批量不是合法完整横截面，必须转化为不可重试 schema 错误。"""

    def fake_fetch() -> FakeFrame:
        """返回没有任何供应商行的空 DataFrame 替身。"""
        return FakeFrame([])

    monkeypatch.setattr(eastmoney_sector_eod.ak, "stock_board_industry_name_em", fake_fetch)

    with pytest.raises(ProviderError, match="no sector eod quotes"):
        asyncio.run(
            AkshareEastmoneySectorEodAdapter(request_timeout_seconds=5).fetch(
                SourceRequest(
                    capability="sector.quote.eod.snapshot.raw",
                    parameters=(
                        ("sectorScheme", "eastmoney.industry"),
                        ("tradeDate", "2026-07-27"),
                    ),
                )
            )
        )


def test_numeric_normalizers_reject_nan_and_fractional_counts() -> None:
    """供应商 NaN 与分数家数必须在 adapter 内隔离，不能传播至质量或持久化层。"""
    with pytest.raises(ValueError, match="finite"):
        eastmoney_sector_eod._optional_decimal_text("NaN")
    with pytest.raises(ValueError, match="non-negative integer"):
        eastmoney_sector_eod._optional_count("1.5")


def _record() -> dict[str, Any]:
    """构造锁定 12 列的东财 EOD 原始行，供 adapter 映射测试复用。"""
    return {
        "排名": 1,
        "板块名称": "证券",
        "板块代码": "BK0475",
        "最新价": "1000",
        "涨跌额": "10",
        "涨跌幅": "1.01",
        "总市值": "123456789",
        "换手率": "3.2",
        "上涨家数": 10,
        "下跌家数": 3,
        "领涨股票": "示例证券",
        "领涨股票-涨跌幅": "5.2",
    }
