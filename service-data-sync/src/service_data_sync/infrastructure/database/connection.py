"""PostgreSQL 连接池的创建、连通性探测与释放。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

from sqlalchemy import Engine, create_engine, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from service_data_sync.bootstrap.errors import DependencyUnavailable
from service_data_sync.bootstrap.settings import Settings


@dataclass
class DatabaseClient:
    """封装服务拥有的 Engine 与短生命周期 `Session`，避免上层管理连接池或事务。"""

    engine: Engine
    _session_factory: sessionmaker[Session] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """以关闭 autoflush、保留已提交字段的策略创建仓储专用 Session 工厂。"""
        self._session_factory = sessionmaker(
            bind=self.engine, autoflush=False, expire_on_commit=False
        )

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
                connection.execute(select(1))
        except SQLAlchemyError as error:
            raise DependencyUnavailable("postgres", "ping") from error

    def session(self) -> Session:
        """创建一个调用方负责关闭的短生命周期 Session，不跨任务或线程共享。"""
        return self._session_factory()

    @contextmanager
    def transaction(self) -> Iterator[Session]:
        """提供提交或异常自动回滚的最小事务边界，并在结束后释放 Session。"""
        with self.session() as session:
            with session.begin():
                yield session

    def close(self) -> None:
        """在关闭时释放全部数据库引擎连接池。"""
        self.engine.dispose()
