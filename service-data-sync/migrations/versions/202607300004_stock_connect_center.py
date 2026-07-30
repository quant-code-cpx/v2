"""增加互联互通中心完整包、官方日历、通道状态和字段可用性。

Revision ID: 202607300004
Revises: 202607300003
Create Date: 2026-07-30
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import func, select, text

from service_data_sync.infrastructure.database.models.market.stock_connect_center import (
    StockConnectBundlePublication,
    StockConnectCalendarObservation,
    StockConnectChannelStatusRevision,
    StockConnectOverviewPublication,
)

revision = "202607300004"
down_revision = "202607300003"
branch_labels = None
depends_on = None

_TABLES = (
    StockConnectCalendarObservation.__table__,
    StockConnectChannelStatusRevision.__table__,
    StockConnectBundlePublication.__table__,
    StockConnectOverviewPublication.__table__,
)


def upgrade() -> None:
    """扩展既有 canonical 事实，并创建只在完整组件齐备后可见的 bundle 表。"""
    op.execute(
        """
        ALTER TABLE stock_connect_channel_daily_revision
          ALTER COLUMN buy_amount TYPE NUMERIC(38, 6),
          ALTER COLUMN sell_amount TYPE NUMERIC(38, 6),
          ALTER COLUMN turnover_amount TYPE NUMERIC(38, 6),
          ALTER COLUMN net_buy_amount TYPE NUMERIC(38, 6),
          ALTER COLUMN quota_balance TYPE NUMERIC(38, 6),
          ADD COLUMN IF NOT EXISTS trade_count BIGINT,
          ADD COLUMN IF NOT EXISTS etf_turnover_amount NUMERIC(38, 6),
          ADD COLUMN IF NOT EXISTS field_availability JSONB,
          ADD COLUMN IF NOT EXISTS center_schema_version INTEGER NOT NULL DEFAULT 0;

        UPDATE stock_connect_channel_daily_revision
        SET field_availability = jsonb_build_object(
          'buyAmount', CASE WHEN buy_amount IS NULL THEN 'SOURCE_MISSING' ELSE 'REPORTED' END,
          'sellAmount', CASE WHEN sell_amount IS NULL THEN 'SOURCE_MISSING' ELSE 'REPORTED' END,
          'turnoverAmount',
            CASE WHEN turnover_amount IS NULL THEN 'SOURCE_MISSING' ELSE 'REPORTED' END,
          'netBuyAmount',
            CASE WHEN net_buy_amount IS NULL THEN 'NOT_APPLICABLE' ELSE 'REPORTED' END,
          'tradeCount', 'SOURCE_MISSING',
          'etfTurnoverAmount', 'SOURCE_MISSING'
        );
        ALTER TABLE stock_connect_channel_daily_revision
          ALTER COLUMN field_availability SET NOT NULL,
          ALTER COLUMN center_schema_version DROP DEFAULT;

        ALTER TABLE stock_connect_active_security_revision
          ALTER COLUMN instrument_id DROP NOT NULL,
          ALTER COLUMN buy_amount TYPE NUMERIC(38, 6),
          ALTER COLUMN sell_amount TYPE NUMERIC(38, 6),
          ALTER COLUMN turnover_amount TYPE NUMERIC(38, 6),
          ADD COLUMN IF NOT EXISTS source_instrument_code VARCHAR(32),
          ADD COLUMN IF NOT EXISTS source_instrument_name VARCHAR(160),
          ADD COLUMN IF NOT EXISTS identity_status VARCHAR(32),
          ADD COLUMN IF NOT EXISTS field_availability JSONB;

        UPDATE stock_connect_active_security_revision
        SET identity_status = CASE
            WHEN instrument_id IS NULL THEN 'SOURCE_UNRESOLVED'
              ELSE 'RESOLVED'
            END,
            field_availability = jsonb_build_object(
              'buyAmount', CASE WHEN buy_amount IS NULL THEN 'SOURCE_MISSING' ELSE 'REPORTED' END,
              'sellAmount', CASE WHEN sell_amount IS NULL THEN 'SOURCE_MISSING' ELSE 'REPORTED' END,
              'turnoverAmount',
                CASE WHEN turnover_amount IS NULL THEN 'SOURCE_MISSING' ELSE 'REPORTED' END,
              'netBuyAmount', 'NOT_APPLICABLE'
            );
        ALTER TABLE stock_connect_active_security_revision
          ALTER COLUMN identity_status SET NOT NULL,
          ALTER COLUMN field_availability SET NOT NULL;

        COMMENT ON COLUMN stock_connect_channel_daily_revision.trade_count IS
          '来源直报成交笔数；来源未提供时为空。';
        COMMENT ON COLUMN stock_connect_channel_daily_revision.etf_turnover_amount IS
          '来源直报 ETF 成交额；来源未提供时为空。';
        COMMENT ON COLUMN stock_connect_channel_daily_revision.field_availability IS
          '逐字段披露、制度不可用或来源缺失状态。';
        COMMENT ON COLUMN stock_connect_channel_daily_revision.center_schema_version IS
          '迁移前历史为零，新互联互通中心 publication 写入版本一。';
        COMMENT ON COLUMN stock_connect_active_security_revision.source_instrument_code IS
          '官方活跃榜证券代码；迁移前历史 revision 可为空且不得进入新完整包。';
        COMMENT ON COLUMN stock_connect_active_security_revision.source_instrument_name IS
          '官方活跃榜或 Securities Master 展示名称。';
        COMMENT ON COLUMN stock_connect_active_security_revision.identity_status IS
            'RESOLVED 或 SOURCE_UNRESOLVED。';
        COMMENT ON COLUMN stock_connect_active_security_revision.field_availability IS
          '排行逐字段披露、制度不可用或来源缺失状态。';
        """
    )
    bind = op.get_bind()
    for table in _TABLES:
        table.create(bind=bind, checkfirst=False)


def downgrade() -> None:
    """仅在新完整包和扩展来源字段均未承载新事实时恢复旧结构。"""
    bind = op.get_bind()
    populated = [
        table.name
        for table in _TABLES
        if bind.execute(select(func.count()).select_from(table)).scalar_one() > 0
    ]
    new_fact_exists = bind.execute(
        text(
            """
            SELECT
              EXISTS (
                SELECT 1 FROM stock_connect_channel_daily_revision
                WHERE center_schema_version = 1
                   OR trade_count IS NOT NULL
                   OR etf_turnover_amount IS NOT NULL
                   OR buy_amount <> round(buy_amount, 4)
                   OR sell_amount <> round(sell_amount, 4)
                   OR turnover_amount <> round(turnover_amount, 4)
                   OR net_buy_amount <> round(net_buy_amount, 4)
                   OR quota_balance <> round(quota_balance, 4)
                   OR abs(buy_amount) >= 100000000000000000000
                   OR abs(sell_amount) >= 100000000000000000000
                   OR abs(turnover_amount) >= 100000000000000000000
                   OR abs(net_buy_amount) >= 100000000000000000000
                   OR abs(quota_balance) >= 100000000000000000000
              )
              OR EXISTS (
                SELECT 1 FROM stock_connect_active_security_revision
                WHERE source_instrument_code IS NOT NULL
                   OR source_instrument_name IS NOT NULL
                   OR instrument_id IS NULL
              )
            """
        )
    ).scalar_one()
    if populated or new_fact_exists:
        raise RuntimeError(
            "cannot downgrade stock-connect center after new facts exist: " + ",".join(populated)
        )
    for table in reversed(_TABLES):
        table.drop(bind=bind, checkfirst=False)
    op.execute(
        """
        ALTER TABLE stock_connect_active_security_revision
          DROP COLUMN field_availability,
          DROP COLUMN identity_status,
          DROP COLUMN source_instrument_name,
          DROP COLUMN source_instrument_code,
          ALTER COLUMN instrument_id SET NOT NULL,
          ALTER COLUMN buy_amount TYPE NUMERIC(24, 4),
          ALTER COLUMN sell_amount TYPE NUMERIC(24, 4),
          ALTER COLUMN turnover_amount TYPE NUMERIC(24, 4);

        ALTER TABLE stock_connect_channel_daily_revision
          DROP COLUMN center_schema_version,
          DROP COLUMN field_availability,
          DROP COLUMN etf_turnover_amount,
          DROP COLUMN trade_count,
          ALTER COLUMN buy_amount TYPE NUMERIC(24, 4),
          ALTER COLUMN sell_amount TYPE NUMERIC(24, 4),
          ALTER COLUMN turnover_amount TYPE NUMERIC(24, 4),
          ALTER COLUMN net_buy_amount TYPE NUMERIC(24, 4),
          ALTER COLUMN quota_balance TYPE NUMERIC(24, 4);
        """
    )
