"""AKShare 中证指数影子快照 adapter 的 SDK 边界与字段映射测试。"""

from __future__ import annotations

import asyncio
import json
from datetime import date

import pytest

from service_data_sync.application.ports.data_source import ProviderError, SourceRequest
from service_data_sync.infrastructure.providers.akshare import csindex_index_snapshot
from service_data_sync.infrastructure.providers.akshare.csindex_index_snapshot import (
    AkshareCsindexIndexSnapshotAdapter,
)


class FakeFrame:
    """提供 adapter 所需最小 DataFrame 接口，避免测试依赖 pandas 实现细节。"""

    def __init__(self, rows: list[dict[str, object]]) -> None:
        """保存确定性来源记录。"""
        self._rows = rows

    @property
    def empty(self) -> bool:
        """按是否存在记录报告来源响应是否为空。"""
        return not self._rows

    def to_dict(self, *, orient: str) -> list[dict[str, object]]:
        """仅支持 adapter 使用的 records 投影。"""
        assert orient == "records"
        return self._rows


def test_catalog_snapshot_calls_only_catalog_sdk_and_emits_neutral_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """中证目录只调用固定 SDK 函数，且不泄漏中文来源列名给应用层。"""
    captured: list[bool] = []

    def fake_catalog() -> FakeFrame:
        """记录唯一 SDK 调用并返回一条最小合法目录记录。"""
        captured.append(True)
        return FakeFrame(
            [
                {
                    "指数代码": 300,
                    "指数简称": "沪深300",
                    "指数全称": "沪深300指数",
                    "基日": "2004-12-31",
                    "基点": "1000",
                    "发布日期": "2005-04-08",
                    # AKShare 1.18.81 会把目录整数投影成浮点展示文本；数值仍必须严格为整数。
                    "样本数量": "300.0",
                }
            ]
        )

    monkeypatch.setattr(csindex_index_snapshot.ak, "index_csindex_all", fake_catalog)
    batch = asyncio.run(
        AkshareCsindexIndexSnapshotAdapter(request_timeout_seconds=5).fetch(
            SourceRequest(
                capability="index.catalog.snapshot",
                parameters=(("administrator", "CSI"),),
            )
        )
    )

    payload = json.loads(batch.payload)
    raw = json.loads(batch.raw_payload or b"{}")
    assert captured == [True]
    assert payload == {
        "schema": "quant-v2.index-catalog-snapshot.v1",
        "administrator": "CSI",
        "records": [
            {
                "indexCode": "000300",
                "indexName": "沪深300",
                "fullName": "沪深300指数",
                "baseDate": "2004-12-31",
                "baseValue": "1000",
                "publishedDate": "2005-04-08",
                "constituentCount": 300,
            }
        ],
    }
    assert raw["records"][0]["指数代码"] == 300


def test_catalog_snapshot_preserves_real_alphanumeric_index_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """中证目录的 ``H00999`` 必须保留为指数身份，不能被旧纯数字质量门过滤。"""

    def fake_catalog() -> FakeFrame:
        """返回已由真实中证目录验证的六码字母数字索引代码。"""
        return FakeFrame(
            [
                {
                    "指数代码": "H00999",
                    "指数简称": "中证A500",
                    "指数全称": "中证A500指数",
                    "基日": "2024-12-31",
                    "基点": "1000",
                    "发布日期": "2025-01-02",
                    "样本数量": "500.0",
                }
            ]
        )

    monkeypatch.setattr(csindex_index_snapshot.ak, "index_csindex_all", fake_catalog)
    batch = asyncio.run(
        AkshareCsindexIndexSnapshotAdapter(request_timeout_seconds=5).fetch(
            SourceRequest(
                capability="index.catalog.snapshot",
                parameters=(("administrator", "CSI"),),
            )
        )
    )

    payload = json.loads(batch.payload)
    assert payload["records"][0]["indexCode"] == "H00999"


@pytest.mark.parametrize("value", ("300.5", "Infinity", "-1"))
def test_catalog_snapshot_rejects_non_integral_or_invalid_sample_count(value: str) -> None:
    """样本数量兼容只接受数值上精确为非负整数的来源值。"""
    with pytest.raises(ValueError, match="source count must be a non-negative integer"):
        csindex_index_snapshot._optional_non_negative_int(value)


def test_constituent_snapshot_preserves_observed_only_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """当前成分接口不得被标记为历史有效成分，来源原字段只留在 raw evidence。"""
    captured: dict[str, str] = {}

    def fake_constituents(*, symbol: str) -> FakeFrame:
        """记录来源指数代码并返回一条含无关字段的当前成员记录。"""
        captured["symbol"] = symbol
        return FakeFrame(
            [
                {
                    "成分券代码": 600000,
                    "成分券名称": "浦发银行",
                    "交易所": "上海证券交易所",
                    "权重": 1.2,
                }
            ]
        )

    monkeypatch.setattr(
        csindex_index_snapshot.ak,
        "index_stock_cons_csindex",
        fake_constituents,
    )
    batch = asyncio.run(
        AkshareCsindexIndexSnapshotAdapter(request_timeout_seconds=5).fetch(
            SourceRequest(
                capability="index.constituent.snapshot",
                parameters=(("administrator", "CSI"), ("indexCode", "000300")),
            )
        )
    )

    payload = json.loads(batch.payload)
    raw = json.loads(batch.raw_payload or b"{}")
    assert captured == {"symbol": "000300"}
    assert payload["sourceAsOfDate"] is None
    assert payload["constituents"] == [
        {"sourceSymbol": "600000", "sourceName": "浦发银行", "sourceExchange": "上海证券交易所"}
    ]
    assert raw["records"][0]["权重"] == 1.2


def test_weight_snapshot_requires_one_explicit_source_date_and_percent_unit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """收盘权重只接受带唯一日期、百分比单位和合法范围的来源记录。"""
    captured: dict[str, str] = {}

    def fake_weights(*, symbol: str) -> FakeFrame:
        """记录来源指数代码并返回一条中证收盘权重记录。"""
        captured["symbol"] = symbol
        return FakeFrame(
            [
                {
                    "成分券代码": 600000,
                    "成分券名称": "浦发银行",
                    "交易所": "上海证券交易所",
                    "日期": date(2026, 7, 28),
                    "权重": "1.2500",
                }
            ]
        )

    monkeypatch.setattr(
        csindex_index_snapshot.ak,
        "index_stock_cons_weight_csindex",
        fake_weights,
    )
    batch = asyncio.run(
        AkshareCsindexIndexSnapshotAdapter(request_timeout_seconds=5).fetch(
            SourceRequest(
                capability="index.weight.snapshot",
                parameters=(("administrator", "CSI"), ("indexCode", "000300")),
            )
        )
    )

    payload = json.loads(batch.payload)
    assert captured == {"symbol": "000300"}
    assert payload["weightDate"] == "2026-07-28"
    assert payload["weightType"] == "OFFICIAL_CLOSE"
    assert payload["weights"][0]["weightValue"] == "1.2500"


def test_adapter_rejects_non_csindex_request_before_calling_sdk() -> None:
    """国证或未知管理人不能误走中证 adapter，也不能触发外部 SDK 调用。"""
    adapter = AkshareCsindexIndexSnapshotAdapter(request_timeout_seconds=5)

    with pytest.raises(ProviderError, match="requires administrator CSI"):
        asyncio.run(
            adapter.fetch(
                SourceRequest(
                    capability="index.catalog.snapshot",
                    parameters=(("administrator", "CNI"),),
                )
            )
        )
