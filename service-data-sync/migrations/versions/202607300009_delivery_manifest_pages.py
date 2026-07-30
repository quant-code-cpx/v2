"""增加不可变交付清单分页与独立租约恢复预算。

Revision ID: 202607300009
Revises: 202607300008
Create Date: 2026-07-30
"""

from __future__ import annotations

from alembic import op

revision = "202607300009"
down_revision = "202607300008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """创建原子 header/page 清单，并把恢复失败次数从普通 dispatch attempt 分离。"""
    op.execute(
        """
        ALTER TABLE data_operation_run
          ADD COLUMN recovery_attempts INTEGER NOT NULL DEFAULT 0,
          ADD CONSTRAINT ck_data_operation_run_recovery_attempts
            CHECK (recovery_attempts >= 0);

        COMMENT ON COLUMN data_operation_run.recovery_attempts IS
          '仅租约过期回收时递增的恢复失败次数，正常公平批次不消耗该预算。';

        CREATE TABLE data_operation_delivery_manifest (
          manifest_id UUID PRIMARY KEY,
          schema_version VARCHAR(80) NOT NULL,
          dataset_code VARCHAR(160) NOT NULL,
          provider_id VARCHAR(128) NOT NULL,
          request_hash VARCHAR(64) NOT NULL,
          root_hash VARCHAR(64) NOT NULL,
          status VARCHAR(24) NOT NULL,
          target_count INTEGER NOT NULL,
          page_count INTEGER NOT NULL,
          available_until TIMESTAMPTZ NOT NULL,
          minimum_remaining_seconds INTEGER NOT NULL,
          created_at TIMESTAMPTZ NOT NULL,
          CONSTRAINT ck_data_operation_delivery_manifest_schema
            CHECK (schema_version = 'quant-v2.delivery-manifest.v1'),
          CONSTRAINT ck_data_operation_delivery_manifest_request_hash
            CHECK (length(request_hash) = 64),
          CONSTRAINT ck_data_operation_delivery_manifest_root_hash
            CHECK (length(root_hash) = 64),
          CONSTRAINT ck_data_operation_delivery_manifest_status
            CHECK (status IN ('ELIGIBLE','REJECTED')),
          CONSTRAINT ck_data_operation_delivery_manifest_remaining
            CHECK (minimum_remaining_seconds >= 0),
          CONSTRAINT ck_data_operation_delivery_manifest_availability
            CHECK (
              available_until >=
                created_at + minimum_remaining_seconds * INTERVAL '1 second'
            ),
          CONSTRAINT ck_data_operation_delivery_manifest_counts
            CHECK (
              (status = 'ELIGIBLE' AND target_count > 0 AND page_count > 0)
              OR
              (status = 'REJECTED' AND target_count = 0 AND page_count = 0)
            )
        );

        CREATE INDEX ix_data_operation_delivery_manifest_request
          ON data_operation_delivery_manifest(request_hash, created_at);

        CREATE TABLE data_operation_delivery_manifest_page (
          manifest_id UUID NOT NULL
            REFERENCES data_operation_delivery_manifest(manifest_id) ON DELETE RESTRICT,
          page_no INTEGER NOT NULL,
          date_from DATE NOT NULL,
          date_to DATE NOT NULL,
          trade_date_count INTEGER NOT NULL,
          target_count INTEGER NOT NULL,
          page_hash VARCHAR(64) NOT NULL,
          evidence_json JSONB NOT NULL,
          created_at TIMESTAMPTZ NOT NULL,
          PRIMARY KEY (manifest_id, page_no),
          CONSTRAINT ck_data_operation_delivery_manifest_page_no
            CHECK (page_no >= 0),
          CONSTRAINT ck_data_operation_delivery_manifest_page_dates
            CHECK (date_from <= date_to),
          CONSTRAINT ck_data_operation_delivery_manifest_page_dates_count
            CHECK (trade_date_count BETWEEN 1 AND 20),
          CONSTRAINT ck_data_operation_delivery_manifest_page_target_count
            CHECK (target_count BETWEEN 1 AND 256),
          CONSTRAINT ck_data_operation_delivery_manifest_page_hash
            CHECK (length(page_hash) = 64)
        );

        CREATE INDEX ix_data_operation_delivery_manifest_page_window
          ON data_operation_delivery_manifest_page(manifest_id, date_from, date_to);

        COMMENT ON TABLE data_operation_delivery_manifest IS
          '一次预检最终冻结的来源交付清单 header；任何更新或删除均由数据库触发器拒绝。';
        COMMENT ON COLUMN data_operation_delivery_manifest.manifest_id IS
          '不可变清单 UUID。';
        COMMENT ON COLUMN data_operation_delivery_manifest.schema_version IS
          '根摘要和分页正文使用的固定合同版本。';
        COMMENT ON COLUMN data_operation_delivery_manifest.dataset_code IS
          '本清单唯一对应的 canonical 数据集。';
        COMMENT ON COLUMN data_operation_delivery_manifest.provider_id IS
          '产生并复核交付证据的冻结来源标识。';
        COMMENT ON COLUMN data_operation_delivery_manifest.request_hash IS
          '规范预检请求的 SHA-256。';
        COMMENT ON COLUMN data_operation_delivery_manifest.root_hash IS
          'header 与有序页面摘要共同生成的 SHA-256。';
        COMMENT ON COLUMN data_operation_delivery_manifest.status IS
          '最终可执行或拒绝状态，不允许后续改写。';
        COMMENT ON COLUMN data_operation_delivery_manifest.target_count IS
          '全部页面业务目标总数。';
        COMMENT ON COLUMN data_operation_delivery_manifest.page_count IS
          '从零连续编号的页面总数。';
        COMMENT ON COLUMN data_operation_delivery_manifest.available_until IS
          '该清单允许新消费的绝对截止时间。';
        COMMENT ON COLUMN data_operation_delivery_manifest.minimum_remaining_seconds IS
          '新 command 受理时必须保留的最小可用窗口秒数。';
        COMMENT ON COLUMN data_operation_delivery_manifest.created_at IS
          '完整预检结束并冻结清单的时间。';

        COMMENT ON TABLE data_operation_delivery_manifest_page IS
          '不可变交付证据页；每页最多二十个完整交易日和二百五十六个业务目标。';
        COMMENT ON COLUMN data_operation_delivery_manifest_page.manifest_id IS
          '所属不可变清单 UUID。';
        COMMENT ON COLUMN data_operation_delivery_manifest_page.page_no IS
          '从零开始的连续页面序号。';
        COMMENT ON COLUMN data_operation_delivery_manifest_page.date_from IS
          '本页第一个完整交易日。';
        COMMENT ON COLUMN data_operation_delivery_manifest_page.date_to IS
          '本页最后一个完整交易日。';
        COMMENT ON COLUMN data_operation_delivery_manifest_page.trade_date_count IS
          '本页不重复交易日数量，最大二十。';
        COMMENT ON COLUMN data_operation_delivery_manifest_page.target_count IS
          '本页业务目标数量，最大二百五十六。';
        COMMENT ON COLUMN data_operation_delivery_manifest_page.page_hash IS
          '页面边界、计数和正文共同生成的 SHA-256。';
        COMMENT ON COLUMN data_operation_delivery_manifest_page.evidence_json IS
          '不含供应商正文的路径、版本、摘要和业务目标证据。';
        COMMENT ON COLUMN data_operation_delivery_manifest_page.created_at IS
          '页面与 header 同事务冻结的时间。';

        CREATE FUNCTION reject_data_operation_delivery_manifest_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
          RAISE EXCEPTION 'delivery manifest rows are immutable';
        END
        $$;

        CREATE TRIGGER trg_data_operation_delivery_manifest_immutable
          BEFORE UPDATE OR DELETE ON data_operation_delivery_manifest
          FOR EACH ROW EXECUTE FUNCTION reject_data_operation_delivery_manifest_mutation();

        CREATE TRIGGER trg_data_operation_delivery_manifest_page_immutable
          BEFORE UPDATE OR DELETE ON data_operation_delivery_manifest_page
          FOR EACH ROW EXECUTE FUNCTION reject_data_operation_delivery_manifest_mutation();
        """
    )


def downgrade() -> None:
    """仅在没有清单和恢复历史时移除新增结构，避免丢失审计与恢复事实。"""
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM data_operation_delivery_manifest)
             OR EXISTS (
               SELECT 1
               FROM data_operation_run
               WHERE recovery_attempts <> 0
             ) THEN
            RAISE EXCEPTION 'delivery manifest or recovery history prevents rollback';
          END IF;
        END
        $$;

        DROP TRIGGER trg_data_operation_delivery_manifest_page_immutable
          ON data_operation_delivery_manifest_page;
        DROP TRIGGER trg_data_operation_delivery_manifest_immutable
          ON data_operation_delivery_manifest;
        DROP FUNCTION reject_data_operation_delivery_manifest_mutation();
        DROP TABLE data_operation_delivery_manifest_page;
        DROP TABLE data_operation_delivery_manifest;
        ALTER TABLE data_operation_run
          DROP CONSTRAINT ck_data_operation_run_recovery_attempts,
          DROP COLUMN recovery_attempts;
        """
    )
