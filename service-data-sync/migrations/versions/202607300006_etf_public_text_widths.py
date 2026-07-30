"""对齐 ETF v2 公开文本与 canonical 存储宽度。

Revision ID: 202607300006
Revises: 202607300005
Create Date: 2026-07-30
"""

from __future__ import annotations

from alembic import op

revision = "202607300006"
down_revision = "202607300005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """拒绝既有超长事实后调整列宽，避免迁移通过显式 cast 静默截断来源文本。"""
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (
            SELECT 1
            FROM etf_profile_version
            WHERE char_length(display_name) > 160
               OR char_length(manager_name) > 160
               OR char_length(custodian_name) > 160
          ) THEN
            RAISE EXCEPTION 'ETF profile public text exceeds v2 contract width';
          END IF;
          IF EXISTS (
            SELECT 1
            FROM etf_status_revision
            WHERE char_length(reason) > 500
          ) THEN
            RAISE EXCEPTION 'ETF status reason exceeds v2 contract width';
          END IF;
        END
        $$;

        ALTER TABLE etf_profile_version
          ALTER COLUMN display_name TYPE VARCHAR(160),
          ALTER COLUMN etf_type TYPE VARCHAR(80),
          ALTER COLUMN management_mode TYPE VARCHAR(80),
          ALTER COLUMN manager_name TYPE VARCHAR(160),
          ALTER COLUMN custodian_name TYPE VARCHAR(160);

        ALTER TABLE etf_daily_bar_revision
          ALTER COLUMN volume_unit TYPE VARCHAR(40),
          ALTER COLUMN trade_status TYPE VARCHAR(80);

        ALTER TABLE etf_status_revision
          ALTER COLUMN status_code TYPE VARCHAR(80),
          ALTER COLUMN reason TYPE VARCHAR(500);
        """
    )


def downgrade() -> None:
    """仅在扩宽字段仍满足旧宽度时回滚，不能为了降级截断已发布 canonical 事实。"""
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (
            SELECT 1
            FROM etf_profile_version
            WHERE char_length(etf_type) > 32
               OR char_length(management_mode) > 24
          ) THEN
            RAISE EXCEPTION 'cannot narrow ETF profile classification columns';
          END IF;
          IF EXISTS (
            SELECT 1
            FROM etf_daily_bar_revision
            WHERE char_length(volume_unit) > 16
               OR char_length(trade_status) > 24
          ) THEN
            RAISE EXCEPTION 'cannot narrow ETF daily bar public text columns';
          END IF;
          IF EXISTS (
            SELECT 1
            FROM etf_status_revision
            WHERE char_length(status_code) > 24
          ) THEN
            RAISE EXCEPTION 'cannot narrow ETF status code column';
          END IF;
        END
        $$;

        ALTER TABLE etf_status_revision
          ALTER COLUMN reason TYPE TEXT,
          ALTER COLUMN status_code TYPE VARCHAR(24);

        ALTER TABLE etf_daily_bar_revision
          ALTER COLUMN trade_status TYPE VARCHAR(24),
          ALTER COLUMN volume_unit TYPE VARCHAR(16);

        ALTER TABLE etf_profile_version
          ALTER COLUMN custodian_name TYPE VARCHAR(200),
          ALTER COLUMN manager_name TYPE VARCHAR(200),
          ALTER COLUMN management_mode TYPE VARCHAR(24),
          ALTER COLUMN etf_type TYPE VARCHAR(32),
          ALTER COLUMN display_name TYPE VARCHAR(200);
        """
    )
