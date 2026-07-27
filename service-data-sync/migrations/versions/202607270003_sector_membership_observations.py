"""创建板块成分观测快照、区间与不可变发布清单。

Revision ID: 202607270003
Revises: 202607270002
Create Date: 2026-07-27
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

# Alembic 使用的版本标识。
revision = "202607270003"
down_revision = "202607270002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """建立仅表达来源观测、绝不伪造真实调入调出日的成分历史存储。"""
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
    _create_snapshot_tables()
    _create_interval_table()
    _create_release_tables()


def downgrade() -> None:
    """仅在没有成分观测事实时删除表，避免破坏已发布审计历史。"""
    bind = op.get_bind()
    has_observations = bind.execute(
        text("SELECT EXISTS (SELECT 1 FROM sector_membership_snapshot)")
    ).scalar_one()
    if has_observations:
        raise RuntimeError("cannot downgrade after sector membership observations exist")
    op.execute("DROP TABLE sector_membership_release_sector")
    op.execute("DROP TABLE sector_membership_release")
    op.execute("DROP TABLE sector_membership_interval")
    op.execute("DROP TABLE sector_membership_quality_result")
    op.execute("DROP TABLE sector_membership_quarantine")
    op.execute("DROP TABLE sector_membership_pending")
    op.execute("DROP TABLE sector_membership_item")
    op.execute("DROP TABLE sector_membership_snapshot")


def _create_snapshot_tables() -> None:
    """创建快照头、已确认成员、待确认成员、隔离项与质量结果表。"""
    op.execute(
        """
        CREATE TABLE sector_membership_snapshot (
          snapshot_id UUID PRIMARY KEY,
          sector_key BIGINT NOT NULL REFERENCES sector_entity(sector_key),
          source_batch_id UUID NOT NULL UNIQUE REFERENCES source_batch(source_batch_id),
          observed_at TIMESTAMPTZ NOT NULL,
          observation_date DATE NOT NULL,
          status VARCHAR(16) NOT NULL CHECK (status IN ('COMPLETE', 'QUARANTINED')),
          quality_status VARCHAR(16) NOT NULL CHECK (
            quality_status IN ('passed', 'warned', 'rejected')
          ),
          member_count INTEGER NOT NULL CHECK (member_count > 0),
          verified_count INTEGER NOT NULL CHECK (verified_count >= 0),
          pending_count INTEGER NOT NULL CHECK (pending_count >= 0),
          quarantine_count INTEGER NOT NULL CHECK (quarantine_count >= 0),
          content_sha256 BYTEA NOT NULL,
          idempotency_key CHAR(64) NOT NULL UNIQUE,
          UNIQUE (sector_key, observed_at, content_sha256),
          CHECK (member_count = verified_count + pending_count + quarantine_count)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_sector_membership_snapshot_complete
        ON sector_membership_snapshot (sector_key, observed_at DESC)
        WHERE status = 'COMPLETE'
        """
    )
    op.execute(
        """
        CREATE TABLE sector_membership_item (
          snapshot_date DATE NOT NULL,
          snapshot_id UUID NOT NULL REFERENCES sector_membership_snapshot(snapshot_id),
          security_id BIGINT NOT NULL REFERENCES equity_instrument(security_id),
          source_symbol CHAR(6) NOT NULL CHECK (source_symbol ~ '^[0-9]{6}$'),
          source_name VARCHAR(200) NOT NULL CHECK (BTRIM(source_name) <> ''),
          content_sha256 BYTEA NOT NULL,
          PRIMARY KEY (snapshot_date, snapshot_id, security_id),
          UNIQUE (snapshot_date, snapshot_id, source_symbol)
        ) PARTITION BY RANGE (snapshot_date)
        """
    )
    op.execute(
        """
        CREATE TABLE sector_membership_pending (
          pending_id BIGSERIAL PRIMARY KEY,
          snapshot_id UUID NOT NULL REFERENCES sector_membership_snapshot(snapshot_id),
          row_ordinal INTEGER NOT NULL CHECK (row_ordinal > 0),
          source_symbol CHAR(6) NOT NULL CHECK (source_symbol ~ '^[0-9]{6}$'),
          source_name VARCHAR(200) NOT NULL CHECK (BTRIM(source_name) <> ''),
          inferred_exchange VARCHAR(4) CHECK (inferred_exchange IN ('SSE', 'SZSE', 'BSE')),
          reason_code VARCHAR(64) NOT NULL,
          created_at TIMESTAMPTZ NOT NULL,
          UNIQUE (snapshot_id, row_ordinal)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE sector_membership_quarantine (
          quarantine_id BIGSERIAL PRIMARY KEY,
          snapshot_id UUID NOT NULL REFERENCES sector_membership_snapshot(snapshot_id),
          row_ordinal INTEGER NOT NULL CHECK (row_ordinal > 0),
          source_symbol CHAR(6) NOT NULL CHECK (source_symbol ~ '^[0-9]{6}$'),
          source_name VARCHAR(200) NOT NULL CHECK (BTRIM(source_name) <> ''),
          reason_code VARCHAR(64) NOT NULL,
          created_at TIMESTAMPTZ NOT NULL,
          UNIQUE (snapshot_id, row_ordinal)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE sector_membership_quality_result (
          snapshot_id UUID NOT NULL REFERENCES sector_membership_snapshot(snapshot_id),
          rule_code VARCHAR(64) NOT NULL,
          severity VARCHAR(16) NOT NULL CHECK (severity IN ('warn', 'error')),
          disposition VARCHAR(16) NOT NULL CHECK (disposition IN ('publish', 'quarantine')),
          actual_value NUMERIC,
          expected_value NUMERIC,
          created_at TIMESTAMPTZ NOT NULL,
          PRIMARY KEY (snapshot_id, rule_code)
        )
        """
    )


def _create_interval_table() -> None:
    """创建半开观测区间；区间端点只表示快照看见或缺席，不表示真实事件日期。"""
    op.execute(
        """
        CREATE TABLE sector_membership_interval (
          interval_id BIGSERIAL PRIMARY KEY,
          sector_key BIGINT NOT NULL REFERENCES sector_entity(sector_key),
          security_id BIGINT NOT NULL REFERENCES equity_instrument(security_id),
          observed_from TIMESTAMPTZ NOT NULL,
          observed_to TIMESTAMPTZ,
          open_snapshot_id UUID NOT NULL REFERENCES sector_membership_snapshot(snapshot_id),
          close_snapshot_id UUID REFERENCES sector_membership_snapshot(snapshot_id),
          observation_range TSTZRANGE GENERATED ALWAYS AS
            (tstzrange(observed_from, observed_to, '[)')) STORED,
          CHECK (observed_to IS NULL OR observed_to > observed_from),
          CHECK (
            (observed_to IS NULL AND close_snapshot_id IS NULL)
            OR (observed_to IS NOT NULL AND close_snapshot_id IS NOT NULL)
          ),
          UNIQUE (sector_key, security_id, observed_from),
          EXCLUDE USING gist (
            sector_key WITH =,
            security_id WITH =,
            observation_range WITH &&
          )
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_sector_membership_interval_current
        ON sector_membership_interval (sector_key, security_id)
        WHERE observed_to IS NULL
        """
    )
    op.execute(
        """
        CREATE INDEX ix_sector_membership_interval_reverse
        ON sector_membership_interval (security_id, observed_from DESC)
        """
    )


def _create_release_tables() -> None:
    """创建 scheme 级不可变 release 及其固定板块快照清单。"""
    op.execute(
        """
        CREATE TABLE sector_membership_release (
          release_id UUID PRIMARY KEY,
          scheme VARCHAR(64) NOT NULL REFERENCES sector_scheme(scheme),
          release_as_of TIMESTAMPTZ NOT NULL,
          coverage_start TIMESTAMPTZ NOT NULL,
          data_version UUID NOT NULL UNIQUE,
          quality_status VARCHAR(16) NOT NULL CHECK (quality_status IN ('passed', 'warned')),
          expected_sector_count INTEGER NOT NULL CHECK (expected_sector_count > 0),
          fresh_sector_count INTEGER NOT NULL CHECK (fresh_sector_count >= 0),
          carried_forward_sector_count INTEGER NOT NULL CHECK (carried_forward_sector_count >= 0),
          identity_coverage_percent NUMERIC(5, 2) NOT NULL CHECK (identity_coverage_percent = 100),
          excluded_identity_count INTEGER NOT NULL CHECK (excluded_identity_count = 0),
          published_at TIMESTAMPTZ NOT NULL,
          superseded_at TIMESTAMPTZ,
          CHECK (fresh_sector_count + carried_forward_sector_count = expected_sector_count)
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_sector_membership_release_current
        ON sector_membership_release (scheme)
        WHERE superseded_at IS NULL
        """
    )
    op.execute(
        """
        CREATE TABLE sector_membership_release_sector (
          release_id UUID NOT NULL REFERENCES sector_membership_release(release_id),
          sector_key BIGINT NOT NULL REFERENCES sector_entity(sector_key),
          snapshot_id UUID NOT NULL REFERENCES sector_membership_snapshot(snapshot_id),
          carried_forward BOOLEAN NOT NULL,
          snapshot_observed_at TIMESTAMPTZ NOT NULL,
          PRIMARY KEY (release_id, sector_key),
          UNIQUE (release_id, snapshot_id)
        )
        """
    )
