"""创建个股三周期行情逐证券窗口覆盖证据。

Revision ID: 202607300017
Revises: 202607300016
Create Date: 2026-07-30
"""

from __future__ import annotations

import re
from uuid import uuid4

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Connection

revision = "202607300017"
down_revision = "202607300016"
branch_labels = None
depends_on = None

_TABLE_NAME = "equity_bar_window_coverage"
_EXPECTED_COLUMNS = (
    ("coverage_id", "UUID", False),
    ("coverage_version", "UUID", False),
    ("period", "VARCHAR(8)", False),
    ("capability", "VARCHAR(100)", False),
    ("security_id", "BIGINT", False),
    ("identifier_version_id", "UUID", False),
    ("coverage_from", "DATE", False),
    ("coverage_to", "DATE", False),
    ("publication_id", "UUID", False),
    ("source_batch_id", "UUID", False),
    ("publication_kind", "VARCHAR(32)", False),
    ("quality_status", "VARCHAR(16)", False),
    ("record_count", "INTEGER", False),
    ("identity_hash", "CHAR(64)", False),
    ("universe_hash", "CHAR(64)", False),
    ("universe_size", "INTEGER", False),
    ("observed_at", "TIMESTAMP WITH TIME ZONE", False),
    ("created_at", "TIMESTAMP WITH TIME ZONE", False),
    ("superseded_at", "TIMESTAMP WITH TIME ZONE", True),
)
_EXPECTED_UNIQUES = {
    "equity_bar_window_coverage_coverage_version_key": ("coverage_version",),
    "uq_equity_bar_coverage_observation": (
        "capability",
        "security_id",
        "coverage_from",
        "coverage_to",
        "observed_at",
    ),
}
_EXPECTED_FOREIGN_KEYS = {
    "equity_bar_window_coverage_security_id_fkey": (
        ("security_id",),
        "equity_instrument",
        ("security_id",),
        "RESTRICT",
    ),
    "equity_bar_window_coverage_identifier_version_id_fkey": (
        ("identifier_version_id",),
        "equity_identifier_version",
        ("version_id",),
        "RESTRICT",
    ),
    "equity_bar_window_coverage_publication_id_fkey": (
        ("publication_id",),
        "dataset_publication",
        ("publication_id",),
        "RESTRICT",
    ),
    "equity_bar_window_coverage_source_batch_id_fkey": (
        ("source_batch_id",),
        "source_batch",
        ("source_batch_id",),
        "RESTRICT",
    ),
}
_EXPECTED_INDEXES = {
    "uq_equity_bar_coverage_current": (
        True,
        ("capability", "security_id", "coverage_from", "coverage_to"),
        "SUPERSEDED_AT IS NULL",
    ),
    "ix_equity_bar_coverage_read": (
        False,
        (
            "capability",
            "security_id",
            "coverage_from",
            "coverage_to",
            "created_at",
        ),
        None,
    ),
    "ix_equity_bar_coverage_source_batch": (
        False,
        ("source_batch_id",),
        None,
    ),
}
_EXPECTED_CHECKS = {
    "ck_equity_bar_coverage_period": "period IN ('1d', '1w', '1mo')",
    "ck_equity_bar_coverage_capability_period": (
        "(period = '1d' AND capability = 'equity.bar.1d.raw') "
        "OR (period = '1w' AND capability = 'equity.bar.1w.raw') "
        "OR (period = '1mo' AND capability = 'equity.bar.1mo.raw')"
    ),
    "ck_equity_bar_coverage_window": "coverage_from <= coverage_to",
    "ck_equity_bar_coverage_publication_kind": (
        "(publication_kind = 'DATA' AND record_count > 0) "
        "OR (publication_kind = 'ZERO_RECORD_COVERAGE' AND record_count = 0)"
    ),
    "ck_equity_bar_coverage_quality": "quality_status = 'passed'",
    "ck_equity_bar_coverage_identity": (
        "identity_hash ~ '^[0-9a-f]{64}$' "
        "AND universe_hash ~ '^[0-9a-f]{64}$' "
        "AND universe_size = 1"
    ),
    "ck_equity_bar_coverage_superseded": "superseded_at IS NULL OR superseded_at >= created_at",
}


def upgrade() -> None:
    """创建覆盖表；回退后保留的同名表必须完整匹配冻结 schema。"""
    connection = op.get_bind()
    if not inspect(connection).has_table(_TABLE_NAME):
        _create_table()
    _validate_preserved_schema(connection)


