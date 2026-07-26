"""PostgreSQL 连接池的创建、连通性探测与释放。"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from service_data_sync.bootstrap.errors import DependencyUnavailable
from service_data_sync.bootstrap.settings import Settings


@dataclass
class DatabaseClient:
    """封装服务拥有的 SQLAlchemy 引擎，避免上层直接管理连接池。"""

    engine: Engine

    @classmethod
    def from_settings(cls, settings: Settings) -> DatabaseClient:
        """根据已校验的私密连接 URL 创建具备探活能力的 PostgreSQL 引擎。"""
        return cls(
            engine=create_engine(
                settings.database_url.get_secret_value(),
                connect_args={"connect_timeout": 5},
                pool_pre_ping=True,
                pool_recycle=1800,
            )
        )

    def ping(self) -> None:
        """执行最小查询，并将 SQL 驱动故障转换为领域错误。"""
        try:
            with self.engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        except SQLAlchemyError as error:
            raise DependencyUnavailable("postgres", "ping") from error

    def close(self) -> None:
        """在关闭时释放全部数据库引擎连接池。"""
        self.engine.dispose()
