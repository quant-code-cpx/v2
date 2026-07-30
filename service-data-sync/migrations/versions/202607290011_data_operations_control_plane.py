# ruff: noqa: E501
"""创建数据运维控制面权威账本。

命令、运行、全局 slot、健康、计划和事件全部在 data-sync PostgreSQL 中持久化；Redis
不承担互斥或终态权威性。回退仅在不存在控制面记录时允许，避免误删审计与运行事实。

Revision ID: 202607290011
Revises: 202607290010
Create Date: 2026-07-29
"""

from __future__ import annotations

from alembic import op

revision = "202607290011"
down_revision = "202607290010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """创建控制面表、全局租约约束、稳定幂等键和必要查询索引。"""
    op.execute(
        """
        CREATE TABLE data_operation_idempotency (
          idempotency_id UUID PRIMARY KEY,
          operation VARCHAR(80) NOT NULL,
          idempotency_key VARCHAR(128) NOT NULL,
          request_hash VARCHAR(64) NOT NULL,
          resource_type VARCHAR(32) NOT NULL,
          resource_id UUID NOT NULL,
          response_json JSONB NOT NULL,
          created_at TIMESTAMPTZ NOT NULL,
          CONSTRAINT uq_data_operation_idempotency UNIQUE (operation, idempotency_key),
          CONSTRAINT ck_data_operation_idempotency_hash CHECK (length(request_hash) = 64)
        );
        CREATE TABLE data_operation_preflight (
          preflight_id UUID PRIMARY KEY,
          request_hash VARCHAR(64) NOT NULL,
          targets_json JSONB NOT NULL,
          result_json JSONB NOT NULL,
          expires_at TIMESTAMPTZ NOT NULL,
          created_at TIMESTAMPTZ NOT NULL,
          CONSTRAINT ck_data_operation_preflight_hash CHECK (length(request_hash) = 64)
        );
        CREATE TABLE data_operation_command (
          command_id UUID PRIMARY KEY,
          submission_id UUID NULL,
          status VARCHAR(24) NOT NULL,
          actor_ref VARCHAR(128) NOT NULL,
          actor_role VARCHAR(24) NOT NULL,
          reason TEXT NOT NULL,
          request_id VARCHAR(128) NOT NULL,
          retry_of_command_id UUID NULL REFERENCES data_operation_command(command_id),
          error_json JSONB NULL,
          requested_at TIMESTAMPTZ NOT NULL,
          started_at TIMESTAMPTZ NULL,
          finished_at TIMESTAMPTZ NULL,
          CONSTRAINT ck_data_operation_command_status CHECK (status IN ('QUEUED','RUNNING','CANCEL_REQUESTED','SUCCEEDED','PARTIAL','FAILED','CANCELLED','REJECTED'))
        );
        CREATE INDEX ix_data_operation_command_status_requested ON data_operation_command(status, requested_at);
        CREATE INDEX ix_data_operation_command_submission_id ON data_operation_command(submission_id);
        CREATE TABLE data_operation_run (
          run_id UUID PRIMARY KEY,
          command_id UUID NOT NULL REFERENCES data_operation_command(command_id) ON DELETE RESTRICT,
          target_index INTEGER NOT NULL,
          dataset_code VARCHAR(160) NOT NULL,
          mode VARCHAR(24) NOT NULL,
          target_json JSONB NOT NULL,
          source_snapshot JSONB NOT NULL,
          status VARCHAR(24) NOT NULL,
          queue_position INTEGER NULL,
          attempt INTEGER NOT NULL,
          completed_partitions INTEGER NOT NULL,
          total_partitions INTEGER NOT NULL,
          processed_records BIGINT NOT NULL,
          estimated_records BIGINT NULL,
          fencing_token BIGINT NULL,
          cancel_requested BOOLEAN NOT NULL,
          error_json JSONB NULL,
          quality_gate_json JSONB NOT NULL,
          requested_at TIMESTAMPTZ NOT NULL,
          started_at TIMESTAMPTZ NULL,
          finished_at TIMESTAMPTZ NULL,
          CONSTRAINT uq_data_operation_run_target_index UNIQUE(command_id, target_index),
          CONSTRAINT uq_data_operation_run_dataset UNIQUE(command_id, dataset_code),
          CONSTRAINT ck_data_operation_run_status CHECK (status IN ('QUEUED','RUNNING','CANCEL_REQUESTED','SUCCEEDED','PARTIAL','FAILED','CANCELLED','INTERRUPTED','SKIPPED'))
        );
        CREATE INDEX ix_data_operation_run_dispatch ON data_operation_run(status, requested_at);
        CREATE TABLE data_operation_partition (
          run_id UUID NOT NULL REFERENCES data_operation_run(run_id) ON DELETE RESTRICT,
          partition_key VARCHAR(240) NOT NULL,
          status VARCHAR(24) NOT NULL,
          attempt INTEGER NOT NULL,
          checkpoint_hash VARCHAR(64) NULL,
          checkpoint_kind VARCHAR(40) NULL,
          checkpoint_updated_at TIMESTAMPTZ NULL,
          error_json JSONB NULL,
          PRIMARY KEY(run_id, partition_key),
          CONSTRAINT ck_data_operation_partition_status CHECK (status IN ('QUEUED','RUNNING','SUCCEEDED','PARTIAL','FAILED','CANCELLED','INTERRUPTED','SKIPPED'))
        );
        CREATE TABLE data_operation_execution_slot (
          slot_key VARCHAR(16) PRIMARY KEY,
          state VARCHAR(16) NOT NULL,
          run_id UUID NULL,
          dataset_code VARCHAR(160) NULL,
          lease_until TIMESTAMPTZ NULL,
          heartbeat_at TIMESTAMPTZ NULL,
          fencing_token BIGINT NOT NULL,
          CONSTRAINT ck_data_operation_slot_global CHECK (slot_key = 'global'),
          CONSTRAINT ck_data_operation_slot_state CHECK (state IN ('IDLE','RUNNING','RECOVERING')),
          CONSTRAINT ck_data_operation_slot_fencing CHECK (fencing_token >= 0)
        );
        INSERT INTO data_operation_execution_slot(slot_key, state, run_id, dataset_code, lease_until, heartbeat_at, fencing_token)
        VALUES ('global', 'IDLE', NULL, NULL, NULL, NULL, 0);
        CREATE TABLE data_operation_event (
          event_id UUID PRIMARY KEY,
          resource_type VARCHAR(24) NOT NULL,
          resource_id UUID NOT NULL,
          action VARCHAR(80) NOT NULL,
          result VARCHAR(24) NOT NULL,
          actor_ref VARCHAR(128) NOT NULL,
          request_id VARCHAR(128) NOT NULL,
          error_json JSONB NULL,
          occurred_at TIMESTAMPTZ NOT NULL
        );
        CREATE INDEX ix_data_operation_event_resource_time ON data_operation_event(resource_type, resource_id, occurred_at);
        CREATE INDEX ix_data_operation_event_actor_time ON data_operation_event(actor_ref, occurred_at);
        CREATE TABLE data_operation_health_check (
          health_check_id UUID PRIMARY KEY,
          submission_id UUID NULL,
          status VARCHAR(24) NOT NULL,
          actor_ref VARCHAR(128) NOT NULL,
          actor_role VARCHAR(24) NOT NULL,
          reason TEXT NOT NULL,
          request_id VARCHAR(128) NOT NULL,
          error_json JSONB NULL,
          requested_at TIMESTAMPTZ NOT NULL,
          started_at TIMESTAMPTZ NULL,
          finished_at TIMESTAMPTZ NULL,
          CONSTRAINT ck_data_operation_health_check_status CHECK (status IN ('QUEUED','RUNNING','SUCCEEDED','PARTIAL','FAILED','CANCELLED','REJECTED'))
        );
        CREATE TABLE data_operation_health_check_target (
          health_check_id UUID NOT NULL REFERENCES data_operation_health_check(health_check_id),
          target_index INTEGER NOT NULL,
          dataset_code VARCHAR(160) NOT NULL,
          requested_data_version UUID NULL,
          resolved_data_version UUID NULL,
          status VARCHAR(24) NOT NULL,
          evaluation_id UUID NULL,
          error_json JSONB NULL,
          PRIMARY KEY(health_check_id, target_index),
          CONSTRAINT uq_data_operation_health_target_index UNIQUE(health_check_id, target_index),
          CONSTRAINT uq_data_operation_health_target_dataset UNIQUE(health_check_id, dataset_code)
        );
        CREATE TABLE data_operation_health_evaluation (
          evaluation_id UUID PRIMARY KEY,
          health_check_id UUID NULL,
          dataset_code VARCHAR(160) NOT NULL,
          data_version UUID NOT NULL,
          release_id UUID NOT NULL,
          policy_code VARCHAR(120) NOT NULL,
          policy_version INTEGER NOT NULL,
          status VARCHAR(24) NOT NULL,
          score INTEGER NULL,
          results_json JSONB NOT NULL,
          evaluated_at TIMESTAMPTZ NOT NULL
        );
        CREATE INDEX ix_data_operation_health_evaluation_dataset_time ON data_operation_health_evaluation(dataset_code, evaluated_at);
        CREATE TABLE data_operation_health_issue (
          issue_id UUID PRIMARY KEY,
          dataset_code VARCHAR(160) NOT NULL,
          rule_code VARCHAR(120) NOT NULL,
          dimension VARCHAR(24) NOT NULL,
          severity VARCHAR(16) NOT NULL,
          status VARCHAR(24) NOT NULL,
          first_detected_at TIMESTAMPTZ NOT NULL,
          last_detected_at TIMESTAMPTZ NOT NULL,
          affected_count BIGINT NULL,
          evidence_summary VARCHAR(500) NULL
        );
        CREATE INDEX ix_data_operation_health_issue_dataset_status ON data_operation_health_issue(dataset_code, status);
        CREATE TABLE data_operation_schedule (
          schedule_id UUID PRIMARY KEY,
          dataset_code VARCHAR(160) NOT NULL,
          mode VARCHAR(24) NOT NULL,
          target_policy_json JSONB NOT NULL,
          frequency_json JSONB NOT NULL,
          misfire_policy VARCHAR(24) NOT NULL,
          coalesce BOOLEAN NOT NULL,
          enabled BOOLEAN NOT NULL,
          version INTEGER NOT NULL,
          revision_id UUID NOT NULL,
          recent_run_at TIMESTAMPTZ NULL,
          next_run_at TIMESTAMPTZ NULL,
          updated_at TIMESTAMPTZ NOT NULL,
          updated_by_actor_ref VARCHAR(128) NOT NULL,
          CONSTRAINT uq_data_operation_schedule_dataset UNIQUE(dataset_code),
          CONSTRAINT ck_data_operation_schedule_version CHECK(version >= 1)
        );
        """
    )


def downgrade() -> None:
    """仅在控制面表为空时删除，保护命令、审计、健康和计划历史。"""
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM data_operation_command)
             OR EXISTS (SELECT 1 FROM data_operation_health_check)
             OR EXISTS (SELECT 1 FROM data_operation_schedule)
             OR EXISTS (SELECT 1 FROM data_operation_event) THEN
            RAISE EXCEPTION 'data operations history prevents rollback';
          END IF;
        END $$;
        DROP TABLE data_operation_health_issue;
        DROP TABLE data_operation_health_evaluation;
        DROP TABLE data_operation_health_check_target;
        DROP TABLE data_operation_health_check;
        DROP TABLE data_operation_event;
        DROP TABLE data_operation_execution_slot;
        DROP TABLE data_operation_partition;
        DROP TABLE data_operation_run;
        DROP TABLE data_operation_command;
        DROP TABLE data_operation_preflight;
        DROP TABLE data_operation_idempotency;
        DROP TABLE data_operation_schedule;
        """
    )
