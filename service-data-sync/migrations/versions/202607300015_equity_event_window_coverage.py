"""创建证券事件逐证券窗口覆盖证据。

Revision ID: 202607300015
Revises: 202607300014
Create Date: 2026-07-30
"""

from __future__ import annotations

import re
from uuid import uuid4

from alembic import op
from sqlalchemy import CheckConstraint, Column, MetaData, Table, inspect
from sqlalchemy.engine import Connection

from service_data_sync.infrastructure.database.models.publication.equity_event_window_coverage import (  # noqa: E501
    EquityEventWindowCoverage,
)

revision = "202607300015"
down_revision = "202607300014"
branch_labels = None
depends_on = None

_TABLE_NAME = EquityEventWindowCoverage.__tablename__
_EXPECTED_UNIQUES = {
    "equity_event_window_coverage_coverage_version_key": ("coverage_version",),
    "uq_equity_event_coverage_observation": (
        "dataset",
        "event_family",
        "security_id",
        "coverage_from",
        "coverage_to",
        "observed_at",
    ),
}
_EXPECTED_FOREIGN_KEYS = {
    "equity_event_window_coverage_security_id_fkey": (
        ("security_id",),
        "equity_instrument",
        ("security_id",),
        "RESTRICT",
    ),
    "equity_event_window_coverage_identifier_version_id_fkey": (
        ("identifier_version_id",),
        "equity_identifier_version",
        ("version_id",),
        "RESTRICT",
    ),
    "equity_event_window_coverage_publication_id_fkey": (
        ("publication_id",),
        "dataset_publication",
        ("publication_id",),
        "RESTRICT",
    ),
    "equity_event_window_coverage_source_batch_id_fkey": (
        ("source_batch_id",),
        "source_batch",
        ("source_batch_id",),
        "RESTRICT",
    ),
}
_EXPECTED_INDEXES = {
    "uq_equity_event_coverage_current": (
        True,
        (
            "dataset",
            "event_family",
            "security_id",
            "coverage_from",
            "coverage_to",
        ),
        "SUPERSEDED_AT IS NULL",
    ),
    "ix_equity_event_coverage_read": (
        False,
        (
            "dataset",
            "event_family",
            "security_id",
            "coverage_from",
            "coverage_to",
            "created_at",
        ),
        None,
    ),
}


def upgrade() -> None:
    """创建覆盖表；若回退后表被保留，必须精确匹配冻结 schema 才可复用。"""
    connection = op.get_bind()
    inspector = inspect(connection)
    if not inspector.has_table(_TABLE_NAME):
        EquityEventWindowCoverage.__table__.create(bind=connection)
    _validate_preserved_schema(connection)


def downgrade() -> None:
    """保留前滚兼容表，使应用回退不会删除合法空窗和 dataVersion 证据。

    旧应用不会读取该表，保留它不会改变旧查询；重新升级只移动 Alembic revision，
    因而不需要停机复制或人为清空审计历史。
    """


def _validate_preserved_schema(connection: Connection) -> None:
    """验证列、约束、外键和索引；任何同名漂移都阻断重新升级。"""
    inspector = inspect(connection)
    _validate_columns(connection, inspector.get_columns(_TABLE_NAME))
    primary_key = inspector.get_pk_constraint(_TABLE_NAME)
    if primary_key.get("name") != f"{_TABLE_NAME}_pkey" or tuple(
        primary_key.get("constrained_columns") or ()
    ) != ("coverage_id",):
        raise RuntimeError("preserved equity event coverage primary key has drifted")
    actual_uniques = {
        str(item["name"]): tuple(item.get("column_names") or ())
        for item in inspector.get_unique_constraints(_TABLE_NAME)
    }
    if actual_uniques != _EXPECTED_UNIQUES:
        raise RuntimeError("preserved equity event coverage unique constraints have drifted")
    actual_foreign_keys = {
        str(item["name"]): (
            tuple(item.get("constrained_columns") or ()),
            str(item.get("referred_table")),
            tuple(item.get("referred_columns") or ()),
            str((item.get("options") or {}).get("ondelete", "")).upper(),
        )
        for item in inspector.get_foreign_keys(_TABLE_NAME)
    }
    if actual_foreign_keys != _EXPECTED_FOREIGN_KEYS:
        raise RuntimeError("preserved equity event coverage foreign keys have drifted")
    _validate_checks(connection, inspector.get_check_constraints(_TABLE_NAME))
    _validate_indexes(inspector.get_indexes(_TABLE_NAME))


