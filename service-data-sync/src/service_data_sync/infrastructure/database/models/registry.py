"""显式登记全部逻辑表模型，提供 Alembic 与架构测试唯一的 metadata 入口。

列表按业务域组织而非按迁移顺序；导入顺序不表达外键写入顺序，也不会执行数据库 I/O。新增
模型必须同时完成可迁移 schema、仓储边界和本注册表登记，否则迁移比对与模型覆盖测试无法
看见它；物理分区仍由父表模型和 `partition_manager` 管理，不应单独登记为业务表。
"""

from __future__ import annotations

from .base import Base

# 跨新数据域共享的来源、规范化、质量、发布与血缘支撑。
from .canonical import (
    CanonicalCheckpoint,
    CanonicalDataset,
    CanonicalRecordLineage,
    DatasetRelease,
    DataSource,
    MethodologyVersion,
    NormalizationRun,
    NormalizedRecordManifest,
    QualityEvaluation,
    QualityResult,
    QuarantineRecord,
    RawPayloadManifest,
    SourceDataset,
)
from .delivery_manifest import (
    DataOperationDeliveryManifest,
    DataOperationDeliveryManifestPage,
    StockConnectStatusCoverageBoundaryLock,
)

# 个股身份、生命周期和日线行情。
from .equity.backfill import (
    EquityBackfillChildResult,
    EquityBackfillChildSpec,
    EquityBackfillChildState,
    EquityBackfillPartitionCheckpoint,
    EquityBackfillPlan,
    EquityBackfillPlanIdentity,
    EquityBackfillPlanPage,
    EquityBackfillPlanSeal,
    EquityBackfillPlanSource,
    EquityBackfillPlanState,
    EquityReferenceGenerationAttempt,
    EquityReferenceGenerationStep,
)
from .equity.identity.equity_identifier_version import EquityIdentifierVersion
from .equity.identity.equity_identity_quarantine import EquityIdentityQuarantine
from .equity.identity.equity_instrument import EquityInstrument
from .equity.identity.equity_lifecycle_checkpoint import EquityLifecycleCheckpoint
from .equity.identity.equity_listing_status_version import EquityListingStatusVersion
from .equity.identity.equity_master_snapshot import EquityMasterSnapshot
from .equity.identity.equity_master_snapshot_member import EquityMasterSnapshotMember
from .equity.identity.equity_name_version import EquityNameVersion
from .equity.identity.equity_presence_anomaly import EquityPresenceAnomaly
from .equity.identity.equity_profile_version import EquityProfileVersion
from .equity.market_data.equity_adjustment_factor import EquityAdjustmentFactor
from .equity.market_data.equity_corporate_action_version import EquityCorporateActionVersion
from .equity.market_data.equity_daily_bar import EquityDailyBar
from .equity.market_data.equity_monthly_bar import EquityMonthlyBar
from .equity.market_data.equity_sync_checkpoint import EquitySyncCheckpoint
from .equity.market_data.equity_weekly_bar import EquityWeeklyBar
from .equity.workspace import (
    EquityDiscoveryAvailability,
    EquityDiscoveryMembership,
    EquityDiscoverySnapshot,
    EquityShareCapitalRevision,
    EquityTradingStatusRevision,
    SwMembershipItem,
    SwMembershipRelease,
)
from .etf import (
    EtfActionVersion,
    EtfDailyBarRevision,
    EtfNavRevision,
    EtfPremiumRevision,
    EtfProfileVersion,
    EtfShareRevision,
    EtfStatusRevision,
    EtfTrackingRelationVersion,
)

# 来源证据与运行账本。
from .execution.sync_partition import SyncPartition
from .execution.sync_run import SyncRun

