"""创建股票中心全量回填父计划与不可变 child 规格。

Revision ID: 202607300016
Revises: 202607300015
Create Date: 2026-07-30
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "202607300016"
down_revision = "202607300015"
branch_labels = None
depends_on = None

_IMMUTABLE_TABLES = (
    "equity_backfill_plan",
    "equity_backfill_plan_identity",
    "equity_backfill_plan_source",
    "equity_backfill_plan_page",
    "equity_backfill_plan_seal",
    "equity_backfill_child_spec",
    "equity_backfill_partition_checkpoint",
    "equity_backfill_child_result",
)


def upgrade() -> None:
    """显式创建权威计划账本；任何预存同名或半成品结构都会使迁移失败。"""
    op.create_table(
        "equity_backfill_plan",
        sa.Column("plan_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("campaign_key", sa.String(length=128), nullable=False),
        sa.Column("plan_version", sa.SmallInteger(), nullable=False),
        sa.Column("request_hash", sa.CHAR(length=64), nullable=False),
        sa.Column(
            "aggregate_publication_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "aggregate_data_version",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "aggregate_components_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "lifecycle_publications_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "reference_bundle_publication_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "reference_bundle_data_version",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "reference_manifest_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("reference_manifest_hash", sa.CHAR(length=64), nullable=False),
        sa.Column("snapshot_observed_on", sa.Date(), nullable=False),
        sa.Column("market_as_of", sa.Date(), nullable=False),
        sa.Column("known_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("roster_hash", sa.CHAR(length=64), nullable=False),
        sa.Column("roster_count", sa.Integer(), nullable=False),
        sa.Column("source_evidence_hash", sa.CHAR(length=64), nullable=False),
        sa.Column(
            "exclusions_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("child_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "plan_version = 1",
            name="ck_equity_backfill_plan_version",
        ),
        sa.CheckConstraint(
            "length(request_hash) = 64",
            name="ck_equity_backfill_plan_request_hash",
        ),
        sa.CheckConstraint(
            "length(roster_hash) = 64",
            name="ck_equity_backfill_plan_roster_hash",
        ),
        sa.CheckConstraint(
            "length(reference_manifest_hash) = 64",
            name="ck_equity_backfill_plan_reference_manifest_hash",
        ),
        sa.CheckConstraint(
            "length(source_evidence_hash) = 64",
            name="ck_equity_backfill_plan_source_evidence_hash",
        ),
        sa.CheckConstraint(
            "roster_count > 0",
            name="ck_equity_backfill_plan_roster_count",
        ),
        sa.CheckConstraint(
            "child_count > 0",
            name="ck_equity_backfill_plan_child_count",
        ),
        sa.CheckConstraint(
            "market_as_of <= snapshot_observed_on",
            name="ck_equity_backfill_plan_date_boundaries",
        ),
        sa.ForeignKeyConstraint(
            ["aggregate_publication_id"],
            ["dataset_publication.publication_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["aggregate_data_version"],
            ["dataset_publication.data_version"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["reference_bundle_publication_id"],
            ["dataset_publication.publication_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["reference_bundle_data_version"],
            ["dataset_publication.data_version"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("plan_id"),
        sa.UniqueConstraint(
            "campaign_key",
            name="uq_equity_backfill_plan_campaign",
        ),
        comment="股票中心全量回填的不可变父计划；数据库是唯一权威。",
    )
    op.create_table(
        "equity_backfill_plan_state",
        sa.Column(
            "plan_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("current_phase", sa.String(length=48), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column(
            "last_error_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "audit_summary_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('BUILDING','HELD','RUNNING','SUCCEEDED','PARTIAL','FAILED','BLOCKED')",
            name="ck_equity_backfill_plan_state_status",
        ),
        sa.CheckConstraint(
            "revision >= 1",
            name="ck_equity_backfill_plan_state_revision",
        ),
        sa.ForeignKeyConstraint(
            ["plan_id"],
            ["equity_backfill_plan.plan_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("plan_id"),
        comment="股票中心回填父计划的可恢复状态与最终机器审计摘要。",
    )
    op.create_table(
        "equity_backfill_plan_identity",
        sa.Column("plan_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column(
            "identifier_version_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("security_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "instrument_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("exchange", sa.String(length=4), nullable=False),
        sa.Column("symbol", sa.String(length=6), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column("known_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("known_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "effective_date_precision",
            sa.String(length=24),
            nullable=False,
        ),
        sa.CheckConstraint(
            "exchange IN ('SSE','SZSE','BSE')",
            name="ck_equity_backfill_identity_exchange",
        ),
        sa.CheckConstraint(
            "symbol ~ '^[0-9]{6}$'",
            name="ck_equity_backfill_identity_symbol",
        ),
        sa.CheckConstraint(
            "effective_date_precision IN ('OFFICIAL_DATE','OBSERVATION_DATE')",
            name="ck_equity_backfill_identity_precision",
        ),
        sa.CheckConstraint(
            "effective_to IS NULL OR effective_to > effective_from",
            name="ck_equity_backfill_identity_effective_range",
        ),
        sa.CheckConstraint(
            "known_to IS NULL OR known_to > known_from",
            name="ck_equity_backfill_identity_known_range",
        ),
        sa.ForeignKeyConstraint(
            ["plan_id"],
            ["equity_backfill_plan.plan_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("plan_id", "ordinal"),
        sa.UniqueConstraint(
            "plan_id",
            "identifier_version_id",
            name="uq_equity_backfill_identity_version",
        ),
        comment="父计划创建时可见的完整 CONFIRMED A 股身份名单。",
    )
    op.create_index(
        "ix_equity_backfill_identity_security",
        "equity_backfill_plan_identity",
        ["plan_id", "security_id", "identifier_version_id"],
        unique=False,
    )
    op.create_table(
        "equity_backfill_plan_source",
        sa.Column("plan_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("dataset_code", sa.String(length=160), nullable=False),
        sa.Column("source_kind", sa.String(length=24), nullable=False),
        sa.Column(
            "publication_dataset_code",
            sa.String(length=160),
            nullable=False,
        ),
        sa.Column(
            "source_snapshot_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("source_snapshot_hash", sa.CHAR(length=64), nullable=False),
        sa.Column("earliest_date", sa.Date(), nullable=True),
        sa.Column(
            "earliest_date_method",
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column("evidence_ref", sa.Text(), nullable=False),
        sa.Column("evidence_sha256", sa.CHAR(length=64), nullable=False),
        sa.Column("source_contract_hash", sa.CHAR(length=64), nullable=False),
        sa.Column(
            "evidence_observed_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "expected_provider_id",
            sa.String(length=120),
            nullable=False,
        ),
        sa.Column(
            "expected_capability",
            sa.String(length=160),
            nullable=False,
        ),
        sa.Column(
            "expected_upstream_source",
            sa.String(length=160),
            nullable=False,
        ),
        sa.Column(
            "expected_adapter_version",
            sa.String(length=96),
            nullable=False,
        ),
        sa.Column(
            "expected_schema_fingerprint",
            sa.CHAR(length=64),
            nullable=False,
        ),
        sa.Column(
            "supported_exchanges_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "internal_executor_code",
            sa.String(length=256),
            nullable=True,
        ),
        sa.Column(
            "input_contract_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("input_contract_hash", sa.CHAR(length=64), nullable=False),
        sa.Column(
            "methodology_code",
            sa.String(length=160),
            nullable=False,
        ),
        sa.Column("methodology_version", sa.Integer(), nullable=False),
        sa.Column("mapping_version", sa.String(length=96), nullable=False),
        sa.CheckConstraint(
            "length(source_snapshot_hash) = 64",
            name="ck_equity_backfill_source_snapshot_hash",
        ),
        sa.CheckConstraint(
            "length(evidence_sha256) = 64",
            name="ck_equity_backfill_source_evidence_hash",
        ),
        sa.CheckConstraint(
            "length(source_contract_hash) = 64",
            name="ck_equity_backfill_source_contract_hash",
        ),
        sa.CheckConstraint(
            "length(expected_schema_fingerprint) = 64",
            name="ck_equity_backfill_source_schema_hash",
        ),
        sa.CheckConstraint(
            "methodology_version >= 1",
            name="ck_equity_backfill_source_methodology_version",
        ),
        sa.CheckConstraint(
            "source_kind IN ('EXTERNAL_PROVIDER','INTERNAL_EXECUTOR')",
            name="ck_equity_backfill_source_kind",
        ),
        sa.CheckConstraint(
            "(source_kind = 'EXTERNAL_PROVIDER' AND internal_executor_code IS NULL) "
            "OR (source_kind = 'INTERNAL_EXECUTOR' "
            "AND internal_executor_code IS NOT NULL)",
            name="ck_equity_backfill_source_executor",
        ),
        sa.CheckConstraint(
            "length(input_contract_hash) = 64",
            name="ck_equity_backfill_source_input_hash",
        ),
        sa.ForeignKeyConstraint(
            ["plan_id"],
            ["equity_backfill_plan.plan_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("plan_id", "dataset_code"),
        comment="每数据集来源边界、adapter/schema/mapping 与方法学冻结证据。",
    )
    op.create_table(
        "equity_backfill_plan_page",
        sa.Column("plan_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("first_ordinal", sa.Integer(), nullable=False),
        sa.Column("last_ordinal", sa.Integer(), nullable=False),
        sa.Column("child_count", sa.SmallInteger(), nullable=False),
        sa.Column("payload_bytes", sa.Integer(), nullable=False),
        sa.Column("page_hash", sa.CHAR(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "page_number >= 1",
            name="ck_equity_backfill_page_number",
        ),
        sa.CheckConstraint(
            "child_count >= 1 AND child_count <= 1000",
            name="ck_equity_backfill_page_child_count",
        ),
        sa.CheckConstraint(
            "last_ordinal >= first_ordinal AND child_count = last_ordinal - first_ordinal + 1",
            name="ck_equity_backfill_page_ordinal_range",
        ),
        sa.CheckConstraint(
            "payload_bytes > 0 AND payload_bytes <= 8388608",
            name="ck_equity_backfill_page_payload_bytes",
        ),
        sa.CheckConstraint(
            "length(page_hash) = 64",
            name="ck_equity_backfill_page_hash",
        ),
        sa.ForeignKeyConstraint(
            ["plan_id"],
            ["equity_backfill_plan.plan_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("plan_id", "page_number"),
        sa.UniqueConstraint(
            "plan_id",
            "first_ordinal",
            name="uq_equity_backfill_page_first_ordinal",
        ),
        comment="回填 child 规格的确定性分页摘要；seal 前不得提交执行。",
    )
    op.create_table(
        "equity_backfill_plan_seal",
        sa.Column("plan_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("page_count", sa.Integer(), nullable=False),
        sa.Column("child_count", sa.Integer(), nullable=False),
        sa.Column("topology_hash", sa.CHAR(length=64), nullable=False),
        sa.Column("page_roster_hash", sa.CHAR(length=64), nullable=False),
        sa.Column("sealed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "page_count >= 1",
            name="ck_equity_backfill_seal_page_count",
        ),
        sa.CheckConstraint(
            "child_count >= 1",
            name="ck_equity_backfill_seal_child_count",
        ),
        sa.CheckConstraint(
            "length(topology_hash) = 64",
            name="ck_equity_backfill_seal_topology_hash",
        ),
        sa.CheckConstraint(
            "length(page_roster_hash) = 64",
            name="ck_equity_backfill_seal_page_roster_hash",
        ),
        sa.ForeignKeyConstraint(
            ["plan_id"],
            ["equity_backfill_plan.plan_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("plan_id"),
        comment="存在且重算一致时父计划才允许从 BUILDING 进入 HELD。",
    )
    op.create_table(
        "equity_backfill_child_spec",
        sa.Column(
            "child_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("plan_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("phase", sa.String(length=48), nullable=False),
        sa.Column("requirement", sa.String(length=24), nullable=False),
        sa.Column("child_key", sa.CHAR(length=64), nullable=False),
        sa.Column("identity_ordinal", sa.Integer(), nullable=True),
        sa.Column("window_from", sa.Date(), nullable=True),
        sa.Column("window_to", sa.Date(), nullable=True),
        sa.Column(
            "targets_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "intents_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "dependency_keys_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "completion_dependency_keys_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "source_hashes_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "submission_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("request_prefix", sa.String(length=96), nullable=False),
        sa.Column("target_count", sa.SmallInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "phase IN ('RAW_SECURITY','CORPORATE_ACTION',"
            "'DERIVED_SECURITY','GLOBAL_EVENT','DISCOVERY_BUILD')",
            name="ck_equity_backfill_child_phase",
        ),
        sa.CheckConstraint(
            "requirement IN ('BASE_REQUIRED','OPTIONAL','FINAL_REQUIRED')",
            name="ck_equity_backfill_child_requirement",
        ),
        sa.CheckConstraint(
            "length(child_key) = 64",
            name="ck_equity_backfill_child_key",
        ),
        sa.CheckConstraint(
            "window_to IS NULL OR (window_from IS NOT NULL AND window_to >= window_from)",
            name="ck_equity_backfill_child_window",
        ),
        sa.CheckConstraint(
            "target_count > 0 AND target_count <= 100",
            name="ck_equity_backfill_child_target_count",
        ),
        sa.ForeignKeyConstraint(
            ["plan_id"],
            ["equity_backfill_plan.plan_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["plan_id", "identity_ordinal"],
            [
                "equity_backfill_plan_identity.plan_id",
                "equity_backfill_plan_identity.ordinal",
            ],
            name="fk_equity_backfill_child_identity",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("child_id"),
        sa.UniqueConstraint(
            "plan_id",
            "ordinal",
            name="uq_equity_backfill_child_ordinal",
        ),
        sa.UniqueConstraint(
            "plan_id",
            "child_key",
            name="uq_equity_backfill_child_key",
        ),
        sa.UniqueConstraint(
            "submission_id",
            name="uq_equity_backfill_child_submission",
        ),
        comment="完整冻结、随后按阶段提交的不可变 command 规格。",
    )
    op.create_index(
        "ix_equity_backfill_child_phase",
        "equity_backfill_child_spec",
        ["plan_id", "phase", "ordinal"],
        unique=False,
    )
    op.create_table(
        "equity_backfill_child_state",
        sa.Column(
            "child_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column(
            "command_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column("resume_count", sa.Integer(), nullable=False),
        sa.Column(
            "last_error_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "audit_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('HELD','SUBMITTING','SUBMITTED','RUNNING','SUCCEEDED',"
            "'PARTIAL','FAILED','CANCELLED','BLOCKED')",
            name="ck_equity_backfill_child_state_status",
        ),
        sa.CheckConstraint(
            "(status IN ('HELD','SUBMITTING') AND command_id IS NULL) "
            "OR status = 'BLOCKED' "
            "OR (status NOT IN ('HELD','SUBMITTING','BLOCKED') "
            "AND command_id IS NOT NULL)",
            name="ck_equity_backfill_child_state_command",
        ),
        sa.CheckConstraint(
            "(command_id IS NULL AND submitted_at IS NULL) "
            "OR (command_id IS NOT NULL AND submitted_at IS NOT NULL)",
            name="ck_equity_backfill_child_state_submitted",
        ),
        sa.CheckConstraint(
            "(status IN ('SUCCEEDED','PARTIAL','FAILED','CANCELLED','BLOCKED') "
            "AND finished_at IS NOT NULL) "
            "OR (status NOT IN ('SUCCEEDED','PARTIAL','FAILED','CANCELLED','BLOCKED') "
            "AND finished_at IS NULL)",
            name="ck_equity_backfill_child_state_finished",
        ),
        sa.CheckConstraint(
            "resume_count >= 0",
            name="ck_equity_backfill_child_resume_count",
        ),
        sa.ForeignKeyConstraint(
            ["child_id"],
            ["equity_backfill_child_spec.child_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["command_id"],
            ["data_operation_command.command_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("child_id"),
        sa.UniqueConstraint("command_id"),
        comment="可恢复 child 状态；规格、来源、窗口和身份均不在此表修改。",
    )
    op.create_index(
        "ix_equity_backfill_child_state_status",
        "equity_backfill_child_state",
        ["status", "updated_at"],
        unique=False,
    )
    op.create_table(
        "equity_backfill_partition_checkpoint",
        sa.Column("checkpoint_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("child_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("command_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("target_index", sa.SmallInteger(), nullable=False),
        sa.Column("dataset_code", sa.String(length=160), nullable=False),
        sa.Column("partition_key", sa.String(length=256), nullable=False),
        sa.Column("window_from", sa.Date(), nullable=False),
        sa.Column("window_to", sa.Date(), nullable=False),
        sa.Column("checkpoint_kind", sa.String(length=32), nullable=False),
        sa.Column("publication_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("data_version", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("release_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("coverage_version", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("publication_kind", sa.String(length=32), nullable=False),
        sa.Column("record_count", sa.Integer(), nullable=False),
        sa.Column(
            "source_batch_ids_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("source_batch_hash", sa.CHAR(length=64), nullable=False),
        sa.Column("output_hash", sa.CHAR(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "target_index >= 0 AND target_index < 100",
            name="ck_equity_backfill_partition_target",
        ),
        sa.CheckConstraint(
            "window_to >= window_from",
            name="ck_equity_backfill_partition_window",
        ),
        sa.CheckConstraint(
            "checkpoint_kind IN ('DATA_VERSION','BAR_COVERAGE_VERSION')",
            name="ck_equity_backfill_partition_kind",
        ),
        sa.CheckConstraint(
            "(checkpoint_kind = 'DATA_VERSION' AND coverage_version IS NULL) "
            "OR (checkpoint_kind = 'BAR_COVERAGE_VERSION' "
            "AND coverage_version IS NOT NULL)",
            name="ck_equity_backfill_partition_coverage",
        ),
        sa.CheckConstraint(
            "publication_kind IN ('DATA','ZERO_RECORD_COVERAGE')",
            name="ck_equity_backfill_partition_publication_kind",
        ),
        sa.CheckConstraint(
            "(publication_kind = 'DATA' AND record_count >= 0) "
            "OR (publication_kind = 'ZERO_RECORD_COVERAGE' AND record_count = 0)",
            name="ck_equity_backfill_partition_record_count",
        ),
        sa.CheckConstraint(
            "length(source_batch_hash) = 64",
            name="ck_equity_backfill_partition_source_hash",
        ),
        sa.CheckConstraint(
            "length(output_hash) = 64",
            name="ck_equity_backfill_partition_output_hash",
        ),
        sa.ForeignKeyConstraint(
            ["child_id"],
            ["equity_backfill_child_spec.child_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["data_operation_run.run_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["command_id"],
            ["data_operation_command.command_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["publication_id"],
            ["dataset_publication.publication_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["data_version"],
            ["dataset_publication.data_version"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["release_id"],
            ["dataset_release.release_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("checkpoint_id"),
        sa.UniqueConstraint(
            "child_id",
            "target_index",
            "partition_key",
            name="uq_equity_backfill_partition_checkpoint",
        ),
        comment="长历史 child 的不可变成功分区、精确输出与崩溃恢复水位。",
    )
    op.create_index(
        "ix_equity_backfill_partition_roster",
        "equity_backfill_partition_checkpoint",
        ["child_id", "target_index", "window_from", "window_to"],
        unique=False,
    )
    op.create_table(
        "equity_backfill_child_result",
        sa.Column("result_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("child_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("command_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("target_index", sa.SmallInteger(), nullable=False),
        sa.Column("terminal_status", sa.String(length=16), nullable=False),
        sa.Column(
            "input_manifest_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("input_manifest_hash", sa.CHAR(length=64), nullable=False),
        sa.Column(
            "output_manifest_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("output_manifest_hash", sa.CHAR(length=64), nullable=False),
        sa.Column(
            "audit_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("audit_hash", sa.CHAR(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "terminal_status IN ('SUCCEEDED','PARTIAL','FAILED','CANCELLED')",
            name="ck_equity_backfill_child_result_status",
        ),
        sa.CheckConstraint(
            "target_index >= 0 AND target_index < 100",
            name="ck_equity_backfill_child_result_target",
        ),
        sa.CheckConstraint(
            "length(input_manifest_hash) = 64",
            name="ck_equity_backfill_child_result_input_hash",
        ),
        sa.CheckConstraint(
            "length(output_manifest_hash) = 64",
            name="ck_equity_backfill_child_result_output_hash",
        ),
        sa.CheckConstraint(
            "length(audit_hash) = 64",
            name="ck_equity_backfill_child_result_audit_hash",
        ),
        sa.ForeignKeyConstraint(
            ["child_id"],
            ["equity_backfill_child_spec.child_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["command_id"],
            ["data_operation_command.command_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["data_operation_run.run_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("result_id"),
        sa.UniqueConstraint(
            "child_id",
            "command_id",
            "target_index",
            name="uq_equity_backfill_child_result_attempt_target",
        ),
        sa.UniqueConstraint(
            "run_id",
            name="uq_equity_backfill_child_result_run",
        ),
        comment="每个 child target run 终态一次性写入的精确输入、输出与审计事实。",
    )
    op.create_index(
        "ix_equity_backfill_child_result_command",
        "equity_backfill_child_result",
        ["command_id"],
        unique=False,
    )

    op.execute(
        """
        CREATE FUNCTION reject_equity_backfill_immutable_change()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
          RAISE EXCEPTION 'equity backfill frozen specification is immutable'
            USING ERRCODE = '55000';
        END;
        $$;
        """
    )
    for table_name in _IMMUTABLE_TABLES:
        op.execute(
            f"""
            CREATE TRIGGER trg_{table_name}_immutable
            BEFORE UPDATE OR DELETE ON {table_name}
            FOR EACH ROW
            EXECUTE FUNCTION reject_equity_backfill_immutable_change();
            """
        )


def downgrade() -> None:
    """仅在账本为空时完整回退；已有计划则拒绝伪造成功的版本倒退。"""
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM equity_backfill_plan LIMIT 1) THEN
            RAISE EXCEPTION
              'cannot downgrade equity backfill plan schema while immutable plans exist'
              USING ERRCODE = '55000';
          END IF;
        END;
        $$;
        """
    )
    for table_name in reversed(_IMMUTABLE_TABLES):
        op.execute(f"DROP TRIGGER trg_{table_name}_immutable ON {table_name}")

    op.drop_index(
        "ix_equity_backfill_child_result_command",
        table_name="equity_backfill_child_result",
    )
    op.drop_table("equity_backfill_child_result")
    op.drop_index(
        "ix_equity_backfill_partition_roster",
        table_name="equity_backfill_partition_checkpoint",
    )
    op.drop_table("equity_backfill_partition_checkpoint")
    op.drop_index(
        "ix_equity_backfill_child_state_status",
        table_name="equity_backfill_child_state",
    )
    op.drop_table("equity_backfill_child_state")
    op.drop_index(
        "ix_equity_backfill_child_phase",
        table_name="equity_backfill_child_spec",
    )
    op.drop_table("equity_backfill_child_spec")
    op.drop_table("equity_backfill_plan_seal")
    op.drop_table("equity_backfill_plan_page")
    op.drop_table("equity_backfill_plan_source")
    op.drop_index(
        "ix_equity_backfill_identity_security",
        table_name="equity_backfill_plan_identity",
    )
    op.drop_table("equity_backfill_plan_identity")
    op.drop_table("equity_backfill_plan_state")
    op.drop_table("equity_backfill_plan")
    op.execute("DROP FUNCTION reject_equity_backfill_immutable_change()")
