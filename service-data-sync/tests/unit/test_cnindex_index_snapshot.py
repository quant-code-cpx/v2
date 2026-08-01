"""AKShare 国证指数影子快照 adapter 的 SDK 边界与字段映射测试。"""

from __future__ import annotations

import asyncio
import hashlib
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


class FakeResponse:
    """提供受控 HTTP 目录读取所需的最小响应接口。"""

    def __init__(self, payload: dict[str, object]) -> None:
        """序列化固定来源 envelope，供测试核对原始证据保留行为。"""
        self.content = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()

    def raise_for_status(self) -> None:
        """模拟成功 HTTP 响应，不额外抛出传输错误。"""


def _catalog_record(**overrides: object) -> dict[str, object]:
    """构造与真实国证原始 JSON 完全同名的最小目录记录。"""
    record: dict[str, object] = {
        "id": 3000,
        "docchannel": 1027,
        "indexcode": "399001",
        "indextype": "100",
        "showcnindex": "1",
        "indexsource": "1",
        "realtimemarket": "1",
        "remark": None,
        "indexname": "深证成指",
        "indexename": "Shenzhen Index",
        "indexfullename": "Shenzhen Component Index",
        "indexfullcname": "深证成份指数",
        "samplesize": 500,
        "closeingPoint": "13578.9301",
        "percent": "0.0221",
        "peStatic": "28.0748",
        "peDynamic": "26.5763",
        "pb": "2.72",
        "volume": 28352911825,
        "amount": "849697787993.46",
        "totalMarketValue": "28240417560056.2",
        "freeMarketValue": "15536953424540.73",
        "sampleshowdate": None,
        "prefixmonth": None,
        "showDetail": "1",
        "dataSource": 0,
    }
    record.update(overrides)
    return record


def _catalog_payload(*records: dict[str, object]) -> dict[str, object]:
    """构造真实来源当前使用的全字典行 envelope。"""
    return {
        "code": 0,
        "data": {
            "status": 0,
            "rows": list(records),
        },
    }


def test_catalog_snapshot_uses_frozen_raw_json_contract_and_keeps_raw_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """国证目录以真实上游 JSON 字段读取，保留原文且不依赖失配 SDK 列名。"""
    source_payload = _catalog_payload(
        _catalog_record(),
        _catalog_record(id=3225, indexcode="AITCNYG", indexname="中华陆股通行业龙头R"),
        _catalog_record(id=4162, indexcode="39926401", indexname="创业软件R"),
    )
    captured: dict[str, object] = {}

    def fake_get(
        url: str,
        *,
        params: dict[str, str],
        timeout: float,
    ) -> FakeResponse:
        """记录受控 URL、冻结参数和超时，并返回真实形状的来源 envelope。"""
        captured.update({"url": url, "params": params, "timeout": timeout})
        return FakeResponse(source_payload)

    def unexpected_sdk_catalog() -> FakeFrame:
        """阻止测试意外退回 AKShare 已知失配的目录列重命名实现。"""
        pytest.fail("catalog must use the controlled raw JSON reader")

    monkeypatch.setattr(cnindex_index_snapshot.requests, "get", fake_get)
    monkeypatch.setattr(cnindex_index_snapshot.ak, "index_all_cni", unexpected_sdk_catalog)
    batch = asyncio.run(
        AkshareCnindexIndexSnapshotAdapter(request_timeout_seconds=5).fetch(
            SourceRequest(
                capability="index.catalog.snapshot",
                parameters=(("administrator", "CNI"),),
            )
        )
    )

    payload = json.loads(batch.payload)
    raw = json.loads(batch.raw_payload or b"{}")
    expected_fingerprint = hashlib.sha256(
        json.dumps(
            sorted(cnindex_index_snapshot._CNI_CATALOG_FIELDS),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    assert captured == {
        "url": "https://www.cnindex.com.cn/index/indexList",
        "params": {"channelCode": "-1", "rows": "2000", "pageNum": "1"},
        "timeout": 5.0,
    }
    assert payload["records"] == [
        {"indexCode": "399001", "indexName": "深证成指", "constituentCount": 500},
        {
            "indexCode": "AITCNYG",
            "indexName": "中华陆股通行业龙头R",
            "constituentCount": 500,
        },
        {"indexCode": "39926401", "indexName": "创业软件R", "constituentCount": 500},
    ]
    assert raw == source_payload
    assert batch.schema_fingerprint == expected_fingerprint


def test_catalog_snapshot_rejects_unknown_raw_column_without_dropping_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """国证目录出现未冻结字段时必须以不可重试 schema 错误停止，而非猜测列含义。"""
    source_payload = _catalog_payload(_catalog_record(unexpectedColumn="new"))

    def fake_get(
        url: str,
        *,
        params: dict[str, str],
        timeout: float,
    ) -> FakeResponse:
        """返回带未知列的来源 envelope。"""
        del url, params, timeout
        return FakeResponse(source_payload)

    monkeypatch.setattr(cnindex_index_snapshot.requests, "get", fake_get)

    with pytest.raises(ProviderError) as raised:
        asyncio.run(
            AkshareCnindexIndexSnapshotAdapter(request_timeout_seconds=5).fetch(
                SourceRequest(
                    capability="index.catalog.snapshot",
                    parameters=(("administrator", "CNI"),),
                )
            )
        )

    assert raised.value.code.value == "schema"
    assert raised.value.retryable is False


def test_catalog_snapshot_marks_transport_timeout_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """受控国证 HTTP 超时必须保持来源不可用且可重试的稳定分类。"""

    def fake_get(
        url: str,
        *,
        params: dict[str, str],
        timeout: float,
    ) -> FakeResponse:
        """模拟在来源读取前发生的网络超时。"""
        del url, params, timeout
        raise cnindex_index_snapshot.requests.Timeout("catalog timeout")

    monkeypatch.setattr(cnindex_index_snapshot.requests, "get", fake_get)

    with pytest.raises(ProviderError) as raised:
        asyncio.run(
            AkshareCnindexIndexSnapshotAdapter(request_timeout_seconds=5).fetch(
                SourceRequest(
                    capability="index.catalog.snapshot",
                    parameters=(("administrator", "CNI"),),
                )
            )
        )

    assert raised.value.code.value == "unavailable"
    assert raised.value.retryable is True


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
