"""创建不完整资金流排行的私有研究观察 schema。

AKShare 当前排行接口只能返回 SDK 合并后的页面，不能证明分页连续或上游总数。迁移将
这类真实来源观察与正式 `money_flow_ranking_snapshot` 隔离：研究行完整保留来源位置、
标准载荷摘要和度量，但没有可供公开消费者读取的 `publication` 路径。

Revision ID: 202607300021
Revises: 202607300020
Create Date: 2026-08-01
"""

from __future__ import annotations

from datetime import date

from alembic import op
from sqlalchemy import func, select
from sqlalchemy.orm import DeclarativeBase

from service_data_sync.infrastructure.database.models.money_flow import (
    MoneyFlowRankingResearchItem,
    MoneyFlowRankingResearchMetric,
    MoneyFlowRankingResearchObservation,
)

# Alembic 使用的版本标识。
revision = "202607300021"
down_revision = "202607300020"
branch_labels = None
depends_on = None

_MODELS: tuple[type[DeclarativeBase], ...] = (
    MoneyFlowRankingResearchObservation,
    MoneyFlowRankingResearchItem,
    MoneyFlowRankingResearchMetric,
)
_PARTITIONED_TABLE = "money_flow_ranking_research_observation"


def upgrade() -> None:
    """按外键依赖创建研究观察、来源位置和分桶度量及近期月分区。"""
    bind = op.get_bind()
    for model in _MODELS:
        model.__table__.create(bind=bind, checkfirst=False)
    for year in (2025, 2026, 2027):
        for month in range(1, 13):
            start = date(year, month, 1)
            end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
            _create_month_partition(start=start, end=end)
    op.execute(
        f"CREATE TABLE {_PARTITIONED_TABLE}_default PARTITION OF {_PARTITIONED_TABLE} DEFAULT"
    )


def downgrade() -> None:
    """仅在研究观察、位置和度量都为空时逆序删除，避免丢失真实来源证据。"""
    bind = op.get_bind()
    populated = [
        model.__tablename__
        for model in _MODELS
        if bind.execute(select(func.count()).select_from(model.__table__)).scalar_one() > 0
    ]
    if populated:
        raise RuntimeError(
            "cannot downgrade money-flow ranking research schema after state exists: "
            + ", ".join(populated)
        )
    for model in reversed(_MODELS):
        model.__table__.drop(bind=bind, checkfirst=False)


def _create_month_partition(*, start: date, end: date) -> None:
    """为按目标交易日分区的研究观察父表创建确定性月度物理分区。"""
    suffix = start.strftime("%Y%m")
    op.execute(
        f"CREATE TABLE {_PARTITIONED_TABLE}_{suffix} PARTITION OF {_PARTITIONED_TABLE} "
        f"FOR VALUES FROM ('{start.isoformat()}') TO ('{end.isoformat()}')"
    )
