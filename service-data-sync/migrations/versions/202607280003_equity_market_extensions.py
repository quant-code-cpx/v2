"""创建个股直取周期线、因子、公司行动、概况与检查点表。

Revision ID: 202607280003
Revises: 202607280002
Create Date: 2026-07-28
"""

from __future__ import annotations

from collections.abc import Iterable

from alembic import op
from sqlalchemy import Connection, text
from sqlalchemy.orm import DeclarativeBase

from service_data_sync.infrastructure.database.models.equity.identity import (
    equity_profile_version as profile_model,
)
from service_data_sync.infrastructure.database.models.equity.market_data import (
    equity_adjustment_factor as factor_model,
)
from service_data_sync.infrastructure.database.models.equity.market_data import (
    equity_corporate_action_version as action_model,
)
from service_data_sync.infrastructure.database.models.equity.market_data import (
    equity_sync_checkpoint as checkpoint_model,
)
from service_data_sync.infrastructure.database.models.equity.market_data.equity_monthly_bar import (
    EquityMonthlyBar,
)
from service_data_sync.infrastructure.database.models.equity.market_data.equity_weekly_bar import (
    EquityWeeklyBar,
)

# Alembic 使用的版本标识。
revision = "202607280003"
down_revision = "202607280002"
branch_labels = None
depends_on = None

_MODELS: tuple[type[DeclarativeBase], ...] = (
    EquityWeeklyBar,
    EquityMonthlyBar,
    factor_model.EquityAdjustmentFactor,
    action_model.EquityCorporateActionVersion,
    profile_model.EquityProfileVersion,
    checkpoint_model.EquitySyncCheckpoint,
)


def upgrade() -> None:
    """创建六张逻辑表及周期线年度分区，并同步中文数据字典。"""
    _create_period_bar_parent("equity_weekly_bar", "周线")
    _create_period_bar_parent("equity_monthly_bar", "月线")
    _create_factor_table()
    _create_action_table()
    _create_profile_table()
    _create_checkpoint_table()
    _create_initial_period_partitions()
    _sync_model_comments(_MODELS)


def downgrade() -> None:
    """仅在新增市场数据表均为空时回退，避免删除已发布 canonical。"""
    has_rows = (
        op.get_bind()
        .execute(
            text(
                """
                SELECT EXISTS (
                  SELECT 1 FROM equity_weekly_bar
                  UNION ALL SELECT 1 FROM equity_monthly_bar
                  UNION ALL SELECT 1 FROM equity_adjustment_factor
                  UNION ALL SELECT 1 FROM equity_corporate_action_version
                  UNION ALL SELECT 1 FROM equity_profile_version
                  UNION ALL SELECT 1 FROM equity_sync_checkpoint
                )
                """
            )
        )
        .scalar_one()
    )
    if has_rows:
        raise RuntimeError("cannot downgrade equity market extensions after state exists")
    op.execute("DROP TABLE equity_sync_checkpoint")
    op.execute("DROP TABLE equity_profile_version")
    op.execute("DROP TABLE equity_corporate_action_version")
    op.execute("DROP TABLE equity_adjustment_factor")
    op.execute("DROP TABLE equity_monthly_bar")
    op.execute("DROP TABLE equity_weekly_bar")


