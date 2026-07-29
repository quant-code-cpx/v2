"""创建指数 P0-A 目录、成分和权重观察表。

Revision ID: 202607290002
Revises: 202607290001
Create Date: 2026-07-29
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import func, select
from sqlalchemy.orm import DeclarativeBase

from service_data_sync.infrastructure.database.models.index import (
    IndexCatalogObservation,
    IndexCatalogObservationItem,
    IndexDefinition,
    IndexObservedSnapshot,
    IndexObservedSnapshotItem,
)

# Alembic 使用的版本标识。
revision = "202607290002"
down_revision = "202607290001"
branch_labels = None
depends_on = None

_MODELS: tuple[type[DeclarativeBase], ...] = (
    IndexDefinition,
    IndexCatalogObservation,
    IndexCatalogObservationItem,
    IndexObservedSnapshot,
    IndexObservedSnapshotItem,
)


def upgrade() -> None:
    """创建仅保存观察事实的指数 P0-A 表，不创建 PIT、事件或业务发布表。"""
    bind = op.get_bind()
    for model in _MODELS:
        model.__table__.create(bind=bind, checkfirst=False)


def downgrade() -> None:
    """仅在影子观察表为空时删除，防止丢失连续探测和原始证据索引。"""
    bind = op.get_bind()
    populated = [
        model.__tablename__
        for model in _MODELS
        if bind.execute(select(func.count()).select_from(model.__table__)).scalar_one() > 0
    ]
    if populated:
        raise RuntimeError(
            "cannot downgrade index shadow observation schema after state exists: "
            + ", ".join(populated)
        )
    for model in reversed(_MODELS):
        model.__table__.drop(bind=bind, checkfirst=False)
