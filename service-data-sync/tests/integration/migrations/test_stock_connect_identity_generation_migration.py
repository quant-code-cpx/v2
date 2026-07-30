"""互联互通稳定身份与 generation staging 迁移回归。"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import make_url
from sqlalchemy.schema import CreateSchema, DropSchema

pytestmark = pytest.mark.integration


def test_stock_connect_identity_generation_upgrade_and_empty_downgrade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """在独占 schema 升到 300012，并验证空表时可安全退回 300011。"""
    if os.environ.get("DATA_SYNC_RUN_INTEGRATION") != "1":
        pytest.skip("requires DATA_SYNC_RUN_INTEGRATION=1")
    database_url = os.environ.get("DATA_SYNC_DATABASE_URL")
    if database_url is None:
        pytest.skip("requires DATA_SYNC_DATABASE_URL")
    schema = f"stock_connect_identity_generation_{uuid4().hex}"
    admin_engine = create_engine(database_url, pool_pre_ping=True)
    migration_url = _schema_url(database_url, schema=schema)
    migration_engine = create_engine(migration_url, pool_pre_ping=True)
    try:
        with admin_engine.begin() as connection:
            connection.execute(CreateSchema(schema))
        monkeypatch.setenv("DATA_SYNC_DATABASE_URL", migration_url)
        config = Config("alembic.ini")

        command.upgrade(config, "202607300012")

        upgraded = inspect(migration_engine)
        assert {
            "stock_connect_hkex_instrument_identity",
            "stock_connect_overview_generation",
            "stock_connect_overview_generation_component",
        } <= set(upgraded.get_table_names())
        component_foreign_keys = {
            value["name"]
            for value in upgraded.get_foreign_keys("stock_connect_overview_generation_component")
        }
        assert "fk_stock_connect_overview_component_generation" in component_foreign_keys

        command.downgrade(config, "202607300011")

        migration_engine.dispose()
        downgraded_engine = create_engine(migration_url, pool_pre_ping=True)
        try:
            assert not {
                "stock_connect_hkex_instrument_identity",
                "stock_connect_overview_generation",
                "stock_connect_overview_generation_component",
            } & set(inspect(downgraded_engine).get_table_names())
        finally:
            downgraded_engine.dispose()
    finally:
        migration_engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True))
        admin_engine.dispose()


def _schema_url(database_url: str, *, schema: str) -> str:
    """把 Alembic 与 inspector 都限制在一次性 schema，避免触碰共享测试表。"""
    url = make_url(database_url)
    options = f"-csearch_path={schema}"
    query = dict(url.query)
    query["options"] = options
    return url.set(query=query).render_as_string(hide_password=False)
