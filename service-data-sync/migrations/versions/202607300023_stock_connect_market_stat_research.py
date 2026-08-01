"""创建 AKShare 沪深港通市场统计的私有 research 观察 schema。

AKShare 东财接口可提供来源报告的通道市场统计，但它不满足既有官方 HKEX 完整包的交付、终态、
制度与许可门槛。本迁移将其 source batch、digest-only manifest、规范化、质量和单日观察固定在
独立 research 表中；不修改官方港通表，不创建 `DatasetRelease`、`DatasetPublication` 或 PIT。
回退前会拒绝删除任何已保存的 research 观察，避免丢失真实来源证据。

Revision ID: 202607300023
Revises: 202607300022
Create Date: 2026-08-01
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import func, select
from sqlalchemy.orm import DeclarativeBase

from service_data_sync.infrastructure.database.models.market import (
    StockConnectMarketStatResearchBatch,
    StockConnectMarketStatResearchObservation,
)

# Alembic 使用的版本标识。
revision = "202607300023"
down_revision = "202607300022"
branch_labels = None
depends_on = None

_MODELS: tuple[type[DeclarativeBase], ...] = (
    StockConnectMarketStatResearchBatch,
    StockConnectMarketStatResearchObservation,
)


def upgrade() -> None:
    """按来源批次到单日观察的外键顺序创建 research-only 表，不触及官方港通实体。"""
    bind = op.get_bind()
    for model in _MODELS:
        model.__table__.create(bind=bind, checkfirst=False)


def downgrade() -> None:
    """仅当两张 research 表为空时逆序删除，避免回退抹去真实 AKShare 观察与血缘。"""
    bind = op.get_bind()
    populated = [
        model.__tablename__
        for model in _MODELS
        if bind.execute(select(func.count()).select_from(model.__table__)).scalar_one() > 0
    ]
    if populated:
        raise RuntimeError(
            "cannot downgrade stock-connect market-stat research schema after state exists: "
            + ", ".join(populated)
        )
    for model in reversed(_MODELS):
        model.__table__.drop(bind=bind, checkfirst=False)
