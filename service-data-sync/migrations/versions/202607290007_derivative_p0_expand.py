"""创建衍生品 P0 真实合约规格与日行情表。

Revision ID: 202607290007
Revises: 202607290006
Create Date: 2026-07-29
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import func, select
from sqlalchemy.orm import DeclarativeBase

from service_data_sync.infrastructure.database.models.market import (
    DerivativeContractRevision,
    DerivativeDailyBarRevision,
)

# Alembic 使用的版本标识。
revision = "202607290007"
down_revision = "202607290006"
branch_labels = None
depends_on = None

_MODELS: tuple[type[DeclarativeBase], ...] = (
    DerivativeContractRevision,
    DerivativeDailyBarRevision,
)


def upgrade() -> None:
    """创建真实合约规格和日行情表，并预建相邻年度与默认分区。"""
    bind = op.get_bind()
    DerivativeContractRevision.__table__.create(bind=bind, checkfirst=False)
    DerivativeDailyBarRevision.__table__.create(bind=bind, checkfirst=False)
    for year in (2025, 2026, 2027):
        _create_year_partition(year)
    op.execute(
        "CREATE TABLE derivative_daily_bar_revision_default "
        "PARTITION OF derivative_daily_bar_revision DEFAULT"
    )


def downgrade() -> None:
    """仅在没有衍生品 P0 事实时回退，防止误删真实合约历史。"""
    bind = op.get_bind()
    populated = [
        model.__tablename__
        for model in _MODELS
        if bind.execute(select(func.count()).select_from(model.__table__)).scalar_one() > 0
    ]
    if populated:
        raise RuntimeError(
            "cannot downgrade derivative P0 schema after state exists: " + ", ".join(populated)
        )
    op.execute("DROP TABLE derivative_daily_bar_revision_default")
    for year in (2027, 2026, 2025):
        op.execute(f"DROP TABLE derivative_daily_bar_revision_{year}")
    DerivativeDailyBarRevision.__table__.drop(bind=bind, checkfirst=False)
    DerivativeContractRevision.__table__.drop(bind=bind, checkfirst=False)


def _create_year_partition(year: int) -> None:
    """为真实合约日行情创建由交易所业务日路由的年度分区。"""
    op.execute(
        "CREATE TABLE derivative_daily_bar_revision_"
        f"{year} PARTITION OF derivative_daily_bar_revision "
        f"FOR VALUES FROM ('{year}-01-01') TO ('{year + 1}-01-01')"
    )
