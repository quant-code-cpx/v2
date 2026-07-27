"""扩展共享观测账本，并创建证券主数据双时间存储。

Revision ID: 202607270001
Revises: 202607260002
Create Date: 2026-07-27
"""

from __future__ import annotations

from uuid import uuid4

from alembic import op
from sqlalchemy import text

# Alembic 使用的版本标识。
revision = "202607270001"
down_revision = "202607260002"
branch_labels = None
depends_on = None

_LEGACY_SOURCE_RUN_ID = "00000000-0000-0000-0000-000000000001"
_LEGACY_MASTER_SEED_RUN_ID = "00000000-0000-0000-0000-000000000002"
_LEGACY_MASTER_SEED_BATCH_ID = "00000000-0000-0000-0000-000000000003"


def upgrade() -> None:
    """创建可恢复的共享观测账本与证券身份、名称、生命周期历史表。"""
    _create_sync_tables()
    _expand_source_batch()
    _create_master_tables()
    _seed_legacy_pending_identities()


def downgrade() -> None:
    """仅在未产生新观测或已发布主数据时安全恢复 P0 共享表结构。"""
    bind = op.get_bind()
    changed_rows = bind.execute(
        text(
            """
            SELECT EXISTS (
              SELECT 1
              FROM source_batch
              WHERE run_id NOT IN (:legacy_run_id, :legacy_seed_run_id)
            )
            """
        ),
        {
            "legacy_run_id": _LEGACY_SOURCE_RUN_ID,
            "legacy_seed_run_id": _LEGACY_MASTER_SEED_RUN_ID,
        },
    ).scalar_one()
    if changed_rows:
        raise RuntimeError(
            "cannot downgrade equity-master expand after new source observations exist"
        )

    op.execute("DROP TABLE data_quality_issue")
    op.execute("DROP TABLE equity_identity_quarantine")
    op.execute("DROP TABLE equity_presence_anomaly")
    op.execute("DROP TABLE equity_master_snapshot_member")
    op.execute("DROP TABLE equity_master_snapshot")
    op.execute("DROP TABLE equity_listing_status_version")
    op.execute("DROP TABLE equity_name_version")
    op.execute("DROP TABLE equity_identifier_version")
    op.execute("DROP TABLE dataset_publication_component")
    op.execute(
        """
        ALTER TABLE equity_instrument
          DROP COLUMN current_master_version,
          DROP COLUMN master_confirmed_at
        """
    )
    op.execute(
        """
        ALTER TABLE dataset_publication
          DROP COLUMN knowledge_cutoff,
          DROP COLUMN effective_as_of
        """
    )
    op.execute(f"DELETE FROM source_batch WHERE source_batch_id = '{_LEGACY_MASTER_SEED_BATCH_ID}'")
    op.execute(
        """
        ALTER TABLE source_batch
          DROP CONSTRAINT uq_source_batch_observation,
          DROP CONSTRAINT fk_source_batch_run,
          DROP CONSTRAINT ck_source_batch_observation_seq,
          DROP COLUMN schema_fingerprint,
          DROP COLUMN adapter_version,
          DROP COLUMN upstream_source,
          DROP COLUMN observation_seq,
          DROP COLUMN partition_key,
          DROP COLUMN run_id
        """
    )
    op.execute("DROP INDEX ix_source_batch_payload_lookup")
    op.execute(
        """
        ALTER TABLE source_batch
        ADD CONSTRAINT source_batch_provider_id_capability_payload_sha256_key
        UNIQUE (provider_id, capability, payload_sha256)
        """
    )
    op.execute("DROP TABLE sync_partition")
    op.execute("DROP TABLE sync_run")


