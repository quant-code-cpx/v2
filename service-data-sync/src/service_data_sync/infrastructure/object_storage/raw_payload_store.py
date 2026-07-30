"""按统一授权门禁在同步失败时归档来源证据清单。

每个批次都会计算摘要、大小、产品和观察时间；成功后清空短生命周期证据。失败时始终
写私有 `manifest`，只有调用方显式选择 `LICENSED_RAW_ALLOWED`、完成许可校验并提供
加密配置时才允许附带来源字节。默认业务模式可用 `MANIFEST_ONLY`，不能因失败绕过许可。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from urllib.parse import urlparse
from uuid import uuid4

from service_data_sync.application.ports.data_source import (
    DataSourcePort,
    ProviderBatch,
    ProviderError,
    ProviderErrorCode,
    SourceRequest,
)
from service_data_sync.application.ports.market_data import RawPayload, RawPayloadStore
from service_data_sync.infrastructure.object_storage.client import ObjectStorageClient

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _PendingEvidence:
    """保存失败证据摘要；仅获许可模式在内存中附带可落盘字节。"""

    sha256: str
    content_type: str
    byte_size: int
    payload: bytes | None
    product: str | None
    source_observed_at: str
    capability: str | None


class S3RawPayloadStore(RawPayloadStore):
    """仅在同步失败时向服务自有 `S3` 桶写入不可变证据清单。

    暂存区按内容摘要和 `MIME` 类型去重，生命周期严格限于一次同步操作。是否在内存
    暂存及最终写入来源字节由同一 retention mode 决定，成功批次永远不提供回放字节。
    """

    def __init__(
        self,
        object_storage: ObjectStorageClient,
        *,
        retention_mode: str = "LICENSED_RAW_ALLOWED",
        kms_key_id: str | None = None,
        rights_evidence_ref: str | None = None,
    ) -> None:
        """接收对象存储、留存策略和非秘密权利证据引用。"""
        if retention_mode not in {"LICENSED_RAW_ALLOWED", "MANIFEST_ONLY"}:
            raise ValueError("raw payload retention mode is invalid")
        if retention_mode == "LICENSED_RAW_ALLOWED" and kms_key_id is not None and not kms_key_id:
            raise ValueError("raw payload KMS key must not be blank")
        if rights_evidence_ref is not None and not rights_evidence_ref.strip():
            raise ValueError("raw payload rights evidence reference must not be blank")
        self._object_storage = object_storage
        self._retention_mode = retention_mode
        self._kms_key_id = kms_key_id
        self._rights_evidence_ref = rights_evidence_ref
        self._pending: dict[tuple[str, str], _PendingEvidence] = {}

    def put(self, payload: RawPayload) -> str:
        """暂存来源字节并返回不可回放标记；成功同步绝不写入对象存储。"""
        self._stage_payload(payload)
        return f"unretained://sha256/{payload.content_sha256}"

    def stage_batch(self, batch: ProviderBatch) -> None:
        """在解码前登记批次证据，使 `schema` 失败也能形成可审计清单。

        原始载荷与标准化载荷都计算摘要；只有已通过外部许可配置的
        `LICENSED_RAW_ALLOWED` 模式才会把相应字节保留到失败归档阶段。
        """
        raw_payload = batch.raw_payload if batch.raw_payload is not None else batch.payload
        raw_content_type = batch.raw_content_type or batch.content_type
        metadata = {
            "product": batch.upstream_source,
            "source_observed_at": batch.observed_at.isoformat(),
            "capability": batch.capability,
        }
        self._stage_bytes(raw_payload, raw_content_type, **metadata)
        self._stage_bytes(batch.payload, batch.content_type, **metadata)

    def stage_failure_summary(
        self,
        payload: bytes,
        content_type: str,
        *,
        capability: str | None = None,
    ) -> None:
        """暂存 adapter 脱敏失败摘要；供应商原始失败响应不得经此入口落盘。"""
        self._stage_bytes(
            payload,
            content_type,
            product="provider-failure-summary",
            source_observed_at=datetime.now(UTC).isoformat(),
            capability=capability,
        )

    def persist_failure(self, error: Exception) -> str | None:
        """把本次已暂存载荷写入失败目录，并返回供日志定位的私有清单 URI。"""
        if not self._pending:
            return None

        failure_id = str(uuid4())
        captured_at = datetime.now(UTC)
        prefix = f"failures/{captured_at:%Y/%m/%d}/{failure_id}"
        objects: list[dict[str, object]] = []
        for sequence, evidence in enumerate(self._pending.values(), start=1):
            item: dict[str, object] = {
                "sha256": evidence.sha256,
                "contentType": evidence.content_type,
                "byteSize": evidence.byte_size,
                "product": evidence.product,
                "sourceObservedAt": evidence.source_observed_at,
                "capability": evidence.capability,
            }
            if evidence.payload is not None:
                # 只有显式许可模式会走到此分支；KMS 参数由调用方的许可配置提供。
                object_key = f"{prefix}/{sequence:03d}-{evidence.sha256}.bin"
                put_options: dict[str, object] = {}
                if self._kms_key_id is not None:
                    put_options = {
                        "ServerSideEncryption": "aws:kms",
                        "SSEKMSKeyId": self._kms_key_id,
                    }
                self._object_storage.client.put_object(
                    Bucket=self._object_storage.bucket,
                    Key=object_key,
                    Body=evidence.payload,
                    ContentType=evidence.content_type,
                    Metadata={"sha256": evidence.sha256},
                    **put_options,
                )
                item["uri"] = f"s3://{self._object_storage.bucket}/{object_key}"
            objects.append(item)

        # `manifest` 只记录定位和完整性元数据；异常文本可能含供应商细节，故仅留错误类别。
        manifest_key = f"{prefix}/manifest.json"
        manifest = json.dumps(
            {
                "failureId": failure_id,
                "capturedAt": captured_at.isoformat(),
                "errorType": type(error).__name__,
                "retentionMode": self._retention_mode,
                "rightsEvidenceRef": self._rights_evidence_ref,
                "objects": objects,
            },
            separators=(",", ":"),
        ).encode()
        manifest_put_options: dict[str, object] = {}
        if self._kms_key_id is not None:
            manifest_put_options = {
                "ServerSideEncryption": "aws:kms",
                "SSEKMSKeyId": self._kms_key_id,
            }
        self._object_storage.client.put_object(
            Bucket=self._object_storage.bucket,
            Key=manifest_key,
            Body=manifest,
            ContentType="application/json",
            Metadata={},
            **manifest_put_options,
        )
        return f"s3://{self._object_storage.bucket}/{manifest_key}"

    def discard(self) -> None:
        """在同步成功或失败证据已写入后释放进程内暂存字节。"""
        self._pending.clear()

    def _stage_bytes(
        self,
        payload: bytes,
        content_type: str,
        *,
        product: str | None,
        source_observed_at: str,
        capability: str | None,
    ) -> None:
        """构造失败证据摘要；默认策略不会在内存中继续保留 licensed 字节。"""
        digest = sha256(payload).hexdigest()
        self._stage(
            _PendingEvidence(
                sha256=digest,
                content_type=content_type,
                byte_size=len(payload),
                payload=(payload if self._retention_mode == "LICENSED_RAW_ALLOWED" else None),
                product=product,
                source_observed_at=source_observed_at,
                capability=capability,
            )
        )

    def _stage_payload(self, payload: RawPayload) -> None:
        """兼容应用层 `put`，并按当前策略决定是否暂存其字节。"""
        self._stage_bytes(
            payload.payload,
            payload.content_type,
            product=None,
            source_observed_at=datetime.now(UTC).isoformat(),
            capability=None,
        )

    def _stage(self, evidence: _PendingEvidence) -> None:
        """按摘要和内容类型去重，避免同一批次在归档编排中重复占用内存。"""
        self._pending.setdefault((evidence.sha256, evidence.content_type), evidence)

    def get(self, uri: str) -> bytes:
        """读取失败证据；成功同步的不可回放标记会明确拒绝读取。

        请求必须指向当前服务配置的桶，不能把调用方提供的 `URI` 当作任意 `S3` 读取能力。
        """
        if uri.startswith("unretained://sha256/"):
            raise ValueError("successful source payload was not retained")
        parsed = urlparse(uri)
        # 限定桶名避免内部诊断接口被利用为跨桶对象读取代理。
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
    """在来源批次返回后立即登记证据，避免解码失败早于清单生成步骤。

    它保持 `DataSourcePort` 的身份和能力语义不变；短生命周期内是否保留来源字节仍由
    同一授权模式决定，因此应用服务不能通过失败路径绕过许可。
    """

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
        """暂存成功批次；adapter 前置失败则暂存其脱敏证据后原样重抛。"""
        try:
            batch = await self._source.fetch(request)
        except ProviderError as error:
            if error.failure_evidence is not None:
                self._store.stage_failure_summary(
                    error.failure_evidence,
                    error.failure_evidence_content_type or "application/json",
                    capability=request.capability,
                )
            if error.code is ProviderErrorCode.CURRENTLY_UNSUPPORTED:
                _persist_handled_provider_observation(self._store, error)
            raise
        self._store.stage_batch(batch)
        return batch


def _persist_handled_provider_observation(
    store: S3RawPayloadStore,
    error: ProviderError,
) -> None:
    """持久化会被应用层转成成功空态的来源证据，归档失败仍不得改变业务分类。"""
    try:
        manifest_uri = store.persist_failure(error)
        if manifest_uri is not None:
            _LOGGER.warning(
                "来源暂不支持证据清单已归档",
                extra={"failure_evidence_manifest_uri": manifest_uri},
            )
    except Exception:
        _LOGGER.exception("来源暂不支持证据清单归档失败")
    finally:
        store.discard()


def retain_failure_evidence[RESULT](
    store: S3RawPayloadStore,
    operation: Callable[[], RESULT],
) -> RESULT:
    """执行同步；仅操作失败时固化受授权门禁控制的证据，且不掩盖原始异常。

    成功和失败后都会清空内存暂存；归档本身失败时仍重新抛出原始同步异常，以保留
    正确的重试分类和根因。
    """
    try:
        return operation()
    except Exception as error:
        try:
            manifest_uri = store.persist_failure(error)
            if manifest_uri is not None:
                _LOGGER.error(
                    "同步失败证据清单已归档",
                    extra={"failure_evidence_manifest_uri": manifest_uri},
                )
        except Exception:
            # 证据清单归档失败不应掩盖同步根因，否则调用方会失去正确重试分类。
            _LOGGER.exception("同步失败证据清单归档失败")
        raise
    finally:
        store.discard()


async def retain_failure_evidence_async[RESULT](
    store: S3RawPayloadStore,
    operation: Callable[[], Awaitable[RESULT]],
) -> RESULT:
    """异步执行同步；仅协程失败时固化受授权门禁控制的来源证据。

    语义与同步包装器相同，区别仅是等待被包装操作；它不会吞掉取消或供应商异常。
    """
    try:
        return await operation()
    except Exception as error:
        try:
            manifest_uri = store.persist_failure(error)
            if manifest_uri is not None:
                _LOGGER.error(
                    "同步失败证据清单已归档",
                    extra={"failure_evidence_manifest_uri": manifest_uri},
                )
        except Exception:
            # 证据清单归档失败不应掩盖同步根因，否则调用方会失去正确重试分类。
            _LOGGER.exception("同步失败证据清单归档失败")
        raise
    finally:
        store.discard()