# 财务报表、指标、估值、质量与发布。
from .financial.derived_financial_metric_revision import DerivedFinancialMetricRevision
from .financial.financial_change_checkpoint import FinancialChangeCheckpoint
from .financial.financial_derivation_input import FinancialDerivationInput
from .financial.financial_field_quarantine import FinancialFieldQuarantine
from .financial.financial_methodology import FinancialMethodology
from .financial.financial_metric_definition import FinancialMetricDefinition
from .financial.financial_publication import FinancialPublication
from .financial.financial_quality_result import FinancialQualityResult
from .financial.financial_report import FinancialReport
from .financial.financial_report_revision import FinancialReportRevision
from .financial.financial_statement_fact import FinancialStatementFact
from .financial.provider_financial_metric_revision import ProviderFinancialMetricRevision
from .financial.valuation_observation_revision import ValuationObservationRevision
from .index import (
    IndexCatalogObservation,
    IndexCatalogObservationItem,
    IndexDefinition,
    IndexObservedSnapshot,
    IndexObservedSnapshotItem,
)
from .market import (
    BlockTradeExecutionRevision,
    BusinessCompositionLabelVersion,
    BusinessCompositionLine,
    BusinessCompositionReportRevision,
    CorporateEarningsValue,
    CorporateEvent,
    CorporateEventRevision,
    DerivativeContract,
    DerivativeContractRevision,
    DerivativeDailyBarRevision,
    DerivativeProduct,
    DisclosureDocument,
    DisclosureDocumentRelation,
    DragonTigerEventRevision,
    DragonTigerSeatItem,
    EtfListing,
    FundLegalEntity,
    FundShareClass,
    InstrumentIdentifierVersion,
    InstrumentLifecycleVersion,
    MarginEligibilityRevision,
    MarginMarketDailyRevision,
    MarginSecurityDailyRevision,
    MarginSystemRiskDailyRevision,
    MarketCalendarDay,
    MarketEntity,
    MarketEntityRelationVersion,
    MarketInstrument,
    MarketOverviewActiveBundle,
    MarketOverviewBundle,
    MarketOverviewBundleComponent,
    MarketOverviewComponentRelease,
    MarketOverviewCurrentPointer,
    MarketOverviewDerivationInputPointer,
    MarketOverviewPointerTransition,
    MarketSessionVersion,
    RestrictedUnlockLot,
    ShareCapitalComponent,
    ShareholderHoldingAction,
    StockConnectActiveSecurityRevision,
    StockConnectBundlePublication,
    StockConnectBundleRollbackAudit,
    StockConnectCalendarObservation,
    StockConnectChannelDailyRevision,
    StockConnectChannelStatusRevision,
    StockConnectDisclosureRegime,
    StockConnectHkexInstrumentIdentity,
    StockConnectHoldingItem,
    StockConnectHoldingSnapshot,
    StockConnectMarketStatResearchBatch,
    StockConnectMarketStatResearchObservation,
    StockConnectOverviewGeneration,
    StockConnectOverviewGenerationComponent,
    StockConnectOverviewPublication,
    StockConnectReadinessCalendarDay,
    StockConnectReadinessSnapshot,
    TradingDisclosureReasonMapVersion,
    TradingVenue,
)
from .money_flow import (
    MoneyFlowBucketDefinition,
    MoneyFlowDailyObservation,
    MoneyFlowMethodology,
    MoneyFlowMethodologyScope,
    MoneyFlowMethodologyVersion,
    MoneyFlowMethodologyWindow,
    MoneyFlowQualityResult,
    MoneyFlowRankingItem,
    MoneyFlowRankingManifest,
    MoneyFlowRankingMetric,
    MoneyFlowRankingResearchItem,
    MoneyFlowRankingResearchMetric,
    MoneyFlowRankingResearchObservation,
    MoneyFlowRankingSnapshot,
    MoneyFlowSeries,
    MoneyFlowUniverseVersion,
)
from .operations import (
    DataOperationCommand,
    DataOperationEvent,
    DataOperationExecutionSlot,
    DataOperationHealthCheck,
    DataOperationHealthCheckTarget,
    DataOperationHealthEvaluation,
    DataOperationHealthIssue,
    DataOperationIdempotency,
    DataOperationPartition,
    DataOperationPreflight,
    DataOperationRun,
    DataOperationRunSourceBatch,
    DataOperationSchedule,
    DataOperationScheduleFire,
    DataOperationScheduleRevision,
)
from .provenance.source_batch import SourceBatch

# 发布版本与质量问题。
from .publication.data_quality_issue import DataQualityIssue
from .publication.dataset_availability_observation import DatasetAvailabilityObservation
from .publication.dataset_publication import DatasetPublication
from .publication.dataset_publication_component import DatasetPublicationComponent
from .publication.equity_bar_window_coverage import EquityBarWindowCoverage
from .publication.equity_event_window_coverage import EquityEventWindowCoverage

# 板块目录与日、周、月行情。
from .sector.catalog.sector_entity import SectorEntity
from .sector.catalog.sector_scheme import SectorScheme

