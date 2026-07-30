"""管理服务进程拥有的 PostgreSQL 连接池、短生命周期会话与事务边界。

仓储只通过本模块取得 `Session`，不自行创建 `Engine`、跨任务复用会话或猜测提交时机。
`transaction` 正常退出才提交，任意异常都会回滚本次数据库写入并关闭会话，因此一次
发布、检查点推进和相关审计记录可以由调用方放在同一原子边界内。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

from sqlalchemy import Engine, create_engine, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from service_data_sync.bootstrap.errors import DependencyUnavailable
from service_data_sync.bootstrap.settings import Settings
from service_data_sync.infrastructure.database.fenced_execution import current_fenced_execution


@dataclass
class DatabaseClient:
    """封装进程级 `Engine` 与每次仓储操作独享的短生命周期 `Session`。

    `Engine` 只负责连接池，不能承载业务事务状态；实际查询和写入必须发生在新建的
    `Session` 中。这样 Celery worker、CLI 命令和并发同步分区不会意外共享未提交数据、
    身份映射缓存或连接生命周期。上层使用 `transaction` 时不应再手工提交或回滚同一会话。
    """

    # `Engine` 由服务进程共享，关闭进程或容器时统一释放其空闲和在借连接。
    engine: Engine
    # 工厂不暴露给仓储，避免绕过本类约定的短生命周期会话策略。
    _session_factory: sessionmaker[Session] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """创建仓储专用会话工厂，明确写入何时送库、何时失效。

        `autoflush=False` 让仓储能在预期位置执行 SQL，而不是在中途查询时意外写入；
        `expire_on_commit=False` 保留刚发布对象的字段，避免提交后为了读取摘要再访问数据库。
        这不改变事务语义，提交仍只由 `transaction` 的正常退出触发。
        """
        self._session_factory = sessionmaker(
            bind=self.engine, autoflush=False, expire_on_commit=False
        )

    @classmethod
    def from_settings(cls, settings: Settings) -> DatabaseClient:
        """由已校验的私密连接 URL 创建 PostgreSQL 引擎及连接池策略。

        五秒连接超时让启动和诊断快速暴露不可用依赖；`pool_pre_ping` 会在借出连接前
        淘汰失效连接，`pool_recycle` 则避免长期空闲连接被基础设施回收后仍被复用。
        本方法只创建本地对象，不会在此时建立连接或执行迁移。
        """
        return cls(
            engine=create_engine(
                settings.database_url.get_secret_value(),
                connect_args={"connect_timeout": 5},
                pool_pre_ping=True,
                pool_recycle=1800,
            )
        )

    def ping(self) -> None:
        """执行无业务副作用的最小查询，将驱动故障转换为统一依赖错误。

        连接失败、认证失败和数据库暂不可用都会保留原始异常链，但对启动诊断暴露稳定的
        `DependencyUnavailable` 语义；本方法不创建表、不读取业务数据，也不打开事务。
        """
        try:
            with self.engine.connect() as connection:
                connection.execute(select(1))
        except SQLAlchemyError as error:
            raise DependencyUnavailable("postgres", "ping") from error

    def session(self) -> Session:
        """创建调用方负责关闭的独立 `Session`，不得跨任务、线程或请求共享。

        此接口适合只读查询或调用方已有更大事务边界的场景；调用方必须使用上下文管理器
        或显式关闭它，否则连接池中的连接可能长期被占用。
        """
        return self._session_factory()

    @contextmanager
    def transaction(self) -> Iterator[Session]:
        """提供一次仓储操作的原子提交/回滚边界，并在结束后归还连接。

        `yield` 之前开始事务；调用方代码无异常返回时 `session.begin()` 提交全部变更，
        包括事实、来源血缘、质量结果和 publication。任何异常则由 SQLAlchemy 回滚，避免
        半完成发布或已推进但未落库的 checkpoint；随后外层上下文关闭会话。该方法不吞掉异常，
        以便任务层决定重试、隔离或终止。异常边界覆盖事务提交本身；若 fenced 终态已在
        提交前写入内存但数据库提交失败，必须同步撤销该内存标记。
        """
        with self.session() as session:
            execution = current_fenced_execution()
            terminal_written_before = (
                execution is not None
                and execution.database is self
                and execution.terminal_written
            )
            try:
                # `try` 必须包住整个事务上下文，才能同时捕获业务体、finalizer 和 commit 失败。
                with session.begin():
                    if (
                        execution is not None
                        and execution.database is self
                        and not execution.terminal_written
                    ):
                        # canonical 写入前锁住 ExecutionSlot，旧 worker 不能跨过此门。
                        execution.assert_current(session)
                    yield session
                    if (
                        execution is not None
                        and execution.database is self
                        and not execution.terminal_written
                    ):
                        # 被执行器显式武装时，run 终态与 canonical 写入共用这一提交事务。
                        execution.finalize_if_armed(session)
            except BaseException:
                if (
                    execution is not None
                    and execution.database is self
                    and not terminal_written_before
                ):
                    # SQL 已回滚，提交前写入的 armed/written 内存状态也必须回滚。
                    execution.rollback_terminal_write()
                raise

    def close(self) -> None:
        """在进程退出时释放连接池；已借出的会话应先由其调用方结束。"""
        self.engine.dispose()
