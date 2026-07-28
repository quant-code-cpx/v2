"""记录上市生命周期官方更正所需的来源证据引用。

Revision ID: 202607270002
Revises: 202607270001
Create Date: 2026-07-27
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

# Alembic 使用的版本标识。
revision = "202607270002"
down_revision = "202607270001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """为官方更正追加可审计的来源证据引用与数据库约束。"""
    op.execute(
        """
        ALTER TABLE equity_listing_status_version
          ADD COLUMN correction_approval_reference VARCHAR(128)
        """
    )
    op.execute(
        """
        ALTER TABLE equity_listing_status_version
          ADD CONSTRAINT ck_equity_listing_status_correction_approval
          CHECK (
            evidence_kind <> 'OFFICIAL_CORRECTION'
            OR correction_approval_reference IS NOT NULL
          )
        """
    )


def downgrade() -> None:
    """仅在没有官方更正事实时移除历史兼容引用列，避免丢失证据。"""
    bind = op.get_bind()
    has_correction = bind.execute(
        text(
            """
            SELECT EXISTS (
              SELECT 1
              FROM equity_listing_status_version
              WHERE evidence_kind = 'OFFICIAL_CORRECTION'
            )
            """
        )
    ).scalar_one()
    if has_correction:
        raise RuntimeError("cannot downgrade after lifecycle official correction exists")
    op.execute(
        """
        ALTER TABLE equity_listing_status_version
          DROP CONSTRAINT ck_equity_listing_status_correction_approval,
          DROP COLUMN correction_approval_reference
        """
    )
