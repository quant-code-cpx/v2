"""同步服务当前目标 schema 的 Declarative 模型单元测试。"""

from __future__ import annotations

from service_data_sync.infrastructure.database.models.registry import ALL_MODELS, Base


def test_registry_explicitly_exposes_every_logical_business_table() -> None:
    """保证维护者只需查看显式 registry 即可发现全部逻辑业务表。"""
    expected_tables = {
        "source_batch",
        "sync_run",
        "sync_partition",
        "dataset_publication",
        "dataset_publication_component",
        "data_quality_issue",
        "equity_instrument",
        "equity_identifier_version",
        "equity_name_version",
        "equity_listing_status_version",
        "equity_master_snapshot",
        "equity_master_snapshot_member",
        "equity_presence_anomaly",
        "equity_identity_quarantine",
        "equity_daily_bar",
        "sector_scheme",
        "sector_entity",
        "sector_daily_bar",
        "sector_weekly_bar",
        "sector_monthly_bar",
        "sector_membership_snapshot",
        "sector_membership_item",
        "sector_membership_pending",
        "sector_membership_quarantine",
        "sector_membership_quality_result",
        "sector_membership_interval",
        "sector_membership_release",
        "sector_membership_release_sector",
        "sector_eod_sync_partition",
        "sector_eod_snapshot",
        "sector_eod_quote",
        "sector_eod_quality_result",
    }

    assert len(ALL_MODELS) == len(expected_tables)
    assert {model.__tablename__ for model in ALL_MODELS} == expected_tables
    assert set(Base.metadata.tables) == expected_tables


def test_every_model_has_chinese_database_comments() -> None:
    """保证模型本身就是维护者可直接阅读的表和字段数据字典入口。"""
    missing_table_comments = [
        table.name for table in Base.metadata.tables.values() if not table.comment
    ]
    missing_comments = [
        f"{table.name}.{column.name}"
        for table in Base.metadata.tables.values()
        for column in table.columns
        if not column.comment
    ]

    assert missing_table_comments == []
    assert missing_comments == []


def test_partitioned_parents_keep_physical_partition_policy_visible() -> None:
    """保证逻辑表模型显示分区策略，而不把物理子分区伪造成业务实体。"""
    assert (
        Base.metadata.tables["equity_daily_bar"].dialect_options["postgresql"]["partition_by"]
        == "RANGE (trade_date)"
    )
    assert (
        Base.metadata.tables["sector_membership_item"].dialect_options["postgresql"]["partition_by"]
        == "RANGE (snapshot_date)"
    )
