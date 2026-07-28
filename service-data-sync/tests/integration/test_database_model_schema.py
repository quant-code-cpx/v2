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


@pytest.mark.integration
def test_financial_statement_fact_keeps_partitioned_revision_foreign_key() -> None:
    """保证报表事实不能脱离同一报告期的报表 revision，即使 PostgreSQL 将约束下推到子分区。"""
    database = DatabaseClient.from_settings(load_settings())
    inspector = inspect(database.engine)
    try:
        foreign_keys = inspector.get_foreign_keys("financial_statement_fact", schema="public")
        assert any(
            set(foreign_key["constrained_columns"]) == {"report_period", "revision_id"}
            and foreign_key["referred_table"].startswith("financial_report_revision")
            for foreign_key in foreign_keys
        )
    finally:
        database.close()


@pytest.mark.integration
@pytest.mark.parametrize(
    ("table_name", "constrained_columns", "referred_table_prefix"),
    (
        (
            "financial_derivation_input",
            {"derived_report_period", "derived_metric_revision_id"},
            "derived_financial_metric_revision",
        ),
        (
            "financial_derivation_input",
            {"input_report_period", "input_revision_id"},
            "financial_report_revision",
        ),
        (
            "money_flow_ranking_item",
            {"target_trade_date", "snapshot_id"},
            "money_flow_ranking_snapshot",
        ),
        (
            "money_flow_ranking_manifest",
            {"target_trade_date", "snapshot_id"},
            "money_flow_ranking_snapshot",
        ),
        (
            "money_flow_ranking_metric",
            {"target_trade_date", "snapshot_id", "supplier_position"},
            "money_flow_ranking_item",
        ),
    ),
)
def test_partition_backed_composite_foreign_keys_remain_enforced(
    table_name: str,
    constrained_columns: set[str],
    referred_table_prefix: str,
) -> None:
    """保证 Alembic 忽略的分区传播噪声仍由 PostgreSQL 真实外键约束。"""
    database = DatabaseClient.from_settings(load_settings())
    inspector = inspect(database.engine)
    try:
        foreign_keys = inspector.get_foreign_keys(table_name, schema="public")
        assert any(
            set(foreign_key["constrained_columns"]) == constrained_columns
            and foreign_key["referred_table"].startswith(referred_table_prefix)
            for foreign_key in foreign_keys
        )
    finally:
        database.close()
