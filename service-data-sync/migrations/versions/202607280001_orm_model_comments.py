"""将 `ORM` 模型中的中文业务说明写入 `PostgreSQL COMMENT`。

此迁移只变更数据库元数据，不改变表、字段、索引、约束或业务记录。说明来自生成时冻结的
快照，避免运行时依赖仍会演进的应用模型；回退也只清除本迁移写入的说明。
"""

from __future__ import annotations

import base64
import json
import zlib
from typing import Any

from alembic import op
from sqlalchemy import Connection, text

# revision identifiers, used by Alembic.
revision = "202607280001"
down_revision = "202607270004"
branch_labels = None
depends_on = None


# 注释快照在生成迁移时从 Declarative ORM metadata 导出，避免执行迁移时依赖会继续演进的应用源码。
_COMPRESSED_COMMENT_SNAPSHOT = (
    "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"
)


def upgrade() -> None:
    """为既有业务表和字段写入冻结快照的中文说明，不改变数据或结构。"""
    connection = op.get_bind()

    for table_name, specification in _comments().items():
        _set_table_comment(connection, table_name, specification["table"])
        for column_name, column_comment in specification["columns"].items():
            _set_column_comment(connection, table_name, column_name, column_comment)


def downgrade() -> None:
    """仅清除本迁移写入的说明，不恢复或删除业务数据，也不改变结构。"""
    connection = op.get_bind()

    for table_name, specification in _comments().items():
        _set_table_comment(connection, table_name, None)
        for column_name in specification["columns"]:
            _set_column_comment(connection, table_name, column_name, None)


def _comments() -> dict[str, dict[str, Any]]:
    """解压生成迁移时固定下来的表字段说明快照。"""
    payload = base64.b64decode(_COMPRESSED_COMMENT_SNAPSHOT)
    return json.loads(zlib.decompress(payload).decode("utf-8"))


def _set_table_comment(
    connection: Connection,
    table_name: str,
    comment: str | None,
) -> None:
    """按 PostgreSQL 标识符规则为指定表写入或移除说明。"""
    quoted_table_name = connection.dialect.identifier_preparer.quote(table_name)
    connection.execute(
        text(f"COMMENT ON TABLE {quoted_table_name} IS {_comment_literal(connection, comment)}")
    )


def _set_column_comment(
    connection: Connection,
    table_name: str,
    column_name: str,
    comment: str | None,
) -> None:
    """按 PostgreSQL 标识符规则为指定字段写入或移除说明。"""
    identifier_preparer = connection.dialect.identifier_preparer
    quoted_table_name = identifier_preparer.quote(table_name)
    quoted_column_name = identifier_preparer.quote(column_name)
    connection.execute(
        text(
            f"COMMENT ON COLUMN {quoted_table_name}.{quoted_column_name} "
            f"IS {_comment_literal(connection, comment)}"
        )
    )


def _comment_literal(connection: Connection, comment: str | None) -> str:
    """将固定迁移快照中的说明安全渲染为 PostgreSQL SQL 字面量。"""
    if comment is None:
        return "NULL"

    if "\x00" in comment:
        raise ValueError("数据库说明不能包含空字符")
    return "'" + comment.replace("'", "''") + "'"
