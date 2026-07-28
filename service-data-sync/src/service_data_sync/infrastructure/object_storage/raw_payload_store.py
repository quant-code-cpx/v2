"""供行情应用用例使用的兼容 S3 原始证据存储。"""

from __future__ import annotations

from urllib.parse import urlparse

from service_data_sync.application.ports.market_data import RawPayload, RawPayloadStore
from service_data_sync.infrastructure.object_storage.client import ObjectStorageClient


class S3RawPayloadStore(RawPayloadStore):
    """向服务自有的兼容 S3 桶写入不可变数据源证据。"""

    def __init__(self, object_storage: ObjectStorageClient) -> None:
        """接收组合根已配置好的对象存储客户端。"""
        self._object_storage = object_storage

    def put(self, payload: RawPayload) -> str:
        """携带校验和元数据持久化字节，并返回稳定的非公开 S3 URI。"""
        self._object_storage.client.put_object(
            Bucket=self._object_storage.bucket,
            Key=payload.object_key,
            Body=payload.payload,
            ContentType=payload.content_type,
            Metadata={"sha256": payload.content_sha256},
        )
        return f"s3://{self._object_storage.bucket}/{payload.object_key}"

    def get(self, uri: str) -> bytes:
        """只读取本服务桶中的受控 S3 URI，拒绝外部桶或路径穿透输入。"""
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
