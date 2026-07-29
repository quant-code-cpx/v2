"""指数 P0-A 影子同步的归档、解码和观察写入测试。"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from service_data_sync.application.index.shadow_sync import IndexShadowSyncService
from service_data_sync.application.ports.data_source import ProviderBatch, ProviderError
from service_data_sync.application.ports.index_shadow import StoredIndexShadowObservation
from service_data_sync.domain.index import IndexAdministrator, IndexCapability, IndexIdentifier


class FakeSource:
    """提供指定批次并记录请求，避免单元测试访问真实指数来源。"""

    provider_id = "fixture-index"

    def __init__(self, batch: ProviderBatch) -> None:
        """保存下一次 fetch 要返回的固定来源批次。"""
        self._batch = batch
        self.requests: list[object] = []

    def capabilities(self) -> frozenset[str]:
        """声明测试批次对应的唯一能力。"""
        return frozenset({self._batch.capability})

    async def fetch(self, request: object) -> ProviderBatch:
        """记录中立请求并返回预置批次。"""
        self.requests.append(request)
        return self._batch


class MemoryRawStore:
    """记录归档顺序与字节，模拟服务自有私有对象存储。"""

    def __init__(self) -> None:
        """初始化空对象列表。"""
        self.payloads: list[object] = []

    def put(self, payload: object) -> str:
        """记录对象并返回测试专用私有 URI。"""
        self.payloads.append(payload)
        return f"s3://fixture/{len(self.payloads)}"

    def get(self, uri: str) -> bytes:
        """本测试不验证 replay，因此拒绝读取请求。"""
        raise AssertionError(uri)


class RecordingRepository:
    """记录应用层传入的观察对象，不依赖 PostgreSQL。"""

    def __init__(self) -> None:
        """初始化目录与快照调用记录。"""
        self.catalog_calls: list[dict[str, object]] = []
        self.snapshot_calls: list[dict[str, object]] = []

    def record_catalog(self, **kwargs: object) -> StoredIndexShadowObservation:
        """记录目录观察并返回研究态结果。"""
        self.catalog_calls.append(kwargs)
        return StoredIndexShadowObservation(uuid4(), len(kwargs["entries"]), "passed")  # type: ignore[arg-type]

    def record_snapshot(self, **kwargs: object) -> StoredIndexShadowObservation:
        """记录当前快照观察并返回研究态结果。"""
        self.snapshot_calls.append(kwargs)
        return StoredIndexShadowObservation(uuid4(), len(kwargs["items"]), "warned")  # type: ignore[arg-type]


def test_catalog_sync_archives_raw_and_normalized_before_recording_observation() -> None:
    """目录同步必须双归档后才写观察，并保留管理人而非 adapter 作为业务来源边界。"""
    batch = _batch(
        "index.catalog.snapshot",
        {
            "schema": "quant-v2.index-catalog-snapshot.v1",
            "administrator": "CSI",
            "records": [{"indexCode": "000300", "indexName": "沪深300", "constituentCount": 300}],
        },
    )
    store = MemoryRawStore()
    repository = RecordingRepository()

    result = asyncio.run(
        IndexShadowSyncService(
            source=FakeSource(batch), repository=repository, raw_payload_store=store
        ).sync_catalog(administrator=IndexAdministrator.CSI)
    )

    assert result.capability == "index.catalog.snapshot"
    assert len(store.payloads) == 2
    assert len(repository.catalog_calls) == 1
    entries = repository.catalog_calls[0]["entries"]
    assert len(entries) == 1  # type: ignore[arg-type]
    assert repository.catalog_calls[0]["administrator"] == "CSI"


def test_weight_sync_converts_confirmed_percentage_to_ratio_without_guessing_exchange() -> None:
    """权重只在 adapter 已确认百分比时转为比例，国证缺失交易所保持空值。"""
    batch = _batch(
        "index.weight.snapshot",
        {
            "schema": "quant-v2.index-weight-close-observed-snapshot.v1",
            "administrator": "CNI",
            "indexCode": "399001",
            "weightDate": "2026-07-28",
            "weightType": "OBSERVED",
            "weights": [
                {
                    "sourceSymbol": "000001",
                    "sourceName": "平安银行",
                    "sourceExchange": None,
                    "weightValue": "2.5",
                }
            ],
        },
    )
    repository = RecordingRepository()

    asyncio.run(
        IndexShadowSyncService(
            source=FakeSource(batch), repository=repository, raw_payload_store=MemoryRawStore()
        ).sync_snapshot(
            identifier=IndexIdentifier(IndexAdministrator.CNI, "399001"),
            capability=IndexCapability.WEIGHT_SNAPSHOT,
        )
    )

    item = repository.snapshot_calls[0]["items"][0]  # type: ignore[index]
    assert str(item.weight_value) == "0.025"  # type: ignore[union-attr]
    assert item.source_exchange is None  # type: ignore[union-attr]
    assert item.weight_kind == "observed"  # type: ignore[union-attr]


def test_invalid_snapshot_does_not_archive_or_record_observation() -> None:
    """重复来源证券表示 schema 或完整性错误，必须在对象存储和仓储写入前失败。"""
    batch = _batch(
        "index.constituent.snapshot",
        {
            "schema": "quant-v2.index-constituent-observed-snapshot.v1",
            "administrator": "CSI",
            "indexCode": "000300",
            "sourceAsOfDate": None,
            "constituents": [
                {"sourceSymbol": "600000", "sourceName": "甲", "sourceExchange": "SSE"},
                {"sourceSymbol": "600000", "sourceName": "乙", "sourceExchange": "SSE"},
            ],
        },
    )
    store = MemoryRawStore()
    repository = RecordingRepository()

    with pytest.raises(ProviderError, match="unique"):
        asyncio.run(
            IndexShadowSyncService(
                source=FakeSource(batch), repository=repository, raw_payload_store=store
            ).sync_snapshot(
                identifier=IndexIdentifier(IndexAdministrator.CSI, "000300"),
                capability=IndexCapability.CONSTITUENT_SNAPSHOT,
            )
        )

    assert store.payloads == []
    assert repository.snapshot_calls == []


def _batch(capability: str, payload: dict[str, object]) -> ProviderBatch:
    """构造同时包含 raw 与标准 JSON 的固定来源批次。"""
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    return ProviderBatch(
        provider_id="fixture-index",
        capability=capability,
        payload=encoded,
        raw_payload=b'{"raw":true}',
        observed_at=datetime(2026, 7, 29, 8, tzinfo=UTC),
        content_type="application/json",
        raw_content_type="application/json",
        upstream_source="fixture-index-owner",
        adapter_version="fixture-v1",
        schema_fingerprint="a" * 64,
    )
