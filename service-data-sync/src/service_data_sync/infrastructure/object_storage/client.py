"""创建并管理服务自有、兼容 `S3` 的对象存储连接。

对象存储只保存失败同步所需的原始证据，不是 `canonical` 数据库，也不承担正常
读取路径。本模块把连接超时、权限探测和驱动层错误统一收口，避免业务任务直接
接触凭据或 `S3 SDK` 细节。
"""

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
    """封装服务拥有的 `S3` 客户端与唯一允许访问的目标桶。

    ``client`` 是基础设施私有句柄；上层只能通过原始证据存储使用它，不能任意指定桶。
    """

    client: Any
    bucket: str

    @classmethod
    def from_settings(cls, settings: Settings) -> ObjectStorageClient:
        """从已校验配置创建客户端，并限制单次基础设施调用的等待成本。

        凭据仅在此处从受保护配置读取；连接与读取各五秒、`SDK` 最多一次重试，避免
        失败的证据归档长期占住同步 worker。
        """
        client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint_url,
            aws_access_key_id=settings.s3_access_key.get_secret_value(),
            aws_secret_access_key=settings.s3_secret_key.get_secret_value(),
            region_name=settings.s3_region,
            config=Config(
                # 证据留存是失败路径，短超时比无限等待更利于保留原始同步错误。
                connect_timeout=5,
                read_timeout=5,
                retries={"max_attempts": 1, "mode": "standard"},
            ),
        )
        return cls(client=client, bucket=settings.s3_bucket)

    def ping(self) -> None:
        """校验目标桶访问权限，不读取或写入任何业务对象。"""
        try:
            self.client.head_bucket(Bucket=self.bucket)
        except (BotoCoreError, ClientError) as error:
            raise DependencyUnavailable("s3", "head_bucket") from error

    def close(self) -> None:
        """在容器关闭时释放底层 HTTP 连接，避免 worker 重启时遗留连接池。"""
        self.client.close()
