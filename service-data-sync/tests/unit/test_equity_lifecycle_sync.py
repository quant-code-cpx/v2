"""显式上市生命周期编排和标准载荷的单元测试。"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime
from uuid import uuid4

import pytest

from service_data_sync.application.equity.lifecycle_sync import (
    EquityLifecycleSyncService,
    decode_equity_lifecycle_batch,
)
from service_data_sync.application.ports.data_source import (
    ProviderBatch,
    ProviderError,
    SourceRequest,
)
from service_data_sync.application.ports.equity_lifecycle import PublishedEquityLifecycle
from service_data_sync.application.ports.market_data import RawPayload
from service_data_sync.domain.equity import Exchange


class FakeSource:
    """返回一条显式退市事实和完整来源血缘的中立数据源替身。"""

    provider_id = "fake-lifecycle"

    def capabilities(self) -> frozenset[str]:
        """声明唯一支持的显式生命周期能力。"""
        return frozenset({"equity.lifecycle.explicit"})

    async def fetch(self, request: SourceRequest) -> ProviderBatch:
        """按请求交易所返回确定性标准生命周期 JSON。"""
        exchange = dict(request.parameters)["exchange"]
        return ProviderBatch(
            provider_id=self.provider_id,
            capability=request.capability,
            payload=json.dumps(
                {
                    "schema": "quant-v2.equity-lifecycle-explicit.v1",
                    "exchange": exchange,
                    "entries": [
                        {
                            "symbol": "600519",
                            "status": "DELISTED",
                            "effectiveOn": "2026-07-01",
                            "evidenceKind": "EXPLICIT_DELISTING",
                            "delistedOn": "2026-07-01",
                        }
                    ],
                },
                separators=(",", ":"),
            ).encode(),
            observed_at=datetime(2026, 7, 2, tzinfo=UTC),
            raw_payload=b'{"source":"explicit"}',
            raw_content_type="application/json",
            upstream_source="test-exchange",
            adapter_version="test-v1",
            schema_fingerprint="a" * 64,
        )


class FakeRawPayloadStore:
    """捕获应用层归档的不可变原始证据。"""

    def __init__(self) -> None:
        """初始化尚未写入对象的内存替身。"""
        self.payloads: list[RawPayload] = []

    def put(self, payload: RawPayload) -> str:
        """保存证据并返回稳定的伪对象地址。"""
        self.payloads.append(payload)
        return f"s3://test/{payload.object_key}"


class FakeRepository:
    """记录应用层传入的生命周期标准事实和来源字段。"""

    def __init__(self) -> None:
        """初始化未收到发布调用的替身状态。"""
        self.kwargs: dict[str, object] = {}

    def publish_lifecycle(self, **kwargs: object) -> PublishedEquityLifecycle:
        """捕获发布参数并返回最小稳定摘要。"""
        self.kwargs = kwargs
        return PublishedEquityLifecycle(
            snapshot_id=uuid4(),
            data_version=uuid4(),
            inserted_count=1,
            unchanged_count=0,
        )


def test_sync_archives_raw_evidence_and_publishes_explicit_lifecycle() -> None:
    """生命周期用例必须归档原始证据，并只交付带显式证据的标准事实。"""
    repository = FakeRepository()
    raw_store = FakeRawPayloadStore()

    result = asyncio.run(
        EquityLifecycleSyncService(
            source=FakeSource(),
            repository=repository,
            raw_payload_store=raw_store,
        ).sync(exchange=Exchange.SSE, target_date=date(2026, 7, 2))
    )

    assert result.exchange is Exchange.SSE
    assert result.inserted_count == 1
    assert raw_store.payloads[0].payload == b'{"source":"explicit"}'
    assert repository.kwargs["upstream_source"] == "test-exchange"
    assert repository.kwargs["entries"]


def test_decode_lifecycle_rejects_non_explicit_delisting_and_duplicate_fact() -> None:
    """目录缺席或重复行不能绕过显式退市证据和幂等边界。"""
    payload = json.dumps(
        {
            "schema": "quant-v2.equity-lifecycle-explicit.v1",
            "exchange": "SSE",
            "entries": [
                {
                    "symbol": "600519",
                    "status": "DELISTED",
                    "effectiveOn": "2026-07-01",
                    "evidenceKind": "CATALOG",
                    "delistedOn": "2026-07-01",
                }
            ],
        }
    ).encode()

    with pytest.raises(ProviderError, match="invalid equity lifecycle entry"):
        decode_equity_lifecycle_batch(payload, exchange=Exchange.SSE)


def test_decode_lifecycle_requires_approval_for_official_correction() -> None:
    """官方更正必须带人工审批引用，不能仅由来源适配器自行放行。"""
    payload = json.dumps(
        {
            "schema": "quant-v2.equity-lifecycle-explicit.v1",
            "exchange": "SSE",
            "entries": [
                {
                    "symbol": "600519",
                    "status": "LISTED",
                    "effectiveOn": "2026-07-01",
                    "evidenceKind": "OFFICIAL_CORRECTION",
                }
            ],
        }
    ).encode()

    with pytest.raises(ProviderError, match="invalid equity lifecycle entry"):
        decode_equity_lifecycle_batch(payload, exchange=Exchange.SSE)
