"""创建融资融券与沪深港通强类型表。

Revision ID: 202607290005
Revises: 202607290004
Create Date: 2026-07-29
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import func, select
from sqlalchemy.orm import DeclarativeBase

from service_data_sync.infrastructure.database.models.market import (
    MarginEligibilityRevision,
    MarginMarketDailyRevision,
    MarginSecurityDailyRevision,
    MarginSystemRiskDailyRevision,
    StockConnectActiveSecurityRevision,
    StockConnectChannelDailyRevision,
    StockConnectDisclosureRegime,
    StockConnectHoldingItem,
    StockConnectHoldingSnapshot,
)

# Alembic 使用的版本标识。
revision = "202607290005"
down_revision = "202607290004"
branch_labels = None
depends_on = None

_MODELS: tuple[type[DeclarativeBase], ...] = (
    MarginMarketDailyRevision,
    MarginSecurityDailyRevision,
    MarginEligibilityRevision,
    MarginSystemRiskDailyRevision,
    StockConnectDisclosureRegime,
    StockConnectChannelDailyRevision,
    StockConnectActiveSecurityRevision,
    StockConnectHoldingSnapshot,
    StockConnectHoldingItem,
)
_PARTITIONED_TABLES = (
    "margin_market_daily_revision",
    "margin_security_daily_revision",
    "margin_system_risk_daily_revision",
    "stock_connect_channel_daily_revision",
    "stock_connect_active_security_revision",
    "stock_connect_holding_snapshot",
    "stock_connect_holding_item",
)


def upgrade() -> None:
    """创建两融和互联互通表，并预建相邻年度与默认分区。"""
    bind = op.get_bind()
    for model in _MODELS:
        model.__table__.create(bind=bind, checkfirst=False)
    for table_name in _PARTITIONED_TABLES:
        for year in (2025, 2026, 2027):
            _create_year_partition(table_name, year)
        op.execute(f"CREATE TABLE {table_name}_default PARTITION OF {table_name} DEFAULT")


def downgrade() -> None:
    """仅在没有两融或互联互通状态时回退，避免删除不可变发布事实。"""
    bind = op.get_bind()
    populated = [
        model.__tablename__
        for model in _MODELS
        if bind.execute(select(func.count()).select_from(model.__table__)).scalar_one() > 0
    ]
    if populated:
        raise RuntimeError(
            "cannot downgrade margin and stock-connect schema after state exists: "
            + ", ".join(populated)
        )
    for table_name in reversed(_PARTITIONED_TABLES):
        op.execute(f"DROP TABLE {table_name}_default")
        for year in (2027, 2026, 2025):
            op.execute(f"DROP TABLE {table_name}_{year}")
    for model in reversed(_MODELS):
        model.__table__.drop(bind=bind, checkfirst=False)


def _create_year_partition(table_name: str, year: int) -> None:
    """为一个交易日或快照日期父表创建确定性年度物理分区。"""
    op.execute(
        f"CREATE TABLE {table_name}_{year} PARTITION OF {table_name} "
        f"FOR VALUES FROM ('{year}-01-01') TO ('{year + 1}-01-01')"
    )
