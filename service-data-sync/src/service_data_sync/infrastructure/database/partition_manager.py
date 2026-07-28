"""按时间创建物理分区；业务仓储只调用模型和本模块公开函数。"""

from __future__ import annotations

from datetime import date

from sqlalchemy import DDL
from sqlalchemy.orm import Session


def ensure_financial_year_partitions(connection: Session, partition_date: date) -> None:
    """确保财务报告、指标和估值所属年度分区及双时态排斥约束存在。"""
    year = partition_date.year
    next_year = year + 1
    range_sql = f"FOR VALUES FROM ('{year}-01-01') TO ('{next_year}-01-01')"
    report_tables = (
        ("financial_report_revision", "financial_report_id WITH ="),
        (
            "provider_financial_metric_revision",
            "security_id WITH =, metric_id WITH =, methodology_id WITH =, "
            "period_basis WITH =, statement_scope WITH =",
        ),
        (
            "derived_financial_metric_revision",
            "security_id WITH =, metric_id WITH =, methodology_id WITH =, "
            "period_basis WITH =, statement_scope WITH =, formula_version WITH =",
        ),
    )
    for parent_table, logical_key in report_tables:
        _ensure_financial_partition(
            connection,
            parent_table=parent_table,
            year=year,
            range_sql=range_sql,
            logical_key=logical_key,
        )
    _ensure_financial_partition(
        connection,
        parent_table="financial_statement_fact",
        year=year,
        range_sql=range_sql,
        logical_key=None,
    )
    _ensure_financial_partition(
        connection,
        parent_table="valuation_observation_revision",
        year=year,
        range_sql=range_sql,
        logical_key="security_id WITH =, metric_id WITH =, methodology_id WITH =",
    )


def _ensure_financial_partition(
    connection: Session,
    *,
    parent_table: str,
    year: int,
    range_sql: str,
    logical_key: str | None,
) -> None:
    """创建一个受固定表名集合约束的年度子表，并在需要时补齐双时态不重叠保护。"""
    table_name = f"{parent_table}_{year}"
    connection.execute(
        DDL(f"CREATE TABLE IF NOT EXISTS {table_name} PARTITION OF {parent_table} {range_sql}")
    )
    if logical_key is None:
        return
    constraint_name = f"ex_{parent_table}_{year}_bitemporal"
    connection.execute(
        DDL(
            f"""
            DO $$
            BEGIN
              IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = '{constraint_name}'
              ) THEN
                ALTER TABLE {table_name}
                ADD CONSTRAINT {constraint_name} EXCLUDE USING gist (
                  {logical_key},
                  effective_range WITH &&,
                  knowledge_range WITH &&
                );
              END IF;
            END
            $$;
            """
        )
    )


def ensure_sector_membership_item_partition(connection: Session, snapshot_date: date) -> None:
    """确保成分快照所属月份的物理分区及反向索引存在。"""
    month_start = snapshot_date.replace(day=1)
    next_month = (
        date(month_start.year + 1, 1, 1)
        if month_start.month == 12
        else date(month_start.year, month_start.month + 1, 1)
    )
    suffix = month_start.strftime("%Y%m")
    table_name = f"sector_membership_item_{suffix}"
    connection.execute(
        DDL(
            f"CREATE TABLE IF NOT EXISTS {table_name} PARTITION OF sector_membership_item "
            f"FOR VALUES FROM ('{month_start.isoformat()}') TO ('{next_month.isoformat()}')"
        )
    )
    connection.execute(
        DDL(
            f"CREATE INDEX IF NOT EXISTS ix_{table_name}_reverse "
            f"ON {table_name} (security_id, snapshot_id)"
        )
    )
