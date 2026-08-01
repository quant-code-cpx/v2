"""指数六码字母数字身份约束迁移的真实 PostgreSQL 升降级回归。"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import String, create_engine, inspect, text
from sqlalchemy.engine import Engine, make_url

pytestmark = pytest.mark.integration

_PREVIOUS_REVISION = "202607300021"
_CURRENT_REVISION = "202607300022"
_CONSTRAINT_NAME = "ck_index_definition_source_code"


def test_index_identifier_alphanumeric_upgrade_and_guarded_downgrade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """升级允许六码字母数字和八码数字；有该行时回退失败，删除后才恢复旧规则。"""
    if os.environ.get("DATA_SYNC_RUN_INTEGRATION") != "1":
        pytest.skip("requires DATA_SYNC_RUN_INTEGRATION=1")
    database_url = os.environ.get("DATA_SYNC_DATABASE_URL")
    if database_url is None:
        pytest.skip("requires DATA_SYNC_DATABASE_URL")
    schema = f"index_identifier_migration_{uuid4().hex}"
    admin_engine = create_engine(database_url, pool_pre_ping=True)
    migration_url = _schema_url(database_url, schema=schema)
    migration_engine = create_engine(migration_url, pool_pre_ping=True)
    try:
        with admin_engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        config = Config("alembic.ini")
        # `ConfigParser` 把 URL 查询中的 `%3D` 当作插值；双写后 Alembic 仍能读取原始 URL。
        config.set_main_option("sqlalchemy.url", migration_url.replace("%", "%%"))
        # 迁移环境从受校验 `Settings` 重读 URL，必须将独占 schema 同步注入而非误迁移共享数据库。
        monkeypatch.setenv("DATA_SYNC_DATABASE_URL", migration_url)

        _seed_pre_upgrade_schema(migration_engine)
        command.upgrade(config, _CURRENT_REVISION)
        _assert_check_expression(migration_engine, "^[A-Z0-9]{6,8}$")
        _assert_column_length(migration_engine, 8)
        _insert_index_definition(migration_engine, code="H00999")
        _insert_index_definition(migration_engine, code="39926401")

        with pytest.raises(Exception, match="不能恢复旧列宽和约束"):
            command.downgrade(config, _PREVIOUS_REVISION)

        with migration_engine.begin() as connection:
            connection.execute(
                text(
                    "DELETE FROM index_definition "
                    "WHERE administrator_code = 'CSI' "
                    "AND source_index_code IN ('H00999', '39926401')"
                )
            )
        command.downgrade(config, _PREVIOUS_REVISION)
        _assert_check_expression(migration_engine, "^[0-9]{6}$")
        _assert_column_length(migration_engine, 6)
    finally:
        migration_engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin_engine.dispose()


def _insert_index_definition(engine: Engine, *, code: str) -> None:
    """插入最小中证观察身份，直接验证数据库检查而非仅验证 ORM 元数据。"""
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO index_definition (
                  index_id,
                  administrator_code,
                  source_index_code,
                  status,
                  created_at
                ) VALUES (
                  :index_id,
                  'CSI',
                  :source_index_code,
                  'observed',
                  :created_at
                )
                """
            ),
            {
                "index_id": uuid4(),
                "source_index_code": code,
                "created_at": datetime.now(UTC),
            },
        )


def _seed_pre_upgrade_schema(engine: Engine) -> None:
    """创建 0022 之前的最小真实表和版本标记，避免迁移回归测试锁住全仓库 schema。"""
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE index_definition (
                  index_id UUID PRIMARY KEY NOT NULL,
                  administrator_code VARCHAR(8) NOT NULL,
                  source_index_code VARCHAR(6) NOT NULL,
                  status VARCHAR(16) NOT NULL,
                  created_at TIMESTAMPTZ NOT NULL,
                  CONSTRAINT ck_index_definition_administrator
                    CHECK (administrator_code IN ('CSI', 'CNI')),
                  CONSTRAINT ck_index_definition_source_code
                    CHECK (source_index_code ~ '^[0-9]{6}$'),
                  CONSTRAINT ck_index_definition_status
                    CHECK (status IN ('observed', 'active', 'retired')),
                  CONSTRAINT uq_index_definition_administrator_code
                    UNIQUE (administrator_code, source_index_code)
                )
                """
            )
        )
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        connection.execute(
            text("INSERT INTO alembic_version (version_num) VALUES (:revision)"),
            {"revision": _PREVIOUS_REVISION},
        )


def _assert_check_expression(engine: Engine, expected_expression: str) -> None:
    """读取 PostgreSQL 实际约束表达式，避免测试只覆盖 Python 模型声明。"""
    constraints = inspect(engine).get_check_constraints("index_definition")
    expression = next(
        str(constraint["sqltext"])
        for constraint in constraints
        if constraint["name"] == _CONSTRAINT_NAME
    )
    assert expected_expression in expression


def _assert_column_length(engine: Engine, expected_length: int) -> None:
    """读取 PostgreSQL 实际列宽，确保八码身份不会只在 Python 层看似可用。"""
    column = next(
        column
        for column in inspect(engine).get_columns("index_definition")
        if column["name"] == "source_index_code"
    )
    column_type = column["type"]
    assert isinstance(column_type, String)
    assert column_type.length == expected_length


def _schema_url(database_url: str, *, schema: str) -> str:
    """把独占 schema 写入 psycopg `search_path`，不触碰共享测试表。"""
    url = make_url(database_url)
    query = dict(url.query)
    query["options"] = f"-csearch_path={schema}"
    return url.set(query=query).render_as_string(hide_password=False)
