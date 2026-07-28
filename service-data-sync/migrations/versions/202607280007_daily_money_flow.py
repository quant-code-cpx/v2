"""创建日频资金流方法学、双时间序列、排行与质量 schema。

Revision ID: 202607280007
Revises: 202607280006
Create Date: 2026-07-28
"""

from __future__ import annotations

from datetime import date

from alembic import op
from sqlalchemy import func, select
from sqlalchemy.orm import DeclarativeBase

from service_data_sync.infrastructure.database.models.money_flow import (
    MoneyFlowBucketDefinition,
    MoneyFlowDailyObservation,
    MoneyFlowMethodology,
    MoneyFlowMethodologyScope,
    MoneyFlowMethodologyVersion,
    MoneyFlowMethodologyWindow,
    MoneyFlowQualityResult,
    MoneyFlowRankingItem,
    MoneyFlowRankingManifest,
    MoneyFlowRankingMetric,
    MoneyFlowRankingSnapshot,
    MoneyFlowSeries,
    MoneyFlowUniverseVersion,
)

# Alembic 使用的版本标识。
revision = "202607280007"
down_revision = "202607280006"
branch_labels = None
depends_on = None

_MODELS: tuple[type[DeclarativeBase], ...] = (
    MoneyFlowMethodology,
    MoneyFlowMethodologyVersion,
    MoneyFlowMethodologyScope,
    MoneyFlowMethodologyWindow,
    MoneyFlowBucketDefinition,
    MoneyFlowUniverseVersion,
    MoneyFlowSeries,
    MoneyFlowDailyObservation,
    MoneyFlowRankingSnapshot,
    MoneyFlowRankingItem,
    MoneyFlowRankingMetric,
    MoneyFlowRankingManifest,
    MoneyFlowQualityResult,
)
_PARTITIONED_TABLES = (
    "money_flow_daily_observation",
    "money_flow_ranking_snapshot",
    "money_flow_ranking_item",
    "money_flow_ranking_metric",
    "money_flow_ranking_manifest",
)


def upgrade() -> None:
    """按依赖顺序创建 13 张逻辑表及近期、当前和下一年度月分区。"""
    bind = op.get_bind()
    for model in _MODELS:
        model.__table__.create(bind=bind, checkfirst=False)
    for year in (2025, 2026, 2027):
        for month in range(1, 13):
            start = date(year, month, 1)
            end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
            for table_name in _PARTITIONED_TABLES:
                _create_month_partition(table_name, start=start, end=end)
    for table_name in _PARTITIONED_TABLES:
        op.execute(f"CREATE TABLE {table_name}_default PARTITION OF {table_name} DEFAULT")


def downgrade() -> None:
    """仅在全部资金流表为空时逆序删除，避免丢失 raw 血缘和 canonical 修订。"""
    bind = op.get_bind()
    populated = [
        model.__tablename__
        for model in _MODELS
        if bind.execute(select(func.count()).select_from(model.__table__)).scalar_one() > 0
    ]
    if populated:
        raise RuntimeError(
            "cannot downgrade daily money-flow schema after state exists: " + ", ".join(populated)
        )
    for model in reversed(_MODELS):
        model.__table__.drop(bind=bind, checkfirst=False)


def _create_month_partition(table_name: str, *, start: date, end: date) -> None:
    """为一个按交易日分区的父表创建确定性月度物理分区。"""
    suffix = start.strftime("%Y%m")
    op.execute(
        f"CREATE TABLE {table_name}_{suffix} PARTITION OF {table_name} "
        f"FOR VALUES FROM ('{start.isoformat()}') TO ('{end.isoformat()}')"
    )