# 板块 EOD 快照、明细和运行账本。
from .sector.eod.sector_eod_quality_result import SectorEodQualityResult
from .sector.eod.sector_eod_quote import SectorEodQuote
from .sector.eod.sector_eod_snapshot import SectorEodSnapshot
from .sector.eod.sector_eod_sync_partition import SectorEodSyncPartition
from .sector.market_data.sector_daily_bar import SectorDailyBar
from .sector.market_data.sector_monthly_bar import SectorMonthlyBar
from .sector.market_data.sector_weekly_bar import SectorWeeklyBar

# 板块成分观测、质量、区间与发布。
from .sector.membership.sector_membership_interval import SectorMembershipInterval
from .sector.membership.sector_membership_item import SectorMembershipItem
from .sector.membership.sector_membership_pending import SectorMembershipPending
from .sector.membership.sector_membership_quality_result import SectorMembershipQualityResult
from .sector.membership.sector_membership_quarantine import SectorMembershipQuarantine
from .sector.membership.sector_membership_release import SectorMembershipRelease
from .sector.membership.sector_membership_release_sector import SectorMembershipReleaseSector
from .sector.membership.sector_membership_snapshot import SectorMembershipSnapshot
from .sector.sw import (
    SwSectorClosure,
    SwSectorMethodology,
    SwSectorNodeRevision,
    SwSectorPublication,
    SwSectorQualityResult,
    SwSectorSyncCheckpoint,
    SwSectorValuationRevision,
)

