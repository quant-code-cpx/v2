"""创建并管理 `Celery` 消息代理所用的 `Redis` 客户端。

`Redis` 在此服务中负责消息投递基础设施，不保存 `canonical` 市场数据。本模块把短超时
和驱动异常转换为组合根可识别的依赖故障，调用业务代码无需理解 `Redis SDK`。
"""

from __future__ import annotations

from dataclasses import dataclass

import redis
from redis.exceptions import RedisError

from service_data_sync.bootstrap.errors import DependencyUnavailable
from service_data_sync.bootstrap.settings import Settings


@dataclass
class RedisClient:
    """封装服务拥有的 `Redis` 客户端，统一超时与依赖错误转换。

    客户端只由容器生命周期持有，任务函数不应自行创建长连接或读取消息代理 `URL`。
    """

    client: redis.Redis

    @classmethod
    def from_settings(cls, settings: Settings) -> RedisClient:
        """根据已校验 broker URL 创建受超时约束的 Redis 客户端。"""
        return cls(
            client=redis.Redis.from_url(
                settings.broker_url.get_secret_value(),
                # 消息代理故障应尽快暴露给健康检查和工作进程，不应静默阻塞业务进程。
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
        """在容器关闭时关闭 `Redis` 客户端，释放连接池中的网络连接。"""
        self.client.close()
