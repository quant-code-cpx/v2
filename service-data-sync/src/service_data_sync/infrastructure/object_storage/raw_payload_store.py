"""供同步失败排障使用的兼容 S3 原始证据存储。"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from hashlib import sha256
from urllib.parse import urlparse
from uuid import uuid4

from service_data_sync.application.ports.data_source import (
    DataSourcePort,
    ProviderBatch,
    SourceRequest,
)
from service_data_sync.application.ports.market_data import RawPayload, RawPayloadStore
from service_data_sync.infrastructure.object_storage.client import ObjectStorageClient

_LOGGER = logging.getLogger(__name__)


class S3RawPayloadStore(RawPayloadStore):
    """仅在同步失败时向服务自有 S3 桶写入不可变来源证据。"""

    def __init__(self, object_storage: ObjectStorageClient) -> None:
        """接收组合根已配置好的对象存储客户端。"""
        self._object_storage = object_storage
        self._pending: dict[tuple[str, str], RawPayload] = {}

    def put(self, payload: RawPayload) -> str:
        """暂存来源字节并返回不可回放标记；成功同步绝不写入对象存储。"""
        self._stage(payload)
        return f"unretained://sha256/{payload.content_sha256}"

    def stage_batch(self, batch: ProviderBatch) -> None:
        """在解码前暂存上游批次，保证 schema 失败也保留可排障来源字节。"""
        raw_payload = batch.raw_payload if batch.raw_payload is not None else batch.payload
        raw_content_type = batch.raw_content_type or batch.content_type
        self._stage_bytes(raw_payload, raw_content_type)
        self._stage_bytes(batch.payload, batch.content_type)

    def persist_failure(self, error: Exception) -> str | None:
        """把本次已暂存载荷写入失败目录，并返回供日志定位的私有清单 URI。"""
        if not self._pending:
            return None

        failure_id = str(uuid4())
        captured_at = datetime.now(UTC)
        prefix = f"failures/{captured_at:%Y/%m/%d}/{failure_id}"
        objects: list[dict[str, object]] = []
        for sequence, payload in enumerate(self._pending.values(), start=1):
            object_key = f"{prefix}/{sequence:03d}-{payload.content_sha256}.bin"
            self._object_storage.client.put_object(
                Bucket=self._object_storage.bucket,
                Key=object_key,
                Body=payload.payload,
                ContentType=payload.content_type,
                Metadata={"sha256": payload.content_sha256},
            )
            objects.append(
                {
                    "uri": f"s3://{self._object_storage.bucket}/{object_key}",
                    "sha256": payload.content_sha256,
                    "contentType": payload.content_type,
                    "byteSize": len(payload.payload),
                }
            )

        manifest_key = f"{prefix}/manifest.json"
        manifest = json.dumps(
            {
                "failureId": failure_id,
                "capturedAt": captured_at.isoformat(),
                "errorType": type(error).__name__,
                "objects": objects,
            },
            separators=(",", ":"),
        ).encode()
        self._object_storage.client.put_object(
            Bucket=self._object_storage.bucket,
            Key=manifest_key,
            Body=manifest,
            ContentType="application/json",
            Metadata={},
        )
        return f"s3://{self._object_storage.bucket}/{manifest_key}"

    def discard(self) -> None:
        """在同步成功或失败证据已写入后释放进程内暂存字节。"""
        self._pending.clear()

    def _stage_bytes(self, payload: bytes, content_type: str) -> None:
        """为仅在内存中留存的来源字节构造稳定摘要，不使用其旧对象路径。"""
        digest = sha256(payload).hexdigest()
        self._stage(
            RawPayload(
                object_key=f"pending/{digest}",
                content_sha256=digest,
                content_type=content_type,
                payload=payload,
            )
        )

    def _stage(self, payload: RawPayload) -> None:
        """按摘要和内容类型去重，避免同一批次在归档编排中重复占用内存。"""
        self._pending.setdefault((payload.content_sha256, payload.content_type), payload)

    def get(self, uri: str) -> bytes:
        """读取失败证据；成功同步的不可回放标记会明确拒绝读取。"""
        if uri.startswith("unretained://sha256/"):
            raise ValueError("successful source payload was not retained")
        parsed = urlparse(uri)
        if parsed.scheme != "s3" or parsed.netloc != self._object_storage.bucket:
            raise ValueError("raw URI must target the configured S3 bucket")
        key = parsed.path.lstrip("/")
        if not key or "//" in key or key.startswith("../"):
            raise ValueError("raw URI key is invalid")
        response = self._object_storage.client.get_object(
            Bucket=self._object_storage.bucket,
            Key=key,
        )
        payload = response["Body"].read()
        if not isinstance(payload, bytes):
            raise ValueError("raw S3 object must be bytes")
        return payload


class FailureEvidenceDataSource:
    """在来源批次返回后立即暂存证据，避免解码失败早于应用层归档步骤。"""

    def __init__(self, source: DataSourcePort, store: S3RawPayloadStore) -> None:
        """保存唯一来源和本次同步私有的失败证据暂存区。"""
        self._source = source
        self._store = store

    @property
    def provider_id(self) -> str:
        """透传来源身份，保持应用层的 provider 选择与血缘语义。"""
        return self._source.provider_id

    def capabilities(self) -> frozenset[str]:
        """不改变来源能力声明，包装器只增加失败时留证行为。"""
        return self._source.capabilities()

    async def fetch(self, request: SourceRequest) -> ProviderBatch:
        """先取得批次并暂存两种载荷，随后让应用层继续校验和发布。"""
        batch = await self._source.fetch(request)
        self._store.stage_batch(batch)
        return batch


def retain_failure_evidence[RESULT](
    store: S3RawPayloadStore,
    operation: Callable[[], RESULT],
) -> RESULT:
    """执行同步；仅操作失败时固化已暂存的来源证据，且不掩盖原始异常。"""
    try:
        return operation()
    except Exception as error:
        try:
            manifest_uri = store.persist_failure(error)
            if manifest_uri is not None:
                _LOGGER.error(
                    "同步失败原始证据已归档",
                    extra={"failure_evidence_manifest_uri": manifest_uri},
                )
        except Exception:
            # 证据归档失败不应掩盖原始同步失败，否则调用方会失去可重试的真实原因。
            _LOGGER.exception("同步失败原始证据归档失败")
        raise
    finally:
        store.discard()


async def retain_failure_evidence_async[RESULT](
    store: S3RawPayloadStore,
    operation: Callable[[], Awaitable[RESULT]],
) -> RESULT:
    """异步执行同步；仅协程失败时固化已暂存来源证据。"""
    try:
        return await operation()
    except Exception as error:
        try:
            manifest_uri = store.persist_failure(error)
            if manifest_uri is not None:
                _LOGGER.error(
                    "同步失败原始证据已归档",
                    extra={"failure_evidence_manifest_uri": manifest_uri},
                )
        except Exception:
            # 证据归档失败不应掩盖原始同步失败，否则调用方会失去可重试的真实原因。
            _LOGGER.exception("同步失败原始证据归档失败")
        raise
    finally:
        store.discard()
