"""创建 P0 标准证券身份、原始血缘、日线与发布表。

Revision ID: 202607260001
Revises:
Create Date: 2026-07-26
"""

from __future__ import annotations

from alembic import op

# Alembic 使用的版本标识。
revision = "202607260001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """创建近期一个月同步需要的追加式 P0 个股表与日期分区。"""
    op.execute(
        """
        CREATE TABLE equity_instrument (
          security_id BIGSERIAL PRIMARY KEY,
          instrument_id UUID NOT NULL UNIQUE,
          exchange VARCHAR(4) NOT NULL CHECK (exchange IN ('SSE', 'SZSE', 'BSE')),
          symbol VARCHAR(6) NOT NULL CHECK (symbol ~ '^[0-9]{6}$'),
          name TEXT,
          listing_status VARCHAR(16) NOT NULL CHECK (
            listing_status IN ('PENDING', 'LISTED', 'SUSPENDED', 'DELISTED')
          ),
          created_at TIMESTAMPTZ NOT NULL,
          updated_at TIMESTAMPTZ NOT NULL,
          UNIQUE (exchange, symbol)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_equity_instrument_exchange_symbol
        ON equity_instrument (exchange, symbol)
        """
    )
    op.execute(
        """
        CREATE TABLE source_batch (
          source_batch_id UUID PRIMARY KEY,
          provider_id VARCHAR(100) NOT NULL,
          capability VARCHAR(100) NOT NULL,
          payload_sha256 CHAR(64) NOT NULL CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
          raw_uri TEXT NOT NULL,
          observed_at TIMESTAMPTZ NOT NULL,
          created_at TIMESTAMPTZ NOT NULL,
          UNIQUE (provider_id, capability, payload_sha256)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE equity_daily_bar (
          security_id BIGINT NOT NULL REFERENCES equity_instrument(security_id),
          trade_date DATE NOT NULL,
          revision INTEGER NOT NULL CHECK (revision > 0),
          open_price NUMERIC(20, 6) NOT NULL CHECK (open_price >= 0),
          high_price NUMERIC(20, 6) NOT NULL CHECK (high_price >= 0),
          low_price NUMERIC(20, 6) NOT NULL CHECK (low_price >= 0),
          close_price NUMERIC(20, 6) NOT NULL CHECK (close_price >= 0),
          volume_shares BIGINT NOT NULL CHECK (volume_shares >= 0),
          amount_cny NUMERIC(24, 4) NOT NULL CHECK (amount_cny >= 0),
          turnover_rate NUMERIC(16, 10),
          is_final BOOLEAN NOT NULL,
          content_sha256 BYTEA NOT NULL,
          source_batch_id UUID NOT NULL REFERENCES source_batch(source_batch_id),
          valid_from TIMESTAMPTZ NOT NULL,
          valid_to TIMESTAMPTZ,
          PRIMARY KEY (security_id, trade_date, revision),
          CHECK (low_price <= LEAST(open_price, close_price)),
          CHECK (high_price >= GREATEST(open_price, close_price)),
          CHECK (low_price <= high_price)
        ) PARTITION BY RANGE (trade_date)
        """
    )
    # 预建当前年份相邻分区；默认分区保证回填和跨新年写入仍可用。
    _create_daily_bar_partition("2025", "2025-01-01", "2026-01-01")
    _create_daily_bar_partition("2026", "2026-01-01", "2027-01-01")
    _create_daily_bar_partition("2027", "2027-01-01", "2028-01-01")
    op.execute("CREATE TABLE equity_daily_bar_default PARTITION OF equity_daily_bar DEFAULT")
    _create_daily_bar_indexes("equity_daily_bar_default")
    op.execute(
        """
        CREATE TABLE dataset_publication (
          publication_id UUID PRIMARY KEY,
          dataset VARCHAR(100) NOT NULL,
          partition_key VARCHAR(240) NOT NULL,
          data_version UUID NOT NULL,
          quality_status VARCHAR(16) NOT NULL CHECK (
            quality_status IN ('passed', 'warned', 'partial')
          ),
          published_at TIMESTAMPTZ NOT NULL,
          superseded_at TIMESTAMPTZ,
          UNIQUE (dataset, partition_key, data_version)
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_dataset_publication_current
        ON dataset_publication (dataset, partition_key)
        WHERE superseded_at IS NULL
        """
    )


def downgrade() -> None:
    """按依赖顺序删除 P0 表，以支持完整迁移回滚。"""
    op.execute("DROP TABLE dataset_publication")
    op.execute("DROP TABLE equity_daily_bar")
    op.execute("DROP TABLE source_batch")
    op.execute("DROP TABLE equity_instrument")


def _create_daily_bar_partition(name: str, start: str, end: str) -> None:
    """创建一个年度父表分区及其当前数据读取索引。"""
    table_name = f"equity_daily_bar_{name}"
    op.execute(
        f"""
        CREATE TABLE {table_name} PARTITION OF equity_daily_bar
        FOR VALUES FROM ('{start}') TO ('{end}')
        """
    )
    _create_daily_bar_indexes(table_name)


def _create_daily_bar_indexes(table_name: str) -> None:
    """为一个日期分区添加当前行唯一索引和覆盖读取索引。"""
    # 部分唯一索引允许保留历史，同时强制每个交易日仅有一个当前修订。
    op.execute(
        f"""
        CREATE UNIQUE INDEX uq_{table_name}_current
        ON {table_name} (security_id, trade_date)
        WHERE valid_to IS NULL
        """
    )
    # `INCLUDE` 覆盖日期窗口读取，且不扩大范围过滤所用的索引键。
    op.execute(
        f"""
        CREATE INDEX ix_{table_name}_current_read
        ON {table_name} (security_id, trade_date DESC)
        INCLUDE (
          open_price, high_price, low_price, close_price, volume_shares,
          amount_cny, turnover_rate, is_final, revision
        )
        WHERE valid_to IS NULL
        """
    )
