"""创建主营构成、公司事件与交易公开信息强类型表。

Revision ID: 202607290006
Revises: 202607290005
Create Date: 2026-07-29
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import func, select
from sqlalchemy.orm import DeclarativeBase

from service_data_sync.infrastructure.database.models.market import (
    BlockTradeExecutionRevision,
    BusinessCompositionLabelVersion,
    BusinessCompositionLine,
    BusinessCompositionReportRevision,
    CorporateEarningsValue,
    CorporateEvent,
    CorporateEventRevision,
    DisclosureDocument,
    DisclosureDocumentRelation,
    DragonTigerEventRevision,
    DragonTigerSeatItem,
    RestrictedUnlockLot,
    ShareCapitalComponent,
    ShareholderHoldingAction,
    TradingDisclosureReasonMapVersion,
)

# Alembic 使用的版本标识。
revision = "202607290006"
down_revision = "202607290005"
branch_labels = None
depends_on = None

_MODELS: tuple[type[DeclarativeBase], ...] = (
    DisclosureDocument,
    DisclosureDocumentRelation,
    BusinessCompositionReportRevision,
    BusinessCompositionLine,
    BusinessCompositionLabelVersion,
    CorporateEvent,
    CorporateEventRevision,
    CorporateEarningsValue,
    RestrictedUnlockLot,
    ShareCapitalComponent,
    ShareholderHoldingAction,
    TradingDisclosureReasonMapVersion,
    DragonTigerEventRevision,
    DragonTigerSeatItem,
    BlockTradeExecutionRevision,
)
_PARTITIONED_TABLES = (
    "business_composition_report_revision",
    "business_composition_line",
    "dragon_tiger_event_revision",
    "dragon_tiger_seat_item",
    "block_trade_execution_revision",
)


def upgrade() -> None:
    """创建事件和交易事实表，并预建相邻年度与默认分区。"""
    bind = op.get_bind()
    for model in _MODELS:
        model.__table__.create(bind=bind, checkfirst=False)
    for table_name in _PARTITIONED_TABLES:
        for year in (2025, 2026, 2027):
            _create_year_partition(table_name, year)
        op.execute(f"CREATE TABLE {table_name}_default PARTITION OF {table_name} DEFAULT")


def downgrade() -> None:
    """仅在没有不可变事件事实时回退，避免误删已发布历史。"""
    bind = op.get_bind()
    populated = [
        model.__tablename__
        for model in _MODELS
        if bind.execute(select(func.count()).select_from(model.__table__)).scalar_one() > 0
    ]
    if populated:
        raise RuntimeError(
            "cannot downgrade equity event and trading schema after state exists: "
            + ", ".join(populated)
        )
    for table_name in reversed(_PARTITIONED_TABLES):
        op.execute(f"DROP TABLE {table_name}_default")
        for year in (2027, 2026, 2025):
            op.execute(f"DROP TABLE {table_name}_{year}")
    for model in reversed(_MODELS):
        model.__table__.drop(bind=bind, checkfirst=False)


def _create_year_partition(table_name: str, year: int) -> None:
    """为按报告期或交易日分区的父表创建确定性年度分区。"""
    op.execute(
        f"CREATE TABLE {table_name}_{year} PARTITION OF {table_name} "
        f"FOR VALUES FROM ('{year}-01-01') TO ('{year + 1}-01-01')"
    )
