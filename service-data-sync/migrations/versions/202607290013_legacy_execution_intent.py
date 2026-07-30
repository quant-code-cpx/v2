"""为遗留入口的私有执行意图增加冻结字段。

该字段只允许 Python 兼容层写入，绝不属于 0022 HTTP 输入或 Run 公开投影；它使被保留的
CLI/Celery 参数可以随 run 冻结，而不会污染通用 SyncTarget。

Revision ID: 202607290013
Revises: 202607290012
Create Date: 2026-07-29
"""

from __future__ import annotations

from alembic import op

revision = "202607290013"
down_revision = "202607290012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """增加可空私有意图列，既有公开 command 继续使用空值。"""
    op.execute("ALTER TABLE data_operation_run ADD COLUMN execution_intent_json JSONB NULL;")


def downgrade() -> None:
    """回退私有意图列前保留已有 run 事实，非空记录禁止丢失。"""
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM data_operation_run WHERE execution_intent_json IS NOT NULL
          ) THEN
            RAISE EXCEPTION 'legacy execution intents prevent rollback';
          END IF;
        END $$;
        """
    )
    op.execute("ALTER TABLE data_operation_run DROP COLUMN execution_intent_json;")
