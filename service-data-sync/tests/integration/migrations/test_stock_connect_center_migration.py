"""互联互通中心 300004 迁移的真实 PostgreSQL 升降级回归。"""

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


def test_stock_connect_center_upgrade_and_empty_downgrade() -> None:
    """在独占 schema 从 300003 升到 300004，再验证空数据时可完整回退。"""
    if os.environ.get("DATA_SYNC_RUN_INTEGRATION") != "1":
        pytest.skip("requires DATA_SYNC_RUN_INTEGRATION=1")
    database_url = os.environ.get("DATA_SYNC_DATABASE_URL")
    if database_url is None:
        pytest.skip("requires DATA_SYNC_DATABASE_URL")
    schema = f"stock_connect_migration_{uuid4().hex}"
    admin_engine = create_engine(database_url, pool_pre_ping=True)
    migration_url = _schema_url(database_url, schema=schema)
    migration_engine = create_engine(migration_url, pool_pre_ping=True)
    try:
        with admin_engine.begin() as connection:
            connection.execute(CreateSchema(schema))
        config = Config("alembic.ini")
        config.set_main_option("sqlalchemy.url", migration_url)

        command.upgrade(config, "202607300003")
        command.upgrade(config, "202607300004")

        upgraded = inspect(migration_engine)
        assert {
            "stock_connect_calendar_observation",
            "stock_connect_channel_status_revision",
            "stock_connect_bundle_publication",
            "stock_connect_overview_publication",
        } <= set(upgraded.get_table_names())
        daily_columns = {
            column["name"]
            for column in upgraded.get_columns("stock_connect_channel_daily_revision")
        }
        active_columns = {
            column["name"]
            for column in upgraded.get_columns("stock_connect_active_security_revision")
        }
        assert {
            "trade_count",
            "etf_turnover_amount",
            "field_availability",
            "center_schema_version",
        } <= daily_columns
        assert {
            "source_instrument_code",
            "source_instrument_name",
            "identity_status",
            "field_availability",
        } <= active_columns

        command.downgrade(config, "202607300003")

        migration_engine.dispose()
        downgraded_engine = create_engine(migration_url, pool_pre_ping=True)
        try:
            downgraded = inspect(downgraded_engine)
            assert "stock_connect_bundle_publication" not in set(downgraded.get_table_names())
            downgraded_daily = {
                column["name"]
                for column in downgraded.get_columns("stock_connect_channel_daily_revision")
            }
            downgraded_active = {
                column["name"]
                for column in downgraded.get_columns("stock_connect_active_security_revision")
            }
            assert "center_schema_version" not in downgraded_daily
            assert "source_instrument_code" not in downgraded_active
        finally:
            downgraded_engine.dispose()
    finally:
        migration_engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True, if_exists=True))
        admin_engine.dispose()


def _schema_url(database_url: str, *, schema: str) -> str:
    """把独占 schema 写入 psycopg `search_path`，不触碰共享测试表。"""
    url = make_url(database_url)
    query = dict(url.query)
    query["options"] = f"-csearch_path={schema}"
    return url.set(query=query).render_as_string(hide_password=False)
