# ruff: noqa: E501

"""创建不产生 canonical 事实的同步可用性观测表。

Revision ID: 202607290009
Revises: 202607290008
Create Date: 2026-07-29
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import func, select

from service_data_sync.infrastructure.database.models.publication.dataset_availability_observation import (
    DatasetAvailabilityObservation,
)

# Alembic 使用的版本标识。
revision = "202607290009"
down_revision = "202607290008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """增加空集和来源不可用的元数据表，不改动既有事实表。"""
    DatasetAvailabilityObservation.__table__.create(bind=op.get_bind(), checkfirst=False)


def downgrade() -> None:
    """仅当尚未记录可用性历史时才允许删除本表。"""
    bind = op.get_bind()
    count = bind.execute(
        select(func.count()).select_from(DatasetAvailabilityObservation.__table__)
    ).scalar_one()
    if count > 0:
        raise RuntimeError("cannot downgrade dataset availability observations after state exists")
    DatasetAvailabilityObservation.__table__.drop(bind=bind, checkfirst=False)
