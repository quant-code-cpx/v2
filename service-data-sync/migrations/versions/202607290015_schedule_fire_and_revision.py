"""增加计划不可变 revision 与确定性 fire 权威账本。

计划当前行仅表达可编辑状态，无法证明一次触发使用的 selector、target policy 和 scheduledFor。
本迁移将 revision 与 fire 独立持久化，使双 scheduler、重启、misfire/coalesce 都可通过数据库唯一
约束复用同一业务 command，而不是依赖内存去重。

Revision ID: 202607290015
Revises: 202607290014
Create Date: 2026-07-29
"""

from __future__ import annotations

from alembic import op

revision = "202607290015"
down_revision = "202607290014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """创建计划 revision 与 fire 表，并为既有计划回填不可变基线快照。"""
    op.execute(
        """
        CREATE TABLE data_operation_schedule_revision (
          revision_id UUID PRIMARY KEY,
          schedule_id UUID NOT NULL REFERENCES data_operation_schedule(schedule_id)
            ON DELETE RESTRICT,
          version INTEGER NOT NULL,
          change_kind VARCHAR(24) NOT NULL,
          dataset_code VARCHAR(160) NOT NULL,
          mode VARCHAR(24) NOT NULL,
          selector_json JSONB NOT NULL,
          target_policy_json JSONB NOT NULL,
          frequency_json JSONB NOT NULL,
          misfire_policy VARCHAR(24) NOT NULL,
          coalesce BOOLEAN NOT NULL,
          enabled BOOLEAN NOT NULL,
          before_hash VARCHAR(64) NOT NULL,
          after_hash VARCHAR(64) NOT NULL,
          actor_ref VARCHAR(128) NOT NULL,
          request_id VARCHAR(128) NOT NULL,
          created_at TIMESTAMPTZ NOT NULL,
          CONSTRAINT uq_data_operation_schedule_revision_version UNIQUE(schedule_id, version),
          CONSTRAINT ck_data_operation_schedule_revision_version CHECK(version >= 1),
          CONSTRAINT ck_data_operation_schedule_revision_before CHECK(length(before_hash) = 64),
          CONSTRAINT ck_data_operation_schedule_revision_after CHECK(length(after_hash) = 64)
        );
        CREATE INDEX ix_data_operation_schedule_revision_schedule_time
          ON data_operation_schedule_revision(schedule_id, created_at DESC);
        CREATE TABLE data_operation_schedule_fire (
          fire_id UUID PRIMARY KEY,
          schedule_id UUID NOT NULL REFERENCES data_operation_schedule(schedule_id)
            ON DELETE RESTRICT,
          revision_id UUID NOT NULL REFERENCES data_operation_schedule_revision(revision_id)
            ON DELETE RESTRICT,
          schedule_version INTEGER NOT NULL,
          scheduled_for TIMESTAMPTZ NOT NULL,
          selector_json JSONB NOT NULL,
          target_policy_json JSONB NOT NULL,
          target_policy_version INTEGER NOT NULL,
          target_json JSONB NULL,
          resolved_observation_date DATE NULL,
          command_id UUID NULL REFERENCES data_operation_command(command_id)
            ON DELETE RESTRICT,
          outcome VARCHAR(24) NOT NULL,
          reason_code VARCHAR(80) NULL,
          coalesced_count INTEGER NOT NULL,
          request_id VARCHAR(128) NOT NULL,
          created_at TIMESTAMPTZ NOT NULL,
          CONSTRAINT uq_data_operation_schedule_fire_occurrence
            UNIQUE(schedule_id, scheduled_for, schedule_version),
          CONSTRAINT uq_data_operation_schedule_fire_command UNIQUE(command_id),
          CONSTRAINT ck_data_operation_schedule_fire_version CHECK(schedule_version >= 1),
          CONSTRAINT ck_data_operation_schedule_fire_coalesced CHECK(coalesced_count >= 0),
          CONSTRAINT ck_data_operation_schedule_fire_outcome
            CHECK(outcome IN ('QUEUED','SKIPPED','REJECTED'))
        );
        CREATE INDEX ix_data_operation_schedule_fire_schedule_time
          ON data_operation_schedule_fire(schedule_id, scheduled_for DESC);
        INSERT INTO data_operation_schedule_revision (
          revision_id,
          schedule_id,
          version,
          change_kind,
          dataset_code,
          mode,
          selector_json,
          target_policy_json,
          frequency_json,
          misfire_policy,
          coalesce,
          enabled,
          before_hash,
          after_hash,
          actor_ref,
          request_id,
          created_at
        )
        SELECT
          schedule.revision_id,
          schedule.schedule_id,
          schedule.version,
          'BASELINE',
          schedule.dataset_code,
          schedule.mode,
          schedule.selector_json,
          schedule.target_policy_json,
          schedule.frequency_json,
          schedule.misfire_policy,
          schedule.coalesce,
          schedule.enabled,
          repeat('0', 64),
          repeat('0', 64),
          COALESCE(NULLIF(schedule.updated_by_actor_ref, ''), 'system:migration'),
          'migration:202607290015',
          schedule.updated_at
        FROM data_operation_schedule AS schedule
        ON CONFLICT (revision_id) DO NOTHING;
        COMMENT ON TABLE data_operation_schedule_revision IS
          '自动计划不可变 revision；每次变更冻结前后摘要和操作者。';
        COMMENT ON TABLE data_operation_schedule_fire IS
          '确定性计划 fire；重启或双 scheduler 不得产生第二个 command。';
        COMMENT ON COLUMN data_operation_schedule_fire.fire_id IS
          'UUIDv5(scheduleId, scheduledFor, scheduleVersion) 推导的稳定 fire 键。';
        COMMENT ON COLUMN data_operation_schedule_fire.target_json IS
          '已解析并冻结到 command 的安全同步目标；跳过或拒绝时为空。';
        """
    )


def downgrade() -> None:
    """仅在没有 revision/fire 历史时允许回退，保护计划审计与幂等事实。"""
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM data_operation_schedule_fire)
             OR EXISTS (SELECT 1 FROM data_operation_schedule_revision) THEN
            RAISE EXCEPTION 'schedule revision or fire history prevents rollback';
          END IF;
        END $$;
        DROP TABLE data_operation_schedule_fire;
        DROP TABLE data_operation_schedule_revision;
        """
    )