def _create_sync_tables() -> None:
    """创建任务运行与分区账本，供所有数据集共享观测身份。"""
    op.execute(
        """
        CREATE TABLE sync_run (
          run_id UUID PRIMARY KEY,
          capability VARCHAR(100) NOT NULL,
          mode VARCHAR(16) NOT NULL CHECK (mode IN ('manual', 'scheduled', 'backfill', 'legacy')),
          request_key VARCHAR(240) NOT NULL UNIQUE,
          target_date DATE,
          status VARCHAR(16) NOT NULL CHECK (
            status IN ('queued', 'running', 'succeeded', 'partial', 'failed', 'cancelled')
          ),
          requested_at TIMESTAMPTZ NOT NULL,
          started_at TIMESTAMPTZ,
          finished_at TIMESTAMPTZ,
          created_at TIMESTAMPTZ NOT NULL
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_sync_run_capability_requested_at
        ON sync_run (capability, requested_at DESC)
        """
    )
    op.execute(
        """
        CREATE TABLE sync_partition (
          run_id UUID NOT NULL REFERENCES sync_run(run_id),
          partition_key VARCHAR(240) NOT NULL,
          status VARCHAR(16) NOT NULL CHECK (
            status IN ('queued', 'running', 'succeeded', 'partial', 'failed', 'cancelled')
          ),
          attempt INTEGER NOT NULL CHECK (attempt > 0),
          lease_owner VARCHAR(128),
          lease_until TIMESTAMPTZ,
          heartbeat_at TIMESTAMPTZ,
          next_retry_at TIMESTAMPTZ,
          checkpoint_json JSONB,
          error_code VARCHAR(64),
          updated_at TIMESTAMPTZ NOT NULL,
          PRIMARY KEY (run_id, partition_key)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_sync_partition_reclaim
        ON sync_partition (lease_until, next_retry_at)
        WHERE status IN ('queued', 'running', 'partial')
        """
    )


def _expand_source_batch() -> None:
    """回填旧证据后移除内容哈希唯一性，改为每次观测独立身份。"""
    op.execute(
        """
        INSERT INTO sync_run (
          run_id, capability, mode, request_key, target_date, status,
          requested_at, started_at, finished_at, created_at
        ) VALUES (
          '00000000-0000-0000-0000-000000000001',
          'legacy.source-batch', 'legacy', 'legacy-source-batch-backfill', NULL, 'succeeded',
          CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
        )
        """
    )
    op.execute(
        """
        ALTER TABLE source_batch
          ADD COLUMN run_id UUID,
          ADD COLUMN partition_key VARCHAR(240),
          ADD COLUMN observation_seq INTEGER,
          ADD COLUMN upstream_source VARCHAR(100),
          ADD COLUMN adapter_version VARCHAR(64),
          ADD COLUMN schema_fingerprint CHAR(64)
        """
    )
    op.execute(
        """
        INSERT INTO sync_partition (
          run_id, partition_key, status, attempt, updated_at
        )
        SELECT
          '00000000-0000-0000-0000-000000000001',
          'legacy:' || source_batch_id::text,
          'succeeded',
          1,
          created_at
        FROM source_batch
        """
    )
    op.execute(
        """
        UPDATE source_batch
        SET run_id = '00000000-0000-0000-0000-000000000001',
            partition_key = 'legacy:' || source_batch_id::text,
            observation_seq = 1,
            upstream_source = provider_id,
            adapter_version = 'legacy-unversioned',
            schema_fingerprint = repeat('0', 64)
        """
    )
    op.execute(
        """
        ALTER TABLE source_batch
        DROP CONSTRAINT source_batch_provider_id_capability_payload_sha256_key
        """
    )
    op.execute(
        """
        CREATE INDEX ix_source_batch_payload_lookup
        ON source_batch (provider_id, capability, payload_sha256)
        """
    )
    op.execute(
        """
        ALTER TABLE source_batch
          ALTER COLUMN run_id SET NOT NULL,
          ALTER COLUMN partition_key SET NOT NULL,
          ALTER COLUMN observation_seq SET NOT NULL,
          ALTER COLUMN upstream_source SET NOT NULL,
          ALTER COLUMN adapter_version SET NOT NULL,
          ALTER COLUMN schema_fingerprint SET NOT NULL,
          ADD CONSTRAINT fk_source_batch_run
            FOREIGN KEY (run_id) REFERENCES sync_run(run_id),
          ADD CONSTRAINT uq_source_batch_observation
            UNIQUE (run_id, partition_key, observation_seq),
          ADD CONSTRAINT ck_source_batch_observation_seq
            CHECK (observation_seq > 0)
        """
    )


