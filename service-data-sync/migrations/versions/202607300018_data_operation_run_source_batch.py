"""创建控制面 run 与真实来源批次的不可变关联。

Revision ID: 202607300018
Revises: 202607300017
Create Date: 2026-07-30
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "202607300018"
down_revision = "202607300017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """创建追加式来源事实和可恢复引用生成账本，并由数据库约束状态迁移。"""
    op.add_column(
        "equity_backfill_partition_checkpoint",
        sa.Column(
            "coverage_versions_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )
    op.execute(
        """
        UPDATE equity_backfill_partition_checkpoint
        SET coverage_versions_json = CASE
          WHEN coverage_version IS NULL THEN '[]'::jsonb
          ELSE jsonb_build_array(coverage_version::text)
        END
        """
    )
    op.alter_column(
        "equity_backfill_partition_checkpoint",
        "coverage_versions_json",
        nullable=False,
    )
    op.drop_constraint(
        "ck_equity_backfill_partition_kind",
        "equity_backfill_partition_checkpoint",
        type_="check",
    )
    op.create_check_constraint(
        "ck_equity_backfill_partition_kind",
        "equity_backfill_partition_checkpoint",
        "checkpoint_kind IN "
        "('DATA_VERSION','BAR_COVERAGE_VERSION','EVENT_COVERAGE_VERSION')",
    )
    op.create_table(
        "data_operation_run_source_batch",
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_batch_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("linked_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["data_operation_run.run_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_batch_id"],
            ["source_batch.source_batch_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("run_id", "source_batch_id"),
        comment="控制面 run 与实际 SourceBatch 的不可变多对多事实；仅由 fencing 完成事务追加。",
    )
    op.create_index(
        "ix_data_operation_run_source_batch_source",
        "data_operation_run_source_batch",
        ["source_batch_id", "run_id"],
        unique=False,
    )
    op.create_table(
        "equity_reference_generation_attempt",
        sa.Column("attempt_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("campaign_key", sa.String(length=128), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("snapshot_observed_on", sa.Date(), nullable=False),
        sa.Column("market_as_of", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column(
            "bundle_publication_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column(
            "bundle_data_version",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column(
            "bundle_release_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column(
            "manifest_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("manifest_hash", sa.CHAR(length=64), nullable=True),
        sa.Column(
            "source_batch_ids_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("source_batch_hash", sa.CHAR(length=64), nullable=True),
        sa.Column(
            "last_error_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sealed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('BUILDING','SEALED','ROLLED_FORWARD','FAILED')",
            name="ck_equity_reference_attempt_status",
        ),
        sa.CheckConstraint(
            "attempt_no >= 1",
            name="ck_equity_reference_attempt_number",
        ),
        sa.CheckConstraint(
            "market_as_of <= snapshot_observed_on",
            name="ck_equity_reference_attempt_dates",
        ),
        sa.CheckConstraint(
            "(status = 'SEALED' AND bundle_publication_id IS NOT NULL "
            "AND bundle_data_version IS NOT NULL AND bundle_release_id IS NOT NULL "
            "AND manifest_json IS NOT NULL AND manifest_hash IS NOT NULL "
            "AND source_batch_ids_json IS NOT NULL AND source_batch_hash IS NOT NULL "
            "AND sealed_at IS NOT NULL) OR "
            "(status <> 'SEALED' AND bundle_publication_id IS NULL "
            "AND bundle_data_version IS NULL AND bundle_release_id IS NULL "
            "AND manifest_json IS NULL AND manifest_hash IS NULL "
            "AND source_batch_ids_json IS NULL AND source_batch_hash IS NULL "
            "AND sealed_at IS NULL)",
            name="ck_equity_reference_attempt_seal",
        ),
        sa.CheckConstraint(
            "manifest_hash IS NULL OR length(manifest_hash) = 64",
            name="ck_equity_reference_attempt_manifest_hash",
        ),
        sa.CheckConstraint(
            "source_batch_hash IS NULL OR length(source_batch_hash) = 64",
            name="ck_equity_reference_attempt_source_hash",
        ),
        sa.ForeignKeyConstraint(
            ["bundle_publication_id"],
            ["dataset_publication.publication_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["bundle_data_version"],
            ["dataset_publication.data_version"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["bundle_release_id"],
            ["dataset_release.release_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("attempt_id"),
        sa.UniqueConstraint(
            "campaign_key",
            "attempt_no",
            name="uq_equity_reference_attempt_number",
        ),
        comment="股票中心当前引用数据生成、跨日滚转与 canonical bundle 封印账本。",
    )
    op.create_index(
        "uq_equity_reference_attempt_building",
        "equity_reference_generation_attempt",
        ["campaign_key"],
        unique=True,
        postgresql_where=sa.text("status = 'BUILDING'"),
    )
    op.create_table(
        "equity_reference_generation_step",
        sa.Column("attempt_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ordinal", sa.SmallInteger(), nullable=False),
        sa.Column("step_key", sa.String(length=64), nullable=False),
        sa.Column("dataset_code", sa.String(length=160), nullable=False),
        sa.Column(
            "target_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("submission_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("command_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("retry_count", sa.SmallInteger(), nullable=False),
        sa.Column(
            "last_error_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "output_publications_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "source_batch_ids_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("output_hash", sa.CHAR(length=64), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "ordinal >= 1 AND ordinal <= 7",
            name="ck_equity_reference_step_ordinal",
        ),
        sa.CheckConstraint(
            "status IN ('HELD','SUBMITTED','RUNNING','SUCCEEDED','FAILED')",
            name="ck_equity_reference_step_status",
        ),
        sa.CheckConstraint(
            "retry_count >= 0 AND retry_count <= 3",
            name="ck_equity_reference_step_retry_count",
        ),
        sa.CheckConstraint(
            "(status = 'HELD' AND command_id IS NULL AND submitted_at IS NULL) OR "
            "(status <> 'HELD' AND command_id IS NOT NULL AND submitted_at IS NOT NULL)",
            name="ck_equity_reference_step_command",
        ),
        sa.CheckConstraint(
            "(status IN ('SUCCEEDED','FAILED') AND finished_at IS NOT NULL) OR "
            "(status NOT IN ('SUCCEEDED','FAILED') AND finished_at IS NULL)",
            name="ck_equity_reference_step_finished",
        ),
        sa.CheckConstraint(
            "(status = 'SUCCEEDED' AND output_publications_json IS NOT NULL "
            "AND source_batch_ids_json IS NOT NULL AND output_hash IS NOT NULL) OR "
            "(status <> 'SUCCEEDED' AND output_publications_json IS NULL "
            "AND source_batch_ids_json IS NULL AND output_hash IS NULL)",
            name="ck_equity_reference_step_output",
        ),
        sa.CheckConstraint(
            "output_hash IS NULL OR length(output_hash) = 64",
            name="ck_equity_reference_step_output_hash",
        ),
        sa.ForeignKeyConstraint(
            ["attempt_id"],
            ["equity_reference_generation_attempt.attempt_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["command_id"],
            ["data_operation_command.command_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("attempt_id", "ordinal"),
        sa.UniqueConstraint(
            "attempt_id",
            "step_key",
            name="uq_equity_reference_step_key",
        ),
        sa.UniqueConstraint(
            "submission_id",
            name="uq_equity_reference_step_submission",
        ),
        comment="股票中心引用 bundle 七个真实控制面命令的可恢复状态。",
    )
    op.create_index(
        "ix_equity_reference_step_status",
        "equity_reference_generation_step",
        ["attempt_id", "status", "ordinal"],
        unique=False,
    )
    op.execute(
        """
        CREATE FUNCTION reject_data_operation_run_source_batch_change()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
          RAISE EXCEPTION 'data operation run source batch is immutable'
            USING ERRCODE = '55000';
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_data_operation_run_source_batch_immutable
        BEFORE UPDATE OR DELETE ON data_operation_run_source_batch
        FOR EACH ROW
        EXECUTE FUNCTION reject_data_operation_run_source_batch_change();
        """
    )
    op.execute(
        """
        CREATE FUNCTION guard_equity_reference_attempt_change()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
          IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'equity reference generation attempt cannot be deleted'
              USING ERRCODE = '55000';
          END IF;
          IF OLD.status <> 'BUILDING' THEN
            RAISE EXCEPTION 'terminal equity reference generation attempt is immutable'
              USING ERRCODE = '55000';
          END IF;
          IF NEW.attempt_id <> OLD.attempt_id
             OR NEW.campaign_key <> OLD.campaign_key
             OR NEW.attempt_no <> OLD.attempt_no
             OR NEW.snapshot_observed_on <> OLD.snapshot_observed_on
             OR NEW.market_as_of <> OLD.market_as_of
             OR NEW.created_at <> OLD.created_at THEN
            RAISE EXCEPTION 'equity reference generation boundaries are immutable'
              USING ERRCODE = '55000';
          END IF;
          RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_equity_reference_attempt_guard
        BEFORE UPDATE OR DELETE ON equity_reference_generation_attempt
        FOR EACH ROW
        EXECUTE FUNCTION guard_equity_reference_attempt_change();
        """
    )
    op.execute(
        """
        CREATE FUNCTION guard_equity_reference_step_change()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
          IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'equity reference generation step cannot be deleted'
              USING ERRCODE = '55000';
          END IF;
          IF OLD.status IN ('SUCCEEDED', 'FAILED') THEN
            RAISE EXCEPTION 'terminal equity reference generation step is immutable'
              USING ERRCODE = '55000';
          END IF;
          IF NEW.attempt_id <> OLD.attempt_id
             OR NEW.ordinal <> OLD.ordinal
             OR NEW.step_key <> OLD.step_key
             OR NEW.dataset_code <> OLD.dataset_code
             OR NEW.target_json <> OLD.target_json
             OR NEW.submission_id <> OLD.submission_id THEN
            RAISE EXCEPTION 'equity reference generation step specification is immutable'
              USING ERRCODE = '55000';
          END IF;
          RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_equity_reference_step_guard
        BEFORE UPDATE OR DELETE ON equity_reference_generation_step
        FOR EACH ROW
        EXECUTE FUNCTION guard_equity_reference_step_change();
        """
    )


def downgrade() -> None:
    """仅在来源与引用账本均为空时删除结构，避免回退抹除真实执行证据。"""
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM data_operation_run_source_batch LIMIT 1)
             OR EXISTS (SELECT 1 FROM equity_reference_generation_attempt LIMIT 1) THEN
            RAISE EXCEPTION
              'cannot downgrade equity reference generation while evidence exists'
              USING ERRCODE = '55000';
          END IF;
        END;
        $$;
        """
    )
    op.execute(
        "DROP TRIGGER trg_equity_reference_step_guard "
        "ON equity_reference_generation_step"
    )
    op.execute("DROP FUNCTION guard_equity_reference_step_change()")
    op.execute(
        "DROP TRIGGER trg_equity_reference_attempt_guard "
        "ON equity_reference_generation_attempt"
    )
    op.execute("DROP FUNCTION guard_equity_reference_attempt_change()")
    op.drop_index(
        "ix_equity_reference_step_status",
        table_name="equity_reference_generation_step",
    )
    op.drop_table("equity_reference_generation_step")
    op.drop_index(
        "uq_equity_reference_attempt_building",
        table_name="equity_reference_generation_attempt",
    )
    op.drop_table("equity_reference_generation_attempt")
    op.execute(
        "DROP TRIGGER trg_data_operation_run_source_batch_immutable "
        "ON data_operation_run_source_batch"
    )
    op.execute("DROP FUNCTION reject_data_operation_run_source_batch_change()")
    op.drop_index(
        "ix_data_operation_run_source_batch_source",
        table_name="data_operation_run_source_batch",
    )
    op.drop_table("data_operation_run_source_batch")
    op.drop_constraint(
        "ck_equity_backfill_partition_kind",
        "equity_backfill_partition_checkpoint",
        type_="check",
    )
    op.create_check_constraint(
        "ck_equity_backfill_partition_kind",
        "equity_backfill_partition_checkpoint",
        "checkpoint_kind IN ('DATA_VERSION','BAR_COVERAGE_VERSION')",
    )
    op.drop_column(
        "equity_backfill_partition_checkpoint",
        "coverage_versions_json",
    )
