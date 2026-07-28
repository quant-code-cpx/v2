"""Alembic 迁移环境：从安全配置读取连接并执行显式 SQL 迁移。"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from service_data_sync.bootstrap.settings import load_settings
from service_data_sync.infrastructure.database.models.registry import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 模型表达当前目标 schema；历史迁移仍使用显式 SQL 管理 PostgreSQL 分区与特殊约束。
target_metadata = Base.metadata
_MANAGED_TABLE_NAMES = frozenset(target_metadata.tables)
_PARTITION_PROPAGATED_FOREIGN_KEYS = frozenset(
    {
        ("financial_statement_fact", frozenset({"report_period", "revision_id"})),
        (
            "financial_derivation_input",
            frozenset({"derived_report_period", "derived_metric_revision_id"}),
        ),
        (
            "financial_derivation_input",
            frozenset({"input_report_period", "input_revision_id"}),
        ),
        (
            "money_flow_ranking_item",
            frozenset({"target_trade_date", "snapshot_id"}),
        ),
        (
            "money_flow_ranking_manifest",
            frozenset({"target_trade_date", "snapshot_id"}),
        ),
        (
            "money_flow_ranking_metric",
            frozenset({"target_trade_date", "snapshot_id", "supplier_position"}),
        ),
    }
)


def _include_object(
    object_: object,
    name: str | None,
    type_: str,
    reflected: bool,
    compare_to: object | None,
) -> bool:
    """仅比较服务逻辑表，排除迁移专属表和运行时物理分区。"""
    del compare_to
    if type_ == "table":
        return name in _MANAGED_TABLE_NAMES

    table = getattr(object_, "table", None)
    if type_ == "foreign_key_constraint" and table is not None:
        constrained_columns = frozenset(
            element.parent.name for element in getattr(object_, "elements", ())
        )
        if (table.name, constrained_columns) in _PARTITION_PROPAGATED_FOREIGN_KEYS:
            # PostgreSQL 会把指向分区父表的复合外键反射为每个物理子表各一条。
            # 这些约束由 integration schema parity 逐项断言，避免 autogenerate 误报删除。
            return False

    del reflected
    return table is None or table.name in _MANAGED_TABLE_NAMES


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
        include_object=_include_object,
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
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=_include_object,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
