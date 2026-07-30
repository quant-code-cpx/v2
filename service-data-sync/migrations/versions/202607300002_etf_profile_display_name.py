"""为 ETF 产品资料扩展来源展示名称。

历史资料没有可复验名称时保持空值；新同步先写入该列，待覆盖率验证完成后再评估收紧约束。

Revision ID: 202607300002
Revises: 202607300001
Create Date: 2026-07-30
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision = "202607300002"
down_revision = "202607300001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """以可兼容历史行的 nullable 列扩展 ETF 资料表。"""
    op.execute(
        """
        ALTER TABLE etf_profile_version
        ADD COLUMN IF NOT EXISTS display_name VARCHAR(200);
        COMMENT ON COLUMN etf_profile_version.display_name IS
          '来源明确提供的 ETF 展示名称；迁移前历史资料可为空。';
        """
    )


def downgrade() -> None:
    """仅在没有已发布展示名称时移除扩展列，避免静默丢失真实资料。"""
    populated = (
        op.get_bind()
        .execute(
            text("SELECT EXISTS (SELECT 1 FROM etf_profile_version WHERE display_name IS NOT NULL)")
        )
        .scalar_one()
    )
    if populated:
        raise RuntimeError("cannot remove ETF display name after values have been published")
    op.drop_column("etf_profile_version", "display_name")
