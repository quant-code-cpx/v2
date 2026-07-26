"""创建板块体系、身份与日周月独立历史行情表。

Revision ID: 202607260002
Revises: 202607260001
Create Date: 2026-07-26
"""

from __future__ import annotations

from alembic import op

# Alembic 使用的版本标识。
revision = "202607260002"
down_revision = "202607260001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """创建板块 P0 所需分类体系、占位身份和三张不可混用的周期表。"""
    op.execute(
        """
        CREATE TABLE sector_scheme (
          scheme VARCHAR(64) PRIMARY KEY,
          display_name TEXT NOT NULL,
          classification_kind VARCHAR(16) NOT NULL CHECK (
            classification_kind IN ('industry', 'concept')
          ),
          created_at TIMESTAMPTZ NOT NULL
        )
        """
    )
    op.execute(
        """
        INSERT INTO sector_scheme (scheme, display_name, classification_kind, created_at)
        VALUES
          ('eastmoney.industry', '东方财富行业板块', 'industry', NOW()),
          ('eastmoney.concept', '东方财富概念板块', 'concept', NOW())
        """
    )
    op.execute(
        """
        CREATE TABLE sector_entity (
          sector_key BIGSERIAL PRIMARY KEY,
          sector_id UUID NOT NULL UNIQUE,
          scheme VARCHAR(64) NOT NULL REFERENCES sector_scheme(scheme),
          sector_code VARCHAR(64) NOT NULL CHECK (BTRIM(sector_code) <> ''),
          name TEXT,
          status VARCHAR(16) NOT NULL CHECK (status IN ('PENDING', 'ACTIVE', 'RETIRED')),
          created_at TIMESTAMPTZ NOT NULL,
          updated_at TIMESTAMPTZ NOT NULL,
          UNIQUE (scheme, sector_code)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_sector_entity_scheme_code
        ON sector_entity (scheme, sector_code)
        """
    )
    _create_sector_bar_table("sector_daily_bar")
    _create_sector_bar_table("sector_weekly_bar")
    _create_sector_bar_table("sector_monthly_bar")


def downgrade() -> None:
    """按外键依赖反序删除板块行情和身份表，以支持完整回滚。"""
    op.execute("DROP TABLE sector_monthly_bar")
    op.execute("DROP TABLE sector_weekly_bar")
    op.execute("DROP TABLE sector_daily_bar")
    op.execute("DROP TABLE sector_entity")
    op.execute("DROP TABLE sector_scheme")


def _create_sector_bar_table(table_name: str) -> None:
    """创建一个上游直接周期表及其当前修订唯一、读取覆盖索引。"""
    op.execute(
        f"""
        CREATE TABLE {table_name} (
          sector_key BIGINT NOT NULL REFERENCES sector_entity(sector_key),
          period_end DATE NOT NULL,
          revision INTEGER NOT NULL CHECK (revision > 0),
          open_price NUMERIC(20, 6) NOT NULL CHECK (open_price >= 0),
          high_price NUMERIC(20, 6) NOT NULL CHECK (high_price >= 0),
          low_price NUMERIC(20, 6) NOT NULL CHECK (low_price >= 0),
          close_price NUMERIC(20, 6) NOT NULL CHECK (close_price >= 0),
          volume_value NUMERIC(24, 4) NOT NULL CHECK (volume_value >= 0),
          volume_unit VARCHAR(32) NOT NULL CHECK (volume_unit = 'provider_native'),
          amount_cny NUMERIC(24, 4) NOT NULL CHECK (amount_cny >= 0),
          amplitude_percent NUMERIC(16, 10),
          change_percent NUMERIC(16, 10),
          change_amount NUMERIC(24, 6),
          turnover_percent NUMERIC(16, 10),
          is_final BOOLEAN NOT NULL,
          content_sha256 BYTEA NOT NULL,
          source_batch_id UUID NOT NULL REFERENCES source_batch(source_batch_id),
          valid_from TIMESTAMPTZ NOT NULL,
          valid_to TIMESTAMPTZ,
          PRIMARY KEY (sector_key, period_end, revision),
          CHECK (low_price <= LEAST(open_price, close_price)),
          CHECK (high_price >= GREATEST(open_price, close_price)),
          CHECK (low_price <= high_price),
          CHECK (amplitude_percent IS NULL OR amplitude_percent >= 0),
          CHECK (turnover_percent IS NULL OR turnover_percent >= 0)
        )
        """
    )
    # 单一当前行允许保留可审计历史，而读取方无需自行挑选版本。
    op.execute(
        f"""
        CREATE UNIQUE INDEX uq_{table_name}_current
        ON {table_name} (sector_key, period_end)
        WHERE valid_to IS NULL
        """
    )
    op.execute(
        f"""
        CREATE INDEX ix_{table_name}_current_read
        ON {table_name} (sector_key, period_end DESC)
        INCLUDE (
          open_price, high_price, low_price, close_price, volume_value, volume_unit,
          amount_cny, amplitude_percent, change_percent, change_amount, turnover_percent,
          is_final, revision
        )
        WHERE valid_to IS NULL
        """
    )
