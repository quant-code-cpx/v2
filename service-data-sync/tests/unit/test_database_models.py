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
        "equity_lifecycle_checkpoint",
        "equity_profile_version",
        "equity_daily_bar",
        "equity_weekly_bar",
        "equity_monthly_bar",
        "equity_adjustment_factor",
        "equity_corporate_action_version",
        "equity_sync_checkpoint",
        "financial_methodology",
        "financial_metric_definition",
        "financial_report",
        "financial_report_revision",
        "financial_statement_fact",
        "provider_financial_metric_revision",
        "derived_financial_metric_revision",
        "valuation_observation_revision",
        "financial_field_quarantine",
        "financial_quality_result",
        "financial_publication",
        "financial_change_checkpoint",
        "financial_derivation_input",
        "money_flow_methodology",
        "money_flow_methodology_version",
        "money_flow_methodology_scope",
        "money_flow_methodology_window",
        "money_flow_bucket_definition",
        "money_flow_universe_version",
        "money_flow_series",
        "money_flow_daily_observation",
        "money_flow_ranking_snapshot",
        "money_flow_ranking_item",
        "money_flow_ranking_metric",
        "money_flow_ranking_manifest",
        "money_flow_quality_result",
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
        "sw_sector_methodology",
        "sw_sector_node_revision",
        "sw_sector_closure",
        "sw_sector_valuation_revision",
        "sw_sector_quality_result",
        "sw_sector_publication",
        "sw_sector_sync_checkpoint",
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
        Base.metadata.tables["equity_weekly_bar"].dialect_options["postgresql"]["partition_by"]
        == "RANGE (period_end)"
    )
    assert (
        Base.metadata.tables["equity_monthly_bar"].dialect_options["postgresql"]["partition_by"]
        == "RANGE (period_end)"
    )
    assert (
        Base.metadata.tables["sector_membership_item"].dialect_options["postgresql"]["partition_by"]
        == "RANGE (snapshot_date)"
    )
    assert (
        Base.metadata.tables["financial_report_revision"].dialect_options["postgresql"][
            "partition_by"
        ]
        == "RANGE (report_period)"
    )
    assert (
        Base.metadata.tables["financial_statement_fact"].dialect_options["postgresql"][
            "partition_by"
        ]
        == "RANGE (report_period)"
    )
    assert (
        Base.metadata.tables["provider_financial_metric_revision"].dialect_options["postgresql"][
            "partition_by"
        ]
        == "RANGE (report_period)"
    )
    assert (
        Base.metadata.tables["derived_financial_metric_revision"].dialect_options["postgresql"][
            "partition_by"
        ]
        == "RANGE (report_period)"
    )
    assert (
        Base.metadata.tables["valuation_observation_revision"].dialect_options["postgresql"][
            "partition_by"
        ]
        == "RANGE (observation_date)"
    )
