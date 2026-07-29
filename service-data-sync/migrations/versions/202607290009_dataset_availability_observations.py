# ruff: noqa: E501

"""创建不产生 `canonical` 事实的同步可用性观测表。

该表让应用层保存一次同步得到合法空集、来源不可用或尚未完成的诊断结论，避免将这些状态
伪装成零值事实。它与真实业务事实分离，因此不会为“无数据”构造虚假的市场、财务等业务行。

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
    """增加空集和来源不可用的诊断元数据表，不改动既有 `canonical` 事实表。"""
    DatasetAvailabilityObservation.__table__.create(bind=op.get_bind(), checkfirst=False)


def downgrade() -> None:
    """仅当尚未记录可用性历史时才允许删除本表。

    一旦已有观测，回退会丢失“空集”与“来源不可用”等诊断证据，因而显式拒绝删除。
    """
    bind = op.get_bind()
    count = bind.execute(
        select(func.count()).select_from(DatasetAvailabilityObservation.__table__)
    ).scalar_one()
    if count > 0:
        raise RuntimeError("cannot downgrade dataset availability observations after state exists")
    DatasetAvailabilityObservation.__table__.drop(bind=bind, checkfirst=False)
