"""不可变原始来源证据存储边界的单元测试。"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import cast

import pytest

from service_data_sync.application.ports.data_source import ProviderBatch, SourceRequest
from service_data_sync.application.ports.market_data import RawPayload
from service_data_sync.infrastructure.object_storage.client import ObjectStorageClient
from service_data_sync.infrastructure.object_storage.raw_payload_store import (
    FailureEvidenceDataSource,
    S3RawPayloadStore,
    retain_failure_evidence,
)


class FakeS3Client:
    """捕获 `put_object` 参数，不连接兼容 S3 的服务。"""

    def __init__(self) -> None:
        """初始化供存储边界断言使用的空调用捕获。"""
        self.calls: list[dict[str, object]] = []

    def put_object(self, **kwargs: object) -> None:
        """恰好一次记录不可变对象写入请求。"""
        self.calls.append(kwargs)

    def get_object(self, **kwargs: object) -> dict[str, object]:
        """返回固定字节流，验证 replay 只能读取配置桶中的对象。"""
        self.calls.append(kwargs)
        return {"Body": FakeBody(b'{"raw":true}')}


class FakeBody:
    """提供 boto3 流式 body 的最小替身。"""

    def __init__(self, payload: bytes) -> None:
        """保存待返回的原始对象字节。"""
        self._payload = payload

    def read(self) -> bytes:
        """返回一次完整 S3 对象内容。"""
        return self._payload


class FakeSource:
    """返回固定原始与标准载荷，验证解码前暂存边界。"""

    provider_id = "akshare-test"

    def capabilities(self) -> frozenset[str]:
        """声明唯一测试能力，保持来源端口完整形状。"""
        return frozenset({"test.raw"})

    async def fetch(self, request: SourceRequest) -> ProviderBatch:
        """返回含不同 raw 与标准字节的一次来源观察。"""
        assert request.capability == "test.raw"
        return ProviderBatch(
            provider_id=self.provider_id,
            capability=request.capability,
            payload=b'{"normalized":true}',
            observed_at=datetime.now(UTC),
            content_type="application/vnd.quant-v2.test+json",
            raw_payload=b'{"akshare":true}',
            raw_content_type="application/json",
        )


def test_raw_payload_store_defers_success_payload_and_returns_unretained_marker() -> None:
    """成功路径仅返回摘要标记，不能把来源字节写入对象存储。"""
    client = FakeS3Client()
    store = S3RawPayloadStore(ObjectStorageClient(client=client, bucket="raw-evidence"))

    uri = store.put(
        RawPayload(
            object_key="raw/equity/test.json",
            content_sha256="a" * 64,
            content_type="application/json",
            payload=b'{"raw":true}',
        )
    )

    assert uri == f"unretained://sha256/{'a' * 64}"
    assert client.calls == []


def test_raw_payload_store_persists_staged_payload_and_manifest_on_failure() -> None:
    """失败路径将暂存来源字节及无敏感错误文本的清单一起写入失败目录。"""
    client = FakeS3Client()
    store = S3RawPayloadStore(ObjectStorageClient(client=client, bucket="raw-evidence"))
    store.put(
        RawPayload(
            object_key="raw/equity/test.json",
            content_sha256="a" * 64,
            content_type="application/json",
            payload=b'{"raw":true}',
        )
    )

    manifest_uri = store.persist_failure(RuntimeError("provider failed"))

    assert manifest_uri is not None and manifest_uri.startswith("s3://raw-evidence/failures/")
    assert len(client.calls) == 2
    assert client.calls[0]["Body"] == b'{"raw":true}'
    manifest = json.loads(cast(bytes, client.calls[1]["Body"]))
    assert manifest["errorType"] == "RuntimeError"
    assert manifest["objects"][0]["sha256"] == "a" * 64


def test_retain_failure_evidence_discards_success_and_archives_exception() -> None:
    """统一执行包装器在成功时释放内存，在异常时固化原始排障证据。"""
    client = FakeS3Client()
    store = S3RawPayloadStore(ObjectStorageClient(client=client, bucket="raw-evidence"))

    def successful_operation() -> str:
        """模拟成功同步期间暂存一份来源载荷。"""
        store.put(
            RawPayload(
                object_key="raw/equity/success.json",
                content_sha256="b" * 64,
                content_type="application/json",
                payload=b'{"success":true}',
            )
        )
        return "published"

    def failing_operation() -> None:
        """模拟写入失败，保留已暂存来源载荷。"""
        store.put(
            RawPayload(
                object_key="raw/equity/failure.json",
                content_sha256="c" * 64,
                content_type="application/json",
                payload=b'{"failure":true}',
            )
        )
        raise RuntimeError("database failed")

    assert retain_failure_evidence(store, successful_operation) == "published"
    assert client.calls == []

    with pytest.raises(RuntimeError, match="database failed"):
        retain_failure_evidence(store, failing_operation)

    assert len(client.calls) == 2


def test_failure_evidence_source_stages_batch_before_a_decode_failure() -> None:
    """来源已返回但标准解码失败时，也必须能保存 AKShare 原始字节。"""
    client = FakeS3Client()
    store = S3RawPayloadStore(ObjectStorageClient(client=client, bucket="raw-evidence"))
    source = FailureEvidenceDataSource(FakeSource(), store)

    def failing_operation() -> None:
        """模拟应用层解码在调用 `put` 前失败的路径。"""
        asyncio.run(source.fetch(SourceRequest(capability="test.raw")))
        raise ValueError("schema drift")

    with pytest.raises(ValueError, match="schema drift"):
        retain_failure_evidence(store, failing_operation)

    assert len(client.calls) == 3
    assert client.calls[0]["Body"] == b'{"akshare":true}'
    assert client.calls[1]["Body"] == b'{"normalized":true}'


def test_raw_payload_store_reads_only_the_configured_private_bucket() -> None:
    """replay 应可读取本服务写入的 raw，但不能把 URI 当作任意 S3 读取能力。"""
    client = FakeS3Client()
    store = S3RawPayloadStore(ObjectStorageClient(client=client, bucket="raw-evidence"))

    assert store.get("s3://raw-evidence/raw/equity/test.json") == b'{"raw":true}'
    assert client.calls == [{"Bucket": "raw-evidence", "Key": "raw/equity/test.json"}]

    with pytest.raises(ValueError, match="configured"):
        store.get("s3://another-bucket/raw/equity/test.json")

    with pytest.raises(ValueError, match="not retained"):
        store.get(f"unretained://sha256/{'a' * 64}")
