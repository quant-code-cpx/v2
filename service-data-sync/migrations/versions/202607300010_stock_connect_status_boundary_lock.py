"""增加互联互通状态 coverage 不可后移边界锁。

Revision ID: 202607300010
Revises: 202607300009
Create Date: 2026-07-30
"""

from __future__ import annotations

from alembic import op

revision = "202607300010"
down_revision = "202607300009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """创建首次锁定后只能前移、不能删除或截断的状态覆盖边界。"""
    op.execute(
        """
        CREATE TABLE stock_connect_status_coverage_boundary_lock (
          scope_key VARCHAR(160) PRIMARY KEY,
          required_from DATE NOT NULL,
          first_manifest_sha256 VARCHAR(64) NOT NULL,
          current_manifest_sha256 VARCHAR(64) NOT NULL,
          first_locked_at TIMESTAMPTZ NOT NULL,
          tightened_at TIMESTAMPTZ NOT NULL,
          CONSTRAINT ck_stock_connect_status_boundary_hashes
            CHECK (
              first_manifest_sha256 ~ '^[0-9a-f]{64}$'
              AND current_manifest_sha256 ~ '^[0-9a-f]{64}$'
            ),
          CONSTRAINT ck_stock_connect_status_boundary_times
            CHECK (first_locked_at <= tightened_at)
        );

        COMMENT ON TABLE stock_connect_status_coverage_boundary_lock IS
          '互联互通状态 coverage requiredFrom 的不可后移持久化锁；'
          '不同 scope 可由同一门禁实现独立测试与演进。';
        COMMENT ON COLUMN stock_connect_status_coverage_boundary_lock.scope_key IS
          '稳定边界作用域；生产固定为互联互通通道状态数据集。';
        COMMENT ON COLUMN stock_connect_status_coverage_boundary_lock.required_from IS
          '自该市场日期起缺少最终状态即失败关闭；只允许前移。';
        COMMENT ON COLUMN stock_connect_status_coverage_boundary_lock.first_manifest_sha256 IS
          '首次锁定边界时状态 coverage 清单原始字节 SHA-256。';
        COMMENT ON COLUMN stock_connect_status_coverage_boundary_lock.current_manifest_sha256 IS
          '最近一次收紧边界时状态 coverage 清单原始字节 SHA-256。';
        COMMENT ON COLUMN stock_connect_status_coverage_boundary_lock.first_locked_at IS
          '首次成功锁定该边界的带时区时间。';
        COMMENT ON COLUMN stock_connect_status_coverage_boundary_lock.tightened_at IS
          '最近一次把边界前移的带时区时间；未收紧时等于首次锁定时间。';

        CREATE FUNCTION enforce_stock_connect_status_boundary_tightening()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
          IF NEW.required_from >
             timezone('Asia/Shanghai', CURRENT_TIMESTAMP)::date THEN
            RAISE EXCEPTION 'status coverage boundary cannot be in the future';
          END IF;

          IF TG_OP = 'UPDATE' AND (
            NEW.scope_key IS DISTINCT FROM OLD.scope_key
            OR NEW.first_manifest_sha256 IS DISTINCT FROM OLD.first_manifest_sha256
            OR NEW.first_locked_at IS DISTINCT FROM OLD.first_locked_at
            OR NEW.required_from >= OLD.required_from
            OR NEW.tightened_at < OLD.tightened_at
          ) THEN
            RAISE EXCEPTION 'status coverage boundary can only move earlier';
          END IF;
          RETURN NEW;
        END
        $$;

        CREATE FUNCTION reject_stock_connect_status_boundary_removal()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
          RAISE EXCEPTION 'status coverage boundary cannot be removed';
        END
        $$;

        CREATE TRIGGER trg_stock_connect_status_boundary_tightening
          BEFORE INSERT OR UPDATE
          ON stock_connect_status_coverage_boundary_lock
          FOR EACH ROW
          EXECUTE FUNCTION enforce_stock_connect_status_boundary_tightening();

        CREATE TRIGGER trg_stock_connect_status_boundary_delete
          BEFORE DELETE
          ON stock_connect_status_coverage_boundary_lock
          FOR EACH ROW
          EXECUTE FUNCTION reject_stock_connect_status_boundary_removal();

        CREATE TRIGGER trg_stock_connect_status_boundary_truncate
          BEFORE TRUNCATE
          ON stock_connect_status_coverage_boundary_lock
          FOR EACH STATEMENT
          EXECUTE FUNCTION reject_stock_connect_status_boundary_removal();
        """
    )


def downgrade() -> None:
    """仅在从未锁定边界时删除结构，避免回滚重新开放后移绕过路径。"""
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM stock_connect_status_coverage_boundary_lock) THEN
            RAISE EXCEPTION 'status coverage boundary history prevents rollback';
          END IF;
        END
        $$;

        DROP TRIGGER trg_stock_connect_status_boundary_truncate
          ON stock_connect_status_coverage_boundary_lock;
        DROP TRIGGER trg_stock_connect_status_boundary_delete
          ON stock_connect_status_coverage_boundary_lock;
        DROP TRIGGER trg_stock_connect_status_boundary_tightening
          ON stock_connect_status_coverage_boundary_lock;
        DROP FUNCTION reject_stock_connect_status_boundary_removal();
        DROP FUNCTION enforce_stock_connect_status_boundary_tightening();
        DROP TABLE stock_connect_status_coverage_boundary_lock;
        """
    )
