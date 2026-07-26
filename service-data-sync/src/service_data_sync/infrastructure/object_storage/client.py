"""兼容 S3 对象存储客户端的创建、探测与关闭。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from service_data_sync.bootstrap.errors import DependencyUnavailable
from service_data_sync.bootstrap.settings import Settings


@dataclass
class ObjectStorageClient:
    """封装服务拥有的对象存储客户端及其目标桶。"""

    client: Any
    bucket: str

    @classmethod
    def from_settings(cls, settings: Settings) -> ObjectStorageClient:
        """创建兼容 S3 的客户端，限制超时且最多重试一次。"""
        client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint_url,
            aws_access_key_id=settings.s3_access_key.get_secret_value(),
            aws_secret_access_key=settings.s3_secret_key.get_secret_value(),
            region_name=settings.s3_region,
            config=Config(
                connect_timeout=5,
                read_timeout=5,
                retries={"max_attempts": 1, "mode": "standard"},
            ),
        )
        return cls(client=client, bucket=settings.s3_bucket)

    def ping(self) -> None:
        """校验目标桶访问权限，不读取或写入业务数据。"""
        try:
            self.client.head_bucket(Bucket=self.bucket)
        except (BotoCoreError, ClientError) as error:
            raise DependencyUnavailable("s3", "head_bucket") from error

    def close(self) -> None:
        """在容器关闭时关闭对象存储客户端。"""
        self.client.close()
