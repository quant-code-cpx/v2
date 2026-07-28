"""板块目录用例的标准载荷、原始归档和激活发布回归测试。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import cast
from uuid import uuid4

from service_data_sync.application.ports.data_source import ProviderBatch, SourceRequest
from service_data_sync.application.ports.market_data import RawPayload
from service_data_sync.application.ports.sector_market_data import (
    PublishedSectorCatalog,
    SectorMarketDataRepository,
)
from service_data_sync.application.sector.catalog_sync import SectorCatalogSyncService
from service_data_sync.domain.sector import SectorCatalogEntry, SectorScheme


class FakeSource:
    """返回确定性行业目录标准载荷的中立数据源替身。"""

    provider_id = "fake-sector-catalog"

    def capabilities(self) -> frozenset[str]:
        """只声明目录能力，确保用例不会错误请求行情能力。"""
        return frozenset({"sector.catalog.raw"})

    async def fetch(self, request: SourceRequest) -> ProviderBatch:
        """校验分类体系参数后返回可审计原始目录记录。"""
        assert request.capability == "sector.catalog.raw"
        assert dict(request.parameters) == {"sectorScheme": "eastmoney.industry"}
        return ProviderBatch(
            provider_id=self.provider_id,
            capability=request.capability,
            payload=(
                b'{"schema":"quant-v2.sector-catalog.v1","sectorScheme":"eastmoney.industry",'
                b'"sectors":[{"code":"BK0002","name":"\xe8\xaf\x81\xe5\x88\xb8"},'
                b'{"code":"BK0001","name":"\xe9\x93\xb6\xe8\xa1\x8c"}]}'
            ),
            raw_payload=b'{"provider":true}',
            raw_content_type="application/json",
            observed_at=datetime(2026, 7, 1, tzinfo=UTC),
        )


class FakeRawPayloadStore:
    """记录 raw 写入先后顺序的对象存储端口替身。"""

    def __init__(self) -> None:
        """初始化空原始载荷集合。"""
        self.payloads: list[RawPayload] = []

    def put(self, payload: RawPayload) -> str:
        """保存载荷并返回测试对象 URI。"""
        self.payloads.append(payload)
        return f"s3://test/{payload.object_key}"

    def get(self, uri: str) -> bytes:
        """目录同步用例不应重读 raw evidence；调用即为流程回归。"""
        raise AssertionError(f"unexpected raw replay read: {uri}")


class FakeRepository:
    """断言目录发布发生在原始归档之后的仓储替身。"""

    def __init__(self, raw_store: FakeRawPayloadStore) -> None:
        """保存原始存储引用和接收到的目录项。"""
        self.raw_store = raw_store
        self.entries: tuple[SectorCatalogEntry, ...] = ()

    def publish_catalog(self, **kwargs: object) -> PublishedSectorCatalog:
        """验证目录项已排序且原始证据先于 canonical 发布。"""
        assert self.raw_store.payloads
        entries = kwargs["entries"]
        assert isinstance(entries, tuple)
        self.entries = entries
        return PublishedSectorCatalog(uuid4(), inserted_count=2, unchanged_count=0)


def test_catalog_sync_archives_raw_evidence_and_activates_sorted_entries() -> None:
    """目录同步必须先归档完整供应商载荷，再发布排序后的稳定代码和名称。"""
    raw_store = FakeRawPayloadStore()
    repository = FakeRepository(raw_store)

    result = asyncio.run(
        SectorCatalogSyncService(
            source=FakeSource(),
            repository=cast(SectorMarketDataRepository, repository),
            raw_payload_store=raw_store,
        ).sync(scheme=SectorScheme.EASTMONEY_INDUSTRY)
    )

    assert result.scheme is SectorScheme.EASTMONEY_INDUSTRY
    assert result.inserted_count == 2
    assert raw_store.payloads[0].payload == b'{"provider":true}'
    assert [entry.identifier.code for entry in repository.entries] == ["BK0001", "BK0002"]
