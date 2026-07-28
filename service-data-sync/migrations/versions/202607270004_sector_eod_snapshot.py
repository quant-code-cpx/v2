"""创建板块 EOD 横截面、质量证据与版本化排行存储。

Revision ID: 202607270004
Revises: 202607270003
Create Date: 2026-07-27
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

# Alembic 使用的版本标识。
revision = "202607270004"
down_revision = "202607270003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """创建完整横截面、行级报价、质量结果和可恢复分区账本。"""
    _create_partition_table()
    _create_snapshot_table()
    _create_quote_table()
    _create_quality_table()


def downgrade() -> None:
    """仅在没有 EOD canonical 数据时删除结构，避免回滚静默丢失历史证据。"""
    bind = op.get_bind()
    has_snapshots = bind.execute(
        text("SELECT EXISTS (SELECT 1 FROM sector_eod_snapshot)")
    ).scalar_one()
    if has_snapshots:
        raise RuntimeError("cannot downgrade sector eod schema after snapshots exist")
    op.execute("DROP TABLE sector_eod_quality_result")
    op.execute("DROP TABLE sector_eod_quote")
    op.execute("DROP TABLE sector_eod_snapshot")
    op.execute("DROP TABLE sector_eod_sync_partition")


def _create_partition_table() -> None:
    """创建按 scheme/交易日定位的 EOD 恢复账本，保留 lease 与稳定错误码。"""
    op.execute(
        """
        CREATE TABLE sector_eod_sync_partition (
          scheme VARCHAR(64) NOT NULL REFERENCES sector_scheme(scheme),
          trade_date DATE NOT NULL,
          run_id UUID NOT NULL REFERENCES sync_run(run_id),
          status VARCHAR(16) NOT NULL CHECK (
            status IN ('queued', 'running', 'succeeded', 'partial', 'failed', 'cancelled')
          ),
          stage VARCHAR(24) NOT NULL CHECK (
            stage IN (
              'requested', 'fetched', 'raw_archived', 'normalized', 'quality_passed', 'published'
            )
          ),
          attempt INTEGER NOT NULL CHECK (attempt >= 0),
          lease_owner VARCHAR(100),
          lease_token UUID,
          lease_expires_at TIMESTAMPTZ,
          last_source_batch_id UUID REFERENCES source_batch(source_batch_id),
          last_error_code VARCHAR(64),
          updated_at TIMESTAMPTZ NOT NULL,
          PRIMARY KEY (scheme, trade_date),
          CHECK (
            (lease_owner IS NULL AND lease_token IS NULL AND lease_expires_at IS NULL)
            OR (
              lease_owner IS NOT NULL AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL
            )
          )
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_sector_eod_sync_partition_reclaim
        ON sector_eod_sync_partition (status, lease_expires_at)
        WHERE status IN ('queued', 'running', 'partial')
        """
    )
    op.execute(
        "CREATE INDEX ix_sector_eod_sync_partition_run ON sector_eod_sync_partition (run_id)"
    )


def _create_snapshot_table() -> None:
    """创建每个分类体系和交易日的不可变 EOD revision header。"""
    op.execute(
        """
        CREATE TABLE sector_eod_snapshot (
          snapshot_id UUID PRIMARY KEY,
          data_version UUID NOT NULL UNIQUE,
          scheme VARCHAR(64) NOT NULL REFERENCES sector_scheme(scheme),
          trade_date DATE NOT NULL,
          revision INTEGER NOT NULL CHECK (revision > 0),
          source_cutoff_at TIMESTAMPTZ NOT NULL,
          observed_at TIMESTAMPTZ NOT NULL,
          finality VARCHAR(32) NOT NULL CHECK (finality = 'post_close_observation'),
          state VARCHAR(16) NOT NULL CHECK (
            state IN ('candidate', 'quarantined', 'published', 'superseded')
          ),
          quality_status VARCHAR(16) NOT NULL CHECK (
            quality_status IN ('passed', 'warned', 'quarantined')
          ),
          record_count INTEGER NOT NULL CHECK (record_count >= 0),
          expected_count INTEGER NOT NULL CHECK (expected_count >= 0),
          coverage_ratio NUMERIC(9, 8) NOT NULL CHECK (
            coverage_ratio >= 0 AND coverage_ratio <= 1
          ),
          normalizer_version VARCHAR(32) NOT NULL,
          content_sha256 BYTEA NOT NULL,
          source_batch_id UUID NOT NULL REFERENCES source_batch(source_batch_id) ON DELETE RESTRICT,
          created_at TIMESTAMPTZ NOT NULL,
          published_at TIMESTAMPTZ,
          superseded_at TIMESTAMPTZ,
          UNIQUE (scheme, trade_date, revision),
          CHECK (observed_at >= source_cutoff_at),
          CHECK (
            (state = 'published' AND published_at IS NOT NULL AND superseded_at IS NULL
              AND quality_status IN ('passed', 'warned'))
            OR (state = 'superseded' AND published_at IS NOT NULL AND superseded_at IS NOT NULL)
            OR (state IN ('candidate', 'quarantined') AND published_at IS NULL)
          )
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_sector_eod_snapshot_current
        ON sector_eod_snapshot (scheme, trade_date)
        WHERE state = 'published' AND superseded_at IS NULL
        """
    )
    op.execute(
        """
        CREATE INDEX ix_sector_eod_snapshot_latest
        ON sector_eod_snapshot (scheme, trade_date DESC)
        INCLUDE (data_version, observed_at, quality_status, published_at)
        WHERE state = 'published' AND superseded_at IS NULL
        """
    )


def _create_quote_table() -> None:
    """创建快照内板块报价，保留观察时名称与来源原生单位而不保存供应商排名。"""
    op.execute(
        """
        CREATE TABLE sector_eod_quote (
          snapshot_id UUID NOT NULL REFERENCES sector_eod_snapshot(snapshot_id) ON DELETE RESTRICT,
          sector_key BIGINT NOT NULL REFERENCES sector_entity(sector_key),
          sector_name VARCHAR(200) NOT NULL CHECK (BTRIM(sector_name) <> ''),
          latest_value NUMERIC(24, 6) CHECK (latest_value IS NULL OR latest_value >= 0),
          latest_value_unit VARCHAR(32) NOT NULL CHECK (latest_value_unit = 'provider_native'),
          change_value NUMERIC(24, 6),
          change_percent NUMERIC(16, 10),
          market_value NUMERIC(30, 4) CHECK (market_value IS NULL OR market_value >= 0),
          market_value_unit VARCHAR(32) NOT NULL CHECK (market_value_unit = 'provider_native'),
          turnover_percent NUMERIC(16, 10) CHECK (
            turnover_percent IS NULL OR turnover_percent >= 0
          ),
          advancers INTEGER CHECK (advancers IS NULL OR advancers >= 0),
          decliners INTEGER CHECK (decliners IS NULL OR decliners >= 0),
          leader_name VARCHAR(200),
          leader_change_percent NUMERIC(16, 10),
          row_sha256 BYTEA NOT NULL,
          PRIMARY KEY (snapshot_id, sector_key)
        )
        """
    )


def _create_quality_table() -> None:
    """创建小型结构化质量证据表，不存放可由 S3 raw 恢复的来源响应。"""
    op.execute(
        """
        CREATE TABLE sector_eod_quality_result (
          quality_result_id UUID PRIMARY KEY,
          snapshot_id UUID NOT NULL REFERENCES sector_eod_snapshot(snapshot_id) ON DELETE RESTRICT,
          rule_code VARCHAR(64) NOT NULL,
          severity VARCHAR(16) NOT NULL CHECK (severity IN ('info', 'warning', 'blocking')),
          passed BOOLEAN NOT NULL,
          actual JSONB NOT NULL,
          threshold JSONB NOT NULL,
          created_at TIMESTAMPTZ NOT NULL,
          UNIQUE (snapshot_id, rule_code)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_sector_eod_quality_result_lookup
        ON sector_eod_quality_result (snapshot_id, severity, passed)
        """
    )
