"""增加互联互通完整包 fenced 回滚审计。

Revision ID: 202607300011
Revises: 202607300010
Create Date: 2026-07-30
"""

from __future__ import annotations

from alembic import op

from service_data_sync.infrastructure.database.models.market.stock_connect_center import (
    StockConnectBundleRollbackAudit,
)

revision = "202607300011"
down_revision = "202607300010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """创建不可变回滚审计，并由外键固定 run、bundle 与指针变更证据。"""
    StockConnectBundleRollbackAudit.__table__.create(bind=op.get_bind(), checkfirst=False)
    op.execute(
        """
        CREATE FUNCTION reject_stock_connect_bundle_rollback_audit_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
          RAISE EXCEPTION 'stock-connect bundle rollback audit is immutable';
        END
        $$;

        CREATE TRIGGER trg_stock_connect_bundle_rollback_audit_update_delete
          BEFORE UPDATE OR DELETE
          ON stock_connect_bundle_rollback_audit
          FOR EACH ROW
          EXECUTE FUNCTION reject_stock_connect_bundle_rollback_audit_mutation();

        CREATE TRIGGER trg_stock_connect_bundle_rollback_audit_truncate
          BEFORE TRUNCATE
          ON stock_connect_bundle_rollback_audit
          FOR EACH STATEMENT
          EXECUTE FUNCTION reject_stock_connect_bundle_rollback_audit_mutation();
        """
    )


def downgrade() -> None:
    """仅在没有真实回滚审计时删除结构，避免部署回退抹除操作证据。"""
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM stock_connect_bundle_rollback_audit) THEN
            RAISE EXCEPTION 'stock-connect bundle rollback audit history prevents downgrade';
          END IF;
        END
        $$;

        DROP TRIGGER trg_stock_connect_bundle_rollback_audit_truncate
          ON stock_connect_bundle_rollback_audit;
        DROP TRIGGER trg_stock_connect_bundle_rollback_audit_update_delete
          ON stock_connect_bundle_rollback_audit;
        DROP FUNCTION reject_stock_connect_bundle_rollback_audit_mutation();
        DROP TABLE stock_connect_bundle_rollback_audit;
        """
    )
