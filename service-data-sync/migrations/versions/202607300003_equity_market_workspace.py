"""增加股票中心缺失事实与冻结发现横截面。

Revision ID: 202607300003
Revises: 202607300002
Create Date: 2026-07-30
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import func, select

from service_data_sync.infrastructure.database.models.equity.workspace import (
    EquityDiscoveryAvailability,
    EquityDiscoveryMembership,
    EquityDiscoverySnapshot,
    EquityShareCapitalRevision,
    EquityTradingStatusRevision,
    SwMembershipItem,
    SwMembershipRelease,
)

revision = "202607300003"
down_revision = "202607300002"
branch_labels = None
depends_on = None

_TABLES = (
    EquityTradingStatusRevision.__table__,
    EquityShareCapitalRevision.__table__,
    SwMembershipRelease.__table__,
    SwMembershipItem.__table__,
    EquityDiscoverySnapshot.__table__,
    EquityDiscoveryMembership.__table__,
    EquityDiscoveryAvailability.__table__,
)


def upgrade() -> None:
    """按外键依赖顺序创建事实、快照发布和发现投影表。"""
    bind = op.get_bind()
    for table in _TABLES:
        table.create(bind=bind, checkfirst=False)


def downgrade() -> None:
    """仅在所有新增表为空时删除，避免丢失来源修订或消费者已见发布。"""
    bind = op.get_bind()
    populated = [
        table.name
        for table in _TABLES
        if bind.execute(select(func.count()).select_from(table)).scalar_one() > 0
    ]
    if populated:
        raise RuntimeError(
            "cannot downgrade equity market workspace after data exists: "
            + ",".join(populated)
        )
    for table in reversed(_TABLES):
        table.drop(bind=bind, checkfirst=False)
