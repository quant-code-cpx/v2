"""数据运维控制面 dispatcher、reaper 和数据库计划 tick 的 Celery 适配器。

消息只用于唤醒。任务每次启动都重建 PostgreSQL 控制面，并由 `ExecutionSlot` 行锁、租约和
fencing token 决定是否真的执行；并发 worker、重复消息或 Redis 重投不能造成并行同步。
"""

from __future__ import annotations

import socket
from uuid import uuid4

from celery import Celery

from service_data_sync.bootstrap.container import build_container
from service_data_sync.bootstrap.settings import Settings
from service_data_sync.infrastructure.data_operations.canonical_executors import (
    register_canonical_executors,
)
from service_data_sync.infrastructure.data_operations.control_plane import (
    DataOperationsControlPlane,
    build_catalog,
)


def register_data_operations_tasks(app: Celery, *, settings: Settings) -> None:
    """注册统一 command dispatcher、lease reaper 和持久化 schedule tick 任务。"""

    @app.task(name="service_data_sync.data_operations.dispatch")
    def dispatch() -> None:
        """尝试执行一个已入账 run；没有可用 slot 或队列时安全返回。"""
        container = build_container(settings)
        try:
            control_plane = DataOperationsControlPlane(
                database=container.database,
                catalog=build_catalog(settings, container.source_registry),
                source_registry=container.source_registry,
                trading_calendar=container.trading_calendar,
                etf_auto_retry_max_attempts=settings.etf_auto_retry_max_attempts,
            )
            register_canonical_executors(control_plane, container)
            control_plane.dispatch_once(f"{socket.gethostname()}:{uuid4()}")
        finally:
            container.close()

    @app.task(name="service_data_sync.data_operations.reap")
    def reap() -> None:
        """回收数据库中过期的全局 slot lease，不访问 Provider。"""
        container = build_container(settings)
        try:
            control_plane = DataOperationsControlPlane(
                database=container.database,
                catalog=build_catalog(settings, container.source_registry),
                source_registry=container.source_registry,
                trading_calendar=container.trading_calendar,
            )
            control_plane.reap_expired_slots()
        finally:
            container.close()

    @app.task(name="service_data_sync.data_operations.health_dispatch")
    def health_dispatch() -> None:
        """执行一个主动健康检查 target，不占用同步全局执行槽。"""
        container = build_container(settings)
        try:
            control_plane = DataOperationsControlPlane(
                database=container.database,
                catalog=build_catalog(settings, container.source_registry),
                source_registry=container.source_registry,
                trading_calendar=container.trading_calendar,
            )
            control_plane.dispatch_health_check_once()
        finally:
            container.close()

    @app.task(name="service_data_sync.data_operations.scheduler_tick")
    def scheduler_tick() -> None:
        """只把到期数据库计划写入同一 command 队列，不直接抓取或发布数据。"""
        container = build_container(settings)
        try:
            control_plane = DataOperationsControlPlane(
                database=container.database,
                catalog=build_catalog(settings, container.source_registry),
                source_registry=container.source_registry,
                trading_calendar=container.trading_calendar,
            )
            control_plane.scheduler_tick()
        finally:
            container.close()
