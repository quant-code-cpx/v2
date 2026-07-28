"""申万中立载荷解码、父级闭包、raw 归档与 replay 测试。"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import replace
from datetime import UTC, date, datetime
from typing import Any, cast
from uuid import UUID

import pytest

from service_data_sync.application.ports.data_source import (
    ProviderBatch,
    ProviderError,
    ProviderErrorCode,
    SourceRequest,
)
from service_data_sync.application.ports.market_data import RawPayload
from service_data_sync.application.ports.sw_sector import (
    SwCheckpoint,
    SwPublishedCapability,
    SwPublishResult,
    SwSourceObservation,
)
from service_data_sync.application.sector import sw_snapshot_sync
from service_data_sync.application.sector.sw_snapshot_sync import (
    SwSnapshotSyncService,
    decode_sw_snapshot,
)
from service_data_sync.domain.sw_sector import SwMethodology

_SNAPSHOT_DATE = date(2026, 7, 28)
_TAXONOMY_VERSION = UUID("10000000-0000-4000-8000-000000000001")
_VALUATION_VERSION = UUID("10000000-0000-4000-8000-000000000002")


class FakeSwSource:
    """返回一个具有完整三级父级关系的中立申万快照。"""

    provider_id = "fake-sw"

    def capabilities(self) -> frozenset[str]:
        """声明唯一申万完整快照能力。"""
        return frozenset({"sector.sw.snapshot.raw"})

    async def fetch(self, request: SourceRequest) -> ProviderBatch:
        """返回独立 raw evidence 与标准载荷。"""
        assert request.parameters == (("snapshotDate", _SNAPSHOT_DATE.isoformat()),)
        return ProviderBatch(
            provider_id=self.provider_id,
            capability=request.capability,
            payload=_payload(),
            raw_payload=b'{"raw":"sw"}',
            raw_content_type="application/json",
            observed_at=datetime(2026, 7, 28, 10, tzinfo=UTC),
            upstream_source="test.sw",
            adapter_version="test-v1",
            schema_fingerprint="a" * 64,
        )


class UnsupportedSwSource:
    """声明不支持申万快照，用于验证应用层能力门。"""

    provider_id = "unsupported"

    def capabilities(self) -> frozenset[str]:
        """返回空能力集合。"""
        return frozenset()

    async def fetch(self, request: SourceRequest) -> ProviderBatch:
        """能力门应在抓取前失败，因此任何调用都代表测试失败。"""
        raise AssertionError(f"不应抓取 {request.capability}")


class MemoryRawStore:
    """以内存保存 raw 与中立重放载荷，模拟私有对象存储。"""

    def __init__(self) -> None:
        """初始化 URI 到字节的空映射。"""
        self.values: dict[str, bytes] = {}

    def put(self, payload: RawPayload) -> str:
        """按写入顺序保存不可变字节并返回私有 URI。"""
        uri = f"s3://test/{len(self.values) + 1}"
        self.values[uri] = payload.payload
        return uri

    def get(self, uri: str) -> bytes:
        """读取精确 URI 的已归档载荷。"""
        return self.values[uri]


class FakeSwRepository:
    """捕获完整快照发布，并提供指定日期 replay checkpoint。"""

    def __init__(self) -> None:
        """初始化发布记录和可选 checkpoint。"""
        self.calls: list[tuple[object, SwSourceObservation]] = []
        self.checkpoint: SwCheckpoint | None = None

    def publish_snapshot(self, *, snapshot: object, source: SwSourceObservation) -> SwPublishResult:
        """记录发布并返回 taxonomy、估值两个独立 dataVersion。"""
        self.calls.append((snapshot, source))
        return _result()

    def get_checkpoint(self, *, snapshot_date: date) -> SwCheckpoint | None:
        """只返回测试预先设置的精确日期 checkpoint。"""
        assert snapshot_date == _SNAPSHOT_DATE
        return self.checkpoint


def test_decoder_builds_three_level_parent_closure_and_ratio_unit() -> None:
    """解码器应将父级名称解析为代码，并把来源百分数除以一百。"""
    snapshot = decode_sw_snapshot(_payload(), expected_date=_SNAPSHOT_DATE)

    edges = {(edge.ancestor_code, edge.descendant_code, edge.depth) for edge in snapshot.closure()}

    assert ("801010.SI", "850111.SI", 2) in edges
    assert ("801016.SI", "850111.SI", 1) in edges
    assert snapshot.valuations[-1].dividend_yield_ratio is not None
    assert str(snapshot.valuations[-1].dividend_yield_ratio) == "0.0061"


def test_publication_hashes_change_when_methodology_semantics_change() -> None:
    """相同行业值切换方法学版本时必须形成新的 taxonomy 与估值消费者版本。"""
    snapshot = decode_sw_snapshot(_payload(), expected_date=_SNAPSHOT_DATE)
    changed = replace(
        snapshot,
        methodology=SwMethodology(
            code=snapshot.methodology.code,
            version=2,
            status="source_reported",
            upstream_source=snapshot.methodology.upstream_source,
            semantic_spec_sha256="e" * 64,
        ),
    )

    assert changed.taxonomy_sha256() != snapshot.taxonomy_sha256()
    assert changed.valuation_sha256() != snapshot.valuation_sha256()


def test_sync_archives_raw_and_normalized_payload_then_replays_without_source() -> None:
    """同步应保存两类证据，replay 应验证两者摘要且不访问上游。"""
    store = MemoryRawStore()
    repository = FakeSwRepository()
    service = SwSnapshotSyncService(
        source=FakeSwSource(),
        repository=cast(object, repository),  # type: ignore[arg-type]
        raw_payload_store=store,
    )

    live = asyncio.run(service.sync(snapshot_date=_SNAPSHOT_DATE))
    source = repository.calls[0][1]
    repository.checkpoint = SwCheckpoint(
        snapshot_date=_SNAPSHOT_DATE,
        summary_sha256=hashlib.sha256(_payload()).hexdigest(),
        raw_sha256=hashlib.sha256(b'{"raw":"sw"}').hexdigest(),
        raw_uri=source.raw_uri,
        normalized_uri=source.normalized_uri,
        provider_id=source.provider_id,
        upstream_source=source.upstream_source,
        adapter_version=source.adapter_version,
        schema_fingerprint=source.schema_fingerprint,
        observed_at=source.observed_at,
        last_data_version=_TAXONOMY_VERSION,
    )
    replay = service.replay(snapshot_date=_SNAPSHOT_DATE)

    assert live.replayed is False
    assert replay.replayed is True
    assert len(store.values) == 2
    assert store.values[source.raw_uri] == b'{"raw":"sw"}'
    assert store.values[source.normalized_uri] == _payload()
    assert len(repository.calls) == 2

    store.values[source.raw_uri] = b'{"raw":"tampered"}'
    with pytest.raises(ValueError, match="raw payload digest"):
        service.replay(snapshot_date=_SNAPSHOT_DATE)


def test_sync_rejects_unsupported_source_and_missing_or_tampered_replay() -> None:
    """应用层应在 I/O 前检查能力，并拒绝缺失 checkpoint 或损坏标准载荷。"""
    store = MemoryRawStore()
    unsupported_repository = FakeSwRepository()
    unsupported = SwSnapshotSyncService(
        source=UnsupportedSwSource(),
        repository=cast(object, unsupported_repository),  # type: ignore[arg-type]
        raw_payload_store=store,
    )

    with pytest.raises(ProviderError) as captured:
        asyncio.run(unsupported.sync(snapshot_date=_SNAPSHOT_DATE))
    assert captured.value.code == ProviderErrorCode.INVALID_REQUEST
    assert captured.value.retryable is False

    repository = FakeSwRepository()
    service = SwSnapshotSyncService(
        source=FakeSwSource(),
        repository=cast(object, repository),  # type: ignore[arg-type]
        raw_payload_store=store,
    )
    with pytest.raises(ValueError, match="checkpoint"):
        service.replay(snapshot_date=_SNAPSHOT_DATE)

    asyncio.run(service.sync(snapshot_date=_SNAPSHOT_DATE))
    source = repository.calls[-1][1]
    repository.checkpoint = _checkpoint(source)
    store.values[source.normalized_uri] = b'{"tampered":true}'

    with pytest.raises(ValueError, match="payload digest"):
        service.replay(snapshot_date=_SNAPSHOT_DATE)


def test_decoder_quarantines_identity_level_and_hierarchy_schema_drift() -> None:
    """解码器应把结构、层级、身份和父级解析异常统一为不可重试 schema 错误。"""
    _assert_schema_error(b"{")

    wrong_identity = _decoded_payload()
    wrong_identity["schema"] = "unexpected"
    _assert_schema_error(wrong_identity)

    missing_methodology = _decoded_payload()
    missing_methodology["methodology"] = None
    _assert_schema_error(missing_methodology)

    missing_levels = _decoded_payload()
    missing_levels["levels"] = []
    _assert_schema_error(missing_levels)

    invalid_entry = _decoded_payload()
    invalid_entry["levels"][0]["items"] = "not-a-list"
    _assert_schema_error(invalid_entry)

    invalid_level = _decoded_payload()
    invalid_level["levels"][0]["level"] = 4
    _assert_schema_error(invalid_level)

    empty_level = _decoded_payload()
    empty_level["levels"][0]["items"] = []
    _assert_schema_error(empty_level)

    non_object_item = _decoded_payload()
    non_object_item["levels"][0]["items"] = ["not-an-object"]
    _assert_schema_error(non_object_item)

    ambiguous_name = _decoded_payload()
    duplicate = dict(ambiguous_name["levels"][0]["items"][0])
    duplicate["code"] = "801011.SI"
    ambiguous_name["levels"][0]["items"].append(duplicate)
    _assert_schema_error(ambiguous_name)

    missing_parent = _decoded_payload()
    missing_parent["levels"][1]["items"][0]["parentName"] = None
    _assert_schema_error(missing_parent)

    unknown_parent = _decoded_payload()
    unknown_parent["levels"][1]["items"][0]["parentName"] = "不存在行业"
    _assert_schema_error(unknown_parent)

    boolean_count = _decoded_payload()
    boolean_count["levels"][0]["items"][0]["componentCount"] = True
    _assert_schema_error(boolean_count)


def test_archive_batch_uses_provider_fallbacks_and_rejects_wrong_batch_type() -> None:
    """归档应在缺少原始响应元数据时使用标准载荷及稳定默认血缘。"""
    store = MemoryRawStore()

    with pytest.raises(TypeError, match="ProviderBatch"):
        sw_snapshot_sync._archive_batch(batch=object(), payload_store=store)

    batch = ProviderBatch(
        provider_id="fallback-provider",
        capability="sector.sw.snapshot.raw",
        payload=_payload(),
        observed_at=datetime(2026, 7, 28, 10, tzinfo=UTC),
        content_type="application/json",
    )
    source = sw_snapshot_sync._archive_batch(batch=batch, payload_store=store)

    assert source.upstream_source == "fallback-provider"
    assert source.source_payload_sha256 == source.normalized_payload_sha256
    assert store.values[source.raw_uri] == _payload()
    assert store.values[source.normalized_uri] == _payload()
    assert (
        source.schema_fingerprint == hashlib.sha256(b"quant-v2.sw-industry-snapshot.v1").hexdigest()
    )


def _payload() -> bytes:
    """构造三层一一父子链及完整估值覆盖的稳定中立载荷。"""
    levels = [
        _level(1, "801010.SI", "农林牧渔", None),
        _level(2, "801016.SI", "种植业", "农林牧渔"),
        _level(3, "850111.SI", "种子", "种植业"),
    ]
    return json.dumps(
        {
            "schema": "quant-v2.sw-industry-snapshot.v1",
            "scheme": "sw.industry",
            "snapshotDate": _SNAPSHOT_DATE.isoformat(),
            "methodology": {
                "code": "test-sw",
                "version": 1,
                "status": "source_reported",
                "upstreamSource": "test.sw",
                "semanticSpecSha256": "b" * 64,
            },
            "levels": levels,
        },
        separators=(",", ":"),
    ).encode()


def _decoded_payload() -> dict[str, Any]:
    """把稳定中立载荷解码为可独立修改的测试对象。"""
    return cast(dict[str, Any], json.loads(_payload()))


def _assert_schema_error(payload: bytes | dict[str, Any]) -> None:
    """断言任意漂移载荷被归类为不可重试 schema 错误。"""
    encoded = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    with pytest.raises(ProviderError) as captured:
        decode_sw_snapshot(encoded, expected_date=_SNAPSHOT_DATE)
    assert captured.value.code == ProviderErrorCode.SCHEMA
    assert captured.value.retryable is False


def _level(level: int, code: str, name: str, parent_name: str | None) -> dict[str, object]:
    """构造一个层级的一条中立行业与估值记录。"""
    return {
        "level": level,
        "items": [
            {
                "code": code,
                "name": name,
                "parentName": parent_name,
                "componentCount": 8,
                "staticPe": "67.1",
                "ttmPe": "95.96",
                "pb": "2.16",
                "dividendYieldPercent": "0.61",
            }
        ],
    }


def _checkpoint(source: SwSourceObservation) -> SwCheckpoint:
    """根据一次成功归档的来源观察构造 replay checkpoint。"""
    return SwCheckpoint(
        snapshot_date=_SNAPSHOT_DATE,
        summary_sha256=hashlib.sha256(_payload()).hexdigest(),
        raw_sha256=hashlib.sha256(b'{"raw":"sw"}').hexdigest(),
        raw_uri=source.raw_uri,
        normalized_uri=source.normalized_uri,
        provider_id=source.provider_id,
        upstream_source=source.upstream_source,
        adapter_version=source.adapter_version,
        schema_fingerprint=source.schema_fingerprint,
        observed_at=source.observed_at,
        last_data_version=_TAXONOMY_VERSION,
    )


def _result() -> SwPublishResult:
    """构造 taxonomy 与估值互不复用的发布版本。"""
    published_at = datetime(2026, 7, 28, 10, 1, tzinfo=UTC)
    return SwPublishResult(
        taxonomy=SwPublishedCapability(
            capability="sector.sw.taxonomy",
            data_version=_TAXONOMY_VERSION,
            snapshot_date=_SNAPSHOT_DATE,
            published_at=published_at,
            inserted_count=3,
            unchanged_count=0,
            row_count=3,
            content_sha256="c" * 64,
        ),
        valuation=SwPublishedCapability(
            capability="sector.sw.valuation",
            data_version=_VALUATION_VERSION,
            snapshot_date=_SNAPSHOT_DATE,
            published_at=published_at,
            inserted_count=3,
            unchanged_count=0,
            row_count=3,
            content_sha256="d" * 64,
        ),
    )
