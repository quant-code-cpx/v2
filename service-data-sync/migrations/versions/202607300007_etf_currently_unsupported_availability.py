"""区分 ETF 当前口径暂不支持与空集、来源故障。

Revision ID: 202607300007
Revises: 202607300006
Create Date: 2026-07-30
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "202607300007"
down_revision = "202607300006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """扩展可用性状态约束，使货币 ETF 收益口径不会冒充 NAV 或来源故障。"""
    op.execute(
        """
        ALTER TABLE dataset_availability_observation
          DROP CONSTRAINT ck_dataset_availability_observation_state,
          ADD CONSTRAINT ck_dataset_availability_observation_state
          CHECK (
            availability IN ('empty', 'source_unavailable', 'currently_unsupported')
          );
        """
    )


def downgrade() -> None:
    """仅在不存在暂不支持观察时恢复旧约束，避免删除真实审计结论。"""
    populated = (
        op.get_bind()
        .execute(
            text(
                """
                SELECT EXISTS (
                  SELECT 1
                  FROM dataset_availability_observation
                  WHERE availability = 'currently_unsupported'
                )
                """
            )
        )
        .scalar_one()
    )
    if populated:
        raise RuntimeError("cannot remove ETF currently-unsupported observations")
    op.execute(
        """
        ALTER TABLE dataset_availability_observation
          DROP CONSTRAINT ck_dataset_availability_observation_state,
          ADD CONSTRAINT ck_dataset_availability_observation_state
          CHECK (availability IN ('empty', 'source_unavailable'));
        """
    )