def _create_period_bar_parent(table_name: str, label: str) -> None:
    """创建周线或月线父表；两者结构相同但来源与 publication 完全独立。"""
    op.execute(
        f"""
        CREATE TABLE {table_name} (
          security_id BIGINT NOT NULL REFERENCES equity_instrument(security_id)
            ON DELETE RESTRICT,
          period_end DATE NOT NULL,
          revision INTEGER NOT NULL CHECK (revision > 0),
          open_price NUMERIC(20, 6) NOT NULL CHECK (open_price >= 0),
          high_price NUMERIC(20, 6) NOT NULL CHECK (high_price >= 0),
          low_price NUMERIC(20, 6) NOT NULL CHECK (low_price >= 0),
          close_price NUMERIC(20, 6) NOT NULL CHECK (close_price >= 0),
          volume_shares BIGINT NOT NULL CHECK (volume_shares >= 0),
          amount_cny NUMERIC(24, 4) NOT NULL CHECK (amount_cny >= 0),
          turnover_rate NUMERIC(16, 10),
          is_final BOOLEAN NOT NULL,
          content_sha256 BYTEA NOT NULL,
          source_batch_id UUID NOT NULL REFERENCES source_batch(source_batch_id)
            ON DELETE RESTRICT,
          valid_from TIMESTAMPTZ NOT NULL,
          valid_to TIMESTAMPTZ,
          PRIMARY KEY (security_id, period_end, revision),
          CHECK (low_price <= LEAST(open_price, close_price)),
          CHECK (high_price >= GREATEST(open_price, close_price)),
          CHECK (low_price <= high_price),
          CHECK (valid_to IS NULL OR valid_to > valid_from)
        ) PARTITION BY RANGE (period_end)
        """
    )
    op.execute(
        f"CREATE INDEX ix_{table_name}_read "
        f"ON {table_name} (security_id, period_end DESC) INCLUDE "
        "(open_price, high_price, low_price, close_price, volume_shares, amount_cny)"
    )
    # 参数只来自受控常量，标签用于解释两张独立表的业务语义。
    op.execute(f"COMMENT ON TABLE {table_name} IS '上游直取的未复权个股{label} revision。'")


def _create_factor_table() -> None:
    """创建累计后复权因子 revision 表和当前序列索引。"""
    op.execute(
        """
        CREATE TABLE equity_adjustment_factor (
          security_id BIGINT NOT NULL REFERENCES equity_instrument(security_id)
            ON DELETE RESTRICT,
          effective_date DATE NOT NULL,
          revision INTEGER NOT NULL CHECK (revision > 0),
          cumulative_factor NUMERIC(38, 18) NOT NULL CHECK (cumulative_factor > 0),
          factor_version UUID NOT NULL,
          content_sha256 BYTEA NOT NULL,
          source_batch_id UUID NOT NULL REFERENCES source_batch(source_batch_id)
            ON DELETE RESTRICT,
          valid_from TIMESTAMPTZ NOT NULL,
          valid_to TIMESTAMPTZ,
          PRIMARY KEY (security_id, effective_date, revision),
          CHECK (valid_to IS NULL OR valid_to > valid_from)
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_equity_adjustment_factor_current
        ON equity_adjustment_factor (security_id, effective_date)
        WHERE valid_to IS NULL
        """
    )
    op.execute(
        """
        CREATE INDEX ix_equity_adjustment_factor_lookup
        ON equity_adjustment_factor (security_id, effective_date DESC)
        INCLUDE (cumulative_factor, factor_version)
        WHERE valid_to IS NULL
        """
    )


def _create_action_table() -> None:
    """创建分红送转事件 revision 表。"""
    op.execute(
        """
        CREATE TABLE equity_corporate_action_version (
          action_id UUID NOT NULL,
          revision INTEGER NOT NULL CHECK (revision > 0),
          security_id BIGINT NOT NULL REFERENCES equity_instrument(security_id)
            ON DELETE RESTRICT,
          source_event_key VARCHAR(160) NOT NULL,
          report_period DATE NOT NULL,
          status VARCHAR(80) NOT NULL,
          announcement_date DATE,
          record_date DATE,
          ex_date DATE,
          cash_dividend_per_10 NUMERIC(30, 10)
            CHECK (cash_dividend_per_10 IS NULL OR cash_dividend_per_10 >= 0),
          bonus_shares_per_10 NUMERIC(30, 10)
            CHECK (bonus_shares_per_10 IS NULL OR bonus_shares_per_10 >= 0),
          transfer_shares_per_10 NUMERIC(30, 10)
            CHECK (transfer_shares_per_10 IS NULL OR transfer_shares_per_10 >= 0),
          content_sha256 BYTEA NOT NULL,
          source_batch_id UUID NOT NULL REFERENCES source_batch(source_batch_id)
            ON DELETE RESTRICT,
          valid_from TIMESTAMPTZ NOT NULL,
          valid_to TIMESTAMPTZ,
          source_description TEXT,
          PRIMARY KEY (action_id, revision),
          UNIQUE (security_id, source_event_key, revision),
          CHECK (valid_to IS NULL OR valid_to > valid_from)
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_equity_corporate_action_current
        ON equity_corporate_action_version (action_id)
        WHERE valid_to IS NULL
        """
    )
    op.execute(
        """
        CREATE INDEX ix_equity_corporate_action_read
        ON equity_corporate_action_version (security_id, report_period DESC)
        WHERE valid_to IS NULL
        """
    )


