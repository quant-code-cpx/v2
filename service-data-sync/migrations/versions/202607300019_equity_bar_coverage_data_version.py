"""冻结个股三周期窗口覆盖的精确消费者数据版本。

Revision ID: 202607300019
Revises: 202607300018
Create Date: 2026-08-01
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect
from sqlalchemy.engine import Connection

revision = "202607300019"
down_revision = "202607300018"
branch_labels = None
depends_on = None

_TABLE_NAME = "equity_bar_window_coverage"
_COLUMN_NAME = "data_version"
_FOREIGN_KEY_NAME = "fk_equity_bar_coverage_data_version"
_FUNCTION_NAME = "guard_equity_bar_coverage_publication_pair"
_TRIGGER_NAME = "trg_equity_bar_coverage_publication_pair"


def upgrade() -> None:
    """追加精确 `data_version`，并以数据库门禁保证其永远匹配 publication。"""
    connection = op.get_bind()
    if _COLUMN_NAME not in _columns(connection):
        # 先允许空值以回填已有不可变行；完成 publication 对账后立即收紧为非空。
        op.add_column(
            _TABLE_NAME,
            sa.Column(
                _COLUMN_NAME,
                sa.UUID(),
                nullable=True,
                comment="与 publication_id 严格配对的消费者不可变数据版本。",
            ),
        )
    _backfill_data_versions(connection)
    op.alter_column(_TABLE_NAME, _COLUMN_NAME, nullable=False)
    if _FOREIGN_KEY_NAME not in _foreign_keys(connection):
        op.create_foreign_key(
            _FOREIGN_KEY_NAME,
            _TABLE_NAME,
            "dataset_publication",
            [_COLUMN_NAME],
            ["data_version"],
            ondelete="RESTRICT",
        )
    _install_guard(connection)
    _validate_schema(connection)


def downgrade() -> None:
    """保留数据版本列和门禁，确保回退不会抹除真实覆盖或让旧代码写出错配血缘。"""


def _columns(connection: Connection) -> dict[str, dict[str, object]]:
    """读取覆盖表列定义，供可重放前滚分辨初建与保留结构。"""
    return {str(column["name"]): column for column in inspect(connection).get_columns(_TABLE_NAME)}


def _foreign_keys(connection: Connection) -> dict[str, dict[str, object]]:
    """读取覆盖表外键，避免重放时重复创建同名约束。"""
    return {
        str(foreign_key["name"]): foreign_key
        for foreign_key in inspect(connection).get_foreign_keys(_TABLE_NAME)
    }


def _backfill_data_versions(connection: Connection) -> None:
    """从 publication 主键回填版本，并拒绝孤儿或已经错配的历史覆盖。"""
    connection.execute(
        sa.text(
            """
            UPDATE equity_bar_window_coverage AS coverage
            SET data_version = publication.data_version
            FROM dataset_publication AS publication
            WHERE coverage.publication_id = publication.publication_id
              AND coverage.data_version IS NULL
            """
        )
    )
    missing_count = connection.scalar(
        sa.text("SELECT count(*) FROM equity_bar_window_coverage WHERE data_version IS NULL")
    )
    if int(missing_count or 0) != 0:
        raise RuntimeError("equity bar coverage has a publication without a data version")
    mismatch_count = connection.scalar(
        sa.text(
            """
            SELECT count(*)
            FROM equity_bar_window_coverage AS coverage
            JOIN dataset_publication AS publication
              ON publication.publication_id = coverage.publication_id
            WHERE coverage.data_version <> publication.data_version
            """
        )
    )
    if int(mismatch_count or 0) != 0:
        raise RuntimeError("equity bar coverage data version does not match publication")


def _install_guard(connection: Connection) -> None:
    """安装 publication/version 配对和覆盖不可变性门禁，兼容旧写入方遗漏新列。"""
    connection.execute(
        sa.text(
            f"""
            CREATE OR REPLACE FUNCTION {_FUNCTION_NAME}()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            DECLARE
              expected_data_version uuid;
            BEGIN
              IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'equity bar coverage cannot be deleted'
                  USING ERRCODE = '55000';
              END IF;

              SELECT data_version
              INTO expected_data_version
              FROM dataset_publication
              WHERE publication_id = NEW.publication_id;
              IF expected_data_version IS NULL THEN
                RAISE EXCEPTION 'equity bar coverage publication is unavailable'
                  USING ERRCODE = '23503';
              END IF;
              IF NEW.data_version IS NULL THEN
                -- 回滚后的旧应用不认识新增列；由权威 publication 补齐，不能默认 latest。
                NEW.data_version := expected_data_version;
              ELSIF NEW.data_version <> expected_data_version THEN
                RAISE EXCEPTION 'equity bar coverage data version mismatches publication'
                  USING ERRCODE = '23514';
              END IF;

              IF TG_OP = 'UPDATE' THEN
                IF NEW.coverage_id IS DISTINCT FROM OLD.coverage_id
                   OR NEW.coverage_version IS DISTINCT FROM OLD.coverage_version
                   OR NEW.period IS DISTINCT FROM OLD.period
                   OR NEW.capability IS DISTINCT FROM OLD.capability
                   OR NEW.security_id IS DISTINCT FROM OLD.security_id
                   OR NEW.identifier_version_id IS DISTINCT FROM OLD.identifier_version_id
                   OR NEW.coverage_from IS DISTINCT FROM OLD.coverage_from
                   OR NEW.coverage_to IS DISTINCT FROM OLD.coverage_to
                   OR NEW.publication_id IS DISTINCT FROM OLD.publication_id
                   OR NEW.data_version IS DISTINCT FROM OLD.data_version
                   OR NEW.source_batch_id IS DISTINCT FROM OLD.source_batch_id
                   OR NEW.publication_kind IS DISTINCT FROM OLD.publication_kind
                   OR NEW.quality_status IS DISTINCT FROM OLD.quality_status
                   OR NEW.record_count IS DISTINCT FROM OLD.record_count
                   OR NEW.identity_hash IS DISTINCT FROM OLD.identity_hash
                   OR NEW.universe_hash IS DISTINCT FROM OLD.universe_hash
                   OR NEW.universe_size IS DISTINCT FROM OLD.universe_size
                   OR NEW.observed_at IS DISTINCT FROM OLD.observed_at
                   OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
                  RAISE EXCEPTION 'equity bar coverage immutable fields cannot change'
                    USING ERRCODE = '55000';
                END IF;
                IF OLD.superseded_at IS NOT NULL
                   OR NEW.superseded_at IS NULL
                   OR NEW.superseded_at < NEW.created_at THEN
                  RAISE EXCEPTION 'equity bar coverage may only be superseded once'
                    USING ERRCODE = '55000';
                END IF;
              END IF;
              RETURN NEW;
            END;
            $$;
            """
        )
    )
    connection.execute(sa.text(f"DROP TRIGGER IF EXISTS {_TRIGGER_NAME} ON {_TABLE_NAME}"))
    connection.execute(
        sa.text(
            f"""
            CREATE TRIGGER {_TRIGGER_NAME}
            BEFORE INSERT OR UPDATE OR DELETE ON {_TABLE_NAME}
            FOR EACH ROW
            EXECUTE FUNCTION {_FUNCTION_NAME}();
            """
        )
    )


def _validate_schema(connection: Connection) -> None:
    """复核列、外键和触发器，拒绝半完成迁移或未来结构漂移。"""
    column = _columns(connection).get(_COLUMN_NAME)
    if column is None or bool(column["nullable"]):
        raise RuntimeError("equity bar coverage data version column is invalid")
    column_type = column["type"].compile(dialect=connection.dialect).upper()
    if column_type != "UUID":
        raise RuntimeError("equity bar coverage data version type is invalid")
    foreign_key = _foreign_keys(connection).get(_FOREIGN_KEY_NAME)
    expected_foreign_key = (
        ("data_version",),
        "dataset_publication",
        ("data_version",),
        "RESTRICT",
    )
    actual_foreign_key = None
    if foreign_key is not None:
        actual_foreign_key = (
            tuple(foreign_key.get("constrained_columns") or ()),
            str(foreign_key.get("referred_table")),
            tuple(foreign_key.get("referred_columns") or ()),
            str((foreign_key.get("options") or {}).get("ondelete", "")).upper(),
        )
    if actual_foreign_key != expected_foreign_key:
        raise RuntimeError("equity bar coverage data version foreign key is invalid")
    trigger_exists = connection.scalar(
        sa.text(
            """
            SELECT EXISTS (
              SELECT 1
              FROM pg_trigger
              WHERE tgrelid = 'equity_bar_window_coverage'::regclass
                AND tgname = :trigger_name
                AND NOT tgisinternal
            )
            """
        ),
        {"trigger_name": _TRIGGER_NAME},
    )
    if trigger_exists is not True:
        raise RuntimeError("equity bar coverage data version trigger is missing")