def downgrade() -> None:
    """保留不可变 coverage 和 publication 血缘，避免应用回退删除全量回填证据。

    旧应用不会读取该表；重新前滚会复核完整 schema 和原 OID，不会悄悄接纳回退期漂移。
    """


def _create_table() -> None:
    """使用自包含 Alembic 声明创建行情覆盖表，不导入运行时 ORM。"""
    op.create_table(
        _TABLE_NAME,
        sa.Column(
            "coverage_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="覆盖观察永久 UUID。",
        ),
        sa.Column(
            "coverage_version",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            unique=True,
            comment="进入回填结果清单的不可变覆盖版本。",
        ),
        sa.Column(
            "period", sa.String(length=8), nullable=False, comment="上游独立返回的日、周或月周期。"
        ),
        sa.Column(
            "capability",
            sa.String(length=100),
            nullable=False,
            comment="与周期严格对应的 provider-neutral capability。",
        ),
        sa.Column(
            "security_id", sa.BigInteger(), nullable=False, comment="被证明覆盖的永久证券身份。"
        ),
        sa.Column(
            "identifier_version_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="请求窗口采用的已确认交易所代码身份版本。",
        ),
        sa.Column(
            "coverage_from",
            sa.Date(),
            nullable=False,
            comment="来源成功检查的包含式起始行情日期。",
        ),
        sa.Column(
            "coverage_to",
            sa.Date(),
            nullable=False,
            comment="来源成功检查的包含式结束行情日期。",
        ),
        sa.Column(
            "publication_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="真实数据或零记录覆盖对应的 immutable canonical publication。",
        ),
        sa.Column(
            "source_batch_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            comment="产生本覆盖结论的精确真实来源观察。",
        ),
        sa.Column(
            "publication_kind",
            sa.String(length=32),
            nullable=False,
            comment="非空事实发布或通过质量门的零记录覆盖发布。",
        ),
        sa.Column(
            "quality_status",
            sa.String(length=16),
            nullable=False,
            comment="固定为 `passed`，失败来源不得形成覆盖。",
        ),
        sa.Column(
            "record_count",
            sa.Integer(),
            nullable=False,
            comment="本次来源响应在请求窗口内返回的标准行情条数。",
        ),
        sa.Column(
            "identity_hash",
            sa.CHAR(length=64),
            nullable=False,
            comment="证券与代码身份版本、不含窗口的稳定 SHA-256。",
        ),
        sa.Column(
            "universe_hash",
            sa.CHAR(length=64),
            nullable=False,
            comment="证券身份版本和本次闭区间组成的稳定 SHA-256。",
        ),
        sa.Column(
            "universe_size",
            sa.Integer(),
            nullable=False,
            comment="单证券行情窗口固定为一个身份分段。",
        ),
        sa.Column(
            "observed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="来源响应被实际观察到的时间。",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="覆盖完成质量门并进入数据库知识时间轴的时刻。",
        ),
        sa.Column(
            "superseded_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="相同精确窗口被更新观察替代的时间；当前结论为空。",
        ),
        sa.CheckConstraint(
            _EXPECTED_CHECKS["ck_equity_bar_coverage_period"],
            name="ck_equity_bar_coverage_period",
        ),
        sa.CheckConstraint(
            _EXPECTED_CHECKS["ck_equity_bar_coverage_capability_period"],
            name="ck_equity_bar_coverage_capability_period",
        ),
        sa.CheckConstraint(
            _EXPECTED_CHECKS["ck_equity_bar_coverage_window"],
            name="ck_equity_bar_coverage_window",
        ),
        sa.CheckConstraint(
            _EXPECTED_CHECKS["ck_equity_bar_coverage_publication_kind"],
            name="ck_equity_bar_coverage_publication_kind",
        ),
        sa.CheckConstraint(
            _EXPECTED_CHECKS["ck_equity_bar_coverage_quality"],
            name="ck_equity_bar_coverage_quality",
        ),
        sa.CheckConstraint(
            _EXPECTED_CHECKS["ck_equity_bar_coverage_identity"],
            name="ck_equity_bar_coverage_identity",
        ),
        sa.CheckConstraint(
            _EXPECTED_CHECKS["ck_equity_bar_coverage_superseded"],
            name="ck_equity_bar_coverage_superseded",
        ),
        sa.ForeignKeyConstraint(
            ["security_id"],
            ["equity_instrument.security_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["identifier_version_id"],
            ["equity_identifier_version.version_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["publication_id"],
            ["dataset_publication.publication_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_batch_id"],
            ["source_batch.source_batch_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("coverage_id"),
        sa.UniqueConstraint(
            "capability",
            "security_id",
            "coverage_from",
            "coverage_to",
            "observed_at",
            name="uq_equity_bar_coverage_observation",
        ),
        comment="个股三周期行情成功同步的逐证券窗口覆盖与零记录 publication 证据。",
    )
    op.create_index(
        "uq_equity_bar_coverage_current",
        _TABLE_NAME,
        ["capability", "security_id", "coverage_from", "coverage_to"],
        unique=True,
        postgresql_where=sa.text("superseded_at IS NULL"),
    )
    op.create_index(
        "ix_equity_bar_coverage_read",
        _TABLE_NAME,
        ["capability", "security_id", "coverage_from", "coverage_to", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_equity_bar_coverage_source_batch",
        _TABLE_NAME,
        ["source_batch_id"],
        unique=False,
    )


def _validate_preserved_schema(connection: Connection) -> None:
    """验证列、主键、唯一键、外键、检查约束和索引；任一漂移阻断前滚。"""
    inspector = inspect(connection)
    actual_columns = tuple(
        (
            str(column["name"]),
            column["type"].compile(dialect=connection.dialect).upper(),
            bool(column["nullable"]),
        )
        for column in inspector.get_columns(_TABLE_NAME)
    )
    if actual_columns != _EXPECTED_COLUMNS:
        raise RuntimeError("preserved equity bar coverage columns have drifted")
    primary_key = inspector.get_pk_constraint(_TABLE_NAME)
    if primary_key.get("name") != f"{_TABLE_NAME}_pkey" or tuple(
        primary_key.get("constrained_columns") or ()
    ) != ("coverage_id",):
        raise RuntimeError("preserved equity bar coverage primary key has drifted")
    actual_uniques = {
        str(item["name"]): tuple(item.get("column_names") or ())
        for item in inspector.get_unique_constraints(_TABLE_NAME)
    }
    if actual_uniques != _EXPECTED_UNIQUES:
        raise RuntimeError("preserved equity bar coverage unique constraints have drifted")
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
        raise RuntimeError("preserved equity bar coverage foreign keys have drifted")
    _validate_checks(connection, inspector.get_check_constraints(_TABLE_NAME))
    _validate_indexes(inspector.get_indexes(_TABLE_NAME))


def _validate_checks(
    connection: Connection,
    actual_checks: list[dict[str, object]],
) -> None:
    """借临时参考表比较 PostgreSQL 规范化 check 表达式，避免依赖源码空白。"""
    reference_name = f"_bar_coverage_expected_{uuid4().hex[:12]}"
    metadata = sa.MetaData()
    reference = sa.Table(
        reference_name,
        metadata,
        sa.Column("period", sa.String(length=8), nullable=False),
        sa.Column("capability", sa.String(length=100), nullable=False),
        sa.Column("coverage_from", sa.Date(), nullable=False),
        sa.Column("coverage_to", sa.Date(), nullable=False),
        sa.Column("publication_kind", sa.String(length=32), nullable=False),
        sa.Column("quality_status", sa.String(length=16), nullable=False),
        sa.Column("record_count", sa.Integer(), nullable=False),
        sa.Column("identity_hash", sa.CHAR(length=64), nullable=False),
        sa.Column("universe_hash", sa.CHAR(length=64), nullable=False),
        sa.Column("universe_size", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
        *(
            sa.CheckConstraint(expression, name=name)
            for name, expression in _EXPECTED_CHECKS.items()
        ),
        prefixes=["TEMPORARY"],
    )
    reference.create(connection)
    try:
        expected = {
            str(item["name"]): _normalize_sql(item.get("sqltext"))
            for item in inspect(connection).get_check_constraints(reference_name)
        }
    finally:
        reference.drop(connection)
    actual = {str(item["name"]): _normalize_sql(item.get("sqltext")) for item in actual_checks}
    if actual != expected:
        raise RuntimeError("preserved equity bar coverage check constraints have drifted")


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
        raise RuntimeError(f"preserved equity bar coverage indexes have drifted: {actual!r}")


def _index_predicate(index: dict[str, object]) -> str | None:
    """提取并规范 partial index 谓词；普通索引保持空值。"""
    options = index.get("dialect_options")
    if not isinstance(options, dict):
        return None
    predicate = options.get("postgresql_where")
    return None if predicate is None else _normalize_sql(predicate).upper()


def _normalize_sql(value: object) -> str:
    """压缩数据库规范表达式空白和无意义最外层括号。"""
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
