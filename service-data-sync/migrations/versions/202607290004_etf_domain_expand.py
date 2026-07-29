"""创建 ETF 强类型 revision 与关系表。

Revision ID: 202607290004
Revises: 202607290003
Create Date: 2026-07-29
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import func, select
from sqlalchemy.orm import DeclarativeBase

from service_data_sync.infrastructure.database.models.etf import (
    EtfActionVersion,
    EtfDailyBarRevision,
    EtfNavRevision,
    EtfPremiumRevision,
    EtfProfileVersion,
    EtfShareRevision,
    EtfStatusRevision,
    EtfTrackingRelationVersion,
)

# Alembic 使用的版本标识。
revision = "202607290004"
down_revision = "202607290003"
branch_labels = None
depends_on = None

_MODELS: tuple[type[DeclarativeBase], ...] = (
    EtfProfileVersion,
    EtfTrackingRelationVersion,
    EtfDailyBarRevision,
    EtfNavRevision,
    EtfShareRevision,
    EtfStatusRevision,
    EtfActionVersion,
    EtfPremiumRevision,
)
_PARTITIONED_TABLES = (
    "etf_daily_bar_revision",
    "etf_nav_revision",
    "etf_share_revision",
    "etf_premium_revision",
)


def upgrade() -> None:
    """创建 ETF 域表和当前相邻年度分区，默认分区保证受控回补不会写失败。"""
    bind = op.get_bind()
    for model in _MODELS:
        model.__table__.create(bind=bind, checkfirst=False)
    for table_name in _PARTITIONED_TABLES:
        _create_year_partition(table_name, 2025)
        _create_year_partition(table_name, 2026)
        _create_year_partition(table_name, 2027)
        op.execute(f"CREATE TABLE {table_name}_default PARTITION OF {table_name} DEFAULT")


def downgrade() -> None:
    """仅在 ETF 域表均为空时回退，避免删除已冻结的 release 事实。"""
    bind = op.get_bind()
    populated = [
        model.__tablename__
        for model in _MODELS
        if bind.execute(select(func.count()).select_from(model.__table__)).scalar_one() > 0
    ]
    if populated:
        raise RuntimeError(
            "cannot downgrade ETF schema after state exists: " + ", ".join(populated)
        )
    for table_name in _PARTITIONED_TABLES:
        for year in (2025, 2026, 2027):
            op.execute(f"DROP TABLE {table_name}_{year}")
        op.execute(f"DROP TABLE {table_name}_default")
    for model in reversed(_MODELS):
        model.__table__.drop(bind=bind, checkfirst=False)


def _create_year_partition(table_name: str, year: int) -> None:
    """为固定年度创建一个日期范围子分区，父表约束会自动下推。"""
    op.execute(
        f"CREATE TABLE {table_name}_{year} PARTITION OF {table_name} "
        f"FOR VALUES FROM ('{year}-01-01') TO ('{year + 1}-01-01')"
    )
