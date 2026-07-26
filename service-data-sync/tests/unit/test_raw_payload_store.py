"""不可变原始来源证据存储边界的单元测试。"""

from __future__ import annotations

from service_data_sync.application.ports.market_data import RawPayload
from service_data_sync.infrastructure.object_storage.client import ObjectStorageClient
from service_data_sync.infrastructure.object_storage.raw_payload_store import S3RawPayloadStore


class FakeS3Client:
    """捕获 `put_object` 参数，不连接兼容 S3 的服务。"""

    def __init__(self) -> None:
        """初始化供存储边界断言使用的空调用捕获。"""
        self.calls: list[dict[str, object]] = []

    def put_object(self, **kwargs: object) -> None:
        """恰好一次记录不可变对象写入请求。"""
        self.calls.append(kwargs)


def test_raw_payload_store_writes_checksum_metadata_and_returns_private_s3_uri() -> None:
    """在标准发布开始前持久化带血缘元数据的原始字节。"""
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

    assert uri == "s3://raw-evidence/raw/equity/test.json"
    assert client.calls == [
        {
            "Bucket": "raw-evidence",
            "Key": "raw/equity/test.json",
            "Body": b'{"raw":true}',
            "ContentType": "application/json",
            "Metadata": {"sha256": "a" * 64},
        }
    ]
