"""同步服务当前目标 schema 的 Declarative 模型单元测试。"""

from __future__ import annotations

from sqlalchemy import CheckConstraint, String
from sqlalchemy.dialects.postgresql import JSONB

from service_data_sync.infrastructure.database.models.registry import ALL_MODELS, Base


def test_registry_explicitly_exposes_every_logical_business_table() -> None:
    """保证维护者只需查看显式 registry 即可发现全部逻辑业务表。"""
    expected_tables = {
        "canonical_dataset",
        "data_source",
        "source_dataset",
        "methodology_version",
        "raw_payload_manifest",
        "normalization_run",
        "normalized_record_manifest",
        "quality_evaluation",
        "quality_result",
        "quarantine_record",
        "dataset_release",
        "canonical_checkpoint",
        "canonical_record_lineage",
        "index_definition",
        "index_catalog_observation",
        "index_catalog_observation_item",
        "index_observed_snapshot",
        "index_observed_snapshot_item",
        "trading_venue",
        "market_entity",
        "market_instrument",
        "instrument_identifier_version",
        "instrument_lifecycle_version",
        "market_entity_relation_version",
        "market_calendar_day",
        "market_session_version",
        "market_overview_component_release",
        "market_overview_bundle",
        "market_overview_bundle_component",
        "market_overview_current_pointer",
        "market_overview_active_bundle",
        "market_overview_pointer_transition",
        "market_overview_derivation_input_pointer",
        "fund_legal_entity",
        "fund_share_class",
        "etf_listing",
        "derivative_product",
        "derivative_contract",
        "derivative_contract_revision",
        "derivative_daily_bar_revision",
        "disclosure_document",
        "disclosure_document_relation",
        "business_composition_report_revision",
        "business_composition_line",
        "business_composition_label_version",
        "corporate_event",
        "corporate_event_revision",
        "corporate_earnings_value",
        "restricted_unlock_lot",
        "share_capital_component",
        "shareholder_holding_action",
        "dragon_tiger_event_revision",
        "dragon_tiger_seat_item",
        "block_trade_execution_revision",
        "trading_disclosure_reason_map_version",
        "etf_profile_version",
        "etf_tracking_relation_version",
        "etf_daily_bar_revision",
        "etf_nav_revision",
        "etf_share_revision",
        "etf_status_revision",
        "etf_action_version",
        "etf_premium_revision",
        "margin_market_daily_revision",
        "margin_security_daily_revision",
        "margin_eligibility_revision",
        "margin_system_risk_daily_revision",
        "stock_connect_disclosure_regime",
        "stock_connect_calendar_observation",
        "stock_connect_channel_daily_revision",
        "stock_connect_channel_status_revision",
        "stock_connect_active_security_revision",
        "stock_connect_bundle_publication",
        "stock_connect_bundle_rollback_audit",
        "stock_connect_hkex_instrument_identity",
        "stock_connect_overview_generation",
        "stock_connect_overview_generation_component",
        "stock_connect_readiness_snapshot",
        "stock_connect_readiness_calendar_day",
        "stock_connect_overview_publication",
        "stock_connect_holding_snapshot",
        "stock_connect_holding_item",
        "stock_connect_market_stat_research_batch",
        "stock_connect_market_stat_research_observation",
        "source_batch",
        "sync_run",
        "sync_partition",
        "data_operation_delivery_manifest",
        "data_operation_delivery_manifest_page",
        "stock_connect_status_coverage_boundary_lock",
        "data_operation_idempotency",
        "data_operation_preflight",
        "data_operation_command",
        "data_operation_run",
        "data_operation_run_source_batch",
        "data_operation_partition",
        "data_operation_execution_slot",
        "data_operation_event",
        "data_operation_health_check",
        "data_operation_health_check_target",
        "data_operation_health_evaluation",
        "data_operation_health_issue",
        "data_operation_schedule",
        "data_operation_schedule_revision",
        "data_operation_schedule_fire",
        "dataset_publication",
        "dataset_publication_component",
        "dataset_availability_observation",
        "equity_bar_window_coverage",
        "equity_event_window_coverage",
        "data_quality_issue",
        "equity_backfill_plan",
        "equity_backfill_plan_state",
        "equity_reference_generation_attempt",
        "equity_reference_generation_step",
        "equity_backfill_plan_identity",
        "equity_backfill_plan_source",
        "equity_backfill_plan_page",
        "equity_backfill_plan_seal",
        "equity_backfill_child_spec",
        "equity_backfill_child_state",
        "equity_backfill_partition_checkpoint",
        "equity_backfill_child_result",
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
        "equity_trading_status_revision",
        "equity_share_capital_revision",
        "sw_membership_release",
        "sw_membership_item",
        "equity_discovery_snapshot",
        "equity_discovery_membership",
        "equity_discovery_availability",
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
        "money_flow_ranking_research_observation",
        "money_flow_ranking_research_item",
        "money_flow_ranking_research_metric",
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


def test_shared_lifecycle_models_preserve_legacy_compatibility_and_release_lineage() -> None:
    """共享支撑表必须保留来源、质量、release 和既有表的 additive 兼容关联。"""
    source_batch = Base.metadata.tables["source_batch"]
    publication = Base.metadata.tables["dataset_publication"]
    release = Base.metadata.tables["dataset_release"]
    lineage = Base.metadata.tables["canonical_record_lineage"]

    assert source_batch.c.source_dataset_id.nullable is True
    assert publication.c.release_id.nullable is True
    assert {foreign_key.target_fullname for foreign_key in source_batch.foreign_keys} >= {
        "source_dataset.source_dataset_id"
    }
    assert {foreign_key.target_fullname for foreign_key in publication.foreign_keys} >= {
        "dataset_release.release_id"
    }
    assert {foreign_key.target_fullname for foreign_key in release.foreign_keys} >= {
        "canonical_dataset.dataset_id",
        "methodology_version.methodology_version_id",
        "normalization_run.normalization_run_id",
    }
    assert {foreign_key.target_fullname for foreign_key in lineage.foreign_keys} >= {
        "dataset_release.release_id",
        "raw_payload_manifest.raw_payload_id",
        "source_batch.source_batch_id",
    }
    assert {index.name for index in publication.indexes} >= {"uq_dataset_publication_release"}


def test_index_shadow_models_keep_observations_separate_from_pit_facts() -> None:
    """指数 P0-A 表仅关联来源与规范化运行，缺失来源日期或交易所时不伪造事实。"""
    definition = Base.metadata.tables["index_definition"]
    snapshot = Base.metadata.tables["index_observed_snapshot"]
    item = Base.metadata.tables["index_observed_snapshot_item"]

    source_code_constraint = next(
        constraint
        for constraint in definition.constraints
        if constraint.name == "ck_index_definition_source_code"
    )
    assert isinstance(source_code_constraint, CheckConstraint)
    assert "^[A-Z0-9]{6,8}$" in str(source_code_constraint.sqltext)
    assert snapshot.c.source_as_of_date.nullable is True
    assert item.c.source_exchange.nullable is True
    assert {foreign_key.target_fullname for foreign_key in snapshot.foreign_keys} >= {
        "canonical_dataset.dataset_id",
        "index_definition.index_id",
        "normalization_run.normalization_run_id",
        "source_batch.source_batch_id",
    }
    assert {constraint.name for constraint in snapshot.constraints} >= {
        "ck_index_observed_snapshot_kind",
        "ck_index_observed_snapshot_quality_status",
    }
    assert {constraint.name for constraint in item.constraints} >= {
        "ck_index_observed_snapshot_item_weight"
    }


def test_market_identity_models_preserve_cross_asset_boundaries() -> None:
    """市场根、可交易工具和新资产扩展必须保留双时间与资产域隔离。"""
    venue = Base.metadata.tables["trading_venue"]
    entity = Base.metadata.tables["market_entity"]
    instrument = Base.metadata.tables["market_instrument"]
    identifier = Base.metadata.tables["instrument_identifier_version"]
    contract = Base.metadata.tables["derivative_contract"]

    assert venue.c.reference_seed_revision.nullable is True
    assert {"entity_id", "entity_kind", "created_at", "retired_at"} == set(entity.c.keys())
    assert {foreign_key.target_fullname for foreign_key in instrument.foreign_keys} >= {
        "market_entity.entity_id",
        "market_entity.entity_kind",
        "trading_venue.venue_id",
    }
    assert {constraint.name for constraint in identifier.constraints} >= {
        "ex_instrument_identifier_code_time",
        "ex_instrument_identifier_entity_time",
    }
    assert {constraint.name for constraint in contract.constraints} >= {
        "ck_derivative_contract_option_structure"
    }


def test_etf_models_keep_price_nav_and_state_semantics_separate() -> None:
    """ETF 日行情、NAV、市场状态和折溢价必须分别建模并保留分区策略。"""
    bar = Base.metadata.tables["etf_daily_bar_revision"]
    nav = Base.metadata.tables["etf_nav_revision"]
    status = Base.metadata.tables["etf_status_revision"]
    premium = Base.metadata.tables["etf_premium_revision"]

    assert bar.dialect_options["postgresql"]["partition_by"] == "RANGE (trade_date)"
    assert nav.dialect_options["postgresql"]["partition_by"] == "RANGE (nav_date)"
    assert {constraint.name for constraint in status.constraints} >= {"ex_etf_status_time"}
    assert {constraint.name for constraint in premium.constraints} >= {
        "ck_etf_premium_value_comparability"
    }


def test_margin_and_stock_connect_models_preserve_disclosure_boundaries() -> None:
    """两融与沪深港通必须将来源统计、证券明细、制度和快照事实保持隔离。"""
    margin_security = Base.metadata.tables["margin_security_daily_revision"]
    regime = Base.metadata.tables["stock_connect_disclosure_regime"]
    holding_item = Base.metadata.tables["stock_connect_holding_item"]

    assert margin_security.dialect_options["postgresql"]["partition_by"] == "RANGE (trade_date)"
    assert {constraint.name for constraint in margin_security.constraints} >= {
        "ck_margin_security_repayment_source"
    }
    assert {constraint.name for constraint in regime.constraints} >= {
        "ex_stock_connect_regime_time"
    }
    assert {foreign_key.target_fullname for foreign_key in holding_item.foreign_keys} >= {
        "stock_connect_holding_snapshot.snapshot_date",
        "stock_connect_holding_snapshot.snapshot_id",
    }


def test_stock_connect_market_stat_research_models_cannot_reference_publication_or_pit() -> None:
    """AKShare 市场统计研究表只保留来源与规范化血缘，不能隐式接入正式港通读取。"""
    batch = Base.metadata.tables["stock_connect_market_stat_research_batch"]
    observation = Base.metadata.tables["stock_connect_market_stat_research_observation"]
    foreign_key_targets = {
        foreign_key.target_fullname
        for table in (batch, observation)
        for foreign_key in table.foreign_keys
    }

    assert batch.c.status.default is None
    assert batch.c.status.server_default is None
    assert observation.c.turnover_amount.nullable is True
    assert observation.c.field_availability.nullable is True
    assert isinstance(observation.c.field_availability.type, JSONB)
    assert observation.c.field_availability.type.none_as_null is True
    assert "dataset_release.release_id" not in foreign_key_targets
    assert "dataset_publication.publication_id" not in foreign_key_targets
    assert "stock_connect_channel_daily_revision.row_id" not in foreign_key_targets
    assert {constraint.name for constraint in batch.constraints} >= {
        "ck_sc_msr_batch_status",
        "uq_sc_msr_batch_source_batch",
    }


def test_equity_expansion_models_preserve_disclosure_event_and_trade_boundaries() -> None:
    """主营、公司事件和交易公开信息必须保持各自的原始事实边界。"""
    report = Base.metadata.tables["business_composition_report_revision"]
    document_relation = Base.metadata.tables["disclosure_document_relation"]
    event_revision = Base.metadata.tables["corporate_event_revision"]
    dragon_tiger = Base.metadata.tables["dragon_tiger_event_revision"]
    block_trade = Base.metadata.tables["block_trade_execution_revision"]

    assert report.dialect_options["postgresql"]["partition_by"] == "RANGE (report_period)"
    assert {constraint.name for constraint in document_relation.constraints} >= {
        "ck_disclosure_document_relation_not_self"
    }
    assert {foreign_key.target_fullname for foreign_key in event_revision.foreign_keys} >= {
        "corporate_event.event_id",
        "disclosure_document.document_id",
    }
    assert dragon_tiger.dialect_options["postgresql"]["partition_by"] == "RANGE (trade_date)"
    assert block_trade.dialect_options["postgresql"]["partition_by"] == "RANGE (trade_date)"


def test_derivative_models_keep_real_contracts_and_reported_prices_separate() -> None:
    """衍生品 P0 只保存真实合约及其日线，不把连续合约当作可交易合约。"""
    contract_revision = Base.metadata.tables["derivative_contract_revision"]
    daily_bar = Base.metadata.tables["derivative_daily_bar_revision"]

    assert {constraint.name for constraint in contract_revision.constraints} >= {
        "ex_derivative_contract_revision_time"
    }
    assert daily_bar.dialect_options["postgresql"]["partition_by"] == "RANGE (trade_date)"
    assert {constraint.name for constraint in daily_bar.constraints} >= {
        "ck_derivative_daily_bar_ohlc",
        "ck_derivative_daily_bar_non_negative_position",
    }


def test_etf_v2_public_text_columns_match_the_typed_contract_widths() -> None:
    """ETF v2 可公开文本在 ORM 层保持同一上限，不能依赖数据库额外余量或隐式截断。"""
    assert _string_length("etf_profile_version", "display_name") == 160
    assert _string_length("etf_profile_version", "etf_type") == 80
    assert _string_length("etf_profile_version", "management_mode") == 80
    assert _string_length("etf_profile_version", "manager_name") == 160
    assert _string_length("etf_profile_version", "custodian_name") == 160
    assert _string_length("etf_daily_bar_revision", "volume_unit") == 40
    assert _string_length("etf_daily_bar_revision", "trade_status") == 80
    assert _string_length("etf_status_revision", "status_code") == 80
    assert _string_length("etf_status_revision", "reason") == 500


def _string_length(table_name: str, column_name: str) -> int | None:
    """读取已确认的字符串列宽，使模型断言同时通过运行时和静态类型门禁。"""
    column_type = Base.metadata.tables[table_name].c[column_name].type
    assert isinstance(column_type, String)
    return column_type.length


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
