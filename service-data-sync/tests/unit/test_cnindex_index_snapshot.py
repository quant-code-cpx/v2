"""AKShare 国证指数影子快照 adapter 的 SDK 边界与字段映射测试。"""

from __future__ import annotations

import asyncio
import json
from datetime import date

import pytest

from service_data_sync.application.ports.data_source import ProviderError, SourceRequest
from service_data_sync.infrastructure.providers.akshare import cnindex_index_snapshot
from service_data_sync.infrastructure.providers.akshare.cnindex_index_snapshot import (
    AkshareCnindexIndexSnapshotAdapter,
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


def test_catalog_snapshot_keeps_unverified_provider_units_out_of_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """国证目录只输出稳定身份和样本数，不把 adapter 已换算的行情单位当作 canonical 事实。"""
    captured: list[bool] = []

    def fake_catalog() -> FakeFrame:
        """记录唯一 SDK 调用并返回含单位不明数值的一条目录记录。"""
        captured.append(True)
        return FakeFrame(
            [
                {
                    "指数代码": "399001",
                    "指数简称": "深证成指",
                    "样本数": "500",
                    "总市值": 123.45,
                }
            ]
        )

    monkeypatch.setattr(cnindex_index_snapshot.ak, "index_all_cni", fake_catalog)
    batch = asyncio.run(
        AkshareCnindexIndexSnapshotAdapter(request_timeout_seconds=5).fetch(
            SourceRequest(
                capability="index.catalog.snapshot",
                parameters=(("administrator", "CNI"),),
            )
        )
    )

    payload = json.loads(batch.payload)
    assert captured == [True]
    assert payload["records"] == [
        {"indexCode": "399001", "indexName": "深证成指", "constituentCount": 500}
    ]


@pytest.mark.parametrize(
    ("capability", "expected_key"),
    [
        ("index.constituent.snapshot", "constituents"),
        ("index.weight.snapshot", "weights"),
    ],
)
def test_detail_snapshot_retains_date_but_never_guesses_exchange(
    monkeypatch: pytest.MonkeyPatch,
    capability: str,
    expected_key: str,
) -> None:
    """国证详情同源输出必须保留唯一日期，且未知交易所保持空值等待身份质量门。"""
    captured: dict[str, str] = {}

    def fake_detail(*, symbol: str) -> FakeFrame:
        """记录 SDK 参数并返回一条带日期、行业和权重的样本详情。"""
        captured["symbol"] = symbol
        return FakeFrame(
            [
                {
                    "日期": date(2026, 7, 28),
                    "样本代码": 1,
                    "样本简称": "平安银行",
                    "所属行业": "银行",
                    "总市值": "123456.78",
                    "权重": "2.50",
                }
            ]
        )

    monkeypatch.setattr(cnindex_index_snapshot.ak, "index_detail_cni", fake_detail)
    batch = asyncio.run(
        AkshareCnindexIndexSnapshotAdapter(request_timeout_seconds=5).fetch(
            SourceRequest(
                capability=capability,
                parameters=(("administrator", "CNI"), ("indexCode", "399001")),
            )
        )
    )

    payload = json.loads(batch.payload)
    assert captured == {"symbol": "399001"}
    assert payload[expected_key][0]["sourceSymbol"] == "000001"
    assert payload[expected_key][0]["sourceExchange"] is None
    if capability == "index.constituent.snapshot":
        assert payload["sourceAsOfDate"] == "2026-07-28"
    else:
        assert payload["weightDate"] == "2026-07-28"
        assert payload["weightType"] == "OBSERVED"


def test_adapter_rejects_non_cnindex_request_before_calling_sdk() -> None:
    """中证或未知管理人不能误走国证 adapter，也不能触发外部 SDK 调用。"""
    adapter = AkshareCnindexIndexSnapshotAdapter(request_timeout_seconds=5)

    with pytest.raises(ProviderError, match="requires administrator CNI"):
        asyncio.run(
            adapter.fetch(
                SourceRequest(
                    capability="index.catalog.snapshot",
                    parameters=(("administrator", "CSI"),),
                )
            )
        )
