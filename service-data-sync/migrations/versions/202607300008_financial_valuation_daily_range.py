"""修复日频估值开放业务区间导致第二日无法写入的问题。

Revision ID: 202607300008
Revises: 202607300007
Create Date: 2026-07-30
"""

from __future__ import annotations

from alembic import op

revision = "202607300008"
down_revision = "202607300007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """把既有估值规范为单日半开区间，并以数据库约束防止再次写入开放区间。"""
    op.execute(
        """
        UPDATE valuation_observation_revision
        SET effective_from = observation_date,
            effective_to = observation_date + 1
        WHERE effective_from IS DISTINCT FROM observation_date
           OR effective_to IS DISTINCT FROM observation_date + 1;

        ALTER TABLE valuation_observation_revision
          DROP CONSTRAINT ck_valuation_observation_effective_range,
          ADD CONSTRAINT ck_valuation_observation_effective_range
          CHECK (
            effective_from = observation_date
            AND effective_to IS NOT NULL
            AND effective_to = observation_date + 1
          );
        """
    )


def downgrade() -> None:
    """恢复通用有效区间约束，但保留已修复的单日边界以免制造重叠历史。"""
    op.execute(
        """
        ALTER TABLE valuation_observation_revision
          DROP CONSTRAINT ck_valuation_observation_effective_range,
          ADD CONSTRAINT ck_valuation_observation_effective_range
          CHECK (effective_to IS NULL OR effective_to > effective_from);
        """
    )