# 该元组是“已登记逻辑表”的显式清单；用于防止遗漏模型，而非运行时建表顺序。
ALL_MODELS: tuple[type[Base], ...] = (
    CanonicalDataset,
    DataSource,
    SourceDataset,
    MethodologyVersion,
    RawPayloadManifest,
    NormalizationRun,
    NormalizedRecordManifest,
    QualityEvaluation,
    QualityResult,
    QuarantineRecord,
    DatasetRelease,
    CanonicalCheckpoint,
    CanonicalRecordLineage,
    IndexDefinition,
    IndexCatalogObservation,
    IndexCatalogObservationItem,
    IndexObservedSnapshot,
    IndexObservedSnapshotItem,
    TradingVenue,
    MarketEntity,
    MarketInstrument,
    InstrumentIdentifierVersion,
    InstrumentLifecycleVersion,
    MarketEntityRelationVersion,
    MarketCalendarDay,
    MarketSessionVersion,
    MarketOverviewActiveBundle,
    MarketOverviewComponentRelease,
    MarketOverviewBundle,
    MarketOverviewBundleComponent,
    MarketOverviewCurrentPointer,
    MarketOverviewDerivationInputPointer,
    MarketOverviewPointerTransition,
    FundLegalEntity,
    FundShareClass,
    EtfListing,
    DerivativeProduct,
    DerivativeContract,
    DerivativeContractRevision,
    DerivativeDailyBarRevision,
    DisclosureDocument,
    DisclosureDocumentRelation,
    BusinessCompositionReportRevision,
    BusinessCompositionLine,
    BusinessCompositionLabelVersion,
    CorporateEvent,
    CorporateEventRevision,
    CorporateEarningsValue,
    RestrictedUnlockLot,
    ShareCapitalComponent,
    ShareholderHoldingAction,
    DragonTigerEventRevision,
    DragonTigerSeatItem,
    BlockTradeExecutionRevision,
    TradingDisclosureReasonMapVersion,
    MarginMarketDailyRevision,
    MarginSecurityDailyRevision,
    MarginEligibilityRevision,
    MarginSystemRiskDailyRevision,
    StockConnectDisclosureRegime,
    StockConnectChannelDailyRevision,
    StockConnectActiveSecurityRevision,
    StockConnectCalendarObservation,
    StockConnectChannelStatusRevision,
    StockConnectBundleRollbackAudit,
    StockConnectBundlePublication,
    StockConnectHkexInstrumentIdentity,
    StockConnectOverviewGeneration,
    StockConnectOverviewGenerationComponent,
    StockConnectOverviewPublication,
    StockConnectReadinessCalendarDay,
    StockConnectReadinessSnapshot,
    StockConnectHoldingSnapshot,
    StockConnectHoldingItem,
    StockConnectMarketStatResearchBatch,
    StockConnectMarketStatResearchObservation,
    SourceBatch,
    SyncRun,
    SyncPartition,
    DataOperationDeliveryManifest,
    DataOperationDeliveryManifestPage,
    StockConnectStatusCoverageBoundaryLock,
    DataOperationIdempotency,
    DataOperationPreflight,
    DataOperationCommand,
    DataOperationRun,
    DataOperationRunSourceBatch,
    DataOperationPartition,
    DataOperationExecutionSlot,
    DataOperationEvent,
    DataOperationHealthCheck,
    DataOperationHealthCheckTarget,
    DataOperationHealthEvaluation,
    DataOperationHealthIssue,
    DataOperationSchedule,
    DataOperationScheduleRevision,
    DataOperationScheduleFire,
    DatasetPublication,
    DatasetAvailabilityObservation,
    EquityBarWindowCoverage,
    EquityEventWindowCoverage,
    DatasetPublicationComponent,
    DataQualityIssue,
    EquityBackfillPlan,
    EquityBackfillPlanState,
    EquityReferenceGenerationAttempt,
    EquityReferenceGenerationStep,
    EquityBackfillPlanIdentity,
    EquityBackfillPlanSource,
    EquityBackfillPlanPage,
    EquityBackfillPlanSeal,
    EquityBackfillChildSpec,
    EquityBackfillChildState,
    EquityBackfillPartitionCheckpoint,
    EquityBackfillChildResult,
    EquityInstrument,
    EquityIdentifierVersion,
    EquityNameVersion,
    EquityListingStatusVersion,
    EquityMasterSnapshot,
    EquityMasterSnapshotMember,
    EquityPresenceAnomaly,
    EquityIdentityQuarantine,
    EquityLifecycleCheckpoint,
    EquityProfileVersion,
    EquityDailyBar,
    EquityWeeklyBar,
    EquityMonthlyBar,
    EquityAdjustmentFactor,
    EquityCorporateActionVersion,
    EquitySyncCheckpoint,
    EquityTradingStatusRevision,
    EquityShareCapitalRevision,
    SwMembershipRelease,
    SwMembershipItem,
    EquityDiscoverySnapshot,
    EquityDiscoveryMembership,
    EquityDiscoveryAvailability,
    EtfProfileVersion,
    EtfTrackingRelationVersion,
    EtfDailyBarRevision,
    EtfNavRevision,
    EtfShareRevision,
    EtfStatusRevision,
    EtfActionVersion,
    EtfPremiumRevision,
    FinancialMethodology,
    FinancialMetricDefinition,
    FinancialReport,
    FinancialReportRevision,
    FinancialStatementFact,
    ProviderFinancialMetricRevision,
    DerivedFinancialMetricRevision,
    ValuationObservationRevision,
    FinancialFieldQuarantine,
    FinancialQualityResult,
    FinancialPublication,
    FinancialChangeCheckpoint,
    FinancialDerivationInput,
    MoneyFlowMethodology,
    MoneyFlowMethodologyVersion,
    MoneyFlowMethodologyScope,
    MoneyFlowMethodologyWindow,
    MoneyFlowBucketDefinition,
    MoneyFlowUniverseVersion,
    MoneyFlowSeries,
    MoneyFlowDailyObservation,
    MoneyFlowRankingSnapshot,
    MoneyFlowRankingItem,
    MoneyFlowRankingMetric,
    MoneyFlowRankingManifest,
    MoneyFlowRankingResearchObservation,
    MoneyFlowRankingResearchItem,
    MoneyFlowRankingResearchMetric,
    MoneyFlowQualityResult,
    SectorScheme,
    SectorEntity,
    SectorDailyBar,
    SectorWeeklyBar,
    SectorMonthlyBar,
    SectorMembershipSnapshot,
    SectorMembershipItem,
    SectorMembershipPending,
    SectorMembershipQuarantine,
    SectorMembershipQualityResult,
    SectorMembershipInterval,
    SectorMembershipRelease,
    SectorMembershipReleaseSector,
    SectorEodSyncPartition,
    SectorEodSnapshot,
    SectorEodQuote,
    SectorEodQualityResult,
    SwSectorMethodology,
    SwSectorNodeRevision,
    SwSectorClosure,
    SwSectorValuationRevision,
    SwSectorQualityResult,
    SwSectorPublication,
    SwSectorSyncCheckpoint,
)

__all__ = ["ALL_MODELS", "Base"]
