"""声明式模型元数据与已迁移 PostgreSQL 结构的集成检查。"""

from __future__ import annotations

import pytest
from sqlalchemy import inspect

from service_data_sync.bootstrap.settings import load_settings
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.database.models.registry import Base


@pytest.mark.integration
def test_models_cover_every_migrated_logical_table_and_column() -> None:
    """保证历史 migration 到 head 后，模型没有遗漏逻辑表或字段。"""
    database = DatabaseClient.from_settings(load_settings())
    inspector = inspect(database.engine)
    try:
        actual_tables = set(inspector.get_table_names(schema="public"))
        expected_tables = set(Base.metadata.tables)

        assert expected_tables <= actual_tables
        for table_name, model_table in Base.metadata.tables.items():
            actual_columns = inspector.get_columns(table_name, schema="public")
            actual_columns_by_name = {column["name"]: column for column in actual_columns}
            actual_column_names = set(actual_columns_by_name)
            assert {column.name for column in model_table.columns} == actual_column_names
            assert (
                inspector.get_table_comment(table_name, schema="public")["text"]
                == model_table.comment
            )
            for column in model_table.columns:
                assert actual_columns_by_name[column.name].get("comment") == column.comment

            primary_key = inspector.get_pk_constraint(table_name, schema="public")
            assert set(primary_key["constrained_columns"] or ()) == {
                column.name for column in model_table.primary_key.columns
            }
    finally:
        database.close()