def _create_master_tables() -> None:
    """创建双时间版本、目录快照、质量问题与聚合发布组件表。"""
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
    op.execute(
        """
        ALTER TABLE dataset_publication
          ADD COLUMN effective_as_of DATE,
          ADD COLUMN knowledge_cutoff TIMESTAMPTZ
        """
    )
    op.execute(
        """
        ALTER TABLE equity_instrument
          ADD COLUMN master_confirmed_at TIMESTAMPTZ,
          ADD COLUMN current_master_version UUID
        """
    )
    op.execute(
        """
        CREATE TABLE equity_identifier_version (
          version_id UUID PRIMARY KEY,
          security_id BIGINT NOT NULL REFERENCES equity_instrument(security_id),
          exchange VARCHAR(4) NOT NULL CHECK (exchange IN ('SSE', 'SZSE', 'BSE')),
          symbol CHAR(6) NOT NULL CHECK (symbol ~ '^[0-9]{6}$'),
          identity_state VARCHAR(16) NOT NULL CHECK (identity_state IN ('PENDING', 'CONFIRMED')),
          effective_from DATE NOT NULL,
          effective_to DATE,
          known_from TIMESTAMPTZ NOT NULL,
          known_to TIMESTAMPTZ,
          effective_date_precision VARCHAR(24) NOT NULL CHECK (
            effective_date_precision IN ('OFFICIAL_DATE', 'OBSERVATION_DATE')
          ),
          source_batch_id UUID NOT NULL REFERENCES source_batch(source_batch_id),
          content_sha256 BYTEA NOT NULL,
          effective_range DATERANGE GENERATED ALWAYS AS
            (daterange(effective_from, effective_to, '[)')) STORED,
          knowledge_range TSTZRANGE GENERATED ALWAYS AS
            (tstzrange(known_from, known_to, '[)')) STORED,
          CHECK (effective_to IS NULL OR effective_to > effective_from),
          CHECK (known_to IS NULL OR known_to > known_from),
          EXCLUDE USING gist (
            security_id WITH =, effective_range WITH &&, knowledge_range WITH &&
          ),
          EXCLUDE USING gist (
            exchange WITH =, symbol WITH =,
            effective_range WITH &&, knowledge_range WITH &&
          )
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_equity_identifier_asof
        ON equity_identifier_version (exchange, symbol, effective_from DESC, known_from DESC)
        INCLUDE (security_id, effective_to, known_to, identity_state)
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_equity_identifier_current_open
        ON equity_identifier_version (exchange, symbol)
        WHERE effective_to IS NULL AND known_to IS NULL
        """
    )
    op.execute(
        """
        CREATE TABLE equity_name_version (
          version_id UUID PRIMARY KEY,
          security_id BIGINT NOT NULL REFERENCES equity_instrument(security_id),
          name VARCHAR(200) NOT NULL CHECK (btrim(name) <> ''),
          effective_from DATE NOT NULL,
          effective_to DATE,
          known_from TIMESTAMPTZ NOT NULL,
          known_to TIMESTAMPTZ,
          effective_date_precision VARCHAR(24) NOT NULL CHECK (
            effective_date_precision IN ('OFFICIAL_DATE', 'OBSERVATION_DATE')
          ),
          source_batch_id UUID NOT NULL REFERENCES source_batch(source_batch_id),
          content_sha256 BYTEA NOT NULL,
          effective_range DATERANGE GENERATED ALWAYS AS
            (daterange(effective_from, effective_to, '[)')) STORED,
          knowledge_range TSTZRANGE GENERATED ALWAYS AS
            (tstzrange(known_from, known_to, '[)')) STORED,
          CHECK (effective_to IS NULL OR effective_to > effective_from),
          CHECK (known_to IS NULL OR known_to > known_from),
          EXCLUDE USING gist (
            security_id WITH =, effective_range WITH &&, knowledge_range WITH &&
          )
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_equity_name_current_prefix
        ON equity_name_version (lower(name) text_pattern_ops)
        WHERE known_to IS NULL
        """
    )
    op.execute(
        """
        CREATE TABLE equity_listing_status_version (
          version_id UUID PRIMARY KEY,
          security_id BIGINT NOT NULL REFERENCES equity_instrument(security_id),
          status VARCHAR(24) NOT NULL CHECK (status IN ('LISTED', 'SUSPENDED', 'DELISTED')),
          listed_on DATE,
          delisted_on DATE,
          effective_from DATE NOT NULL,
          effective_to DATE,
          known_from TIMESTAMPTZ NOT NULL,
          known_to TIMESTAMPTZ,
          effective_date_precision VARCHAR(24) NOT NULL CHECK (
            effective_date_precision IN ('OFFICIAL_DATE', 'OBSERVATION_DATE')
          ),
          evidence_kind VARCHAR(24) NOT NULL CHECK (
            evidence_kind IN (
              'CATALOG', 'EXPLICIT_LISTING', 'EXPLICIT_SUSPENSION',
              'EXPLICIT_RESUMPTION', 'EXPLICIT_DELISTING', 'OFFICIAL_CORRECTION'
            )
          ),
          source_batch_id UUID NOT NULL REFERENCES source_batch(source_batch_id),
          content_sha256 BYTEA NOT NULL,
          effective_range DATERANGE GENERATED ALWAYS AS
            (daterange(effective_from, effective_to, '[)')) STORED,
          knowledge_range TSTZRANGE GENERATED ALWAYS AS
            (tstzrange(known_from, known_to, '[)')) STORED,
          CHECK (effective_to IS NULL OR effective_to > effective_from),
          CHECK (known_to IS NULL OR known_to > known_from),
          CHECK (delisted_on IS NULL OR listed_on IS NULL OR delisted_on >= listed_on),
          CHECK (
            status <> 'DELISTED'
            OR evidence_kind IN ('EXPLICIT_DELISTING', 'OFFICIAL_CORRECTION')
          ),
          EXCLUDE USING gist (
            security_id WITH =, effective_range WITH &&, knowledge_range WITH &&
          )
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_equity_listing_status_asof
        ON equity_listing_status_version (security_id, effective_from DESC, known_from DESC)
        INCLUDE (status, effective_to, known_to)
        """
    )
    op.execute(
        """
        CREATE TABLE equity_master_snapshot (
          snapshot_id UUID PRIMARY KEY,
          exchange VARCHAR(4) NOT NULL CHECK (exchange IN ('SSE', 'SZSE', 'BSE')),
          snapshot_kind VARCHAR(32) NOT NULL CHECK (snapshot_kind IN ('CATALOG', 'LIFECYCLE')),
          target_date DATE NOT NULL,
          source_batch_id UUID NOT NULL UNIQUE REFERENCES source_batch(source_batch_id),
          observed_at TIMESTAMPTZ NOT NULL,
          row_count INTEGER NOT NULL CHECK (row_count >= 0),
          schema_fingerprint CHAR(64) NOT NULL CHECK (schema_fingerprint ~ '^[0-9a-f]{64}$'),
          completeness VARCHAR(16) NOT NULL CHECK (
            completeness IN ('COMPLETE', 'PARTIAL', 'REJECTED')
          ),
          quality_status VARCHAR(16) NOT NULL CHECK (
            quality_status IN ('passed', 'warned', 'rejected')
          ),
          business_sha256 BYTEA NOT NULL
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_equity_master_snapshot_lookup
        ON equity_master_snapshot (exchange, snapshot_kind, observed_at DESC)
        """
    )
    op.execute(
        """
        CREATE TABLE equity_master_snapshot_member (
          snapshot_id UUID NOT NULL REFERENCES equity_master_snapshot(snapshot_id),
          row_ordinal INTEGER NOT NULL CHECK (row_ordinal > 0),
          exchange VARCHAR(4) NOT NULL CHECK (exchange IN ('SSE', 'SZSE', 'BSE')),
          symbol CHAR(6) NOT NULL CHECK (symbol ~ '^[0-9]{6}$'),
          name VARCHAR(200),
          listed_on DATE,
          candidate_status VARCHAR(24),
          candidate_status_date DATE,
          effective_date_precision VARCHAR(24) NOT NULL CHECK (
            effective_date_precision IN ('OFFICIAL_DATE', 'OBSERVATION_DATE')
          ),
          security_id BIGINT REFERENCES equity_instrument(security_id),
          resolution_status VARCHAR(16) NOT NULL CHECK (
            resolution_status IN ('resolved', 'pending', 'conflict', 'rejected')
          ),
          content_sha256 BYTEA NOT NULL,
          PRIMARY KEY (snapshot_id, row_ordinal),
          UNIQUE (snapshot_id, exchange, symbol)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE equity_presence_anomaly (
          anomaly_id UUID PRIMARY KEY,
          security_id BIGINT NOT NULL REFERENCES equity_instrument(security_id),
          exchange VARCHAR(4) NOT NULL CHECK (exchange IN ('SSE', 'SZSE', 'BSE')),
          symbol CHAR(6) NOT NULL CHECK (symbol ~ '^[0-9]{6}$'),
          first_missing_snapshot_id UUID NOT NULL REFERENCES equity_master_snapshot(snapshot_id),
          last_missing_snapshot_id UUID NOT NULL REFERENCES equity_master_snapshot(snapshot_id),
          consecutive_count INTEGER NOT NULL CHECK (consecutive_count > 0),
          status VARCHAR(16) NOT NULL CHECK (status IN ('open', 'resolved')),
          resolved_at TIMESTAMPTZ
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_equity_presence_anomaly_open
        ON equity_presence_anomaly (security_id)
        WHERE status = 'open'
        """
    )
    op.execute(
        """
        CREATE TABLE equity_identity_quarantine (
          issue_id UUID PRIMARY KEY,
          snapshot_id UUID NOT NULL REFERENCES equity_master_snapshot(snapshot_id),
          row_ordinal INTEGER NOT NULL,
          conflict_code VARCHAR(64) NOT NULL,
          candidate_json JSONB NOT NULL,
          related_security_ids BIGINT[] NOT NULL DEFAULT '{}',
          status VARCHAR(16) NOT NULL CHECK (status IN ('open', 'resolved', 'dismissed')),
          reviewed_by VARCHAR(128),
          reviewed_at TIMESTAMPTZ,
          resolution VARCHAR(64),
          FOREIGN KEY (snapshot_id, row_ordinal)
            REFERENCES equity_master_snapshot_member(snapshot_id, row_ordinal)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_equity_identity_quarantine_open
        ON equity_identity_quarantine (snapshot_id, row_ordinal)
        WHERE status = 'open'
        """
    )
    op.execute(
        """
        CREATE TABLE dataset_publication_component (
          aggregate_publication_id UUID NOT NULL REFERENCES dataset_publication(publication_id),
          component_partition_key VARCHAR(240) NOT NULL,
          component_data_version UUID NOT NULL,
          PRIMARY KEY (aggregate_publication_id, component_partition_key)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE data_quality_issue (
          issue_id UUID PRIMARY KEY,
          run_id UUID NOT NULL REFERENCES sync_run(run_id),
          partition_key VARCHAR(240) NOT NULL,
          source_batch_id UUID REFERENCES source_batch(source_batch_id),
          snapshot_id UUID REFERENCES equity_master_snapshot(snapshot_id),
          rule_code VARCHAR(64) NOT NULL,
          severity VARCHAR(16) NOT NULL CHECK (severity IN ('warn', 'error')),
          sample_json JSONB,
          status VARCHAR(16) NOT NULL CHECK (status IN ('open', 'resolved')),
          created_at TIMESTAMPTZ NOT NULL,
          resolved_at TIMESTAMPTZ
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_data_quality_issue_open
        ON data_quality_issue (run_id, partition_key, created_at DESC)
        WHERE status = 'open'
        """
    )


