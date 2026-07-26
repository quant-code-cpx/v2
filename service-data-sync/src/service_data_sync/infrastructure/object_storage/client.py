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
    client: Any
    bucket: str

    @classmethod
    def from_settings(cls, settings: Settings) -> ObjectStorageClient:
        """Create S3-compatible client with bounded timeouts and one retry attempt."""
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
        """Verify configured bucket access without reading or writing business data."""
        try:
            self.client.head_bucket(Bucket=self.bucket)
        except (BotoCoreError, ClientError) as error:
            raise DependencyUnavailable("s3", "head_bucket") from error

    def close(self) -> None:
        """Close object-storage client during container shutdown."""
        self.client.close()
