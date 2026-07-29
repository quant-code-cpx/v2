"""创建跨资产市场身份、场所和新资产扩展表。

Revision ID: 202607290003
Revises: 202607290002
Create Date: 2026-07-29
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import func, select
from sqlalchemy.orm import DeclarativeBase

from service_data_sync.infrastructure.database.models.market import (
    DerivativeContract,
    DerivativeProduct,
    EtfListing,
    FundLegalEntity,
    FundShareClass,
    InstrumentIdentifierVersion,
    InstrumentLifecycleVersion,
    MarketCalendarDay,
    MarketEntity,
    MarketEntityRelationVersion,
    MarketInstrument,
    MarketSessionVersion,
    TradingVenue,
)

# Alembic 使用的版本标识。
revision = "202607290003"
down_revision = "202607290002"
branch_labels = None
depends_on = None

_MODELS: tuple[type[DeclarativeBase], ...] = (
    TradingVenue,
    MarketEntity,
    MarketInstrument,
    InstrumentIdentifierVersion,
    InstrumentLifecycleVersion,
    MarketEntityRelationVersion,
    MarketCalendarDay,
    MarketSessionVersion,
    FundLegalEntity,
    FundShareClass,
    EtfListing,
    DerivativeProduct,
    DerivativeContract,
)


def upgrade() -> None:
    """以 expand-only 方式创建新身份层，并复用既有股票永久 UUID。"""
    bind = op.get_bind()
    # 双时间排斥约束依赖标量 UUID 和文本的 GiST 操作符类。
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
    for model in _MODELS:
        model.__table__.create(bind=bind, checkfirst=False)

    # 旧股票表继续以 security_id 服务大表关联；只补同 UUID 的根和工具映射，不重编号。
    op.execute(
        """
        INSERT INTO market_entity (entity_id, entity_kind, created_at, retired_at)
        SELECT instrument_id, 'EQUITY', created_at, NULL
        FROM equity_instrument
        ON CONFLICT (entity_id) DO NOTHING
        """
    )
    op.execute(
        """
        INSERT INTO market_instrument
          (instrument_id, instrument_kind, primary_venue_id, tradable_from, tradable_to)
        SELECT instrument_id, 'EQUITY', NULL, NULL, NULL
        FROM equity_instrument
        ON CONFLICT (instrument_id) DO NOTHING
        """
    )


def downgrade() -> None:
    """仅在新身份层没有业务状态时回退，防止删除跨资产永久引用。"""
    bind = op.get_bind()
    populated = [
        model.__tablename__
        for model in _MODELS
        if bind.execute(select(func.count()).select_from(model.__table__)).scalar_one() > 0
    ]
    if populated:
        raise RuntimeError(
            "cannot downgrade market identity schema after state exists: " + ", ".join(populated)
        )
    for model in reversed(_MODELS):
        model.__table__.drop(bind=bind, checkfirst=False)
