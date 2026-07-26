"""Redis 客户端的创建、连通性探测与关闭。"""

from __future__ import annotations

from dataclasses import dataclass

import redis
from redis.exceptions import RedisError

from service_data_sync.bootstrap.errors import DependencyUnavailable
from service_data_sync.bootstrap.settings import Settings


@dataclass
class RedisClient:
    """封装服务拥有的 Redis 客户端，统一超时与领域错误转换。"""

    client: redis.Redis

    @classmethod
    def from_settings(cls, settings: Settings) -> RedisClient:
        """根据已校验 broker URL 创建受超时约束的 Redis 客户端。"""
        return cls(
            client=redis.Redis.from_url(
                settings.broker_url.get_secret_value(),
                socket_connect_timeout=5,
                socket_timeout=5,
            )
        )

    def ping(self) -> None:
        """检查 Redis 可达性，并将驱动错误转换为领域错误。"""
        try:
            self.client.ping()
        except RedisError as error:
            raise DependencyUnavailable("redis", "ping") from error

    def close(self) -> None:
        """在容器关闭时关闭 Redis 客户端。"""
        self.client.close()
