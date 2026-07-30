"""增加互联互通 official-calendar readiness 快照。

Revision ID: 202607300013
Revises: 202607300012
Create Date: 2026-07-30
"""

from __future__ import annotations

from alembic import op

from service_data_sync.infrastructure.database.models.market.stock_connect_readiness import (
    StockConnectReadinessCalendarDay,
    StockConnectReadinessSnapshot,
)

revision = "202607300013"
down_revision = "202607300012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """创建先落 PROBING、后单向终结的 snapshot 及不可变逐日官方日历证据。"""
    StockConnectReadinessSnapshot.__table__.create(bind=op.get_bind(), checkfirst=False)
    StockConnectReadinessCalendarDay.__table__.create(bind=op.get_bind(), checkfirst=False)
    op.execute(
        """
        CREATE FUNCTION enforce_stock_connect_readiness_snapshot_transition()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
          IF OLD.status <> 'PROBING' OR NEW.status = 'PROBING' THEN
            RAISE EXCEPTION 'stock-connect readiness snapshot is already terminal';
          END IF;
          IF NEW.snapshot_id <> OLD.snapshot_id
             OR NEW.schema_version <> OLD.schema_version
             OR NEW.request_hash <> OLD.request_hash
             OR NEW.selected_channel_set <> OLD.selected_channel_set
             OR NEW.created_at <> OLD.created_at THEN
            RAISE EXCEPTION 'stock-connect readiness snapshot identity is immutable';
          END IF;
          RETURN NEW;
        END
        $$;

        CREATE FUNCTION reject_stock_connect_readiness_evidence_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
          RAISE EXCEPTION 'stock-connect readiness evidence is immutable';
        END
        $$;

        CREATE TRIGGER trg_stock_connect_readiness_snapshot_update
          BEFORE UPDATE
          ON stock_connect_readiness_snapshot
          FOR EACH ROW
          EXECUTE FUNCTION enforce_stock_connect_readiness_snapshot_transition();

        CREATE TRIGGER trg_stock_connect_readiness_snapshot_delete
          BEFORE DELETE
          ON stock_connect_readiness_snapshot
          FOR EACH ROW
          EXECUTE FUNCTION reject_stock_connect_readiness_evidence_mutation();

        CREATE TRIGGER trg_stock_connect_readiness_snapshot_truncate
          BEFORE TRUNCATE
          ON stock_connect_readiness_snapshot
          FOR EACH STATEMENT
          EXECUTE FUNCTION reject_stock_connect_readiness_evidence_mutation();

        CREATE TRIGGER trg_stock_connect_readiness_calendar_day_update_delete
          BEFORE UPDATE OR DELETE
          ON stock_connect_readiness_calendar_day
          FOR EACH ROW
          EXECUTE FUNCTION reject_stock_connect_readiness_evidence_mutation();

        CREATE TRIGGER trg_stock_connect_readiness_calendar_day_truncate
          BEFORE TRUNCATE
          ON stock_connect_readiness_calendar_day
          FOR EACH STATEMENT
          EXECUTE FUNCTION reject_stock_connect_readiness_evidence_mutation();
        """
    )


def downgrade() -> None:
    """仅在没有 readiness 证据时删除结构，防止部署回退抹除来源失败历史。"""
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM stock_connect_readiness_snapshot)
             OR EXISTS (SELECT 1 FROM stock_connect_readiness_calendar_day) THEN
            RAISE EXCEPTION 'stock-connect readiness history prevents downgrade';
          END IF;
        END
        $$;

        DROP TRIGGER trg_stock_connect_readiness_calendar_day_truncate
          ON stock_connect_readiness_calendar_day;
        DROP TRIGGER trg_stock_connect_readiness_calendar_day_update_delete
          ON stock_connect_readiness_calendar_day;
        DROP TRIGGER trg_stock_connect_readiness_snapshot_truncate
          ON stock_connect_readiness_snapshot;
        DROP TRIGGER trg_stock_connect_readiness_snapshot_delete
          ON stock_connect_readiness_snapshot;
        DROP TRIGGER trg_stock_connect_readiness_snapshot_update
          ON stock_connect_readiness_snapshot;
        DROP FUNCTION reject_stock_connect_readiness_evidence_mutation();
        DROP FUNCTION enforce_stock_connect_readiness_snapshot_transition();
        DROP TABLE stock_connect_readiness_calendar_day;
        DROP TABLE stock_connect_readiness_snapshot;
        """
    )
