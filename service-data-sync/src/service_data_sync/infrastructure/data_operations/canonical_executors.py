"""把已审核数据运维 target 映射到既有 canonical 同步用例。

此模块只由统一 dispatcher 注册。它不暴露 CLI、Celery 或 HTTP 入口，因此旧入口无法绕过
PostgreSQL ExecutionSlot 与 fencing token 直接调用同步 use case。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from functools import partial
from math import ceil
from time import monotonic, sleep
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select

from service_data_sync.application.corporate_events.sync import CorporateEventsSyncService
from service_data_sync.application.derivative.daily_bar_sync import DerivativeDailyBarSyncService
from service_data_sync.application.equity.daily_bar_sync import EquityDailyBarSyncService
from service_data_sync.application.equity.lifecycle_sync import EquityLifecycleSyncService
from service_data_sync.application.equity.market_extension_sync import (
    EquityAdjustmentFactorSyncService,
    EquityCompanyProfileSyncService,
    EquityCorporateActionSyncService,
    EquityPeriodBarSyncService,
)
from service_data_sync.application.equity.master_catalog_sync import EquityCatalogSyncService
from service_data_sync.application.equity.workspace_sync import (
    EquityShareCapitalSyncService,
    EquityTradingStatusSyncService,
    SwMembershipSyncService,
)
from service_data_sync.application.etf.daily_bar_sync import EtfDailyBarSyncService
from service_data_sync.application.etf.nav_sync import EtfNavSyncService
from service_data_sync.application.etf.reference_sync import (
    EtfMasterSyncService,
    EtfStatusSyncService,
)
from service_data_sync.application.financial.sync import FinancialSyncService
from service_data_sync.application.index.shadow_sync import IndexShadowSyncService
from service_data_sync.application.margin.market_daily_sync import MarginMarketDailySyncService
from service_data_sync.application.margin.security_sync import (
    MarginEligibilitySyncService,
    MarginSecurityDailySyncService,
)
from service_data_sync.application.market_overview.sync import MarketOverviewSyncService
from service_data_sync.application.money_flow.sync import MoneyFlowSyncService
from service_data_sync.application.ports.data_source import (
    DataSourcePort,
    ProviderError,
    ProviderErrorCode,
    SourcePreflightVerificationPort,
    SourceRequest,
)
from service_data_sync.application.ports.delivery_manifest import (
    DeliveryManifestIntegrityError,
    DeliveryManifestPageDescriptor,
    DeliveryManifestUnavailable,
)
from service_data_sync.application.ports.financial_sync import FinancialPublicationResult
from service_data_sync.application.ports.market_overview import MarketOverviewRepository
from service_data_sync.application.ports.sector_eod import SectorEodExecutionMode
from service_data_sync.application.sector.bar_sync import SectorBarSyncService
from service_data_sync.application.sector.catalog_sync import SectorCatalogSyncService
from service_data_sync.application.sector.eod_schedule import (
    sector_eod_scheduler_target_date,
    sector_eod_source_cutoff_at,
)
from service_data_sync.application.sector.eod_snapshot_sync import SectorEodSnapshotSyncService
from service_data_sync.application.sector.membership_sync import (
    SectorMembershipSyncResult,
    SectorMembershipSyncService,
)
from service_data_sync.application.sector.sw_snapshot_sync import (
    SwSnapshotSyncResult,
    SwSnapshotSyncService,
)
from service_data_sync.application.stock_connect.bundle_sync import (
    StockConnectDailyBundleSyncService,
)
from service_data_sync.application.stock_connect_research.market_stat_sync import (
    StockConnectMarketStatResearchSyncService,
)
from service_data_sync.application.trading_events.sync import (
    BlockTradeSyncService,
    DragonTigerSyncService,
)
from service_data_sync.bootstrap.container import ServiceContainer
from service_data_sync.bootstrap.financial_derived import run_financial_derivation
from service_data_sync.domain.derivative import DerivativeContractIdentifier
from service_data_sync.domain.equity import EquityBarPeriod, EquityIdentifier, Exchange
from service_data_sync.domain.etf import EtfIdentifier
from service_data_sync.domain.index import IndexAdministrator, IndexCapability, IndexIdentifier
from service_data_sync.domain.margin import MarginVenue
from service_data_sync.domain.sector import (
    SectorIdentifier,
    SectorPeriod,
    SectorScheme,
)
from service_data_sync.domain.stock_connect import StockConnectChannel
from service_data_sync.infrastructure.data_operations.control_plane import (
    DataOperationsControlPlane,
    ExecutionClaim,
    ExecutionOutcome,
    money_flow_source_capability,
)
from service_data_sync.infrastructure.data_operations.equity_backfill_checkpoint import (
    completed_equity_bar_partitions,
    completed_equity_event_partitions,
    equity_backfill_event_partition_keys,
    equity_backfill_partition_key,
    finalize_equity_bar_partitions,
    finalize_equity_event_partitions,
    record_equity_bar_partition,
    record_equity_event_partitions,
)
from service_data_sync.infrastructure.database.fenced_execution import (
    FencedExecution,
    current_fenced_execution,
)
from service_data_sync.infrastructure.database.models.operations import (
    DataOperationPartition,
    DataOperationRun,
)
from service_data_sync.infrastructure.database.models.publication.dataset_publication import (
    DatasetPublication,
)
from service_data_sync.infrastructure.database.models.sector.sw import (
    SwSectorNodeRevision,
    SwSectorPublication,
)
from service_data_sync.infrastructure.object_storage.raw_payload_store import (
    FailureEvidenceDataSource,
    S3RawPayloadStore,
    retain_failure_evidence,
)
from service_data_sync.infrastructure.persistence import (
    stock_connect_market_stat_research_repository as stock_connect_research_repository,
)
from service_data_sync.infrastructure.persistence.corporate_events_repository import (
    CorporateSourceApproval,
    SqlAlchemyCorporateEventsRepository,
)
from service_data_sync.infrastructure.persistence.dataset_availability_repository import (
    SqlAlchemyDatasetAvailabilityRepository,
)
from service_data_sync.infrastructure.persistence.delivery_manifest_repository import (
    SqlAlchemyDeliveryManifestRepository,
)
from service_data_sync.infrastructure.persistence.derivative_market_data_repository import (
    DerivativeSourceApproval,
    SqlAlchemyDerivativeDailyBarRepository,
)
from service_data_sync.infrastructure.persistence.equity_discovery_repository import (
    SqlAlchemyEquityDiscoveryRepository,
)
from service_data_sync.infrastructure.persistence.equity_lifecycle_repository import (
    SqlAlchemyEquityLifecycleRepository,
)
from service_data_sync.infrastructure.persistence.equity_market_data_repository import (
    SqlAlchemyEquityMarketDataRepository,
)
from service_data_sync.infrastructure.persistence.equity_master_repository import (
    SqlAlchemyEquityMasterRepository,
)
from service_data_sync.infrastructure.persistence.equity_master_resolved_repository import (
    SqlAlchemyResolvedEquityMasterRepository,
)
from service_data_sync.infrastructure.persistence.equity_workspace_repository import (
    EquityWorkspaceSourceApproval,
    SqlAlchemyEquityWorkspaceRepository,
)
from service_data_sync.infrastructure.persistence.etf_market_data_repository import (
    EtfSourceApproval,
    SqlAlchemyEtfMarketDataRepository,
)
from service_data_sync.infrastructure.persistence.etf_reference_repository import (
    EtfReferenceSourceApproval,
    SqlAlchemyEtfReferenceRepository,
)
from service_data_sync.infrastructure.persistence.etf_universe_repository import (
    EtfUniverseUnavailable,
    load_frozen_etf_universe,
)
from service_data_sync.infrastructure.persistence.financial_sync_repository import (
    SqlAlchemyFinancialSyncRepository,
)
from service_data_sync.infrastructure.persistence.index_shadow_repository import (
    SqlAlchemyIndexShadowRepository,
)
from service_data_sync.infrastructure.persistence.margin_market_data_repository import (
    MarginSourceApproval,
    SqlAlchemyMarginMarketDataRepository,
)
from service_data_sync.infrastructure.persistence.market_overview_repository import (
    SqlAlchemyMarketOverviewRepository,
)
from service_data_sync.infrastructure.persistence.money_flow_repository import (
    SqlAlchemyMoneyFlowRepository,
)
from service_data_sync.infrastructure.persistence.sector_eod_repository import (
    SqlAlchemySectorEodRepository,
)
from service_data_sync.infrastructure.persistence.sector_market_data_repository import (
    SqlAlchemySectorMarketDataRepository,
)
from service_data_sync.infrastructure.persistence.sector_membership_repository import (
    SqlAlchemySectorMembershipRepository,
)
from service_data_sync.infrastructure.persistence.stock_connect_center_repository import (
    SqlAlchemyStockConnectCenterRepository,
)
from service_data_sync.infrastructure.persistence.stock_connect_market_data_repository import (
    SqlAlchemyStockConnectMarketDataRepository,
    StockConnectSourceApproval,
)
from service_data_sync.infrastructure.persistence.sw_sector_repository import (
    SqlAlchemySwSectorRepository,
)
from service_data_sync.infrastructure.persistence.trading_events_repository import (
    SqlAlchemyTradingEventsRepository,
    TradingEventsSourceApproval,
)
from service_data_sync.infrastructure.providers.official.stock_connect import (
    stock_connect_bundle_targets_from_evidence,
    stock_connect_preflight_evidence_from_delivery_page,
)

from ..database.models.equity.identity.equity_identifier_version import (
    EquityIdentifierVersion,
)
from ..database.models.equity.identity.equity_instrument import EquityInstrument
from ..database.models.sector.catalog.sector_entity import SectorEntity


@dataclass(frozen=True, slots=True)
class _EquityBarPartitionResult:
    """统一日、周、月行情窗口的真实 publication 与 coverage 返回值。"""

    inserted_count: int
    unchanged_count: int
    data_version: UUID
    coverage_version: UUID
    source_batch_id: UUID
    publication_kind: str


_HISTORY_START = date(1990, 12, 19)
_EQUITY_BAR_PERIODS: dict[str, EquityBarPeriod] = {
    "equity.bar.1d.raw": EquityBarPeriod.DAY_1,
    "equity.bar.1w.raw": EquityBarPeriod.WEEK_1,
    "equity.bar.1mo.raw": EquityBarPeriod.MONTH_1,
}
_EQUITY_REFERENCE_CAPABILITIES = frozenset(
    {"equity.adjustment_factor", "equity.corporate_action", "equity.profile"}
)
_FINANCIAL_EXECUTIONS = {
    "financial.report": "financial.statement.raw",
    "financial.provider-metric": "financial.metric.raw",
    "financial.valuation": "financial.valuation.raw",
}
_FINANCIAL_DERIVED_DATASET = "financial.derived-metric"
_SECTOR_CATALOG_CAPABILITY = "sector.catalog.raw"
_SECTOR_BAR_PERIODS: dict[str, SectorPeriod] = {
    "sector.bar.1d.raw": SectorPeriod.DAY_1,
    "sector.bar.1w.raw": SectorPeriod.WEEK_1,
    "sector.bar.1mo.raw": SectorPeriod.MONTH_1,
}
_SECTOR_BAR_EXECUTION_BATCH_DAYS = 1
_SECTOR_MEMBERSHIP_DATASET = "sector.membership.release"
_SECTOR_MEMBERSHIP_CAPABILITY = "sector.membership.snapshot.raw"
_SECTOR_EOD_CAPABILITY = "sector.quote.eod.snapshot.raw"
_SECTOR_SW_CAPABILITY = "sector.sw.snapshot.raw"
_SECTOR_SW_SCHEME = "sw.industry"
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_ETF_EXECUTIONS: dict[str, tuple[str, str]] = {
    "fund.etf.profile.reported": ("MASTER", "fund.etf.master"),
    "fund.etf.trading_state.reported": ("STATUS", "fund.etf.trading_state"),
    "fund.etf.bar.1d.reported": ("BARS", "fund.etf.bar.1d.raw"),
    "fund.etf.nav.1d.reported": ("NAV", "fund.etf.nav.1d.reported"),
}
_MARGIN_EXECUTIONS: dict[str, tuple[str, str]] = {
    "market.margin.market.1d.reported": ("MARKET", "market.margin.market.1d.reported"),
    "market.margin.security.1d.reported": ("SECURITY", "market.margin.security.1d.reported"),
    "market.margin.eligibility.reported": ("ELIGIBILITY", "market.margin.eligibility.reported"),
}
_MARGIN_EXECUTION_BATCH_DAYS = 5
_STOCK_CONNECT_RESEARCH_DATASET = "market.stock_connect.market_stat.research"
_STOCK_CONNECT_RESEARCH_CAPABILITY = "market.stock_connect.market_stat.reported"
_STOCK_CONNECT_RESEARCH_EXECUTION_BATCH_DAYS = 5
_DERIVATIVE_DATASET = "derivative.bar.1d.reported"
_DERIVATIVE_CAPABILITY = "derivative.bar.1d.reported"
_DERIVATIVE_EXECUTION_BATCH_DAYS = 31
_MONEY_FLOW_DAILY_DATASET = "money_flow.daily"
_MONEY_FLOW_RANKING_DATASET = "money_flow.ranking"
_EQUITY_TRADING_STATUS_CAPABILITY = "equity.trading_status.1d"
_EQUITY_SHARE_CAPITAL_CAPABILITY = "equity.share_capital.reported"
_EQUITY_DISCOVERY_DATASET = "equity.discovery.eod"
_EQUITY_MASTER_DATASET = "equity.master.cn-a"
_EQUITY_MASTER_CAPABILITY = "equity.master.catalog"
_EQUITY_LIFECYCLE_CAPABILITY = "equity.lifecycle.explicit"
_EQUITY_MASTER_RESOLVED_DATASET = "equity.master.resolved"
_SW_MEMBERSHIP_CAPABILITY = "sector.sw2021.membership.snapshot"
_STOCK_CONNECT_BUNDLE_DATASET = "market.stock_connect.overview.bundle"
_STOCK_CONNECT_EXECUTION_BATCH_DAYS = 5
_MARKET_OVERVIEW_BUNDLE_DATASET = "market.overview-and-sectors.bundle"
_MARKET_OVERVIEW_CORRECTION_SESSIONS = 25
_CORPORATE_EVENTS_DATASET = "equity.corporate_event.earnings.reported"
_CORPORATE_EVENTS_CAPABILITY = "corporate.disclosure.earnings.p0"
_DRAGON_TIGER_DATASET = "equity.dragon_tiger.disclosure.reported"
_DRAGON_TIGER_CAPABILITY = "market.dragon_tiger.disclosure.1d"
_BLOCK_TRADE_DATASET = "equity.block_trade.execution.reported"
_BLOCK_TRADE_CAPABILITY = "market.block_trade.execution.1d"
_STOCK_CONNECT_CAPABILITIES = frozenset(
    {
        "market.stock_connect.market_stat.reported",
        "market.stock_connect.active_security.snapshot",
        "market.stock_connect.trading_calendar",
        "market.stock_connect.channel_status.eod",
    }
)
_INDEX_DATASET_TARGETS: dict[str, tuple[IndexAdministrator, IndexCapability, bool]] = {
    "index.csi.catalog.snapshot": (
        IndexAdministrator.CSI,
        IndexCapability.CATALOG_SNAPSHOT,
        False,
    ),
    "index.csi.constituent.snapshot": (
        IndexAdministrator.CSI,
        IndexCapability.CONSTITUENT_SNAPSHOT,
        True,
    ),
    "index.csi.weight.snapshot": (
        IndexAdministrator.CSI,
        IndexCapability.WEIGHT_SNAPSHOT,
        True,
    ),
    "index.cni.catalog.snapshot": (
        IndexAdministrator.CNI,
        IndexCapability.CATALOG_SNAPSHOT,
        False,
    ),
    "index.cni.constituent.snapshot": (
        IndexAdministrator.CNI,
        IndexCapability.CONSTITUENT_SNAPSHOT,
        True,
    ),
    "index.cni.weight.snapshot": (
        IndexAdministrator.CNI,
        IndexCapability.WEIGHT_SNAPSHOT,
        True,
    ),
}


@dataclass(frozen=True, slots=True)
class _FrozenEquityIdentity:
    """保存一次股本运行在受理时冻结的永久身份和当时有效代码。"""

    instrument_id: UUID
    identifier: EquityIdentifier
    identity_as_of: date


@dataclass(frozen=True, slots=True)
class _FrozenSectorBarIdentity:
    """保存受理时冻结的板块主键与东财分类体系代码，重试不得重新枚举目录。"""

    sector_key: int
    identifier: SectorIdentifier


def register_canonical_executors(
    control_plane: DataOperationsControlPlane, container: ServiceContainer
) -> None:
    """注册当前已具备 fenced canonical 发布路径的数据集执行器。"""
    control_plane.register_executor(
        _EQUITY_MASTER_DATASET,
        partial(_execute_equity_master, container=container),
    )
    control_plane.register_executor(
        _EQUITY_LIFECYCLE_CAPABILITY,
        partial(_execute_equity_lifecycle, container=container),
    )
    control_plane.register_executor(
        _EQUITY_MASTER_RESOLVED_DATASET,
        partial(_execute_equity_master_resolved, container=container),
    )
    for dataset_code, period in _EQUITY_BAR_PERIODS.items():
        control_plane.register_executor(
            dataset_code,
            partial(_execute_equity_bar, container=container, period=period),
        )
    for dataset_code in _EQUITY_REFERENCE_CAPABILITIES:
        control_plane.register_executor(
            dataset_code,
            partial(_execute_equity_reference, container=container, capability=dataset_code),
        )
    for dataset_code, provider_capability in _FINANCIAL_EXECUTIONS.items():
        control_plane.register_executor(
            dataset_code,
            partial(
                _execute_financial_capability,
                container=container,
                provider_capability=provider_capability,
            ),
        )
    control_plane.register_executor(
        _FINANCIAL_DERIVED_DATASET,
        partial(_execute_financial_derived_metric, container=container),
    )
    control_plane.register_executor(
        "sector.catalog.raw",
        partial(_execute_sector_catalog, container=container),
    )
    for dataset_code, period in _SECTOR_BAR_PERIODS.items():
        control_plane.register_executor(
            dataset_code,
            partial(_execute_sector_bar, container=container, period=period),
        )
    control_plane.register_executor(
        _SECTOR_MEMBERSHIP_DATASET,
        partial(_execute_sector_membership, container=container),
    )
    control_plane.register_executor(
        "sector.quote.eod.snapshot",
        partial(_execute_sector_eod_snapshot, container=container),
    )
    control_plane.register_executor(
        "sector.sw.taxonomy",
        partial(_execute_sector_sw_taxonomy, container=container),
    )
    for dataset_code in _ETF_EXECUTIONS:
        control_plane.register_executor(
            dataset_code,
            partial(_execute_etf, container=container, dataset_code=dataset_code),
        )
    for dataset_code in _INDEX_DATASET_TARGETS:
        control_plane.register_executor(
            dataset_code,
            partial(_execute_index_shadow, container=container, dataset_code=dataset_code),
        )
    for dataset_code, (operation, capability) in _MARGIN_EXECUTIONS.items():
        control_plane.register_executor(
            dataset_code,
            partial(
                _execute_margin,
                container=container,
                dataset_code=dataset_code,
                operation=operation,
                capability=capability,
            ),
        )
    control_plane.register_executor(
        _DERIVATIVE_DATASET,
        partial(_execute_derivative_daily_bar, container=container),
    )
    control_plane.register_executor(
        _MONEY_FLOW_DAILY_DATASET,
        partial(_execute_money_flow, container=container),
    )
    control_plane.register_executor(
        _MONEY_FLOW_RANKING_DATASET,
        partial(_execute_money_flow, container=container),
    )
    control_plane.register_executor(
        _STOCK_CONNECT_RESEARCH_DATASET,
        partial(_execute_stock_connect_market_stat_research, container=container),
    )
    control_plane.register_executor(
        _EQUITY_TRADING_STATUS_CAPABILITY,
        partial(_execute_equity_trading_status, container=container),
    )
    control_plane.register_executor(
        _EQUITY_SHARE_CAPITAL_CAPABILITY,
        partial(_execute_equity_share_capital, container=container),
    )
    control_plane.register_executor(
        _EQUITY_DISCOVERY_DATASET,
        partial(_execute_equity_discovery, container=container),
    )
    control_plane.register_executor(
        _SW_MEMBERSHIP_CAPABILITY,
        partial(_execute_sw_membership, container=container),
    )
    control_plane.register_executor(
        _STOCK_CONNECT_BUNDLE_DATASET,
        partial(_execute_stock_connect_bundle, container=container),
    )
    control_plane.register_executor(
        _MARKET_OVERVIEW_BUNDLE_DATASET,
        partial(_execute_market_overview_bundle, container=container),
    )
    control_plane.register_executor(
        _CORPORATE_EVENTS_DATASET,
        partial(_execute_corporate_events, container=container),
    )
    control_plane.register_executor(
        _DRAGON_TIGER_DATASET,
        partial(
            _execute_trading_events,
            container=container,
            dataset_code=_DRAGON_TIGER_DATASET,
            capability=_DRAGON_TIGER_CAPABILITY,
            operation="DRAGON_TIGER",
        ),
    )
    control_plane.register_executor(
        _BLOCK_TRADE_DATASET,
        partial(
            _execute_trading_events,
            container=container,
            dataset_code=_BLOCK_TRADE_DATASET,
            capability=_BLOCK_TRADE_CAPABILITY,
            operation="BLOCK_TRADE",
        ),
    )


def _execute_corporate_events(
    claim: ExecutionClaim, *, container: ServiceContainer
) -> ExecutionOutcome:
    """同步真实业绩披露；全量回填逐窗持久化后才允许收敛成功终态。"""
    if claim.dataset_code != _CORPORATE_EVENTS_DATASET:
        raise ValueError("corporate events dataset does not match executor")
    start, end = _equity_backfill_window(claim) or _event_window(claim.target)
    windows = _bounded_windows(start, end, 31)
    identifier = _event_identifier(claim.target.get("selector"), allow_global=True)
    provider = _frozen_provider(claim.source_snapshot, container, _CORPORATE_EVENTS_CAPABILITY)
    raw_store = S3RawPayloadStore(container.object_storage)
    execution = _required_execution()
    is_backfill = _is_equity_backfill(claim)
    partition_roster = tuple(
        (
            window_from,
            window_to,
            equity_backfill_event_partition_keys(
                dataset_code=claim.dataset_code,
                window_from=window_from,
                window_to=window_to,
            ),
        )
        for window_from, window_to in windows
    )
    expected_partition_keys = frozenset(
        partition_key
        for _window_from, _window_to, partition_keys in partition_roster
        for partition_key in partition_keys
    )
    completed_keys = (
        completed_equity_event_partitions(
            container.database,
            claim=claim,
            expected_partition_keys=expected_partition_keys,
        )
        if is_backfill
        else frozenset()
    )
    inserted = 0
    unchanged = 0
    excluded = 0
    data_version: UUID | None = None
    for index, (window_from, window_to, partition_keys) in enumerate(partition_roster):
        completed_in_window = set(partition_keys) & set(completed_keys)
        if completed_in_window:
            if completed_in_window != set(partition_keys):
                raise RuntimeError(
                    "equity backfill earnings window has a partial family checkpoint"
                )
            continue
        if _cancel_requested(container):
            return ExecutionOutcome(
                status="PARTIAL" if index else "CANCELLED",
                completed_partitions=index,
                total_partitions=len(windows),
                processed_records=inserted + unchanged,
            )
        source_ids_before = frozenset(execution.source_batch_ids)
        result = retain_failure_evidence(
            raw_store,
            lambda window_from=window_from, window_to=window_to, index=index: asyncio.run(
                CorporateEventsSyncService(
                    source=FailureEvidenceDataSource(provider, raw_store),
                    repository=_corporate_events_repository(
                        container,
                        provider_id=provider.provider_id,
                    ),
                    raw_payload_store=raw_store,
                ).sync(
                    start=window_from,
                    end=window_to,
                    identifier=identifier,
                    before_final_publication=(
                        execution.arm_terminal_write
                        if not is_backfill and index == len(windows) - 1
                        else None
                    ),
                )
            ),
        )
        inserted += result.inserted_count
        unchanged += result.unchanged_count
        excluded += result.excluded_count
        data_version = result.data_version
        if is_backfill:
            record_equity_event_partitions(
                container.database,
                claim=claim,
                execution=execution,
                window_from=window_from,
                window_to=window_to,
                source_batch_ids=tuple(set(execution.source_batch_ids) - source_ids_before),
            )
    if is_backfill:
        finalize_equity_event_partitions(
            container.database,
            claim=claim,
            execution=execution,
            ordered_partition_keys=tuple(
                partition_key
                for _window_from, _window_to, partition_keys in partition_roster
                for partition_key in partition_keys
            ),
        )
    return ExecutionOutcome(
        status="SUCCEEDED",
        completed_partitions=len(windows),
        total_partitions=len(windows),
        processed_records=inserted + unchanged,
        quality_gate={
            "status": "passed",
            "excludedOutOfRoster": excluded,
        },
        checkpoint_kind="data-version" if data_version is not None else None,
        checkpoint_position=None if data_version is None else str(data_version),
    )


def _execute_trading_events(
    claim: ExecutionClaim,
    *,
    container: ServiceContainer,
    dataset_code: str,
    capability: str,
    operation: str,
) -> ExecutionOutcome:
    """同步一种真实交易披露；全量回填按窗口封印后才允许收敛终态。"""
    if claim.dataset_code != dataset_code:
        raise ValueError("trading events dataset does not match executor")
    selector = claim.target.get("selector")
    if (
        isinstance(selector, dict)
        and selector.get("kind") == "TRADING_EVENT"
        and selector.get("operation") != operation
    ):
        raise ValueError("trading event operation does not match dataset")
    # 交易事件的全市场范围由受控 `TRADING_EVENT` selector 表达；`GLOBAL` 仅属于业绩事件。
    identifier = _event_identifier(selector, allow_global=False)
    start, end = _equity_backfill_window(claim) or _event_window(claim.target)
    windows = _bounded_windows(start, end, 31)
    provider = _frozen_provider(claim.source_snapshot, container, capability)
    raw_store = S3RawPayloadStore(container.object_storage)
    execution = _required_execution()
    is_backfill = _is_equity_backfill(claim)
    partition_roster = tuple(
        (
            window_from,
            window_to,
            equity_backfill_event_partition_keys(
                dataset_code=claim.dataset_code,
                window_from=window_from,
                window_to=window_to,
            ),
        )
        for window_from, window_to in windows
    )
    expected_partition_keys = frozenset(
        partition_key
        for _window_from, _window_to, partition_keys in partition_roster
        for partition_key in partition_keys
    )
    completed_keys = (
        completed_equity_event_partitions(
            container.database,
            claim=claim,
            expected_partition_keys=expected_partition_keys,
        )
        if is_backfill
        else frozenset()
    )
    repository = _trading_events_repository(container, provider_id=provider.provider_id)
    source = FailureEvidenceDataSource(provider, raw_store)
    inserted = 0
    unchanged = 0
    excluded = 0
    data_version: UUID | None = None
    for index, (window_from, window_to, partition_keys) in enumerate(partition_roster):
        completed_in_window = set(partition_keys) & set(completed_keys)
        if completed_in_window:
            if completed_in_window != set(partition_keys):
                raise RuntimeError("equity backfill trading-event window has a partial checkpoint")
            continue
        if _cancel_requested(container):
            return ExecutionOutcome(
                status="PARTIAL" if index else "CANCELLED",
                completed_partitions=index,
                total_partitions=len(windows),
                processed_records=inserted + unchanged,
            )
        final_callback = (
            execution.arm_terminal_write if not is_backfill and index == len(windows) - 1 else None
        )
        source_ids_before = frozenset(execution.source_batch_ids)
        if operation == "DRAGON_TIGER":

            def sync_dragon_tiger_window(
                window_from: date = window_from,
                window_to: date = window_to,
                final_callback: Callable[[], None] | None = final_callback,
            ) -> Any:
                """同步单个龙虎榜窗口，并把末窗口栅栏仅交给对应发布事务。"""
                return asyncio.run(
                    DragonTigerSyncService(
                        source=source,
                        repository=repository,
                        raw_payload_store=raw_store,
                    ).sync(
                        start=window_from,
                        end=window_to,
                        identifier=identifier,
                        before_final_publication=final_callback,
                    )
                )

            result = retain_failure_evidence(
                raw_store,
                sync_dragon_tiger_window,
            )
        elif operation == "BLOCK_TRADE":

            def sync_block_trade_window(
                window_from: date = window_from,
                window_to: date = window_to,
                final_callback: Callable[[], None] | None = final_callback,
            ) -> Any:
                """同步单个大宗交易窗口，并把末窗口栅栏仅交给对应发布事务。"""
                return asyncio.run(
                    BlockTradeSyncService(
                        source=source,
                        repository=repository,
                        raw_payload_store=raw_store,
                    ).sync(
                        start=window_from,
                        end=window_to,
                        identifier=identifier,
                        before_final_publication=final_callback,
                    )
                )

            result = retain_failure_evidence(
                raw_store,
                sync_block_trade_window,
            )
        else:
            raise ValueError("trading event operation is unsupported")
        inserted += result.inserted_count
        unchanged += result.unchanged_count
        excluded += result.excluded_count
        data_version = result.data_version
        if is_backfill:
            record_equity_event_partitions(
                container.database,
                claim=claim,
                execution=execution,
                window_from=window_from,
                window_to=window_to,
                source_batch_ids=tuple(set(execution.source_batch_ids) - source_ids_before),
            )
    if is_backfill:
        finalize_equity_event_partitions(
            container.database,
            claim=claim,
            execution=execution,
            ordered_partition_keys=tuple(
                partition_key
                for _window_from, _window_to, partition_keys in partition_roster
                for partition_key in partition_keys
            ),
        )
    return ExecutionOutcome(
        status="SUCCEEDED",
        completed_partitions=len(windows),
        total_partitions=len(windows),
        processed_records=inserted + unchanged,
        quality_gate={
            "status": "passed",
            "excludedOutOfRoster": excluded,
        },
        checkpoint_kind="data-version" if data_version is not None else None,
        checkpoint_position=None if data_version is None else str(data_version),
    )


def _execute_equity_master(
    claim: ExecutionClaim, *, container: ServiceContainer
) -> ExecutionOutcome:
    """经统一栅栏发布三所交易所目录，再原子切换全市场身份聚合。"""
    if claim.target.get("mode") not in {"FULL", "OBSERVATION_DATE"}:
        raise ValueError("equity master target mode is unsupported")
    exchanges = _equity_exchanges(claim.target["selector"], global_only=True)
    target_date = _catalog_snapshot_date(claim.target)
    provider = _frozen_provider(
        claim.source_snapshot,
        container,
        _EQUITY_MASTER_CAPABILITY,
    )
    repository = SqlAlchemyEquityMasterRepository(container.database)
    raw_store = S3RawPayloadStore(container.object_storage)
    execution = _required_execution()
    inserted = 0
    unchanged = 0
    for index, exchange in enumerate(exchanges):
        if _cancel_requested(container):
            return ExecutionOutcome(
                status="PARTIAL" if index else "CANCELLED",
                completed_partitions=index,
                total_partitions=len(exchanges),
                processed_records=inserted + unchanged,
            )
        result = retain_failure_evidence(
            raw_store,
            lambda current=exchange: asyncio.run(
                EquityCatalogSyncService(
                    source=FailureEvidenceDataSource(provider, raw_store),
                    repository=repository,
                    raw_payload_store=raw_store,
                ).sync(exchange=current, target_date=target_date)
            ),
        )
        inserted += result.inserted_count
        unchanged += result.unchanged_count
        # 每所 child release 会通过统一发布器精确记录一个控制面分区；这里不能再次累计。
    execution.arm_terminal_write()
    repository.publish_cn_a_aggregate()
    return ExecutionOutcome(
        status="SUCCEEDED",
        completed_partitions=len(exchanges),
        total_partitions=len(exchanges),
        processed_records=inserted + unchanged,
    )


def _execute_stock_connect_bundle(
    claim: ExecutionClaim, *, container: ServiceContainer
) -> ExecutionOutcome:
    """按官方日历串行发布通道完整包，末次 bundle 事务原子完成控制面 run。"""
    selector = claim.target.get("selector")
    if (
        claim.dataset_code != _STOCK_CONNECT_BUNDLE_DATASET
        or not isinstance(selector, dict)
        or selector.get("kind") != "STOCK_CONNECT"
        or selector.get("operation") != "MARKET"
    ):
        raise ValueError("stock-connect bundle dataset and selector do not match")
    channel_value = selector.get("channel")
    direction_value = selector.get("direction")
    if not isinstance(channel_value, str) or channel_value not in {"ALL", "SH", "SZ"}:
        raise ValueError("stock-connect channel is invalid")
    channel_values = ("SH", "SZ") if channel_value == "ALL" else (channel_value,)
    directions = ("NORTHBOUND", "SOUTHBOUND") if direction_value is None else (direction_value,)
    if any(value not in {"NORTHBOUND", "SOUTHBOUND"} for value in directions):
        raise ValueError("stock-connect direction is invalid")
    provider = _frozen_provider(
        claim.source_snapshot,
        container,
        "market.stock_connect.market_stat.reported",
    )
    if not _STOCK_CONNECT_CAPABILITIES.issubset(provider.capabilities()):
        raise ProviderError(
            ProviderErrorCode.UNAVAILABLE,
            "frozen official stock-connect capabilities are incomplete",
            retryable=True,
        )
    manifest_id, manifest_hash, target_count, page_count = _stock_connect_manifest_reference(
        claim.execution_intent
    )
    if not isinstance(provider, SourcePreflightVerificationPort):
        raise ValueError("stock-connect frozen delivery manifest is unavailable")
    manifest_repository = SqlAlchemyDeliveryManifestRepository(container.database)
    completed_before, _failed_before = _operation_partition_counts(
        container,
        run_id=claim.run_id,
        prefix="stock-connect:",
    )
    if completed_before > target_count:
        raise ValueError("stock-connect completed partition count exceeds manifest")
    if completed_before == target_count:
        return ExecutionOutcome(
            status="SUCCEEDED",
            completed_partitions=target_count,
            total_partitions=target_count,
        )
    required_remaining_seconds = (
        ceil(
            (target_count - completed_before)
            / container.settings.stock_connect_min_partitions_per_minute
            * 60
        )
        + container.settings.stock_connect_delivery_expiry_safety_seconds
    )
    try:
        reference = manifest_repository.require_available(
            manifest_id=manifest_id,
            expected_root_hash=manifest_hash,
            observed_at=datetime.now(UTC),
            required_remaining_seconds=required_remaining_seconds,
        )
        descriptors = manifest_repository.list_page_descriptors(
            manifest_id=manifest_id,
            expected_root_hash=manifest_hash,
            observed_at=datetime.now(UTC),
        )
        descriptor = _stock_connect_pending_page(
            descriptors,
            completed_partitions=completed_before,
            expected_target_count=target_count,
            expected_page_count=page_count,
        )
        page = manifest_repository.load_page(
            manifest_id=manifest_id,
            expected_root_hash=manifest_hash,
            page_no=descriptor.page_no,
            observed_at=datetime.now(UTC),
        )
    except (DeliveryManifestIntegrityError, DeliveryManifestUnavailable) as error:
        raise ProviderError(
            ProviderErrorCode.UNAVAILABLE,
            "frozen official stock-connect delivery manifest is unavailable",
            retryable=False,
        ) from error
    if (
        reference.target_count != target_count
        or reference.page_count != page_count
        or page.target_count != descriptor.target_count
    ):
        raise ValueError("stock-connect delivery manifest reference drifted")
    manifest = stock_connect_preflight_evidence_from_delivery_page(page.evidence)
    frozen_targets = stock_connect_bundle_targets_from_evidence(manifest)
    if len(frozen_targets) != page.target_count:
        raise ValueError("stock-connect delivery page target count differs")
    approval = StockConnectSourceApproval(
        provider_id=provider.provider_id,
        source_code="official_stock_connect_licensed",
        legal_name="HKEX / SSE / SZSE official market-data delivery",
        source_kind="official_exchange",
        rights_status="licensed",
        license_scope=container.settings.stock_connect_license_scope,
        rights_evidence_ref=(
            container.settings.stock_connect_raw_retention_license_reference or None
        ),
    )
    raw_store = S3RawPayloadStore(
        container.object_storage,
        retention_mode=container.settings.stock_connect_raw_retention_mode.value,
        kms_key_id=(
            container.settings.stock_connect_raw_kms_key_id
            if container.settings.stock_connect_raw_retention_mode.value == "LICENSED_RAW_ALLOWED"
            else None
        ),
        rights_evidence_ref=(
            container.settings.stock_connect_raw_retention_license_reference or None
        ),
    )
    source = FailureEvidenceDataSource(provider, raw_store)
    service = StockConnectDailyBundleSyncService(
        source=source,
        market_repository=SqlAlchemyStockConnectMarketDataRepository(
            container.database,
            approved_sources={provider.provider_id: approval},
        ),
        center_repository=SqlAlchemyStockConnectCenterRepository(container.database),
        raw_payload_store=raw_store,
    )
    allowed_channels = set(channel_values)
    allowed_directions = set(directions)
    tasks = [
        (
            StockConnectChannel(channel=current_channel, direction=direction),
            trade_date,
        )
        for current_channel, direction, trade_date in frozen_targets
        if current_channel in allowed_channels and direction in allowed_directions
    ]
    if len(tasks) != len(frozen_targets):
        raise ValueError("stock-connect frozen targets do not match the accepted selector")
    overview_channels_by_date = {
        trade_date: tuple(
            sorted(
                f"{current.channel}_{current.direction}"
                for current, current_date in tasks
                if current_date == trade_date
            )
        )
        for _channel, trade_date in tasks
    }
    execution = _required_execution()
    partition_keys = frozenset(
        _stock_connect_partition(channel=channel, trade_date=trade_date)
        for channel, trade_date in tasks
    )
    succeeded = _prepare_operation_partitions(
        container,
        run_id=claim.run_id,
        partition_keys=partition_keys,
        subject="stock-connect delivery manifest",
        allowed_existing_prefix="stock-connect:",
        expected_total_partitions=target_count,
    )
    execution.completed_partitions = completed_before
    batch, page_has_more = _stock_connect_next_batch(tasks, succeeded=succeeded)
    has_more = page_has_more or descriptor.page_no < page_count - 1
    if not batch:
        raise RuntimeError("stock-connect manifest page has no pending partition")
    batch_dates = sorted({trade_date for _channel, trade_date in batch})
    revalidation = provider.verify_preflight_evidence(
        manifest,
        timeout_seconds=container.settings.stock_connect_preflight_timeout_seconds,
        target_keys=tuple(
            _stock_connect_partition(channel=channel, trade_date=trade_date)
            for channel, trade_date in batch
        ),
    )
    if not revalidation or not all(item.accepted for item in revalidation):
        raise ProviderError(
            ProviderErrorCode.UNAVAILABLE,
            "frozen official stock-connect delivery manifest changed before execution",
            retryable=False,
        )

    def run_tasks() -> None:
        """执行一个按完整业务日切分的公平批次，成功分区由完整包事务原子记账。"""
        for index, (channel, trade_date) in enumerate(batch):
            if _cancel_requested(container):
                return
            partition_key = _stock_connect_partition(
                channel=channel,
                trade_date=trade_date,
            )
            _record_operation_partition(
                container,
                run_id=claim.run_id,
                partition_key=partition_key,
                status="RUNNING",
                error_code=None,
            )
            asyncio.run(
                service.sync(
                    channel=channel,
                    trade_date=trade_date,
                    overview_generation_id=claim.run_id,
                    overview_channels=overview_channels_by_date[trade_date],
                    before_bundle_publication=(
                        execution.arm_terminal_write
                        if not has_more and index == len(batch) - 1
                        else None
                    ),
                )
            )

    retain_failure_evidence(raw_store, run_tasks)
    completed, _failed = _operation_partition_counts(
        container,
        run_id=claim.run_id,
        prefix="stock-connect:",
    )
    if _cancel_requested(container):
        return ExecutionOutcome(
            status="PARTIAL" if completed else "CANCELLED",
            completed_partitions=completed,
            total_partitions=target_count,
            processed_records=execution.processed_records,
        )
    if has_more:
        return ExecutionOutcome(
            status="YIELDED",
            completed_partitions=completed,
            total_partitions=target_count,
            processed_records=execution.processed_records,
            checkpoint_kind="stock-connect-batch",
            checkpoint_position=batch_dates[-1].isoformat(),
        )
    return ExecutionOutcome(
        status="SUCCEEDED",
        completed_partitions=completed,
        total_partitions=target_count,
        processed_records=execution.processed_records,
    )


def _execute_market_overview_bundle(
    claim: ExecutionClaim,
    *,
    container: ServiceContainer,
) -> ExecutionOutcome:
    """在全局 fencing slot 内按开市日升序回填并原子发布市场完整包。"""
    selector = claim.target.get("selector")
    if (
        claim.dataset_code != _MARKET_OVERVIEW_BUNDLE_DATASET
        or not isinstance(selector, dict)
        or selector.get("kind") != "GLOBAL"
    ):
        raise ValueError("market overview dataset requires a GLOBAL selector")
    provider = _frozen_provider(
        claim.source_snapshot,
        container,
        "market.source.preflight",
    )
    required_capabilities = {
        "market.source.preflight",
        "market.calendar",
        "index.bar.1d",
        "equity.catalog",
        "equity.quote.eod",
        "equity.daily-basic.eod",
        "equity.suspension.eod",
        "equity.limit-price.eod",
        "market.turnover.qa.reported",
        "money-flow.market.dc.eod",
        "money-flow.equity.order-size.eod",
        "sector.catalog.dc",
        "sector.quote.eod.dc",
        "sector.membership.dc",
        "sector.money-flow.dc.eod",
        "sw.taxonomy",
        "sw.membership",
        "sw.market-data",
    }
    if not required_capabilities.issubset(provider.capabilities()):
        raise ProviderError(
            ProviderErrorCode.UNAVAILABLE,
            "frozen market overview capabilities are incomplete",
            retryable=True,
        )
    start, end, incremental = _market_overview_window(claim.target)
    raw_store = S3RawPayloadStore(container.object_storage)
    source = FailureEvidenceDataSource(provider, raw_store)
    calendar_dates = retain_failure_evidence(
        raw_store,
        lambda: _market_overview_trading_dates(
            source,
            start=start - timedelta(days=120),
            end=end,
        ),
    )
    eligible_dates = [value for value in calendar_dates if start <= value <= end]
    if not eligible_dates:
        raise ValueError("market overview window has no eligible common trading day")
    repository = SqlAlchemyMarketOverviewRepository(container.database)
    trading_dates = _market_overview_pending_dates(
        repository=repository,
        eligible_dates=eligible_dates,
        incremental=incremental,
    )
    if not trading_dates:
        return ExecutionOutcome(
            status="SUCCEEDED",
            completed_partitions=0,
            total_partitions=0,
            processed_records=0,
            checkpoint_kind="TRADING_DATE",
            checkpoint_position=eligible_dates[-1].isoformat(),
            quality_gate={"status": "passed", "bundleCount": 0},
        )
    service = MarketOverviewSyncService(
        source=source,
        repository=repository,
    )
    # 权限与 schema 探针必须先于第一个 canonical 写；同一 frozen provider run 只需一次。
    retain_failure_evidence(
        raw_store,
        lambda: asyncio.run(service.preflight(trade_date=trading_dates[-1])),
    )
    seed_dates = _market_overview_bootstrap_seed_dates(
        repository=repository,
        calendar_dates=calendar_dates,
        first_target=trading_dates[0],
    )
    if seed_dates:
        retain_failure_evidence(
            raw_store,
            lambda: asyncio.run(service.seed_derivation_inputs(trade_dates=tuple(seed_dates))),
        )
    execution = _required_execution()
    completed = 0
    processed = 0
    for index, trade_date in enumerate(trading_dates):
        if _cancel_requested(container):
            return ExecutionOutcome(
                status="PARTIAL" if completed else "CANCELLED",
                completed_partitions=completed,
                total_partitions=len(trading_dates),
                processed_records=processed,
            )
        if index == len(trading_dates) - 1:
            execution.arm_terminal_write()
        result = retain_failure_evidence(
            raw_store,
            lambda current=trade_date: asyncio.run(
                service.sync(
                    trade_date=current,
                    preflight_checked=True,
                )
            ),
        )
        completed += 1
        processed += result.component_count
    return ExecutionOutcome(
        status="SUCCEEDED",
        completed_partitions=completed,
        total_partitions=len(trading_dates),
        processed_records=processed,
        checkpoint_kind="TRADING_DATE",
        checkpoint_position=trading_dates[-1].isoformat(),
        quality_gate={"status": "passed", "bundleCount": completed},
    )


def _market_overview_window(
    target: dict[str, object],
) -> tuple[date, date, bool]:
    """把运维模式转换为明确窗口；初始全量必须显式 DATE_RANGE，禁止猜源起点。"""
    mode = target.get("mode")
    today = datetime.now(_SHANGHAI).date()
    if mode == "INCREMENTAL":
        # 60 个自然日足以覆盖 25 个共同交易日，并为长假留下恢复余量。
        return today - timedelta(days=60), today, True
    if mode == "OBSERVATION_DATE":
        value = target.get("observationDate")
        if not isinstance(value, str):
            raise ValueError("market observationDate is required")
        target_date = date.fromisoformat(value)
        return target_date, target_date, False
    if mode == "DATE_RANGE":
        start_value = target.get("dateFrom")
        end_value = target.get("dateTo")
        if not isinstance(start_value, str) or not isinstance(end_value, str):
            raise ValueError("market date range is invalid")
        start = date.fromisoformat(start_value)
        end = date.fromisoformat(end_value)
        if start > end or (end - start).days > 120:
            raise ValueError("market date range must be ordered and at most 120 days")
        return start, end, False
    raise ValueError("market overview supports INCREMENTAL, OBSERVATION_DATE or DATE_RANGE")


def _market_overview_pending_dates(
    *,
    repository: MarketOverviewRepository,
    eligible_dates: list[date],
    incremental: bool,
) -> list[date]:
    """增量模式升序补齐最近 25 个共同交易日的全部 active bundle 缺口。"""
    if not incremental:
        return list(eligible_dates)
    correction_window = eligible_dates[-_MARKET_OVERVIEW_CORRECTION_SESSIONS:]
    pending = [
        trade_date
        for trade_date in correction_window
        if repository.get_bundle(trade_date=trade_date) is None
    ]
    current = repository.get_bundle(trade_date=None)
    if current is not None and any(value < current.trade_date for value in pending):
        raise ValueError(
            "market overview has a historical active gap that requires controlled chain replay"
        )
    return pending


def _market_overview_trading_dates(
    provider: DataSourcePort,
    *,
    start: date,
    end: date,
) -> list[date]:
    """从 frozen Tushare 日历取得共同开市日，并应用 17:20 EOD eligibility。"""
    batch = asyncio.run(
        provider.fetch(
            SourceRequest(
                capability="market.calendar",
                parameters=(
                    ("end", end.isoformat()),
                    ("start", start.isoformat()),
                ),
            )
        )
    )
    try:
        payload = json.loads(batch.payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProviderError(
            ProviderErrorCode.SCHEMA,
            "market calendar payload is invalid",
            retryable=False,
        ) from error
    if not isinstance(payload, dict) or payload.get("schema") != "quant-v2.market-calendar.v1":
        raise ProviderError(
            ProviderErrorCode.SCHEMA,
            "market calendar schema is invalid",
            retryable=False,
        )
    rows = payload.get("records")
    if not isinstance(rows, list):
        raise ProviderError(
            ProviderErrorCode.SCHEMA,
            "market calendar records are invalid",
            retryable=False,
        )
    by_venue: dict[str, set[date]] = {"SSE": set(), "SZSE": set()}
    for row in rows:
        if not isinstance(row, dict):
            raise ProviderError(
                ProviderErrorCode.SCHEMA,
                "market calendar row is invalid",
                retryable=False,
            )
        venue = row.get("venue")
        if venue in by_venue and row.get("isTradingDay") is True:
            by_venue[str(venue)].add(date.fromisoformat(str(row["tradeDate"])))
    common = by_venue["SSE"] & by_venue["SZSE"]
    now = datetime.now(_SHANGHAI)
    if now.time() < datetime.strptime("17:20", "%H:%M").time():
        common.discard(now.date())
    return sorted(value for value in common if start <= value <= end)


def _market_overview_bootstrap_seed_dates(
    *,
    repository: SqlAlchemyMarketOverviewRepository,
    calendar_dates: list[date],
    first_target: date,
) -> list[date]:
    """自动补齐强弱、当前周和当前月所需日线，避免重复抓取高成本 membership。"""
    try:
        target_index = calendar_dates.index(first_target)
    except ValueError as error:
        raise ValueError("market bootstrap target is absent from the common calendar") from error
    if target_index < 19:
        raise ValueError("market bootstrap calendar has fewer than 20 sessions")
    twenty_day_start = calendar_dates[target_index - 19]
    same_month = [
        value
        for value in calendar_dates[: target_index + 1]
        if (value.year, value.month) == (first_target.year, first_target.month)
    ]
    target_iso = first_target.isocalendar()[:2]
    same_week = [
        value
        for value in calendar_dates[: target_index + 1]
        if value.isocalendar()[:2] == target_iso
    ]
    if not same_month or not same_week:
        raise ValueError("market bootstrap calendar period boundaries are incomplete")
    bootstrap_start = min(twenty_day_start, same_month[0], same_week[0])
    required = [value for value in calendar_dates if bootstrap_start <= value < first_target]
    existing_sector = {
        component.trade_date
        for component in repository.list_derivation_inputs(
            dataset_code="sector.quote.eod.dc",
            start=bootstrap_start,
            end=first_target,
        )
    }
    existing_sw = {
        component.trade_date
        for component in repository.list_derivation_inputs(
            dataset_code="sw.market-data",
            start=bootstrap_start,
            end=first_target,
        )
    }
    return [value for value in required if value not in existing_sector or value not in existing_sw]


def _execute_equity_lifecycle(
    claim: ExecutionClaim, *, container: ServiceContainer
) -> ExecutionOutcome:
    """经统一栅栏发布显式生命周期分区，且不再覆盖证券目录 publication。"""
    if claim.target.get("mode") not in {"FULL", "INCREMENTAL", "OBSERVATION_DATE"}:
        raise ValueError("equity lifecycle target mode is unsupported")
    exchanges = _equity_exchanges(claim.target["selector"], global_only=False)
    target_date = _catalog_snapshot_date(claim.target)
    provider = _frozen_provider(
        claim.source_snapshot,
        container,
        _EQUITY_LIFECYCLE_CAPABILITY,
    )
    repository = SqlAlchemyEquityLifecycleRepository(container.database)
    raw_store = S3RawPayloadStore(container.object_storage)
    inserted = 0
    unchanged = 0
    for index, exchange in enumerate(exchanges):
        if _cancel_requested(container):
            return ExecutionOutcome(
                status="PARTIAL" if index else "CANCELLED",
                completed_partitions=index,
                total_partitions=len(exchanges),
                processed_records=inserted + unchanged,
            )
        if index == len(exchanges) - 1:
            _required_execution().arm_terminal_write()
        result = retain_failure_evidence(
            raw_store,
            lambda current=exchange: asyncio.run(
                EquityLifecycleSyncService(
                    source=FailureEvidenceDataSource(provider, raw_store),
                    repository=repository,
                    raw_payload_store=raw_store,
                ).sync(exchange=current, target_date=target_date)
            ),
        )
        inserted += result.inserted_count
        unchanged += result.unchanged_count
    return ExecutionOutcome(
        status="SUCCEEDED",
        completed_partitions=len(exchanges),
        total_partitions=len(exchanges),
        processed_records=inserted + unchanged,
    )


def _execute_equity_master_resolved(
    claim: ExecutionClaim, *, container: ServiceContainer
) -> ExecutionOutcome:
    """通过已注册 fenced executor 发布 providerless 的目录/生命周期 resolved 视图。"""
    selector = claim.target.get("selector")
    if (
        claim.dataset_code != _EQUITY_MASTER_RESOLVED_DATASET
        or claim.target.get("mode") != "FULL"
        or not isinstance(selector, dict)
        or selector.get("kind") != "GLOBAL"
    ):
        raise ValueError("equity resolved master target is unsupported")
    if claim.source_snapshot and any(
        value.get("sourceKind") != "INTERNAL_EXECUTOR" for value in claim.source_snapshot
    ):
        raise ValueError("equity resolved master must not bind an external provider")
    if _cancel_requested(container):
        return ExecutionOutcome(
            status="CANCELLED",
            completed_partitions=0,
            total_partitions=1,
        )
    result = SqlAlchemyResolvedEquityMasterRepository(container.database).publish()
    return ExecutionOutcome(
        status="SUCCEEDED",
        completed_partitions=1,
        total_partitions=1,
        processed_records=result.component_count,
        checkpoint_kind="data-version",
        checkpoint_position=str(result.data_version),
    )


def _execute_margin(
    claim: ExecutionClaim,
    *,
    container: ServiceContainer,
    dataset_code: str,
    operation: str,
    capability: str,
) -> ExecutionOutcome:
    """按冻结日期窗执行一段两融场所批次，并以持久化分区支持安全续跑。

    每个 batch 都经 `DatabaseClient` 的 fencing 事务发布，随后才标记该窗口成功；不能
    先推进水位再调用来源。窗口之间主动 `YIELDED`，避免深市逐日接口长期占用全局槽。
    """
    selector = claim.target.get("selector")
    if (
        claim.dataset_code != dataset_code
        or not isinstance(selector, dict)
        or selector.get("kind") != "MARGIN"
        or selector.get("operation") != operation
        or selector.get("security") is not None
    ):
        raise ValueError("margin dataset and selector do not match executor")
    venue_code = selector.get("venue")
    if not isinstance(venue_code, str):
        raise ValueError("margin venue is invalid")
    venue = MarginVenue(venue_code)
    if operation == "ELIGIBILITY" and venue.code not in {"SZSE", "BSE"}:
        raise ValueError("margin eligibility source is only available for SZSE or BSE")
    if operation in {"MARKET", "SECURITY"} and venue.code not in {"SSE", "SZSE"}:
        raise ValueError("margin market and security sources are only available for SSE or SZSE")
    start, end = _akshare_batched_window(
        claim,
        dataset_code=dataset_code,
        batch_days=_MARGIN_EXECUTION_BATCH_DAYS,
    )
    windows = _bounded_windows(start, end, _MARGIN_EXECUTION_BATCH_DAYS)
    partition_by_window = {
        window: _margin_partition(operation=operation, venue=venue, window=window)
        for window in windows
    }
    partition_keys = frozenset(partition_by_window.values())
    completed_before = _prepare_operation_partitions(
        container,
        run_id=claim.run_id,
        partition_keys=partition_keys,
        subject="margin date windows",
    )
    pending_windows = tuple(
        window for window in windows if partition_by_window[window] not in completed_before
    )
    if not pending_windows:
        return ExecutionOutcome(
            status="SUCCEEDED",
            completed_partitions=len(completed_before),
            total_partitions=len(windows),
        )
    if _cancel_requested(container):
        return ExecutionOutcome(
            status="PARTIAL" if completed_before else "CANCELLED",
            completed_partitions=len(completed_before),
            total_partitions=len(windows),
        )
    window_from, window_to = pending_windows[0]
    partition_key = partition_by_window[pending_windows[0]]
    _record_operation_partition(
        container,
        run_id=claim.run_id,
        partition_key=partition_key,
        status="RUNNING",
        error_code=None,
    )
    provider = _frozen_provider(claim.source_snapshot, container, capability)
    repository = _margin_repository(container, provider_id=provider.provider_id)
    raw_store = S3RawPayloadStore(container.object_storage)
    source = FailureEvidenceDataSource(provider, raw_store)

    def sync_margin_window() -> Any:
        """调用唯一 operation 的既有同步用例，禁止跨数据集复用或补齐字段。"""
        if operation == "MARKET":
            return asyncio.run(
                MarginMarketDailySyncService(
                    source=source,
                    repository=repository,
                    raw_payload_store=raw_store,
                ).sync(venue=venue, start=window_from, end=window_to)
            )
        if operation == "SECURITY":
            return asyncio.run(
                MarginSecurityDailySyncService(
                    source=source,
                    repository=repository,
                    raw_payload_store=raw_store,
                ).sync(venue=venue, start=window_from, end=window_to)
            )
        if operation == "ELIGIBILITY":
            return asyncio.run(
                MarginEligibilitySyncService(
                    source=source,
                    repository=repository,
                    raw_payload_store=raw_store,
                ).sync(venue=venue, start=window_from, end=window_to)
            )
        raise ValueError("margin operation is unsupported")

    try:
        retain_failure_evidence(raw_store, sync_margin_window)
    except ProviderError as error:
        _record_operation_partition(
            container,
            run_id=claim.run_id,
            partition_key=partition_key,
            status="FAILED",
            error_code=_p0_partition_error_code(error.code.value),
            error_retryable=error.retryable,
        )
        raise
    except ValueError:
        _record_operation_partition(
            container,
            run_id=claim.run_id,
            partition_key=partition_key,
            status="FAILED",
            error_code="INVALID_SOURCE_DATA",
            error_retryable=False,
        )
        raise
    _record_operation_partition(
        container,
        run_id=claim.run_id,
        partition_key=partition_key,
        status="SUCCEEDED",
        error_code=None,
    )
    completed, _failed = _operation_partition_counts(
        container,
        run_id=claim.run_id,
        prefix="margin:",
    )
    execution = _required_execution()
    outcome = ExecutionOutcome(
        status="YIELDED" if len(pending_windows) > 1 else "SUCCEEDED",
        completed_partitions=completed,
        total_partitions=len(windows),
        processed_records=execution.processed_records,
        checkpoint_kind=execution.checkpoint_kind,
        checkpoint_position=execution.checkpoint_position,
    )
    return outcome


def _execute_index_shadow(
    claim: ExecutionClaim,
    *,
    container: ServiceContainer,
    dataset_code: str,
) -> ExecutionOutcome:
    """写入单一中证或国证当前研究快照，并禁止形成正式发布或历史有效区间。"""
    selector = claim.target.get("selector")
    expected = _INDEX_DATASET_TARGETS.get(dataset_code)
    if (
        claim.dataset_code != dataset_code
        or claim.target.get("mode") != "FULL"
        or expected is None
        or not isinstance(selector, dict)
        or selector.get("kind") != "INDEX"
    ):
        raise ValueError("index shadow dataset and selector do not match executor")
    administrator, capability, requires_index_code = expected
    index_code = selector.get("indexCode")
    if (
        selector.get("administrator") != administrator.value
        or selector.get("capability") != capability.value
        or (requires_index_code and not isinstance(index_code, str))
        or (not requires_index_code and index_code is not None)
    ):
        raise ValueError("index shadow selector is invalid")
    identifier = (
        None
        if index_code is None
        else IndexIdentifier(administrator=administrator, code=index_code)
    )
    partition_key = _index_shadow_partition(
        administrator=administrator,
        capability=capability,
        index_code=index_code,
    )
    completed_before = _prepare_operation_partitions(
        container,
        run_id=claim.run_id,
        partition_keys=frozenset({partition_key}),
        subject="index research snapshot",
    )
    if partition_key in completed_before:
        return ExecutionOutcome(status="SUCCEEDED", completed_partitions=1, total_partitions=1)
    if _cancel_requested(container):
        return ExecutionOutcome(status="CANCELLED", completed_partitions=0, total_partitions=1)
    _record_operation_partition(
        container,
        run_id=claim.run_id,
        partition_key=partition_key,
        status="RUNNING",
        error_code=None,
    )
    provider = _frozen_provider(claim.source_snapshot, container, capability.value)
    raw_store = S3RawPayloadStore(container.object_storage)
    service = IndexShadowSyncService(
        source=FailureEvidenceDataSource(provider, raw_store),
        repository=SqlAlchemyIndexShadowRepository(container.database),
        raw_payload_store=raw_store,
    )
    try:
        if identifier is None:
            result = retain_failure_evidence(
                raw_store,
                lambda: asyncio.run(service.sync_catalog(administrator=administrator)),
            )
        else:
            result = retain_failure_evidence(
                raw_store,
                lambda: asyncio.run(
                    service.sync_snapshot(identifier=identifier, capability=capability)
                ),
            )
    except ProviderError as error:
        _record_operation_partition(
            container,
            run_id=claim.run_id,
            partition_key=partition_key,
            status="FAILED",
            error_code=_p0_partition_error_code(error.code.value),
            error_retryable=error.retryable,
        )
        raise
    except ValueError:
        _record_operation_partition(
            container,
            run_id=claim.run_id,
            partition_key=partition_key,
            status="FAILED",
            error_code="INVALID_SOURCE_DATA",
            error_retryable=False,
        )
        raise
    except Exception:
        _record_operation_partition(
            container,
            run_id=claim.run_id,
            partition_key=partition_key,
            status="FAILED",
            error_code="INDEX_RESEARCH_SYNC_FAILED",
            error_retryable=True,
            error_stage="PERSISTENCE",
        )
        raise
    _record_operation_partition(
        container,
        run_id=claim.run_id,
        partition_key=partition_key,
        status="SUCCEEDED",
        error_code=None,
    )
    # IndexShadowRepository 只写 research 观察和来源摘要；绝不调用 arm_terminal_write。
    return ExecutionOutcome(
        status="SUCCEEDED",
        completed_partitions=1,
        total_partitions=1,
        processed_records=result.observation.item_count,
    )


def _execute_stock_connect_market_stat_research(
    claim: ExecutionClaim,
    *,
    container: ServiceContainer,
) -> ExecutionOutcome:
    """按冻结日期窗和通道方向记录 AKShare 港通统计 research，不触碰官方完整包。"""
    selector = claim.target.get("selector")
    if (
        claim.dataset_code != _STOCK_CONNECT_RESEARCH_DATASET
        or not isinstance(selector, dict)
        or selector.get("kind") != "STOCK_CONNECT_RESEARCH"
        or selector.get("operation") != "MARKET_STAT"
    ):
        raise ValueError("stock-connect research dataset and selector do not match executor")
    channel_value = selector.get("channel")
    direction_value = selector.get("direction")
    if not isinstance(channel_value, str) or channel_value not in {"ALL", "SH", "SZ"}:
        raise ValueError("stock-connect research channel is invalid")
    if direction_value not in {"NORTHBOUND", "SOUTHBOUND", None}:
        raise ValueError("stock-connect research direction is invalid")
    start, end = _akshare_batched_window(
        claim,
        dataset_code=_STOCK_CONNECT_RESEARCH_DATASET,
        batch_days=_STOCK_CONNECT_RESEARCH_EXECUTION_BATCH_DAYS,
    )
    channels = ("SH", "SZ") if channel_value == "ALL" else (channel_value,)
    directions = ("NORTHBOUND", "SOUTHBOUND") if direction_value is None else (direction_value,)
    tasks = tuple(
        (StockConnectChannel(channel=channel, direction=direction), window)
        for window in _bounded_windows(start, end, _STOCK_CONNECT_RESEARCH_EXECUTION_BATCH_DAYS)
        for channel in channels
        for direction in directions
    )
    partition_by_task = {
        task: _stock_connect_research_partition(channel=task[0], window=task[1]) for task in tasks
    }
    completed_before = _prepare_operation_partitions(
        container,
        run_id=claim.run_id,
        partition_keys=frozenset(partition_by_task.values()),
        subject="stock-connect research date windows",
    )
    pending = tuple(task for task in tasks if partition_by_task[task] not in completed_before)
    if not pending:
        return ExecutionOutcome(
            status="SUCCEEDED",
            completed_partitions=len(completed_before),
            total_partitions=len(tasks),
        )
    if _cancel_requested(container):
        return ExecutionOutcome(
            status="PARTIAL" if completed_before else "CANCELLED",
            completed_partitions=len(completed_before),
            total_partitions=len(tasks),
        )
    channel, window = pending[0]
    window_from, window_to = window
    partition_key = partition_by_task[(channel, window)]
    _record_operation_partition(
        container,
        run_id=claim.run_id,
        partition_key=partition_key,
        status="RUNNING",
        error_code=None,
    )
    provider = _frozen_provider(
        claim.source_snapshot,
        container,
        _STOCK_CONNECT_RESEARCH_CAPABILITY,
    )
    failure_evidence_store = S3RawPayloadStore(container.object_storage)
    service = StockConnectMarketStatResearchSyncService(
        source=provider,
        repository=stock_connect_research_repository.SqlAlchemyStockConnectMarketStatResearchRepository(
            container.database
        ),
        failure_evidence_store=failure_evidence_store,
    )
    try:
        result = asyncio.run(service.sync(channel=channel, start=window_from, end=window_to))
    except ProviderError as error:
        _record_operation_partition(
            container,
            run_id=claim.run_id,
            partition_key=partition_key,
            status="FAILED",
            error_code=_p0_partition_error_code(error.code.value),
            error_retryable=error.retryable,
        )
        raise
    except ValueError:
        _record_operation_partition(
            container,
            run_id=claim.run_id,
            partition_key=partition_key,
            status="FAILED",
            error_code="INVALID_SOURCE_DATA",
            error_retryable=False,
        )
        raise
    except Exception:
        _record_operation_partition(
            container,
            run_id=claim.run_id,
            partition_key=partition_key,
            status="FAILED",
            error_code="STOCK_CONNECT_RESEARCH_SYNC_FAILED",
            error_retryable=True,
            error_stage="PERSISTENCE",
        )
        raise
    _record_operation_partition(
        container,
        run_id=claim.run_id,
        partition_key=partition_key,
        status="SUCCEEDED",
        error_code=None,
    )
    completed, _failed = _operation_partition_counts(
        container,
        run_id=claim.run_id,
        prefix="stock-connect-research:",
    )
    # Research 仓储只保存观察、来源摘要和质量，不生成 DatasetRelease、Publication 或 PIT。
    return ExecutionOutcome(
        status="YIELDED" if len(pending) > 1 else "SUCCEEDED",
        completed_partitions=completed,
        total_partitions=len(tasks),
        processed_records=result.batch.inserted_count,
    )


def _execute_derivative_daily_bar(
    claim: ExecutionClaim,
    *,
    container: ServiceContainer,
) -> ExecutionOutcome:
    """按冻结窗口同步一个真实期货合约，并以一窗一调度批次避免长历史任务独占。"""
    selector = claim.target.get("selector")
    if (
        claim.dataset_code != _DERIVATIVE_DATASET
        or not isinstance(selector, dict)
        or selector.get("kind") != "CONTRACT"
    ):
        raise ValueError("derivative dataset requires a contract selector")
    venue = selector.get("venue")
    contract_code = selector.get("contract")
    if not isinstance(venue, str) or not isinstance(contract_code, str):
        raise ValueError("derivative contract selector is invalid")
    contract = DerivativeContractIdentifier(venue=venue, contract_code=contract_code)
    start, end = _akshare_batched_window(
        claim,
        dataset_code=_DERIVATIVE_DATASET,
        batch_days=_DERIVATIVE_EXECUTION_BATCH_DAYS,
    )
    windows = _bounded_windows(start, end, _DERIVATIVE_EXECUTION_BATCH_DAYS)
    partition_by_window = {
        window: _derivative_partition(contract=contract, window=window) for window in windows
    }
    partition_keys = frozenset(partition_by_window.values())
    completed_before = _prepare_operation_partitions(
        container,
        run_id=claim.run_id,
        partition_keys=partition_keys,
        subject="derivative contract date windows",
    )
    pending_windows = tuple(
        window for window in windows if partition_by_window[window] not in completed_before
    )
    if not pending_windows:
        return ExecutionOutcome(
            status="SUCCEEDED",
            completed_partitions=len(completed_before),
            total_partitions=len(windows),
        )
    if _cancel_requested(container):
        return ExecutionOutcome(
            status="PARTIAL" if completed_before else "CANCELLED",
            completed_partitions=len(completed_before),
            total_partitions=len(windows),
        )
    window_from, window_to = pending_windows[0]
    partition_key = partition_by_window[pending_windows[0]]
    _record_operation_partition(
        container,
        run_id=claim.run_id,
        partition_key=partition_key,
        status="RUNNING",
        error_code=None,
    )
    provider = _frozen_provider(claim.source_snapshot, container, _DERIVATIVE_CAPABILITY)
    raw_store = S3RawPayloadStore(container.object_storage)
    source = FailureEvidenceDataSource(provider, raw_store)
    repository = _derivative_repository(container, provider_id=provider.provider_id)

    def sync_derivative_window() -> Any:
        """调用真实合约日线用例，连续合约或空身份不会进入仓储。"""
        return asyncio.run(
            DerivativeDailyBarSyncService(
                source=source,
                repository=repository,
                raw_payload_store=raw_store,
            ).sync(contract=contract, start=window_from, end=window_to)
        )

    try:
        retain_failure_evidence(raw_store, sync_derivative_window)
    except ProviderError as error:
        _record_operation_partition(
            container,
            run_id=claim.run_id,
            partition_key=partition_key,
            status="FAILED",
            error_code=_p0_partition_error_code(error.code.value),
            error_retryable=error.retryable,
        )
        raise
    except ValueError:
        _record_operation_partition(
            container,
            run_id=claim.run_id,
            partition_key=partition_key,
            status="FAILED",
            error_code="INVALID_SOURCE_DATA",
            error_retryable=False,
        )
        raise
    _record_operation_partition(
        container,
        run_id=claim.run_id,
        partition_key=partition_key,
        status="SUCCEEDED",
        error_code=None,
    )
    completed, _failed = _operation_partition_counts(
        container,
        run_id=claim.run_id,
        prefix="derivative:",
    )
    execution = _required_execution()
    return ExecutionOutcome(
        status="YIELDED" if len(pending_windows) > 1 else "SUCCEEDED",
        completed_partitions=completed,
        total_partitions=len(windows),
        processed_records=execution.processed_records,
        checkpoint_kind=execution.checkpoint_kind,
        checkpoint_position=execution.checkpoint_position,
    )


def _execute_sector_bar(
    claim: ExecutionClaim,
    *,
    container: ServiceContainer,
    period: SectorPeriod,
) -> ExecutionOutcome:
    """执行一个冻结板块和一日窗口，失败留证且绝不把空响应发布为 K 线。"""
    if claim.dataset_code != period.capability:
        raise ValueError("sector bar dataset does not match executor period")
    roster = _frozen_sector_bar_roster(claim)
    start, end = _akshare_batched_window(
        claim,
        dataset_code=claim.dataset_code,
        batch_days=_SECTOR_BAR_EXECUTION_BATCH_DAYS,
    )
    windows = _bounded_windows(start, end, _SECTOR_BAR_EXECUTION_BATCH_DAYS)
    tasks = tuple((identity, window) for window in windows for identity in roster)
    if not tasks:
        raise ValueError("sector bar frozen roster must not be empty")
    completed_before = _completed_operation_partitions(
        container,
        run_id=claim.run_id,
        prefix="sector-bar:",
    )
    pending = next(
        (
            (identity, window)
            for identity, window in tasks
            if _sector_bar_partition(period=period, identity=identity, window=window)
            not in completed_before
        ),
        None,
    )
    if pending is None:
        return ExecutionOutcome(
            status="SUCCEEDED",
            completed_partitions=len(completed_before),
            total_partitions=len(tasks),
        )
    identity, window = pending
    partition_key = _sector_bar_partition(period=period, identity=identity, window=window)
    _prepare_operation_partitions(
        container,
        run_id=claim.run_id,
        partition_keys=frozenset({partition_key}),
        subject="sector bar frozen roster",
        allowed_existing_prefix="sector-bar:",
        expected_total_partitions=len(tasks),
    )
    if _cancel_requested(container):
        return ExecutionOutcome(
            status="PARTIAL" if completed_before else "CANCELLED",
            completed_partitions=len(completed_before),
            total_partitions=len(tasks),
        )
    _record_operation_partition(
        container,
        run_id=claim.run_id,
        partition_key=partition_key,
        status="RUNNING",
        error_code=None,
    )
    raw_store = S3RawPayloadStore(container.object_storage)
    try:
        _assert_frozen_sector_bar_identity(container, identity=identity)
        provider = _frozen_provider(claim.source_snapshot, container, period.capability)
        result = retain_failure_evidence(
            raw_store,
            lambda: asyncio.run(
                SectorBarSyncService(
                    source=FailureEvidenceDataSource(provider, raw_store),
                    repository=SqlAlchemySectorMarketDataRepository(container.database),
                ).sync(
                    identifier=identity.identifier,
                    period=period,
                    start=window[0],
                    end=window[1],
                )
            ),
        )
    except ProviderError as error:
        _record_operation_partition(
            container,
            run_id=claim.run_id,
            partition_key=partition_key,
            status="FAILED",
            error_code=_p0_partition_error_code(error.code.value),
            error_retryable=error.retryable,
        )
        raise
    except ValueError:
        _record_operation_partition(
            container,
            run_id=claim.run_id,
            partition_key=partition_key,
            status="FAILED",
            error_code="INVALID_SOURCE_DATA",
            error_retryable=False,
        )
        raise
    _record_operation_partition(
        container,
        run_id=claim.run_id,
        partition_key=partition_key,
        status="SUCCEEDED",
        error_code=None,
    )
    completed, _failed = _operation_partition_counts(
        container,
        run_id=claim.run_id,
        prefix="sector-bar:",
    )
    execution = _required_execution()
    return ExecutionOutcome(
        status="YIELDED" if completed < len(tasks) else "SUCCEEDED",
        completed_partitions=completed,
        total_partitions=len(tasks),
        processed_records=result.inserted_count + result.unchanged_count,
        checkpoint_kind=execution.checkpoint_kind,
        checkpoint_position=execution.checkpoint_position,
    )


def _execute_money_flow(
    claim: ExecutionClaim,
    *,
    container: ServiceContainer,
) -> ExecutionOutcome:
    """抓取一个冻结资金流方法学能力，完整性不足只落研究观察不伪造 publication。"""
    if claim.dataset_code not in {_MONEY_FLOW_DAILY_DATASET, _MONEY_FLOW_RANKING_DATASET}:
        raise ValueError("money-flow dataset is unsupported")
    selector = claim.target.get("selector")
    if not isinstance(selector, dict) or selector.get("kind") != "MONEY_FLOW":
        raise ValueError("money-flow selector is invalid")
    capability = money_flow_source_capability(claim.dataset_code, selector)
    partition_key = _money_flow_partition(
        claim=claim,
        capability=capability,
        selector=selector,
    )
    completed_before = _prepare_operation_partitions(
        container,
        run_id=claim.run_id,
        partition_keys=frozenset({partition_key}),
        subject="money-flow frozen target",
        allowed_existing_prefix="money-flow:",
        expected_total_partitions=1,
    )
    if partition_key in completed_before:
        return ExecutionOutcome(status="SUCCEEDED", completed_partitions=1, total_partitions=1)
    if _cancel_requested(container):
        return ExecutionOutcome(
            status="CANCELLED",
            completed_partitions=0,
            total_partitions=1,
        )
    _record_operation_partition(
        container,
        run_id=claim.run_id,
        partition_key=partition_key,
        status="RUNNING",
        error_code=None,
    )
    raw_store = S3RawPayloadStore(container.object_storage)
    try:
        provider = _frozen_provider(claim.source_snapshot, container, capability)
        parameters = _money_flow_parameters(
            claim=claim,
            container=container,
            capability=capability,
            selector=selector,
        )
        result = retain_failure_evidence(
            raw_store,
            lambda: asyncio.run(
                MoneyFlowSyncService(
                    source=FailureEvidenceDataSource(provider, raw_store),
                    repository=SqlAlchemyMoneyFlowRepository(container.database),
                ).sync(
                    capability=capability,
                    parameters=parameters,
                    run_id=claim.run_id,
                    partition_key=partition_key,
                )
            ),
        )
    except ProviderError as error:
        _record_operation_partition(
            container,
            run_id=claim.run_id,
            partition_key=partition_key,
            status="FAILED",
            error_code=_p0_partition_error_code(error.code.value),
            error_retryable=error.retryable,
        )
        raise
    except ValueError:
        _record_operation_partition(
            container,
            run_id=claim.run_id,
            partition_key=partition_key,
            status="FAILED",
            error_code="INVALID_SOURCE_DATA",
            error_retryable=False,
        )
        raise
    _record_operation_partition(
        container,
        run_id=claim.run_id,
        partition_key=partition_key,
        status="SUCCEEDED",
        error_code=None,
    )
    publication = result.publication
    # 当前两个 AKShare 方法学均为 research；来源成功入库不等于存在可公开的 canonical 版本。
    return ExecutionOutcome(
        status="SUCCEEDED" if publication.published else "PARTIAL",
        completed_partitions=1,
        total_partitions=1,
        processed_records=(
            publication.inserted_count + publication.revised_count + publication.unchanged_count
        ),
    )


def _execute_etf(
    claim: ExecutionClaim,
    *,
    container: ServiceContainer,
    dataset_code: str,
) -> ExecutionOutcome:
    """执行 ETF 目录、旧单只或冻结双市全集；全集按实体分区独立提交。"""
    expected_operation, capability = _ETF_EXECUTIONS[dataset_code]
    selector = claim.target.get("selector")
    if (
        claim.dataset_code != dataset_code
        or not isinstance(selector, dict)
        or selector.get("kind") != "ETF"
        or selector.get("operation") != expected_operation
    ):
        raise ValueError("ETF dataset and selector operation do not match")
    execution = current_fenced_execution()
    if execution is None:
        raise RuntimeError("canonical executor requires an active fencing context")
    observation_date: date | None = None
    if expected_operation == "MASTER":
        observation_date = _etf_observation_date(claim.target)
        if (
            claim.target.get("mode") == "OBSERVATION_DATE"
            and observation_date != datetime.now(_SHANGHAI).date()
        ):
            return _reject_stale_etf_master(
                claim,
                container=container,
                all_venues=selector.get("scope") == "ALL_VENUES",
            )
    provider = _frozen_provider(claim.source_snapshot, container, capability)
    raw_store = S3RawPayloadStore(container.object_storage)
    availability = SqlAlchemyDatasetAvailabilityRepository(container.database)
    market_approval = _etf_market_approval(provider.provider_id)
    reference_approval = _etf_reference_approval(provider.provider_id)
    if expected_operation == "MASTER":
        assert observation_date is not None
        service = EtfMasterSyncService(
            source=FailureEvidenceDataSource(provider, raw_store),
            repository=SqlAlchemyEtfReferenceRepository(
                container.database,
                approved_sources={provider.provider_id: reference_approval},
            ),
            raw_payload_store=raw_store,
            availability_repository=availability,
        )
        if selector.get("scope") == "ALL_VENUES":
            return _execute_etf_master_all_venues(
                claim,
                container=container,
                service=service,
                raw_store=raw_store,
                observation_date=observation_date,
            )
        venue = selector.get("venue")
        if not isinstance(venue, str):
            raise ValueError("ETF master venue is invalid")
        if (
            venue == "SZSE"
            and claim.target.get("mode") == "OBSERVATION_DATE"
            and observation_date != datetime.now(_SHANGHAI).date()
        ):
            raise ValueError("SZSE ETF directory does not support historical snapshots")
        # 回调仅在应用用例确认非空事实并即将进入 canonical 发布事务时执行。
        result = retain_failure_evidence(
            raw_store,
            lambda: asyncio.run(
                service.sync(
                    venue=venue,
                    observation_date=observation_date,
                    before_final_publication=execution.arm_terminal_write,
                )
            ),
        )
        if result.availability == "source_unavailable":
            return ExecutionOutcome(
                status="FAILED",
                completed_partitions=0,
                total_partitions=1,
                error=_etf_source_unavailable_error(
                    reason_code=result.reason_code,
                    retryable=result.retryable,
                ),
            )
        return ExecutionOutcome(
            status="SUCCEEDED",
            completed_partitions=1,
            total_partitions=1,
            processed_records=result.inserted_count + result.unchanged_count,
        )
    service = _etf_partition_service(
        container=container,
        operation=expected_operation,
        provider=provider,
        raw_store=raw_store,
        availability=availability,
        market_approval=market_approval,
        reference_approval=reference_approval,
    )
    start, end = _etf_window(claim.target, execution_intent=claim.execution_intent)
    if selector.get("scope") == "ALL_ETFS":
        return _execute_etf_all(
            claim,
            container=container,
            selector=selector,
            service=service,
            raw_store=raw_store,
            start=start,
            end=end,
        )
    identifier_text = selector.get("etf")
    if not isinstance(identifier_text, str):
        raise ValueError("ETF identifier is invalid")
    identifier = EtfIdentifier.parse(identifier_text)
    # 旧单只执行继续把唯一 publication 与 run 成功终态放在同一 fencing 事务。
    result = _sync_etf_partition(
        service,
        raw_store=raw_store,
        identifier=identifier,
        start=start,
        end=end,
        before_final_publication=execution.arm_terminal_write,
    )
    if result.availability == "source_unavailable":
        return ExecutionOutcome(
            status="FAILED",
            completed_partitions=0,
            total_partitions=1,
            error=_etf_source_unavailable_error(
                reason_code=result.reason_code,
                retryable=result.retryable,
            ),
        )
    return ExecutionOutcome(
        status="SUCCEEDED",
        completed_partitions=1,
        total_partitions=1,
        processed_records=result.inserted_count + result.unchanged_count,
    )


def _reject_stale_etf_master(
    claim: ExecutionClaim,
    *,
    container: ServiceContainer,
    all_venues: bool,
) -> ExecutionOutcome:
    """在任何目录 Provider 请求前终止跨日 current-snapshot fire，避免把当前目录冒充历史快照。"""
    error = _etf_source_unavailable_error(
        reason_code="etf-profile-current-snapshot-unrecoverable",
        retryable=False,
    )
    if not all_venues:
        return ExecutionOutcome(
            status="FAILED",
            completed_partitions=0,
            total_partitions=1,
            error=error,
        )
    partition_keys = frozenset({"venue:SSE", "venue:SZSE"})
    _prepare_operation_partitions(
        container,
        run_id=claim.run_id,
        partition_keys=partition_keys,
        subject="ETF profile venues",
    )
    for partition_key in sorted(partition_keys):
        _record_operation_partition(
            container,
            run_id=claim.run_id,
            partition_key=partition_key,
            status="FAILED",
            error_code="ETF_PROFILE_CURRENT_SNAPSHOT_UNRECOVERABLE",
            error_retryable=False,
        )
    return ExecutionOutcome(
        status="FAILED",
        completed_partitions=0,
        total_partitions=2,
        error=error,
    )


def _execute_etf_master_all_venues(
    claim: ExecutionClaim,
    *,
    container: ServiceContainer,
    service: EtfMasterSyncService,
    raw_store: S3RawPayloadStore,
    observation_date: date,
) -> ExecutionOutcome:
    """显式同步 SSE、SZSE 两个目录分区，单边失败不会回滚另一边已发布 publication。"""
    venues = ("SSE", "SZSE")
    partition_keys = frozenset(f"venue:{venue}" for venue in venues)
    succeeded = _prepare_operation_partitions(
        container,
        run_id=claim.run_id,
        partition_keys=partition_keys,
        subject="ETF profile venues",
    )
    processed_records = 0
    circuit_error: dict[str, Any] | None = None
    failed_retryability: list[bool] = []
    for venue in venues:
        partition_key = f"venue:{venue}"
        if partition_key in succeeded:
            continue
        if _cancel_requested(container):
            completed, failed = _operation_partition_counts(
                container,
                run_id=claim.run_id,
                prefix="venue:",
            )
            return ExecutionOutcome(
                status="PARTIAL" if completed + failed else "CANCELLED",
                completed_partitions=completed,
                total_partitions=len(venues),
                processed_records=processed_records,
                error=(
                    _etf_cancelled_after_progress(completed=completed, failed=failed)
                    if completed + failed
                    else None
                ),
            )
        _record_operation_partition(
            container,
            run_id=claim.run_id,
            partition_key=partition_key,
            status="RUNNING",
            error_code=None,
        )
        try:
            if (
                venue == "SZSE"
                and claim.target.get("mode") == "OBSERVATION_DATE"
                and observation_date != datetime.now(_SHANGHAI).date()
            ):
                raise ValueError("SZSE ETF directory does not support historical snapshots")
            result = retain_failure_evidence(
                raw_store,
                lambda current=venue: asyncio.run(
                    service.sync(
                        venue=current,
                        observation_date=observation_date,
                    )
                ),
            )
        except ProviderError as error:
            reason_code = error.code.value
            _record_operation_partition(
                container,
                run_id=claim.run_id,
                partition_key=partition_key,
                status="FAILED",
                error_code=_etf_partition_error_code(reason_code),
                error_retryable=error.retryable,
            )
            failed_retryability.append(error.retryable)
            if _etf_source_circuit_opens(
                reason_code=reason_code,
                retryable=error.retryable,
            ):
                circuit_error = _etf_source_unavailable_error(
                    reason_code=reason_code,
                    retryable=error.retryable,
                )
                break
            continue
        except ValueError:
            _record_operation_partition(
                container,
                run_id=claim.run_id,
                partition_key=partition_key,
                status="FAILED",
                error_code="INVALID_SOURCE_DATA",
            )
            failed_retryability.append(False)
            circuit_error = _etf_source_unavailable_error(
                reason_code="invalid_source_data",
                retryable=False,
            )
            break
        if result.availability == "source_unavailable":
            reason_code = result.reason_code or "source_unavailable"
            _record_operation_partition(
                container,
                run_id=claim.run_id,
                partition_key=partition_key,
                status="FAILED",
                error_code=_etf_partition_error_code(reason_code),
                error_retryable=result.retryable,
            )
            failed_retryability.append(result.retryable)
            if _etf_source_circuit_opens(
                reason_code=reason_code,
                retryable=result.retryable,
            ):
                circuit_error = _etf_source_unavailable_error(
                    reason_code=reason_code,
                    retryable=result.retryable,
                )
                break
            continue
        processed_records += result.inserted_count + result.unchanged_count
        _record_operation_partition(
            container,
            run_id=claim.run_id,
            partition_key=partition_key,
            status="SUCCEEDED",
            error_code=None,
        )
    completed, failed = _operation_partition_counts(
        container,
        run_id=claim.run_id,
        prefix="venue:",
    )
    return ExecutionOutcome(
        status="SUCCEEDED" if failed == 0 else "FAILED" if completed == 0 else "PARTIAL",
        completed_partitions=completed,
        total_partitions=len(venues),
        processed_records=processed_records,
        error=(
            None
            if failed == 0
            else circuit_error
            or _etf_partition_failure(
                failed,
                retryable=all(failed_retryability),
            )
        ),
    )


def _etf_partition_service(
    *,
    container: ServiceContainer,
    operation: str,
    provider: DataSourcePort,
    raw_store: S3RawPayloadStore,
    availability: SqlAlchemyDatasetAvailabilityRepository,
    market_approval: EtfSourceApproval,
    reference_approval: EtfReferenceSourceApproval,
) -> EtfDailyBarSyncService | EtfNavSyncService | EtfStatusSyncService:
    """构造复用同一冻结来源和 canonical 仓储的 ETF 单实体同步用例。"""
    source = FailureEvidenceDataSource(provider, raw_store)
    if operation == "BARS":
        return EtfDailyBarSyncService(
            source=source,
            repository=SqlAlchemyEtfMarketDataRepository(
                container.database,
                approved_sources={provider.provider_id: market_approval},
            ),
            raw_payload_store=raw_store,
            availability_repository=availability,
        )
    if operation == "NAV":
        return EtfNavSyncService(
            source=source,
            repository=SqlAlchemyEtfMarketDataRepository(
                container.database,
                approved_sources={provider.provider_id: market_approval},
            ),
            raw_payload_store=raw_store,
            availability_repository=availability,
        )
    return EtfStatusSyncService(
        source=source,
        repository=SqlAlchemyEtfReferenceRepository(
            container.database,
            approved_sources={provider.provider_id: reference_approval},
        ),
        raw_payload_store=raw_store,
        availability_repository=availability,
    )


def _sync_etf_partition(
    service: EtfDailyBarSyncService | EtfNavSyncService | EtfStatusSyncService,
    *,
    raw_store: S3RawPayloadStore,
    identifier: EtfIdentifier,
    start: date,
    end: date,
    before_final_publication: Any = None,
) -> Any:
    """执行一个真实 ETF 分区并保留失败证据；调用方决定是否武装终态事务。"""
    return retain_failure_evidence(
        raw_store,
        lambda: asyncio.run(
            service.sync(
                etf=identifier,
                start=start,
                end=end,
                before_final_publication=before_final_publication,
            )
        ),
    )


def _execute_etf_all(
    claim: ExecutionClaim,
    *,
    container: ServiceContainer,
    selector: dict[str, Any],
    service: EtfDailyBarSyncService | EtfNavSyncService | EtfStatusSyncService,
    raw_store: S3RawPayloadStore,
    start: date,
    end: date,
) -> ExecutionOutcome:
    """按冻结双市目录逐 ETF 发布，成功分区可跨崩溃和显式 retry 继承。"""
    versions_raw = selector.get("profileDataVersions")
    if not isinstance(versions_raw, dict) or set(versions_raw) != {"SSE", "SZSE"}:
        raise ValueError("ALL_ETFS requires frozen SSE and SZSE profile data versions")
    try:
        versions = {venue: UUID(str(versions_raw[venue])) for venue in ("SSE", "SZSE")}
        with container.database.session() as session:
            snapshot = load_frozen_etf_universe(
                session,
                profile_data_versions=versions,
            )
    except (TypeError, ValueError, EtfUniverseUnavailable) as error:
        raise ValueError("frozen ETF universe is unavailable") from error
    intent = claim.execution_intent
    if (
        not isinstance(intent, dict)
        or intent.get("etfUniverseCount") != snapshot.count
        or intent.get("etfUniverseHash") != snapshot.universe_hash
    ):
        raise ValueError("frozen ETF universe evidence does not match preflight")
    nav_operation = selector.get("operation") == "NAV"
    if nav_operation and (
        intent.get("etfNavEligibleCount") != snapshot.nav_eligible_count
        or intent.get("etfNavUnsupportedCount") != snapshot.nav_unsupported_count
    ):
        raise ValueError("frozen ETF NAV eligibility evidence does not match preflight")
    succeeded = _prepare_etf_operation_partitions(
        container,
        run_id=claim.run_id,
        identifiers=snapshot.identifiers,
    )
    unsupported_by_key = (
        {item.identifier.qualified_key: item for item in snapshot.nav_unsupported}
        if nav_operation
        else {}
    )
    if unsupported_by_key and not isinstance(service, EtfNavSyncService):
        raise ValueError("frozen ETF NAV eligibility requires the NAV sync service")
    for member in snapshot.nav_unsupported if nav_operation else ():
        partition_key = _etf_partition(member.identifier)
        if partition_key in succeeded:
            continue
        assert isinstance(service, EtfNavSyncService)
        service.mark_currently_unsupported(
            etf=member.identifier,
            start=start,
            end=end,
            reason_code=member.reason_code,
        )
        _record_operation_partition(
            container,
            run_id=claim.run_id,
            partition_key=partition_key,
            status="SKIPPED",
            error_code=member.reason_code,
            error_stage="ELIGIBILITY",
            checkpoint_evidence={
                "profileDataVersions": {
                    venue: str(snapshot.profile_data_versions[venue]) for venue in ("SSE", "SZSE")
                },
                "etfUniverseHash": snapshot.universe_hash,
                "etf": member.identifier.qualified_key,
                "reasonCode": member.reason_code,
            },
        )
    processed_records = 0
    circuit_error: dict[str, Any] | None = None
    failed_retryability: list[bool] = []
    last_provider_call_at: float | None = None
    for identifier in snapshot.identifiers:
        partition_key = _etf_partition(identifier)
        if partition_key in succeeded or identifier.qualified_key in unsupported_by_key:
            continue
        if _cancel_requested(container):
            completed, failed = _etf_partition_counts(container, run_id=claim.run_id)
            return ExecutionOutcome(
                status="PARTIAL" if completed + failed else "CANCELLED",
                completed_partitions=completed,
                total_partitions=snapshot.count,
                processed_records=processed_records,
                error=(
                    _etf_cancelled_after_progress(completed=completed, failed=failed)
                    if completed + failed
                    else None
                ),
            )
        _record_operation_partition(
            container,
            run_id=claim.run_id,
            partition_key=partition_key,
            status="RUNNING",
            error_code=None,
        )
        try:
            last_provider_call_at = _pace_etf_provider(
                container,
                last_provider_call_at=last_provider_call_at,
            )
            result = _sync_etf_partition(
                service,
                raw_store=raw_store,
                identifier=identifier,
                start=start,
                end=end,
            )
        except ProviderError as error:
            reason_code = error.code.value
            _record_operation_partition(
                container,
                run_id=claim.run_id,
                partition_key=partition_key,
                status="FAILED",
                error_code=_etf_partition_error_code(reason_code),
                error_retryable=error.retryable,
            )
            failed_retryability.append(error.retryable)
            if _etf_source_circuit_opens(
                reason_code=reason_code,
                retryable=error.retryable,
            ):
                circuit_error = _etf_source_unavailable_error(
                    reason_code=reason_code,
                    retryable=error.retryable,
                )
                break
            continue
        except ValueError:
            _record_operation_partition(
                container,
                run_id=claim.run_id,
                partition_key=partition_key,
                status="FAILED",
                error_code="INVALID_SOURCE_DATA",
            )
            failed_retryability.append(False)
            circuit_error = _etf_source_unavailable_error(
                reason_code="invalid_source_data",
                retryable=False,
            )
            break
        if result.availability == "source_unavailable":
            reason_code = result.reason_code or "source_unavailable"
            _record_operation_partition(
                container,
                run_id=claim.run_id,
                partition_key=partition_key,
                status="FAILED",
                error_code=_etf_partition_error_code(reason_code),
                error_retryable=result.retryable,
            )
            failed_retryability.append(result.retryable)
            if _etf_source_circuit_opens(
                reason_code=reason_code,
                retryable=result.retryable,
            ):
                circuit_error = _etf_source_unavailable_error(
                    reason_code=reason_code,
                    retryable=result.retryable,
                )
                break
            continue
        if result.availability == "currently_unsupported":
            _record_operation_partition(
                container,
                run_id=claim.run_id,
                partition_key=partition_key,
                status="SKIPPED",
                error_code=result.reason_code or "CURRENTLY_UNSUPPORTED",
                error_stage="ELIGIBILITY",
            )
            continue
        processed_records += result.inserted_count + result.unchanged_count
        _record_operation_partition(
            container,
            run_id=claim.run_id,
            partition_key=partition_key,
            status="SUCCEEDED",
            error_code=None,
        )
    completed, failed = _etf_partition_counts(container, run_id=claim.run_id)
    if failed == 0:
        status = "SUCCEEDED"
        error = None
    elif completed == 0:
        status = "FAILED"
        error = circuit_error or _etf_partition_failure(
            failed,
            retryable=all(failed_retryability),
        )
    else:
        status = "PARTIAL"
        error = circuit_error or _etf_partition_failure(
            failed,
            retryable=all(failed_retryability),
        )
    if error is not None and error.get("retryable") is True:
        _wait_for_etf_auto_retry(container, attempt=claim.attempt)
    return ExecutionOutcome(
        status=status,
        completed_partitions=completed,
        total_partitions=snapshot.count,
        processed_records=processed_records,
        error=error,
    )


def _etf_partition_failure(failed: int, *, retryable: bool) -> dict[str, Any]:
    """构造不含代码、URL 或来源正文的全量 ETF 分区失败摘要。"""
    return {
        "code": "etf-partition-failures",
        "stage": "PROVIDER_FETCH",
        "retryable": retryable,
        "message": (
            f"{failed} ETF partitions failed and can be retried"
            if retryable
            else f"{failed} ETF partitions failed with a non-retryable error"
        ),
    }


def _etf_source_unavailable_error(
    *,
    reason_code: str | None,
    retryable: bool,
) -> dict[str, Any]:
    """把应用层非事实来源不可用结果映射为失败 run，而不泄漏上游正文。"""
    normalized_reason = reason_code or "source_unavailable"
    return {
        "code": normalized_reason.replace("_", "-"),
        "stage": "PROVIDER_FETCH",
        "retryable": retryable,
        "message": "ETF data source is unavailable",
    }


def _etf_partition_error_code(reason_code: str) -> str:
    """把内部稳定来源原因压成分区账本允许的短代码，不写入 Provider 原文。"""
    return reason_code.upper().replace("-", "_")[:64]


def _etf_source_circuit_opens(*, reason_code: str, retryable: bool) -> bool:
    """对系统性或永久来源失败立即熔断，避免全市场逐实体放大认证、限流和配置错误。"""
    return not retryable or reason_code in {
        ProviderErrorCode.RATE_LIMITED.value,
        ProviderErrorCode.AUTHENTICATION.value,
        ProviderErrorCode.INVALID_REQUEST.value,
        ProviderErrorCode.SCHEMA.value,
        "capability_not_configured",
    }


def _pace_etf_provider(
    container: ServiceContainer,
    *,
    last_provider_call_at: float | None,
) -> float:
    """在全量逐实体请求间执行可配置最小间隔，避免向腾讯或东财突发放大。"""
    settings = getattr(container, "settings", None)
    interval = float(getattr(settings, "etf_provider_min_interval_seconds", 0))
    now = monotonic()
    if last_provider_call_at is not None and interval > 0:
        remaining = interval - (now - last_provider_call_at)
        if remaining > 0:
            sleep(remaining)
    return monotonic()


def _wait_for_etf_auto_retry(container: ServiceContainer, *, attempt: int) -> None:
    """在释放执行槽前执行有上限指数退避，随后由十秒 dispatcher tick 自动续跑同一 run。"""
    settings = getattr(container, "settings", None)
    base = float(getattr(settings, "etf_auto_retry_base_seconds", 0))
    maximum = float(getattr(settings, "etf_auto_retry_max_seconds", 0))
    if base <= 0 or maximum <= 0:
        return
    delay = min(maximum, base * (2 ** max(0, attempt - 1)))
    sleep(delay)


def _etf_cancelled_after_progress(*, completed: int, failed: int) -> dict[str, Any]:
    """构造取消时已存在分区结果的部分完成摘要。"""
    return {
        "code": "cancelled-after-partial-progress",
        "stage": "QUEUE",
        "retryable": True,
        "message": f"Cancelled after {completed} succeeded and {failed} failed ETF partitions",
    }


def _etf_market_approval(provider_id: str) -> EtfSourceApproval:
    """生成 ETF 行情与净值仓储使用的内部研究来源批准边界。"""
    return EtfSourceApproval(
        provider_id=provider_id,
        source_code="akshare_reviewed_public_market",
        legal_name="AKShare 聚合的公开市场来源",
        source_kind="aggregator",
        rights_status="internal",
        license_scope="internal_research_no_redistribution",
    )


def _etf_reference_approval(provider_id: str) -> EtfReferenceSourceApproval:
    """生成 ETF 目录与状态仓储使用的内部研究来源批准边界。"""
    return EtfReferenceSourceApproval(
        provider_id=provider_id,
        source_code="akshare_reviewed_public_market",
        legal_name="AKShare 聚合的公开市场来源",
        source_kind="aggregator",
        rights_status="internal",
        license_scope="internal_research_no_redistribution",
    )


def _etf_observation_date(target: dict[str, object]) -> date:
    """解析 ETF 目录观察日；FULL 使用上海当前自然日，绝不静默回退 publication。"""
    mode = target.get("mode")
    if mode == "FULL":
        return datetime.now(_SHANGHAI).date()
    if mode == "OBSERVATION_DATE":
        value = target.get("observationDate")
        if isinstance(value, str):
            return date.fromisoformat(value)
    raise ValueError("ETF master target mode is unsupported")


def _etf_window(
    target: dict[str, object],
    *,
    execution_intent: dict[str, Any] | None,
) -> tuple[date, date]:
    """优先读取受理时冻结日期窗；旧兼容调用才按既有模式解析当前日期。"""
    if execution_intent is not None:
        frozen_start = execution_intent.get("etfResolvedDateFrom")
        frozen_end = execution_intent.get("etfResolvedDateTo")
        if isinstance(frozen_start, str) and isinstance(frozen_end, str):
            start = date.fromisoformat(frozen_start)
            end = date.fromisoformat(frozen_end)
            if start > end:
                raise ValueError("ETF frozen date window is invalid")
            return start, end
    selector = target.get("selector")
    if isinstance(selector, dict) and selector.get("scope") == "ALL_ETFS":
        raise ValueError("ALL_ETFS requires a frozen execution date window")
    mode = target.get("mode")
    end = datetime.now(_SHANGHAI).date()
    if mode == "FULL":
        return _HISTORY_START, end
    if mode == "INCREMENTAL":
        return end - timedelta(days=31), end
    if mode == "OBSERVATION_DATE":
        value = target.get("observationDate")
        if isinstance(value, str):
            observation_date = date.fromisoformat(value)
            return observation_date, observation_date
    if mode == "DATE_RANGE":
        start_value = target.get("dateFrom")
        end_value = target.get("dateTo")
        if isinstance(start_value, str) and isinstance(end_value, str):
            return date.fromisoformat(start_value), date.fromisoformat(end_value)
    raise ValueError("ETF date target mode is unsupported")


def _execute_sector_catalog(
    claim: ExecutionClaim, *, container: ServiceContainer
) -> ExecutionOutcome:
    """串行发布 selector 覆盖的行业/概念完整目录，并在末次发布内完成 run。"""
    if claim.target.get("mode") not in {"FULL", "INCREMENTAL", "OBSERVATION_DATE"}:
        raise ValueError("sector catalog target mode is unsupported")
    schemes = _catalog_schemes(claim.target["selector"])
    observation_date = _catalog_snapshot_date(claim.target)
    provider = _frozen_provider(claim.source_snapshot, container, _SECTOR_CATALOG_CAPABILITY)
    repository = SqlAlchemySectorMarketDataRepository(container.database)
    inserted = 0
    unchanged = 0
    for index, scheme in enumerate(schemes):
        if _cancel_requested(container):
            return ExecutionOutcome(
                status="PARTIAL" if index else "CANCELLED",
                completed_partitions=index,
                total_partitions=len(schemes),
                processed_records=inserted + unchanged,
            )
        result = _sync_sector_catalog(
            provider=provider,
            repository=repository,
            raw_store=S3RawPayloadStore(container.object_storage),
            scheme=scheme,
            observation_date=observation_date,
            final_write=index == len(schemes) - 1,
        )
        inserted += result[0]
        unchanged += result[1]
    return ExecutionOutcome(
        status="SUCCEEDED",
        completed_partitions=len(schemes),
        total_partitions=len(schemes),
        processed_records=inserted + unchanged,
    )


def _execute_sector_membership(
    claim: ExecutionClaim, *, container: ServiceContainer
) -> ExecutionOutcome:
    """按 ACTIVE 目录抓取真实板块成分，只有每个所选 scheme 都有稳定 release 才成功。"""
    if claim.target.get("mode") not in {"FULL", "INCREMENTAL", "OBSERVATION_DATE"}:
        raise ValueError("sector membership target mode is unsupported")
    selections = _membership_selections(claim.target["selector"])
    observation_date = _membership_observation_date(claim.target)
    provider = _frozen_provider(
        claim.source_snapshot,
        container,
        _SECTOR_MEMBERSHIP_CAPABILITY,
    )
    repository = SqlAlchemySectorMembershipRepository(container.database)
    total_partitions = sum(
        _membership_partition_count(repository, scheme=scheme, sector_codes=sector_codes)
        for scheme, sector_codes in selections
    )
    raw_store = S3RawPayloadStore(container.object_storage)
    completed_partitions = 0
    all_released = True
    for index, (scheme, sector_codes) in enumerate(selections):
        if _cancel_requested(container):
            return ExecutionOutcome(
                status="PARTIAL" if completed_partitions else "CANCELLED",
                completed_partitions=completed_partitions,
                total_partitions=total_partitions,
                processed_records=completed_partitions,
            )
        result = _sync_sector_membership(
            provider=provider,
            repository=repository,
            raw_store=raw_store,
            scheme=scheme,
            sector_codes=sector_codes,
            observation_date=observation_date,
            final_write=index == len(selections) - 1 and all_released,
        )
        completed_partitions += len(result.items) + len(result.failures)
        all_released = all_released and result.release is not None
    return ExecutionOutcome(
        status="SUCCEEDED" if all_released else "PARTIAL",
        completed_partitions=completed_partitions,
        total_partitions=total_partitions,
        processed_records=completed_partitions,
        error=(
            None
            if all_released
            else {
                "code": "sector-membership-release-unavailable",
                "stage": "QUALITY_GATE",
                "retryable": True,
            }
        ),
    )


def _execute_sector_eod_snapshot(
    claim: ExecutionClaim, *, container: ServiceContainer
) -> ExecutionOutcome:
    """按 selector 串行发布板块 EOD 完整横截面，不允许 command 静默降级为 shadow。"""
    if claim.target.get("mode") not in {"FULL", "OBSERVATION_DATE"}:
        raise ValueError("sector eod target mode is unsupported")
    if not container.settings.sector_eod_publish_enabled:
        raise RuntimeError("sector eod canonical publication is disabled")
    schemes = _eod_schemes(claim.target["selector"])
    trade_date = _current_snapshot_date(claim.target)
    provider = _frozen_provider(claim.source_snapshot, container, _SECTOR_EOD_CAPABILITY)
    repository = SqlAlchemySectorEodRepository(container.database)
    for index, scheme in enumerate(schemes):
        if _cancel_requested(container):
            return ExecutionOutcome(
                status="PARTIAL" if index else "CANCELLED",
                completed_partitions=index,
                total_partitions=len(schemes),
            )
        _sync_sector_eod_snapshot(
            container=container,
            provider=provider,
            repository=repository,
            raw_store=S3RawPayloadStore(container.object_storage),
            scheme=scheme,
            trade_date=trade_date,
            final_write=index == len(schemes) - 1,
        )
    return ExecutionOutcome(
        status="SUCCEEDED",
        completed_partitions=len(schemes),
        total_partitions=len(schemes),
    )


def _execute_sector_sw_taxonomy(
    claim: ExecutionClaim, *, container: ServiceContainer
) -> ExecutionOutcome:
    """发布一个申万完整 taxonomy/估值快照，selector 不会伪造逐行业来源过滤。"""
    if claim.target.get("mode") not in {"FULL", "OBSERVATION_DATE"}:
        raise ValueError("SW sector target mode is unsupported")
    _validate_sw_selector(claim.target["selector"])
    snapshot_date = _current_snapshot_date(claim.target)
    if _cancel_requested(container):
        return ExecutionOutcome(status="CANCELLED", completed_partitions=0, total_partitions=1)
    provider = _frozen_provider(claim.source_snapshot, container, _SECTOR_SW_CAPABILITY)
    result = _sync_sector_sw_taxonomy(
        provider=provider,
        repository=SqlAlchemySwSectorRepository(container.database),
        raw_store=S3RawPayloadStore(container.object_storage),
        snapshot_date=snapshot_date,
    )
    return ExecutionOutcome(
        status="SUCCEEDED",
        completed_partitions=1,
        total_partitions=1,
        processed_records=result.publications.taxonomy.row_count,
    )


def _execute_equity_trading_status(
    claim: ExecutionClaim, *, container: ServiceContainer
) -> ExecutionOutcome:
    """通过单一 fenced publication 同步指定观察日普通停牌完整清单。"""
    if claim.target.get("mode") not in {"FULL", "OBSERVATION_DATE"}:
        raise ValueError("equity trading status target mode is unsupported")
    selector = claim.target.get("selector")
    if not isinstance(selector, dict) or selector.get("kind") != "GLOBAL":
        raise ValueError("equity trading status requires GLOBAL selector")
    observation_date = _latest_complete_trading_day(claim.target, container=container)
    provider = _frozen_provider(claim.source_snapshot, container, _EQUITY_TRADING_STATUS_CAPABILITY)
    raw_store = S3RawPayloadStore(container.object_storage)
    execution = _required_execution()
    execution.arm_terminal_write()
    result = retain_failure_evidence(
        raw_store,
        lambda: asyncio.run(
            EquityTradingStatusSyncService(
                source=FailureEvidenceDataSource(provider, raw_store),
                repository=_equity_workspace_repository(container),
                raw_payload_store=raw_store,
            ).sync(observation_date=observation_date)
        ),
    )
    return ExecutionOutcome(
        status="SUCCEEDED",
        completed_partitions=1,
        total_partitions=1,
        processed_records=result.inserted_count + result.unchanged_count,
    )


def _execute_equity_discovery(
    claim: ExecutionClaim, *, container: ServiceContainer
) -> ExecutionOutcome:
    """只从冻结 canonical 组件构建发现横截面，并允许可选族原因化降级。"""
    if claim.target.get("mode") not in {"FULL", "OBSERVATION_DATE"}:
        raise ValueError("equity discovery target mode is unsupported")
    selector = claim.target.get("selector")
    if not isinstance(selector, dict) or selector.get("kind") != "GLOBAL":
        raise ValueError("equity discovery requires GLOBAL selector")
    if _cancel_requested(container):
        return ExecutionOutcome(
            status="CANCELLED",
            completed_partitions=0,
            total_partitions=1,
        )
    as_of = _latest_complete_trading_day(claim.target, container=container)
    execution = _required_execution()
    execution.arm_terminal_write()
    result = SqlAlchemyEquityDiscoveryRepository(container.database).build(
        as_of=as_of,
        reference_manifest=_equity_backfill_reference_manifest(claim),
    )
    return ExecutionOutcome(
        status="SUCCEEDED",
        completed_partitions=1,
        total_partitions=1,
        processed_records=result.row_count,
    )


def _execute_equity_share_capital(
    claim: ExecutionClaim, *, container: ServiceContainer
) -> ExecutionOutcome:
    """按受理时冻结的永久身份名单串行发布股本历史并安全续跑。"""
    if claim.target.get("mode") not in {"FULL", "INCREMENTAL"}:
        raise ValueError("equity share capital target mode is unsupported")
    identities = _frozen_share_capital_roster(claim)
    if not identities:
        return ExecutionOutcome(status="SUCCEEDED", completed_partitions=0, total_partitions=0)
    completed = _prepare_operation_partitions(
        container,
        run_id=claim.run_id,
        partition_keys=frozenset(
            _security_partition(identity.instrument_id) for identity in identities
        ),
        subject="share capital identity roster",
    )
    remaining = tuple(
        identity
        for identity in identities
        if _security_partition(identity.instrument_id) not in completed
    )
    execution = _required_execution()
    execution.completed_partitions = len(completed)
    provider = _frozen_provider(claim.source_snapshot, container, _EQUITY_SHARE_CAPITAL_CAPABILITY)
    repository = _equity_workspace_repository(container)
    raw_store = S3RawPayloadStore(container.object_storage)
    inserted = 0
    unchanged = 0
    failed = 0
    for index, identity in enumerate(remaining):
        if _cancel_requested(container):
            return ExecutionOutcome(
                status="PARTIAL" if completed or index else "CANCELLED",
                completed_partitions=len(completed) + index,
                total_partitions=len(identities),
                processed_records=inserted + unchanged,
            )
        if not _frozen_equity_identity_is_current(container, identity=identity):
            _record_operation_partition(
                container,
                run_id=claim.run_id,
                partition_key=_security_partition(identity.instrument_id),
                status="FAILED",
                error_code="IDENTITY_CHANGED",
            )
            failed += 1
            continue
        final_write = index == len(remaining) - 1 and failed == 0
        if final_write:
            execution.arm_terminal_write()
        try:
            result = retain_failure_evidence(
                raw_store,
                lambda current=identity: asyncio.run(
                    EquityShareCapitalSyncService(
                        source=FailureEvidenceDataSource(provider, raw_store),
                        repository=repository,
                        raw_payload_store=raw_store,
                    ).sync(
                        identifier=current.identifier,
                        instrument_id=current.instrument_id,
                        identity_as_of=current.identity_as_of,
                    )
                ),
            )
        except ProviderError as error:
            execution.disarm_terminal_write()
            _record_operation_partition(
                container,
                run_id=claim.run_id,
                partition_key=_security_partition(identity.instrument_id),
                status="FAILED",
                error_code="SOURCE_UNAVAILABLE",
            )
            if error.retryable:
                raise
            failed += 1
            continue
        inserted += result.inserted_count
        unchanged += result.unchanged_count
        if not final_write:
            _record_operation_partition(
                container,
                run_id=claim.run_id,
                partition_key=_security_partition(identity.instrument_id),
                status="SUCCEEDED",
                error_code=None,
            )
    if not remaining:
        return ExecutionOutcome(
            status="SUCCEEDED",
            completed_partitions=len(identities),
            total_partitions=len(identities),
        )
    return ExecutionOutcome(
        status="PARTIAL" if failed else "SUCCEEDED",
        completed_partitions=len(identities) - failed,
        total_partitions=len(identities),
        processed_records=inserted + unchanged,
    )


def _execute_sw_membership(
    claim: ExecutionClaim, *, container: ServiceContainer
) -> ExecutionOutcome:
    """按已发布 taxonomy 节点串行发布当前申万成分，不从页面分析列推断父级。"""
    if claim.target.get("mode") not in {"FULL", "OBSERVATION_DATE"}:
        raise ValueError("SW membership target mode is unsupported")
    observation_date = _current_snapshot_date(claim.target)
    node_codes = _sw_membership_node_codes(
        container,
        selector=claim.target["selector"],
    )
    if not node_codes:
        raise RuntimeError("published SW taxonomy contains no third-level nodes")
    provider = _frozen_provider(claim.source_snapshot, container, _SW_MEMBERSHIP_CAPABILITY)
    repository = _equity_workspace_repository(container)
    raw_store = S3RawPayloadStore(container.object_storage)
    inserted = 0
    unchanged = 0
    for index, node_code in enumerate(node_codes):
        if _cancel_requested(container):
            return ExecutionOutcome(
                status="PARTIAL" if index else "CANCELLED",
                completed_partitions=index,
                total_partitions=len(node_codes),
                processed_records=inserted + unchanged,
            )
        if index == len(node_codes) - 1:
            _required_execution().arm_terminal_write()
        result = retain_failure_evidence(
            raw_store,
            lambda current=node_code: asyncio.run(
                SwMembershipSyncService(
                    source=FailureEvidenceDataSource(provider, raw_store),
                    repository=repository,
                    raw_payload_store=raw_store,
                ).sync(node_code=current, observation_date=observation_date)
            ),
        )
        inserted += result.inserted_count
        unchanged += result.unchanged_count
    return ExecutionOutcome(
        status="SUCCEEDED",
        completed_partitions=len(node_codes),
        total_partitions=len(node_codes),
        processed_records=inserted + unchanged,
    )


def _equity_workspace_repository(
    container: ServiceContainer,
) -> SqlAlchemyEquityWorkspaceRepository:
    """构造只允许个人内部研究、禁止再分发的显式 P0 来源批准仓储。"""
    approval = EquityWorkspaceSourceApproval(
        provider_id="akshare",
        source_code="akshare",
        legal_name="AKShare",
        source_kind="community_aggregator",
        rights_status="personal_internal_research",
        license_scope="internal_research_no_redistribution",
    )
    return SqlAlchemyEquityWorkspaceRepository(
        container.database,
        approved_sources={approval.provider_id: approval},
    )


def _required_execution() -> FencedExecution:
    """返回当前 fenced execution；绕过统一 dispatcher 的调用立即失败。"""
    execution = current_fenced_execution()
    if execution is None:
        raise RuntimeError("canonical executor requires an active fencing context")
    return execution


def _sw_membership_node_codes(container: ServiceContainer, *, selector: object) -> tuple[str, ...]:
    """从当前已发布申万 taxonomy 读取三级节点，单节点也必须先存在于该 publication。"""
    if not isinstance(selector, dict):
        raise ValueError("SW membership selector is invalid")
    with container.database.session() as session:
        publication = session.execute(
            select(SwSectorPublication)
            .join(
                DatasetPublication,
                DatasetPublication.data_version == SwSectorPublication.data_version,
            )
            .where(
                DatasetPublication.dataset == "sector.sw.taxonomy",
                DatasetPublication.superseded_at.is_(None),
                SwSectorPublication.capability == "sector.sw.taxonomy",
            )
            .order_by(
                SwSectorPublication.snapshot_date.desc(),
                SwSectorPublication.published_at.desc(),
            )
            .limit(1)
        ).scalar_one_or_none()
        if publication is None:
            raise RuntimeError("SW taxonomy publication is unavailable")
        rows = (
            session.execute(
                select(SwSectorNodeRevision.sector_code).where(
                    SwSectorNodeRevision.snapshot_date == publication.snapshot_date,
                    SwSectorNodeRevision.methodology_id == publication.methodology_id,
                    SwSectorNodeRevision.level == 3,
                    SwSectorNodeRevision.known_to.is_(None),
                    SwSectorNodeRevision.quality_status.in_(("passed", "warned")),
                )
            )
            .scalars()
            .all()
        )
    available = {
        value[:-3] if isinstance(value, str) and value.endswith(".SI") else "" for value in rows
    }
    available.discard("")
    kind = selector.get("kind")
    if kind == "GLOBAL":
        return tuple(sorted(available))
    if kind == "SECTOR" and selector.get("scheme") == _SECTOR_SW_SCHEME:
        raw_code = selector.get("sectorCode")
        if not isinstance(raw_code, str):
            raise ValueError("SW membership sector code is invalid")
        code = raw_code[:-3] if raw_code.endswith(".SI") else raw_code
        if code not in available:
            raise ValueError("SW membership sector is not in the published taxonomy")
        return (code,)
    raise ValueError("SW membership requires GLOBAL or SW SECTOR selector")


def _catalog_schemes(selector: object) -> tuple[SectorScheme, ...]:
    """把板块目录选择器转换为稳定 scheme 序列，拒绝目录不支持的逐板块范围。"""
    if not isinstance(selector, dict):
        raise ValueError("sector catalog selector is invalid")
    if selector.get("kind") == "GLOBAL":
        return tuple(SectorScheme)
    if selector.get("kind") == "SCHEME":
        return (_sector_scheme(selector),)
    raise ValueError("sector catalog requires GLOBAL or SCHEME selector")


def _equity_exchanges(selector: object, *, global_only: bool) -> tuple[Exchange, ...]:
    """把受控选择器转换为交易所分区，并为全市场聚合拒绝局部刷新。"""
    if not isinstance(selector, dict):
        raise ValueError("equity exchange selector is invalid")
    if selector.get("kind") == "GLOBAL":
        return tuple(Exchange)
    if not global_only and selector.get("kind") == "EXCHANGE":
        value = selector.get("exchange")
        if isinstance(value, str):
            try:
                return (Exchange(value),)
            except ValueError as error:
                raise ValueError("equity exchange selector is invalid") from error
    raise ValueError(
        "equity master requires GLOBAL selector"
        if global_only
        else "equity lifecycle requires GLOBAL or EXCHANGE selector"
    )


def _eod_schemes(selector: object) -> tuple[SectorScheme, ...]:
    """把 EOD 选择器转换为完整横截面 scheme，逐板块目标仍必须拉取全量质量门。"""
    if not isinstance(selector, dict):
        raise ValueError("sector eod selector is invalid")
    kind = selector.get("kind")
    if kind == "GLOBAL":
        return tuple(SectorScheme)
    if kind in {"SCHEME", "SECTOR"}:
        # 东财只提供分类体系完整横截面；局部板块请求必须保留完整覆盖率质量门。
        return (_sector_scheme(selector),)
    raise ValueError("sector eod selector is unsupported")


def _sector_scheme(selector: dict[str, object]) -> SectorScheme:
    """读取严格选择器中的东财分类体系，拒绝未知文本被误映射为默认来源。"""
    value = selector.get("scheme")
    if not isinstance(value, str):
        raise ValueError("sector scheme is invalid")
    try:
        return SectorScheme(value)
    except ValueError as error:
        raise ValueError("sector scheme is unsupported") from error


def _validate_sw_selector(selector: object) -> None:
    """验证申万当前快照只接受其固定方法学，避免借 selector 切换到东财体系。"""
    if not isinstance(selector, dict):
        raise ValueError("SW sector selector is invalid")
    kind = selector.get("kind")
    if kind == "GLOBAL":
        return
    if kind in {"SCHEME", "SECTOR"} and selector.get("scheme") == _SECTOR_SW_SCHEME:
        # 申万来源始终返回三级完整闭包，不能为单节点请求丢弃父级或估值集合。
        return
    raise ValueError("SW sector selector is unsupported")


def _current_snapshot_date(target: dict[str, object]) -> date:
    """解析具名观察日，或为 FULL 当前完整快照固定上海本地自然日。"""
    mode = target.get("mode")
    if mode == "OBSERVATION_DATE":
        value = target.get("observationDate")
        if not isinstance(value, str):
            raise ValueError("observation date is invalid")
        try:
            return date.fromisoformat(value)
        except ValueError as error:
            raise ValueError("observation date is invalid") from error
    if mode == "FULL":
        return sector_eod_scheduler_target_date(datetime.now(UTC))
    raise ValueError("current snapshot target mode is unsupported")


def _latest_complete_trading_day(target: dict[str, object], *, container: ServiceContainer) -> date:
    """依据权威日历与 16:15 截点选择最近完整交易日，绝不把盘中今日当 EOD。"""
    now = datetime.now(UTC)
    candidate = _current_snapshot_date(target)
    if target.get("mode") == "OBSERVATION_DATE":
        state = container.trading_calendar.is_open(trade_date=candidate)
        if state is None:
            raise RuntimeError("authoritative trading calendar is unavailable")
        if not state:
            raise ValueError("observation date is not an open trading day")
        if sector_eod_source_cutoff_at(candidate) > now:
            raise ValueError("observation date has not reached the final EOD cutoff")
        return candidate
    for _ in range(32):
        state = container.trading_calendar.is_open(trade_date=candidate)
        if state is None:
            raise RuntimeError("authoritative trading calendar is unavailable")
        if state and sector_eod_source_cutoff_at(candidate) <= now:
            return candidate
        candidate -= timedelta(days=1)
    raise RuntimeError("recent complete trading day is unavailable")


def _catalog_snapshot_date(target: dict[str, object]) -> date:
    """为目录当前快照提供受控观察日，INCREMENTAL 也不能绕过历史日期校验。"""
    if target.get("mode") == "INCREMENTAL":
        return sector_eod_scheduler_target_date(datetime.now(UTC))
    return _current_snapshot_date(target)


def _membership_observation_date(target: dict[str, object]) -> date:
    """把当前集合固定到真实抓取自然日，并防止 executor 绕过预检倒填历史。"""
    current = datetime.now(_SHANGHAI).date()
    if target.get("mode") in {"FULL", "INCREMENTAL"}:
        return current
    value = target.get("observationDate")
    if not isinstance(value, str) or date.fromisoformat(value) != current:
        raise ValueError("sector membership supports only the current observation date")
    return current


def _membership_selections(
    selector: object,
) -> tuple[tuple[SectorScheme, tuple[str, ...] | None], ...]:
    """把 GLOBAL、SCHEME 或 SECTOR 选择器收敛为不跨体系混用的显式分区。"""
    if not isinstance(selector, dict):
        raise ValueError("sector membership selector is invalid")
    kind = selector.get("kind")
    if kind == "GLOBAL":
        return tuple((scheme, None) for scheme in SectorScheme)
    scheme_value = selector.get("scheme")
    if not isinstance(scheme_value, str):
        raise ValueError("sector membership scheme is invalid")
    try:
        scheme = SectorScheme(scheme_value)
    except ValueError as error:
        raise ValueError("sector membership scheme is unsupported") from error
    if kind == "SCHEME":
        return ((scheme, None),)
    sector_code = selector.get("sectorCode")
    if kind == "SECTOR" and isinstance(sector_code, str) and sector_code:
        return ((scheme, (sector_code,)),)
    raise ValueError("sector membership selector is invalid")


def _membership_partition_count(
    repository: SqlAlchemySectorMembershipRepository,
    *,
    scheme: SectorScheme,
    sector_codes: tuple[str, ...] | None,
) -> int:
    """按仓储冻结的 ACTIVE 集合计算真实分区数，未知单板块在抓源前失败。"""
    active_codes = tuple(
        sector.identifier.code for sector in repository.list_active_sectors(scheme=scheme)
    )
    if sector_codes is None:
        return len(active_codes)
    if not set(sector_codes).issubset(active_codes):
        raise ValueError("sector membership selection contains an inactive sector")
    return len(sector_codes)


def _sync_sector_membership(
    *,
    provider: DataSourcePort,
    repository: SqlAlchemySectorMembershipRepository,
    raw_store: S3RawPayloadStore,
    scheme: SectorScheme,
    sector_codes: tuple[str, ...] | None,
    observation_date: date,
    final_write: bool,
) -> SectorMembershipSyncResult:
    """执行一个 scheme 成分任务，并只在真实 release 事务中武装最终成功。"""
    execution = _required_execution()
    return retain_failure_evidence(
        raw_store,
        lambda: asyncio.run(
            SectorMembershipSyncService(
                source=FailureEvidenceDataSource(provider, raw_store),
                repository=repository,
                raw_payload_store=raw_store,
                retry_delay_seconds=2,
            ).sync_scheme(
                scheme=scheme,
                observation_date=observation_date,
                sector_codes=sector_codes,
                before_final_publication=(execution.arm_terminal_write if final_write else None),
            )
        ),
    )


def _sync_sector_catalog(
    *,
    provider: DataSourcePort,
    repository: SqlAlchemySectorMarketDataRepository,
    raw_store: S3RawPayloadStore,
    scheme: SectorScheme,
    observation_date: date,
    final_write: bool,
) -> tuple[int, int]:
    """执行一个目录分区，并把末次 activation 与控制面终态绑定到同一事务。"""
    execution = current_fenced_execution()
    if execution is None:
        raise RuntimeError("canonical executor requires an active fencing context")
    result = retain_failure_evidence(
        raw_store,
        lambda: asyncio.run(
            SectorCatalogSyncService(
                source=FailureEvidenceDataSource(provider, raw_store),
                repository=repository,
                raw_payload_store=raw_store,
            ).sync(
                scheme=scheme,
                observation_date=observation_date,
                before_final_publication=execution.arm_terminal_write if final_write else None,
            )
        ),
    )
    return result.inserted_count, result.unchanged_count


def _sync_sector_eod_snapshot(
    *,
    container: ServiceContainer,
    provider: DataSourcePort,
    repository: SqlAlchemySectorEodRepository,
    raw_store: S3RawPayloadStore,
    scheme: SectorScheme,
    trade_date: date,
    final_write: bool,
) -> None:
    """重新抓取一个完整 EOD 分区，最终 publish 前才武装控制面终态。

    成功 raw 只保留摘要和不可回放标记；恢复不会尝试读取旧 payload，而是使用同一冻结
    provider 重新抓取当前可观察快照。
    """
    execution = current_fenced_execution()
    if execution is None:
        raise RuntimeError("canonical executor requires an active fencing context")
    service = SectorEodSnapshotSyncService(
        source=FailureEvidenceDataSource(provider, raw_store),
        repository=repository,
        raw_payload_store=raw_store,
        trading_calendar=container.trading_calendar,
    )
    retain_failure_evidence(
        raw_store,
        lambda: asyncio.run(
            service.sync(
                scheme=scheme,
                trade_date=trade_date,
                source_cutoff_at=sector_eod_source_cutoff_at(trade_date),
                execution_mode=SectorEodExecutionMode.PUBLISH,
                before_final_publication=execution.arm_terminal_write if final_write else None,
            )
        ),
    )


def _sync_sector_sw_taxonomy(
    *,
    provider: DataSourcePort,
    repository: SqlAlchemySwSectorRepository,
    raw_store: S3RawPayloadStore,
    snapshot_date: date,
) -> SwSnapshotSyncResult:
    """执行申万完整快照，并把 taxonomy/估值双发布与 run 终态合并提交。"""
    execution = current_fenced_execution()
    if execution is None:
        raise RuntimeError("canonical executor requires an active fencing context")
    return retain_failure_evidence(
        raw_store,
        lambda: asyncio.run(
            SwSnapshotSyncService(
                source=FailureEvidenceDataSource(provider, raw_store),
                repository=repository,
                raw_payload_store=raw_store,
            ).sync(
                snapshot_date=snapshot_date,
                before_final_publication=execution.arm_terminal_write,
            )
        ),
    )


def _execute_equity_bar(
    claim: ExecutionClaim,
    *,
    container: ServiceContainer,
    period: EquityBarPeriod,
) -> ExecutionOutcome:
    """串行同步个股行情；回填按不可变分区水位跳过成功窗口并从完整 seal 收敛。"""
    selector = claim.target["selector"]
    repository = SqlAlchemyEquityMarketDataRepository(container.database)
    identifiers = tuple(_equity_identifiers(selector, repository))
    if not identifiers:
        return ExecutionOutcome(status="SUCCEEDED", completed_partitions=0, total_partitions=0)
    start, end = _equity_backfill_window(claim) or _window_for_target(claim.target)
    windows = _bounded_windows(start, end, 366)
    is_backfill = (
        claim.execution_intent is not None
        and claim.execution_intent.get("kind") == "EQUITY_BACKFILL"
    )
    partition_roster = tuple(
        (
            identifier,
            window_from,
            window_to,
            equity_backfill_partition_key(
                dataset_code=claim.dataset_code,
                exchange=identifier.exchange.value,
                symbol=identifier.symbol,
                window_from=window_from,
                window_to=window_to,
            ),
        )
        for identifier in identifiers
        for window_from, window_to in windows
    )
    completed_keys = (
        completed_equity_bar_partitions(
            container.database,
            claim=claim,
            expected_partition_keys=frozenset(item[3] for item in partition_roster),
        )
        if is_backfill
        else frozenset()
    )
    raw_store = S3RawPayloadStore(container.object_storage)
    inserted = 0
    unchanged = 0
    completed = len(completed_keys)
    data_version: UUID | None = None
    coverage_version: UUID | None = None
    total_partitions = len(partition_roster)
    execution = _required_execution()
    for partition_index, (
        identifier,
        window_from,
        window_to,
        partition_key,
    ) in enumerate(partition_roster):
        if partition_key in completed_keys:
            continue
        if _cancel_requested(container):
            return ExecutionOutcome(
                status="PARTIAL" if completed else "CANCELLED",
                completed_partitions=completed,
                total_partitions=total_partitions,
                processed_records=inserted + unchanged,
            )
        source_ids_before = frozenset(execution.source_batch_ids)
        result = _sync_equity_bar(
            container=container,
            repository=repository,
            raw_store=raw_store,
            identifier=identifier,
            period=period,
            start=window_from,
            end=window_to,
            # 回填在全部分区 checkpoint seal 后显式终结；普通任务仍与末次发布同事务。
            final_write=not is_backfill and partition_index == total_partitions - 1,
            source_snapshot=claim.source_snapshot,
        )
        inserted += result.inserted_count
        unchanged += result.unchanged_count
        data_version = result.data_version
        coverage_version = result.coverage_version
        if is_backfill:
            window_source_ids = (set(execution.source_batch_ids) - source_ids_before) | {
                result.source_batch_id
            }
            record_equity_bar_partition(
                container.database,
                claim=claim,
                execution=execution,
                partition_key=partition_key,
                window_from=window_from,
                window_to=window_to,
                coverage_version=result.coverage_version,
                data_version=result.data_version,
                source_batch_ids=tuple(window_source_ids),
                publication_kind=result.publication_kind,
                record_count=result.inserted_count + result.unchanged_count,
            )
        completed += 1
    if is_backfill:
        finalize_equity_bar_partitions(
            container.database,
            claim=claim,
            execution=execution,
            ordered_partition_keys=tuple(item[3] for item in partition_roster),
        )
    return ExecutionOutcome(
        status="SUCCEEDED",
        completed_partitions=completed,
        total_partitions=total_partitions,
        processed_records=inserted + unchanged,
        checkpoint_kind=(
            "bar-coverage-version"
            if coverage_version is not None
            else "data-version"
            if data_version is not None
            else None
        ),
        checkpoint_position=(
            str(coverage_version)
            if coverage_version is not None
            else None
            if data_version is None
            else str(data_version)
        ),
    )


def _cancel_requested(container: ServiceContainer) -> bool:
    """在每个 GLOBAL 分区前读取当前 run 取消状态，避免继续发起不需要的 Provider 请求。"""
    execution = current_fenced_execution()
    if execution is None:
        raise RuntimeError("canonical executor requires an active fencing context")
    with container.database.session() as session:
        return execution.is_cancel_requested(session)


def _etf_partition(identifier: EtfIdentifier) -> str:
    """生成不依赖代码前缀推断、直接绑定 canonical ETF 身份的稳定分区键。"""
    return f"etf:{identifier.qualified_key}"


def _stock_connect_partition(
    *,
    channel: StockConnectChannel,
    trade_date: date,
) -> str:
    """用业务日、通道和方向生成冻结日包分区键，恢复时不得重新下载成功前缀。"""
    return f"stock-connect:{trade_date.isoformat()}:{channel.channel}:{channel.direction}"


def _stock_connect_manifest_reference(
    intent: dict[str, Any] | None,
) -> tuple[UUID, str, int, int]:
    """解析只含 immutable header 身份和计数的执行意图，拒绝夹带页面正文。"""
    if not isinstance(intent, dict) or set(intent) != {"stockConnectDeliveryManifestRef"}:
        raise ValueError("stock-connect delivery manifest reference is missing")
    raw = intent.get("stockConnectDeliveryManifestRef")
    if not isinstance(raw, dict) or set(raw) != {
        "manifestId",
        "rootHash",
        "targetCount",
        "pageCount",
    }:
        raise ValueError("stock-connect delivery manifest reference is invalid")
    manifest_id = raw.get("manifestId")
    root_hash = raw.get("rootHash")
    target_count = raw.get("targetCount")
    page_count = raw.get("pageCount")
    if (
        not isinstance(manifest_id, str)
        or not isinstance(root_hash, str)
        or len(root_hash) != 64
        or not isinstance(target_count, int)
        or isinstance(target_count, bool)
        or target_count < 1
        or not isinstance(page_count, int)
        or isinstance(page_count, bool)
        or page_count < 1
    ):
        raise ValueError("stock-connect delivery manifest reference value is invalid")
    return UUID(manifest_id), root_hash, target_count, page_count


def _stock_connect_pending_page(
    descriptors: Sequence[DeliveryManifestPageDescriptor],
    *,
    completed_partitions: int,
    expected_target_count: int,
    expected_page_count: int,
) -> DeliveryManifestPageDescriptor:
    """按顺序完成水位选择唯一待处理页，不读取此前已完成页面正文。"""
    if (
        len(descriptors) != expected_page_count
        or sum(item.target_count for item in descriptors) != expected_target_count
        or completed_partitions < 0
        or completed_partitions >= expected_target_count
    ):
        raise DeliveryManifestIntegrityError(
            "stock-connect delivery manifest page directory differs"
        )
    cumulative = 0
    for descriptor in descriptors:
        cumulative += descriptor.target_count
        if completed_partitions < cumulative:
            return descriptor
    raise DeliveryManifestIntegrityError("stock-connect delivery manifest has no pending page")


def _stock_connect_next_batch(
    tasks: Sequence[tuple[StockConnectChannel, date]],
    *,
    succeeded: frozenset[str],
) -> tuple[list[tuple[StockConnectChannel, date]], bool]:
    """跳过已成功日包，并选取不会拆散同一业务日的下一个公平批次。"""
    pending = [
        (channel, trade_date)
        for channel, trade_date in tasks
        if _stock_connect_partition(channel=channel, trade_date=trade_date) not in succeeded
    ]
    if not pending:
        return [], False
    batch_dates = sorted(
        {trade_date for _channel, trade_date in pending},
        reverse=True,
    )[:_STOCK_CONNECT_EXECUTION_BATCH_DAYS]
    selected_dates = set(batch_dates)
    batch = [
        (channel, trade_date) for channel, trade_date in pending if trade_date in selected_dates
    ]
    return batch, len(batch) < len(pending)


def _prepare_etf_operation_partitions(
    container: ServiceContainer,
    *,
    run_id: UUID,
    identifiers: tuple[EtfIdentifier, ...],
) -> frozenset[str]:
    """持久化冻结全集分区并返回已成功水位；额外分区代表 target 被篡改而拒绝。"""
    return _prepare_operation_partitions(
        container,
        run_id=run_id,
        partition_keys=frozenset(_etf_partition(identifier) for identifier in identifiers),
        subject="ETF universe",
    )


def _prepare_operation_partitions(
    container: ServiceContainer,
    *,
    run_id: UUID,
    partition_keys: frozenset[str],
    subject: str,
    allowed_existing_prefix: str | None = None,
    expected_total_partitions: int | None = None,
) -> frozenset[str]:
    """持久化当前页分区并校验恢复水位，分页任务只允许同 manifest 的既有前缀。"""
    with container.database.transaction() as session:
        run = session.get(DataOperationRun, run_id)
        if run is None:
            raise RuntimeError("data operation run is unavailable")
        existing = {
            partition.partition_key: partition
            for partition in session.scalars(
                select(DataOperationPartition).where(DataOperationPartition.run_id == run_id)
            ).all()
            if partition.partition_key != "default"
        }
        unexpected = set(existing) - partition_keys
        if allowed_existing_prefix is not None:
            unexpected = {key for key in unexpected if not key.startswith(allowed_existing_prefix)}
        if unexpected:
            raise RuntimeError(f"operation partitions do not match frozen {subject}")
        for partition_key in sorted(partition_keys - set(existing)):
            session.add(
                DataOperationPartition(
                    run_id=run_id,
                    partition_key=partition_key,
                    status="QUEUED",
                    attempt=0,
                    checkpoint_hash=None,
                    checkpoint_kind=None,
                    checkpoint_updated_at=None,
                    error_json=None,
                )
            )
        succeeded = frozenset(
            partition_key
            for partition_key, partition in existing.items()
            if partition_key in partition_keys
            if partition.status in {"SUCCEEDED", "SKIPPED"}
        )
        total_partitions = (
            len(partition_keys) if expected_total_partitions is None else expected_total_partitions
        )
        if total_partitions < len(partition_keys):
            raise RuntimeError(f"operation partition total is invalid for {subject}")
        run.total_partitions = total_partitions
        run.completed_partitions = sum(
            1 for partition in existing.values() if partition.status in {"SUCCEEDED", "SKIPPED"}
        )
        return succeeded


def _etf_partition_counts(
    container: ServiceContainer,
    *,
    run_id: UUID,
) -> tuple[int, int]:
    """从持久化分区而非循环索引读取成功、失败计数，供取消和恢复后准确汇总。"""
    return _operation_partition_counts(container, run_id=run_id, prefix="etf:")


def _operation_partition_counts(
    container: ServiceContainer,
    *,
    run_id: UUID,
    prefix: str,
) -> tuple[int, int]:
    """按受控分区前缀汇总成功和失败水位，避免默认终态分区污染长批次进度。"""
    with container.database.session() as session:
        statuses = session.scalars(
            select(DataOperationPartition.status).where(
                DataOperationPartition.run_id == run_id,
                DataOperationPartition.partition_key.startswith(prefix),
            )
        ).all()
    return statuses.count("SUCCEEDED") + statuses.count("SKIPPED"), statuses.count("FAILED")


def _frozen_share_capital_roster(claim: ExecutionClaim) -> tuple[_FrozenEquityIdentity, ...]:
    """严格解析受理时冻结的股本身份名单，并拒绝缺失、篡改或代码重复。"""
    intent = claim.execution_intent
    if not isinstance(intent, dict):
        raise ValueError("share capital execution intent is missing")
    raw_roster = intent.get("equityInstrumentRoster")
    roster_hash = intent.get("equityInstrumentRosterHash")
    if (
        not isinstance(raw_roster, list)
        or len(raw_roster) > 100_000
        or not isinstance(roster_hash, str)
        or len(roster_hash) != 64
    ):
        raise ValueError("share capital execution roster is invalid")
    calculated_hash = hashlib.sha256(
        json.dumps(
            raw_roster,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).hexdigest()
    if calculated_hash != roster_hash:
        raise ValueError("share capital execution roster hash does not match")
    parsed: list[_FrozenEquityIdentity] = []
    for raw in raw_roster:
        if not isinstance(raw, dict) or set(raw) != {
            "instrumentId",
            "exchange",
            "symbol",
            "identityAsOf",
        }:
            raise ValueError("share capital execution roster item is invalid")
        try:
            parsed.append(
                _FrozenEquityIdentity(
                    instrument_id=UUID(str(raw["instrumentId"])),
                    identifier=EquityIdentifier.parse(f"{raw['exchange']}.{raw['symbol']}"),
                    identity_as_of=date.fromisoformat(str(raw["identityAsOf"])),
                )
            )
        except (TypeError, ValueError) as error:
            raise ValueError("share capital execution roster item is invalid") from error
    expected_order = sorted(
        parsed,
        key=lambda item: (
            item.identifier.exchange.value,
            item.identifier.symbol,
            str(item.instrument_id),
        ),
    )
    if parsed != expected_order:
        raise ValueError("share capital execution roster order is invalid")
    if len({item.instrument_id for item in parsed}) != len(parsed) or len(
        {item.identifier for item in parsed}
    ) != len(parsed):
        raise ValueError("share capital execution roster contains duplicate identity")
    selector = claim.target.get("selector")
    if not isinstance(selector, dict):
        raise ValueError("share capital selector is invalid")
    if selector.get("kind") == "INSTRUMENT":
        if len(parsed) != 1 or parsed[0].identifier != EquityIdentifier.parse(
            f"{selector.get('exchange')}.{selector.get('symbol')}"
        ):
            raise ValueError("share capital roster does not match instrument selector")
    elif selector.get("kind") != "GLOBAL":
        raise ValueError("share capital requires GLOBAL or INSTRUMENT selector")
    return tuple(parsed)


def _frozen_equity_identity_is_current(
    container: ServiceContainer, *, identity: _FrozenEquityIdentity
) -> bool:
    """确认冻结代码仍开放绑定同一永久身份，代码复用后禁止调用当前代码型 Provider。"""
    with container.database.session() as session:
        values = session.execute(
            select(EquityInstrument.instrument_id)
            .join(
                EquityIdentifierVersion,
                EquityIdentifierVersion.security_id == EquityInstrument.security_id,
            )
            .where(
                EquityInstrument.instrument_id == identity.instrument_id,
                EquityIdentifierVersion.exchange == identity.identifier.exchange.value,
                EquityIdentifierVersion.symbol == identity.identifier.symbol,
                EquityIdentifierVersion.identity_state == "CONFIRMED",
                EquityIdentifierVersion.effective_to.is_(None),
                EquityIdentifierVersion.known_to.is_(None),
            )
        ).scalars()
        return tuple(values) == (identity.instrument_id,)


def _security_partition(instrument_id: UUID) -> str:
    """用永久证券 UUID 生成股本续跑分区，代码退市或复用不会碰撞。"""
    return f"security:{instrument_id}"


def _completed_operation_partitions(
    container: ServiceContainer, *, run_id: UUID, prefix: str
) -> frozenset[str]:
    """读取同 run 已安全完成分区，恢复尝试不得再次调用 Provider。"""
    with container.database.session() as session:
        values = session.execute(
            select(DataOperationPartition.partition_key).where(
                DataOperationPartition.run_id == run_id,
                DataOperationPartition.status == "SUCCEEDED",
                DataOperationPartition.partition_key.startswith(prefix),
            )
        ).scalars()
        return frozenset(str(value) for value in values)


def _record_operation_partition(
    container: ServiceContainer,
    *,
    run_id: UUID,
    partition_key: str,
    status: str,
    error_code: str | None,
    error_retryable: bool = False,
    error_stage: str = "PROVIDER_FETCH",
    checkpoint_evidence: dict[str, object] | None = None,
) -> None:
    """持久化分区水位；资格跳过用摘要绑定 run 内冻结版本、全集和原因。"""
    now = datetime.now(UTC)
    with container.database.transaction() as session:
        run = session.get(DataOperationRun, run_id)
        if run is None:
            raise RuntimeError("data operation run is unavailable")
        partition = session.get(
            DataOperationPartition,
            {"run_id": run_id, "partition_key": partition_key},
        )
        error = (
            None
            if error_code is None
            else {
                "code": error_code,
                "stage": error_stage,
                "retryable": error_retryable,
            }
        )
        evidence_hash = (
            None
            if checkpoint_evidence is None
            else hashlib.sha256(
                json.dumps(
                    checkpoint_evidence,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
        )
        values = {
            "status": status,
            "attempt": run.attempt,
            "checkpoint_hash": (
                hashlib.sha256(partition_key.encode()).hexdigest()
                if status == "SUCCEEDED"
                else evidence_hash
            ),
            "checkpoint_kind": (
                "canonical-partition"
                if status == "SUCCEEDED"
                else "etf-nav-eligibility"
                if evidence_hash is not None
                else None
            ),
            "checkpoint_updated_at": (
                now if status == "SUCCEEDED" or evidence_hash is not None else None
            ),
            "error_json": error,
        }
        if partition is None:
            session.add(
                DataOperationPartition(
                    run_id=run_id,
                    partition_key=partition_key,
                    **values,
                )
            )
        else:
            for key, value in values.items():
                setattr(partition, key, value)
        session.flush()
        if partition_key.startswith(
            (
                "etf:",
                "venue:",
                "stock-connect:",
                "margin:",
                "derivative:",
                "sector-bar:",
                "money-flow:",
            )
        ):
            statuses = session.scalars(
                select(DataOperationPartition.status).where(
                    DataOperationPartition.run_id == run_id,
                    DataOperationPartition.partition_key != "default",
                )
            ).all()
            run.completed_partitions = statuses.count("SUCCEEDED") + statuses.count("SKIPPED")
            run.total_partitions = max(run.total_partitions, len(statuses))


def _execute_equity_reference(
    claim: ExecutionClaim, *, container: ServiceContainer, capability: str
) -> ExecutionOutcome:
    """串行执行复权因子、公司行动或公司概况；公司行动回填逐窗持久化恢复。"""
    repository = SqlAlchemyEquityMarketDataRepository(container.database)
    identifiers = tuple(_equity_identifiers(claim.target["selector"], repository))
    if not identifiers:
        return ExecutionOutcome(status="SUCCEEDED", completed_partitions=0, total_partitions=0)
    start, end = _equity_backfill_window(claim) or _window_for_target(
        claim.target, allow_undated=capability == "equity.profile"
    )
    windows = (
        _bounded_windows(start, end, 1098)
        if capability == "equity.corporate_action"
        else ((start, end),)
    )
    raw_store = S3RawPayloadStore(container.object_storage)
    inserted = 0
    unchanged = 0
    completed = 0
    data_version: UUID | None = None
    total_partitions = len(identifiers) * len(windows)
    execution = _required_execution()
    is_event_backfill = capability == "equity.corporate_action" and _is_equity_backfill(claim)
    # 事件 coverage 只属于公司行动回填；参考数据不能借用事件 checkpoint 族。
    event_partition_roster = (
        tuple(
            (
                identifier,
                window_from,
                window_to,
                equity_backfill_event_partition_keys(
                    dataset_code=claim.dataset_code,
                    window_from=window_from,
                    window_to=window_to,
                ),
            )
            for identifier in identifiers
            for window_from, window_to in windows
        )
        if is_event_backfill
        else ()
    )
    expected_event_keys = frozenset(
        partition_key
        for _identifier, _window_from, _window_to, partition_keys in event_partition_roster
        for partition_key in partition_keys
    )
    completed_event_keys = (
        completed_equity_event_partitions(
            container.database,
            claim=claim,
            expected_partition_keys=expected_event_keys,
        )
        if is_event_backfill
        else frozenset()
    )
    for identifier_index, identifier in enumerate(identifiers):
        for window_index, (window_from, window_to) in enumerate(windows):
            partition_index = identifier_index * len(windows) + window_index
            partition_keys = (
                equity_backfill_event_partition_keys(
                    dataset_code=claim.dataset_code,
                    window_from=window_from,
                    window_to=window_to,
                )
                if is_event_backfill
                else ()
            )
            completed_in_window = set(partition_keys) & set(completed_event_keys)
            if completed_in_window:
                if completed_in_window != set(partition_keys):
                    raise RuntimeError(
                        "equity backfill corporate-action window has a partial checkpoint"
                    )
                completed += 1
                continue
            if _cancel_requested(container):
                return ExecutionOutcome(
                    status="PARTIAL" if partition_index else "CANCELLED",
                    completed_partitions=partition_index,
                    total_partitions=total_partitions,
                    processed_records=inserted + unchanged,
                )
            source_ids_before = frozenset(execution.source_batch_ids)
            result = _sync_equity_reference(
                container=container,
                repository=repository,
                raw_store=raw_store,
                identifier=identifier,
                capability=capability,
                start=window_from,
                end=window_to,
                final_write=(not is_event_backfill and partition_index == total_partitions - 1),
                source_snapshot=claim.source_snapshot,
            )
            inserted += result[0]
            unchanged += result[1]
            data_version = result[2]
            completed += 1
            if is_event_backfill:
                record_equity_event_partitions(
                    container.database,
                    claim=claim,
                    execution=execution,
                    window_from=window_from,
                    window_to=window_to,
                    source_batch_ids=tuple(set(execution.source_batch_ids) - source_ids_before),
                )
    if is_event_backfill:
        finalize_equity_event_partitions(
            container.database,
            claim=claim,
            execution=execution,
            ordered_partition_keys=tuple(
                partition_key
                for _identifier, _window_from, _window_to, partition_keys in event_partition_roster
                for partition_key in partition_keys
            ),
        )
    return ExecutionOutcome(
        status="SUCCEEDED",
        completed_partitions=completed,
        total_partitions=total_partitions,
        processed_records=inserted + unchanged,
        checkpoint_kind="data-version" if data_version is not None else None,
        checkpoint_position=None if data_version is None else str(data_version),
    )


def _execute_financial_capability(
    claim: ExecutionClaim,
    *,
    container: ServiceContainer,
    provider_capability: str,
) -> ExecutionOutcome:
    """按控制面数据集独立执行一种财务能力，不产生其他 publication 旁路写入。"""
    expected_capability = _FINANCIAL_EXECUTIONS.get(claim.dataset_code)
    if expected_capability != provider_capability:
        raise ValueError("financial control-plane capability binding is invalid")
    identity_repository = SqlAlchemyEquityMarketDataRepository(container.database)
    identifiers = tuple(_equity_identifiers(claim.target["selector"], identity_repository))
    if not identifiers:
        raise ValueError("financial target resolved no confirmed equity identity")
    provider = _frozen_provider(claim.source_snapshot, container, provider_capability)
    raw_store = S3RawPayloadStore(container.object_storage)
    repository = SqlAlchemyFinancialSyncRepository(container.database)
    inserted = 0
    unchanged = 0
    data_version: UUID | None = None
    for index, identifier in enumerate(identifiers):
        if _cancel_requested(container):
            return ExecutionOutcome(
                status="PARTIAL" if index else "CANCELLED",
                completed_partitions=index,
                total_partitions=len(identifiers),
                processed_records=inserted + unchanged,
            )
        result = _sync_financial_capability(
            repository=repository,
            raw_store=raw_store,
            identifier=identifier,
            provider=provider,
            dataset_code=claim.dataset_code,
            final_write=index == len(identifiers) - 1,
        )
        inserted += result.inserted_count
        unchanged += result.unchanged_count
        data_version = result.data_version
    return ExecutionOutcome(
        status="SUCCEEDED",
        completed_partitions=len(identifiers),
        total_partitions=len(identifiers),
        processed_records=inserted + unchanged,
        checkpoint_kind="data-version",
        checkpoint_position=None if data_version is None else str(data_version),
    )


def _execute_financial_derived_metric(
    claim: ExecutionClaim, *, container: ServiceContainer
) -> ExecutionOutcome:
    """从指定证券已发布报表生成平台指标，不调用 provider，也不接受隐式全市场扇出。"""
    mode = claim.target.get("mode")
    if mode not in {"FULL", "INCREMENTAL"}:
        raise ValueError("financial derived target mode is unsupported")
    selector = claim.target.get("selector")
    if not isinstance(selector, dict) or selector.get("kind") != "INSTRUMENT":
        raise ValueError("financial derived metric requires an instrument selector")
    exchange = selector.get("exchange")
    symbol = selector.get("symbol")
    if not isinstance(exchange, str) or not isinstance(symbol, str):
        raise ValueError("financial derived instrument selector is invalid")
    identifier = EquityIdentifier.parse(f"{exchange}.{symbol}")
    if _cancel_requested(container):
        return ExecutionOutcome(status="CANCELLED", completed_partitions=0, total_partitions=1)
    execution = _required_execution()
    result = run_financial_derivation(
        database=container.database,
        exchange=identifier.exchange,
        symbol=identifier.symbol,
        mode="backfill" if mode == "FULL" else "manual",
        request_key=f"data-operation:{claim.run_id}:{identifier.qualified_symbol}",
        run_id=claim.run_id,
        before_final_publication=execution.arm_terminal_write,
    )
    return ExecutionOutcome(
        status="SUCCEEDED",
        completed_partitions=1,
        total_partitions=1,
        processed_records=result.publication.row_count,
        checkpoint_kind="data-version",
        checkpoint_position=str(result.publication.data_version),
    )


def _event_window(target: dict[str, object]) -> tuple[date, date]:
    """把事件模式限制为 31 天显式回填或固定 31 天增量纠错窗。"""
    mode = target.get("mode")
    if mode == "DATE_RANGE":
        start_value = target.get("dateFrom")
        end_value = target.get("dateTo")
        if not isinstance(start_value, str) or not isinstance(end_value, str):
            raise ValueError("event date range target is invalid")
        start, end = date.fromisoformat(start_value), date.fromisoformat(end_value)
    elif mode == "INCREMENTAL":
        end = datetime.now(_SHANGHAI).date()
        start = end - timedelta(days=30)
    else:
        raise ValueError("event target mode is unsupported")
    if start > end or (end - start).days + 1 > 31:
        raise ValueError("event target window must contain at most 31 days")
    return start, end


def _event_identifier(selector: object, *, allow_global: bool) -> EquityIdentifier | None:
    """解析事件 selector；全市场交易事件仅由受控 operation selector 表达。"""
    if not isinstance(selector, dict):
        raise ValueError("event selector is invalid")
    kind = selector.get("kind")
    if kind == "INSTRUMENT":
        exchange = selector.get("exchange")
        symbol = selector.get("symbol")
        if not isinstance(exchange, str) or not isinstance(symbol, str):
            raise ValueError("event instrument selector is invalid")
        return EquityIdentifier.parse(f"{exchange}.{symbol}")
    if (kind == "GLOBAL" and allow_global) or (kind == "TRADING_EVENT" and not allow_global):
        return None
    raise ValueError("event selector does not match dataset")


def _corporate_events_repository(
    container: ServiceContainer, *, provider_id: str
) -> SqlAlchemyCorporateEventsRepository:
    """构造只允许现有 AKShare 个人内部研究策略的业绩事件仓储。"""
    if provider_id != "akshare":
        raise ProviderError(
            ProviderErrorCode.UNAVAILABLE,
            "corporate events source approval is unavailable",
            retryable=False,
        )
    approval = CorporateSourceApproval(
        provider_id=provider_id,
        source_code="akshare",
        legal_name="AKShare",
        source_kind="community_aggregator",
        rights_status="personal_internal_research",
        license_scope="internal_research_no_redistribution",
    )
    return SqlAlchemyCorporateEventsRepository(
        container.database,
        approved_sources={provider_id: approval},
    )


def _trading_events_repository(
    container: ServiceContainer, *, provider_id: str
) -> SqlAlchemyTradingEventsRepository:
    """构造只允许现有 AKShare 个人内部研究策略的交易披露仓储。"""
    if provider_id != "akshare":
        raise ProviderError(
            ProviderErrorCode.UNAVAILABLE,
            "trading events source approval is unavailable",
            retryable=False,
        )
    approval = TradingEventsSourceApproval(
        provider_id=provider_id,
        source_code="akshare",
        legal_name="AKShare",
        source_kind="community_aggregator",
        rights_status="personal_internal_research",
        license_scope="internal_research_no_redistribution",
    )
    return SqlAlchemyTradingEventsRepository(
        container.database,
        approved_sources={provider_id: approval},
    )


def _margin_repository(
    container: ServiceContainer,
    *,
    provider_id: str,
) -> SqlAlchemyMarginMarketDataRepository:
    """构造仅允许已审核 AKShare 聚合来源的两融发布仓储，其他 provider 必须失败关闭。"""
    if provider_id != "akshare":
        raise ProviderError(
            ProviderErrorCode.UNAVAILABLE,
            "margin source approval is unavailable",
            retryable=False,
        )
    approval = MarginSourceApproval(
        provider_id=provider_id,
        source_code="akshare_reviewed_public_market",
        legal_name="AKShare 聚合的公开市场来源",
        source_kind="aggregator",
        rights_status="internal",
        license_scope="internal_research_no_redistribution",
    )
    return SqlAlchemyMarginMarketDataRepository(
        container.database,
        approved_sources={provider_id: approval},
    )


def _derivative_repository(
    container: ServiceContainer,
    *,
    provider_id: str,
) -> SqlAlchemyDerivativeDailyBarRepository:
    """构造仅允许已审核 AKShare 聚合来源的真实合约日线发布仓储。"""
    if provider_id != "akshare":
        raise ProviderError(
            ProviderErrorCode.UNAVAILABLE,
            "derivative source approval is unavailable",
            retryable=False,
        )
    approval = DerivativeSourceApproval(
        provider_id=provider_id,
        source_code="akshare_reviewed_public_market",
        legal_name="AKShare 聚合的公开市场来源",
        source_kind="aggregator",
        rights_status="internal",
        license_scope="internal_research_no_redistribution",
    )
    return SqlAlchemyDerivativeDailyBarRepository(
        container.database,
        approved_sources={provider_id: approval},
    )


def _akshare_batched_window(
    claim: ExecutionClaim,
    *,
    dataset_code: str,
    batch_days: int,
) -> tuple[date, date]:
    """读取受理时冻结的日期窗口；仅旧兼容 run 才按其 target 解析当前锚点。"""
    intent = claim.execution_intent
    if isinstance(intent, dict) and any(
        key in intent
        for key in (
            "akshareResolvedDateFrom",
            "akshareResolvedDateTo",
            "akshareExecutionBatchDays",
        )
    ):
        start_text = intent.get("akshareResolvedDateFrom")
        end_text = intent.get("akshareResolvedDateTo")
        frozen_batch_days = intent.get("akshareExecutionBatchDays")
        if (
            not isinstance(start_text, str)
            or not isinstance(end_text, str)
            or frozen_batch_days != batch_days
        ):
            raise ValueError("AKShare batched execution intent is invalid")
        start, end = date.fromisoformat(start_text), date.fromisoformat(end_text)
    else:
        start, end = _akshare_batched_target_window(claim.target)
    if start > end or dataset_code not in {
        *_MARGIN_EXECUTIONS,
        _STOCK_CONNECT_RESEARCH_DATASET,
        _DERIVATIVE_DATASET,
        *_SECTOR_BAR_PERIODS,
    }:
        raise ValueError("AKShare batched execution window is invalid")
    return start, end


def _akshare_batched_target_window(target: dict[str, object]) -> tuple[date, date]:
    """兼容旧系统命令的目标日期解析；新命令始终走冻结 intent 防止跨日漂移。"""
    mode = target.get("mode")
    anchor = datetime.now(_SHANGHAI).date()
    if mode == "FULL":
        return _HISTORY_START, anchor
    if mode == "INCREMENTAL":
        return anchor - timedelta(days=31), anchor
    if mode == "DATE_RANGE":
        start_text = target.get("dateFrom")
        end_text = target.get("dateTo")
        if isinstance(start_text, str) and isinstance(end_text, str):
            return date.fromisoformat(start_text), date.fromisoformat(end_text)
    if mode == "OBSERVATION_DATE":
        observation = target.get("observationDate")
        if isinstance(observation, str):
            resolved = date.fromisoformat(observation)
            return resolved, resolved
    raise ValueError("AKShare batched target mode is unsupported")


def _margin_partition(
    *,
    operation: str,
    venue: MarginVenue,
    window: tuple[date, date],
) -> str:
    """构造两融可恢复日期分区键，操作、场所和包含端窗口必须全部参与身份。"""
    window_from, window_to = window
    return f"margin:{operation}:{venue.code}:{window_from.isoformat()}:{window_to.isoformat()}"


def _index_shadow_partition(
    *,
    administrator: IndexAdministrator,
    capability: IndexCapability,
    index_code: str | None,
) -> str:
    """构造管理人、能力和可空目录身份均参与的研究快照恢复分区。"""
    scope = "catalog" if index_code is None else index_code
    return f"index:{administrator.value}:{capability.value}:{scope}"


def _stock_connect_research_partition(
    *,
    channel: StockConnectChannel,
    window: tuple[date, date],
) -> str:
    """构造 AKShare 港通研究通道方向和包含端日期窗的可恢复分区。"""
    window_from, window_to = window
    return (
        f"stock-connect-research:{channel.channel}_{channel.direction}:"
        f"{window_from.isoformat()}:{window_to.isoformat()}"
    )


def _derivative_partition(
    *,
    contract: DerivativeContractIdentifier,
    window: tuple[date, date],
) -> str:
    """构造真实合约日线的可恢复分区键，绝不以连续合约或产品简称替代身份。"""
    window_from, window_to = window
    return f"derivative:{contract.qualified_key}:{window_from.isoformat()}:{window_to.isoformat()}"


def _sector_bar_partition(
    *,
    period: SectorPeriod,
    identity: _FrozenSectorBarIdentity,
    window: tuple[date, date],
) -> str:
    """按冻结主键、原生周期和一日窗口生成可恢复板块 K 线分区。"""
    window_from, window_to = window
    return (
        f"sector-bar:{period.value}:{identity.sector_key}:"
        f"{window_from.isoformat()}:{window_to.isoformat()}"
    )


def _frozen_sector_bar_roster(claim: ExecutionClaim) -> tuple[_FrozenSectorBarIdentity, ...]:
    """解析并复核预检冻结的东财目录，不回读后来新增的板块。"""
    intent = claim.execution_intent
    required_keys = {
        "sectorBarRoster",
        "sectorBarRosterHash",
        "akshareResolvedDateFrom",
        "akshareResolvedDateTo",
        "akshareExecutionBatchDays",
    }
    if not isinstance(intent, dict) or set(intent) != required_keys:
        raise ValueError("sector bar execution intent is invalid")
    roster_raw = intent.get("sectorBarRoster")
    roster_hash = intent.get("sectorBarRosterHash")
    if not isinstance(roster_raw, list) or not isinstance(roster_hash, str):
        raise ValueError("sector bar frozen roster is invalid")
    calculated_hash = hashlib.sha256(
        json.dumps(
            roster_raw,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).hexdigest()
    if calculated_hash != roster_hash:
        raise ValueError("sector bar frozen roster hash does not match")
    roster: list[_FrozenSectorBarIdentity] = []
    for item in roster_raw:
        if not isinstance(item, dict) or set(item) != {"sectorKey", "scheme", "sectorCode"}:
            raise ValueError("sector bar frozen roster item is invalid")
        sector_key = item.get("sectorKey")
        scheme = item.get("scheme")
        sector_code = item.get("sectorCode")
        if (
            not isinstance(sector_key, str)
            or not sector_key.isdigit()
            or int(sector_key) < 1
            or not isinstance(scheme, str)
            or not isinstance(sector_code, str)
        ):
            raise ValueError("sector bar frozen roster value is invalid")
        try:
            identifier = SectorIdentifier(SectorScheme(scheme), sector_code)
        except ValueError as error:
            raise ValueError("sector bar frozen roster identifier is invalid") from error
        roster.append(_FrozenSectorBarIdentity(sector_key=int(sector_key), identifier=identifier))
    if not roster:
        raise ValueError("sector bar frozen roster must not be empty")
    if len({item.sector_key for item in roster}) != len(roster) or len(
        {item.identifier.qualified_key for item in roster}
    ) != len(roster):
        raise ValueError("sector bar frozen roster contains duplicate identities")
    selector = claim.target.get("selector")
    if not isinstance(selector, dict):
        raise ValueError("sector bar target selector is invalid")
    selector_kind = selector.get("kind")
    if selector_kind == "GLOBAL":
        return tuple(roster)
    if selector_kind == "SCHEME" and isinstance(selector.get("scheme"), str):
        if all(item.identifier.scheme.value == selector["scheme"] for item in roster):
            return tuple(roster)
    if (
        selector_kind == "SECTOR"
        and isinstance(selector.get("scheme"), str)
        and isinstance(selector.get("sectorCode"), str)
    ):
        if len(roster) == 1 and (
            roster[0].identifier.scheme.value == selector["scheme"]
            and roster[0].identifier.code == selector["sectorCode"]
        ):
            return tuple(roster)
    raise ValueError("sector bar frozen roster does not match target selector")


def _assert_frozen_sector_bar_identity(
    container: ServiceContainer,
    *,
    identity: _FrozenSectorBarIdentity,
) -> None:
    """确认冻结主键仍指向同一板块代码，防止目录修订后代码错配写入。"""
    with container.database.session() as session:
        row = (
            session.execute(
                select(SectorEntity.scheme, SectorEntity.sector_code).where(
                    SectorEntity.sector_key == identity.sector_key
                )
            )
            .mappings()
            .one_or_none()
        )
    if (
        row is None
        or str(row["scheme"]) != identity.identifier.scheme.value
        or str(row["sector_code"]) != identity.identifier.code
    ):
        raise ValueError("sector bar frozen identity is no longer available")


def _money_flow_partition(
    *,
    claim: ExecutionClaim,
    capability: str,
    selector: Mapping[str, object],
) -> str:
    """对冻结方法学目标取摘要，避免暴露名称同时隔离每日与排行观察。"""
    descriptor = {
        "datasetCode": claim.dataset_code,
        "capability": capability,
        "selector": selector,
        "target": claim.target,
    }
    digest = hashlib.sha256(
        json.dumps(
            descriptor,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).hexdigest()
    return f"money-flow:{capability}:{digest}"


def _money_flow_parameters(
    *,
    claim: ExecutionClaim,
    container: ServiceContainer,
    capability: str,
    selector: Mapping[str, object],
) -> tuple[tuple[str, str], ...]:
    """把受控 selector 映射为唯一 adapter 参数，禁止透传供应商自由参数。"""
    if capability != money_flow_source_capability(claim.dataset_code, selector):
        raise ValueError("money-flow capability does not match frozen selector")
    scope = selector.get("scope")
    if claim.dataset_code == _MONEY_FLOW_DAILY_DATASET:
        if selector.get("operation") != "DAILY":
            raise ValueError("money-flow daily selector operation is invalid")
        if scope == "EQUITY":
            exchange = selector.get("exchange")
            symbol = selector.get("symbol")
            if not isinstance(exchange, str) or not isinstance(symbol, str):
                raise ValueError("money-flow daily equity selector is invalid")
            return (("exchange", exchange), ("symbol", symbol))
        if scope == "SECTOR":
            scheme = selector.get("scheme")
            sector_code = selector.get("sectorCode")
            if (
                not isinstance(scheme, str)
                or scheme != "eastmoney.industry"
                or not isinstance(sector_code, str)
            ):
                raise ValueError("money-flow daily sector selector is invalid")
            sector_name = _active_sector_name(
                container,
                scheme=scheme,
                sector_code=sector_code,
            )
            return (("scheme", scheme), ("sectorName", sector_name))
        if scope == "MARKET":
            return (("marketCode", "cn-a"),)
        raise ValueError("money-flow daily scope is invalid")
    if claim.dataset_code != _MONEY_FLOW_RANKING_DATASET or selector.get("operation") != "RANKING":
        raise ValueError("money-flow ranking selector operation is invalid")
    observation = claim.target.get("observationDate")
    if not isinstance(observation, str):
        raise ValueError("money-flow ranking observation date is invalid")
    observation_date = date.fromisoformat(observation)
    if observation_date != datetime.now(_SHANGHAI).date():
        # SDK 没有历史日期参数；跨日排队的旧 run 必须失败而非把今日页面标成昨日。
        raise ValueError("money-flow ranking observation date is no longer current")
    methodology = selector.get("methodology")
    window = selector.get("window")
    if not isinstance(window, str):
        raise ValueError("money-flow ranking window is invalid")
    if methodology == "EASTMONEY_ORDER_SIZE":
        indicator = {
            "TODAY": "今日",
            "DAY_3": "3日",
            "DAY_5": "5日",
            "DAY_10": "10日",
        }.get(window)
        if indicator is None or scope not in {"EQUITY", "SECTOR"}:
            raise ValueError("EastMoney money-flow ranking selector is invalid")
        values: list[tuple[str, str]] = [("targetDate", observation), ("indicator", indicator)]
        if scope == "SECTOR":
            sector_type = selector.get("sectorType")
            source_sector_type = {
                "INDUSTRY": "行业资金流",
                "CONCEPT": "概念资金流",
                "REGION": "地域资金流",
            }.get(str(sector_type))
            if source_sector_type is None:
                raise ValueError("EastMoney money-flow ranking sector type is invalid")
            values.append(("sectorType", source_sector_type))
        return tuple(values)
    if methodology == "THS_TRADE_DIRECTION":
        indicator = {
            "INTRADAY": "即时",
            "DAY_3": "3日排行",
            "DAY_5": "5日排行",
            "DAY_10": "10日排行",
            "DAY_20": "20日排行",
        }.get(window)
        if indicator is None or scope not in {"EQUITY", "INDUSTRY", "CONCEPT"}:
            raise ValueError("THS money-flow ranking selector is invalid")
        return (("targetDate", observation), ("indicator", indicator))
    raise ValueError("money-flow ranking methodology is invalid")


def _active_sector_name(
    container: ServiceContainer,
    *,
    scheme: str,
    sector_code: str,
) -> str:
    """读取已激活的目录名称；东财接口所需名称绝不能由代码或猜测替代。"""
    with container.database.session() as session:
        name = session.execute(
            select(SectorEntity.name).where(
                SectorEntity.scheme == scheme,
                SectorEntity.sector_code == sector_code,
                SectorEntity.status == "ACTIVE",
            )
        ).scalar_one_or_none()
    if not isinstance(name, str) or not name.strip():
        raise ValueError("money-flow sector name is unavailable from active catalog")
    return name.strip()


def _p0_partition_error_code(reason_code: str) -> str:
    """将稳定 provider 原因压缩为分区账本错误码，避免把响应正文写入运维记录。"""
    return reason_code.upper().replace("-", "_")[:64]


def _frozen_provider(
    source_snapshot: list[dict[str, Any]], container: ServiceContainer, capability: str
) -> DataSourcePort:
    """从 run 冻结 sourceSnapshot 取回仍可用的唯一 provider，拒绝执行时悄然换源。"""
    bindings = [
        binding
        for binding in source_snapshot
        if binding.get("sourceDataset") == capability and binding.get("effective") is True
    ]
    if len(bindings) != 1 or not isinstance(bindings[0].get("providerId"), str):
        raise ProviderError(
            ProviderErrorCode.UNAVAILABLE,
            "frozen source binding is unavailable",
            retryable=True,
        )
    provider_id = str(bindings[0]["providerId"])
    try:
        provider = container.source_registry.get(provider_id)
    except KeyError as error:
        raise ProviderError(
            ProviderErrorCode.UNAVAILABLE,
            "frozen source provider is unavailable",
            retryable=True,
        ) from error
    if capability not in provider.capabilities():
        raise ProviderError(
            ProviderErrorCode.UNAVAILABLE,
            "frozen source capability is unavailable",
            retryable=True,
        )
    return provider


def _sync_financial_capability(
    *,
    repository: SqlAlchemyFinancialSyncRepository,
    raw_store: S3RawPayloadStore,
    identifier: EquityIdentifier,
    provider: DataSourcePort,
    dataset_code: str,
    final_write: bool,
) -> FinancialPublicationResult:
    """调用单一财务用例，并让该数据集末次 publication 与控制面终态同事务提交。"""
    execution = current_fenced_execution()
    if execution is None:
        raise RuntimeError("canonical executor requires an active fencing context")
    source = FailureEvidenceDataSource(provider, raw_store)
    service = FinancialSyncService(
        source=source,
        repository=repository,
        raw_payload_store=raw_store,
    )
    before_publication = execution.arm_terminal_write if final_write else None

    def run_sync() -> FinancialPublicationResult:
        """在同步 dispatcher 线程中运行指定的单能力异步财务用例。"""
        if dataset_code == "financial.report":
            pending = service.sync_reports(
                exchange=identifier.exchange,
                symbol=identifier.symbol,
                before_publication=before_publication,
            )
        elif dataset_code == "financial.provider-metric":
            pending = service.sync_provider_metrics(
                exchange=identifier.exchange,
                symbol=identifier.symbol,
                before_publication=before_publication,
            )
        elif dataset_code == "financial.valuation":
            pending = service.sync_valuations(
                exchange=identifier.exchange,
                symbol=identifier.symbol,
                before_publication=before_publication,
            )
        else:
            raise ValueError("unsupported financial control-plane dataset")
        return asyncio.run(pending)

    return retain_failure_evidence(raw_store, run_sync)


def _equity_identifiers(
    selector: dict[str, object], repository: SqlAlchemyEquityMarketDataRepository
) -> Iterable[EquityIdentifier]:
    """从已验证 selector 解析单证券或 GLOBAL 已确认证券目录，不接收自由 Provider 参数。"""
    kind = selector.get("kind")
    if kind == "INSTRUMENT":
        exchange = selector.get("exchange")
        symbol = selector.get("symbol")
        if not isinstance(exchange, str) or not isinstance(symbol, str):
            raise ValueError("instrument selector is invalid")
        yield EquityIdentifier.parse(f"{exchange}.{symbol}")
        return
    if kind == "GLOBAL":
        for instrument in repository.list_instruments(query=None, limit=100_000):
            if instrument.listing_status != "PENDING":
                yield instrument.identifier
        return
    raise ValueError("equity bars require GLOBAL or INSTRUMENT selector")


def _equity_backfill_window(claim: ExecutionClaim) -> tuple[date, date] | None:
    """从严格私有意图读取低基数 child 的冻结内部日期范围。"""
    intent = claim.execution_intent
    if not isinstance(intent, dict) or intent.get("kind") != "EQUITY_BACKFILL":
        return None
    start_text = intent.get("backfillDateFrom")
    end_text = intent.get("backfillDateTo")
    if start_text is None and end_text is None:
        return None
    if not isinstance(start_text, str) or not isinstance(end_text, str):
        raise ValueError("equity backfill internal window is invalid")
    start = date.fromisoformat(start_text)
    end = date.fromisoformat(end_text)
    if start > end:
        raise ValueError("equity backfill internal window is reversed")
    return start, end


def _equity_backfill_reference_manifest(
    claim: ExecutionClaim,
) -> tuple[dict[str, Any], ...] | None:
    """从已 claim 的 append-only 输入中取出唯一封印引用 bundle。

    普通 discovery 刷新没有该输入，保留其当前 publication 语义；历史计划只允许控制面
    注入的一个 bundle。这里同时核对私有 intent 的版本和摘要，避免执行器因手工构造的
    `ExecutionClaim` 或错误拼接而在历史任务中静默退回 current 数据。
    """
    if not _is_equity_backfill(claim):
        return None
    intent = claim.execution_intent
    assert isinstance(intent, Mapping)
    bundles = [
        item
        for item in claim.input_manifest
        if isinstance(item, Mapping) and item.get("kind") == "REFERENCE_BUNDLE"
    ]
    if len(bundles) != 1:
        raise ValueError("equity backfill discovery requires one exact reference bundle")
    bundle = bundles[0]
    publication_id = bundle.get("publicationId")
    data_version = bundle.get("dataVersion")
    manifest_hash = bundle.get("manifestHash")
    components = bundle.get("components")
    if (
        publication_id != intent.get("referenceBundlePublicationId")
        or data_version != intent.get("referenceBundleDataVersion")
        or manifest_hash != intent.get("referenceManifestHash")
        or not isinstance(components, list)
        or not components
        or any(not isinstance(component, Mapping) for component in components)
    ):
        raise ValueError("equity backfill reference bundle does not match frozen intent")
    return tuple(dict(component) for component in components)


def _is_equity_backfill(claim: ExecutionClaim) -> bool:
    """判断当前 claim 是否来自数据库已封印的股票全量回填 child。"""
    return (
        isinstance(claim.execution_intent, dict)
        and claim.execution_intent.get("kind") == "EQUITY_BACKFILL"
    )


def _bounded_windows(start: date, end: date, maximum_days: int) -> tuple[tuple[date, date], ...]:
    """把冻结范围确定性切为包含端窗口，供单 child checkpoint 恢复。"""
    if maximum_days < 1 or start > end:
        raise ValueError("bounded window arguments are invalid")
    windows: list[tuple[date, date]] = []
    cursor = start
    while cursor <= end:
        window_end = min(end, cursor + timedelta(days=maximum_days - 1))
        windows.append((cursor, window_end))
        cursor = window_end + timedelta(days=1)
    return tuple(windows)


def _window_for_target(
    target: dict[str, object], *, allow_undated: bool = False
) -> tuple[date, date]:
    """把合同同步模式转换为既有个股用例所需的有界日期窗口。"""
    end = date.today()
    mode = target.get("mode")
    if mode == "FULL":
        return _HISTORY_START, end
    if mode == "INCREMENTAL":
        return end - timedelta(days=31), end
    if mode == "DATE_RANGE":
        start_value = target.get("dateFrom")
        end_value = target.get("dateTo")
        if not isinstance(start_value, str) or not isinstance(end_value, str):
            raise ValueError("date range target is invalid")
        return date.fromisoformat(start_value), date.fromisoformat(end_value)
    if allow_undated and mode == "FULL":
        return _HISTORY_START, end
    raise ValueError("equity target mode is unsupported")


def _sync_equity_bar(
    *,
    container: ServiceContainer,
    repository: SqlAlchemyEquityMarketDataRepository,
    raw_store: S3RawPayloadStore,
    identifier: EquityIdentifier,
    period: EquityBarPeriod,
    start: date,
    end: date,
    final_write: bool,
    source_snapshot: list[dict[str, Any]],
) -> _EquityBarPartitionResult:
    """调用 canonical 用例；真实数据与有来源证据的零记录 coverage 均为可审计成功。"""
    provider = _frozen_provider(source_snapshot, container, period.capability)
    execution = current_fenced_execution()
    if execution is None:
        raise RuntimeError("canonical executor requires an active fencing context")
    if final_write:
        execution.arm_terminal_write()
    if period is EquityBarPeriod.DAY_1:
        result = retain_failure_evidence(
            raw_store,
            lambda: asyncio.run(
                EquityDailyBarSyncService(
                    source=FailureEvidenceDataSource(provider, raw_store),
                    repository=repository,
                    raw_payload_store=raw_store,
                ).sync(identifier=identifier, start=start, end=end)
            ),
        )
    else:
        result = retain_failure_evidence(
            raw_store,
            lambda: asyncio.run(
                EquityPeriodBarSyncService(
                    source=FailureEvidenceDataSource(provider, raw_store),
                    repository=repository,
                    raw_payload_store=raw_store,
                ).sync(identifier=identifier, period=period, start=start, end=end)
            ),
        )
    if (
        result.data_version is None
        or result.coverage_version is None
        or result.source_batch_id is None
        or result.publication_kind not in {"DATA", "ZERO_RECORD_COVERAGE"}
    ):
        raise RuntimeError("equity bar window produced no exact publication coverage")
    return _EquityBarPartitionResult(
        inserted_count=result.inserted_count,
        unchanged_count=result.unchanged_count,
        data_version=result.data_version,
        coverage_version=result.coverage_version,
        source_batch_id=result.source_batch_id,
        publication_kind=result.publication_kind,
    )


def _sync_equity_reference(
    *,
    container: ServiceContainer,
    repository: SqlAlchemyEquityMarketDataRepository,
    raw_store: S3RawPayloadStore,
    identifier: EquityIdentifier,
    capability: str,
    start: date,
    end: date,
    final_write: bool,
    source_snapshot: list[dict[str, Any]],
) -> tuple[int, int, UUID | None]:
    """调用一个既有个股参考数据用例，并在最终 canonical 写事务前武装终态回调。"""
    provider = _frozen_provider(source_snapshot, container, capability)
    execution = current_fenced_execution()
    if execution is None:
        raise RuntimeError("canonical executor requires an active fencing context")
    if final_write:
        execution.arm_terminal_write()
    source = FailureEvidenceDataSource(provider, raw_store)
    if capability == "equity.adjustment_factor":
        result = retain_failure_evidence(
            raw_store,
            lambda: asyncio.run(
                EquityAdjustmentFactorSyncService(
                    source=source,
                    repository=repository,
                    raw_payload_store=raw_store,
                ).sync(identifier=identifier, start=start, end=end)
            ),
        )
    elif capability == "equity.corporate_action":
        result = retain_failure_evidence(
            raw_store,
            lambda: asyncio.run(
                EquityCorporateActionSyncService(
                    source=source,
                    repository=repository,
                    raw_payload_store=raw_store,
                ).sync(identifier=identifier, start=start, end=end)
            ),
        )
    else:
        result = retain_failure_evidence(
            raw_store,
            lambda: asyncio.run(
                EquityCompanyProfileSyncService(
                    source=source,
                    repository=repository,
                    raw_payload_store=raw_store,
                ).sync(identifier=identifier)
            ),
        )
    return result.inserted_count, result.unchanged_count, result.data_version
