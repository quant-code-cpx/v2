"""修复财务指标不同报告期错误共享双时态逻辑键的问题。

Revision ID: 202607300014
Revises: 202607300013
Create Date: 2026-07-30
"""

from __future__ import annotations

from collections.abc import Iterable

from alembic import op
from sqlalchemy import text
from sqlalchemy.engine import Connection

revision = "202607300014"
down_revision = "202607300013"
branch_labels = None
depends_on = None

_TABLE_LOGICAL_KEYS = {
    "provider_financial_metric_revision": (
        "security_id WITH =, metric_id WITH =, methodology_id WITH =, "
        "period_basis WITH =, statement_scope WITH ="
    ),
    "derived_financial_metric_revision": (
        "security_id WITH =, metric_id WITH =, methodology_id WITH =, "
        "period_basis WITH =, statement_scope WITH =, formula_version WITH ="
    ),
}


def upgrade() -> None:
    """把报告期纳入排斥键，并恢复每个报告期 revision 的开放业务有效区间。"""
    connection = op.get_bind()
    for parent_table, logical_key in _TABLE_LOGICAL_KEYS.items():
        children = tuple(_partition_names(connection, parent_table))
        _drop_partition_constraints(connection, children)
        # 报告期本身已区分逻辑事实，当前 revision 应从其保守有效日起持续可见。
        connection.execute(text(f"UPDATE {parent_table} SET effective_to = NULL"))
        _add_partition_constraints(
            connection,
            children,
            logical_key=f"report_period WITH =, {logical_key}",
        )


def downgrade() -> None:
    """采用前滚兼容回退，保留正确约束、开放区间和全部报告期数据。

    旧约束遗漏 `report_period`，无法表示“同一指标、同一有效日起存在多个报告期”
    这一合法状态。应用版本回退不依赖该错误约束，因此数据库仅回退 Alembic revision
    标记，不破坏已经修正的 schema；随后重新升级会幂等重建相同正确约束。
    """


def _partition_names(connection: Connection, parent_table: str) -> Iterable[str]:
    """返回指定固定父表的全部直接分区名，避免硬编码年份范围。"""
    return connection.execute(
        text(
            """
            SELECT child.relname
            FROM pg_inherits
            JOIN pg_class AS parent ON parent.oid = pg_inherits.inhparent
            JOIN pg_class AS child ON child.oid = pg_inherits.inhrelid
            JOIN pg_namespace AS namespace ON namespace.oid = child.relnamespace
            WHERE parent.relname = :parent_table
              AND namespace.nspname = current_schema()
            ORDER BY child.relname
            """
        ),
        {"parent_table": parent_table},
    ).scalars()


def _drop_partition_constraints(connection: Connection, children: tuple[str, ...]) -> None:
    """删除旧年度分区约束，为逻辑键原子替换清出空间。"""
    preparer = connection.dialect.identifier_preparer
    for table_name in children:
        constraint_name = f"ex_{table_name}_bitemporal"
        connection.execute(
            text(
                f"ALTER TABLE {preparer.quote(table_name)} "
                f"DROP CONSTRAINT IF EXISTS {preparer.quote(constraint_name)}"
            )
        )


def _add_partition_constraints(
    connection: Connection,
    children: tuple[str, ...],
    *,
    logical_key: str,
) -> None:
    """以固定受控键重建年度分区的双时态排斥约束。"""
    preparer = connection.dialect.identifier_preparer
    for table_name in children:
        constraint_name = f"ex_{table_name}_bitemporal"
        connection.execute(
            text(
                f"ALTER TABLE {preparer.quote(table_name)} "
                f"ADD CONSTRAINT {preparer.quote(constraint_name)} EXCLUDE USING gist ("
                f"{logical_key}, effective_range WITH &&, knowledge_range WITH &&)"
            )
        )
