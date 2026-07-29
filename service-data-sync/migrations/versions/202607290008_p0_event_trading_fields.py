"""补齐公司事件与交易公开信息 P0 的强类型字段。

Revision ID: 202607290008
Revises: 202607290007
Create Date: 2026-07-29
"""

from __future__ import annotations

from alembic import op

# Alembic 使用的版本标识。
revision = "202607290008"
down_revision = "202607290007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """以加法方式补足 P0 字段；使用幂等 DDL 兼容由最新 ORM 直接建表的新环境。"""
    _add("disclosure_document", "announced_on DATE")
    _add("disclosure_document", "source_batch_id UUID")
    _add("etf_profile_version", "methodology_version_id UUID")
    _add("etf_profile_version", "release_id UUID")
    _add("margin_eligibility_revision", "evidence_basis VARCHAR(24)")
    _add("stock_connect_active_security_revision", "market_stat_release_id UUID")
    op.execute(
        "DO $$ BEGIN "
        "IF NOT EXISTS (SELECT 1 FROM pg_constraint "
        "WHERE conname = 'fk_stock_connect_active_market_stat_release') THEN "
        "ALTER TABLE stock_connect_active_security_revision "
        "ADD CONSTRAINT fk_stock_connect_active_market_stat_release "
        "FOREIGN KEY (market_stat_release_id) "
        "REFERENCES dataset_release(release_id) ON DELETE RESTRICT; "
        "END IF; END $$"
    )
    op.execute(
        "DO $$ BEGIN "
        "IF NOT EXISTS (SELECT 1 FROM pg_constraint "
        "WHERE conname = 'fk_etf_profile_methodology') THEN "
        "ALTER TABLE etf_profile_version ADD CONSTRAINT fk_etf_profile_methodology "
        "FOREIGN KEY (methodology_version_id) "
        "REFERENCES methodology_version(methodology_version_id) ON DELETE RESTRICT; "
        "END IF; END $$"
    )
    op.execute(
        "DO $$ BEGIN "
        "IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_etf_profile_release') THEN "
        "ALTER TABLE etf_profile_version ADD CONSTRAINT fk_etf_profile_release "
        "FOREIGN KEY (release_id) REFERENCES dataset_release(release_id) ON DELETE RESTRICT; "
        "END IF; END $$"
    )
    op.execute(
        "DO $$ BEGIN "
        "IF NOT EXISTS (SELECT 1 FROM pg_constraint "
        "WHERE conname = 'fk_disclosure_document_source_batch') THEN "
        "ALTER TABLE disclosure_document ADD CONSTRAINT fk_disclosure_document_source_batch "
        "FOREIGN KEY (source_batch_id) "
        "REFERENCES source_batch(source_batch_id) ON DELETE RESTRICT; "
        "END IF; END $$"
    )
    _add("corporate_earnings_value", "prior_value NUMERIC(24, 4)")
    _add("corporate_earnings_value", "metric_unit VARCHAR(24)")
    _add("corporate_earnings_value", "preliminary_status VARCHAR(24)")
    _add("corporate_earnings_value", "change_ratio_low NUMERIC(18, 10)")
    _add("corporate_earnings_value", "change_ratio_high NUMERIC(18, 10)")
    _add("dragon_tiger_event_revision", "close_price NUMERIC(24, 8)")
    _add("dragon_tiger_event_revision", "deal_amount NUMERIC(24, 4)")
    _add("dragon_tiger_event_revision", "deal_ratio NUMERIC(18, 10)")
    _add("dragon_tiger_event_revision", "net_ratio NUMERIC(18, 10)")
    _add("dragon_tiger_event_revision", "turnover_ratio NUMERIC(18, 10)")
    _add("dragon_tiger_seat_item", "buy_amount NUMERIC(24, 4)")
    _add("dragon_tiger_seat_item", "sell_amount NUMERIC(24, 4)")
    _add("dragon_tiger_seat_item", "net_amount NUMERIC(24, 4)")
    _add("dragon_tiger_seat_item", "buy_ratio NUMERIC(18, 10)")
    _add("dragon_tiger_seat_item", "sell_ratio NUMERIC(18, 10)")
    _add("block_trade_execution_revision", "buyer_seat_code VARCHAR(64)")
    _add("block_trade_execution_revision", "seller_seat_code VARCHAR(64)")
    _add("block_trade_execution_revision", "source_daily_rank INTEGER")


def downgrade() -> None:
    """回退仅移除本迁移添加的列；不触碰既有 release、raw 或其他专题事实。"""
    op.execute(
        "ALTER TABLE stock_connect_active_security_revision "
        "DROP CONSTRAINT IF EXISTS fk_stock_connect_active_market_stat_release"
    )
    op.execute(
        "ALTER TABLE disclosure_document "
        "DROP CONSTRAINT IF EXISTS fk_disclosure_document_source_batch"
    )
    op.execute("ALTER TABLE etf_profile_version DROP CONSTRAINT IF EXISTS fk_etf_profile_release")
    op.execute(
        "ALTER TABLE etf_profile_version DROP CONSTRAINT IF EXISTS fk_etf_profile_methodology"
    )
    for table_name, column_name in reversed(
        (
            ("block_trade_execution_revision", "source_daily_rank"),
            ("block_trade_execution_revision", "seller_seat_code"),
            ("block_trade_execution_revision", "buyer_seat_code"),
            ("dragon_tiger_seat_item", "sell_ratio"),
            ("dragon_tiger_seat_item", "buy_ratio"),
            ("dragon_tiger_seat_item", "net_amount"),
            ("dragon_tiger_seat_item", "sell_amount"),
            ("dragon_tiger_seat_item", "buy_amount"),
            ("dragon_tiger_event_revision", "turnover_ratio"),
            ("dragon_tiger_event_revision", "net_ratio"),
            ("dragon_tiger_event_revision", "deal_ratio"),
            ("dragon_tiger_event_revision", "deal_amount"),
            ("dragon_tiger_event_revision", "close_price"),
            ("corporate_earnings_value", "preliminary_status"),
            ("corporate_earnings_value", "change_ratio_high"),
            ("corporate_earnings_value", "change_ratio_low"),
            ("corporate_earnings_value", "metric_unit"),
            ("corporate_earnings_value", "prior_value"),
            ("stock_connect_active_security_revision", "market_stat_release_id"),
            ("margin_eligibility_revision", "evidence_basis"),
            ("etf_profile_version", "release_id"),
            ("etf_profile_version", "methodology_version_id"),
            ("disclosure_document", "source_batch_id"),
            ("disclosure_document", "announced_on"),
        )
    ):
        op.execute(f"ALTER TABLE {table_name} DROP COLUMN IF EXISTS {column_name}")


def _add(table_name: str, definition: str) -> None:
    """添加一个可空字段，历史记录保持原义且新环境不会因重复列失败。"""
    op.execute(f"ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS {definition}")
