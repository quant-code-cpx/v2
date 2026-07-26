"""Alembic 迁移环境：从安全配置读取连接并执行显式 SQL 迁移。"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from service_data_sync.bootstrap.settings import load_settings

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 分区和当前行索引依赖 PostgreSQL 特性，因此迁移使用显式 SQL。
target_metadata = None


def _database_url() -> str:
    """从已校验配置读取迁移数据库 URL，且不记录其中的密钥。"""
    return load_settings().database_url.get_secret_value()


def run_migrations_offline() -> None:
    """生成 SQL 迁移语句，不打开数据库连接。"""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """通过短生命周期、无连接池的 SQLAlchemy 连接执行迁移。"""
    configuration = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = _database_url()
    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
