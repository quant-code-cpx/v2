"""增加港股稳定身份与总览 generation staging。

Revision ID: 202607300012
Revises: 202607300011
Create Date: 2026-07-30
"""

from __future__ import annotations

from alembic import op

from service_data_sync.infrastructure.database.models.market import (
    StockConnectOverviewGeneration,
    StockConnectOverviewGenerationComponent,
)
from service_data_sync.infrastructure.database.models.market.stock_connect_identity import (
    StockConnectHkexInstrumentIdentity,
)

revision = "202607300012"
down_revision = "202607300011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """创建稳定身份映射、运行级 staging header 和不可变组件清单。"""
    StockConnectHkexInstrumentIdentity.__table__.create(bind=op.get_bind(), checkfirst=False)
    StockConnectOverviewGeneration.__table__.create(bind=op.get_bind(), checkfirst=False)
    StockConnectOverviewGenerationComponent.__table__.create(
        bind=op.get_bind(),
        checkfirst=False,
    )
    op.execute(
        """
        CREATE FUNCTION reject_stock_connect_overview_generation_component_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
          RAISE EXCEPTION 'stock-connect overview generation component is immutable';
        END
        $$;

        CREATE TRIGGER trg_stock_connect_overview_generation_component_update_delete
          BEFORE UPDATE OR DELETE
          ON stock_connect_overview_generation_component
          FOR EACH ROW
          EXECUTE FUNCTION reject_stock_connect_overview_generation_component_mutation();

        CREATE TRIGGER trg_stock_connect_overview_generation_component_truncate
          BEFORE TRUNCATE
          ON stock_connect_overview_generation_component
          FOR EACH STATEMENT
          EXECUTE FUNCTION reject_stock_connect_overview_generation_component_mutation();
        """
    )


def downgrade() -> None:
    """仅在没有稳定身份或 generation 审计数据时删除结构，避免回退抹除证据。"""
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM stock_connect_hkex_instrument_identity)
             OR EXISTS (SELECT 1 FROM stock_connect_overview_generation)
             OR EXISTS (SELECT 1 FROM stock_connect_overview_generation_component) THEN
            RAISE EXCEPTION 'stock-connect identity or generation history prevents downgrade';
          END IF;
        END
        $$;

        DROP TRIGGER trg_stock_connect_overview_generation_component_truncate
          ON stock_connect_overview_generation_component;
        DROP TRIGGER trg_stock_connect_overview_generation_component_update_delete
          ON stock_connect_overview_generation_component;
        DROP FUNCTION reject_stock_connect_overview_generation_component_mutation();
        DROP TABLE stock_connect_overview_generation_component;
        DROP TABLE stock_connect_overview_generation;
        DROP TABLE stock_connect_hkex_instrument_identity;
        """
    )
