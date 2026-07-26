from __future__ import annotations

from dataclasses import dataclass

import redis
from redis.exceptions import RedisError

from service_data_sync.bootstrap.errors import DependencyUnavailable
from service_data_sync.bootstrap.settings import Settings


@dataclass
class RedisClient:
    client: redis.Redis

    @classmethod
    def from_settings(cls, settings: Settings) -> RedisClient:
        """Create timeout-bounded Redis client from validated broker URL."""
        return cls(
            client=redis.Redis.from_url(
                settings.broker_url.get_secret_value(),
                socket_connect_timeout=5,
                socket_timeout=5,
            )
        )

    def ping(self) -> None:
        """Check Redis reachability and translate driver errors to domain error."""
        try:
            self.client.ping()
        except RedisError as error:
            raise DependencyUnavailable("redis", "ping") from error

    def close(self) -> None:
        """Close Redis client during container shutdown."""
        self.client.close()