def _seed_legacy_pending_identities() -> None:
    """为 P0 行情创建受限 PENDING 标识，避免新解析器回退到当前列。"""
    bind = op.get_bind()
    op.execute(
        """
        INSERT INTO sync_run (
          run_id, capability, mode, request_key, target_date, status,
          requested_at, started_at, finished_at, created_at
        ) VALUES (
          '00000000-0000-0000-0000-000000000002',
          'equity.master.legacy.seed', 'legacy', 'equity-master-legacy-seed', NULL, 'succeeded',
          CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
        )
        """
    )
    op.execute(
        """
        INSERT INTO sync_partition (
          run_id, partition_key, status, attempt, updated_at
        ) VALUES (
          '00000000-0000-0000-0000-000000000002',
          'legacy:equity-master-seed', 'succeeded', 1, CURRENT_TIMESTAMP
        )
        """
    )
    op.execute(
        """
        INSERT INTO source_batch (
          source_batch_id, provider_id, capability, payload_sha256, raw_uri,
          observed_at, created_at, run_id, partition_key, observation_seq,
          upstream_source, adapter_version, schema_fingerprint
        ) VALUES (
          '00000000-0000-0000-0000-000000000003',
          'legacy-migration', 'equity.master.legacy.seed', repeat('0', 64),
          's3://legacy/unavailable/equity-master-seed.json', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP,
          '00000000-0000-0000-0000-000000000002', 'legacy:equity-master-seed', 1,
          'legacy-migration', 'legacy-unversioned', repeat('0', 64)
        )
        """
    )
    rows = bind.execute(
        text("SELECT security_id, exchange, symbol, created_at FROM equity_instrument")
    ).mappings()
    for row in rows:
        bind.execute(
            text(
                """
                INSERT INTO equity_identifier_version (
                  version_id, security_id, exchange, symbol, identity_state,
                  effective_from, effective_to, known_from, known_to,
                  effective_date_precision, source_batch_id, content_sha256
                ) VALUES (
                  :version_id, :security_id, :exchange, :symbol, 'PENDING',
                  (:created_at AT TIME ZONE 'Asia/Shanghai')::date, NULL,
                  :created_at, NULL, 'OBSERVATION_DATE', :source_batch_id, :content_sha256
                )
                """
            ),
            {
                "version_id": uuid4(),
                "security_id": row["security_id"],
                "exchange": row["exchange"],
                "symbol": row["symbol"],
                "created_at": row["created_at"],
                "source_batch_id": _LEGACY_MASTER_SEED_BATCH_ID,
                "content_sha256": bytes(32),
            },
        )