def _validate_columns(
    connection: Connection,
    actual_columns: list[dict[str, object]],
) -> None:
    """按声明顺序比较列名、数据库类型、空值、默认值和注释。"""
    expected = tuple(
        (
            column.name,
            column.type.compile(dialect=connection.dialect).upper(),
            bool(column.nullable),
            None if column.server_default is None else str(column.server_default.arg),
            column.comment,
        )
        for column in EquityEventWindowCoverage.__table__.columns
    )
    actual = tuple(
        (
            str(column["name"]),
            column["type"].compile(dialect=connection.dialect).upper(),
            bool(column["nullable"]),
            column.get("default"),
            column.get("comment"),
        )
        for column in actual_columns
    )
    if actual != expected:
        raise RuntimeError("preserved equity event coverage columns have drifted")


def _validate_checks(
    connection: Connection,
    actual_checks: list[dict[str, object]],
) -> None:
    """借临时参考表比较 PostgreSQL 规范化后的 check 表达式，而非脆弱源码空白。"""
    reference_name = f"_event_coverage_expected_{uuid4().hex[:12]}"
    metadata = MetaData()
    reference = Table(
        reference_name,
        metadata,
        *[
            Column(column.name, column.type, nullable=column.nullable)
            for column in EquityEventWindowCoverage.__table__.columns
        ],
        *[
            CheckConstraint(str(constraint.sqltext), name=constraint.name)
            for constraint in EquityEventWindowCoverage.__table__.constraints
            if isinstance(constraint, CheckConstraint)
        ],
        prefixes=["TEMPORARY"],
    )
    reference.create(connection)
    try:
        expected_checks = {
            str(item["name"]): _normalize_sql(item.get("sqltext"))
            for item in inspect(connection).get_check_constraints(reference_name)
        }
    finally:
        reference.drop(connection)
    normalized_actual = {
        str(item["name"]): _normalize_sql(item.get("sqltext")) for item in actual_checks
    }
    if normalized_actual != expected_checks:
        raise RuntimeError("preserved equity event coverage check constraints have drifted")


def _validate_indexes(actual_indexes: list[dict[str, object]]) -> None:
    """比较非约束索引、列顺序、唯一性和 partial predicate。"""
    actual = {
        str(item["name"]): (
            bool(item.get("unique")),
            tuple(item.get("column_names") or ()),
            _index_predicate(item),
        )
        for item in actual_indexes
        if item.get("duplicates_constraint") is None
    }
    if actual != _EXPECTED_INDEXES:
        raise RuntimeError(f"preserved equity event coverage indexes have drifted: {actual!r}")


def _index_predicate(index: dict[str, object]) -> str | None:
    """提取并规范 partial index 谓词；普通索引保持空值。"""
    options = index.get("dialect_options")
    if not isinstance(options, dict):
        return None
    predicate = options.get("postgresql_where")
    return None if predicate is None else _normalize_sql(predicate).upper()


def _normalize_sql(value: object) -> str:
    """压缩数据库规范表达式的空白和无意义最外层括号。"""
    normalized = re.sub(r"\s+", " ", str(value)).strip()
    while normalized.startswith("(") and normalized.endswith(")"):
        inner = normalized[1:-1].strip()
        if not _balanced_parentheses(inner):
            break
        normalized = inner
    return normalized


def _balanced_parentheses(value: str) -> bool:
    """判断移除最外层括号后表达式是否仍保持括号平衡。"""
    depth = 0
    in_literal = False
    for character in value:
        if character == "'":
            in_literal = not in_literal
        elif not in_literal and character == "(":
            depth += 1
        elif not in_literal and character == ")":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0 and not in_literal
