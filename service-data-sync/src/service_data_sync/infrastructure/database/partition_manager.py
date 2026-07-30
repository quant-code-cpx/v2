"""按业务日期按需创建 PostgreSQL 物理分区与必要索引。

分区表仍属于已迁移的逻辑父表：这里绝不定义新业务字段、修改历史迁移或替代仓储发布流程。
函数只执行可重复的 `CREATE ... IF NOT EXISTS` 和约束补齐，且不提交事务；调用方必须把
分区准备与对应事实写入放进同一数据库事务，失败时两者一起回滚。
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import DDL
from sqlalchemy.orm import Session


def ensure_financial_year_partitions(connection: Session, partition_date: date) -> None:
    """确保 `partition_date` 所属年度的财务事实子表和双时态保护已经存在。

    这里的日期是报表期、估值观察日等业务事实日期，不是抓取或入库时间；因此历史回填会
    准确落入历史年度分区。所有父表按同一年度边界创建，避免查询单个报告年度时扫描全表。
    对有双时态 revision 的表，再建立排斥约束，防止同一业务键在有效时间和知识时间都重叠。
    重跑安全：已存在子表或约束不会被重建；本函数也不会自行提交。
    """
    year = partition_date.year
    next_year = year + 1
    range_sql = f"FOR VALUES FROM ('{year}-01-01') TO ('{next_year}-01-01')"
    # 只有本模块维护的固定父表可参与动态 DDL，避免把外部字符串拼入表名造成对象注入。
    # `logical_key` 是同一事实在两条时间轴上不能重叠的业务身份，不是物理主键。
    report_tables = (
        ("financial_report_revision", "financial_report_id WITH ="),
        (
            "provider_financial_metric_revision",
            "report_period WITH =, security_id WITH =, metric_id WITH =, methodology_id WITH =, "
            "period_basis WITH =, statement_scope WITH =",
        ),
        (
            "derived_financial_metric_revision",
            "report_period WITH =, security_id WITH =, metric_id WITH =, methodology_id WITH =, "
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
    """创建一个受固定白名单约束的年度子表，并按需补齐双时态排斥约束。

    PostgreSQL 的分区 DDL 在调用方当前事务中执行；后续任一步失败时，外层事务回滚会撤销
    本次新建对象和数据写入。`IF NOT EXISTS` 让多个重试或并发 worker 安全地复用既有子表。
    若给出 `logical_key`，约束禁止同一事实在 `effective_range` 与 `knowledge_range` 同时重叠，
    从而保留“当时事实”和“系统何时知道它”的双时态可复验性。
    """
    table_name = f"{parent_table}_{year}"
    connection.execute(
        DDL(f"CREATE TABLE IF NOT EXISTS {table_name} PARTITION OF {parent_table} {range_sql}")
    )
    # 非 revision 明细没有双时态区间；仅创建按业务年度裁剪的物理子表即可。
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
    """确保成分快照日期所属月份的成分子表及证券反向查询索引已经存在。

    `snapshot_date` 是完整上游成分观察的业务日期；历史重放必须写回该月份，而不能按今天
    创建分区。月分区只优化存储和裁剪，不产生新的 ORM 类或改变快照不可变语义。函数不提交，
    因而分区创建、快照头、成员行与质量结果可随外层事务一起成功或回滚。
    """
    month_start = snapshot_date.replace(day=1)
    # 用月初和下一月初组成半开区间，避免月底天数和跨年计算的边界错误。
    next_month = (
        date(month_start.year + 1, 1, 1)
        if month_start.month == 12
        else date(month_start.year, month_start.month + 1, 1)
    )
    suffix = month_start.strftime("%Y%m")
    table_name = f"sector_membership_item_{suffix}"
    # 同一证券跨快照的追溯是内部读取和质量核验常见路径，索引避免全月分区扫描。
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