def _create_profile_table() -> None:
    """创建公司概况 revision 表。"""
    op.execute(
        """
        CREATE TABLE equity_profile_version (
          security_id BIGINT NOT NULL REFERENCES equity_instrument(security_id)
            ON DELETE RESTRICT,
          revision INTEGER NOT NULL CHECK (revision > 0),
          company_name VARCHAR(300) NOT NULL,
          english_name VARCHAR(500),
          industry VARCHAR(300),
          legal_representative VARCHAR(160),
          established_on DATE,
          website VARCHAR(1000),
          email VARCHAR(500),
          phone VARCHAR(300),
          registered_address TEXT,
          office_address TEXT,
          main_business TEXT,
          business_scope TEXT,
          summary TEXT,
          content_sha256 BYTEA NOT NULL,
          source_batch_id UUID NOT NULL REFERENCES source_batch(source_batch_id)
            ON DELETE RESTRICT,
          valid_from TIMESTAMPTZ NOT NULL,
          valid_to TIMESTAMPTZ,
          PRIMARY KEY (security_id, revision),
          CHECK (valid_to IS NULL OR valid_to > valid_from)
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_equity_profile_current
        ON equity_profile_version (security_id)
        WHERE valid_to IS NULL
        """
    )


def _create_checkpoint_table() -> None:
    """创建按能力隔离的增量检查点。"""
    op.execute(
        """
        CREATE TABLE equity_sync_checkpoint (
          capability VARCHAR(80) NOT NULL,
          partition_key VARCHAR(160) NOT NULL,
          last_window_end DATE,
          data_version UUID NOT NULL REFERENCES dataset_publication(data_version)
            ON DELETE RESTRICT,
          updated_at TIMESTAMPTZ NOT NULL,
          PRIMARY KEY (capability, partition_key)
        )
        """
    )


def _create_initial_period_partitions() -> None:
    """预建十年历史、当前年和下一年周期线分区，支持首轮历史回填。"""
    current_year = (
        op.get_bind().execute(text("SELECT EXTRACT(YEAR FROM CURRENT_DATE)::integer")).scalar_one()
    )
    for year in range(current_year - 10, current_year + 2):
        _create_period_partition("equity_weekly_bar", year)
        _create_period_partition("equity_monthly_bar", year)


def _create_period_partition(table_name: str, year: int) -> None:
    """创建单个周期表年度子分区与当前 revision 唯一索引。"""
    child = f"{table_name}_{year}"
    op.execute(
        f"CREATE TABLE {child} PARTITION OF {table_name} "
        f"FOR VALUES FROM ('{year}-01-01') TO ('{year + 1}-01-01')"
    )
    op.execute(
        f"CREATE UNIQUE INDEX uq_{child}_current "
        f"ON {child} (security_id, period_end) WHERE valid_to IS NULL"
    )


def _sync_model_comments(models: Iterable[type[DeclarativeBase]]) -> None:
    """把新增 Declarative 模型的中文表列说明写入 schema。"""
    connection = op.get_bind()
    for model in models:
        table = model.__table__
        _set_comment(connection, "TABLE", table.name, None, table.comment)
        for column in table.columns:
            _set_comment(connection, "COLUMN", table.name, column.name, column.comment)


def _set_comment(
    connection: Connection,
    kind: str,
    table_name: str,
    column_name: str | None,
    comment: str | None,
) -> None:
    """安全渲染受控模型说明，不把标识或文本作为外部输入。"""
    preparer = connection.dialect.identifier_preparer
    target = preparer.quote(table_name)
    if column_name is not None:
        target = f"{target}.{preparer.quote(column_name)}"
    literal = "NULL" if comment is None else "'" + comment.replace("'", "''") + "'"
    connection.execute(text(f"COMMENT ON {kind} {target} IS {literal}"))
