"""证券目录应用编排与标准载荷校验的单元测试。"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime
from uuid import uuid4

import pytest

from service_data_sync.application.equity.master_catalog_sync import (
    EquityCatalogSyncService,
    decode_equity_catalog_batch,
)
from service_data_sync.application.ports.data_source import (
    ProviderBatch,
    ProviderError,
    SourceRequest,
)
from service_data_sync.application.ports.equity_master import (
    PublishedCnAAggregate,
    PublishedEquityCatalog,
)
from service_data_sync.application.ports.market_data import RawPayload
from service_data_sync.domain.equity import Exchange


class FakeSource:
    """返回一个来源元数据完整的确定性目录批次。"""

    provider_id = "fake-equity-catalog"

    def capabilities(self) -> frozenset[str]:
        """仅声明证券目录同步所需能力。"""
        return frozenset({"equity.master.catalog"})

    async def fetch(self, request: SourceRequest) -> ProviderBatch:
        """根据标准交易所请求构造一份可发布的目录 JSON。"""
        exchange = dict(request.parameters)["exchange"]
        return ProviderBatch(
            provider_id=self.provider_id,
            capability=request.capability,
            payload=json.dumps(
                {
                    "schema": "quant-v2.equity-master-catalog.v1",
                    "exchange": exchange,
                    "entries": [{"symbol": "600519", "name": "贵州茅台", "listedOn": "2001-08-27"}],
                },
                separators=(",", ":"),
            ).encode(),
            observed_at=datetime(2026, 7, 27, tzinfo=UTC),
            raw_payload=b'{"raw":true}',
            raw_content_type="application/json",
            upstream_source="test-upstream",
            adapter_version="test-v1",
            schema_fingerprint="a" * 64,
        )


class FakeRawPayloadStore:
    """捕获发布前必须归档的不可变原始证据。"""

    def __init__(self) -> None:
        """初始化空的原始对象捕获容器。"""
        self.payloads: list[RawPayload] = []

    def put(self, payload: RawPayload) -> str:
        """保存测试中的原始载荷并返回确定性对象 URI。"""
        self.payloads.append(payload)
        return f"s3://test/{payload.object_key}"

    def get(self, uri: str) -> bytes:
        """主数据目录同步不读取历史 raw；意外调用必须中断测试。"""
        raise AssertionError(f"unexpected raw replay read: {uri}")


class FakeRepository:
    """记录用例交给主数据仓储的标准目录事实。"""

    def __init__(self) -> None:
        """初始化尚未接收目录发布请求的替身。"""
        self.kwargs: dict[str, object] = {}

    def publish_catalog(self, **kwargs: object) -> PublishedEquityCatalog:
        """捕获完整输入，并返回一份最小发布摘要。"""
        self.kwargs = kwargs
        return PublishedEquityCatalog(
            snapshot_id=uuid4(),
            data_version=uuid4(),
            inserted_count=1,
            unchanged_count=0,
        )

    def publish_cn_a_aggregate(self) -> PublishedCnAAggregate:
        """防止目录单所同步意外触发三所聚合发布。"""
        raise AssertionError("单所目录同步不应调用聚合发布")


def test_sync_archives_raw_evidence_and_passes_catalog_lineage_to_repository() -> None:
    """目录用例必须先归档原始字节，再交付标准行与 adapter 血缘。"""
    repository = FakeRepository()
    raw_store = FakeRawPayloadStore()

    result = asyncio.run(
        EquityCatalogSyncService(
            source=FakeSource(),
            repository=repository,
            raw_payload_store=raw_store,
        ).sync(exchange=Exchange.SSE, target_date=date(2026, 7, 27))
    )

    assert result.exchange is Exchange.SSE
    assert result.inserted_count == 1
    assert raw_store.payloads[0].payload == b'{"raw":true}'
    assert repository.kwargs["adapter_version"] == "test-v1"
    assert repository.kwargs["upstream_source"] == "test-upstream"


def test_decode_catalog_rejects_duplicate_symbols() -> None:
    """同一完整交易所快照中的重复代码必须在写库前隔离。"""
    payload = json.dumps(
        {
            "schema": "quant-v2.equity-master-catalog.v1",
            "exchange": "SSE",
            "entries": [
                {"symbol": "600519", "name": "甲", "listedOn": None},
                {"symbol": "600519", "name": "乙", "listedOn": None},
            ],
        }
    ).encode()

    with pytest.raises(ProviderError, match="duplicate symbols"):
        decode_equity_catalog_batch(payload, exchange=Exchange.SSE)
