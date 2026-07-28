"""按时间创建物理分区；业务仓储只调用模型和本模块公开函数。"""

from __future__ import annotations

from datetime import date

from sqlalchemy import DDL
from sqlalchemy.orm import Session


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
