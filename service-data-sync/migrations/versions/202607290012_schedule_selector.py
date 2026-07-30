"""为数据运维计划补充冻结业务选择器。

已有本地开发库可能已经应用 202607290011；因此通过独立迁移补齐 selector_json，而不重写已执行
的控制面基线迁移。历史计划只可能来自开发环境，使用 GLOBAL 作为安全回填值。

Revision ID: 202607290012
Revises: 202607290011
Create Date: 2026-07-29
"""

from __future__ import annotations

from alembic import op

revision = "202607290012"
down_revision = "202607290011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """新增不可为空选择器，并为已存在计划回填受限 GLOBAL 目标。"""
    op.execute("ALTER TABLE data_operation_schedule ADD COLUMN selector_json JSONB NULL;")
    op.execute(
        'UPDATE data_operation_schedule SET selector_json = \'{"kind": "GLOBAL"}\'::jsonb '
        "WHERE selector_json IS NULL;"
    )
    op.execute("ALTER TABLE data_operation_schedule ALTER COLUMN selector_json SET NOT NULL;")


def downgrade() -> None:
    """回退新增列；调用方需先确认不会丢失计划目标语义。"""
    op.execute("ALTER TABLE data_operation_schedule DROP COLUMN selector_json;")
