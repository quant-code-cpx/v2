"""数据运维命令、运行、健康与计划的应用服务。

此服务不直接调用 Provider 或具体 adapter。它只把已审核的数据集能力转成 PostgreSQL
权威命令，并由 dispatcher 在全局槽内调用注入的执行器。这样 HTTP、CLI、Celery、计划、
重试和恢复共用同一个 command 边界，Redis 消息重复投递不会绕过 fencing。
"""

from __future__ import annotations

import base64
import hashlib
import inspect
import json
import logging
import marshal
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import Enum
from functools import partial
from pathlib import Path
from threading import Event, Thread
from typing import Any, Literal
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from service_data_sync.application.ports.data_source import (
    ProviderError,
    ProviderPreflightComponent,
    ProviderPreflightRequest,
    SourcePreflightProbePort,
    SourceStatusCoverageBoundaryPort,
)
from service_data_sync.application.ports.delivery_manifest import (
    DeliveryManifestTradeDate,
    DeliveryManifestUnavailable,
    build_immutable_delivery_manifest,
)
from service_data_sync.application.ports.trading_calendar import TradingCalendarPort
from service_data_sync.application.source_registry import SourceRegistry
from service_data_sync.bootstrap.settings import Settings
from service_data_sync.infrastructure.data_operations.equity_backfill import (
    PHASES,
    FrozenIdentity,
    FrozenReferenceBundle,
    compute_roster_hash,
)
from service_data_sync.infrastructure.data_operations.schedule_engine import (
    ScheduleCalendarUnavailableError,
    ScheduleFrequencyError,
    due_occurrences,
    next_occurrence,
    next_occurrences,
    resolve_observation_date,
    validate_frequency,
)
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.database.fenced_execution import (
    FencedExecution,
    fenced_execution,
)
from service_data_sync.infrastructure.database.models.canonical import (
    CanonicalDataset,
    CanonicalRecordLineage,
    DatasetRelease,
    QualityEvaluation,
    QualityResult,
)
from service_data_sync.infrastructure.database.models.equity.backfill import (
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
)
from service_data_sync.infrastructure.database.models.equity.identity.equity_identifier_version import (  # noqa: E501
    EquityIdentifierVersion,
)
from service_data_sync.infrastructure.database.models.equity.identity.equity_instrument import (
    EquityInstrument,
)
from service_data_sync.infrastructure.database.models.operations import (
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
from service_data_sync.infrastructure.database.models.provenance.source_batch import SourceBatch
from service_data_sync.infrastructure.database.models.publication.dataset_publication import (
    DatasetPublication,
)
from service_data_sync.infrastructure.database.models.publication.dataset_publication_component import (  # noqa: E501
    DatasetPublicationComponent,
)
from service_data_sync.infrastructure.database.models.publication.equity_bar_window_coverage import (  # noqa: E501
    EquityBarWindowCoverage,
)
from service_data_sync.infrastructure.database.models.publication.equity_event_window_coverage import (  # noqa: E501
    EquityEventWindowCoverage,
)
from service_data_sync.infrastructure.database.models.registry import ALL_MODELS
from service_data_sync.infrastructure.database.models.sector.catalog.sector_entity import (
    SectorEntity,
)
from service_data_sync.infrastructure.persistence.delivery_manifest_repository import (
    SqlAlchemyDeliveryManifestRepository,
)
from service_data_sync.infrastructure.persistence.etf_universe_repository import (
    EtfUniverseSnapshot,
    EtfUniverseUnavailable,
    load_frozen_etf_universe,
    resolve_current_etf_profile_data_versions,
)
from service_data_sync.infrastructure.persistence.event_window_coverage import (
    EquityWindowIdentityUnavailable,
)
from service_data_sync.infrastructure.persistence.stock_connect_readiness_repository import (
    SqlAlchemyStockConnectReadinessRepository,
    StockConnectReadinessProbeOutcome,
    StockConnectReadinessRepository,
    stock_connect_readiness_probe_outcome,
)
from service_data_sync.infrastructure.persistence.stock_connect_status_boundary_repository import (
    SqlAlchemyStockConnectStatusBoundaryRepository,
    StockConnectStatusBoundaryRepository,
    StockConnectStatusBoundaryViolation,
)
from service_data_sync.infrastructure.providers.official.stock_connect import (
    stock_connect_delivery_manifest_days_from_evidence,
    stock_connect_delivery_window_from_evidence,
)

_TERMINAL_RUNS = frozenset({"SUCCEEDED", "PARTIAL", "FAILED", "CANCELLED", "SKIPPED"})
_TERMINAL_COMMANDS = frozenset({"SUCCEEDED", "PARTIAL", "FAILED", "CANCELLED", "REJECTED"})
_LOGGER = logging.getLogger(__name__)
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_ETF_HISTORY_START = date(1990, 12, 19)
_AKSHARE_BATCHED_DATASETS = frozenset(
    {
        "market.margin.market.1d.reported",
        "market.margin.security.1d.reported",
        "market.margin.eligibility.reported",
        "market.stock_connect.market_stat.research",
        "derivative.bar.1d.reported",
        "sector.bar.1d.raw",
        "sector.bar.1w.raw",
        "sector.bar.1mo.raw",
    }
)
_MARGIN_EXECUTION_BATCH_DAYS = 5
_STOCK_CONNECT_RESEARCH_EXECUTION_BATCH_DAYS = 5
_DERIVATIVE_EXECUTION_BATCH_DAYS = 31
# EastMoney 板块历史接口尚未获得大窗口成功实测；先以最小日窗证实每次真实请求，
# 后续只能经新探针和显式版本调整放大，不能凭经验把失败窗口伪装成可恢复进度。
_SECTOR_BAR_EXECUTION_BATCH_DAYS = 1
_SECTOR_BAR_DATASETS = frozenset({"sector.bar.1d.raw", "sector.bar.1w.raw", "sector.bar.1mo.raw"})
_MONEY_FLOW_DAILY_DATASET = "money_flow.daily"
_MONEY_FLOW_RANKING_DATASET = "money_flow.ranking"
_STOCK_CONNECT_RESEARCH_DATASET = "market.stock_connect.market_stat.research"
_INDEX_DATASET_TARGETS: dict[str, tuple[str, str, bool]] = {
    "index.csi.catalog.snapshot": ("CSI", "index.catalog.snapshot", False),
    "index.csi.constituent.snapshot": ("CSI", "index.constituent.snapshot", True),
    "index.csi.weight.snapshot": ("CSI", "index.weight.snapshot", True),
    "index.cni.catalog.snapshot": ("CNI", "index.catalog.snapshot", False),
    "index.cni.constituent.snapshot": ("CNI", "index.constituent.snapshot", True),
    "index.cni.weight.snapshot": ("CNI", "index.weight.snapshot", True),
}
_TERMINAL_HEALTH_CHECKS = frozenset({"SUCCEEDED", "PARTIAL", "FAILED", "CANCELLED", "REJECTED"})
_LEASE_SECONDS = 60
_PREFLIGHT_TTL_SECONDS = 600
_MAX_RECOVERY_ATTEMPTS = 3
_HEALTH_RESULT_LIMIT = 500
_SCHEDULE_CALENDAR_CODE = "SSE-SZSE"
_SCHEDULE_CALENDAR_TIMEZONE = "Asia/Shanghai"
_MARKET_OVERVIEW_DATASET_CODE = "market.overview-and-sectors.bundle"
_MARKET_OVERVIEW_SCHEDULE_LOCAL_TIME = "19:20"
_EQUITY_BACKFILL_DATASETS = frozenset(
    {
        "equity.bar.1d.raw",
        "equity.bar.1w.raw",
        "equity.bar.1mo.raw",
        "equity.adjustment_factor",
        "equity.corporate_action",
        "equity.corporate_event.earnings.reported",
        "equity.dragon_tiger.disclosure.reported",
        "equity.block_trade.execution.reported",
        "equity.discovery.eod",
    }
)
_EQUITY_BACKFILL_DERIVED_DATASETS = frozenset({"equity.discovery.eod"})


def _akshare_execution_batch_days(dataset_code: str) -> int:
    """返回已审核 AKShare 同步能力的公平日期批次上限，未知数据集必须失败关闭。"""
    if dataset_code in {
        "market.margin.market.1d.reported",
        "market.margin.security.1d.reported",
        "market.margin.eligibility.reported",
    }:
        return _MARGIN_EXECUTION_BATCH_DAYS
    if dataset_code == _STOCK_CONNECT_RESEARCH_DATASET:
        return _STOCK_CONNECT_RESEARCH_EXECUTION_BATCH_DAYS
    if dataset_code == "derivative.bar.1d.reported":
        return _DERIVATIVE_EXECUTION_BATCH_DAYS
    if dataset_code in _SECTOR_BAR_DATASETS:
        return _SECTOR_BAR_EXECUTION_BATCH_DAYS
    raise ValueError("AKShare dataset is not configured for batched execution")


def _akshare_batched_partition_count(*, start: date, end: date, dataset_code: str) -> int:
    """计算冻结包含端日期窗的恢复分区数，预检和执行必须使用同一切分口径。"""
    if start > end:
        raise ValueError("AKShare date window is reversed")
    batch_days = _akshare_execution_batch_days(dataset_code)
    return ((end - start).days + batch_days) // batch_days


def money_flow_source_capability(
    dataset_code: str,
    selector: Mapping[str, object],
) -> str:
    """由已规范化资金流目标选择唯一真实 adapter capability，禁止方法学间回退。"""
    if selector.get("kind") != "MONEY_FLOW":
        raise ValueError("money-flow selector is invalid")
    scope = selector.get("scope")
    if dataset_code == _MONEY_FLOW_DAILY_DATASET:
        if selector.get("operation") != "DAILY":
            raise ValueError("money-flow daily operation is invalid")
        try:
            return {
                "EQUITY": "money_flow.order_size.daily.equity.raw",
                "SECTOR": "money_flow.order_size.daily.sector.raw",
                "MARKET": "money_flow.order_size.daily.market.raw",
            }[str(scope)]
        except KeyError as error:
            raise ValueError("money-flow daily scope is invalid") from error
    if dataset_code != _MONEY_FLOW_RANKING_DATASET or selector.get("operation") != "RANKING":
        raise ValueError("money-flow ranking operation is invalid")
    methodology = selector.get("methodology")
    if methodology == "EASTMONEY_ORDER_SIZE":
        try:
            return {
                "EQUITY": "money_flow.order_size.ranking.equity.raw",
                "SECTOR": "money_flow.order_size.ranking.sector.raw",
            }[str(scope)]
        except KeyError as error:
            raise ValueError("EastMoney money-flow ranking scope is invalid") from error
    if methodology == "THS_TRADE_DIRECTION":
        try:
            return {
                "EQUITY": "money_flow.trade_direction.ranking.equity.raw",
                "INDUSTRY": "money_flow.trade_direction.ranking.industry.raw",
                "CONCEPT": "money_flow.trade_direction.ranking.concept.raw",
            }[str(scope)]
        except KeyError as error:
            raise ValueError("THS money-flow ranking scope is invalid") from error
    raise ValueError("money-flow ranking methodology is invalid")


def _stock_connect_selected_channels(target: Mapping[str, object]) -> tuple[str, ...]:
    """从已规范化控制面目标展开公开通道代码，不依赖 provider 响应猜方向。"""
    selector = target.get("selector")
    if not isinstance(selector, Mapping):
        raise ValueError("stock-connect target selector is unavailable")
    channel_value = selector.get("channel")
    direction_value = selector.get("direction")
    if channel_value not in {"ALL", "SH", "SZ"} or direction_value not in {
        None,
        "NORTHBOUND",
        "SOUTHBOUND",
    }:
        raise ValueError("stock-connect target selector is invalid")
    channels = ("SH", "SZ") if channel_value == "ALL" else (str(channel_value),)
    directions = (
        ("NORTHBOUND", "SOUTHBOUND") if direction_value is None else (str(direction_value),)
    )
    return tuple(
        sorted(f"{channel}_{direction}" for channel in channels for direction in directions)
    )


class OperationProblem(RuntimeError):
    """表示可安全投影为内部 RFC 9457 Problem Details 的业务失败。"""

    def __init__(self, *, status: int, code: str, detail: str) -> None:
        """保存稳定 HTTP 状态、错误码和不含内部细节的说明。"""
        super().__init__(detail)
        self.status = status
        self.code = code
        self.detail = detail


class QualityGateBlocked(RuntimeError):
    """表示发布前质量门已阻断当前候选，调用方必须回滚该次 publication。"""

    def __init__(self, gate: dict[str, Any]) -> None:
        """保存可安全写入失败 run 的质量门摘要，不携带来源原文或内部证据。"""
        super().__init__("ingestion quality gate blocked publication")
        self.gate = gate


class EquityBackfillPreconditionFailed(RuntimeError):
    """表示冻结计划与实时身份、来源或 child 绑定已漂移，禁止重试执行。"""


@dataclass(frozen=True, slots=True)
class PublicationBinding:
    """表示已验证的消费者版本、immutable release 与 canonical 数据集绑定。"""

    publication: DatasetPublication
    release: DatasetRelease
    canonical_dataset: CanonicalDataset | None


@dataclass(frozen=True, slots=True)
class DatasetDefinition:
    """描述从 canonical registry、adapter 能力和配置归并出的数据集运维目录项。"""

    dataset_code: str
    display_name: str
    domain: str
    description: str
    grain: str
    capability: str | None
    modes: tuple[str, ...]
    schedule_modes: tuple[str, ...]
    source_capabilities: tuple[str, ...] = ()
    selector_kinds: tuple[str, ...] = ("GLOBAL",)
    dispatcher_ready: bool = False
    config_enabled: bool = True
    lifecycle: str = "PRODUCTION"
    model_only: bool = False
    providerless: bool = False
    max_range_days: int | None = 366
    correction_lookback_days: int = 7
    publication_dataset_code: str | None = None
    provider_id: str | None = None
    upstream_source: str | None = None
    # 来源准入元数据仅写入不可变审计快照，绝不能替代连通性、schema 或质量等技术门。
    approval_status: str = "APPROVED"
    rights_status: str | None = None
    license_scope: str | None = None
    data_as_of_kind: str = "OBSERVATION_DATE"
    data_as_of_label: str = "数据截至日"


@dataclass(frozen=True, slots=True)
class ExecutionClaim:
    """表示 worker 已在事务中取得的 run 与当前 fencing token。"""

    run_id: UUID
    dataset_code: str
    fencing_token: int
    target: dict[str, Any]
    source_snapshot: list[dict[str, Any]]
    execution_intent: dict[str, Any] | None = None
    input_manifest: tuple[dict[str, Any], ...] = ()
    attempt: int = 1


@dataclass(frozen=True, slots=True)
class ExecutionOutcome:
    """表示执行器返回的已脱敏运行结果，发布与 checkpoint 仍由控制面原子提交。"""

    status: Literal["SUCCEEDED", "PARTIAL", "FAILED", "CANCELLED", "YIELDED"]
    completed_partitions: int = 1
    total_partitions: int = 1
    processed_records: int = 0
    estimated_records: int | None = None
    checkpoint_kind: str | None = None
    checkpoint_position: str | None = None
    quality_gate: dict[str, Any] | None = None
    error: dict[str, Any] | None = None


RunExecutor = Callable[[ExecutionClaim], ExecutionOutcome]


def build_catalog(settings: Settings, registry: SourceRegistry) -> dict[str, DatasetDefinition]:
    """从既有 canonical capability、已注册 adapter 与开关构建稳定运维目录。

    `providerId` 与真实 `upstreamSource` 不从界面猜测，而是在每次响应内由此目录和当前
    adapter 注册共同投影。未注册 adapter 的数据集仍可发现，但会明确显示 SOURCE_UNAVAILABLE。
    """
    definitions: tuple[DatasetDefinition, ...] = (
        DatasetDefinition(
            "equity.master.cn-a",
            "A 股证券目录",
            "equity",
            "沪深北交易所当前证券目录与稳定身份",
            "交易所 × 证券 × 观察日",
            "equity.master.catalog",
            ("FULL",),
            ("FULL",),
            selector_kinds=("GLOBAL",),
            dispatcher_ready=True,
            config_enabled=settings.akshare_enabled,
            lifecycle="CANDIDATE",
            provider_id="akshare-eastmoney-equity-catalog",
            upstream_source="eastmoney.equity-catalog",
            approval_status="CANDIDATE",
            rights_status="unverified",
            license_scope="unverified",
            data_as_of_label="目录观察日",
        ),
        DatasetDefinition(
            "equity.lifecycle.explicit",
            "A 股上市生命周期",
            "equity",
            "交易所明确披露的上市、终止上市与在册状态",
            "交易所 × 证券 × 生效日",
            "equity.lifecycle.explicit",
            ("FULL", "INCREMENTAL"),
            ("INCREMENTAL",),
            selector_kinds=("GLOBAL", "EXCHANGE"),
            dispatcher_ready=True,
            config_enabled=settings.akshare_enabled,
            lifecycle="CANDIDATE",
            provider_id="akshare-official-exchange-equity-lifecycle",
            upstream_source="sse.szse.bse.lifecycle",
            approval_status="CANDIDATE",
            rights_status="unverified",
            license_scope="unverified",
            data_as_of_kind="EVENT_DATE",
            data_as_of_label="状态生效日",
        ),
        DatasetDefinition(
            "equity.master.resolved",
            "A 股已解析主数据",
            "equity",
            "仅由已发布目录和显式生命周期组件固定的可重放主数据视图",
            "交易所 × 固定目录版本 × 固定生命周期版本",
            None,
            ("FULL",),
            (),
            selector_kinds=("GLOBAL",),
            dispatcher_ready=True,
            # providerless 派生 publication 只依赖已有 canonical 输入；不能被某个 Provider
            # 的实时开关误判为不可运行。
            config_enabled=True,
            providerless=True,
            provider_id="platform",
            upstream_source="platform-derived",
            data_as_of_label="输入组件共同可安全读取的最早业务日期",
        ),
        DatasetDefinition(
            "equity.bar.1d.raw",
            "个股日线行情",
            "equity",
            "A 股未复权日线行情",
            "证券 × 交易日",
            "equity.bar.1d.raw",
            ("FULL", "INCREMENTAL", "DATE_RANGE"),
            ("FULL", "INCREMENTAL"),
            selector_kinds=("GLOBAL", "INSTRUMENT"),
            dispatcher_ready=True,
            config_enabled=settings.equity_market_enabled,
            lifecycle="RESEARCH",
            provider_id="akshare-tencent",
            upstream_source="tencent.equity-kline",
            approval_status="RESEARCH",
            rights_status="unverified",
            license_scope="unverified",
            data_as_of_kind="TRADING_DATE",
            data_as_of_label="行情交易日",
        ),
        DatasetDefinition(
            "equity.bar.1w.raw",
            "个股周线行情",
            "equity",
            "上游独立周线行情",
            "证券 × 周期结束日",
            "equity.bar.1w.raw",
            ("FULL", "INCREMENTAL", "DATE_RANGE"),
            ("FULL", "INCREMENTAL"),
            selector_kinds=("GLOBAL", "INSTRUMENT"),
            dispatcher_ready=True,
            config_enabled=settings.equity_market_enabled,
            lifecycle="RESEARCH",
            provider_id="akshare-eastmoney-equity-period",
            upstream_source="eastmoney.equity-kline",
            approval_status="RESEARCH",
            rights_status="unverified",
            license_scope="unverified",
            data_as_of_kind="TRADING_DATE",
            data_as_of_label="行情交易日",
        ),
        DatasetDefinition(
            "equity.bar.1mo.raw",
            "个股月线行情",
            "equity",
            "上游独立月线行情",
            "证券 × 月份",
            "equity.bar.1mo.raw",
            ("FULL", "INCREMENTAL", "DATE_RANGE"),
            ("FULL", "INCREMENTAL"),
            selector_kinds=("GLOBAL", "INSTRUMENT"),
            dispatcher_ready=True,
            config_enabled=settings.equity_market_enabled,
            lifecycle="RESEARCH",
            provider_id="akshare-eastmoney-equity-period",
            upstream_source="eastmoney.equity-kline",
            approval_status="RESEARCH",
            rights_status="unverified",
            license_scope="unverified",
            data_as_of_kind="TRADING_DATE",
            data_as_of_label="行情交易日",
        ),
        DatasetDefinition(
            "equity.adjustment_factor",
            "个股复权因子",
            "equity",
            "个股累计后复权因子",
            "证券 × 生效日",
            "equity.adjustment_factor",
            ("FULL", "INCREMENTAL", "DATE_RANGE"),
            ("FULL", "INCREMENTAL"),
            selector_kinds=("GLOBAL", "INSTRUMENT"),
            dispatcher_ready=True,
            config_enabled=settings.equity_market_enabled,
            max_range_days=365 * 40,
            lifecycle="RESEARCH",
            provider_id="akshare-sina-adjustment-factor",
            upstream_source="sina.hfq-factor",
            approval_status="RESEARCH",
            rights_status="unverified",
            license_scope="unverified",
            data_as_of_kind="EVENT_DATE",
            data_as_of_label="因子生效日",
        ),
        DatasetDefinition(
            "equity.corporate_action",
            "个股公司行动",
            "equity",
            "分红送转及公司行动修订",
            "证券 × 报告期",
            "equity.corporate_action",
            ("FULL", "INCREMENTAL", "DATE_RANGE"),
            ("FULL", "INCREMENTAL"),
            selector_kinds=("GLOBAL", "INSTRUMENT"),
            dispatcher_ready=True,
            config_enabled=settings.equity_market_enabled,
            max_range_days=3 * 366,
            lifecycle="RESEARCH",
            provider_id="akshare-eastmoney-corporate-action",
            upstream_source="eastmoney.share-bonus",
            approval_status="RESEARCH",
            rights_status="unverified",
            license_scope="unverified",
            data_as_of_kind="EVENT_DATE",
            data_as_of_label="公司行动日",
        ),
        DatasetDefinition(
            "equity.profile",
            "个股公司概况",
            "equity",
            "公司基础资料当前修订",
            "证券",
            "equity.profile",
            ("FULL", "INCREMENTAL"),
            ("INCREMENTAL",),
            selector_kinds=("GLOBAL", "INSTRUMENT"),
            dispatcher_ready=True,
            config_enabled=settings.equity_market_enabled,
            lifecycle="RESEARCH",
            provider_id="akshare-cninfo-company-profile",
            upstream_source="cninfo.company-profile",
            approval_status="RESEARCH",
            rights_status="unverified",
            license_scope="unverified",
            data_as_of_label="资料观察日",
        ),
        DatasetDefinition(
            "equity.trading_status.1d",
            "A 股普通停复牌",
            "equity",
            "来源明确披露的日频普通停牌清单，与暂停上市生命周期分离",
            "证券 × 交易日 × 普通交易状态",
            "equity.trading_status.1d",
            ("FULL", "OBSERVATION_DATE"),
            ("OBSERVATION_DATE",),
            selector_kinds=("GLOBAL",),
            dispatcher_ready=True,
            config_enabled=settings.akshare_enabled and settings.equity_market_enabled,
            lifecycle="RESEARCH",
            provider_id="akshare",
            upstream_source="eastmoney.trading-suspension",
            approval_status="RESEARCH",
            data_as_of_kind="TRADING_DATE",
            data_as_of_label="停牌观察交易日",
        ),
        DatasetDefinition(
            "equity.share_capital.reported",
            "A 股历史股本结构",
            "equity",
            "来源报告的总股本、已上市流通 A 股与受限股本历史",
            "证券 × 股本生效日",
            "equity.share_capital.reported",
            ("FULL", "INCREMENTAL"),
            ("INCREMENTAL",),
            selector_kinds=("GLOBAL", "INSTRUMENT"),
            dispatcher_ready=True,
            config_enabled=settings.akshare_enabled and settings.equity_market_enabled,
            lifecycle="RESEARCH",
            provider_id="akshare",
            upstream_source="eastmoney.share-capital",
            approval_status="RESEARCH",
            data_as_of_kind="EVENT_DATE",
            data_as_of_label="股本生效日",
        ),
        DatasetDefinition(
            "equity.discovery.eod",
            "A 股发现 EOD 横截面",
            "equity",
            "仅从已发布 canonical 组件构建的统一筛选排序横截面",
            "发布版本 × 证券",
            None,
            ("FULL", "OBSERVATION_DATE"),
            ("OBSERVATION_DATE",),
            selector_kinds=("GLOBAL",),
            dispatcher_ready=True,
            config_enabled=settings.equity_market_enabled,
            providerless=True,
            provider_id="platform",
            upstream_source="platform-derived",
            data_as_of_kind="TRADING_DATE",
            data_as_of_label="横截面交易日",
        ),
        DatasetDefinition(
            "sector.catalog.raw",
            "板块目录",
            "sector",
            "行业与概念板块目录观察",
            "分类体系 × 观察日",
            "sector.catalog.raw",
            ("FULL", "INCREMENTAL", "OBSERVATION_DATE"),
            ("FULL", "INCREMENTAL", "OBSERVATION_DATE"),
            selector_kinds=("GLOBAL", "SCHEME"),
            dispatcher_ready=True,
            config_enabled=settings.sector_enabled,
            lifecycle="RESEARCH",
            provider_id="akshare-eastmoney-sector",
            upstream_source="eastmoney.sector-catalog",
            approval_status="RESEARCH",
            data_as_of_label="目录观察日",
        ),
        DatasetDefinition(
            "sector.bar.1d.raw",
            "东财板块日线",
            "sector",
            "东财行业与概念板块的上游原生日线，不由周月线或日线互相派生",
            "分类体系 × 板块 × 交易日",
            "sector.bar.1d.raw",
            ("FULL", "INCREMENTAL", "DATE_RANGE"),
            (),
            selector_kinds=("GLOBAL", "SCHEME", "SECTOR"),
            dispatcher_ready=True,
            config_enabled=settings.akshare_enabled and settings.sector_enabled,
            lifecycle="CANDIDATE",
            provider_id="akshare-eastmoney-sector",
            upstream_source="eastmoney.sector-hist",
            approval_status="CANDIDATE",
            data_as_of_kind="TRADING_DATE",
            data_as_of_label="板块交易日",
        ),
        DatasetDefinition(
            "sector.bar.1w.raw",
            "东财板块周线",
            "sector",
            "东财行业与概念板块的上游原生周线，不从日线聚合",
            "分类体系 × 板块 × 周期结束日",
            "sector.bar.1w.raw",
            ("FULL", "INCREMENTAL", "DATE_RANGE"),
            (),
            selector_kinds=("GLOBAL", "SCHEME", "SECTOR"),
            dispatcher_ready=True,
            config_enabled=settings.akshare_enabled and settings.sector_enabled,
            lifecycle="CANDIDATE",
            provider_id="akshare-eastmoney-sector",
            upstream_source="eastmoney.sector-hist",
            approval_status="CANDIDATE",
            data_as_of_kind="PERIOD_END_DATE",
            data_as_of_label="板块周线周期结束日",
        ),
        DatasetDefinition(
            "sector.bar.1mo.raw",
            "东财板块月线",
            "sector",
            "东财行业与概念板块的上游原生月线，不从日线或周线聚合",
            "分类体系 × 板块 × 周期结束日",
            "sector.bar.1mo.raw",
            ("FULL", "INCREMENTAL", "DATE_RANGE"),
            (),
            selector_kinds=("GLOBAL", "SCHEME", "SECTOR"),
            dispatcher_ready=True,
            config_enabled=settings.akshare_enabled and settings.sector_enabled,
            lifecycle="CANDIDATE",
            provider_id="akshare-eastmoney-sector",
            upstream_source="eastmoney.sector-hist",
            approval_status="CANDIDATE",
            data_as_of_kind="PERIOD_END_DATE",
            data_as_of_label="板块月线周期结束日",
        ),
        DatasetDefinition(
            "sector.membership.release",
            "东财板块成分发布",
            "sector",
            "行业与概念板块当前成分完整快照及稳定发布",
            "分类体系 × 板块 × 观察日",
            "sector.membership.snapshot.raw",
            ("FULL", "INCREMENTAL", "OBSERVATION_DATE"),
            ("FULL", "INCREMENTAL", "OBSERVATION_DATE"),
            selector_kinds=("GLOBAL", "SCHEME", "SECTOR"),
            dispatcher_ready=True,
            config_enabled=(
                settings.akshare_enabled
                and settings.sector_enabled
                and settings.sector_membership_enabled
            ),
            lifecycle="RESEARCH",
            provider_id="akshare-eastmoney-sector-membership",
            upstream_source="eastmoney.sector-membership",
            approval_status="RESEARCH",
            data_as_of_kind="OBSERVATION_DATE",
            data_as_of_label="成分观察日",
        ),
        DatasetDefinition(
            "sector.quote.eod.snapshot",
            "板块 EOD 横截面",
            "sector",
            "收盘后板块横截面观察",
            "分类体系 × 交易日",
            "sector.quote.eod.snapshot.raw",
            ("FULL", "OBSERVATION_DATE"),
            ("OBSERVATION_DATE",),
            selector_kinds=("GLOBAL", "SCHEME", "SECTOR"),
            dispatcher_ready=True,
            config_enabled=(
                settings.sector_enabled
                and settings.sector_eod_enabled
                and settings.sector_eod_publish_enabled
            ),
            provider_id="akshare-eastmoney-sector-eod",
            upstream_source="eastmoney.sector-eod",
            data_as_of_kind="TRADING_DATE",
            data_as_of_label="横截面交易日",
        ),
        DatasetDefinition(
            "financial.report",
            "财务报告",
            "financial",
            "公司财务报告与指标",
            "证券 × 报告期",
            "financial.statement.raw",
            ("FULL", "INCREMENTAL"),
            ("FULL", "INCREMENTAL"),
            selector_kinds=("GLOBAL", "INSTRUMENT"),
            dispatcher_ready=True,
            config_enabled=settings.financial_enabled,
            provider_id="akshare-eastmoney-financial",
            upstream_source="eastmoney.financial",
            data_as_of_kind="REPORT_PERIOD",
            data_as_of_label="财务报告期",
        ),
        DatasetDefinition(
            "financial.provider-metric",
            "供应商财务指标",
            "financial",
            "供应商直接报告期指标，独立于披露报表发布",
            "证券 × 报告期 × 指标",
            "financial.metric.raw",
            ("FULL", "INCREMENTAL"),
            ("FULL", "INCREMENTAL"),
            selector_kinds=("GLOBAL", "INSTRUMENT"),
            dispatcher_ready=True,
            config_enabled=settings.financial_enabled,
            provider_id="akshare-eastmoney-financial",
            upstream_source="eastmoney.financial",
            data_as_of_kind="REPORT_PERIOD",
            data_as_of_label="财务报告期",
        ),
        DatasetDefinition(
            "financial.valuation",
            "历史估值观察",
            "financial",
            "供应商日频估值观察，独立于报表和财务指标发布",
            "证券 × 观察日 × 估值指标",
            "financial.valuation.raw",
            ("FULL", "INCREMENTAL"),
            ("FULL", "INCREMENTAL"),
            selector_kinds=("GLOBAL", "INSTRUMENT"),
            dispatcher_ready=True,
            config_enabled=settings.financial_enabled,
            provider_id="akshare-eastmoney-financial",
            upstream_source="eastmoney.financial",
            data_as_of_kind="TRADING_DATE",
            data_as_of_label="估值观察日",
        ),
        DatasetDefinition(
            "financial.derived-metric",
            "衍生财务指标",
            "financial",
            "基于已发布财务数据的平台衍生指标",
            "证券 × 报告期",
            None,
            ("FULL", "INCREMENTAL"),
            ("INCREMENTAL",),
            selector_kinds=("INSTRUMENT",),
            dispatcher_ready=True,
            config_enabled=settings.financial_enabled,
            providerless=True,
            max_range_days=None,
            provider_id="platform",
            upstream_source="platform-derived",
            data_as_of_kind="REPORT_PERIOD",
            data_as_of_label="财务报告期",
        ),
        DatasetDefinition(
            _MONEY_FLOW_DAILY_DATASET,
            "日频资金流",
            "money_flow",
            "东财订单规模个股、板块与市场日频资金流；仅研究方法学，未形成公开 publication",
            "对象 × 交易日",
            "money_flow.order_size.daily.equity.raw",
            ("FULL", "INCREMENTAL"),
            (),
            source_capabilities=(
                "money_flow.order_size.daily.equity.raw",
                "money_flow.order_size.daily.sector.raw",
                "money_flow.order_size.daily.market.raw",
            ),
            selector_kinds=("MONEY_FLOW",),
            dispatcher_ready=True,
            config_enabled=settings.money_flow_enabled,
            lifecycle="RESEARCH",
            provider_id="akshare-eastmoney-money-flow",
            upstream_source="eastmoney.money-flow",
            approval_status="RESEARCH",
            data_as_of_kind="TRADING_DATE",
            data_as_of_label="资金流交易日",
        ),
        DatasetDefinition(
            _MONEY_FLOW_RANKING_DATASET,
            "供应商资金流排行",
            "money_flow",
            "东财订单规模与同花顺交易方向排行的真实来源研究观察；"
            "未证完整分页不进入正式排行 publication",
            "方法学 × 样本池 × 窗口 × 观察日 × 供应商位置",
            None,
            ("OBSERVATION_DATE",),
            (),
            source_capabilities=(
                "money_flow.order_size.ranking.equity.raw",
                "money_flow.order_size.ranking.sector.raw",
                "money_flow.trade_direction.ranking.equity.raw",
                "money_flow.trade_direction.ranking.industry.raw",
                "money_flow.trade_direction.ranking.concept.raw",
            ),
            selector_kinds=("MONEY_FLOW",),
            dispatcher_ready=True,
            config_enabled=settings.money_flow_enabled,
            lifecycle="RESEARCH",
            approval_status="RESEARCH",
            data_as_of_kind="OBSERVATION_DATE",
            data_as_of_label="供应商排行观察日",
        ),
        DatasetDefinition(
            "sector.sw.taxonomy",
            "申万行业目录",
            "sector",
            "申万三级行业 taxonomy",
            "层级节点 × 快照日",
            "sector.sw.snapshot.raw",
            ("FULL", "OBSERVATION_DATE"),
            ("OBSERVATION_DATE",),
            selector_kinds=("GLOBAL", "SCHEME", "SECTOR"),
            dispatcher_ready=True,
            config_enabled=settings.sector_enabled and settings.sw_sector_enabled,
            lifecycle="RESEARCH",
            provider_id="akshare-legulegu-sw-industry",
            upstream_source="legulegu.sw-industry",
            approval_status="RESEARCH",
            data_as_of_kind="SNAPSHOT_DATE",
            data_as_of_label="行业快照日",
        ),
        DatasetDefinition(
            "sector.sw2021.membership.snapshot",
            "申万三级行业成分",
            "sector",
            "按申万三级节点冻结的当前证券成分观察",
            "申万三级节点 × 证券 × 观察日",
            "sector.sw2021.membership.snapshot",
            ("FULL", "OBSERVATION_DATE"),
            ("OBSERVATION_DATE",),
            selector_kinds=("GLOBAL", "SECTOR"),
            dispatcher_ready=True,
            config_enabled=(
                settings.akshare_enabled and settings.sector_enabled and settings.sw_sector_enabled
            ),
            lifecycle="RESEARCH",
            provider_id="akshare",
            upstream_source="legulegu.sw-index-composition",
            approval_status="RESEARCH",
            data_as_of_kind="SNAPSHOT_DATE",
            data_as_of_label="成分观察日",
        ),
        DatasetDefinition(
            "fund.etf.profile.reported",
            "ETF 产品资料",
            "fund",
            "沪深 ETF 产品目录与报告资料",
            "交易所 × ETF × 观察日",
            "fund.etf.master",
            ("FULL", "OBSERVATION_DATE"),
            ("OBSERVATION_DATE",),
            selector_kinds=("ETF",),
            dispatcher_ready=True,
            config_enabled=settings.akshare_enabled,
            lifecycle="CANDIDATE",
            provider_id="akshare",
            upstream_source="sse-szse.official-etf-directory",
            approval_status="CANDIDATE",
            data_as_of_label="产品观察日",
        ),
        DatasetDefinition(
            "fund.etf.trading_state.reported",
            "ETF 报告状态",
            "fund",
            "ETF 申购、赎回等来源报告状态修订",
            "ETF × 状态生效日",
            "fund.etf.trading_state",
            ("FULL", "INCREMENTAL", "DATE_RANGE", "OBSERVATION_DATE"),
            ("INCREMENTAL", "OBSERVATION_DATE"),
            selector_kinds=("ETF",),
            dispatcher_ready=True,
            config_enabled=settings.akshare_enabled,
            lifecycle="CANDIDATE",
            provider_id="akshare",
            upstream_source="eastmoney.etf.nav-json",
            approval_status="CANDIDATE",
            data_as_of_kind="EVENT_DATE",
            data_as_of_label="状态生效日",
        ),
        DatasetDefinition(
            "fund.etf.bar.1d.reported",
            "ETF 日线行情",
            "fund",
            "ETF 日频行情报告值",
            "ETF × 交易日",
            "fund.etf.bar.1d.raw",
            ("FULL", "INCREMENTAL", "DATE_RANGE"),
            ("INCREMENTAL",),
            selector_kinds=("ETF",),
            dispatcher_ready=True,
            config_enabled=settings.akshare_enabled,
            lifecycle="CANDIDATE",
            provider_id="akshare",
            upstream_source="tencent.etf-kline",
            approval_status="CANDIDATE",
            data_as_of_kind="TRADING_DATE",
            data_as_of_label="行情交易日",
        ),
        DatasetDefinition(
            "fund.etf.nav.1d.reported",
            "ETF 日频净值",
            "fund",
            "ETF 单位净值、累计净值与报告日期",
            "ETF × 净值日期",
            "fund.etf.nav.1d.reported",
            ("FULL", "INCREMENTAL", "DATE_RANGE"),
            ("INCREMENTAL",),
            selector_kinds=("ETF",),
            dispatcher_ready=True,
            config_enabled=settings.akshare_enabled,
            lifecycle="CANDIDATE",
            provider_id="akshare",
            upstream_source="eastmoney.etf.nav-json",
            approval_status="CANDIDATE",
            data_as_of_kind="TRADING_DATE",
            data_as_of_label="净值日期",
        ),
        DatasetDefinition(
            "index.csi.catalog.snapshot",
            "中证指数目录研究快照",
            "index",
            "AKShare 转发的中证指数当前目录观察；不声明历史有效期或消费者 publication",
            "中证管理人 × 当前来源观察批次",
            "index.catalog.snapshot",
            ("FULL",),
            (),
            selector_kinds=("INDEX",),
            dispatcher_ready=True,
            config_enabled=settings.index_enabled
            and settings.index_source_policy in {"akshare-csindex", "akshare-csindex-cnindex"},
            lifecycle="RESEARCH",
            provider_id="akshare-csindex-index-snapshot",
            upstream_source="csindex",
            approval_status="RESEARCH",
            data_as_of_kind="OBSERVATION_DATE",
            data_as_of_label="来源目录观测日",
        ),
        DatasetDefinition(
            "index.csi.constituent.snapshot",
            "中证指数成分研究快照",
            "index",
            "AKShare 转发的中证指数当前成分观察；不推断成分历史生效区间",
            "中证管理人 × 指数代码 × 当前来源观察批次",
            "index.constituent.snapshot",
            ("FULL",),
            (),
            selector_kinds=("INDEX",),
            dispatcher_ready=True,
            config_enabled=settings.index_enabled
            and settings.index_source_policy in {"akshare-csindex", "akshare-csindex-cnindex"},
            lifecycle="RESEARCH",
            provider_id="akshare-csindex-index-snapshot",
            upstream_source="csindex",
            approval_status="RESEARCH",
            data_as_of_kind="OBSERVATION_DATE",
            data_as_of_label="来源成分观测日",
        ),
        DatasetDefinition(
            "index.csi.weight.snapshot",
            "中证指数权重研究快照",
            "index",
            "AKShare 转发的中证指数当前权重观察；不转换为历史正式权重",
            "中证管理人 × 指数代码 × 当前来源观察批次",
            "index.weight.snapshot",
            ("FULL",),
            (),
            selector_kinds=("INDEX",),
            dispatcher_ready=True,
            config_enabled=settings.index_enabled
            and settings.index_source_policy in {"akshare-csindex", "akshare-csindex-cnindex"},
            lifecycle="RESEARCH",
            provider_id="akshare-csindex-index-snapshot",
            upstream_source="csindex",
            approval_status="RESEARCH",
            data_as_of_kind="OBSERVATION_DATE",
            data_as_of_label="来源权重观测日",
        ),
        DatasetDefinition(
            "index.cni.catalog.snapshot",
            "国证指数目录研究快照",
            "index",
            "AKShare 转发的国证指数当前目录观察；不声明历史有效期或消费者 publication",
            "国证管理人 × 当前来源观察批次",
            "index.catalog.snapshot",
            ("FULL",),
            (),
            selector_kinds=("INDEX",),
            dispatcher_ready=True,
            config_enabled=settings.index_enabled
            and settings.index_source_policy in {"akshare-cnindex", "akshare-csindex-cnindex"},
            lifecycle="RESEARCH",
            provider_id="akshare-cnindex-index-snapshot",
            upstream_source="cnindex",
            approval_status="RESEARCH",
            data_as_of_kind="OBSERVATION_DATE",
            data_as_of_label="来源目录观测日",
        ),
        DatasetDefinition(
            "index.cni.constituent.snapshot",
            "国证指数成分研究快照",
            "index",
            "AKShare 转发的国证指数当前成分观察；不推断成分历史生效区间",
            "国证管理人 × 指数代码 × 当前来源观察批次",
            "index.constituent.snapshot",
            ("FULL",),
            (),
            selector_kinds=("INDEX",),
            dispatcher_ready=True,
            config_enabled=settings.index_enabled
            and settings.index_source_policy in {"akshare-cnindex", "akshare-csindex-cnindex"},
            lifecycle="RESEARCH",
            provider_id="akshare-cnindex-index-snapshot",
            upstream_source="cnindex",
            approval_status="RESEARCH",
            data_as_of_kind="OBSERVATION_DATE",
            data_as_of_label="来源成分观测日",
        ),
        DatasetDefinition(
            "index.cni.weight.snapshot",
            "国证指数权重研究快照",
            "index",
            "AKShare 转发的国证指数当前权重观察；不转换为历史正式权重",
            "国证管理人 × 指数代码 × 当前来源观察批次",
            "index.weight.snapshot",
            ("FULL",),
            (),
            selector_kinds=("INDEX",),
            dispatcher_ready=True,
            config_enabled=settings.index_enabled
            and settings.index_source_policy in {"akshare-cnindex", "akshare-csindex-cnindex"},
            lifecycle="RESEARCH",
            provider_id="akshare-cnindex-index-snapshot",
            upstream_source="cnindex",
            approval_status="RESEARCH",
            data_as_of_kind="OBSERVATION_DATE",
            data_as_of_label="来源权重观测日",
        ),
        DatasetDefinition(
            "market.margin.market.1d.reported",
            "融资融券市场汇总",
            "margin",
            "沪深交易所融资融券市场日汇总",
            "交易所 × 交易日",
            "market.margin.market.1d.reported",
            ("FULL", "INCREMENTAL", "DATE_RANGE"),
            (),
            selector_kinds=("MARGIN",),
            dispatcher_ready=True,
            config_enabled=settings.akshare_enabled,
            lifecycle="RESEARCH",
            provider_id="akshare",
            upstream_source="sse-szse.margin",
            approval_status="RESEARCH",
            data_as_of_kind="TRADING_DATE",
            data_as_of_label="两融交易日",
        ),
        DatasetDefinition(
            "market.margin.security.1d.reported",
            "融资融券证券明细",
            "margin",
            "沪深交易所证券级融资融券日报",
            "交易所 × 证券 × 交易日",
            "market.margin.security.1d.reported",
            ("FULL", "INCREMENTAL", "DATE_RANGE"),
            (),
            selector_kinds=("MARGIN",),
            dispatcher_ready=True,
            config_enabled=settings.akshare_enabled,
            lifecycle="RESEARCH",
            provider_id="akshare",
            upstream_source="sse-szse.margin",
            approval_status="RESEARCH",
            data_as_of_kind="TRADING_DATE",
            data_as_of_label="两融交易日",
        ),
        DatasetDefinition(
            "market.margin.eligibility.reported",
            "融资融券标的资格",
            "margin",
            "交易所披露的融资融券标的资格快照",
            "交易所 × 证券 × 快照日",
            "market.margin.eligibility.reported",
            ("FULL", "OBSERVATION_DATE"),
            (),
            selector_kinds=("MARGIN",),
            dispatcher_ready=True,
            config_enabled=settings.akshare_enabled,
            lifecycle="RESEARCH",
            provider_id="akshare",
            upstream_source="szse.margin-underlying,bse.margin-underlying",
            approval_status="RESEARCH",
            data_as_of_kind="SNAPSHOT_DATE",
            data_as_of_label="资格快照日",
        ),
        DatasetDefinition(
            "market.stock_connect.market_stat.research",
            "港通市场统计研究观察",
            "stock_connect",
            "AKShare/EastMoney 报告的港通市场统计研究观察；独立于官方完整包，"
            "永不形成正式 publication",
            "通道 × 方向 × 来源报告交易日",
            "market.stock_connect.market_stat.reported",
            ("OBSERVATION_DATE", "DATE_RANGE"),
            (),
            selector_kinds=("STOCK_CONNECT_RESEARCH",),
            dispatcher_ready=True,
            config_enabled=settings.akshare_enabled,
            lifecycle="RESEARCH",
            max_range_days=31,
            provider_id="akshare",
            upstream_source="eastmoney.stock-connect",
            approval_status="RESEARCH",
            data_as_of_kind="TRADING_DATE",
            data_as_of_label="来源报告交易日",
        ),
        DatasetDefinition(
            "market.stock_connect.overview.bundle",
            "沪深港通中心完整包",
            "stock_connect",
            "官方日历、通道统计、来源活跃榜、身份与最终状态的原子完整包",
            "通道 × 方向 × 官方交易日",
            "market.stock_connect.market_stat.reported",
            ("FULL", "INCREMENTAL", "DATE_RANGE"),
            (),
            source_capabilities=(
                "market.stock_connect.market_stat.reported",
                "market.stock_connect.active_security.snapshot",
                "market.stock_connect.trading_calendar",
                "market.stock_connect.instrument_master.reported",
                "market.stock_connect.channel_status.eod",
            ),
            selector_kinds=("STOCK_CONNECT",),
            dispatcher_ready=True,
            config_enabled=settings.stock_connect_enabled,
            lifecycle="CANDIDATE",
            provider_id="official-stock-connect",
            upstream_source="HKEX Data Marketplace / OMD-C / SSE MDGW / SZSE STEP",
            approval_status="APPROVED",
            data_as_of_kind="TRADING_DATE",
            data_as_of_label="互联互通交易日",
        ),
        DatasetDefinition(
            "market.overview-and-sectors.bundle",
            "市场概览与行业板块完整包",
            "market",
            "沪深 A 股指数、宽度、成交、资金流、东财板块与申万行业的原子 EOD 完整包",
            "沪深共同交易日 × 完整组件 manifest",
            "market.source.preflight",
            ("INCREMENTAL", "OBSERVATION_DATE", "DATE_RANGE"),
            ("INCREMENTAL",),
            source_capabilities=(
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
            ),
            selector_kinds=("GLOBAL",),
            dispatcher_ready=True,
            config_enabled=settings.market_overview_enabled,
            lifecycle=(
                "PRODUCTION"
                if settings.market_data_license_scope.value == "commercial-redistribution-approved"
                else "CANDIDATE"
            ),
            provider_id="tushare-pro",
            upstream_source="Tushare Pro licensed datasets",
            approval_status=(
                "APPROVED" if settings.market_data_license_scope.value != "disabled" else "BLOCKED"
            ),
            max_range_days=120,
            correction_lookback_days=25,
            data_as_of_kind="TRADING_DATE",
            data_as_of_label="沪深共同交易日",
        ),
        DatasetDefinition(
            "equity.corporate_event.earnings.reported",
            "公司业绩披露事件",
            "equity_event",
            "公司业绩预告与快报披露事件",
            "证券 × 披露事件 × 事件日",
            "corporate.disclosure.earnings.p0",
            ("INCREMENTAL", "DATE_RANGE"),
            ("INCREMENTAL",),
            selector_kinds=("GLOBAL", "INSTRUMENT"),
            dispatcher_ready=True,
            config_enabled=settings.akshare_enabled,
            lifecycle="RESEARCH",
            max_range_days=31,
            correction_lookback_days=31,
            provider_id="akshare",
            upstream_source="eastmoney.earnings",
            approval_status="RESEARCH",
            data_as_of_kind="EVENT_DATE",
            data_as_of_label="披露事件日",
        ),
        DatasetDefinition(
            "equity.dragon_tiger.disclosure.reported",
            "龙虎榜披露",
            "equity_event",
            "龙虎榜事件、上榜原因与席位明细",
            "证券 × 披露事件 × 交易日",
            "market.dragon_tiger.disclosure.1d",
            ("INCREMENTAL", "DATE_RANGE"),
            ("INCREMENTAL",),
            selector_kinds=("TRADING_EVENT", "INSTRUMENT"),
            dispatcher_ready=True,
            config_enabled=settings.akshare_enabled,
            lifecycle="RESEARCH",
            max_range_days=31,
            correction_lookback_days=31,
            provider_id="akshare",
            upstream_source="eastmoney.dragon-tiger",
            approval_status="RESEARCH",
            data_as_of_kind="EVENT_DATE",
            data_as_of_label="龙虎榜交易日",
        ),
        DatasetDefinition(
            "equity.block_trade.execution.reported",
            "大宗交易成交",
            "equity_event",
            "大宗交易逐笔成交与披露信息",
            "证券 × 成交记录 × 交易日",
            "market.block_trade.execution.1d",
            ("INCREMENTAL", "DATE_RANGE"),
            ("INCREMENTAL",),
            selector_kinds=("TRADING_EVENT", "INSTRUMENT"),
            dispatcher_ready=True,
            config_enabled=settings.akshare_enabled,
            lifecycle="RESEARCH",
            max_range_days=31,
            correction_lookback_days=31,
            provider_id="akshare",
            upstream_source="eastmoney.block-trade",
            approval_status="RESEARCH",
            data_as_of_kind="EVENT_DATE",
            data_as_of_label="大宗交易日",
        ),
        DatasetDefinition(
            "derivative.bar.1d.reported",
            "衍生品合约日线",
            "derivative",
            "真实期货合约日频行情",
            "交易场所 × 合约 × 交易日",
            "derivative.bar.1d.reported",
            ("FULL", "INCREMENTAL", "DATE_RANGE"),
            (),
            selector_kinds=("CONTRACT",),
            dispatcher_ready=True,
            config_enabled=settings.akshare_enabled,
            lifecycle="RESEARCH",
            provider_id="akshare",
            upstream_source="eastmoney.futures",
            approval_status="RESEARCH",
            data_as_of_kind="TRADING_DATE",
            data_as_of_label="合约交易日",
        ),
    )
    # 显式 manifest 承载业务名称与粒度；启动时检查它所引用的现有 ORM 逻辑表，防止漂移。
    required_tables: dict[str, tuple[str, ...]] = {
        "equity.master.cn-a": (
            "equity_instrument",
            "equity_master_snapshot",
            "equity_master_snapshot_member",
        ),
        "equity.lifecycle.explicit": (
            "equity_listing_status_version",
            "equity_lifecycle_checkpoint",
        ),
        "equity.master.resolved": (
            "dataset_publication",
            "dataset_publication_component",
            "dataset_release",
            "canonical_record_lineage",
        ),
        "equity.bar.1d.raw": ("equity_daily_bar",),
        "equity.bar.1w.raw": ("equity_weekly_bar",),
        "equity.bar.1mo.raw": ("equity_monthly_bar",),
        "equity.adjustment_factor": ("equity_adjustment_factor",),
        "equity.corporate_action": ("equity_corporate_action_version",),
        "equity.profile": ("equity_profile_version",),
        "equity.trading_status.1d": ("equity_trading_status_revision",),
        "equity.share_capital.reported": ("equity_share_capital_revision",),
        "equity.discovery.eod": (
            "equity_discovery_snapshot",
            "equity_discovery_membership",
            "equity_discovery_availability",
        ),
        "sector.catalog.raw": ("sector_entity", "sector_scheme"),
        "sector.bar.1d.raw": ("sector_daily_bar",),
        "sector.bar.1w.raw": ("sector_weekly_bar",),
        "sector.bar.1mo.raw": ("sector_monthly_bar",),
        "sector.membership.release": (
            "sector_membership_release",
            "sector_membership_release_sector",
            "sector_membership_snapshot",
            "sector_membership_item",
            "sector_membership_interval",
            "sector_membership_pending",
            "sector_membership_quarantine",
            "sector_membership_quality_result",
        ),
        "sector.quote.eod.snapshot": ("sector_eod_snapshot", "sector_eod_quote"),
        "financial.report": ("financial_report", "financial_report_revision"),
        "financial.provider-metric": ("provider_financial_metric_revision",),
        "financial.valuation": ("valuation_observation_revision",),
        "financial.derived-metric": (
            "derived_financial_metric_revision",
            "financial_derivation_input",
            "financial_publication",
            "sync_run",
        ),
        _MONEY_FLOW_DAILY_DATASET: (
            "money_flow_methodology",
            "money_flow_methodology_version",
            "money_flow_bucket_definition",
            "money_flow_universe_version",
            "money_flow_series",
            "money_flow_daily_observation",
            "money_flow_quality_result",
        ),
        _MONEY_FLOW_RANKING_DATASET: (
            "money_flow_methodology",
            "money_flow_methodology_version",
            "money_flow_bucket_definition",
            "money_flow_ranking_research_observation",
            "money_flow_ranking_research_item",
            "money_flow_ranking_research_metric",
            "money_flow_quality_result",
        ),
        "sector.sw.taxonomy": ("sw_sector_node_revision", "sw_sector_publication"),
        "sector.sw2021.membership.snapshot": (
            "sw_membership_release",
            "sw_membership_item",
        ),
        "fund.etf.profile.reported": ("etf_profile_version",),
        "fund.etf.trading_state.reported": ("etf_status_revision",),
        "fund.etf.bar.1d.reported": ("etf_daily_bar_revision",),
        "fund.etf.nav.1d.reported": ("etf_nav_revision",),
        "index.csi.catalog.snapshot": (
            "index_definition",
            "index_catalog_observation",
            "index_catalog_observation_item",
        ),
        "index.csi.constituent.snapshot": (
            "index_definition",
            "index_observed_snapshot",
            "index_observed_snapshot_item",
        ),
        "index.csi.weight.snapshot": (
            "index_definition",
            "index_observed_snapshot",
            "index_observed_snapshot_item",
        ),
        "index.cni.catalog.snapshot": (
            "index_definition",
            "index_catalog_observation",
            "index_catalog_observation_item",
        ),
        "index.cni.constituent.snapshot": (
            "index_definition",
            "index_observed_snapshot",
            "index_observed_snapshot_item",
        ),
        "index.cni.weight.snapshot": (
            "index_definition",
            "index_observed_snapshot",
            "index_observed_snapshot_item",
        ),
        "market.margin.market.1d.reported": ("margin_market_daily_revision",),
        "market.margin.security.1d.reported": ("margin_security_daily_revision",),
        "market.margin.eligibility.reported": ("margin_eligibility_revision",),
        _STOCK_CONNECT_RESEARCH_DATASET: (
            "stock_connect_market_stat_research_batch",
            "stock_connect_market_stat_research_observation",
        ),
        "market.stock_connect.overview.bundle": (
            "stock_connect_channel_daily_revision",
            "stock_connect_active_security_revision",
            "stock_connect_calendar_observation",
            "stock_connect_channel_status_revision",
            "stock_connect_bundle_publication",
            "stock_connect_overview_publication",
        ),
        "market.overview-and-sectors.bundle": (
            "market_overview_component_release",
            "market_overview_bundle",
            "market_overview_bundle_component",
            "market_overview_active_bundle",
            "market_overview_current_pointer",
            "market_overview_pointer_transition",
            "market_overview_derivation_input_pointer",
        ),
        "equity.corporate_event.earnings.reported": (
            "corporate_event",
            "corporate_event_revision",
            "corporate_earnings_value",
        ),
        "equity.dragon_tiger.disclosure.reported": (
            "dragon_tiger_event_revision",
            "dragon_tiger_seat_item",
        ),
        "equity.block_trade.execution.reported": ("block_trade_execution_revision",),
        "derivative.bar.1d.reported": (
            "derivative_contract",
            "derivative_contract_revision",
            "derivative_daily_bar_revision",
        ),
    }
    if len(definitions) != 46 or len({item.dataset_code for item in definitions}) != 46:
        raise RuntimeError("data operations catalog must contain 46 unique datasets")
    registered_tables = {model.__tablename__ for model in ALL_MODELS}
    missing = sorted(
        table
        for tables in required_tables.values()
        for table in tables
        if table not in registered_tables
    )
    if missing:
        raise RuntimeError(
            f"data operations catalog references unregistered tables: {', '.join(missing)}"
        )
    # 每个来源按自身开关核对，避免关闭 AKShare 时误伤独立启用的官方互联互通适配器。
    provider_ids = set(registry.provider_ids())
    if not settings.akshare_enabled and "akshare" in provider_ids:
        raise RuntimeError(
            "data operations source registry conflicts with disabled provider config"
        )
    if not settings.stock_connect_enabled and "official-stock-connect" in provider_ids:
        raise RuntimeError(
            "data operations source registry conflicts with disabled stock-connect config"
        )
    return {definition.dataset_code: definition for definition in definitions}


class DataOperationsControlPlane:
    """以 PostgreSQL 为权威实现内部数据运维控制面所有状态转换。"""

    def __init__(
        self,
        *,
        database: DatabaseClient,
        catalog: dict[str, DatasetDefinition],
        source_registry: SourceRegistry,
        trading_calendar: TradingCalendarPort | None = None,
        now: Callable[[], datetime] | None = None,
        etf_auto_retry_max_attempts: int = 3,
        stock_connect_status_boundary_repository: (
            StockConnectStatusBoundaryRepository | None
        ) = None,
        stock_connect_readiness_repository: (StockConnectReadinessRepository | None) = None,
    ) -> None:
        """接收数据库、冻结目录和 provider-neutral 注册表，不接收具体 adapter。"""
        if not 1 <= etf_auto_retry_max_attempts <= 5:
            raise ValueError("ETF automatic retry attempts must be between 1 and 5")
        self._database = database
        self._catalog = catalog
        self._source_registry = source_registry
        self._trading_calendar = trading_calendar
        self._now = now or (lambda: datetime.now(UTC))
        self._etf_auto_retry_max_attempts = etf_auto_retry_max_attempts
        self._stock_connect_status_boundary_repository = (
            stock_connect_status_boundary_repository
            or SqlAlchemyStockConnectStatusBoundaryRepository(database)
        )
        self._stock_connect_readiness_repository = (
            stock_connect_readiness_repository
            or SqlAlchemyStockConnectReadinessRepository(database)
        )
        self._executors: dict[str, RunExecutor] = {}

    def register_executor(self, dataset_code: str, executor: RunExecutor) -> None:
        """注册 worker 使用的中立执行器；执行器不能绕过本类的 slot/fencing 提交。"""
        if dataset_code not in self._catalog:
            raise ValueError("unknown data operation dataset")
        self._executors[dataset_code] = executor

    def overview(self) -> dict[str, Any]:
        """返回目录计数、队列、失败统计和当前全局执行槽。"""
        with self._database.session() as session:
            slot = self._ensure_slot(session)
            queued = (
                session.scalar(
                    select(func.count())
                    .select_from(DataOperationRun)
                    .where(DataOperationRun.status == "QUEUED")
                )
                or 0
            )
            since = self._now() - timedelta(hours=24)
            failed = (
                session.scalar(
                    select(func.count())
                    .select_from(DataOperationRun)
                    .where(
                        DataOperationRun.status == "FAILED", DataOperationRun.finished_at >= since
                    )
                )
                or 0
            )
            summaries = [
                self._dataset_summary(session, definition) for definition in self._catalog.values()
            ]
            health = self._aggregate_health(summaries)
            return {
                "datasetCount": len(summaries),
                "enabledDatasetCount": sum(
                    1 for item in summaries if item["availability"] == "ENABLED"
                ),
                "healthSummary": health,
                "executionSlot": self._slot_view(slot),
                "queuedRunCount": queued,
                "failedRunCount24h": failed,
                "generatedAt": self._iso(self._now()),
            }

    def list_datasets(self, request: dict[str, Any]) -> dict[str, Any]:
        """按目录字段过滤并使用独立 cursor 返回数据集运维摘要。"""
        self._validate_limit(request.get("limit", 50), maximum=200)
        query = self._optional_text(request.get("query"))
        with self._database.session() as session:
            rows = [
                self._dataset_summary(session, definition) for definition in self._catalog.values()
            ]
            if query is not None:
                query_key = query.casefold()
                rows = [
                    item
                    for item in rows
                    if query_key in item["datasetCode"].casefold()
                    or query_key in item["displayName"].casefold()
                ]
            for key, field in (
                ("domains", "domain"),
                ("availability", "availability"),
                ("observationStates", "observationState"),
            ):
                wanted = request.get(key)
                if wanted:
                    rows = [item for item in rows if item[field] in set(wanted)]
            rows.sort(key=lambda item: item["datasetCode"])
            offset = self._decode_offset(request.get("cursor"))
            limit = int(request.get("limit", 50))
            visible = rows[offset : offset + limit]
            next_cursor = (
                self._encode_offset(offset + limit) if offset + limit < len(rows) else None
            )
            return {
                "items": visible,
                "nextCursor": next_cursor,
                "totalEstimate": len(rows),
                "generatedAt": self._iso(self._now()),
            }

    def dataset_detail(self, dataset_code: str) -> dict[str, Any]:
        """返回单个数据集的来源、能力、最新发布、错误和健康规则。"""
        definition = self._definition(dataset_code)
        with self._database.session() as session:
            summary = self._dataset_summary(session, definition)
            publication = self._latest_publication(session, definition.dataset_code)
            model_only = definition.model_only
            return {
                "summary": summary,
                "description": definition.description,
                "ownerService": "service-data-sync",
                "grain": definition.grain,
                "freshnessPolicy": None
                if model_only
                else {
                    "timezone": "Asia/Shanghai",
                    "calendarCode": "SSE-SZSE",
                    "warnAfterMinutes": 1440,
                    "criticalAfterMinutes": 4320,
                },
                "latestPublication": self._publication_view(session, publication),
                "latestError": self._latest_error(session, dataset_code),
                "healthRules": self._health_rules(model_only),
            }

    def preflight(self, targets: list[dict[str, Any]]) -> dict[str, Any]:
        """校验并持久化有时效预检，不抢占 slot、不创建 command。"""
        normalized = self._validate_targets(targets, etf_all_profile_versions="DRAFT")
        now = self._now()
        with self._database.transaction() as session:
            frozen_targets, universe_by_dataset = self._freeze_etf_all_targets(
                session,
                normalized,
            )
            equity_rosters = {
                index: self._freeze_share_capital_roster(
                    session,
                    target=target,
                    identity_as_of=now.astimezone(_SHANGHAI).date(),
                )
                for index, target in enumerate(frozen_targets)
                if target["datasetCode"] == "equity.share_capital.reported"
            }
            sector_bar_rosters = {
                index: self._freeze_sector_bar_roster(session, target=target)
                for index, target in enumerate(frozen_targets)
                if target["datasetCode"] in _SECTOR_BAR_DATASETS
            }
            request_hash = self._hash(frozen_targets)
            results = [
                self._preflight_target(
                    target,
                    etf_universe=universe_by_dataset.get(str(target["datasetCode"])),
                    equity_roster=equity_rosters.get(index),
                    sector_bar_roster=sector_bar_rosters.get(index),
                )
                for index, target in enumerate(frozen_targets)
            ]
            # 远端全窗探针可能持续数分钟；TTL 必须从证据全部冻结完成后起算。
            completed_at = self._now()
            preflight = DataOperationPreflight(
                preflight_id=uuid4(),
                request_hash=request_hash,
                targets_json=frozen_targets,
                result_json=results,
                created_at=completed_at,
                expires_at=completed_at + timedelta(seconds=_PREFLIGHT_TTL_SECONDS),
            )
            slot = self._ensure_slot(session)
            session.add(preflight)
            queue_depth = (
                session.scalar(
                    select(func.count())
                    .select_from(DataOperationRun)
                    .where(DataOperationRun.status == "QUEUED")
                )
                or 0
            )
            slot_view = self._slot_view(slot)
        return {
            "preflightId": str(preflight.preflight_id),
            "requestHash": request_hash,
            "expiresAt": self._iso(preflight.expires_at),
            "queueDepth": queue_depth,
            "executionSlot": slot_view,
            # 万级永久身份名单仅用于受理事务，不进入内部 HTTP 预检响应。
            "targets": [
                {
                    key: value
                    for key, value in item.items()
                    if key
                    not in {
                        "equityInstrumentRoster",
                        "equityInstrumentRosterHash",
                        "sectorBarRoster",
                        "sectorBarRosterHash",
                        "deliveryManifestRef",
                        "minimumExecutionWindowSeconds",
                        "readinessSnapshotRef",
                        "sourceEvidence",
                    }
                }
                for item in results
            ],
            "accepted": all(item["eligible"] for item in results),
        }

    def submit_command(
        self, *, request: dict[str, Any], idempotency_key: str, request_id: str
    ) -> dict[str, Any]:
        """受理公开合同的 command；公开请求绝不携带私有 LegacyExecutionIntent。"""
        allowed_request_keys = {"submissionId", "preflightId", "requestHash", "targets", "actor"}
        if set(request) != allowed_request_keys:
            raise OperationProblem(
                status=422,
                code="invalid-command-request",
                detail="Command request has unsupported fields",
            )
        self._require_idempotency_key(idempotency_key)
        targets = self._validate_targets(
            self._require_list(request, "targets"),
            etf_all_profile_versions="FROZEN",
        )
        submission_id = self._uuid_field(request, "submissionId")
        preflight_id = self._uuid_field(request, "preflightId")
        request_hash = self._require_string(request, "requestHash", max_length=64)
        actor = self._actor(request)
        return self._submit_validated_command(
            targets=targets,
            submission_id=submission_id,
            preflight_id=preflight_id,
            request_hash=request_hash,
            actor=actor,
            idempotency_key=idempotency_key,
            request_id=request_id,
            operation_hash=self._hash(request),
            execution_intents=(None,) * len(targets),
        )

    def submit_system_legacy_command(
        self,
        *,
        targets: list[dict[str, Any]],
        intents: list[dict[str, Any]],
        reason: str,
        idempotency_key: str,
        request_id: str,
        submission_id: UUID,
    ) -> dict[str, Any]:
        """仅供 Python 兼容层提交 SYSTEM command，并把严格私有执行意图冻结到 child run。"""
        self._require_idempotency_key(idempotency_key)
        normalized_targets = self._validate_targets(targets)
        if len(intents) != len(normalized_targets):
            raise OperationProblem(
                status=422,
                code="invalid-legacy-intent-count",
                detail="Legacy intent count must match sync target count",
            )
        normalized_intents = tuple(
            self._validate_legacy_execution_intent(intent) for intent in intents
        )
        intent_kinds = {str(intent["kind"]) for intent in normalized_intents}
        if intent_kinds == {"EQUITY_BACKFILL"}:
            if any(
                target["datasetCode"] not in _EQUITY_BACKFILL_DATASETS
                for target in normalized_targets
            ):
                raise OperationProblem(
                    status=422,
                    code="equity-backfill-dataset-unsupported",
                    detail="Dataset is not enabled for equity backfill",
                )
            self._assert_equity_backfill_submission(
                targets=normalized_targets,
                intents=normalized_intents,
                submission_id=submission_id,
            )
        elif intent_kinds != {"STANDARD"}:
            # 只有数据库权威股票回填获得专用私有语义；其他 legacy kind 继续 fail-closed。
            raise OperationProblem(
                status=422,
                code="legacy-intent-unsupported",
                detail="Legacy intent is not enabled for this system command",
            )
        preflight = self.preflight(normalized_targets)
        actor_ref = (
            "system:equity-backfill" if intent_kinds == {"EQUITY_BACKFILL"} else "system:legacy"
        )
        actor = self._actor({"actor": {"actorRef": actor_ref, "role": "SYSTEM", "reason": reason}})
        return self._submit_validated_command(
            targets=normalized_targets,
            submission_id=submission_id,
            preflight_id=UUID(str(preflight["preflightId"])),
            request_hash=str(preflight["requestHash"]),
            actor=actor,
            idempotency_key=idempotency_key,
            request_id=request_id,
            # 预检 UUID 每次重试都会变化，不能进入内部幂等摘要，否则 Celery 重投会误报冲突。
            operation_hash=self._hash(
                {
                    "submissionId": str(submission_id),
                    "targets": normalized_targets,
                    "executionIntents": normalized_intents,
                    "reason": actor["reason"],
                }
            ),
            execution_intents=normalized_intents,
        )

    def _submit_validated_command(
        self,
        *,
        targets: list[dict[str, Any]],
        submission_id: UUID,
        preflight_id: UUID,
        request_hash: str,
        actor: dict[str, str],
        idempotency_key: str,
        request_id: str,
        operation_hash: str,
        execution_intents: tuple[dict[str, Any] | None, ...],
    ) -> dict[str, Any]:
        """在同一事务写 command、child runs 和幂等账本，私有意图不进入 HTTP target。"""
        if len(execution_intents) != len(targets):
            raise ValueError("execution intents must align with targets")
        now = self._now()
        with self._database.transaction() as session:
            replay = self._idempotent_resource(
                session, "command-submit", idempotency_key, operation_hash
            )
            if replay is not None:
                return replay.response_json
            preflight = session.get(DataOperationPreflight, preflight_id)
            if preflight is None or preflight.expires_at <= now:
                raise OperationProblem(
                    status=409, code="preflight-expired", detail="Sync preflight has expired"
                )
            if preflight.request_hash != request_hash or preflight.targets_json != targets:
                raise OperationProblem(
                    status=409,
                    code="preflight-mismatch",
                    detail="Sync request does not match preflight",
                )
            if not all(bool(item.get("eligible")) for item in preflight.result_json):
                raise OperationProblem(
                    status=422, code="preflight-rejected", detail="Sync target is not eligible"
                )
            command = DataOperationCommand(
                command_id=uuid4(),
                submission_id=submission_id,
                status="QUEUED",
                actor_ref=actor["actorRef"],
                actor_role=actor["role"],
                reason=actor["reason"],
                request_id=request_id,
                retry_of_command_id=None,
                error_json=None,
                requested_at=now,
                started_at=None,
                finished_at=None,
            )
            session.add(command)
            queue_base = int(
                session.scalar(
                    select(func.count())
                    .select_from(DataOperationRun)
                    .where(DataOperationRun.status == "QUEUED")
                )
                or 0
            )
            runs: list[DataOperationRun] = []
            equity_backfill_intents: list[dict[str, Any]] = []
            for index, target in enumerate(targets):
                definition = self._definition(target["datasetCode"])
                preflight_result = preflight.result_json[index]
                source_snapshot = self._source_snapshot(definition, target=target)
                resolved_execution_intent = (
                    execution_intents[index]
                    if execution_intents[index] is not None
                    else self._execution_intent_from_preflight(
                        target=target,
                        result=preflight_result,
                    )
                )
                if (
                    resolved_execution_intent is not None
                    and resolved_execution_intent.get("kind") == "EQUITY_BACKFILL"
                ):
                    self._assert_equity_backfill_run_binding(
                        session,
                        target=target,
                        intent=resolved_execution_intent,
                        submission_id=submission_id,
                        source_snapshot=source_snapshot,
                    )
                    equity_backfill_intents.append(resolved_execution_intent)
                run = DataOperationRun(
                    run_id=uuid4(),
                    command_id=command.command_id,
                    target_index=index,
                    dataset_code=definition.dataset_code,
                    mode=target["mode"],
                    target_json=target,
                    source_snapshot=source_snapshot,
                    execution_intent_json=resolved_execution_intent,
                    status="QUEUED",
                    queue_position=queue_base + index + 1,
                    attempt=0,
                    recovery_attempts=0,
                    completed_partitions=0,
                    total_partitions=int(preflight_result["estimatedPartitions"]),
                    processed_records=0,
                    estimated_records=None,
                    fencing_token=None,
                    cancel_requested=False,
                    error_json=None,
                    quality_gate_json=self._not_evaluated_gate(),
                    requested_at=now,
                    started_at=None,
                    finished_at=None,
                )
                session.add(run)
                runs.append(run)
            if equity_backfill_intents:
                self._bind_equity_backfill_command(
                    session,
                    intents=equity_backfill_intents,
                    command_id=command.command_id,
                    submitted_at=now,
                )
            self._record_event(
                session,
                "COMMAND",
                command.command_id,
                "SUBMIT",
                "ACCEPTED",
                actor["actorRef"],
                request_id,
                None,
            )
            session.flush()
            receipt = self._command_receipt(
                session,
                command.command_id,
                submission_id,
                target={"resourceType": "COMMAND", "resourceId": str(command.command_id)},
                target_status="QUEUED",
            )
            self._record_idempotency(
                session,
                "command-submit",
                idempotency_key,
                operation_hash,
                "COMMAND",
                command.command_id,
                receipt,
                now,
            )
            return receipt

    def _bind_equity_backfill_command(
        self,
        session: Session,
        *,
        intents: Sequence[dict[str, Any]],
        command_id: UUID,
        submitted_at: datetime,
    ) -> None:
        """在 command 可见前原子绑定唯一 frozen child，消除 worker 抢跑窗口。"""
        plan_child_keys = {(str(intent["planId"]), str(intent["childKey"])) for intent in intents}
        if len(plan_child_keys) != 1:
            raise OperationProblem(
                status=409,
                code="equity-backfill-precondition-failed",
                detail="One command must bind exactly one frozen equity backfill child",
            )
        plan_id_text, child_key = next(iter(plan_child_keys))
        plan_id = UUID(plan_id_text)
        child = session.scalar(
            select(EquityBackfillChildSpec)
            .where(
                EquityBackfillChildSpec.plan_id == plan_id,
                EquityBackfillChildSpec.child_key == child_key,
            )
            .with_for_update()
        )
        if child is None:
            raise OperationProblem(
                status=409,
                code="equity-backfill-precondition-failed",
                detail="Frozen equity backfill child does not exist",
            )
        state = session.get(EquityBackfillChildState, child.child_id, with_for_update=True)
        if state is None or state.status != "SUBMITTING" or state.command_id is not None:
            raise OperationProblem(
                status=409,
                code="equity-backfill-precondition-failed",
                detail="Frozen equity backfill child is not ready for atomic command binding",
            )
        state.command_id = command_id
        state.status = "SUBMITTED"
        state.submitted_at = submitted_at
        state.updated_at = submitted_at

    def command_detail(self, command_id: UUID) -> dict[str, Any]:
        """读取 command 与按 target_index 稳定排序的 child runs。"""
        with self._database.session() as session:
            command = session.get(DataOperationCommand, command_id)
            if command is None:
                raise OperationProblem(
                    status=404, code="command-not-found", detail="Data sync command is not found"
                )
            runs = session.scalars(
                select(DataOperationRun)
                .where(DataOperationRun.command_id == command_id)
                .order_by(DataOperationRun.target_index)
            ).all()
            return {
                "commandId": str(command.command_id),
                "submissionId": self._uuid_text(command.submission_id),
                "status": command.status,
                "requestedAt": self._iso(command.requested_at),
                "startedAt": self._iso(command.started_at),
                "finishedAt": self._iso(command.finished_at),
                "actorRef": command.actor_ref,
                "childRuns": [self._run_summary(run) for run in runs],
                "error": command.error_json,
            }

    def cancel_command(
        self, *, request: dict[str, Any], idempotency_key: str, request_id: str
    ) -> dict[str, Any]:
        """请求取消 command 或单一 run；终态目标产生 cancel_too_late 事件但不篡改真实终态。"""
        self._require_idempotency_key(idempotency_key)
        actor = self._actor(request)
        submission_id = self._uuid_field(request, "submissionId")
        target = self._action_target(request)
        operation_hash = self._hash(request)
        now = self._now()
        with self._database.transaction() as session:
            replay = self._idempotent_resource(
                session, "command-cancel", idempotency_key, operation_hash
            )
            if replay is not None:
                return replay.response_json
            command_id, target_status = self._request_cancel(
                session, target, actor, request_id, now
            )
            receipt = self._command_receipt(
                session, command_id, submission_id, target=target, target_status=target_status
            )
            self._record_idempotency(
                session,
                "command-cancel",
                idempotency_key,
                operation_hash,
                "COMMAND",
                command_id,
                receipt,
                now,
            )
            return receipt

    def retry_command(
        self, *, request: dict[str, Any], idempotency_key: str, request_id: str
    ) -> dict[str, Any]:
        """基于可重试终态复制目标为新的 command，绝不复用原 run。"""
        self._require_idempotency_key(idempotency_key)
        actor = self._actor(request)
        submission_id = self._uuid_field(request, "submissionId")
        target = self._action_target(request)
        operation_hash = self._hash(request)
        now = self._now()
        with self._database.transaction() as session:
            replay = self._idempotent_resource(
                session, "command-retry", idempotency_key, operation_hash
            )
            if replay is not None:
                return replay.response_json
            originals, original_command = self._retryable_runs(session, target)
            if not originals:
                raise OperationProblem(
                    status=422, code="nothing-retryable", detail="Target has no retryable run"
                )
            originals = self._expand_equity_backfill_retry_runs(
                session,
                retryable_runs=originals,
                original_command_id=original_command,
            )
            backfill_state = self._equity_backfill_retry_state(
                session,
                originals=originals,
                original_command_id=original_command,
                submission_id=submission_id,
            )
            command = DataOperationCommand(
                command_id=uuid4(),
                submission_id=submission_id,
                status="QUEUED",
                actor_ref=actor["actorRef"],
                actor_role=actor["role"],
                reason=actor["reason"],
                request_id=request_id,
                retry_of_command_id=original_command,
                error_json=None,
                requested_at=now,
                started_at=None,
                finished_at=None,
            )
            session.add(command)
            for index, old in enumerate(originals):
                new_run_id = uuid4()
                inherited = self._retry_partitions(session, old)
                session.add(
                    DataOperationRun(
                        run_id=new_run_id,
                        command_id=command.command_id,
                        target_index=index,
                        dataset_code=old.dataset_code,
                        mode=old.mode,
                        target_json=old.target_json,
                        source_snapshot=old.source_snapshot,
                        execution_intent_json=old.execution_intent_json,
                        status="QUEUED",
                        queue_position=None,
                        attempt=0,
                        recovery_attempts=0,
                        completed_partitions=len(inherited),
                        total_partitions=max(old.total_partitions, 1),
                        processed_records=0,
                        estimated_records=old.estimated_records,
                        fencing_token=None,
                        cancel_requested=False,
                        error_json=None,
                        quality_gate_json=self._not_evaluated_gate(),
                        requested_at=now,
                        started_at=None,
                        finished_at=None,
                    )
                )
                for partition in inherited:
                    session.add(
                        DataOperationPartition(
                            run_id=new_run_id,
                            partition_key=partition.partition_key,
                            status="SUCCEEDED",
                            attempt=0,
                            checkpoint_hash=partition.checkpoint_hash,
                            checkpoint_kind=partition.checkpoint_kind,
                            checkpoint_updated_at=partition.checkpoint_updated_at,
                            error_json=None,
                        )
                    )
            if backfill_state is not None:
                backfill_state.command_id = command.command_id
                backfill_state.status = "SUBMITTED"
                backfill_state.resume_count += 1
                backfill_state.last_error_json = None
                backfill_state.audit_json = None
                backfill_state.submitted_at = now
                backfill_state.finished_at = None
                backfill_state.updated_at = now
            self._record_event(
                session,
                "COMMAND",
                command.command_id,
                "RETRY",
                "ACCEPTED",
                actor["actorRef"],
                request_id,
                None,
            )
            session.flush()
            old_status = self._target_status(session, target)
            receipt = self._command_receipt(
                session, command.command_id, submission_id, target=target, target_status=old_status
            )
            self._record_idempotency(
                session,
                "command-retry",
                idempotency_key,
                operation_hash,
                "COMMAND",
                command.command_id,
                receipt,
                now,
            )
            return receipt

    def _equity_backfill_retry_state(
        self,
        session: Session,
        *,
        originals: Sequence[DataOperationRun],
        original_command_id: UUID,
        submission_id: UUID,
    ) -> EquityBackfillChildState | None:
        """验证重试仍属于同一冻结 child，并返回可原子改绑的新尝试状态行。"""
        intents = [
            run.execution_intent_json
            for run in originals
            if isinstance(run.execution_intent_json, dict)
            and run.execution_intent_json.get("kind") == "EQUITY_BACKFILL"
        ]
        if not intents:
            return None
        child_keys = {str(intent["childKey"]) for intent in intents}
        plan_ids = {UUID(str(intent["planId"])) for intent in intents}
        if len(intents) != len(originals) or len(child_keys) != 1 or len(plan_ids) != 1:
            raise OperationProblem(
                status=409,
                code="equity-backfill-retry-mismatch",
                detail="Retry target crosses frozen equity backfill child boundaries",
            )
        child = session.scalar(
            select(EquityBackfillChildSpec).where(
                EquityBackfillChildSpec.plan_id == next(iter(plan_ids)),
                EquityBackfillChildSpec.child_key == next(iter(child_keys)),
            )
        )
        if child is None or child.submission_id != submission_id:
            raise OperationProblem(
                status=409,
                code="equity-backfill-retry-mismatch",
                detail="Retry submission does not match the frozen equity backfill child",
            )
        state = session.get(EquityBackfillChildState, child.child_id, with_for_update=True)
        if (
            state is None
            or state.command_id != original_command_id
            or state.status not in {"PARTIAL", "FAILED", "CANCELLED"}
        ):
            raise OperationProblem(
                status=409,
                code="equity-backfill-retry-mismatch",
                detail="Equity backfill child is not retryable from its current state",
            )
        return state

    def _expand_equity_backfill_retry_runs(
        self,
        session: Session,
        *,
        retryable_runs: Sequence[DataOperationRun],
        original_command_id: UUID,
    ) -> list[DataOperationRun]:
        """股票 child 任一 target 失败时复制完整 target 组，保持冻结索引与结果清单完整。

        普通 command 继续只复制失败 run。股票 child 的多个 target 共享一个不可变 command
        规格，若只复制失败子集会把原 targetIndex 压缩并使 append-only 结果缺项；完整重放
        虽可能重复已成功的幂等抓取，但能维持同一来源、意图、依赖和审计边界。
        """
        has_backfill = any(
            isinstance(run.execution_intent_json, dict)
            and run.execution_intent_json.get("kind") == "EQUITY_BACKFILL"
            for run in retryable_runs
        )
        if not has_backfill:
            return list(retryable_runs)
        all_runs = list(
            session.scalars(
                select(DataOperationRun)
                .where(DataOperationRun.command_id == original_command_id)
                .order_by(DataOperationRun.target_index)
            ).all()
        )
        if not all_runs or any(
            not isinstance(run.execution_intent_json, dict)
            or run.execution_intent_json.get("kind") != "EQUITY_BACKFILL"
            or run.status not in {"SUCCEEDED", "PARTIAL", "FAILED", "CANCELLED", "INTERRUPTED"}
            for run in all_runs
        ):
            raise OperationProblem(
                status=409,
                code="equity-backfill-retry-mismatch",
                detail="Equity backfill retry must reproduce one complete terminal child",
            )
        if [run.target_index for run in all_runs] != list(range(len(all_runs))):
            raise OperationProblem(
                status=409,
                code="equity-backfill-retry-mismatch",
                detail="Equity backfill target indexes are not contiguous",
            )
        return all_runs

    def _retry_partitions(
        self,
        session: Session,
        run: DataOperationRun,
    ) -> tuple[DataOperationPartition, ...]:
        """只为原样冻结的长批次继承成功分区，失败分区必须在新 run 中重做。"""
        selector = run.target_json.get("selector")
        etf_all = (
            run.dataset_code
            in {
                "fund.etf.bar.1d.reported",
                "fund.etf.nav.1d.reported",
                "fund.etf.trading_state.reported",
            }
            and isinstance(selector, dict)
            and selector.get("scope") == "ALL_ETFS"
        )
        etf_all_venues = (
            run.dataset_code == "fund.etf.profile.reported"
            and isinstance(selector, dict)
            and selector.get("scope") == "ALL_VENUES"
        )
        stock_connect = (
            run.dataset_code == "market.stock_connect.overview.bundle"
            and isinstance(selector, dict)
            and selector.get("kind") == "STOCK_CONNECT"
        )
        akshare_batched = run.dataset_code in _AKSHARE_BATCHED_DATASETS
        if (
            run.dataset_code != "equity.share_capital.reported"
            and not etf_all
            and not etf_all_venues
            and not stock_connect
            and not akshare_batched
        ):
            return ()
        # `retry_command` 不接受新的 target 或来源；若未来放宽该合同，调用方必须先增加
        # 结构等价校验，不能把旧来源的水位继承给不同快照。
        partition_prefix = (
            "etf:"
            if etf_all
            else "venue:"
            if etf_all_venues
            else "stock-connect:"
            if stock_connect
            else "margin:"
            if run.dataset_code
            in {
                "market.margin.market.1d.reported",
                "market.margin.security.1d.reported",
                "market.margin.eligibility.reported",
            }
            else "derivative:"
            if run.dataset_code == "derivative.bar.1d.reported"
            else "sector-bar:"
            if run.dataset_code in _SECTOR_BAR_DATASETS
            else "security:"
        )
        checkpoint_kind = "stock-connect-bundle" if stock_connect else "canonical-partition"
        return tuple(
            session.scalars(
                select(DataOperationPartition).where(
                    DataOperationPartition.run_id == run.run_id,
                    DataOperationPartition.status == "SUCCEEDED",
                    DataOperationPartition.checkpoint_kind == checkpoint_kind,
                    DataOperationPartition.partition_key.startswith(partition_prefix),
                )
            ).all()
        )

    def list_runs(self, request: dict[str, Any]) -> dict[str, Any]:
        """分页检索 run；cursor 仅绑定 run 列表，不与分区或 timeline 复用。"""
        limit = self._validate_limit(request.get("limit", 50), maximum=200)
        with self._database.session() as session:
            statement = select(DataOperationRun).order_by(
                DataOperationRun.requested_at.desc(), DataOperationRun.run_id.desc()
            )
            if codes := request.get("datasetCodes"):
                statement = statement.where(DataOperationRun.dataset_code.in_(codes))
            if statuses := request.get("statuses"):
                statement = statement.where(DataOperationRun.status.in_(statuses))
            rows = session.scalars(statement).all()
            offset = self._decode_offset(request.get("cursor"))
            visible = rows[offset : offset + limit]
            next_cursor = (
                self._encode_offset(offset + limit) if offset + limit < len(rows) else None
            )
            return {"items": [self._run_summary(row) for row in visible], "nextCursor": next_cursor}

    def run_detail(self, request: dict[str, Any]) -> dict[str, Any]:
        """读取 run、独立 partition/timeline cursor 页与受控 checkpoint 摘要。"""
        run_id = self._uuid_field(request, "runId")
        partitions_limit = self._validate_limit(request.get("partitionsLimit", 100), maximum=200)
        timeline_limit = self._validate_limit(request.get("timelineLimit", 100), maximum=200)
        with self._database.session() as session:
            run = session.get(DataOperationRun, run_id)
            if run is None:
                raise OperationProblem(
                    status=404, code="run-not-found", detail="Data sync run is not found"
                )
            partitions = session.scalars(
                select(DataOperationPartition)
                .where(DataOperationPartition.run_id == run_id)
                .order_by(DataOperationPartition.partition_key)
            ).all()
            events = session.scalars(
                select(DataOperationEvent)
                .where(
                    DataOperationEvent.resource_type == "RUN",
                    DataOperationEvent.resource_id == run_id,
                )
                .order_by(DataOperationEvent.occurred_at.desc(), DataOperationEvent.event_id.desc())
            ).all()
            partition_offset = self._decode_offset(request.get("partitionsCursor"))
            timeline_offset = self._decode_offset(request.get("timelineCursor"))
            visible_partitions = partitions[partition_offset : partition_offset + partitions_limit]
            visible_events = events[timeline_offset : timeline_offset + timeline_limit]
            command = session.get(DataOperationCommand, run.command_id)
            assert command is not None
            return {
                "run": self._run_summary(run),
                "target": run.target_json,
                "sourceSnapshot": run.source_snapshot,
                "attempt": run.attempt,
                "actorRef": command.actor_ref,
                "qualityGate": run.quality_gate_json,
                "partitionCount": len(partitions),
                "partitions": [self._partition_view(item) for item in visible_partitions],
                "partitionsNextCursor": self._encode_offset(partition_offset + partitions_limit)
                if partition_offset + partitions_limit < len(partitions)
                else None,
                "timelineEventCount": len(events),
                "timeline": [self._event_view(item) for item in visible_events],
                "timelineNextCursor": self._encode_offset(timeline_offset + timeline_limit)
                if timeline_offset + timeline_limit < len(events)
                else None,
                "fencingToken": run.fencing_token,
            }

    def claim_next_run(self, worker_id: str) -> ExecutionClaim | None:
        """用 PostgreSQL 行锁与租约取得全局唯一执行槽，绝不依赖进程或 Redis 锁。"""
        self._require_string({"worker": worker_id}, "worker", max_length=128)
        now = self._now()
        with self._database.transaction() as session:
            slot = self._ensure_slot(session, lock=True)
            if slot.state != "IDLE":
                if slot.lease_until is None or slot.lease_until > now:
                    return None
                self._reap_locked_slot(session, slot, now)
            active_backfill_plan_ids = tuple(
                session.scalars(
                    select(EquityBackfillPlanState.plan_id).where(
                        EquityBackfillPlanState.status == "RUNNING"
                    )
                ).all()
            )
            if len(active_backfill_plan_ids) > 1:
                raise RuntimeError("multiple equity backfill plans are running")
            run_statement = select(DataOperationRun).where(DataOperationRun.status == "QUEUED")
            if active_backfill_plan_ids:
                active_plan_id = active_backfill_plan_ids[0]
                # 全量回填期间只消费同一冻结计划的 child；普通命令仍可排队但不能
                # 在 discovery 之前推进 current publication，计划终态后会恢复正常公平队列。
                run_statement = run_statement.where(
                    DataOperationRun.execution_intent_json["kind"].astext == "EQUITY_BACKFILL",
                    DataOperationRun.execution_intent_json["planId"].astext == str(active_plan_id),
                )
            run = session.scalars(
                run_statement.order_by(
                    DataOperationRun.requested_at,
                    DataOperationRun.target_index,
                )
                .with_for_update(skip_locked=True)
                .limit(1)
            ).first()
            if run is None:
                return None
            command = session.get(DataOperationCommand, run.command_id, with_for_update=True)
            assert command is not None
            if command.status in _TERMINAL_COMMANDS or run.cancel_requested:
                run.status = "CANCELLED"
                run.finished_at = now
                self._refresh_command_status(session, command, now)
                return None
            token = slot.fencing_token + 1
            slot.state = "RUNNING"
            slot.run_id = run.run_id
            slot.dataset_code = run.dataset_code
            slot.lease_until = now + timedelta(seconds=_LEASE_SECONDS)
            slot.heartbeat_at = now
            slot.fencing_token = token
            run.status = "RUNNING"
            run.fencing_token = token
            run.started_at = now
            run.attempt += 1
            run.queue_position = None
            if command.started_at is None:
                command.started_at = now
            command.status = "RUNNING"
            input_manifest = self._claim_equity_backfill_input_manifest(
                session,
                run=run,
                command=command,
                now=now,
            )
            self._record_event(
                session,
                "RUN",
                run.run_id,
                "START",
                "STARTED",
                command.actor_ref,
                command.request_id,
                None,
            )
            return ExecutionClaim(
                run_id=run.run_id,
                dataset_code=run.dataset_code,
                fencing_token=token,
                target=run.target_json,
                source_snapshot=run.source_snapshot,
                execution_intent=run.execution_intent_json,
                input_manifest=input_manifest,
                attempt=run.attempt,
            )

    def heartbeat(self, *, run_id: UUID, fencing_token: int) -> bool:
        """仅在当前 slot 与 token 完全一致时续租；陈旧 worker 无法复活。"""
        now = self._now()
        with self._database.transaction() as session:
            slot = self._ensure_slot(session, lock=True)
            if (
                slot.state != "RUNNING"
                or slot.run_id != run_id
                or slot.fencing_token != fencing_token
                or slot.lease_until is None
                or slot.lease_until <= now
            ):
                return False
            slot.heartbeat_at = now
            slot.lease_until = now + timedelta(seconds=_LEASE_SECONDS)
            return True

    def _claim_equity_backfill_input_manifest(
        self,
        session: Session,
        *,
        run: DataOperationRun,
        command: DataOperationCommand,
        now: datetime,
    ) -> tuple[dict[str, Any], ...]:
        """原子推进 child，并从 append-only 依赖结果解析精确输入版本。"""
        intent = getattr(run, "execution_intent_json", None)
        if not isinstance(intent, dict) or intent.get("kind") != "EQUITY_BACKFILL":
            return ()
        plan_id = UUID(str(intent["planId"]))
        child_key = str(intent["childKey"])
        child = session.scalar(
            select(EquityBackfillChildSpec).where(
                EquityBackfillChildSpec.plan_id == plan_id,
                EquityBackfillChildSpec.child_key == child_key,
            )
        )
        if child is None:
            raise EquityBackfillPreconditionFailed(
                "Frozen equity backfill child does not exist at claim"
            )
        state = session.get(EquityBackfillChildState, child.child_id, with_for_update=True)
        if (
            state is None
            or state.command_id != command.command_id
            or state.status not in {"SUBMITTED", "RUNNING"}
        ):
            raise EquityBackfillPreconditionFailed(
                "Frozen equity backfill child command is not atomically bound"
            )
        state.status = "RUNNING"
        state.updated_at = now
        source = session.get(EquityBackfillPlanSource, (plan_id, run.dataset_code))
        if source is None or source.source_kind != "INTERNAL_EXECUTOR":
            return ()

        dependency_keys = set(child.dependency_keys_json)
        dependency_keys.update(child.completion_dependency_keys_json)
        if child.phase == "DISCOVERY_BUILD":
            phase_index = PHASES.index(child.phase)
            dependency_specs = session.scalars(
                select(EquityBackfillChildSpec)
                .where(
                    EquityBackfillChildSpec.plan_id == plan_id,
                    EquityBackfillChildSpec.phase.in_(PHASES[:phase_index]),
                )
                .order_by(EquityBackfillChildSpec.ordinal)
            ).all()
        elif dependency_keys:
            dependency_specs = session.scalars(
                select(EquityBackfillChildSpec)
                .where(
                    EquityBackfillChildSpec.plan_id == plan_id,
                    EquityBackfillChildSpec.child_key.in_(dependency_keys),
                )
                .order_by(EquityBackfillChildSpec.ordinal)
            ).all()
        else:
            dependency_specs = []
        dependency_ids = [spec.child_id for spec in dependency_specs]
        dependency_states = (
            session.scalars(
                select(EquityBackfillChildState).where(
                    EquityBackfillChildState.child_id.in_(dependency_ids)
                )
            ).all()
            if dependency_ids
            else []
        )
        state_by_child = {state.child_id: state for state in dependency_states}
        results = (
            session.scalars(
                select(EquityBackfillChildResult)
                .where(EquityBackfillChildResult.child_id.in_(dependency_ids))
                .order_by(
                    EquityBackfillChildResult.child_id,
                    EquityBackfillChildResult.target_index,
                )
            ).all()
            if dependency_ids
            else []
        )
        results_by_child: dict[UUID, list[EquityBackfillChildResult]] = {}
        for result in results:
            state = state_by_child.get(result.child_id)
            if state is not None and result.command_id == state.command_id:
                results_by_child.setdefault(result.child_id, []).append(result)
        manifests: list[dict[str, Any]] = []
        for spec in dependency_specs:
            dependency_state = state_by_child.get(spec.child_id)
            if (
                dependency_state is not None
                and dependency_state.status == "BLOCKED"
                and dependency_state.command_id is None
            ):
                for target_index in range(spec.target_count):
                    manifests.append(
                        {
                            "childKey": spec.child_key,
                            "targetIndex": target_index,
                            "terminalStatus": "BLOCKED",
                            "outputManifestHash": self._hash([]),
                            "outputs": [],
                        }
                    )
                continue
            child_results = results_by_child.get(spec.child_id, [])
            if len(child_results) != spec.target_count:
                raise EquityBackfillPreconditionFailed(
                    "Dependency child has no complete append-only result manifest"
                )
            for result in child_results:
                manifests.append(
                    {
                        "childKey": spec.child_key,
                        "targetIndex": result.target_index,
                        "terminalStatus": result.terminal_status,
                        "outputManifestHash": result.output_manifest_hash,
                        "outputs": result.output_manifest_json,
                    }
                )
        if child.phase == "DISCOVERY_BUILD":
            plan = session.get(EquityBackfillPlan, plan_id)
            if plan is None:
                raise EquityBackfillPreconditionFailed(
                    "Frozen equity backfill plan does not exist at claim"
                )
            manifests.insert(
                0,
                {
                    "kind": "REFERENCE_BUNDLE",
                    "publicationId": str(plan.reference_bundle_publication_id),
                    "dataVersion": str(plan.reference_bundle_data_version),
                    "manifestHash": plan.reference_manifest_hash,
                    "components": plan.reference_manifest_json,
                },
            )
        return tuple(manifests)

    def complete_run(
        self,
        *,
        run_id: UUID,
        fencing_token: int,
        outcome: ExecutionOutcome,
        source_batch_ids: tuple[UUID, ...] = (),
    ) -> bool:
        """同一事务校验 fencing、提交终态、checkpoint、质量门和 slot 释放。

        此处是 stale worker 的最终防线：token 不匹配、槽已超时或归属变化时返回 false，
        不写 publication 结果、checkpoint、分区或终态。
        """
        now = self._now()
        with self._database.transaction() as session:
            return self._complete_run_in_session(
                session,
                run_id=run_id,
                fencing_token=fencing_token,
                outcome=outcome,
                source_batch_ids=source_batch_ids,
                now=now,
            )

    def _complete_run_in_session(
        self,
        session: Session,
        *,
        run_id: UUID,
        fencing_token: int,
        outcome: ExecutionOutcome,
        source_batch_ids: tuple[UUID, ...],
        now: datetime,
    ) -> bool:
        """在调用方已有事务内验证 fencing 并同时完成 run、分区、健康与 slot 状态。"""
        slot = self._ensure_slot(session, lock=True)
        if (
            slot.state != "RUNNING"
            or slot.run_id != run_id
            or slot.fencing_token != fencing_token
            or slot.lease_until is None
            or slot.lease_until <= now
        ):
            return False
        run = session.get(DataOperationRun, run_id, with_for_update=True)
        if (
            run is None
            or run.status not in {"RUNNING", "CANCEL_REQUESTED"}
            or run.fencing_token != fencing_token
        ):
            return False
        command = session.get(DataOperationCommand, run.command_id, with_for_update=True)
        assert command is not None
        final_status = (
            "CANCELLED"
            if run.cancel_requested and outcome.status in {"SUCCEEDED", "FAILED", "CANCELLED"}
            else outcome.status
        )
        if final_status not in {"FAILED", "CANCELLED"}:
            # 来源批次血缘只对会产生成功产物的终态有意义：执行器失败时其 canonical
            # 事务已回滚，但 FencedExecution 内存中的批次 ID 不会随事务回滚，是
            # 不可达的幽灵引用；在这里严格校验会让失败终态永远无法落账，只能等待
            # 租约过期被 reaper 收尾，并把真实失败原因掩盖成 lease-expired。
            self._record_run_source_batches(
                session,
                run_id=run_id,
                source_batch_ids=source_batch_ids,
                linked_at=now,
            )
        if outcome.status == "YIELDED":
            # 长回填只完成一个内部公平批次：保留同一 run 和分区水位，移到队尾后释放全局槽。
            run.status = "QUEUED"
            run.completed_partitions = max(0, outcome.completed_partitions)
            run.total_partitions = max(0, outcome.total_partitions)
            run.processed_records = max(0, run.processed_records) + max(
                0, outcome.processed_records
            )
            run.estimated_records = outcome.estimated_records
            run.error_json = None
            run.quality_gate_json = self._not_evaluated_gate()
            run.finished_at = None
            run.fencing_token = None
            run.requested_at = now
            run.queue_position = (
                int(
                    session.scalar(
                        select(func.count())
                        .select_from(DataOperationRun)
                        .where(DataOperationRun.status == "QUEUED")
                    )
                    or 0
                )
                + 1
            )
            self._record_event(
                session,
                "RUN",
                run.run_id,
                "BATCH_YIELD",
                "QUEUED",
                command.actor_ref,
                command.request_id,
                None,
            )
            self._refresh_command_status(session, command, now)
            slot.state = "IDLE"
            slot.run_id = None
            slot.dataset_code = None
            slot.lease_until = None
            slot.heartbeat_at = None
            return True
        final_error = outcome.error
        quality_gate = outcome.quality_gate
        if (
            final_status == "SUCCEEDED"
            and quality_gate is not None
            and quality_gate.get("disposition") == "BLOCKED"
        ):
            # 非 canonical 执行器也不能把明确阻断的质量门伪装为成功；canonical 路径会在
            # publication 所在事务内更早抛出，使数据库一起回滚。
            final_status = "FAILED"
            final_error = quality_gate.get("error") or self._error(
                "ingestion-quality-blocked",
                "QUALITY_GATE",
                False,
                "Ingestion quality gate blocked publication",
            )
        if final_status == "SUCCEEDED" and self._is_equity_backfill_run(run):
            try:
                self._require_equity_backfill_output_publication(
                    session,
                    run=run,
                    checkpoint_kind=outcome.checkpoint_kind,
                    checkpoint_position=outcome.checkpoint_position,
                    source_batch_ids=source_batch_ids,
                )
            except EquityBackfillPreconditionFailed:
                final_status = "FAILED"
                final_error = self._error(
                    "equity-backfill-output-binding-missing",
                    "PERSIST",
                    False,
                    "Equity backfill run produced no exact canonical publication",
                )
                quality_gate = self._failed_gate(final_error)
        if self._should_auto_retry_etf_run(
            run,
            final_status=final_status,
            error=final_error,
        ):
            run.status = "QUEUED"
            run.completed_partitions = max(0, outcome.completed_partitions)
            run.total_partitions = max(0, outcome.total_partitions)
            run.processed_records = max(0, run.processed_records) + max(
                0, outcome.processed_records
            )
            run.estimated_records = outcome.estimated_records
            run.error_json = final_error
            run.quality_gate_json = self._not_evaluated_gate()
            run.finished_at = None
            run.fencing_token = None
            self._record_event(
                session,
                "RUN",
                run.run_id,
                "AUTO_RETRY",
                "QUEUED",
                command.actor_ref,
                command.request_id,
                final_error,
            )
            self._refresh_command_status(session, command, now)
            slot.state = "IDLE"
            slot.run_id = None
            slot.dataset_code = None
            slot.lease_until = None
            slot.heartbeat_at = None
            return True
        run.status = final_status
        run.completed_partitions = max(0, outcome.completed_partitions)
        run.total_partitions = max(0, outcome.total_partitions)
        run.processed_records = max(0, run.processed_records) + max(0, outcome.processed_records)
        run.estimated_records = outcome.estimated_records
        run.error_json = final_error
        run.quality_gate_json = (
            quality_gate or self._not_evaluated_gate()
            if final_status == "SUCCEEDED"
            else quality_gate or self._failed_gate(final_error)
        )
        run.finished_at = now
        has_explicit_batch_partitions = bool(
            session.scalar(
                select(func.count())
                .select_from(DataOperationPartition)
                .where(
                    DataOperationPartition.run_id == run_id,
                    or_(
                        DataOperationPartition.partition_key.startswith("etf:"),
                        DataOperationPartition.partition_key.startswith("venue:"),
                        DataOperationPartition.partition_key.startswith("stock-connect:"),
                        DataOperationPartition.partition_key.startswith("margin:"),
                        DataOperationPartition.partition_key.startswith("derivative:"),
                    ),
                )
            )
        )
        partition: DataOperationPartition | None = None
        if not has_explicit_batch_partitions:
            partition = session.get(
                DataOperationPartition, {"run_id": run_id, "partition_key": "default"}
            )
            if partition is None:
                partition = DataOperationPartition(
                    run_id=run_id,
                    partition_key="default",
                    status=final_status,
                    attempt=run.attempt,
                    checkpoint_hash=None,
                    checkpoint_kind=None,
                    checkpoint_updated_at=None,
                    error_json=final_error,
                )
                session.add(partition)
            else:
                partition.status = final_status
                partition.attempt = run.attempt
                partition.error_json = final_error
        if (
            partition is not None
            and final_status == "SUCCEEDED"
            and outcome.checkpoint_position is not None
        ):
            # 位置本身永不写入控制面，只有定长摘要与本 token 一起提交。
            position_hash = hashlib.sha256(outcome.checkpoint_position.encode()).hexdigest()
            partition.checkpoint_hash = position_hash
            partition.checkpoint_kind = outcome.checkpoint_kind or "opaque"
            partition.checkpoint_updated_at = now
        self._record_equity_backfill_child_result(
            session,
            run=run,
            command=command,
            terminal_status=final_status,
            outcome=outcome,
            source_batch_ids=source_batch_ids,
            now=now,
        )
        self._record_event(
            session,
            "RUN",
            run.run_id,
            "COMPLETE",
            final_status,
            command.actor_ref,
            command.request_id,
            final_error,
        )
        self._refresh_command_status(session, command, now)
        self._refresh_equity_backfill_child_state(session, command, now)
        slot.state = "IDLE"
        slot.run_id = None
        slot.dataset_code = None
        slot.lease_until = None
        slot.heartbeat_at = None
        if final_status == "SUCCEEDED" and run.quality_gate_json.get("disposition") in {
            "PASSED",
            "WARNED",
        }:
            self._record_automatic_health_evaluation(
                session,
                run,
                self._checkpoint_data_version(outcome.checkpoint_kind, outcome.checkpoint_position),
                now,
            )
        return True

    @staticmethod
    def _record_run_source_batches(
        session: Session,
        *,
        run_id: UUID,
        source_batch_ids: tuple[UUID, ...],
        linked_at: datetime,
    ) -> None:
        """在 run 完成事务内幂等封印实际来源，拒绝不存在的伪批次引用。"""
        for source_batch_id in sorted(set(source_batch_ids), key=str):
            source = session.get(SourceBatch, source_batch_id)
            if source is None:
                raise RuntimeError("data operation run references an unknown source batch")
            key = {"run_id": run_id, "source_batch_id": source_batch_id}
            if session.get(DataOperationRunSourceBatch, key) is None:
                session.add(
                    DataOperationRunSourceBatch(
                        run_id=run_id,
                        source_batch_id=source_batch_id,
                        linked_at=linked_at,
                    )
                )

    def _should_auto_retry_etf_run(
        self,
        run: DataOperationRun,
        *,
        final_status: str,
        error: dict[str, Any] | None,
    ) -> bool:
        """只续跑冻结全集的可重试失败，并以 run attempt 限制自动循环次数。"""
        selector = run.target_json.get("selector")
        return (
            final_status in {"FAILED", "PARTIAL"}
            and isinstance(error, dict)
            and error.get("retryable") is True
            and run.attempt < self._etf_auto_retry_max_attempts
            and run.dataset_code
            in {
                "fund.etf.bar.1d.reported",
                "fund.etf.nav.1d.reported",
                "fund.etf.trading_state.reported",
            }
            and isinstance(selector, dict)
            and selector.get("scope") == "ALL_ETFS"
        )

    def _finalize_canonical_publication(self, session: Session, execution: FencedExecution) -> None:
        """把 canonical publication/checkpoint 与成功 run 终态置于同一数据库提交。"""
        run = session.get(DataOperationRun, execution.run_id, with_for_update=True)
        if run is None:
            raise RuntimeError("fenced canonical publication has no run")
        try:
            self._validate_equity_backfill_run_in_session(
                session,
                run,
                finalizing=True,
                source_batch_ids=tuple(getattr(execution, "source_batch_ids", ())),
            )
        except OperationProblem as error:
            raise EquityBackfillPreconditionFailed(
                "Frozen equity backfill finalizer intent is invalid"
            ) from error
        self._validate_equity_backfill_output_publication(session, run, execution)
        quality_gate = self._ingestion_quality_gate(session, run, execution)
        if quality_gate["disposition"] == "BLOCKED":
            # 此异常穿过 DatabaseClient.transaction，使刚写入的 release/publication/checkpoint
            # 一并回滚；dispatcher 随后只把失败与质量门事实写入 run 账本。
            raise QualityGateBlocked(quality_gate)
        completed_partitions = max(1, execution.completed_partitions)
        completed = self._complete_run_in_session(
            session,
            run_id=execution.run_id,
            fencing_token=execution.fencing_token,
            outcome=ExecutionOutcome(
                status="SUCCEEDED",
                completed_partitions=completed_partitions,
                total_partitions=max(run.total_partitions, completed_partitions),
                processed_records=execution.processed_records,
                checkpoint_kind=execution.checkpoint_kind,
                checkpoint_position=execution.checkpoint_position,
                quality_gate=quality_gate,
            ),
            source_batch_ids=tuple(execution.source_batch_ids),
            now=self._now(),
        )
        if not completed:
            raise RuntimeError("fenced canonical publication lost its execution slot")

    def _checkpoint_data_version(self, kind: str | None, position: str | None) -> UUID | None:
        """仅把受控 data-version checkpoint 解析为 UUID，其他 opaque 位置绝不当版本使用。"""
        if kind != "data-version" or position is None:
            return None
        try:
            return UUID(position)
        except (TypeError, ValueError, AttributeError):
            return None

    def _ingestion_quality_gate(
        self, session: Session, run: DataOperationRun, execution: FencedExecution
    ) -> dict[str, Any]:
        """依据同事务写入的真实 publication 与 canonical 质量事实裁决发布前质量门。"""
        publication = self._publication_for_checkpoint(
            session,
            run,
            checkpoint_kind=execution.checkpoint_kind,
            checkpoint_position=execution.checkpoint_position,
        )
        if publication is None:
            if execution.checkpoint_kind in {
                "data-version",
                "bar-coverage-version",
                "event-coverage-version",
            }:
                return self._failed_gate(
                    self._error(
                        "publication-binding-missing",
                        "QUALITY_GATE",
                        False,
                        "Published checkpoint cannot be bound to the current run",
                    )
                )
            # 无受控输出 checkpoint 的幂等空跑没有候选 publication，不能伪装成已通过质量门。
            return self._not_evaluated_gate()
        return self._quality_gate_for_publication(session, publication)

    def _publication_for_checkpoint(
        self,
        session: Session,
        run: DataOperationRun,
        *,
        checkpoint_kind: str | None,
        checkpoint_position: str | None,
    ) -> DatasetPublication | None:
        """把受控 dataVersion、行情或事件 coverage checkpoint 解析为精确 publication。"""
        data_version = self._checkpoint_data_version(checkpoint_kind, checkpoint_position)
        if data_version is not None:
            return self._publication_for_data_version(session, run.dataset_code, data_version)
        if checkpoint_kind == "event-coverage-version" and checkpoint_position is not None:
            intent = run.execution_intent_json
            if not isinstance(intent, dict):
                return None
            try:
                plan_id = UUID(str(intent["planId"]))
                coverage_version = UUID(checkpoint_position)
            except (KeyError, TypeError, ValueError, AttributeError):
                return None
            child = session.scalar(
                select(EquityBackfillChildSpec).where(
                    EquityBackfillChildSpec.plan_id == plan_id,
                    EquityBackfillChildSpec.child_key == str(intent.get("childKey")),
                )
            )
            if child is None:
                return None
            checkpoint = session.scalar(
                select(EquityBackfillPartitionCheckpoint).where(
                    EquityBackfillPartitionCheckpoint.child_id == child.child_id,
                    EquityBackfillPartitionCheckpoint.target_index == run.target_index,
                    EquityBackfillPartitionCheckpoint.coverage_version == coverage_version,
                    EquityBackfillPartitionCheckpoint.checkpoint_kind == "EVENT_COVERAGE_VERSION",
                )
            )
            return (
                None
                if checkpoint is None
                else session.get(DatasetPublication, checkpoint.publication_id)
            )
        if (
            checkpoint_kind != "bar-coverage-version"
            or run.dataset_code
            not in {
                "equity.bar.1d.raw",
                "equity.bar.1w.raw",
                "equity.bar.1mo.raw",
            }
            or checkpoint_position is None
        ):
            return None
        try:
            coverage_version = UUID(checkpoint_position)
        except (TypeError, ValueError, AttributeError):
            return None
        coverage = session.scalar(
            select(EquityBarWindowCoverage).where(
                EquityBarWindowCoverage.coverage_version == coverage_version
            )
        )
        if coverage is None or coverage.capability != run.dataset_code:
            return None
        return session.get(DatasetPublication, coverage.publication_id)

    def _quality_gate_for_publication(
        self, session: Session, publication: DatasetPublication
    ) -> dict[str, Any]:
        """将真实 publication 与 canonical QualityEvaluation 投影为当前候选的质量门摘要。"""
        publication_status = publication.quality_status.casefold()
        if publication_status == "partial":
            return self._failed_gate(
                self._error(
                    "ingestion-quality-blocked",
                    "QUALITY_GATE",
                    False,
                    "Publication quality is partial and cannot replace production data",
                )
            )
        if publication_status not in {"passed", "warned"}:
            return self._failed_gate(
                self._error(
                    "ingestion-quality-unknown",
                    "QUALITY_GATE",
                    False,
                    "Publication has no approved ingestion quality decision",
                )
            )
        if publication.release_id is None:
            # 兼容期 publication 仍可携带它自己的已批准质量状态；它不能用于 health 的
            # release 绑定，但也不能被控制面无条件判为通过。
            return self._warned_gate() if publication_status == "warned" else self._passed_gate()
        release = session.get(DatasetRelease, publication.release_id)
        if release is None:
            return self._failed_gate(
                self._error(
                    "release-binding-missing",
                    "QUALITY_GATE",
                    False,
                    "Publication release cannot be verified",
                )
            )
        evaluation = session.scalar(
            select(QualityEvaluation)
            .where(QualityEvaluation.normalization_run_id == release.normalization_run_id)
            .order_by(QualityEvaluation.evaluated_at.desc())
            .limit(1)
        )
        if evaluation is not None:
            status = evaluation.status.casefold()
            if status == "blocked":
                affected_count = sum(
                    item.affected_count
                    for item in session.scalars(
                        select(QualityResult).where(
                            QualityResult.evaluation_id == evaluation.evaluation_id,
                            QualityResult.passed.is_(False),
                        )
                    ).all()
                )
                gate = self._failed_gate(
                    self._error(
                        "ingestion-quality-blocked",
                        "QUALITY_GATE",
                        False,
                        "Ingestion quality gate blocked publication",
                    )
                )
                gate["affectedCount"] = affected_count
                return gate
            if status == "warned":
                return self._warned_gate()
            if status != "passed":
                return self._failed_gate(
                    self._error(
                        "ingestion-quality-unknown",
                        "QUALITY_GATE",
                        False,
                        "Canonical quality decision is not publishable",
                    )
                )
        return self._warned_gate() if publication_status == "warned" else self._passed_gate()

    def dispatch_once(self, worker_id: str) -> bool:
        """取得一个 run 后执行注入执行器，并始终通过 fencing 事务完成。"""
        claim = self.claim_next_run(worker_id)
        if claim is None:
            return False
        source_batch_ids: tuple[UUID, ...] = ()
        executor = self._executors.get(claim.dataset_code)
        precondition_failed = False
        try:
            self._assert_equity_backfill_execution_claim(claim)
        except EquityBackfillPreconditionFailed:
            precondition_failed = True
        if precondition_failed:
            # 此门发生在 heartbeat 和 executor 之前，身份或来源漂移不会产生任何 Provider 调用。
            outcome = ExecutionOutcome(
                status="FAILED",
                error=self._error(
                    "equity-backfill-precondition-failed",
                    "PRECONDITION",
                    False,
                    "Frozen equity backfill precondition failed",
                ),
            )
        elif executor is None:
            outcome = ExecutionOutcome(
                status="FAILED",
                error=self._error(
                    "executor-unavailable", "QUEUE", True, "No approved executor is registered"
                ),
            )
        else:
            execution = FencedExecution(
                database=self._database,
                run_id=claim.run_id,
                fencing_token=claim.fencing_token,
                finalizer=self._finalize_canonical_publication,
            )
            heartbeat_stop, heartbeat_thread = self._start_heartbeat(claim)
            try:
                with fenced_execution(execution):
                    outcome = executor(claim)
            except QualityGateBlocked as blocked:
                # 质量门在 canonical publication 的提交事务内阻断候选，不能以普通执行异常
                # 重试并意外发布；失败 run 保留同一脱敏 gate 摘要供 API 对账。
                execution.disarm_terminal_write()
                outcome = ExecutionOutcome(
                    status="FAILED",
                    quality_gate=blocked.gate,
                    error=blocked.gate["error"],
                )
            except EquityBackfillPreconditionFailed:
                execution.disarm_terminal_write()
                outcome = ExecutionOutcome(
                    status="FAILED",
                    error=self._error(
                        "equity-backfill-precondition-failed",
                        "PRECONDITION",
                        False,
                        "Frozen equity backfill precondition failed",
                    ),
                )
            except EquityWindowIdentityUnavailable:
                # 身份窗口缺口发生在 canonical 写事务内；不能误报为 Provider 或数据库故障，
                # 否则重试会重复抓取相同来源，却仍可能把历史事实绑定到错误证券。
                execution.disarm_terminal_write()
                outcome = ExecutionOutcome(
                    status="FAILED",
                    error=self._error(
                        "equity-identity-window-unavailable",
                        "PRECONDITION",
                        False,
                        "Equity identity does not cover requested window",
                    ),
                )
            except ProviderError as error:
                # Provider 原文可能含 URL、响应体或凭据，只映射稳定来源阶段错误。
                execution.disarm_terminal_write()
                outcome = ExecutionOutcome(
                    status="FAILED",
                    error=self._error(
                        "source-unavailable",
                        "PROVIDER_FETCH",
                        error.retryable,
                        "Data source is unavailable",
                    ),
                )
            except Exception:
                execution.disarm_terminal_write()
                outcome = ExecutionOutcome(
                    status="FAILED",
                    error=self._error(
                        "execution-failed", "PERSIST", True, "Data sync execution failed"
                    ),
                )
            finally:
                heartbeat_stop.set()
                heartbeat_thread.join(timeout=1)
            source_batch_ids = tuple(execution.source_batch_ids)
            if execution.terminal_written:
                return True
        self.complete_run(
            run_id=claim.run_id,
            fencing_token=claim.fencing_token,
            outcome=outcome,
            source_batch_ids=source_batch_ids,
        )
        return True

    def _assert_equity_backfill_execution_claim(self, claim: ExecutionClaim) -> None:
        """在 Provider 零调用点重验已取得 fencing token 的回填 run。"""
        intent = claim.execution_intent
        if not isinstance(intent, dict) or intent.get("kind") != "EQUITY_BACKFILL":
            return
        try:
            with self._database.transaction() as session:
                run = session.get(DataOperationRun, claim.run_id, with_for_update=True)
                if (
                    run is None
                    or run.status != "RUNNING"
                    or run.fencing_token != claim.fencing_token
                    or run.target_json != claim.target
                    or run.source_snapshot != claim.source_snapshot
                    or run.execution_intent_json != intent
                ):
                    raise EquityBackfillPreconditionFailed(
                        "Claim no longer matches frozen equity backfill run"
                    )
                self._validate_equity_backfill_run_in_session(
                    session,
                    run,
                    finalizing=False,
                    source_batch_ids=(),
                )
        except OperationProblem as error:
            raise EquityBackfillPreconditionFailed(
                "Frozen equity backfill execution intent is invalid"
            ) from error

    def _validate_equity_backfill_run_in_session(
        self,
        session: Session,
        run: DataOperationRun,
        *,
        finalizing: bool,
        source_batch_ids: tuple[UUID, ...],
    ) -> None:
        """在 dispatch 或 canonical 提交事务中重验 run，并可核验真实来源批次版本。"""
        intent = getattr(run, "execution_intent_json", None)
        if not isinstance(intent, dict) or intent.get("kind") != "EQUITY_BACKFILL":
            return
        command = session.get(DataOperationCommand, run.command_id)
        if command is None or command.submission_id is None:
            raise EquityBackfillPreconditionFailed("Equity backfill command binding is missing")
        self._validate_equity_backfill_binding(
            session,
            target=run.target_json,
            intent=intent,
            submission_id=command.submission_id,
            source_snapshot=run.source_snapshot,
            child_statuses=frozenset({"SUBMITTED", "RUNNING"}),
        )
        if finalizing:
            self._validate_equity_backfill_source_batch(
                session,
                run,
                intent,
                source_batch_ids,
            )

    def _validate_equity_backfill_source_batch(
        self,
        session: Session,
        run: DataOperationRun,
        intent: Mapping[str, Any],
        source_batch_ids: tuple[UUID, ...],
    ) -> None:
        """在 publication 提交前比对本次真实来源的 adapter、schema 与映射合同。"""
        plan_id = UUID(str(intent["planId"]))
        source = session.get(EquityBackfillPlanSource, (plan_id, run.dataset_code))
        if source is None:
            raise EquityBackfillPreconditionFailed("Frozen source contract is missing at finalizer")
        if source.source_kind == "INTERNAL_EXECUTOR":
            if source_batch_ids:
                raise EquityBackfillPreconditionFailed(
                    "Internal executor unexpectedly recorded external source batches"
                )
            return
        if not source_batch_ids:
            raise EquityBackfillPreconditionFailed(
                "External equity backfill run recorded no exact source batch"
            )
        observations = session.scalars(
            select(SourceBatch)
            .where(SourceBatch.source_batch_id.in_(source_batch_ids))
            .order_by(SourceBatch.source_batch_id)
        ).all()
        if len(observations) != len(source_batch_ids) or any(
            observation.capability != source.expected_capability
            or observation.provider_id != source.expected_provider_id
            or observation.upstream_source != source.expected_upstream_source
            or observation.adapter_version != source.expected_adapter_version
            or observation.schema_fingerprint != source.expected_schema_fingerprint
            for observation in observations
        ):
            raise EquityBackfillPreconditionFailed(
                "Observed adapter or schema differs from frozen source contract"
            )

    def _is_equity_backfill_run(self, run: DataOperationRun) -> bool:
        """判断 run 是否携带受控股票全量回填私有意图。"""
        intent = getattr(run, "execution_intent_json", None)
        return isinstance(intent, dict) and intent.get("kind") == "EQUITY_BACKFILL"

    def _require_equity_backfill_output_publication(
        self,
        session: Session,
        *,
        run: DataOperationRun,
        checkpoint_kind: str | None,
        checkpoint_position: str | None,
        source_batch_ids: tuple[UUID, ...],
    ) -> tuple[
        DatasetPublication,
        DatasetRelease,
        EquityBarWindowCoverage | None,
        tuple[EquityBackfillPartitionCheckpoint, ...],
    ]:
        """把成功回填严格绑定到目标数据集的精确 canonical publication 与 release。"""
        publication = self._publication_for_checkpoint(
            session,
            run,
            checkpoint_kind=checkpoint_kind,
            checkpoint_position=checkpoint_position,
        )
        if publication is None or publication.release_id is None:
            raise EquityBackfillPreconditionFailed(
                "Equity backfill output publication or immutable release is missing"
            )
        release = session.get(DatasetRelease, publication.release_id)
        if release is None:
            raise EquityBackfillPreconditionFailed(
                "Equity backfill output release cannot be verified"
            )
        intent = run.execution_intent_json
        if not isinstance(intent, dict):
            raise EquityBackfillPreconditionFailed("Equity backfill output intent is unavailable")
        source = session.get(
            EquityBackfillPlanSource,
            (UUID(str(intent["planId"])), run.dataset_code),
        )
        if source is None or publication.dataset != source.publication_dataset_code:
            raise EquityBackfillPreconditionFailed(
                "Equity backfill output dataset differs from frozen publication contract"
            )
        coverage: EquityBarWindowCoverage | None = None
        partition_checkpoints: tuple[EquityBackfillPartitionCheckpoint, ...] = ()
        if checkpoint_kind == "bar-coverage-version":
            try:
                coverage_version = UUID(str(checkpoint_position))
            except (TypeError, ValueError, AttributeError) as error:
                raise EquityBackfillPreconditionFailed(
                    "Equity backfill bar coverage checkpoint is invalid"
                ) from error
            coverage = session.scalar(
                select(EquityBarWindowCoverage)
                .where(EquityBarWindowCoverage.coverage_version == coverage_version)
                .with_for_update()
            )
            expected_period = {
                "equity.bar.1d.raw": "1d",
                "equity.bar.1w.raw": "1w",
                "equity.bar.1mo.raw": "1mo",
            }.get(run.dataset_code)
            identity = intent.get("identity")
            selector = run.target_json.get("selector")
            if (
                coverage is None
                or expected_period is None
                or not isinstance(identity, dict)
                or not isinstance(selector, dict)
                or coverage.capability != run.dataset_code
                or coverage.period != expected_period
                or coverage.security_id != int(identity["securityId"])
                or coverage.identifier_version_id != UUID(str(identity["identifierVersionId"]))
                or selector.get("kind") != "INSTRUMENT"
                or selector.get("exchange") != identity.get("exchange")
                or selector.get("symbol") != identity.get("symbol")
                or coverage.publication_id != publication.publication_id
                or coverage.quality_status != "passed"
                or coverage.universe_size != 1
                or coverage.source_batch_id not in source_batch_ids
            ):
                raise EquityBackfillPreconditionFailed(
                    "Equity backfill bar coverage differs from frozen target"
                )
            backfill_from = intent.get("backfillDateFrom")
            backfill_to = intent.get("backfillDateTo")
            if (
                not isinstance(backfill_from, str)
                or not isinstance(backfill_to, str)
                or not (
                    date.fromisoformat(backfill_from)
                    <= coverage.coverage_from
                    <= coverage.coverage_to
                    <= date.fromisoformat(backfill_to)
                )
                or (coverage.publication_kind == "DATA" and coverage.record_count <= 0)
                or (
                    coverage.publication_kind == "ZERO_RECORD_COVERAGE"
                    and coverage.record_count != 0
                )
                or coverage.publication_kind not in {"DATA", "ZERO_RECORD_COVERAGE"}
            ):
                raise EquityBackfillPreconditionFailed(
                    "Equity backfill bar coverage window or result kind is invalid"
                )
            partition_checkpoints = self._validate_equity_backfill_bar_partition_seal(
                session,
                run=run,
                intent=intent,
                final_coverage=coverage,
                source_batch_ids=source_batch_ids,
            )
        elif checkpoint_kind == "event-coverage-version":
            partition_checkpoints = self._validate_equity_backfill_event_partition_seal(
                session,
                run=run,
                intent=intent,
                final_publication=publication,
                source_batch_ids=source_batch_ids,
                checkpoint_position=checkpoint_position,
            )
        elif checkpoint_kind != "data-version":
            raise EquityBackfillPreconditionFailed(
                "Equity backfill success has no exact output checkpoint"
            )
        return publication, release, coverage, partition_checkpoints

    def _validate_equity_backfill_bar_partition_seal(
        self,
        session: Session,
        *,
        run: DataOperationRun,
        intent: Mapping[str, Any],
        final_coverage: EquityBarWindowCoverage,
        source_batch_ids: tuple[UUID, ...],
    ) -> tuple[EquityBackfillPartitionCheckpoint, ...]:
        """重算全部 366 日窗口并核对逐窗 publication、coverage、来源与摘要。"""
        start_text = intent.get("backfillDateFrom")
        end_text = intent.get("backfillDateTo")
        identity = intent.get("identity")
        if (
            not isinstance(start_text, str)
            or not isinstance(end_text, str)
            or not isinstance(identity, Mapping)
        ):
            raise EquityBackfillPreconditionFailed(
                "Equity backfill bar partition boundaries are unavailable"
            )
        start = date.fromisoformat(start_text)
        end = date.fromisoformat(end_text)
        expected_windows: list[tuple[date, date]] = []
        cursor = start
        while cursor <= end:
            window_to = min(end, cursor + timedelta(days=365))
            expected_windows.append((cursor, window_to))
            cursor = window_to + timedelta(days=1)
        plan_id = UUID(str(intent["planId"]))
        child = session.scalar(
            select(EquityBackfillChildSpec).where(
                EquityBackfillChildSpec.plan_id == plan_id,
                EquityBackfillChildSpec.child_key == str(intent["childKey"]),
            )
        )
        if child is None:
            raise EquityBackfillPreconditionFailed(
                "Equity backfill bar partition child is unavailable"
            )
        checkpoints = tuple(
            session.scalars(
                select(EquityBackfillPartitionCheckpoint)
                .where(
                    EquityBackfillPartitionCheckpoint.child_id == child.child_id,
                    EquityBackfillPartitionCheckpoint.target_index == run.target_index,
                )
                .order_by(
                    EquityBackfillPartitionCheckpoint.window_from,
                    EquityBackfillPartitionCheckpoint.window_to,
                )
            ).all()
        )
        exchange = str(identity["exchange"])
        symbol = str(identity["symbol"])
        expected_keys = tuple(
            (
                f"{run.dataset_code}:{exchange}:{symbol}:"
                f"{window_from.isoformat()}:{window_to.isoformat()}"
            )
            for window_from, window_to in expected_windows
        )
        if (
            len(checkpoints) != len(expected_windows)
            or tuple(checkpoint.partition_key for checkpoint in checkpoints) != expected_keys
        ):
            raise EquityBackfillPreconditionFailed(
                "Equity backfill bar partition seal is incomplete"
            )
        expected_period = {
            "equity.bar.1d.raw": "1d",
            "equity.bar.1w.raw": "1w",
            "equity.bar.1mo.raw": "1mo",
        }.get(run.dataset_code)
        execution_sources = set(source_batch_ids)
        sealed_sources: set[UUID] = set()
        for checkpoint, (window_from, window_to) in zip(checkpoints, expected_windows, strict=True):
            try:
                checkpoint_sources = tuple(
                    UUID(value) for value in checkpoint.source_batch_ids_json
                )
            except (TypeError, ValueError) as error:
                raise EquityBackfillPreconditionFailed(
                    "Equity backfill partition SourceBatch manifest is invalid"
                ) from error
            publication = session.get(DatasetPublication, checkpoint.publication_id)
            release = session.get(DatasetRelease, checkpoint.release_id)
            coverage = session.scalar(
                select(EquityBarWindowCoverage).where(
                    EquityBarWindowCoverage.coverage_version == checkpoint.coverage_version
                )
            )
            output = {
                "datasetCode": run.dataset_code,
                "partitionKey": checkpoint.partition_key,
                "windowFrom": window_from.isoformat(),
                "windowTo": window_to.isoformat(),
                "publicationId": str(checkpoint.publication_id),
                "dataVersion": str(checkpoint.data_version),
                "releaseId": str(checkpoint.release_id),
                "coverageVersion": str(checkpoint.coverage_version),
                "coverageVersions": checkpoint.coverage_versions_json,
                "publicationKind": checkpoint.publication_kind,
                "recordCount": checkpoint.record_count,
                "sourceBatchIds": checkpoint.source_batch_ids_json,
            }
            if (
                expected_period is None
                or checkpoint.dataset_code != run.dataset_code
                or checkpoint.window_from != window_from
                or checkpoint.window_to != window_to
                or checkpoint.checkpoint_kind != "BAR_COVERAGE_VERSION"
                or not checkpoint_sources
                or len(checkpoint_sources) != len(set(checkpoint_sources))
                or list(map(str, sorted(checkpoint_sources, key=str)))
                != checkpoint.source_batch_ids_json
                or self._hash(checkpoint.source_batch_ids_json) != checkpoint.source_batch_hash
                or self._hash(output) != checkpoint.output_hash
                or not set(checkpoint_sources) <= execution_sources
                or publication is None
                or release is None
                or coverage is None
                or checkpoint.coverage_versions_json != [str(checkpoint.coverage_version)]
                or publication.publication_id != checkpoint.publication_id
                or publication.data_version != checkpoint.data_version
                or publication.release_id != checkpoint.release_id
                or publication.dataset != run.dataset_code
                or publication.quality_status != "passed"
                or release.release_id != checkpoint.release_id
                or coverage.coverage_version != checkpoint.coverage_version
                or coverage.publication_id != checkpoint.publication_id
                or coverage.period != expected_period
                or coverage.capability != run.dataset_code
                or coverage.security_id != int(identity["securityId"])
                or coverage.identifier_version_id != UUID(str(identity["identifierVersionId"]))
                or coverage.coverage_from != window_from
                or coverage.coverage_to != window_to
                or coverage.source_batch_id not in checkpoint_sources
                or coverage.publication_kind != checkpoint.publication_kind
                or coverage.record_count != checkpoint.record_count
                or coverage.quality_status != "passed"
            ):
                raise EquityBackfillPreconditionFailed(
                    "Equity backfill bar partition output differs from its seal"
                )
            sealed_sources.update(checkpoint_sources)
        if (
            sealed_sources != execution_sources
            or checkpoints[-1].coverage_version != final_coverage.coverage_version
        ):
            raise EquityBackfillPreconditionFailed(
                "Equity backfill bar partition source or final coverage seal differs"
            )
        return checkpoints

    def _validate_equity_backfill_event_partition_seal(
        self,
        session: Session,
        *,
        run: DataOperationRun,
        intent: Mapping[str, Any],
        final_publication: DatasetPublication,
        source_batch_ids: tuple[UUID, ...],
        checkpoint_position: str | None,
    ) -> tuple[EquityBackfillPartitionCheckpoint, ...]:
        """重算事件窗口与族清单，并核对每个 coverage、publication、来源和最终指针。"""
        start_text = intent.get("backfillDateFrom")
        end_text = intent.get("backfillDateTo")
        families_by_dataset = {
            "equity.corporate_action": ("CORPORATE_ACTION",),
            "equity.corporate_event.earnings.reported": (
                "EARNINGS_FORECAST",
                "EARNINGS_EXPRESS",
            ),
            "equity.dragon_tiger.disclosure.reported": ("DRAGON_TIGER",),
            "equity.block_trade.execution.reported": ("BLOCK_TRADE",),
        }
        families = families_by_dataset.get(run.dataset_code)
        if not isinstance(start_text, str) or not isinstance(end_text, str) or families is None:
            raise EquityBackfillPreconditionFailed(
                "Equity backfill event partition boundaries are unavailable"
            )
        start = date.fromisoformat(start_text)
        end = date.fromisoformat(end_text)
        maximum_days = 1098 if run.dataset_code == "equity.corporate_action" else 31
        windows: list[tuple[date, date]] = []
        cursor = start
        while cursor <= end:
            window_to = min(end, cursor + timedelta(days=maximum_days - 1))
            windows.append((cursor, window_to))
            cursor = window_to + timedelta(days=1)
        plan_id = UUID(str(intent["planId"]))
        child = session.scalar(
            select(EquityBackfillChildSpec).where(
                EquityBackfillChildSpec.plan_id == plan_id,
                EquityBackfillChildSpec.child_key == str(intent["childKey"]),
            )
        )
        if child is None:
            raise EquityBackfillPreconditionFailed(
                "Equity backfill event partition child is unavailable"
            )
        checkpoints = tuple(
            session.scalars(
                select(EquityBackfillPartitionCheckpoint)
                .where(
                    EquityBackfillPartitionCheckpoint.child_id == child.child_id,
                    EquityBackfillPartitionCheckpoint.target_index == run.target_index,
                )
                .order_by(
                    EquityBackfillPartitionCheckpoint.window_from,
                    EquityBackfillPartitionCheckpoint.window_to,
                    EquityBackfillPartitionCheckpoint.partition_key,
                )
            ).all()
        )
        expected = {
            (f"{run.dataset_code}:{family}:{window_from.isoformat()}:{window_to.isoformat()}"): (
                family,
                window_from,
                window_to,
            )
            for window_from, window_to in windows
            for family in families
        }
        if len(checkpoints) != len(expected) or {
            checkpoint.partition_key for checkpoint in checkpoints
        } != set(expected):
            raise EquityBackfillPreconditionFailed(
                "Equity backfill event partition seal is incomplete"
            )
        execution_sources = set(source_batch_ids)
        sealed_sources: set[UUID] = set()
        identity = intent.get("identity")
        final_coverage_version: UUID | None = None
        for checkpoint in checkpoints:
            family, window_from, window_to = expected[checkpoint.partition_key]
            try:
                checkpoint_sources = tuple(
                    UUID(value) for value in checkpoint.source_batch_ids_json
                )
                coverage_versions = tuple(
                    UUID(value) for value in checkpoint.coverage_versions_json
                )
            except (TypeError, ValueError) as error:
                raise EquityBackfillPreconditionFailed(
                    "Equity backfill event coverage manifest is invalid"
                ) from error
            coverages = tuple(
                session.scalars(
                    select(EquityEventWindowCoverage)
                    .where(EquityEventWindowCoverage.coverage_version.in_(coverage_versions))
                    .order_by(
                        EquityEventWindowCoverage.security_id,
                        EquityEventWindowCoverage.coverage_from,
                        EquityEventWindowCoverage.coverage_to,
                        EquityEventWindowCoverage.coverage_version,
                    )
                ).all()
            )
            aggregate_version = uuid5(
                child.child_id,
                (
                    f"target:{run.target_index}:event-coverages:"
                    f"{':'.join(sorted(map(str, coverage_versions)))}"
                ),
            )
            publication = session.get(DatasetPublication, checkpoint.publication_id)
            release = session.get(DatasetRelease, checkpoint.release_id)
            record_count = sum(coverage.record_count for coverage in coverages)
            publication_kind = "ZERO_RECORD_COVERAGE" if record_count == 0 else "DATA"
            output = {
                "datasetCode": run.dataset_code,
                "partitionKey": checkpoint.partition_key,
                "windowFrom": window_from.isoformat(),
                "windowTo": window_to.isoformat(),
                "publicationId": str(checkpoint.publication_id),
                "dataVersion": str(checkpoint.data_version),
                "releaseId": str(checkpoint.release_id),
                "coverageVersion": str(checkpoint.coverage_version),
                "coverageVersions": checkpoint.coverage_versions_json,
                "publicationKind": checkpoint.publication_kind,
                "recordCount": checkpoint.record_count,
                "sourceBatchIds": checkpoint.source_batch_ids_json,
            }
            if (
                checkpoint.dataset_code != run.dataset_code
                or checkpoint.window_from != window_from
                or checkpoint.window_to != window_to
                or checkpoint.checkpoint_kind != "EVENT_COVERAGE_VERSION"
                or checkpoint.coverage_version != aggregate_version
                or not coverage_versions
                or len(coverage_versions) != len(set(coverage_versions))
                or checkpoint.coverage_versions_json != sorted(map(str, coverage_versions))
                or len(coverages) != len(coverage_versions)
                or not checkpoint_sources
                or len(checkpoint_sources) != len(set(checkpoint_sources))
                or checkpoint.source_batch_ids_json != sorted(map(str, checkpoint_sources))
                or self._hash(checkpoint.source_batch_ids_json) != checkpoint.source_batch_hash
                or self._hash(output) != checkpoint.output_hash
                or not set(checkpoint_sources) <= execution_sources
                or publication is None
                or release is None
                or publication.publication_id != checkpoint.publication_id
                or publication.data_version != checkpoint.data_version
                or publication.release_id != checkpoint.release_id
                or publication.dataset != run.dataset_code
                or publication.quality_status != "passed"
                or publication.effective_as_of != window_to
                or any(
                    coverage.dataset != run.dataset_code
                    or coverage.event_family != family
                    or coverage.publication_id != checkpoint.publication_id
                    or coverage.source_batch_id not in checkpoint_sources
                    or coverage.coverage_from < window_from
                    or coverage.coverage_to > window_to
                    or coverage.coverage_from > coverage.coverage_to
                    for coverage in coverages
                )
                or checkpoint.record_count != record_count
                or checkpoint.publication_kind != publication_kind
            ):
                raise EquityBackfillPreconditionFailed(
                    "Equity backfill event partition output differs from its seal"
                )
            if isinstance(identity, Mapping) and (
                len(coverages) != 1
                or coverages[0].security_id != int(identity["securityId"])
                or coverages[0].identifier_version_id != UUID(str(identity["identifierVersionId"]))
                or coverages[0].coverage_scope != "INSTRUMENT"
                or coverages[0].coverage_from != window_from
                or coverages[0].coverage_to != window_to
            ):
                raise EquityBackfillPreconditionFailed(
                    "Equity backfill event instrument coverage is invalid"
                )
            if identity is None and any(
                coverage.coverage_scope != "GLOBAL" for coverage in coverages
            ):
                raise EquityBackfillPreconditionFailed(
                    "Equity backfill global event coverage is invalid"
                )
            sealed_sources.update(checkpoint_sources)
            if checkpoint.publication_id == final_publication.publication_id:
                final_coverage_version = checkpoint.coverage_version
        try:
            requested_final = UUID(str(checkpoint_position))
        except (TypeError, ValueError, AttributeError) as error:
            raise EquityBackfillPreconditionFailed(
                "Equity backfill final event coverage is invalid"
            ) from error
        if sealed_sources != execution_sources or final_coverage_version != requested_final:
            raise EquityBackfillPreconditionFailed(
                "Equity backfill event source or final coverage seal differs"
            )
        return checkpoints

    def _validate_equity_backfill_output_publication(
        self,
        session: Session,
        run: DataOperationRun,
        execution: FencedExecution,
    ) -> None:
        """在 canonical 提交前拒绝缺少精确输出版本的伪成功。"""
        if not self._is_equity_backfill_run(run):
            return
        self._require_equity_backfill_output_publication(
            session,
            run=run,
            checkpoint_kind=execution.checkpoint_kind,
            checkpoint_position=execution.checkpoint_position,
            source_batch_ids=tuple(execution.source_batch_ids),
        )

    def _record_equity_backfill_child_result(
        self,
        session: Session,
        *,
        run: DataOperationRun,
        command: DataOperationCommand,
        terminal_status: str,
        outcome: ExecutionOutcome,
        source_batch_ids: tuple[UUID, ...],
        now: datetime,
    ) -> None:
        """按 run 追加精确输入、输出 publication 与来源审计，历史尝试永不改写。"""
        if not self._is_equity_backfill_run(run):
            return
        intent = run.execution_intent_json
        assert isinstance(intent, dict)
        plan_id = UUID(str(intent["planId"]))
        child = session.scalar(
            select(EquityBackfillChildSpec).where(
                EquityBackfillChildSpec.plan_id == plan_id,
                EquityBackfillChildSpec.child_key == str(intent["childKey"]),
            )
        )
        if child is None or command.submission_id != child.submission_id:
            raise EquityBackfillPreconditionFailed(
                "Equity backfill result cannot bind its immutable child"
            )
        input_manifest = list(
            self._claim_equity_backfill_input_manifest(
                session,
                run=run,
                command=command,
                now=now,
            )
        )
        output_manifest: list[dict[str, Any]] = []
        if terminal_status == "SUCCEEDED":
            (
                publication,
                release,
                coverage,
                partition_checkpoints,
            ) = self._require_equity_backfill_output_publication(
                session,
                run=run,
                checkpoint_kind=outcome.checkpoint_kind,
                checkpoint_position=outcome.checkpoint_position,
                source_batch_ids=source_batch_ids,
            )
            release_source_batch_ids = sorted(
                set(
                    session.scalars(
                        select(CanonicalRecordLineage.source_batch_id).where(
                            CanonicalRecordLineage.release_id == release.release_id
                        )
                    ).all()
                ),
                key=str,
            )
            output_manifest.append(
                {
                    "datasetCode": run.dataset_code,
                    "publicationDatasetCode": publication.dataset,
                    "publicationId": str(publication.publication_id),
                    "dataVersion": str(publication.data_version),
                    "releaseId": str(release.release_id),
                    "partitionKey": publication.partition_key,
                    "qualityStatus": publication.quality_status,
                    "publishedAt": publication.published_at.isoformat(),
                    "effectiveAsOf": (
                        None
                        if publication.effective_as_of is None
                        else publication.effective_as_of.isoformat()
                    ),
                    "knowledgeCutoff": (
                        None
                        if publication.knowledge_cutoff is None
                        else publication.knowledge_cutoff.isoformat()
                    ),
                    "rowCount": release.record_count,
                    "coverageVersion": (
                        None if coverage is None else str(coverage.coverage_version)
                    ),
                    "coverageFrom": (
                        None if coverage is None else coverage.coverage_from.isoformat()
                    ),
                    "coverageTo": (None if coverage is None else coverage.coverage_to.isoformat()),
                    "publicationKind": ("DATA" if coverage is None else coverage.publication_kind),
                    "partitionManifest": [
                        {
                            "partitionKey": checkpoint.partition_key,
                            "windowFrom": checkpoint.window_from.isoformat(),
                            "windowTo": checkpoint.window_to.isoformat(),
                            "publicationId": str(checkpoint.publication_id),
                            "dataVersion": str(checkpoint.data_version),
                            "releaseId": str(checkpoint.release_id),
                            "coverageVersion": (
                                None
                                if checkpoint.coverage_version is None
                                else str(checkpoint.coverage_version)
                            ),
                            "coverageVersions": checkpoint.coverage_versions_json,
                            "publicationKind": checkpoint.publication_kind,
                            "recordCount": checkpoint.record_count,
                            "sourceBatchIds": checkpoint.source_batch_ids_json,
                            "outputHash": checkpoint.output_hash,
                        }
                        for checkpoint in partition_checkpoints
                    ],
                    "releaseSourceBatchIds": [
                        str(source_batch_id) for source_batch_id in release_source_batch_ids
                    ],
                    "executionSourceBatchIds": [
                        str(source_batch_id)
                        for source_batch_id in sorted(source_batch_ids, key=str)
                    ],
                    "target": run.target_json,
                }
            )
        source = session.get(EquityBackfillPlanSource, (plan_id, run.dataset_code))
        if source is None:
            raise EquityBackfillPreconditionFailed(
                "Equity backfill result source contract is missing"
            )
        input_hash = self._hash(input_manifest)
        output_hash = self._hash(output_manifest)
        audit = {
            "planId": str(plan_id),
            "childKey": child.child_key,
            "commandId": str(command.command_id),
            "runId": str(run.run_id),
            "targetIndex": run.target_index,
            "terminalStatus": terminal_status,
            "sourceContractHash": source.source_contract_hash,
            "sourceSnapshotHash": source.source_snapshot_hash,
            "executorCode": source.internal_executor_code,
            "methodologyCode": source.methodology_code,
            "methodologyVersion": source.methodology_version,
            "mappingVersion": source.mapping_version,
            "inputManifestHash": input_hash,
            "outputManifestHash": output_hash,
            "sourceBatchIds": [
                str(source_batch_id) for source_batch_id in sorted(source_batch_ids, key=str)
            ],
            "processedRecords": max(0, outcome.processed_records),
            "completedPartitions": max(0, outcome.completed_partitions),
            "totalPartitions": max(0, outcome.total_partitions),
            "qualityGate": outcome.quality_gate,
            "error": outcome.error,
        }
        result_id = uuid5(run.run_id, f"equity-backfill-result:{run.target_index}")
        existing = session.scalar(
            select(EquityBackfillChildResult).where(EquityBackfillChildResult.run_id == run.run_id)
        )
        values = {
            "result_id": result_id,
            "child_id": child.child_id,
            "run_id": run.run_id,
            "command_id": command.command_id,
            "target_index": run.target_index,
            "terminal_status": terminal_status,
            "input_manifest_json": input_manifest,
            "input_manifest_hash": input_hash,
            "output_manifest_json": output_manifest,
            "output_manifest_hash": output_hash,
            "audit_json": audit,
            "audit_hash": self._hash(audit),
            "created_at": now,
        }
        if existing is None:
            session.add(EquityBackfillChildResult(**values))
            return
        if any(getattr(existing, key) != value for key, value in values.items()):
            raise EquityBackfillPreconditionFailed(
                "Equity backfill append-only result replay differs from persisted fact"
            )

    def _refresh_equity_backfill_child_state(
        self,
        session: Session,
        command: DataOperationCommand,
        now: datetime,
    ) -> None:
        """在 command 全部 run 结束后收敛 child 状态，并引用本次 append-only 结果摘要。"""
        if command.status not in _TERMINAL_COMMANDS:
            return
        state = session.scalar(
            select(EquityBackfillChildState)
            .where(EquityBackfillChildState.command_id == command.command_id)
            .with_for_update()
        )
        if state is None:
            return
        child = session.get(EquityBackfillChildSpec, state.child_id)
        if child is None:
            raise EquityBackfillPreconditionFailed(
                "Equity backfill child state has no immutable specification"
            )
        results = session.scalars(
            select(EquityBackfillChildResult)
            .where(
                EquityBackfillChildResult.child_id == child.child_id,
                EquityBackfillChildResult.command_id == command.command_id,
            )
            .order_by(EquityBackfillChildResult.target_index)
        ).all()
        if len(results) != child.target_count:
            raise EquityBackfillPreconditionFailed(
                "Equity backfill terminal command has incomplete append-only results"
            )
        state.status = (
            command.status
            if command.status in {"SUCCEEDED", "PARTIAL", "FAILED", "CANCELLED"}
            else "FAILED"
        )
        failed_runs = session.scalars(
            select(DataOperationRun)
            .where(
                DataOperationRun.command_id == command.command_id,
                DataOperationRun.error_json.is_not(None),
            )
            .order_by(DataOperationRun.target_index)
        ).all()
        state.last_error_json = (
            None
            if not failed_runs
            else {
                "runErrors": [
                    {
                        "runId": str(failed_run.run_id),
                        "targetIndex": failed_run.target_index,
                        "error": failed_run.error_json,
                    }
                    for failed_run in failed_runs
                ]
            }
        )
        state.audit_json = {
            "commandId": str(command.command_id),
            "submissionId": (None if command.submission_id is None else str(command.submission_id)),
            "status": state.status,
            "resultHashes": [result.audit_hash for result in results],
        }
        state.finished_at = now
        state.updated_at = now

    def _start_heartbeat(self, claim: ExecutionClaim) -> tuple[Event, Thread]:
        """启动独立心跳线程，使慢 Provider 抓取期间的 lease 不会无故到期。"""
        stop = Event()
        thread = Thread(
            target=self._heartbeat_until_stopped,
            args=(claim.run_id, claim.fencing_token, stop),
            name=f"data-operation-heartbeat-{claim.run_id}",
            daemon=True,
        )
        thread.start()
        return stop, thread

    def _heartbeat_until_stopped(self, run_id: UUID, fencing_token: int, stop: Event) -> None:
        """按 lease 的三分之一续租；发现 token 已失效后立即退出而不尝试复活。"""
        interval = max(1, _LEASE_SECONDS // 3)
        while not stop.wait(interval):
            try:
                current = self.heartbeat(run_id=run_id, fencing_token=fencing_token)
            except SQLAlchemyError as error:
                # 续租事务未确认提交时不能把连接故障当作成功，也不能在后台重试而延长
                # 不确定的 owner；停止心跳后由 lease 到期与 recovery 收敛，后续写入仍会
                # 通过 `FencedExecution` 在事务内重验 token。
                _LOGGER.warning(
                    "数据运维心跳数据库事务失败（%s）；停止续租并等待租约回收",
                    type(error).__name__,
                    extra={
                        "data_operation_run_id": str(run_id),
                        "fencing_token": fencing_token,
                    },
                )
                return
            if not current:
                return

    def reap_expired_slots(self) -> int:
        """回收过期 lease，将中断 run 留为可追溯事实并释放唯一执行槽。"""
        now = self._now()
        with self._database.transaction() as session:
            slot = self._ensure_slot(session, lock=True)
            if slot.state == "IDLE" or slot.lease_until is not None and slot.lease_until > now:
                return 0
            self._reap_locked_slot(session, slot, now)
            return 1

    def list_health_evaluations(self, request: dict[str, Any]) -> dict[str, Any]:
        """分页读取不可变健康评估摘要；当前开放问题另行计算。"""
        limit = self._validate_limit(request.get("limit", 50), maximum=200)
        with self._database.session() as session:
            statement = select(DataOperationHealthEvaluation).order_by(
                DataOperationHealthEvaluation.evaluated_at.desc(),
                DataOperationHealthEvaluation.evaluation_id.desc(),
            )
            if codes := request.get("datasetCodes"):
                statement = statement.where(DataOperationHealthEvaluation.dataset_code.in_(codes))
            if statuses := request.get("statuses"):
                statement = statement.where(DataOperationHealthEvaluation.status.in_(statuses))
            rows = session.scalars(statement).all()
            offset = self._decode_offset(request.get("cursor"))
            visible = rows[offset : offset + limit]
            return {
                "items": [self._health_summary(session, row) for row in visible],
                "nextCursor": self._encode_offset(offset + limit)
                if offset + limit < len(rows)
                else None,
            }

    def health_evaluation_detail(self, request: dict[str, Any]) -> dict[str, Any]:
        """返回不可变规则结果和查询时的当前问题投影，两个 cursor 互不混用。"""
        evaluation_id = self._uuid_field(request, "evaluationId")
        limit = self._validate_limit(request.get("issuesLimit", 100), maximum=100)
        with self._database.session() as session:
            evaluation = session.get(DataOperationHealthEvaluation, evaluation_id)
            if evaluation is None:
                raise OperationProblem(
                    status=404,
                    code="health-evaluation-not-found",
                    detail="Health evaluation is not found",
                )
            issues = session.scalars(
                select(DataOperationHealthIssue)
                .where(
                    DataOperationHealthIssue.dataset_code == evaluation.dataset_code,
                    DataOperationHealthIssue.status.in_(("OPEN", "ACKNOWLEDGED")),
                )
                .order_by(DataOperationHealthIssue.last_detected_at.desc())
            ).all()
            offset = self._decode_offset(request.get("issuesCursor"))
            visible = issues[offset : offset + limit]
            return {
                "evaluation": self._health_evaluation_view(evaluation),
                "currentOpenIssueCount": len(issues),
                "currentOpenIssues": [self._issue_view(issue) for issue in visible],
                "currentOpenIssuesNextCursor": self._encode_offset(offset + limit)
                if offset + limit < len(issues)
                else None,
                "issueProjectionAsOf": self._iso(self._now()),
            }

    def submit_health_check(
        self, *, request: dict[str, Any], idempotency_key: str, request_id: str
    ) -> dict[str, Any]:
        """受理独立健康检查批次；后续执行不抢占同步 slot。"""
        self._require_idempotency_key(idempotency_key)
        targets = self._validate_health_targets(self._require_list(request, "targets"))
        submission_id = self._uuid_field(request, "submissionId")
        actor = self._actor(request)
        now = self._now()
        operation_hash = self._hash(request)
        with self._database.transaction() as session:
            replay = self._idempotent_resource(
                session, "health-submit", idempotency_key, operation_hash
            )
            if replay is not None:
                return replay.response_json
            check = DataOperationHealthCheck(
                health_check_id=uuid4(),
                submission_id=submission_id,
                status="QUEUED",
                actor_ref=actor["actorRef"],
                actor_role=actor["role"],
                reason=actor["reason"],
                request_id=request_id,
                error_json=None,
                requested_at=now,
                started_at=None,
                finished_at=None,
            )
            session.add(check)
            for index, target in enumerate(targets):
                requested_data_version = self._parse_uuid_or_none(target.get("dataVersion"))
                binding = self._health_publication_binding(
                    session,
                    target["datasetCode"],
                    requested_data_version,
                )
                binding_error = (
                    None
                    if binding is not None
                    else self._error(
                        "health-publication-binding-unavailable",
                        "HEALTH_EVALUATION",
                        False,
                        "Health target has no immutable production release binding",
                    )
                )
                session.add(
                    DataOperationHealthCheckTarget(
                        health_check_id=check.health_check_id,
                        target_index=index,
                        dataset_code=target["datasetCode"],
                        requested_data_version=requested_data_version,
                        # null target 在受理时冻结为当前 production 版本，后续 dispatcher 不会
                        # 因新 publication 而偷偷改查不同版本。
                        resolved_data_version=binding.publication.data_version
                        if binding is not None
                        else None,
                        status="QUEUED" if binding is not None else "REJECTED",
                        evaluation_id=None,
                        error_json=binding_error,
                    )
                )
            session.flush()
            self._refresh_health_check_status(session, check, now)
            self._record_event(
                session,
                "HEALTH_CHECK",
                check.health_check_id,
                "SUBMIT",
                "ACCEPTED",
                actor["actorRef"],
                request_id,
                None,
            )
            receipt = {
                "healthCheckId": str(check.health_check_id),
                "submissionId": str(submission_id),
                "status": check.status,
                "acceptedAt": self._iso(now),
            }
            self._record_idempotency(
                session,
                "health-submit",
                idempotency_key,
                operation_hash,
                "HEALTH_CHECK",
                check.health_check_id,
                receipt,
                now,
            )
            return receipt

    def health_check_detail(self, health_check_id: UUID) -> dict[str, Any]:
        """读取健康检查与按原提交顺序返回的逐 target 结果。"""
        with self._database.session() as session:
            check = session.get(DataOperationHealthCheck, health_check_id)
            if check is None:
                raise OperationProblem(
                    status=404, code="health-check-not-found", detail="Health check is not found"
                )
            targets = session.scalars(
                select(DataOperationHealthCheckTarget)
                .where(DataOperationHealthCheckTarget.health_check_id == health_check_id)
                .order_by(DataOperationHealthCheckTarget.target_index)
            ).all()
            return {
                "healthCheckId": str(check.health_check_id),
                "submissionId": self._uuid_text(check.submission_id),
                "status": check.status,
                "requestedAt": self._iso(check.requested_at),
                "startedAt": self._iso(check.started_at),
                "finishedAt": self._iso(check.finished_at),
                "actorRef": check.actor_ref,
                "targets": [self._health_target_view(item) for item in targets],
                "error": check.error_json,
            }

    def dispatch_health_check_once(self) -> bool:
        """执行一个排队健康 target，并把 evaluationId 写回同一权威批次。

        主动健康检查不占用同步全局 slot，因为它只读取既有发布；每个 target 仍通过
        PostgreSQL 行锁领取，成功、失败和有序 target 状态均可由 service-api 逐项对账。
        """
        now = self._now()
        with self._database.transaction() as session:
            target = session.scalars(
                select(DataOperationHealthCheckTarget)
                .where(DataOperationHealthCheckTarget.status == "QUEUED")
                .order_by(
                    DataOperationHealthCheckTarget.health_check_id,
                    DataOperationHealthCheckTarget.target_index,
                )
                .with_for_update(skip_locked=True)
                .limit(1)
            ).first()
            if target is None:
                return False
            check = session.get(
                DataOperationHealthCheck,
                target.health_check_id,
                with_for_update=True,
            )
            assert check is not None
            check.status = "RUNNING"
            check.started_at = check.started_at or now
            binding = self._health_publication_binding(
                session,
                target.dataset_code,
                target.resolved_data_version,
            )
            if binding is None:
                # 目标在受理后仍可能因清理或错误迁移失去 immutable release；此时失败该 target，
                # 不能回退到“最新版本”或伪造 UUID。
                target.status = "FAILED"
                target.evaluation_id = None
                target.error_json = self._error(
                    "health-publication-binding-lost",
                    "HEALTH_EVALUATION",
                    False,
                    "Health target lost its immutable publication binding",
                )
                event_result = "FAILED"
                event_error = target.error_json
            else:
                target.status = "RUNNING"
                evaluation = self._record_health_evaluation(
                    session,
                    binding=binding,
                    health_check_id=check.health_check_id,
                    now=now,
                )
                target.resolved_data_version = binding.publication.data_version
                target.evaluation_id = evaluation.evaluation_id
                target.status = "SUCCEEDED"
                target.error_json = None
                event_result = "SUCCEEDED"
                event_error = None
            self._refresh_health_check_status(session, check, now)
            self._record_event(
                session,
                "HEALTH_CHECK",
                check.health_check_id,
                "COMPLETE_TARGET",
                event_result,
                check.actor_ref,
                check.request_id,
                event_error,
            )
            return True

    def list_schedules(self, request: dict[str, Any]) -> dict[str, Any]:
        """分页读取计划；不接受任意 cron 字符串。"""
        limit = self._validate_limit(request.get("limit", 50), maximum=200)
        with self._database.session() as session:
            statement = select(DataOperationSchedule).order_by(DataOperationSchedule.dataset_code)
            if codes := request.get("datasetCodes"):
                statement = statement.where(DataOperationSchedule.dataset_code.in_(codes))
            if request.get("enabled") is not None:
                statement = statement.where(
                    DataOperationSchedule.enabled == bool(request["enabled"])
                )
            rows = session.scalars(statement).all()
            offset = self._decode_offset(request.get("cursor"))
            visible = rows[offset : offset + limit]
            return {
                "items": [self._schedule_view(item) for item in visible],
                "nextCursor": self._encode_offset(offset + limit)
                if offset + limit < len(rows)
                else None,
            }

    def upsert_schedule(
        self, *, request: dict[str, Any], idempotency_key: str, request_id: str
    ) -> dict[str, Any]:
        """创建或乐观锁更新 datasetCode 唯一计划，更新不得改绑数据集。"""
        self._require_idempotency_key(idempotency_key)
        actor = self._actor(request)
        self._uuid_field(request, "submissionId")
        dataset_code = self._require_string(request, "datasetCode", max_length=160)
        definition = self._definition(dataset_code)
        mode = self._require_string(request, "mode", max_length=24)
        selector = self._validate_selector(
            self._require_dict(request, "selector"),
            definition,
            etf_all_profile_versions="DRAFT",
        )
        self._validate_etf_dataset_operation(selector, definition)
        self._validate_margin_dataset_operation(selector, definition)
        self._validate_index_dataset_selector(selector, definition)
        self._validate_stock_connect_research_dataset_selector(selector, definition)
        policy = self._require_dict(request, "targetPolicy")
        frequency = self._require_dict(request, "frequency")
        frequency = self._validate_schedule(definition, mode, policy, frequency)
        misfire_policy = self._require_string(request, "misfirePolicy", max_length=24)
        if misfire_policy not in {"SKIP", "RUN_ONCE"}:
            raise OperationProblem(
                status=422,
                code="invalid-schedule-misfire-policy",
                detail="Schedule misfire policy is invalid",
            )
        coalesce = request.get("coalesce")
        enabled = request.get("enabled")
        if not isinstance(coalesce, bool) or not isinstance(enabled, bool):
            raise OperationProblem(
                status=422,
                code="validation-error",
                detail="Schedule coalesce and enabled must be booleans",
            )
        expected = request.get("expectedVersion")
        schedule_id = self._parse_uuid_or_none(request.get("scheduleId"))
        if (schedule_id is None) != (expected is None):
            raise OperationProblem(
                status=422,
                code="schedule-version-pair-invalid",
                detail="scheduleId and expectedVersion must be both null or both present",
            )
        if schedule_id is not None and (
            not isinstance(expected, int) or isinstance(expected, bool) or expected < 1
        ):
            raise OperationProblem(
                status=422,
                code="schedule-version-pair-invalid",
                detail="Schedule expectedVersion is invalid",
            )
        now = self._now()
        operation_hash = self._hash(request)
        with self._database.transaction() as session:
            replay = self._idempotent_resource(
                session, "schedule-upsert", idempotency_key, operation_hash
            )
            if replay is not None:
                return replay.response_json
            if schedule_id is None:
                if expected is not None:
                    raise OperationProblem(
                        status=400,
                        code="validation-error",
                        detail="Create schedule must not have expectedVersion",
                    )
                existing = session.scalar(
                    select(DataOperationSchedule)
                    .where(DataOperationSchedule.dataset_code == dataset_code)
                    .with_for_update()
                )
                if existing is not None:
                    raise OperationProblem(
                        status=409, code="schedule-exists", detail="Dataset already has a schedule"
                    )
                schedule = DataOperationSchedule(
                    schedule_id=uuid4(),
                    dataset_code=dataset_code,
                    mode=mode,
                    selector_json=selector,
                    target_policy_json=policy,
                    frequency_json=frequency,
                    misfire_policy=misfire_policy,
                    coalesce=coalesce,
                    enabled=enabled,
                    version=1,
                    revision_id=uuid4(),
                    recent_run_at=None,
                    next_run_at=(
                        self._next_schedule_occurrence(frequency, now) if enabled else None
                    ),
                    updated_at=now,
                    updated_by_actor_ref=actor["actorRef"],
                )
                session.add(schedule)
                before_snapshot: dict[str, Any] = {}
            else:
                schedule = session.get(DataOperationSchedule, schedule_id, with_for_update=True)
                if schedule is None:
                    raise OperationProblem(
                        status=404, code="schedule-not-found", detail="Schedule is not found"
                    )
                if schedule.dataset_code != dataset_code:
                    raise OperationProblem(
                        status=422,
                        code="schedule-dataset-immutable",
                        detail="Schedule dataset cannot be changed",
                    )
                if expected != schedule.version:
                    raise OperationProblem(
                        status=409,
                        code="schedule-version-conflict",
                        detail="Schedule version does not match",
                    )
                before_snapshot = self._schedule_snapshot(schedule)
                schedule.mode = mode
                schedule.selector_json = selector
                schedule.target_policy_json = policy
                schedule.frequency_json = frequency
                schedule.misfire_policy = misfire_policy
                schedule.coalesce = coalesce
                schedule.enabled = enabled
                schedule.version += 1
                schedule.revision_id = uuid4()
                schedule.next_run_at = (
                    self._next_schedule_occurrence(frequency, now) if schedule.enabled else None
                )
                schedule.updated_at = now
                schedule.updated_by_actor_ref = actor["actorRef"]
            self._append_schedule_revision(
                session,
                schedule=schedule,
                change_kind="UPSERT",
                before_snapshot=before_snapshot,
                actor_ref=actor["actorRef"],
                request_id=request_id,
                now=now,
            )
            self._record_event(
                session,
                "SCHEDULE",
                schedule.schedule_id,
                "UPSERT",
                "ACCEPTED",
                actor["actorRef"],
                request_id,
                None,
            )
            response = self._schedule_view(schedule)
            self._record_idempotency(
                session,
                "schedule-upsert",
                idempotency_key,
                operation_hash,
                "SCHEDULE",
                schedule.schedule_id,
                response,
                now,
            )
            return response

    def set_schedule_enabled(
        self, *, request: dict[str, Any], idempotency_key: str, request_id: str
    ) -> dict[str, Any]:
        """按乐观锁启停计划，禁止丢失并发编辑。"""
        self._require_idempotency_key(idempotency_key)
        actor = self._actor(request)
        submission_id = self._uuid_field(request, "submissionId")
        del submission_id
        schedule_id = self._uuid_field(request, "scheduleId")
        expected = request.get("expectedVersion")
        enabled = request.get("enabled")
        if (
            not isinstance(expected, int)
            or isinstance(expected, bool)
            or not isinstance(enabled, bool)
        ):
            raise OperationProblem(
                status=400, code="validation-error", detail="Schedule enable request is invalid"
            )
        now = self._now()
        operation_hash = self._hash(request)
        with self._database.transaction() as session:
            replay = self._idempotent_resource(
                session, "schedule-enabled", idempotency_key, operation_hash
            )
            if replay is not None:
                return replay.response_json
            schedule = session.get(DataOperationSchedule, schedule_id, with_for_update=True)
            if schedule is None:
                raise OperationProblem(
                    status=404, code="schedule-not-found", detail="Schedule is not found"
                )
            if schedule.version != expected:
                raise OperationProblem(
                    status=409,
                    code="schedule-version-conflict",
                    detail="Schedule version does not match",
                )
            if enabled:
                definition = self._definition(schedule.dataset_code)
                self._validate_schedule(
                    definition,
                    schedule.mode,
                    schedule.target_policy_json,
                    schedule.frequency_json,
                )
            before_snapshot = self._schedule_snapshot(schedule)
            schedule.enabled = enabled
            schedule.version += 1
            schedule.revision_id = uuid4()
            schedule.next_run_at = (
                self._next_schedule_occurrence(schedule.frequency_json, now) if enabled else None
            )
            schedule.updated_at = now
            schedule.updated_by_actor_ref = actor["actorRef"]
            self._append_schedule_revision(
                session,
                schedule=schedule,
                change_kind="SET_ENABLED",
                before_snapshot=before_snapshot,
                actor_ref=actor["actorRef"],
                request_id=request_id,
                now=now,
            )
            self._record_event(
                session,
                "SCHEDULE",
                schedule.schedule_id,
                "SET_ENABLED",
                "ACCEPTED",
                actor["actorRef"],
                request_id,
                None,
            )
            response = self._schedule_view(schedule)
            self._record_idempotency(
                session,
                "schedule-enabled",
                idempotency_key,
                operation_hash,
                "SCHEDULE",
                schedule.schedule_id,
                response,
                now,
            )
            return response

    def list_events(self, request: dict[str, Any]) -> dict[str, Any]:
        """分页检索不可变运维事件；事件 cursor 独立于 run timeline。"""
        limit = self._validate_limit(request.get("limit", 50), maximum=200)
        occurred_from = self._datetime_or_none(request.get("occurredFrom"), "occurredFrom")
        occurred_to = self._datetime_or_none(request.get("occurredTo"), "occurredTo")
        if occurred_from is not None and occurred_to is not None and occurred_from > occurred_to:
            raise OperationProblem(
                status=422,
                code="invalid-event-time-range",
                detail="Event time range is invalid",
            )
        with self._database.session() as session:
            statement = select(DataOperationEvent).order_by(
                DataOperationEvent.occurred_at.desc(), DataOperationEvent.event_id.desc()
            )
            if actor_refs := request.get("actorRefs"):
                statement = statement.where(DataOperationEvent.actor_ref.in_(actor_refs))
            if resource_types := request.get("resourceTypes"):
                statement = statement.where(DataOperationEvent.resource_type.in_(resource_types))
            if actions := request.get("actions"):
                statement = statement.where(DataOperationEvent.action.in_(actions))
            if occurred_from is not None:
                statement = statement.where(DataOperationEvent.occurred_at >= occurred_from)
            if occurred_to is not None:
                statement = statement.where(DataOperationEvent.occurred_at <= occurred_to)
            rows = session.scalars(statement).all()
            offset = self._decode_offset(request.get("cursor"))
            visible = rows[offset : offset + limit]
            return {
                "items": [self._event_view(row) for row in visible],
                "nextCursor": self._encode_offset(offset + limit)
                if offset + limit < len(rows)
                else None,
            }

    def scheduler_tick(self) -> int:
        """锁定到期计划，将确定性 fire 转成同一 command 队列而不直接同步。"""
        now = self._now()
        created = 0
        with self._database.transaction() as session:
            schedules = session.scalars(
                select(DataOperationSchedule)
                .where(
                    DataOperationSchedule.enabled.is_(True),
                    or_(
                        DataOperationSchedule.next_run_at <= now,
                        DataOperationSchedule.next_run_at.is_(None),
                    ),
                )
                .with_for_update(skip_locked=True)
            ).all()
            for schedule in schedules:
                if schedule.next_run_at is None:
                    # 日历短暂不可用时保留 enabled 状态但不猜测下次日期；恢复后从当前时刻
                    # 重新计算未来 fire，旧的未知日期不会被误判为休市或补投风暴。
                    schedule.next_run_at = self._next_schedule_occurrence(
                        schedule.frequency_json, now
                    )
                    continue
                revision = session.get(
                    DataOperationScheduleRevision,
                    schedule.revision_id,
                    with_for_update=True,
                )
                if revision is None:
                    # 015 迁移会回填既有计划；此保护用于异常人工数据，避免无 revision 的 fire
                    # 绕过冻结快照和外键审计。
                    schedule.next_run_at = None
                    self._record_event(
                        session,
                        "SCHEDULE",
                        schedule.schedule_id,
                        "FIRE",
                        "REJECTED",
                        self._schedule_actor_ref(schedule.schedule_id),
                        f"schedule:{schedule.schedule_id}:revision-missing",
                        self._error(
                            "schedule-revision-missing",
                            "SCHEDULE",
                            False,
                            "Schedule revision is unavailable",
                        ),
                    )
                    continue
                try:
                    due, next_run_at = due_occurrences(
                        schedule.frequency_json,
                        schedule.next_run_at,
                        now,
                        calendar=self._trading_calendar,
                    )
                except ScheduleCalendarUnavailableError:
                    self._record_schedule_rejected_fire(
                        session,
                        schedule=schedule,
                        revision=revision,
                        scheduled_for=schedule.next_run_at,
                        reason_code="schedule-calendar-unavailable",
                        now=now,
                    )
                    schedule.next_run_at = None
                    continue
                except ScheduleFrequencyError:
                    self._record_schedule_rejected_fire(
                        session,
                        schedule=schedule,
                        revision=revision,
                        scheduled_for=schedule.next_run_at,
                        reason_code="schedule-frequency-invalid",
                        now=now,
                    )
                    schedule.next_run_at = None
                    continue
                if len(due) == 1:
                    created += self._queue_schedule_fire(
                        session,
                        schedule=schedule,
                        revision=revision,
                        scheduled_for=due[0],
                        coalesced_count=0,
                        now=now,
                    )
                elif schedule.misfire_policy == "SKIP":
                    if schedule.coalesce:
                        self._record_schedule_skipped_fire(
                            session,
                            schedule=schedule,
                            revision=revision,
                            scheduled_for=due[-1],
                            reason_code="schedule-misfire-skipped-coalesced",
                            coalesced_count=len(due) - 1,
                            now=now,
                        )
                    else:
                        for scheduled_for in due:
                            self._record_schedule_skipped_fire(
                                session,
                                schedule=schedule,
                                revision=revision,
                                scheduled_for=scheduled_for,
                                reason_code="schedule-misfire-skipped",
                                coalesced_count=0,
                                now=now,
                            )
                elif schedule.coalesce:
                    created += self._queue_schedule_fire(
                        session,
                        schedule=schedule,
                        revision=revision,
                        scheduled_for=due[-1],
                        coalesced_count=len(due) - 1,
                        now=now,
                    )
                else:
                    # RUN_ONCE 的语义是恢复时只执行最新一次；关闭 coalesce 仍留下每个
                    # 漏跑 fire 的独立审计事实，不能悄悄把它们吞并成一个普通成功。
                    for scheduled_for in due[:-1]:
                        self._record_schedule_skipped_fire(
                            session,
                            schedule=schedule,
                            revision=revision,
                            scheduled_for=scheduled_for,
                            reason_code="schedule-misfire-run-once",
                            coalesced_count=0,
                            now=now,
                        )
                    created += self._queue_schedule_fire(
                        session,
                        schedule=schedule,
                        revision=revision,
                        scheduled_for=due[-1],
                        coalesced_count=0,
                        now=now,
                    )
                schedule.next_run_at = next_run_at
        return created

    def _dataset_summary(self, session: Session, definition: DatasetDefinition) -> dict[str, Any]:
        """投影目录摘要，所有 freshness/来源/健康判断均由服务端产生。"""
        latest = session.scalar(
            select(DataOperationRun)
            .where(DataOperationRun.dataset_code == definition.dataset_code)
            .order_by(DataOperationRun.requested_at.desc())
            .limit(1)
        )
        latest_success = session.scalar(
            select(DataOperationRun)
            .where(
                DataOperationRun.dataset_code == definition.dataset_code,
                DataOperationRun.status == "SUCCEEDED",
            )
            .order_by(DataOperationRun.finished_at.desc())
            .limit(1)
        )
        schedule = session.scalar(
            select(DataOperationSchedule).where(
                DataOperationSchedule.dataset_code == definition.dataset_code
            )
        )
        latest_evaluation = session.scalar(
            select(DataOperationHealthEvaluation)
            .where(DataOperationHealthEvaluation.dataset_code == definition.dataset_code)
            .order_by(DataOperationHealthEvaluation.evaluated_at.desc())
            .limit(1)
        )
        latest_publication = self._latest_publication(session, definition.dataset_code)
        providers = self._providers_for(definition)
        if definition.model_only:
            availability = "MODEL_ONLY"
            availability_reason = None
            freshness_status = "NOT_APPLICABLE"
            freshness_reason = None
        elif not definition.config_enabled:
            availability = "DISABLED"
            availability_reason = "dataset_disabled_by_config"
            freshness_status, freshness_reason = "UNKNOWN", availability_reason
        elif not providers and not definition.providerless:
            availability = "SOURCE_UNAVAILABLE"
            availability_reason = "provider_not_registered"
            freshness_status, freshness_reason = "UNKNOWN", availability_reason
        elif not definition.dispatcher_ready:
            availability = "DISABLED"
            availability_reason = "dispatcher_not_registered"
            freshness_status, freshness_reason = "UNKNOWN", availability_reason
        elif (
            latest is not None
            and latest.error_json is not None
            and latest.error_json.get("stage") == "PROVIDER_FETCH"
        ):
            availability = "UNKNOWN"
            availability_reason = "latest_source_observation_failed"
            freshness_status, freshness_reason = "UNKNOWN", availability_reason
        else:
            availability = "ENABLED"
            availability_reason = None
            freshness_status, freshness_reason = self._freshness(latest_publication)
        observation_state, observation_reason = self._observation_state(latest, latest_publication)
        return {
            "datasetCode": definition.dataset_code,
            "displayName": definition.display_name,
            "domain": definition.domain,
            "schemaVersion": 1,
            "lifecycleStatus": definition.lifecycle,
            "availability": availability,
            "availabilityReasonCode": availability_reason,
            "observationState": observation_state,
            "observationStateReasonCode": observation_reason,
            "sourceBindings": self._source_bindings(definition, providers),
            "capability": self._capability_view(definition, availability == "ENABLED"),
            "timing": {
                "lastAttemptStartedAt": self._iso(latest.started_at) if latest else None,
                "lastAttemptFinishedAt": self._iso(latest.finished_at) if latest else None,
                "lastAttemptStatus": latest.status if latest else None,
                "lastSuccessAt": self._iso(latest_success.finished_at) if latest_success else None,
                "lastPublishedAt": self._iso(latest_publication.published_at)
                if latest_publication
                else None,
                "dataAsOf": self._publication_data_as_of(latest_publication)
                or self._data_as_of(latest_success),
                "dataAsOfKind": definition.data_as_of_kind,
                "dataAsOfLabel": definition.data_as_of_label,
                "coverageFrom": self._target_date(latest_success, "dateFrom"),
                "coverageTo": self._target_date(latest_success, "dateTo")
                or self._publication_data_as_of(latest_publication)
                or self._data_as_of(latest_success),
                "freshnessStatus": freshness_status,
                "freshnessLagValue": None
                if freshness_status in {"NOT_APPLICABLE", "UNKNOWN"}
                else self._freshness_lag(latest_publication),
                "freshnessLagUnit": None
                if freshness_status in {"NOT_APPLICABLE", "UNKNOWN"}
                else "MINUTES",
                "freshnessReasonCode": freshness_reason,
                "freshnessEvaluatedAt": self._iso(self._now()),
            },
            "latestRun": self._run_summary(latest) if latest else None,
            "healthSummary": self._health_summary_for(
                session, definition.dataset_code, latest_evaluation
            ),
            "scheduleSummary": self._schedule_summary(schedule) if schedule else None,
        }

    def _definition(self, dataset_code: str) -> DatasetDefinition:
        """返回已登记数据集，未知编码用稳定 404 拒绝。"""
        try:
            return self._catalog[dataset_code]
        except KeyError as error:
            raise OperationProblem(
                status=404, code="dataset-not-found", detail="Dataset is not registered"
            ) from error

    def _validate_targets(
        self,
        raw_targets: list[dict[str, Any]],
        *,
        etf_all_profile_versions: Literal["DRAFT", "FROZEN"] = "FROZEN",
    ) -> list[dict[str, Any]]:
        """验证 1—100 个顺序稳定、datasetCode 唯一且模式范围合法的同步目标。"""
        if not 1 <= len(raw_targets) <= 100:
            raise OperationProblem(
                status=422,
                code="invalid-target-count",
                detail="Sync target count must be between 1 and 100",
            )
        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in raw_targets:
            if not isinstance(raw, dict):
                raise OperationProblem(
                    status=400, code="validation-error", detail="Sync target is invalid"
                )
            allowed_target_keys = {
                "datasetCode",
                "mode",
                "selector",
                "dateFrom",
                "dateTo",
                "observationDate",
            }
            if set(raw) - allowed_target_keys:
                raise OperationProblem(
                    status=422,
                    code="invalid-sync-target",
                    detail="Sync target has unsupported fields",
                )
            dataset_code = self._require_string(raw, "datasetCode", max_length=160)
            if dataset_code in seen:
                raise OperationProblem(
                    status=422,
                    code="duplicate-dataset-code",
                    detail="Dataset code must be unique in a batch",
                )
            seen.add(dataset_code)
            definition = self._definition(dataset_code)
            mode = self._require_string(raw, "mode", max_length=24)
            if mode not in definition.modes:
                raise OperationProblem(
                    status=422,
                    code="unsupported-sync-mode",
                    detail="Dataset does not support this sync mode",
                )
            target = {
                "datasetCode": dataset_code,
                "mode": mode,
                "selector": self._validate_selector(
                    self._require_dict(raw, "selector"),
                    definition,
                    etf_all_profile_versions=etf_all_profile_versions,
                ),
                "dateFrom": raw.get("dateFrom"),
                "dateTo": raw.get("dateTo"),
                "observationDate": raw.get("observationDate"),
            }
            self._validate_etf_dataset_operation(target["selector"], definition)
            self._validate_margin_dataset_operation(target["selector"], definition)
            self._validate_index_dataset_selector(target["selector"], definition)
            self._validate_stock_connect_research_dataset_selector(target["selector"], definition)
            self._validate_target_shape(target, definition)
            self._validate_sector_bar_dataset_selector(target["selector"], definition)
            self._validate_money_flow_dataset_operation(target, definition)
            normalized.append(target)
        return normalized

    def _validate_legacy_execution_intent(self, raw: dict[str, Any]) -> dict[str, Any]:
        """规范化仅 Python 兼容层可用的 LegacyExecutionIntent，禁止自由 Provider 参数。"""
        if not isinstance(raw, dict):
            raise OperationProblem(
                status=422,
                code="invalid-legacy-intent",
                detail="Legacy execution intent is invalid",
            )
        kind = raw.get("kind")
        simple_kinds = {"STANDARD", "REPLAY_RAW", "PUBLISH", "REPLAY_AND_PUBLISH"}
        if kind in simple_kinds and set(raw) == {"kind"}:
            return {"kind": kind}
        if kind == "ROLLBACK" and set(raw) == {"kind", "revision"}:
            revision = raw.get("revision")
            if isinstance(revision, int) and not isinstance(revision, bool) and revision >= 1:
                return {"kind": "ROLLBACK", "revision": revision}
        if kind == "EQUITY_BACKFILL":
            return self._validate_equity_backfill_intent(raw)
        raise OperationProblem(
            status=422,
            code="invalid-legacy-intent",
            detail="Legacy execution intent shape is invalid",
        )

    def _validate_equity_backfill_intent(self, raw: dict[str, Any]) -> dict[str, Any]:
        """严格校验股票回填私有意图，不接受可扩展 Provider 参数或缺失冻结身份。"""
        allowed = {
            "kind",
            "planId",
            "referenceBundlePublicationId",
            "referenceBundleDataVersion",
            "referenceManifestHash",
            "childKey",
            "targetIndex",
            "sourceSnapshotHash",
            "sourceContractHash",
            "sourceSupportedExchanges",
            "sourceEarliestDate",
            "backfillDateFrom",
            "backfillDateTo",
            "windowInclusionReason",
            "rosterHash",
            "snapshotObservedOn",
            "marketAsOf",
            "knownAt",
            "observationSemantics",
            "identity",
        }
        if set(raw) != allowed:
            raise OperationProblem(
                status=422,
                code="invalid-equity-backfill-intent",
                detail="Equity backfill intent shape is invalid",
            )
        for key in (
            "planId",
            "referenceBundlePublicationId",
            "referenceBundleDataVersion",
        ):
            self._validated_uuid_text(raw.get(key), key)
        for key in (
            "childKey",
            "sourceSnapshotHash",
            "sourceContractHash",
            "rosterHash",
            "referenceManifestHash",
        ):
            if not isinstance(raw.get(key), str) or re.fullmatch(r"[0-9a-f]{64}", raw[key]) is None:
                raise OperationProblem(
                    status=422,
                    code="invalid-equity-backfill-intent",
                    detail=f"Equity backfill {key} is invalid",
                )
        target_index = raw.get("targetIndex")
        if (
            not isinstance(target_index, int)
            or isinstance(target_index, bool)
            or not 0 <= target_index < 100
        ):
            raise OperationProblem(
                status=422,
                code="invalid-equity-backfill-intent",
                detail="Equity backfill targetIndex is invalid",
            )
        supported_exchanges = raw.get("sourceSupportedExchanges")
        if (
            not isinstance(supported_exchanges, list)
            or any(exchange not in {"SSE", "SZSE", "BSE"} for exchange in supported_exchanges)
            or sorted(set(supported_exchanges)) != supported_exchanges
        ):
            raise OperationProblem(
                status=422,
                code="invalid-equity-backfill-intent",
                detail="Equity backfill sourceSupportedExchanges is invalid",
            )
        snapshot_observed_on = self._validated_date_text(
            raw.get("snapshotObservedOn"), "snapshotObservedOn"
        )
        market_as_of = self._validated_date_text(raw.get("marketAsOf"), "marketAsOf")
        if market_as_of > snapshot_observed_on:
            raise OperationProblem(
                status=422,
                code="invalid-equity-backfill-intent",
                detail="Equity backfill marketAsOf is after snapshotObservedOn",
            )
        self._validated_datetime_text(raw.get("knownAt"), "knownAt")
        if raw.get("observationSemantics") not in {
            "DERIVED_FROM_EXACT_INPUTS",
            "FROZEN_PLAN_BOUNDARY",
        }:
            raise OperationProblem(
                status=422,
                code="invalid-equity-backfill-intent",
                detail="Equity backfill observationSemantics is invalid",
            )
        earliest = raw.get("sourceEarliestDate")
        if earliest is not None:
            self._validated_date_text(earliest, "sourceEarliestDate")
        backfill_from = raw.get("backfillDateFrom")
        backfill_to = raw.get("backfillDateTo")
        if (backfill_from is None) != (backfill_to is None):
            raise OperationProblem(
                status=422,
                code="invalid-equity-backfill-intent",
                detail="Equity backfill internal window is incomplete",
            )
        if backfill_from is not None and backfill_to is not None:
            resolved_backfill_from = self._validated_date_text(backfill_from, "backfillDateFrom")
            resolved_backfill_to = self._validated_date_text(backfill_to, "backfillDateTo")
            if resolved_backfill_from > resolved_backfill_to:
                raise OperationProblem(
                    status=422,
                    code="invalid-equity-backfill-intent",
                    detail="Equity backfill internal window is invalid",
                )
        reason = raw.get("windowInclusionReason")
        if reason not in {
            "FROZEN_PLAN_PREREQUISITE",
            "IDENTITY_AND_SOURCE_CLIPPED_WINDOW",
            "FULL_LEGAL_IDENTITY_WINDOW",
            "SOURCE_STARTS_INSIDE_EVENT_WINDOW",
            "FULL_PROVEN_EVENT_WINDOW",
        }:
            raise OperationProblem(
                status=422,
                code="invalid-equity-backfill-intent",
                detail="Equity backfill inclusion reason is invalid",
            )
        identity = raw.get("identity")
        if identity is not None:
            if not isinstance(identity, dict) or set(identity) != {
                "ordinal",
                "identifierVersionId",
                "securityId",
                "instrumentId",
                "exchange",
                "symbol",
                "effectiveFrom",
                "effectiveTo",
                "knownFrom",
                "knownTo",
            }:
                raise OperationProblem(
                    status=422,
                    code="invalid-equity-backfill-intent",
                    detail="Equity backfill identity shape is invalid",
                )
            ordinal = identity.get("ordinal")
            security_id = identity.get("securityId")
            if (
                not isinstance(ordinal, int)
                or isinstance(ordinal, bool)
                or ordinal < 1
                or not isinstance(security_id, int)
                or isinstance(security_id, bool)
                or security_id < 1
            ):
                raise OperationProblem(
                    status=422,
                    code="invalid-equity-backfill-intent",
                    detail="Equity backfill identity numeric keys are invalid",
                )
            self._validated_uuid_text(identity.get("identifierVersionId"), "identifierVersionId")
            self._validated_uuid_text(identity.get("instrumentId"), "instrumentId")
            if (
                identity.get("exchange") not in {"SSE", "SZSE", "BSE"}
                or not isinstance(identity.get("symbol"), str)
                or re.fullmatch(r"[0-9]{6}", identity["symbol"]) is None
            ):
                raise OperationProblem(
                    status=422,
                    code="invalid-equity-backfill-intent",
                    detail="Equity backfill identity selector is invalid",
                )
            self._validated_date_text(identity.get("effectiveFrom"), "effectiveFrom")
            if identity.get("effectiveTo") is not None:
                self._validated_date_text(identity["effectiveTo"], "effectiveTo")
            self._validated_datetime_text(identity.get("knownFrom"), "knownFrom")
            if identity.get("knownTo") is not None:
                self._validated_datetime_text(identity["knownTo"], "knownTo")
        return {**raw, "identity": None if identity is None else dict(identity)}

    def _validated_uuid_text(self, value: object, field: str) -> UUID:
        """校验私有意图内 UUID 文本并返回解析值，不接受非字符串隐式转换。"""
        if not isinstance(value, str):
            raise OperationProblem(
                status=422,
                code="invalid-equity-backfill-intent",
                detail=f"Equity backfill {field} is invalid",
            )
        try:
            return UUID(value)
        except ValueError as error:
            raise OperationProblem(
                status=422,
                code="invalid-equity-backfill-intent",
                detail=f"Equity backfill {field} is invalid",
            ) from error

    def _validated_date_text(self, value: object, field: str) -> date:
        """校验私有意图内 ISO 日历日期，不允许 datetime 或自由文本。"""
        if not isinstance(value, str):
            raise OperationProblem(
                status=422,
                code="invalid-equity-backfill-intent",
                detail=f"Equity backfill {field} is invalid",
            )
        try:
            return date.fromisoformat(value)
        except ValueError as error:
            raise OperationProblem(
                status=422,
                code="invalid-equity-backfill-intent",
                detail=f"Equity backfill {field} is invalid",
            ) from error

    def _validated_datetime_text(self, value: object, field: str) -> datetime:
        """校验私有意图内带时区 ISO 时间，禁止无时区知识边界。"""
        if not isinstance(value, str):
            raise OperationProblem(
                status=422,
                code="invalid-equity-backfill-intent",
                detail=f"Equity backfill {field} is invalid",
            )
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as error:
            raise OperationProblem(
                status=422,
                code="invalid-equity-backfill-intent",
                detail=f"Equity backfill {field} is invalid",
            ) from error
        if parsed.tzinfo is None:
            raise OperationProblem(
                status=422,
                code="invalid-equity-backfill-intent",
                detail=f"Equity backfill {field} must include timezone",
            )
        return parsed

    def _assert_equity_backfill_submission(
        self,
        *,
        targets: list[dict[str, Any]],
        intents: tuple[dict[str, Any], ...],
        submission_id: UUID,
    ) -> None:
        """在任何 Provider 预检前校验数据库权威 plan、child、身份与来源绑定。"""
        try:
            with self._database.transaction() as session:
                for target, intent in zip(targets, intents, strict=True):
                    self._validate_equity_backfill_binding(
                        session,
                        target=target,
                        intent=intent,
                        submission_id=submission_id,
                        source_snapshot=self._source_snapshot(
                            self._definition(str(target["datasetCode"])),
                            target=target,
                        ),
                        child_statuses=frozenset({"SUBMITTING"}),
                    )
        except EquityBackfillPreconditionFailed as error:
            raise OperationProblem(
                status=409,
                code="equity-backfill-precondition-failed",
                detail=str(error),
            ) from error

    def _assert_equity_backfill_run_binding(
        self,
        session: Session,
        *,
        target: dict[str, Any],
        intent: dict[str, Any],
        submission_id: UUID,
        source_snapshot: list[dict[str, Any]],
    ) -> None:
        """在 command/run 原子写入前再次校验，封闭预检期间的来源或身份漂移。"""
        try:
            self._validate_equity_backfill_binding(
                session,
                target=target,
                intent=intent,
                submission_id=submission_id,
                source_snapshot=source_snapshot,
                child_statuses=frozenset({"SUBMITTING"}),
            )
        except EquityBackfillPreconditionFailed as error:
            raise OperationProblem(
                status=409,
                code="equity-backfill-precondition-failed",
                detail=str(error),
            ) from error

    def _validate_equity_backfill_binding(
        self,
        session: Session,
        *,
        target: Mapping[str, Any],
        intent: Mapping[str, Any],
        submission_id: UUID,
        source_snapshot: list[dict[str, Any]],
        child_statuses: frozenset[str],
    ) -> None:
        """验证一个 target 与冻结 child 的逐索引、来源、身份和 publication 不变量。"""
        plan_id = self._validated_uuid_text(intent.get("planId"), "planId")
        child_key = str(intent.get("childKey"))
        target_index = int(intent.get("targetIndex", -1))
        child = session.scalar(
            select(EquityBackfillChildSpec).where(
                EquityBackfillChildSpec.plan_id == plan_id,
                EquityBackfillChildSpec.child_key == child_key,
            )
        )
        if child is None:
            raise EquityBackfillPreconditionFailed("Frozen equity backfill child does not exist")
        if child.submission_id != submission_id:
            raise EquityBackfillPreconditionFailed("Frozen child submission identity changed")
        if not 0 <= target_index < len(child.targets_json):
            raise EquityBackfillPreconditionFailed("Frozen child target index is out of range")
        if child.targets_json[target_index] != dict(target) or child.intents_json[
            target_index
        ] != dict(intent):
            raise EquityBackfillPreconditionFailed("Frozen child target or intent changed")
        if len(child.targets_json) != len(child.intents_json) or child.target_count != len(
            child.targets_json
        ):
            raise EquityBackfillPreconditionFailed("Frozen child target and intent counts diverged")
        dataset_codes = [str(item.get("datasetCode")) for item in child.targets_json]
        if len(dataset_codes) != len(set(dataset_codes)):
            raise EquityBackfillPreconditionFailed("Frozen child contains duplicate datasets")
        plan = session.get(EquityBackfillPlan, plan_id)
        plan_state = session.get(EquityBackfillPlanState, plan_id)
        child_state = session.get(EquityBackfillChildState, child.child_id)
        if plan is None or plan_state is None or child_state is None:
            raise EquityBackfillPreconditionFailed("Frozen equity backfill state is incomplete")
        if (
            plan_state.status != "RUNNING"
            or plan_state.current_phase != child.phase
            or child_state.status not in child_statuses
        ):
            raise EquityBackfillPreconditionFailed(
                "Equity backfill child is not in the active submission phase"
            )
        self._validate_equity_backfill_seal(session, plan)
        self._validate_equity_backfill_dependencies(session, child)
        if (
            intent.get("rosterHash") != plan.roster_hash
            or intent.get("referenceBundlePublicationId")
            != str(plan.reference_bundle_publication_id)
            or intent.get("referenceBundleDataVersion") != str(plan.reference_bundle_data_version)
            or intent.get("referenceManifestHash") != plan.reference_manifest_hash
            or self._validated_date_text(intent.get("snapshotObservedOn"), "snapshotObservedOn")
            != plan.snapshot_observed_on
            or self._validated_date_text(intent.get("marketAsOf"), "marketAsOf")
            != plan.market_as_of
            or self._validated_datetime_text(intent.get("knownAt"), "knownAt") != plan.known_at
        ):
            raise EquityBackfillPreconditionFailed("Frozen plan temporal boundary changed")
        self._validate_equity_backfill_publications(session, plan)
        dataset_code = str(target.get("datasetCode"))
        expected_observation_semantics = (
            "DERIVED_FROM_EXACT_INPUTS"
            if dataset_code in _EQUITY_BACKFILL_DERIVED_DATASETS
            else "FROZEN_PLAN_BOUNDARY"
        )
        if intent.get("observationSemantics") != expected_observation_semantics:
            raise EquityBackfillPreconditionFailed("Frozen dataset observation semantics changed")
        source = session.get(EquityBackfillPlanSource, (plan_id, dataset_code))
        if source is None:
            raise EquityBackfillPreconditionFailed("Frozen source contract is missing")
        if (
            child.source_hashes_json.get(dataset_code) != source.source_snapshot_hash
            or intent.get("sourceSnapshotHash") != source.source_snapshot_hash
            or intent.get("sourceContractHash") != source.source_contract_hash
            or intent.get("sourceSupportedExchanges") != source.supported_exchanges_json
            or source.source_snapshot_json != source_snapshot
            or self._hash(source_snapshot) != source.source_snapshot_hash
        ):
            raise EquityBackfillPreconditionFailed("Frozen source snapshot or contract changed")
        expected_earliest = (
            None if source.earliest_date is None else source.earliest_date.isoformat()
        )
        if intent.get("sourceEarliestDate") != expected_earliest:
            raise EquityBackfillPreconditionFailed("Frozen source boundary changed")
        self._validate_equity_backfill_source_identity(source, source_snapshot)
        identity_json = intent.get("identity")
        if identity_json is None:
            self._validate_equity_backfill_roster(session, plan)
        elif isinstance(identity_json, Mapping):
            identity = session.get(
                EquityBackfillPlanIdentity,
                (plan_id, int(identity_json["ordinal"])),
            )
            if identity is None:
                raise EquityBackfillPreconditionFailed("Frozen identity does not exist")
            if identity.exchange not in source.supported_exchanges_json:
                raise EquityBackfillPreconditionFailed(
                    "Frozen source contract does not support the identity exchange"
                )
            self._validate_equity_backfill_identity(session, plan, identity, identity_json)
            self._validate_equity_backfill_target_window(plan, source, identity, target)
            self._validate_equity_backfill_internal_window(
                plan,
                child,
                source,
                target,
                intent,
                identity,
            )
        else:
            raise EquityBackfillPreconditionFailed("Frozen identity shape changed")
        if identity_json is None:
            self._validate_equity_backfill_global_window(plan, source, target)
            self._validate_equity_backfill_internal_window(
                plan,
                child,
                source,
                target,
                intent,
                None,
            )

    def _validate_equity_backfill_dependencies(
        self,
        session: Session,
        child: EquityBackfillChildSpec,
    ) -> None:
        """要求直接依赖成功，并使早期基础门成功、可选 child 有明确终态后再跨阶段。"""
        if child.dependency_keys_json:
            dependencies = session.execute(
                select(EquityBackfillChildSpec, EquityBackfillChildState)
                .join(
                    EquityBackfillChildState,
                    EquityBackfillChildState.child_id == EquityBackfillChildSpec.child_id,
                )
                .where(
                    EquityBackfillChildSpec.plan_id == child.plan_id,
                    EquityBackfillChildSpec.child_key.in_(child.dependency_keys_json),
                )
            ).all()
            if len(dependencies) != len(set(child.dependency_keys_json)) or any(
                state.status != "SUCCEEDED" for _spec, state in dependencies
            ):
                raise EquityBackfillPreconditionFailed(
                    "Equity backfill direct dependencies have not succeeded"
                )
        if child.completion_dependency_keys_json:
            completion_dependencies = session.execute(
                select(EquityBackfillChildSpec, EquityBackfillChildState)
                .join(
                    EquityBackfillChildState,
                    EquityBackfillChildState.child_id == EquityBackfillChildSpec.child_id,
                )
                .where(
                    EquityBackfillChildSpec.plan_id == child.plan_id,
                    EquityBackfillChildSpec.child_key.in_(child.completion_dependency_keys_json),
                )
            ).all()
            terminal_statuses = {
                "SUCCEEDED",
                "PARTIAL",
                "FAILED",
                "CANCELLED",
                "BLOCKED",
            }
            if len(completion_dependencies) != len(
                set(child.completion_dependency_keys_json)
            ) or any(
                state.status not in terminal_statuses for _spec, state in completion_dependencies
            ):
                raise EquityBackfillPreconditionFailed(
                    "Equity backfill completion dependencies are not terminal"
                )
        phase_index = PHASES.index(child.phase)
        if phase_index == 0:
            return
        previous = session.execute(
            select(EquityBackfillChildSpec, EquityBackfillChildState)
            .join(
                EquityBackfillChildState,
                EquityBackfillChildState.child_id == EquityBackfillChildSpec.child_id,
            )
            .where(
                EquityBackfillChildSpec.plan_id == child.plan_id,
                EquityBackfillChildSpec.phase.in_(PHASES[:phase_index]),
            )
        ).all()
        for spec, state in previous:
            if spec.requirement == "BASE_REQUIRED" and state.status != "SUCCEEDED":
                raise EquityBackfillPreconditionFailed(
                    "Required earlier equity backfill child has not succeeded"
                )
            if spec.requirement == "OPTIONAL" and state.status not in {
                "SUCCEEDED",
                "PARTIAL",
                "FAILED",
                "CANCELLED",
                "BLOCKED",
            }:
                raise EquityBackfillPreconditionFailed(
                    "Optional earlier equity backfill child lacks a terminal availability result"
                )

    def _validate_equity_backfill_source_identity(
        self,
        source: EquityBackfillPlanSource,
        source_snapshot: list[dict[str, Any]],
    ) -> None:
        """要求控制面绑定仍包含来源合同指定的 provider、上游和方法学。"""
        if self._hash(source.input_contract_json) != source.input_contract_hash:
            raise EquityBackfillPreconditionFailed("Frozen source input contract changed")
        matching = [
            binding
            for binding in source_snapshot
            if binding.get("providerId") == source.expected_provider_id
            and binding.get("upstreamSource") == source.expected_upstream_source
            and binding.get("methodologyCode") == source.methodology_code
            and binding.get("methodologyVersion") == source.methodology_version
            and binding.get("mappingCodeSha256") == source.mapping_version
            and bool(binding.get("effective"))
        ]
        if not matching:
            raise EquityBackfillPreconditionFailed(
                "Approved source identity is no longer effective"
            )
        if source.source_kind == "INTERNAL_EXECUTOR" and not any(
            binding.get("sourceKind") == "INTERNAL_EXECUTOR"
            and binding.get("adapterId") == source.internal_executor_code
            and binding.get("codeSha256") == source.expected_adapter_version
            for binding in matching
        ):
            raise EquityBackfillPreconditionFailed("Internal executor identity changed")

    def _validate_equity_backfill_publications(
        self, session: Session, plan: EquityBackfillPlan
    ) -> None:
        """确认冻结 master 聚合、三所组件与生命周期 publication 仍可被精确解析。"""
        aggregate = session.get(DatasetPublication, plan.aggregate_publication_id)
        if (
            aggregate is None
            or aggregate.dataset != "equity.master.cn-a"
            or aggregate.partition_key != "CN_A_STABLE"
            or aggregate.data_version != plan.aggregate_data_version
            or aggregate.quality_status != "passed"
        ):
            raise EquityBackfillPreconditionFailed("Frozen aggregate master publication changed")
        component_rows = session.scalars(
            select(DatasetPublicationComponent)
            .where(
                DatasetPublicationComponent.aggregate_publication_id
                == plan.aggregate_publication_id
            )
            .order_by(DatasetPublicationComponent.component_partition_key)
        ).all()
        live_components = [
            {
                "exchange": row.component_partition_key,
                "dataVersion": str(row.component_data_version),
            }
            for row in component_rows
        ]
        frozen_components = [
            {
                "exchange": str(item.get("exchange")),
                "dataVersion": str(item.get("dataVersion")),
            }
            for item in plan.aggregate_components_json
        ]
        if live_components != frozen_components or len(live_components) != 3:
            raise EquityBackfillPreconditionFailed("Frozen master components changed")
        for item in plan.lifecycle_publications_json:
            try:
                publication_id = UUID(str(item["publicationId"]))
                data_version = UUID(str(item["dataVersion"]))
            except (KeyError, ValueError) as error:
                raise EquityBackfillPreconditionFailed(
                    "Frozen lifecycle publication shape is invalid"
                ) from error
            publication = session.get(DatasetPublication, publication_id)
            if (
                publication is None
                or publication.dataset != "equity.lifecycle.explicit"
                or publication.partition_key != item.get("exchange")
                or publication.data_version != data_version
                or publication.quality_status != "passed"
            ):
                raise EquityBackfillPreconditionFailed("Frozen lifecycle publication changed")
        if len(plan.lifecycle_publications_json) != 3:
            raise EquityBackfillPreconditionFailed("Lifecycle publication coverage is incomplete")
        bundle = session.get(
            DatasetPublication,
            plan.reference_bundle_publication_id,
        )
        if (
            bundle is None
            or bundle.dataset != "equity.workspace.reference-bundle"
            or bundle.partition_key != "CN_A_REFERENCE"
            or bundle.data_version != plan.reference_bundle_data_version
            or bundle.release_id is None
            or bundle.quality_status != "passed"
            or self._hash(plan.reference_manifest_json) != plan.reference_manifest_hash
        ):
            raise EquityBackfillPreconditionFailed("Frozen equity reference bundle changed")
        if session.get(DatasetRelease, bundle.release_id) is None:
            raise EquityBackfillPreconditionFailed(
                "Frozen equity reference bundle release is missing"
            )
        try:
            FrozenReferenceBundle(
                publication_id=bundle.publication_id,
                data_version=bundle.data_version,
                release_id=bundle.release_id,
                snapshot_observed_on=plan.snapshot_observed_on,
                market_as_of=plan.market_as_of,
                manifest=tuple(plan.reference_manifest_json),
                manifest_hash=plan.reference_manifest_hash,
            ).validate()
        except ValueError as error:
            raise EquityBackfillPreconditionFailed(
                "Frozen equity reference bundle manifest is invalid"
            ) from error
        manifest_components: dict[str, UUID] = {}
        manifest_source_batch_ids: set[UUID] = set()
        for component in plan.reference_manifest_json:
            try:
                component_publication_id = UUID(str(component["publicationId"]))
                component_data_version = UUID(str(component["dataVersion"]))
                release_value = component["releaseId"]
                component_release_id = None if release_value is None else UUID(str(release_value))
                component_source_batch_ids = tuple(
                    UUID(str(value)) for value in component["sourceBatchIds"]
                )
            except (KeyError, TypeError, ValueError) as error:
                raise EquityBackfillPreconditionFailed(
                    "Frozen equity reference component shape is invalid"
                ) from error
            dataset_code = str(component.get("datasetCode"))
            partition_key = str(component.get("partitionKey"))
            manifest_components[f"{dataset_code}|{partition_key}"] = component_data_version
            manifest_source_batch_ids.update(component_source_batch_ids)
            component_publication = session.get(
                DatasetPublication,
                component_publication_id,
            )
            if (
                component_publication is None
                or component_publication.dataset != dataset_code
                or component_publication.partition_key != partition_key
                or component_publication.data_version != component_data_version
                or component_publication.release_id != component_release_id
                or (
                    None
                    if component_publication.effective_as_of is None
                    else component_publication.effective_as_of.isoformat()
                )
                != component.get("effectiveAsOf")
                or component_publication.quality_status not in {"passed", "warned"}
                or not component_source_batch_ids
            ):
                raise EquityBackfillPreconditionFailed("Frozen equity reference component changed")
            source_batches = session.scalars(
                select(SourceBatch).where(
                    SourceBatch.source_batch_id.in_(component_source_batch_ids)
                )
            ).all()
            if len(source_batches) != len(set(component_source_batch_ids)):
                raise EquityBackfillPreconditionFailed(
                    "Frozen equity reference source evidence is incomplete"
                )
        bundle_components = {
            row.component_partition_key: row.component_data_version
            for row in session.scalars(
                select(DatasetPublicationComponent).where(
                    DatasetPublicationComponent.aggregate_publication_id == bundle.publication_id
                )
            ).all()
        }
        bundle_source_batch_ids = set(
            session.scalars(
                select(CanonicalRecordLineage.source_batch_id).where(
                    CanonicalRecordLineage.release_id == bundle.release_id
                )
            ).all()
        )
        if (
            bundle_components != manifest_components
            or bundle_source_batch_ids != manifest_source_batch_ids
        ):
            raise EquityBackfillPreconditionFailed(
                "Frozen equity reference bundle lineage is incomplete"
            )

    def _validate_equity_backfill_seal(
        self,
        session: Session,
        plan: EquityBackfillPlan,
    ) -> None:
        """要求分页连续、计数和 page roster 与 append-only seal 完全一致。"""
        seal = session.get(EquityBackfillPlanSeal, plan.plan_id)
        pages = session.scalars(
            select(EquityBackfillPlanPage)
            .where(EquityBackfillPlanPage.plan_id == plan.plan_id)
            .order_by(EquityBackfillPlanPage.page_number)
        ).all()
        if (
            seal is None
            or seal.child_count != plan.child_count
            or seal.page_count != len(pages)
            or sum(page.child_count for page in pages) != plan.child_count
        ):
            raise EquityBackfillPreconditionFailed("Equity backfill plan is not completely sealed")
        expected_first = 1
        page_roster: list[dict[str, Any]] = []
        for expected_page_number, page in enumerate(pages, start=1):
            if (
                page.page_number != expected_page_number
                or page.first_ordinal != expected_first
                or page.last_ordinal - page.first_ordinal + 1 != page.child_count
            ):
                raise EquityBackfillPreconditionFailed(
                    "Equity backfill plan pages are not contiguous"
                )
            page_roster.append(
                {
                    "pageNumber": page.page_number,
                    "firstOrdinal": page.first_ordinal,
                    "lastOrdinal": page.last_ordinal,
                    "childCount": page.child_count,
                    "payloadBytes": page.payload_bytes,
                    "pageHash": page.page_hash,
                }
            )
            expected_first = page.last_ordinal + 1
        if self._hash(page_roster) != seal.page_roster_hash:
            raise EquityBackfillPreconditionFailed(
                "Equity backfill page roster differs from its seal"
            )

    def _validate_equity_backfill_roster(self, session: Session, plan: EquityBackfillPlan) -> None:
        """重验全部计划身份与实时双时态行，覆盖全局事件和 discovery 执行。"""
        rows = session.scalars(
            select(EquityBackfillPlanIdentity)
            .where(EquityBackfillPlanIdentity.plan_id == plan.plan_id)
            .order_by(EquityBackfillPlanIdentity.ordinal)
        ).all()
        identities = tuple(self._frozen_identity(row) for row in rows)
        if (
            len(identities) != plan.roster_count
            or compute_roster_hash(identities) != plan.roster_hash
        ):
            raise EquityBackfillPreconditionFailed("Frozen equity identity roster changed")
        for row in rows:
            self._validate_equity_backfill_live_identity(session, row)

    def _validate_equity_backfill_identity(
        self,
        session: Session,
        plan: EquityBackfillPlan,
        identity: EquityBackfillPlanIdentity,
        intent: Mapping[str, Any],
    ) -> None:
        """比较私有意图、plan identity 与实时确认身份的每个稳定字段。"""
        expected = {
            "ordinal": identity.ordinal,
            "identifierVersionId": str(identity.identifier_version_id),
            "securityId": identity.security_id,
            "instrumentId": str(identity.instrument_id),
            "exchange": identity.exchange,
            "symbol": identity.symbol,
            "effectiveFrom": identity.effective_from.isoformat(),
            "effectiveTo": (
                None if identity.effective_to is None else identity.effective_to.isoformat()
            ),
            "knownFrom": identity.known_from.isoformat(),
            "knownTo": None if identity.known_to is None else identity.known_to.isoformat(),
        }
        if dict(intent) != expected:
            raise EquityBackfillPreconditionFailed("Frozen identity intent changed")
        if identity.plan_id != plan.plan_id:
            raise EquityBackfillPreconditionFailed("Frozen identity belongs to another plan")
        self._validate_equity_backfill_live_identity(session, identity)

    def _validate_equity_backfill_live_identity(
        self, session: Session, identity: EquityBackfillPlanIdentity
    ) -> None:
        """比较计划身份与 canonical 实时版本，代码复用时绝不按 symbol 取当前行。"""
        version = session.get(EquityIdentifierVersion, identity.identifier_version_id)
        instrument = session.get(EquityInstrument, identity.security_id)
        if (
            version is None
            or instrument is None
            or version.identity_state != "CONFIRMED"
            or version.security_id != identity.security_id
            or instrument.instrument_id != identity.instrument_id
            or version.exchange != identity.exchange
            or version.symbol != identity.symbol
            or version.effective_from != identity.effective_from
            or version.effective_to != identity.effective_to
            or version.known_from != identity.known_from
            or version.known_to != identity.known_to
            or version.effective_date_precision != identity.effective_date_precision
        ):
            raise EquityBackfillPreconditionFailed("Canonical equity identity changed")

    def _validate_equity_backfill_target_window(
        self,
        plan: EquityBackfillPlan,
        source: EquityBackfillPlanSource,
        identity: EquityBackfillPlanIdentity,
        target: Mapping[str, Any],
    ) -> None:
        """禁止证券 target 越过来源起点、身份半开区间或计划业务日。"""
        selector = target.get("selector")
        if selector != {
            "kind": "INSTRUMENT",
            "exchange": identity.exchange,
            "symbol": identity.symbol,
        }:
            raise EquityBackfillPreconditionFailed(
                "Target selector no longer matches frozen identity"
            )
        if target.get("mode") == "DATE_RANGE":
            start = self._validated_date_text(target.get("dateFrom"), "dateFrom")
            end = self._validated_date_text(target.get("dateTo"), "dateTo")
            target_boundary = (
                plan.snapshot_observed_on
                if target.get("datasetCode") == "equity.corporate_action"
                else plan.market_as_of
            )
            if (
                start < identity.effective_from
                or identity.effective_to is not None
                and end >= identity.effective_to
                or source.earliest_date is not None
                and start < source.earliest_date
                or end > target_boundary
            ):
                raise EquityBackfillPreconditionFailed(
                    "Target window exceeds identity, source or plan boundary"
                )

    def _validate_equity_backfill_global_window(
        self,
        plan: EquityBackfillPlan,
        source: EquityBackfillPlanSource,
        target: Mapping[str, Any],
    ) -> None:
        """禁止全局事件在已证明来源边界前取数或越过冻结计划日。"""
        if target.get("mode") == "DATE_RANGE":
            start = self._validated_date_text(target.get("dateFrom"), "dateFrom")
            end = self._validated_date_text(target.get("dateTo"), "dateTo")
            target_boundary = (
                plan.snapshot_observed_on
                if target.get("datasetCode") == "equity.corporate_event.earnings.reported"
                else plan.market_as_of
            )
            if (
                source.earliest_date is None
                or start < source.earliest_date
                or end > target_boundary
            ):
                raise EquityBackfillPreconditionFailed(
                    "Global target exceeds frozen source or plan boundary"
                )
        if target.get("mode") == "OBSERVATION_DATE":
            dataset_code = target.get("datasetCode")
            expected_observation_date = (
                plan.market_as_of
                if dataset_code in {"equity.trading_status.1d", "equity.discovery.eod"}
                else plan.snapshot_observed_on
            )
            if (
                self._validated_date_text(target.get("observationDate"), "observationDate")
                != expected_observation_date
            ):
                raise EquityBackfillPreconditionFailed(
                    "Observation target differs from its frozen plan boundary"
                )

    def _validate_equity_backfill_internal_window(
        self,
        plan: EquityBackfillPlan,
        child: EquityBackfillChildSpec,
        source: EquityBackfillPlanSource,
        target: Mapping[str, Any],
        intent: Mapping[str, Any],
        identity: EquityBackfillPlanIdentity | None,
    ) -> None:
        """校验低基数 child 冻结的全历史范围，执行器只能按该范围内部分窗。"""
        from_text = intent.get("backfillDateFrom")
        to_text = intent.get("backfillDateTo")
        if from_text is None and to_text is None:
            if child.window_from is not None or child.window_to is not None:
                raise EquityBackfillPreconditionFailed(
                    "Frozen child internal window is missing from intent"
                )
            return
        start = self._validated_date_text(from_text, "backfillDateFrom")
        end = self._validated_date_text(to_text, "backfillDateTo")
        if child.window_from != start or child.window_to != end:
            raise EquityBackfillPreconditionFailed("Frozen child internal window changed")
        if source.earliest_date is not None and start < source.earliest_date:
            raise EquityBackfillPreconditionFailed(
                "Internal window starts before proven source coverage"
            )
        dataset_code = str(target.get("datasetCode"))
        boundary = (
            plan.snapshot_observed_on
            if dataset_code
            in {
                "equity.corporate_action",
                "equity.corporate_event.earnings.reported",
            }
            else plan.market_as_of
        )
        if end > boundary:
            raise EquityBackfillPreconditionFailed(
                "Internal window exceeds its frozen plan boundary"
            )
        if identity is not None and (
            start < identity.effective_from
            or identity.effective_to is not None
            and end >= identity.effective_to
        ):
            raise EquityBackfillPreconditionFailed(
                "Internal window exceeds frozen identity validity"
            )

    def _frozen_identity(self, value: EquityBackfillPlanIdentity) -> FrozenIdentity:
        """把 ORM 计划身份投影为共享摘要算法使用的不可变值对象。"""
        return FrozenIdentity(
            ordinal=value.ordinal,
            identifier_version_id=value.identifier_version_id,
            security_id=value.security_id,
            instrument_id=value.instrument_id,
            exchange=value.exchange,
            symbol=value.symbol,
            effective_from=value.effective_from,
            effective_to=value.effective_to,
            known_from=value.known_from,
            known_to=value.known_to,
            effective_date_precision=value.effective_date_precision,
        )

    def _validate_target_shape(self, target: dict[str, Any], definition: DatasetDefinition) -> None:
        """验证模式与日期字段互斥，并限制 DATE_RANGE 不能超过数据集能力范围。"""
        mode = target["mode"]
        date_from, date_to, observation = (
            target["dateFrom"],
            target["dateTo"],
            target["observationDate"],
        )
        if mode == "DATE_RANGE":
            if (
                not isinstance(date_from, str)
                or not isinstance(date_to, str)
                or observation is not None
            ):
                raise OperationProblem(
                    status=422,
                    code="invalid-date-range",
                    detail="DATE_RANGE requires dateFrom and dateTo",
                )
            try:
                start, end = date.fromisoformat(date_from), date.fromisoformat(date_to)
            except ValueError as error:
                raise OperationProblem(
                    status=422, code="invalid-date-range", detail="DATE_RANGE dates are invalid"
                ) from error
            if (
                start > end
                or definition.max_range_days is not None
                and (end - start).days + 1 > definition.max_range_days
            ):
                raise OperationProblem(
                    status=422, code="invalid-date-range", detail="DATE_RANGE exceeds dataset limit"
                )
        elif mode == "OBSERVATION_DATE":
            if date_from is not None or date_to is not None or not isinstance(observation, str):
                raise OperationProblem(
                    status=422,
                    code="invalid-observation-date",
                    detail="OBSERVATION_DATE requires observationDate",
                )
            try:
                date.fromisoformat(observation)
            except ValueError as error:
                raise OperationProblem(
                    status=422,
                    code="invalid-observation-date",
                    detail="Observation date is invalid",
                ) from error
        elif date_from is not None or date_to is not None or observation is not None:
            raise OperationProblem(
                status=422,
                code="invalid-sync-target",
                detail="FULL and INCREMENTAL do not accept date fields",
            )

    def _validate_selector(
        self,
        raw: dict[str, Any],
        definition: DatasetDefinition,
        *,
        etf_all_profile_versions: Literal["DRAFT", "FROZEN"] = "FROZEN",
    ) -> dict[str, Any]:
        """严格规范化合同允许的业务选择器，拒绝任意 Provider 参数和未知字段。"""
        kind = self._require_string(raw, "kind", max_length=24)
        if kind == "MONEY_FLOW":
            if kind not in definition.selector_kinds:
                raise OperationProblem(
                    status=422,
                    code="unsupported-target-selector",
                    detail="Dataset does not support this target selector",
                )
            return self._money_flow_selector(raw, definition)
        allowed_keys: dict[str, set[str]] = {
            "GLOBAL": {"kind"},
            "INSTRUMENT": {"kind", "exchange", "symbol"},
            "SECTOR": {"kind", "scheme", "sectorCode"},
            "SCHEME": {"kind", "scheme"},
            "EXCHANGE": {"kind", "exchange"},
            "CONTRACT": {"kind", "venue", "contract"},
            "ETF": set(raw),
            "MARGIN": {"kind", "operation", "venue", "security"},
            "STOCK_CONNECT": {"kind", "operation", "channel", "direction"},
            "STOCK_CONNECT_RESEARCH": {"kind", "operation", "channel", "direction"},
            "TRADING_EVENT": {"kind", "operation"},
            "INDEX": {"kind", "administrator", "capability", "indexCode"},
        }
        if kind not in allowed_keys or (kind != "ETF" and set(raw) != allowed_keys[kind]):
            raise OperationProblem(
                status=422,
                code="invalid-target-selector",
                detail="Target selector shape is invalid",
            )
        if kind not in definition.selector_kinds:
            raise OperationProblem(
                status=422,
                code="unsupported-target-selector",
                detail="Dataset does not support this target selector",
            )
        if kind == "GLOBAL":
            return {"kind": kind}
        if kind == "INSTRUMENT":
            return self._instrument_selector(raw)
        if kind == "SECTOR":
            return {
                "kind": kind,
                "scheme": self._require_string(raw, "scheme", max_length=64),
                "sectorCode": self._require_string(raw, "sectorCode", max_length=120),
            }
        if kind == "SCHEME":
            return {
                "kind": kind,
                "scheme": self._require_string(raw, "scheme", max_length=64),
            }
        if kind == "EXCHANGE":
            return {"kind": kind, "exchange": self._exchange(raw, "exchange")}
        if kind == "CONTRACT":
            return {
                "kind": kind,
                "venue": self._enum(raw, "venue", {"CFFEX", "SHFE", "DCE", "CZCE", "INE"}),
                "contract": self._pattern_string(
                    raw, "contract", max_length=64, pattern=r"[0-9A-Z._-]+"
                ),
            }
        if kind == "ETF":
            return self._etf_selector(
                raw,
                profile_versions=etf_all_profile_versions,
            )
        if kind == "MARGIN":
            return self._margin_selector(raw)
        if kind == "STOCK_CONNECT":
            return self._stock_connect_selector(raw)
        if kind == "STOCK_CONNECT_RESEARCH":
            return self._stock_connect_research_selector(raw)
        if kind == "TRADING_EVENT":
            return {
                "kind": kind,
                "operation": self._enum(raw, "operation", {"DRAGON_TIGER", "BLOCK_TRADE"}),
            }
        if kind == "INDEX":
            return self._index_selector(raw)
        raise OperationProblem(
            status=422,
            code="invalid-target-selector",
            detail="Target selector kind is invalid",
        )

    def _money_flow_selector(
        self,
        raw: dict[str, Any],
        definition: DatasetDefinition,
    ) -> dict[str, Any]:
        """冻结资金流唯一方法学、范围与窗口，拒绝把自由 AKShare 参数透传到 adapter。"""
        if definition.dataset_code == _MONEY_FLOW_DAILY_DATASET:
            operation = raw.get("operation")
            scope = raw.get("scope")
            if operation != "DAILY" or scope not in {"EQUITY", "SECTOR", "MARKET"}:
                raise OperationProblem(
                    status=422,
                    code="invalid-target-selector",
                    detail="Money-flow daily selector is invalid",
                )
            allowed = {
                "EQUITY": {"kind", "operation", "scope", "exchange", "symbol"},
                "SECTOR": {"kind", "operation", "scope", "scheme", "sectorCode"},
                "MARKET": {"kind", "operation", "scope"},
            }[scope]
            if set(raw) != allowed:
                raise OperationProblem(
                    status=422,
                    code="invalid-target-selector",
                    detail="Money-flow daily selector shape is invalid",
                )
            result: dict[str, Any] = {
                "kind": "MONEY_FLOW",
                "operation": "DAILY",
                "scope": scope,
            }
            if scope == "EQUITY":
                result.update(
                    {
                        "exchange": self._exchange(raw, "exchange"),
                        "symbol": self._pattern_string(
                            raw, "symbol", max_length=6, pattern=r"[0-9]{6}"
                        ),
                    }
                )
            elif scope == "SECTOR":
                scheme = self._require_string(raw, "scheme", max_length=64)
                if scheme != "eastmoney.industry":
                    raise OperationProblem(
                        status=422,
                        code="money-flow-sector-scheme-unsupported",
                        detail="EastMoney daily sector money flow only supports eastmoney.industry",
                    )
                result.update(
                    {
                        "scheme": scheme,
                        "sectorCode": self._require_string(raw, "sectorCode", max_length=120),
                    }
                )
            return result
        if definition.dataset_code != _MONEY_FLOW_RANKING_DATASET:
            raise OperationProblem(
                status=422,
                code="money-flow-dataset-operation-mismatch",
                detail="Money-flow selector is not valid for this dataset",
            )
        if raw.get("operation") != "RANKING":
            raise OperationProblem(
                status=422,
                code="invalid-target-selector",
                detail="Money-flow ranking operation is invalid",
            )
        methodology = raw.get("methodology")
        scope = raw.get("scope")
        if methodology == "EASTMONEY_ORDER_SIZE":
            if scope == "EQUITY":
                allowed = {"kind", "operation", "methodology", "scope", "window"}
                windows = {"TODAY", "DAY_3", "DAY_5", "DAY_10"}
            elif scope == "SECTOR":
                allowed = {
                    "kind",
                    "operation",
                    "methodology",
                    "scope",
                    "window",
                    "sectorType",
                }
                windows = {"TODAY", "DAY_5", "DAY_10"}
            else:
                allowed, windows = set(), set()
            if set(raw) != allowed or raw.get("window") not in windows:
                raise OperationProblem(
                    status=422,
                    code="invalid-target-selector",
                    detail="EastMoney money-flow ranking selector is invalid",
                )
            result = {
                "kind": "MONEY_FLOW",
                "operation": "RANKING",
                "methodology": methodology,
                "scope": scope,
                "window": str(raw["window"]),
            }
            if scope == "SECTOR":
                result["sectorType"] = self._enum(
                    raw,
                    "sectorType",
                    {"INDUSTRY", "CONCEPT", "REGION"},
                )
            return result
        if methodology == "THS_TRADE_DIRECTION":
            allowed = {"kind", "operation", "methodology", "scope", "window"}
            if (
                set(raw) != allowed
                or scope not in {"EQUITY", "INDUSTRY", "CONCEPT"}
                or raw.get("window") not in {"INTRADAY", "DAY_3", "DAY_5", "DAY_10", "DAY_20"}
            ):
                raise OperationProblem(
                    status=422,
                    code="invalid-target-selector",
                    detail="THS money-flow ranking selector is invalid",
                )
            return {
                "kind": "MONEY_FLOW",
                "operation": "RANKING",
                "methodology": methodology,
                "scope": str(scope),
                "window": str(raw["window"]),
            }
        raise OperationProblem(
            status=422,
            code="invalid-target-selector",
            detail="Money-flow ranking methodology is invalid",
        )

    def _instrument_selector(self, raw: dict[str, Any]) -> dict[str, Any]:
        """规范化股票交易所与代码，作为顶层或保证金嵌套选择器复用。"""
        return {
            "kind": "INSTRUMENT",
            "exchange": self._exchange(raw, "exchange"),
            "symbol": self._pattern_string(raw, "symbol", max_length=32, pattern=r"[0-9A-Z.-]+"),
        }

    def _etf_selector(
        self,
        raw: dict[str, Any],
        *,
        profile_versions: Literal["DRAFT", "FROZEN"],
    ) -> dict[str, Any]:
        """兼容旧单只形状，并严格校验双场所目录与双市场全集语义。"""
        legacy_keys = {"kind", "operation", "venue", "etf"}
        all_venues_keys = {"kind", "operation", "scope", "venue", "etf"}
        all_keys = {
            "kind",
            "operation",
            "venue",
            "scope",
            "etf",
            "profileDataVersions",
        }
        raw_keys = frozenset(raw)
        if raw_keys not in {
            frozenset(legacy_keys),
            frozenset(all_venues_keys),
            frozenset(all_keys),
        }:
            raise OperationProblem(
                status=422,
                code="invalid-target-selector",
                detail="ETF selector shape is invalid",
            )
        operation = self._enum(raw, "operation", {"MASTER", "STATUS", "BARS", "NAV"})
        if raw_keys == all_venues_keys:
            if (
                operation != "MASTER"
                or raw.get("scope") != "ALL_VENUES"
                or raw.get("venue") is not None
                or raw.get("etf") is not None
            ):
                raise OperationProblem(
                    status=422,
                    code="invalid-target-selector",
                    detail="ETF all-venues master selector is invalid",
                )
            return {
                "kind": "ETF",
                "operation": "MASTER",
                "scope": "ALL_VENUES",
                "venue": None,
                "etf": None,
            }
        if raw_keys == all_keys:
            if (
                operation == "MASTER"
                or raw.get("scope") != "ALL_ETFS"
                or raw.get("venue") is not None
                or raw.get("etf") is not None
            ):
                raise OperationProblem(
                    status=422,
                    code="invalid-target-selector",
                    detail="ETF all-market selector is invalid",
                )
            versions = raw.get("profileDataVersions")
            if profile_versions == "DRAFT":
                if versions is not None:
                    raise OperationProblem(
                        status=422,
                        code="invalid-target-selector",
                        detail="ETF all-market preflight must not pin profile versions",
                    )
                normalized_versions: dict[str, str] | None = None
            else:
                if not isinstance(versions, dict) or set(versions) != {"SSE", "SZSE"}:
                    raise OperationProblem(
                        status=422,
                        code="invalid-target-selector",
                        detail="ETF all-market selector requires SSE and SZSE profile versions",
                    )
                try:
                    normalized_versions = {
                        venue: str(UUID(str(versions[venue]))) for venue in ("SSE", "SZSE")
                    }
                except (TypeError, ValueError) as error:
                    raise OperationProblem(
                        status=422,
                        code="invalid-target-selector",
                        detail="ETF profile data version is invalid",
                    ) from error
            return {
                "kind": "ETF",
                "operation": operation,
                "venue": None,
                "scope": "ALL_ETFS",
                "etf": None,
                "profileDataVersions": normalized_versions,
            }
        venue = raw["venue"]
        etf = raw["etf"]
        if venue is not None and venue not in {"SSE", "SZSE"}:
            raise OperationProblem(
                status=422, code="invalid-target-selector", detail="ETF venue is invalid"
            )
        if operation == "MASTER":
            if venue is None or etf is not None:
                raise OperationProblem(
                    status=422,
                    code="invalid-target-selector",
                    detail="ETF master selector is invalid",
                )
        elif not isinstance(etf, str) or re.fullmatch(r"(?:SSE|SZSE)\.[0-9]{6}", etf) is None:
            raise OperationProblem(
                status=422, code="invalid-target-selector", detail="ETF selector is invalid"
            )
        elif venue is not None and etf.split(".", 1)[0] != venue:
            raise OperationProblem(
                status=422,
                code="invalid-target-selector",
                detail="ETF venue and qualified identifier must match",
            )
        return {"kind": "ETF", "operation": operation, "venue": venue, "etf": etf}

    def _validate_etf_dataset_operation(
        self,
        selector: dict[str, Any],
        definition: DatasetDefinition,
    ) -> None:
        """绑定 ETF datasetCode 与唯一操作，避免审计数据集和真实发布能力错位。"""
        if selector.get("kind") != "ETF":
            return
        expected = {
            "fund.etf.profile.reported": "MASTER",
            "fund.etf.trading_state.reported": "STATUS",
            "fund.etf.bar.1d.reported": "BARS",
            "fund.etf.nav.1d.reported": "NAV",
        }.get(definition.dataset_code)
        if expected is None or selector.get("operation") != expected:
            raise OperationProblem(
                status=422,
                code="etf-dataset-operation-mismatch",
                detail="ETF dataset and selector operation do not match",
            )

    def _validate_margin_dataset_operation(
        self,
        selector: dict[str, Any],
        definition: DatasetDefinition,
    ) -> None:
        """绑定两融数据集、场所和可真实执行的全场所抓取范围。

        当前 `AKShare` 两融接口只支持沪深全场所批次；不能接受单证券后悄然把整所
        明细入库，且上交所资格名单没有可用来源接口，必须在受理前明确拒绝。
        """
        if selector.get("kind") != "MARGIN":
            return
        expected = {
            "market.margin.market.1d.reported": "MARKET",
            "market.margin.security.1d.reported": "SECURITY",
            "market.margin.eligibility.reported": "ELIGIBILITY",
        }.get(definition.dataset_code)
        if expected is None or selector.get("operation") != expected:
            raise OperationProblem(
                status=422,
                code="margin-dataset-operation-mismatch",
                detail="Margin dataset and selector operation do not match",
            )
        if selector.get("security") is not None:
            raise OperationProblem(
                status=422,
                code="margin-security-selector-unsupported",
                detail="Margin P0 only supports venue-wide source batches",
            )
        venue = selector.get("venue")
        if expected == "ELIGIBILITY" and venue not in {"SZSE", "BSE"}:
            raise OperationProblem(
                status=422,
                code="margin-eligibility-venue-unsupported",
                detail="AKShare margin eligibility is only available for SZSE or BSE",
            )
        if expected in {"MARKET", "SECURITY"} and venue not in {"SSE", "SZSE"}:
            raise OperationProblem(
                status=422,
                code="margin-venue-unsupported",
                detail="AKShare margin market and security data only support SSE or SZSE",
            )

    @staticmethod
    def _validate_index_dataset_selector(
        selector: dict[str, Any],
        definition: DatasetDefinition,
    ) -> None:
        """把六个指数 research 数据集绑定到唯一管理人、能力和代码空值语义。"""
        if selector.get("kind") != "INDEX":
            return
        expected = _INDEX_DATASET_TARGETS.get(definition.dataset_code)
        if expected is None:
            raise OperationProblem(
                status=422,
                code="index-dataset-selector-unsupported",
                detail="Index selector is not available for this dataset",
            )
        administrator, capability, requires_index_code = expected
        if (
            selector.get("administrator") != administrator
            or selector.get("capability") != capability
            or (requires_index_code and not isinstance(selector.get("indexCode"), str))
            or (not requires_index_code and selector.get("indexCode") is not None)
        ):
            raise OperationProblem(
                status=422,
                code="index-dataset-selector-mismatch",
                detail="Index dataset and selector do not match",
            )

    @staticmethod
    def _validate_stock_connect_research_dataset_selector(
        selector: dict[str, Any],
        definition: DatasetDefinition,
    ) -> None:
        """隔离 AKShare 港通统计 research 命令，禁止复用官方完整包或未登记数据集。"""
        if selector.get("kind") != "STOCK_CONNECT_RESEARCH":
            return
        if (
            definition.dataset_code != _STOCK_CONNECT_RESEARCH_DATASET
            or selector.get("operation") != "MARKET_STAT"
        ):
            raise OperationProblem(
                status=422,
                code="stock-connect-research-dataset-selector-mismatch",
                detail="Stock-connect research dataset and selector do not match",
            )

    def _validate_money_flow_dataset_operation(
        self,
        target: dict[str, Any],
        definition: DatasetDefinition,
    ) -> None:
        """把资金流数据集和 selector 绑定到真实 SDK 能力，并阻断伪历史排行。"""
        selector = target["selector"]
        if selector.get("kind") != "MONEY_FLOW":
            return
        if definition.dataset_code == _MONEY_FLOW_DAILY_DATASET:
            if selector.get("operation") != "DAILY":
                raise OperationProblem(
                    status=422,
                    code="money-flow-dataset-operation-mismatch",
                    detail="Money-flow daily dataset requires DAILY selector",
                )
            return
        if (
            definition.dataset_code != _MONEY_FLOW_RANKING_DATASET
            or selector.get("operation") != "RANKING"
        ):
            raise OperationProblem(
                status=422,
                code="money-flow-dataset-operation-mismatch",
                detail="Money-flow ranking dataset requires RANKING selector",
            )
        observation = target.get("observationDate")
        if (
            not isinstance(observation, str)
            or date.fromisoformat(observation) != self._now().astimezone(_SHANGHAI).date()
        ):
            # 上游接口没有历史日期参数；禁止以今日 SDK 页面倒填任意历史 observationDate。
            raise OperationProblem(
                status=422,
                code="money-flow-ranking-historical-observation-unsupported",
                detail="Money-flow ranking only supports the current Shanghai observation date",
            )

    @staticmethod
    def _validate_sector_bar_dataset_selector(
        selector: dict[str, Any],
        definition: DatasetDefinition,
    ) -> None:
        """限定东财板块原生 K 线只能使用其行业、概念目录身份。"""
        if definition.dataset_code not in _SECTOR_BAR_DATASETS:
            return
        if selector.get("kind") not in {"GLOBAL", "SCHEME", "SECTOR"}:
            raise OperationProblem(
                status=422,
                code="sector-bar-selector-unsupported",
                detail="Sector bar dataset requires a sector catalog selector",
            )
        scheme = selector.get("scheme")
        if scheme is not None and scheme not in {"eastmoney.industry", "eastmoney.concept"}:
            raise OperationProblem(
                status=422,
                code="sector-bar-scheme-unsupported",
                detail="EastMoney sector bars only support industry or concept schemes",
            )

    def _margin_selector(self, raw: dict[str, Any]) -> dict[str, Any]:
        """校验两融场所、操作和全场所批次范围，禁止尚未实现的单证券筛选。"""
        security = raw["security"]
        if security is not None:
            raise OperationProblem(
                status=422,
                code="margin-security-selector-unsupported",
                detail="Margin security selector must be null",
            )
        operation = self._enum(raw, "operation", {"MARKET", "SECURITY", "ELIGIBILITY"})
        venue = self._enum(raw, "venue", {"SSE", "SZSE", "BSE"})
        return {
            "kind": "MARGIN",
            "operation": operation,
            "venue": venue,
            "security": None,
        }

    def _stock_connect_selector(self, raw: dict[str, Any]) -> dict[str, Any]:
        """校验完整包市场与方向；单个 `MARKET` 目标可覆盖四条通道。"""
        direction = raw["direction"]
        if direction not in {"NORTHBOUND", "SOUTHBOUND", None}:
            raise OperationProblem(
                status=422,
                code="invalid-target-selector",
                detail="Stock connect direction is invalid",
            )
        return {
            "kind": "STOCK_CONNECT",
            # 完整包执行器会同时同步统计、来源活跃榜、身份与状态；拆成多个操作会破坏原子边界。
            "operation": self._enum(raw, "operation", {"MARKET"}),
            "channel": self._enum(raw, "channel", {"ALL", "SH", "SZ"}),
            "direction": direction,
        }

    def _stock_connect_research_selector(self, raw: dict[str, Any]) -> dict[str, Any]:
        """校验 AKShare 港通统计 research 通道范围，不让其伪装为官方完整包。"""
        direction = raw["direction"]
        if direction not in {"NORTHBOUND", "SOUTHBOUND", None}:
            raise OperationProblem(
                status=422,
                code="invalid-target-selector",
                detail="Stock-connect research direction is invalid",
            )
        return {
            "kind": "STOCK_CONNECT_RESEARCH",
            "operation": self._enum(raw, "operation", {"MARKET_STAT"}),
            "channel": self._enum(raw, "channel", {"ALL", "SH", "SZ"}),
            "direction": direction,
        }

    def _index_selector(self, raw: dict[str, Any]) -> dict[str, Any]:
        """校验指数研究观察的管理人、当前快照能力和 indexCode 空值规则。"""
        administrator = self._enum(raw, "administrator", {"CSI", "CNI"})
        capability = self._enum(
            raw,
            "capability",
            {"index.catalog.snapshot", "index.constituent.snapshot", "index.weight.snapshot"},
        )
        index_code = raw["indexCode"]
        if capability == "index.catalog.snapshot":
            if index_code is not None:
                raise OperationProblem(
                    status=422,
                    code="invalid-target-selector",
                    detail="Index catalog selector requires null indexCode",
                )
            return {
                "kind": "INDEX",
                "administrator": administrator,
                "capability": capability,
                "indexCode": None,
            }
        if index_code is None:
            raise OperationProblem(
                status=422,
                code="invalid-target-selector",
                detail="Index snapshot selector requires indexCode",
            )
        return {
            "kind": "INDEX",
            "administrator": administrator,
            "capability": capability,
            "indexCode": self._pattern_string(
                raw,
                "indexCode",
                max_length=8,
                pattern=r"[A-Z0-9]{6,8}",
            ),
        }

    def _exchange(self, raw: dict[str, Any], key: str) -> str:
        """读取合同限制的沪深北交易所代码。"""
        return self._enum(raw, key, {"SSE", "SZSE", "BSE"})

    def _enum(self, raw: dict[str, Any], key: str, values: set[str]) -> str:
        """读取有限枚举字段，避免将自由文本传入 adapter 或计划。"""
        value = raw.get(key)
        if not isinstance(value, str) or value not in values:
            raise OperationProblem(
                status=422, code="invalid-target-selector", detail=f"{key} is invalid"
            )
        return value

    def _pattern_string(
        self, raw: dict[str, Any], key: str, *, max_length: int, pattern: str
    ) -> str:
        """读取满足合同正则的受限字符串，拒绝空白和任意 URI。"""
        value = self._require_string(raw, key, max_length=max_length)
        if re.fullmatch(pattern, value) is None:
            raise OperationProblem(
                status=422, code="invalid-target-selector", detail=f"{key} is invalid"
            )
        return value

    def _freeze_etf_all_targets(
        self,
        session: Session,
        targets: Sequence[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], dict[str, EtfUniverseSnapshot]]:
        """把 ALL_ETFS 草稿冻结为 exact 双市目录版本，并记录唯一全集摘要。"""
        frozen: list[dict[str, Any]] = []
        universes: dict[str, EtfUniverseSnapshot] = {}
        snapshot: EtfUniverseSnapshot | None = None
        for target in targets:
            selector = target.get("selector")
            if not isinstance(selector, dict) or selector.get("scope") != "ALL_ETFS":
                frozen.append(target)
                continue
            if snapshot is None:
                try:
                    versions = resolve_current_etf_profile_data_versions(session)
                    snapshot = load_frozen_etf_universe(
                        session,
                        profile_data_versions=versions,
                    )
                except EtfUniverseUnavailable as error:
                    raise OperationProblem(
                        status=409,
                        code=error.reason_code,
                        detail="Current SSE and SZSE ETF profile publications are required",
                    ) from error
            frozen_selector = {
                **selector,
                "profileDataVersions": {
                    venue: str(snapshot.profile_data_versions[venue]) for venue in ("SSE", "SZSE")
                },
            }
            frozen_target = {**target, "selector": frozen_selector}
            frozen.append(frozen_target)
            universes[str(target["datasetCode"])] = snapshot
        return frozen, universes

    def _freeze_share_capital_roster(
        self,
        session: Session,
        *,
        target: dict[str, Any],
        identity_as_of: date,
    ) -> tuple[dict[str, str], ...]:
        """冻结股本任务的永久证券名单，执行和重试都不得重新读取当前代码目录。"""
        selector = target["selector"]
        statement = (
            select(
                EquityInstrument.instrument_id,
                EquityIdentifierVersion.exchange,
                EquityIdentifierVersion.symbol,
            )
            .join(
                EquityIdentifierVersion,
                EquityIdentifierVersion.security_id == EquityInstrument.security_id,
            )
            .where(
                EquityInstrument.master_confirmed_at.is_not(None),
                EquityInstrument.listing_status != "PENDING",
                EquityIdentifierVersion.identity_state == "CONFIRMED",
                EquityIdentifierVersion.effective_from <= identity_as_of,
                or_(
                    EquityIdentifierVersion.effective_to.is_(None),
                    EquityIdentifierVersion.effective_to > identity_as_of,
                ),
                EquityIdentifierVersion.known_to.is_(None),
            )
        )
        if selector["kind"] == "INSTRUMENT":
            statement = statement.where(
                EquityIdentifierVersion.exchange == selector["exchange"],
                EquityIdentifierVersion.symbol == selector["symbol"],
            )
        elif selector["kind"] != "GLOBAL":
            raise ValueError("share capital requires GLOBAL or INSTRUMENT selector")
        rows = session.execute(
            statement.order_by(
                EquityIdentifierVersion.exchange,
                EquityIdentifierVersion.symbol,
                EquityInstrument.instrument_id,
            )
        ).mappings()
        roster = tuple(
            {
                "instrumentId": str(row["instrument_id"]),
                "exchange": str(row["exchange"]),
                "symbol": str(row["symbol"]),
                "identityAsOf": identity_as_of.isoformat(),
            }
            for row in rows
        )
        if selector["kind"] == "INSTRUMENT" and len(roster) != 1:
            raise OperationProblem(
                status=409,
                code="equity-identity-unavailable",
                detail="Selected equity identity is missing or ambiguous",
            )
        if len({item["instrumentId"] for item in roster}) != len(roster):
            raise RuntimeError("share capital roster contains duplicate permanent identities")
        return roster

    def _freeze_sector_bar_roster(
        self,
        session: Session,
        *,
        target: dict[str, Any],
    ) -> tuple[dict[str, str], ...]:
        """冻结东财板块 K 线的 ACTIVE 目录身份，执行和重试绝不读取后来新增板块。"""
        selector = target["selector"]
        statement = select(
            SectorEntity.sector_key,
            SectorEntity.scheme,
            SectorEntity.sector_code,
        ).where(
            SectorEntity.status == "ACTIVE",
            SectorEntity.scheme.in_(("eastmoney.industry", "eastmoney.concept")),
        )
        if selector["kind"] in {"SCHEME", "SECTOR"}:
            statement = statement.where(SectorEntity.scheme == selector["scheme"])
        if selector["kind"] == "SECTOR":
            statement = statement.where(SectorEntity.sector_code == selector["sectorCode"])
        rows = session.execute(
            statement.order_by(
                SectorEntity.scheme,
                SectorEntity.sector_code,
                SectorEntity.sector_key,
            )
        ).mappings()
        roster = tuple(
            {
                "sectorKey": str(row["sector_key"]),
                "scheme": str(row["scheme"]),
                "sectorCode": str(row["sector_code"]),
            }
            for row in rows
        )
        if selector["kind"] == "SECTOR" and len(roster) != 1:
            raise OperationProblem(
                status=409,
                code="sector-catalog-identity-unavailable",
                detail="Selected active EastMoney sector is missing or ambiguous",
            )
        if len({item["sectorKey"] for item in roster}) != len(roster) or len(
            {(item["scheme"], item["sectorCode"]) for item in roster}
        ) != len(roster):
            raise RuntimeError("sector bar roster contains duplicate identities")
        return roster

    def _technical_preflight_eligible(
        self,
        definition: DatasetDefinition,
        providers: tuple[str, ...],
    ) -> bool:
        """仅按可执行技术条件判断预检资格，来源准入元数据只保留审计用途。

        `approvalStatus`、`rightsStatus` 与 `licenseScope` 会随 run 快照冻结，方便追溯来源
        声明；它们不代表 adapter 连通性、载荷结构或质量结论，不能阻断 command、dispatcher
        或 fenced executor。真正的技术失败仍由已注册 provider、能力匹配、配置开关、在线探针
        与后续 schema/质量门决定。
        """
        return (
            not definition.model_only
            and (bool(providers) or definition.providerless)
            and definition.dispatcher_ready
            and definition.config_enabled
            and bool(definition.modes)
        )

    def _preflight_target(
        self,
        target: dict[str, Any],
        *,
        etf_universe: EtfUniverseSnapshot | None = None,
        equity_roster: tuple[dict[str, str], ...] | None = None,
        sector_bar_roster: tuple[dict[str, str], ...] | None = None,
    ) -> dict[str, Any]:
        """产生有界预检；互联互通额外在线验证官方 entitlement 与最终落地。"""
        definition = self._definition(target["datasetCode"])
        providers = self._providers_for(definition, target=target)
        eligible = self._technical_preflight_eligible(definition, providers)
        membership_historical_snapshot = (
            definition.dataset_code == "sector.membership.release"
            and target["mode"] == "OBSERVATION_DATE"
            and target["observationDate"] != self._now().astimezone(_SHANGHAI).date().isoformat()
        )
        if membership_historical_snapshot:
            # 东财成分接口只返回当前集合；历史观察日必须在排队前拒绝，不能把今天快照倒填为历史。
            eligible = False
        source_checks: tuple[ProviderPreflightComponent, ...] = ()
        source_evidence: Mapping[str, object] | None = None
        delivery_manifest_ref: dict[str, object] | None = None
        minimum_execution_window_seconds: int | None = None
        readiness_snapshot_ref: str | None = None
        if eligible and definition.dataset_code == "market.stock_connect.overview.bundle":
            provider = None
            readiness_snapshot_id = uuid4()
            readiness_request_hash = self._hash(target)
            readiness_evidence: Mapping[str, object] | None = None
            try:
                self._stock_connect_readiness_repository.begin(
                    snapshot_id=readiness_snapshot_id,
                    request_hash=readiness_request_hash,
                    selected_channels=_stock_connect_selected_channels(target),
                    observed_at=self._now(),
                )
            except Exception:
                source_checks = (
                    ProviderPreflightComponent(
                        component="stock-connect-readiness-snapshot",
                        accepted=False,
                        reason="READINESS_SNAPSHOT_BEGIN_FAILED",
                    ),
                )
            else:
                provider = self._source_registry.get(providers[0]) if len(providers) == 1 else None
                if provider is None or not isinstance(
                    provider,
                    SourcePreflightProbePort,
                ):
                    source_checks = (
                        ProviderPreflightComponent(
                            component="official-stock-connect-preflight",
                            accepted=False,
                            reason="PREFLIGHT_PROBE_UNAVAILABLE",
                        ),
                    )
                elif not isinstance(provider, SourceStatusCoverageBoundaryPort):
                    source_checks = (
                        ProviderPreflightComponent(
                            component="stock-connect-status-boundary-lock",
                            accepted=False,
                            reason="STATUS_BOUNDARY_CLAIM_UNAVAILABLE",
                        ),
                    )
                else:
                    try:
                        boundary = provider.status_coverage_boundary()
                        self._stock_connect_status_boundary_repository.claim(
                            required_from=boundary.required_from,
                            manifest_sha256=boundary.manifest_sha256,
                            observed_at=self._now(),
                        )
                    except StockConnectStatusBoundaryViolation as error:
                        source_checks = (
                            ProviderPreflightComponent(
                                component="stock-connect-status-boundary-lock",
                                accepted=False,
                                reason=error.reason,
                            ),
                        )
                    except Exception:
                        source_checks = (
                            ProviderPreflightComponent(
                                component="stock-connect-status-boundary-lock",
                                accepted=False,
                                reason="STATUS_BOUNDARY_LOCK_FAILED",
                            ),
                        )
                    else:
                        try:
                            report = provider.preflight_probe(
                                ProviderPreflightRequest(
                                    dataset_code=str(target["datasetCode"]),
                                    mode=str(target["mode"]),
                                    selector=dict(target["selector"]),
                                    date_from=target["dateFrom"],
                                    date_to=target["dateTo"],
                                    observation_date=target["observationDate"],
                                    timeout_seconds=3_600,
                                )
                            )
                            source_checks = (
                                ProviderPreflightComponent(
                                    component="stock-connect-status-boundary-lock",
                                    accepted=True,
                                    reason="STATUS_BOUNDARY_LOCKED",
                                ),
                                *report.components,
                            )
                            source_evidence = report.execution_evidence
                            readiness_evidence = report.readiness_evidence
                        except Exception:
                            # 预检只暴露稳定原因码；凭据、路径和底层异常不得进入控制面响应。
                            source_checks = (
                                ProviderPreflightComponent(
                                    component="stock-connect-status-boundary-lock",
                                    accepted=True,
                                    reason="STATUS_BOUNDARY_LOCKED",
                                ),
                                ProviderPreflightComponent(
                                    component="official-stock-connect-preflight",
                                    accepted=False,
                                    reason="PREFLIGHT_PROBE_FAILED",
                                ),
                            )
            eligible = bool(source_checks) and all(item.accepted for item in source_checks)
            if eligible and source_evidence is not None and provider is not None:
                try:
                    available_until, minimum_execution_window_seconds = (
                        stock_connect_delivery_window_from_evidence(source_evidence)
                    )
                    days = tuple(
                        DeliveryManifestTradeDate(
                            trade_date=trade_date,
                            target_count=target_count,
                            evidence=day_evidence,
                        )
                        for trade_date, target_count, day_evidence in (
                            stock_connect_delivery_manifest_days_from_evidence(source_evidence)
                        )
                    )
                    reference = SqlAlchemyDeliveryManifestRepository(self._database).persist(
                        build_immutable_delivery_manifest(
                            manifest_id=uuid4(),
                            dataset_code=definition.dataset_code,
                            provider_id=provider.provider_id,
                            request_hash=self._hash(target),
                            status="ELIGIBLE",
                            available_until=available_until,
                            # 完成预算在受理时按最新剩余时间校验；header 本身只冻结绝对截止。
                            minimum_remaining_seconds=0,
                            created_at=self._now(),
                            days=days,
                        )
                    )
                    delivery_manifest_ref = {
                        "manifestId": str(reference.manifest_id),
                        "rootHash": reference.root_hash,
                        "targetCount": reference.target_count,
                        "pageCount": reference.page_count,
                    }
                except Exception:
                    source_checks = (
                        *source_checks,
                        ProviderPreflightComponent(
                            component="immutable-delivery-manifest",
                            accepted=False,
                            reason="DELIVERY_MANIFEST_PERSIST_FAILED",
                        ),
                    )
                    eligible = False
                    minimum_execution_window_seconds = None
            if source_checks[0].reason != "READINESS_SNAPSHOT_BEGIN_FAILED":
                outcome = stock_connect_readiness_probe_outcome(source_checks)
                if outcome.status == "PENDING" and delivery_manifest_ref is None:
                    outcome = StockConnectReadinessProbeOutcome(
                        status="FAILED",
                        reason_code="PREFLIGHT_FAILED",
                        detail="Official stock-connect manifest did not complete",
                    )
                try:
                    self._stock_connect_readiness_repository.finish(
                        snapshot_id=readiness_snapshot_id,
                        outcome=outcome,
                        evidence=readiness_evidence,
                        manifest_id=(
                            None
                            if delivery_manifest_ref is None
                            else UUID(str(delivery_manifest_ref["manifestId"]))
                        ),
                        completed_at=self._now(),
                        request_hash=readiness_request_hash,
                    )
                except Exception:
                    source_checks = (
                        *source_checks,
                        ProviderPreflightComponent(
                            component="stock-connect-readiness-snapshot",
                            accepted=False,
                            reason="READINESS_SNAPSHOT_FINISH_FAILED",
                        ),
                    )
                    eligible = False
                    delivery_manifest_ref = None
                    minimum_execution_window_seconds = None
                else:
                    readiness_snapshot_ref = str(readiness_snapshot_id)
                    eligible = outcome.status == "PENDING"
            # 全量供应商证据仅用于构造不可变页面，永不写入 preflight JSONB。
            source_evidence = None
        resolved_from = target["dateFrom"]
        resolved_to = target["dateTo"] or target["observationDate"]
        selector = target["selector"]
        if definition.dataset_code in _AKSHARE_BATCHED_DATASETS:
            resolved_from, resolved_to = self._resolve_akshare_batched_window(
                target,
                anchor_date=self._now().astimezone(_SHANGHAI).date(),
            )
        elif selector.get("kind") == "ETF" and selector.get("operation") != "MASTER":
            resolved_from, resolved_to = self._resolve_etf_window(
                target,
                anchor_date=self._now().astimezone(_SHANGHAI).date(),
            )
        estimated_partitions = self._estimated_partitions(
            target,
            definition=definition,
            eligible=eligible,
            etf_universe=etf_universe,
            equity_roster=equity_roster,
            sector_bar_roster=sector_bar_roster,
            resolved_from=resolved_from,
            resolved_to=resolved_to,
        )
        membership_catalog_empty = (
            definition.dataset_code == "sector.membership.release"
            and eligible
            and estimated_partitions == 0
        )
        if membership_catalog_empty:
            # 没有 ACTIVE 目录时执行必然失败，预检直接阻断并要求先完成板块目录同步。
            eligible = False
        sector_bar_catalog_empty = (
            definition.dataset_code in _SECTOR_BAR_DATASETS
            and eligible
            and estimated_partitions == 0
        )
        if sector_bar_catalog_empty:
            # K 线只能基于预检冻结的 ACTIVE 东财目录，空名单不可提交空 publication。
            eligible = False
        if (
            eligible
            and definition.dataset_code == "market.stock_connect.overview.bundle"
            and delivery_manifest_ref is not None
        ):
            target_count = delivery_manifest_ref.get("targetCount")
            if not isinstance(target_count, int) or target_count < 1:
                eligible = False
                estimated_partitions = 0
            else:
                estimated_partitions = target_count
        result = {
            "target": target,
            "eligible": eligible,
            "estimatedPartitions": estimated_partitions,
            "estimatedProviderCalls": (
                self._estimated_provider_calls(
                    target,
                    definition=definition,
                    eligible=eligible,
                    estimated_partitions=estimated_partitions,
                    resolved_from=resolved_from,
                    resolved_to=resolved_to,
                )
            ),
            "resolvedDateFrom": resolved_from,
            "resolvedDateTo": resolved_to,
            "warnings": [] if eligible else ["数据集当前没有已注册来源或不支持人工同步"],
        }
        if membership_historical_snapshot:
            result["warnings"] = ["东财板块成分仅支持当前观察日，不能伪装为历史快照"]
        elif membership_catalog_empty:
            result["warnings"] = ["当前没有 ACTIVE 板块目录，请先同步 sector.catalog.raw"]
        elif sector_bar_catalog_empty:
            result["warnings"] = ["当前没有可同步的 ACTIVE 东财板块，请先同步 sector.catalog.raw"]
        if etf_universe is not None:
            result["universeCount"] = etf_universe.count
            result["universeHash"] = etf_universe.universe_hash
            result["navEligibleCount"] = etf_universe.nav_eligible_count
            result["navUnsupportedCount"] = etf_universe.nav_unsupported_count
        if equity_roster is not None:
            result["equityInstrumentRoster"] = list(equity_roster)
            result["equityInstrumentRosterHash"] = self._hash(equity_roster)
        if sector_bar_roster is not None:
            result["sectorBarRoster"] = list(sector_bar_roster)
            result["sectorBarRosterHash"] = self._hash(sector_bar_roster)
        if source_checks:
            result["sourceChecks"] = [
                {
                    "component": item.component,
                    "accepted": item.accepted,
                    "reason": item.reason,
                }
                for item in source_checks
            ]
            if not eligible:
                result["warnings"] = ["互联互通官方来源实时预检未通过，命令不会进入队列"]
        if delivery_manifest_ref is not None:
            # HTTP 响应会剔除该引用；它只供同一 preflight 的受理事务绑定 immutable pages。
            result["deliveryManifestRef"] = delivery_manifest_ref
        if minimum_execution_window_seconds is not None:
            result["minimumExecutionWindowSeconds"] = minimum_execution_window_seconds
        if readiness_snapshot_ref is not None:
            # 该引用只用于把后续 run 与先于 provider 落盘的 readiness 尝试关联。
            result["readinessSnapshotRef"] = readiness_snapshot_ref
        return result

    def _estimated_partitions(
        self,
        target: dict[str, Any],
        *,
        definition: DatasetDefinition,
        eligible: bool,
        etf_universe: EtfUniverseSnapshot | None = None,
        equity_roster: tuple[dict[str, str], ...] | None = None,
        sector_bar_roster: tuple[dict[str, str], ...] | None = None,
        resolved_from: str | None = None,
        resolved_to: str | None = None,
    ) -> int:
        """按真实 selector 粒度估算 Provider 调用，避免把全市场逐证券任务显示成一次调用。"""
        if not eligible:
            return 0
        selector = target["selector"]
        if definition.dataset_code in _SECTOR_BAR_DATASETS:
            if sector_bar_roster is None:
                raise RuntimeError("frozen sector bar roster is required for preflight")
            if not isinstance(resolved_from, str) or not isinstance(resolved_to, str):
                raise RuntimeError("sector bar target is missing its resolved date window")
            return len(sector_bar_roster) * _akshare_batched_partition_count(
                start=date.fromisoformat(resolved_from),
                end=date.fromisoformat(resolved_to),
                dataset_code=definition.dataset_code,
            )
        if definition.dataset_code in _AKSHARE_BATCHED_DATASETS:
            if not isinstance(resolved_from, str) or not isinstance(resolved_to, str):
                raise RuntimeError("AKShare batched target is missing its resolved date window")
            start, end = date.fromisoformat(resolved_from), date.fromisoformat(resolved_to)
            return _akshare_batched_partition_count(
                start=start,
                end=end,
                dataset_code=definition.dataset_code,
            )
        if selector.get("kind") == "ETF" and selector.get("scope") == "ALL_ETFS":
            if etf_universe is None:
                raise RuntimeError("frozen ETF universe is required for ALL_ETFS preflight")
            return etf_universe.count
        if selector.get("kind") == "ETF" and selector.get("scope") == "ALL_VENUES":
            return 2
        if definition.dataset_code == "equity.master.cn-a":
            return 3
        if definition.dataset_code == "equity.lifecycle.explicit":
            return 3 if selector["kind"] == "GLOBAL" else 1
        if definition.dataset_code == "equity.share_capital.reported" and selector["kind"] in {
            "GLOBAL",
            "INSTRUMENT",
        }:
            if equity_roster is None:
                raise RuntimeError("frozen share capital roster is required for preflight")
            return len(equity_roster)
        if definition.dataset_code == "sector.membership.release":
            statement = (
                select(func.count())
                .select_from(SectorEntity)
                .where(SectorEntity.status == "ACTIVE")
            )
            if selector["kind"] in {"SCHEME", "SECTOR"}:
                statement = statement.where(SectorEntity.scheme == selector["scheme"])
            if selector["kind"] == "SECTOR":
                statement = statement.where(SectorEntity.sector_code == selector["sectorCode"])
            with self._database.session() as session:
                return int(session.execute(statement).scalar_one())
        return 1

    def _estimated_provider_calls(
        self,
        target: dict[str, Any],
        *,
        definition: DatasetDefinition,
        eligible: bool,
        estimated_partitions: int,
        resolved_from: str | None,
        resolved_to: str | None,
    ) -> int:
        """按 adapter 实际调用粒度估算来源请求，逐日两融接口不能伪装成单次调用。"""
        if not eligible:
            return 0
        if definition.providerless:
            return 0
        if definition.dataset_code not in _AKSHARE_BATCHED_DATASETS:
            return estimated_partitions
        if not isinstance(resolved_from, str) or not isinstance(resolved_to, str):
            raise RuntimeError("AKShare batched target is missing its resolved date window")
        start, end = date.fromisoformat(resolved_from), date.fromisoformat(resolved_to)
        selector = target["selector"]
        if definition.dataset_code in {
            "market.margin.security.1d.reported",
            "market.margin.eligibility.reported",
        } or (
            definition.dataset_code == "market.margin.market.1d.reported"
            and selector.get("venue") == "SZSE"
        ):
            return (end - start).days + 1
        return estimated_partitions

    def _resolve_etf_window(
        self,
        target: dict[str, Any],
        *,
        anchor_date: date,
    ) -> tuple[str, str]:
        """把 ETF 非目录模式解析为冻结包含端日期窗，执行时不得重新读取当前日期。"""
        mode = target["mode"]
        if mode == "FULL":
            return _ETF_HISTORY_START.isoformat(), anchor_date.isoformat()
        if mode == "INCREMENTAL":
            return (anchor_date - timedelta(days=31)).isoformat(), anchor_date.isoformat()
        if mode == "OBSERVATION_DATE":
            observation_date = str(target["observationDate"])
            return observation_date, observation_date
        if mode == "DATE_RANGE":
            return str(target["dateFrom"]), str(target["dateTo"])
        raise RuntimeError("ETF preflight mode has no frozen date window")

    def _resolve_akshare_batched_window(
        self,
        target: dict[str, Any],
        *,
        anchor_date: date,
    ) -> tuple[str, str]:
        """冻结两融和真实合约日线的日期边界，续跑绝不因跨日而扩展请求范围。"""
        mode = target["mode"]
        if mode == "FULL":
            return _ETF_HISTORY_START.isoformat(), anchor_date.isoformat()
        if mode == "INCREMENTAL":
            return (anchor_date - timedelta(days=31)).isoformat(), anchor_date.isoformat()
        if mode == "OBSERVATION_DATE":
            return str(target["observationDate"]), str(target["observationDate"])
        if mode == "DATE_RANGE":
            return str(target["dateFrom"]), str(target["dateTo"])
        raise RuntimeError("AKShare batched target mode is unsupported")

    def _execution_intent_from_preflight(
        self,
        *,
        target: dict[str, Any],
        result: dict[str, Any],
    ) -> dict[str, Any] | None:
        """从持久化预检结果生成私有交付清单、时间窗、证券名单和对账证据。"""
        selector = target["selector"]
        if target["datasetCode"] == "market.stock_connect.overview.bundle":
            raw_reference = result.get("deliveryManifestRef")
            minimum_window = result.get("minimumExecutionWindowSeconds")
            readiness_snapshot_ref = result.get("readinessSnapshotRef")
            if (
                not isinstance(raw_reference, dict)
                or not isinstance(minimum_window, int)
                or not isinstance(readiness_snapshot_ref, str)
            ):
                raise RuntimeError("stock-connect preflight did not freeze its delivery manifest")
            manifest_id = raw_reference.get("manifestId")
            root_hash = raw_reference.get("rootHash")
            target_count = raw_reference.get("targetCount")
            page_count = raw_reference.get("pageCount")
            if (
                not isinstance(manifest_id, str)
                or not isinstance(root_hash, str)
                or len(root_hash) != 64
                or not isinstance(target_count, int)
                or target_count < 1
                or not isinstance(page_count, int)
                or page_count < 1
                or minimum_window < 0
            ):
                raise RuntimeError("stock-connect delivery manifest is invalid")
            try:
                parsed_manifest_id = UUID(manifest_id)
                parsed_readiness_snapshot_id = UUID(readiness_snapshot_ref)
                reference = SqlAlchemyDeliveryManifestRepository(self._database).require_available(
                    manifest_id=parsed_manifest_id,
                    expected_root_hash=root_hash,
                    observed_at=self._now(),
                    required_remaining_seconds=minimum_window,
                )
            except (ValueError, DeliveryManifestUnavailable) as error:
                raise OperationProblem(
                    status=409,
                    code="preflight-expired",
                    detail="Stock-connect delivery window is no longer sufficient",
                ) from error
            if reference.target_count != target_count or reference.page_count != page_count:
                raise RuntimeError("stock-connect delivery manifest reference drifted")
            return {
                "stockConnectDeliveryManifestRef": {
                    "manifestId": manifest_id,
                    "rootHash": root_hash,
                    "targetCount": target_count,
                    "pageCount": page_count,
                },
                "stockConnectReadinessSnapshotId": str(parsed_readiness_snapshot_id),
            }
        if target["datasetCode"] == "equity.share_capital.reported":
            roster = result.get("equityInstrumentRoster")
            roster_hash = result.get("equityInstrumentRosterHash")
            if not isinstance(roster, list) or not isinstance(roster_hash, str):
                raise RuntimeError("share capital preflight did not freeze its identity roster")
            return {
                "equityInstrumentRoster": roster,
                "equityInstrumentRosterHash": roster_hash,
            }
        if target["datasetCode"] in _SECTOR_BAR_DATASETS:
            roster = result.get("sectorBarRoster")
            roster_hash = result.get("sectorBarRosterHash")
            resolved_from = result.get("resolvedDateFrom")
            resolved_to = result.get("resolvedDateTo")
            if (
                not isinstance(roster, list)
                or not isinstance(roster_hash, str)
                or len(roster_hash) != 64
                or not isinstance(resolved_from, str)
                or not isinstance(resolved_to, str)
            ):
                raise RuntimeError("sector bar preflight did not freeze its roster and date window")
            return {
                "sectorBarRoster": roster,
                "sectorBarRosterHash": roster_hash,
                "akshareResolvedDateFrom": resolved_from,
                "akshareResolvedDateTo": resolved_to,
                "akshareExecutionBatchDays": _akshare_execution_batch_days(target["datasetCode"]),
            }
        if target["datasetCode"] in _AKSHARE_BATCHED_DATASETS:
            resolved_from = result.get("resolvedDateFrom")
            resolved_to = result.get("resolvedDateTo")
            if not isinstance(resolved_from, str) or not isinstance(resolved_to, str):
                raise RuntimeError("AKShare batched preflight did not freeze its date window")
            # 只冻结已验证的日期边界和固定批次大小；运行期不得重新读取当前日期或扩张范围。
            return {
                "akshareResolvedDateFrom": resolved_from,
                "akshareResolvedDateTo": resolved_to,
                "akshareExecutionBatchDays": _akshare_execution_batch_days(target["datasetCode"]),
            }
        if selector.get("kind") != "ETF" or selector.get("operation") == "MASTER":
            return None
        intent: dict[str, Any] = {
            "etfResolvedDateFrom": str(result["resolvedDateFrom"]),
            "etfResolvedDateTo": str(result["resolvedDateTo"]),
        }
        if selector.get("scope") == "ALL_ETFS":
            intent.update(
                {
                    "etfUniverseCount": int(result["universeCount"]),
                    "etfUniverseHash": str(result["universeHash"]),
                }
            )
            if selector.get("operation") == "NAV":
                intent.update(
                    {
                        "etfNavEligibleCount": int(result["navEligibleCount"]),
                        "etfNavUnsupportedCount": int(result["navUnsupportedCount"]),
                    }
                )
        return intent

    def system_source_snapshots(
        self, dataset_codes: Iterable[str]
    ) -> dict[str, list[dict[str, Any]]]:
        """返回计划创建时必须冻结的实时控制面来源，不访问 Provider 或写数据库。"""
        result: dict[str, list[dict[str, Any]]] = {}
        for dataset_code in dataset_codes:
            if dataset_code in result:
                raise ValueError("source snapshot dataset codes must be unique")
            definition = self._definition(dataset_code)
            snapshot = self._source_snapshot(definition)
            if any(
                not bool(binding.get("effective"))
                or not isinstance(binding.get("mappingCodeSha256"), str)
                for binding in snapshot
            ):
                raise OperationProblem(
                    status=409,
                    code="equity-backfill-source-unavailable",
                    detail=f"Frozen technical source is unavailable for {dataset_code}",
                )
            result[dataset_code] = snapshot
        return result

    def _source_snapshot(
        self,
        definition: DatasetDefinition,
        *,
        target: Mapping[str, object] | None = None,
    ) -> list[dict[str, Any]]:
        """在 run 受理时冻结 provider、真实上游、adapter 与方法学标识。"""
        snapshot: list[dict[str, Any]]
        if definition.providerless:
            executor_identity = self._internal_executor_identity(definition.dataset_code)
            if executor_identity is not None:
                snapshot = [
                    {
                        "providerId": "platform",
                        "upstreamSource": "platform-derived",
                        "sourceDataset": definition.dataset_code,
                        "adapterId": executor_identity["executorCode"],
                        "codeSha256": executor_identity["codeSha256"],
                        "mappingExecutor": executor_identity["executorCode"],
                        "mappingCodeSha256": executor_identity["codeSha256"],
                        "methodologyCode": definition.dataset_code,
                        "methodologyVersion": 1,
                        **self._source_admission_audit(definition),
                        "role": "PRIMARY",
                        "effective": True,
                        "sourceKind": "INTERNAL_EXECUTOR",
                    }
                ]
            else:
                snapshot = []
        else:
            snapshot = []
        if not snapshot:
            bindings = self._source_bindings(
                definition,
                self._providers_for(definition, target=target),
                target=target,
            )
            if bindings:
                executor_identity = self._internal_executor_identity(definition.dataset_code)
                snapshot = (
                    bindings
                    if executor_identity is None
                    else [
                        {
                            **binding,
                            "mappingExecutor": executor_identity["executorCode"],
                            "mappingCodeSha256": executor_identity["codeSha256"],
                        }
                        for binding in bindings
                    ]
                )
            else:
                snapshot = [
                    {
                        "providerId": "unavailable",
                        "upstreamSource": "unavailable",
                        "sourceDataset": definition.dataset_code,
                        "adapterId": "unavailable",
                        "methodologyCode": "unavailable",
                        "methodologyVersion": 1,
                        **self._source_admission_audit(definition),
                        "role": "PRIMARY",
                        "effective": False,
                    }
                ]
        return snapshot

    def _internal_executor_identity(self, dataset_code: str) -> dict[str, str] | None:
        """解包注册器并冻结真实函数、字节码、源码和稳定绑定参数。"""
        executor = self._executors.get(dataset_code)
        if executor is None:
            return None
        bound_args: list[object] = []
        bound_kwargs: dict[str, object] = {}
        unwrapped: Callable[..., Any] = executor
        while isinstance(unwrapped, partial):
            bound_args.extend(unwrapped.args)
            bound_kwargs.update(unwrapped.keywords or {})
            unwrapped = unwrapped.func
        unwrapped = inspect.unwrap(unwrapped)
        module = inspect.getmodule(unwrapped)
        module_file = None if module is None else getattr(module, "__file__", None)
        if not isinstance(module_file, str):
            return None
        path = Path(module_file)
        try:
            source_bytes = path.read_bytes()
        except OSError:
            return None
        module_name = getattr(module, "__name__", "unknown")
        qualified_name = getattr(unwrapped, "__qualname__", unwrapped.__class__.__qualname__)
        binding_manifest = {
            "args": [self._stable_executor_binding(value) for value in bound_args],
            "kwargs": {
                key: self._stable_executor_binding(value)
                for key, value in sorted(bound_kwargs.items())
            },
        }
        binding_hash = self._hash(binding_manifest)
        code = getattr(unwrapped, "__code__", None)
        bytecode = b"" if code is None else marshal.dumps(code)
        code_hash = hashlib.sha256(
            source_bytes
            + b"\0"
            + module_name.encode()
            + b"\0"
            + qualified_name.encode()
            + b"\0"
            + binding_hash.encode()
            + b"\0"
            + bytecode
        ).hexdigest()
        return {
            "executorCode": f"{module_name}:{qualified_name}:{binding_hash[:16]}",
            "codeSha256": code_hash,
        }

    def _stable_executor_binding(self, value: object) -> object:
        """把 `partial` 绑定值投影为跨进程稳定且不泄露配置秘密的代码身份。"""
        if value is None or isinstance(value, (bool, int, float, str)):
            return value
        if isinstance(value, Enum):
            return {
                "type": f"{value.__class__.__module__}:{value.__class__.__qualname__}",
                "value": value.value,
            }
        if isinstance(value, (date, datetime, UUID)):
            return str(value)
        if isinstance(value, Mapping):
            return {
                str(key): self._stable_executor_binding(item)
                for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            }
        if isinstance(value, (tuple, list)):
            return [self._stable_executor_binding(item) for item in value]
        # 容器、客户端等运行时依赖只冻结类型，避免对象地址或密钥进入证据。
        return {"type": f"{value.__class__.__module__}:{value.__class__.__qualname__}"}

    def _source_capabilities_for(
        self,
        definition: DatasetDefinition,
        *,
        target: Mapping[str, object] | None = None,
    ) -> tuple[str, ...]:
        """返回目标真正会调用的来源能力；未给目标时仅用于目录摘要。"""
        if target is not None and definition.dataset_code in {
            _MONEY_FLOW_DAILY_DATASET,
            _MONEY_FLOW_RANKING_DATASET,
        }:
            selector = target.get("selector")
            if not isinstance(selector, Mapping):
                raise ValueError("money-flow target selector is unavailable")
            return (money_flow_source_capability(definition.dataset_code, selector),)
        return definition.source_capabilities or (
            (definition.capability,) if definition.capability is not None else ()
        )

    def _providers_for(
        self,
        definition: DatasetDefinition,
        *,
        target: Mapping[str, object] | None = None,
    ) -> tuple[str, ...]:
        """返回目录指定且具备全部能力的 provider，禁止同名 capability 越权替换来源。"""
        capabilities = self._source_capabilities_for(definition, target=target)
        if not capabilities:
            return ()
        provider_ids = set(self._source_registry.provider_ids())
        if definition.provider_id is not None:
            # 目录中的 provider 是技术能力和血缘绑定边界；AKShare 即使伪声明官方
            # capability 也不能接管。
            provider_ids.intersection_update({definition.provider_id})
        if definition.dataset_code == _MONEY_FLOW_RANKING_DATASET and target is None:
            provider_ids.intersection_update(
                {
                    provider.provider_id
                    for capability in capabilities
                    for provider in self._source_registry.for_capability(capability)
                }
            )
        else:
            for capability in capabilities:
                provider_ids.intersection_update(
                    provider.provider_id
                    for provider in self._source_registry.for_capability(capability)
                )
        return tuple(sorted(provider_ids))

    def _source_admission_audit(self, definition: DatasetDefinition) -> dict[str, str | None]:
        """返回随 source snapshot 冻结的来源声明，不把声明解释为运行准入结论。"""
        return {
            "approvalStatus": definition.approval_status,
            "rightsStatus": definition.rights_status,
            "licenseScope": definition.license_scope,
        }

    def _source_bindings(
        self,
        definition: DatasetDefinition,
        providers: Iterable[str],
        *,
        target: Mapping[str, object] | None = None,
    ) -> list[dict[str, Any]]:
        """将 provider 标识投影为明确区分 adapter 与真实 upstream 的安全绑定。"""
        values = list(providers)
        capabilities = self._source_capabilities_for(definition, target=target)
        if not values and (definition.model_only or definition.providerless):
            return [
                {
                    "providerId": definition.provider_id or "platform",
                    "upstreamSource": definition.upstream_source or "platform-derived",
                    "sourceDataset": definition.dataset_code,
                    "adapterId": "platform-derived",
                    "methodologyCode": "platform-derived",
                    "methodologyVersion": 1,
                    **self._source_admission_audit(definition),
                    "role": "PRIMARY",
                    "effective": True,
                }
            ]
        if not values and definition.provider_id is not None:
            return [
                {
                    "providerId": definition.provider_id,
                    "upstreamSource": definition.upstream_source
                    or self._upstream_source(definition.provider_id, definition),
                    "sourceDataset": capability,
                    "adapterId": f"{definition.provider_id}:{capability}",
                    "methodologyCode": definition.dataset_code,
                    "methodologyVersion": 1,
                    **self._source_admission_audit(definition),
                    "role": "PRIMARY",
                    "effective": False,
                }
                for capability in capabilities
            ]
        bindings: list[dict[str, Any]] = []
        for capability in capabilities:
            capable_providers = tuple(
                provider
                for provider in values
                if capability in self._source_registry.get(provider).capabilities()
            )
            for provider_index, provider in enumerate(capable_providers):
                bindings.append(
                    {
                        "providerId": provider,
                        "upstreamSource": self._upstream_source(provider, definition),
                        "sourceDataset": capability,
                        "adapterId": f"{provider}:{capability}",
                        "methodologyCode": definition.dataset_code,
                        "methodologyVersion": 1,
                        **self._source_admission_audit(definition),
                        "role": "PRIMARY" if provider_index == 0 else "SHADOW",
                        "effective": provider_index == 0,
                    }
                )
        return bindings

    def _upstream_source(self, provider: str, definition: DatasetDefinition) -> str:
        """返回 provider 包装器后真实上游来源名称，避免误把 AKShare 当原始来源。"""
        if definition.upstream_source is not None:
            return definition.upstream_source
        if provider == "akshare":
            if definition.domain == "financial" or definition.domain == "sector":
                return "eastmoney"
            if definition.dataset_code == "equity.bar.1d.raw":
                return "tencent"
            return "akshare-backed-source"
        if provider == "akshare-eastmoney-financial":
            return "eastmoney"
        return provider

    def _capability_view(self, definition: DatasetDefinition, available: bool) -> dict[str, Any]:
        """投影手工与计划能力，MODEL_ONLY 永远没有可提交同步模式。"""
        manual_modes = list(definition.modes) if not definition.model_only else []
        schedule_modes = list(definition.schedule_modes) if not definition.model_only else []
        options = self._schedule_target_policy_options(definition) if schedule_modes else []
        return {
            "supportedModes": manual_modes,
            "scheduleSupportedModes": schedule_modes,
            "scheduleTargetPolicyOptions": options,
            "selectorKinds": list(definition.selector_kinds),
            "maxRangeDays": definition.max_range_days,
            "scheduleEligible": available and bool(schedule_modes),
            "manualEnabled": available and bool(manual_modes),
            "correctionLookbackDays": definition.correction_lookback_days,
        }

    def _slot_view(self, slot: DataOperationExecutionSlot) -> dict[str, Any]:
        """投影不含 fencing token 的全局执行槽状态。"""
        return {
            "state": slot.state,
            "runId": self._uuid_text(slot.run_id),
            "datasetCode": slot.dataset_code,
            "leaseUntil": self._iso(slot.lease_until),
            "heartbeatAt": self._iso(slot.heartbeat_at),
        }

    def _ensure_slot(self, session: Session, *, lock: bool = False) -> DataOperationExecutionSlot:
        """读取或初始化单例 global slot，初始化也在当前事务完成。"""
        statement = select(DataOperationExecutionSlot).where(
            DataOperationExecutionSlot.slot_key == "global"
        )
        if lock:
            statement = statement.with_for_update()
        slot = session.scalar(statement)
        if slot is None:
            slot = DataOperationExecutionSlot(
                slot_key="global",
                state="IDLE",
                run_id=None,
                dataset_code=None,
                lease_until=None,
                heartbeat_at=None,
                fencing_token=0,
            )
            session.add(slot)
            session.flush()
            if lock:
                slot = session.scalar(
                    select(DataOperationExecutionSlot)
                    .where(DataOperationExecutionSlot.slot_key == "global")
                    .with_for_update()
                )
                assert slot is not None
        return slot

    def _reap_locked_slot(
        self, session: Session, slot: DataOperationExecutionSlot, now: datetime
    ) -> None:
        """在持有 slot 行锁时复用同一过期 run 重排，耗尽预算才写失败终态。"""
        if slot.run_id is not None:
            run = session.get(DataOperationRun, slot.run_id, with_for_update=True)
            if run is not None and run.status in {"RUNNING", "CANCEL_REQUESTED"}:
                command = session.get(DataOperationCommand, run.command_id, with_for_update=True)
                if command is not None:
                    recovery_error = self._error(
                        "lease-expired", "RECOVERY", True, "Worker lease expired"
                    )
                    # 公平批次会多次正常 claim，只有真实租约丢失才消耗独立恢复预算。
                    run.recovery_attempts += 1
                    if run.cancel_requested or run.status == "CANCEL_REQUESTED":
                        # 已请求取消的崩溃 worker 不应在恢复预算耗尽后伪装成执行失败。
                        run.status = "CANCELLED"
                        run.finished_at = now
                        self._record_event(
                            session,
                            "RUN",
                            run.run_id,
                            "REAP",
                            "CANCELLED",
                            "system:recovery",
                            command.request_id,
                            None,
                        )
                        self._refresh_command_status(session, command, now)
                    elif run.recovery_attempts >= _MAX_RECOVERY_ATTEMPTS:
                        run.status = "FAILED"
                        run.finished_at = now
                        run.error_json = recovery_error
                        self._record_event(
                            session,
                            "RUN",
                            run.run_id,
                            "REAP",
                            "FAILED",
                            "system:recovery",
                            command.request_id,
                            recovery_error,
                        )
                        self._refresh_command_status(session, command, now)
                    else:
                        # 保持同一 run UUID、冻结 target/sourceSnapshot 和尝试计数；
                        # 下一次 claim 增加 attempt。
                        run.status = "QUEUED"
                        run.fencing_token = None
                        run.error_json = recovery_error
                        run.queue_position = None
                        self._record_event(
                            session,
                            "RUN",
                            run.run_id,
                            "REAP",
                            "QUEUED",
                            "system:recovery",
                            command.request_id,
                            recovery_error,
                        )
                        command.status = "QUEUED"
        slot.state = "IDLE"
        slot.run_id = None
        slot.dataset_code = None
        slot.lease_until = None
        slot.heartbeat_at = None

    def _refresh_command_status(
        self, session: Session, command: DataOperationCommand, now: datetime
    ) -> None:
        """从 child run 终态计算 command 聚合状态，保留 PARTIAL 与取消语义。"""
        runs = session.scalars(
            select(DataOperationRun)
            .where(DataOperationRun.command_id == command.command_id)
            .order_by(DataOperationRun.target_index)
        ).all()
        statuses = [run.status for run in runs]
        if any(status in {"RUNNING", "CANCEL_REQUESTED"} for status in statuses):
            command.status = (
                "CANCEL_REQUESTED" if command.status == "CANCEL_REQUESTED" else "RUNNING"
            )
            command.error_json = None
            return
        if any(status == "QUEUED" for status in statuses):
            command.status = (
                "CANCEL_REQUESTED" if command.status == "CANCEL_REQUESTED" else "QUEUED"
            )
            command.error_json = None
            return
        if statuses and all(status == "CANCELLED" for status in statuses):
            command.status = "CANCELLED"
        elif statuses and all(status == "SUCCEEDED" for status in statuses):
            command.status = "SUCCEEDED"
        elif any(status == "SUCCEEDED" for status in statuses):
            command.status = "PARTIAL"
        else:
            command.status = "FAILED"
        command.error_json = self._command_error_from_runs(runs, command.status)
        command.finished_at = now

    @staticmethod
    def _command_error_from_runs(
        runs: Sequence[DataOperationRun], command_status: str
    ) -> dict[str, Any] | None:
        """从终态 child 选择可执行的脱敏错误，供 command 详情和整批重试入口使用。

        一个 command 可能混合多个失败原因。若存在可重试 child，优先暴露按提交顺序最早的
        可重试错误，使 Web 的整批重试入口与控制面实际可复制的 run 一致；否则保留最早的
        失败摘要。成功或取消不继承历史错误，避免旧失败误导当前已完成 command。
        """
        if command_status in {"SUCCEEDED", "CANCELLED"}:
            return None
        errors = [dict(run.error_json) for run in runs if isinstance(run.error_json, dict)]
        if not errors:
            return None
        retryable = [error for error in errors if error.get("retryable") is True]
        return retryable[0] if retryable else errors[0]

    def _request_cancel(
        self,
        session: Session,
        target: dict[str, str],
        actor: dict[str, str],
        request_id: str,
        now: datetime,
    ) -> tuple[UUID, str]:
        """修改未终态目标的取消标志，或为已终态目标记录 `cancel_too_late`。"""
        resource_id = UUID(target["resourceId"])
        if target["resourceType"] == "RUN":
            run = session.get(DataOperationRun, resource_id, with_for_update=True)
            if run is None:
                raise OperationProblem(
                    status=404, code="run-not-found", detail="Data sync run is not found"
                )
            command = session.get(DataOperationCommand, run.command_id, with_for_update=True)
            assert command is not None
            if run.status in _TERMINAL_RUNS:
                self._record_event(
                    session,
                    "RUN",
                    run.run_id,
                    "CANCEL",
                    "FAILED",
                    actor["actorRef"],
                    request_id,
                    self._error(
                        "cancel_too_late",
                        "CANCEL",
                        False,
                        "Run has already reached a terminal state",
                    ),
                )
                return command.command_id, run.status
            if run.status == "QUEUED":
                run.status = "CANCELLED"
                run.finished_at = now
            else:
                run.status = "CANCEL_REQUESTED"
                run.cancel_requested = True
            self._record_event(
                session,
                "RUN",
                run.run_id,
                "CANCEL",
                "CANCEL_REQUESTED",
                actor["actorRef"],
                request_id,
                None,
            )
            self._refresh_command_status(session, command, now)
            return command.command_id, run.status
        command = session.get(DataOperationCommand, resource_id, with_for_update=True)
        if command is None:
            raise OperationProblem(
                status=404, code="command-not-found", detail="Data sync command is not found"
            )
        runs = session.scalars(
            select(DataOperationRun)
            .where(DataOperationRun.command_id == command.command_id)
            .with_for_update()
        ).all()
        if command.status in _TERMINAL_COMMANDS:
            self._record_event(
                session,
                "COMMAND",
                command.command_id,
                "CANCEL",
                "FAILED",
                actor["actorRef"],
                request_id,
                self._error(
                    "cancel_too_late",
                    "CANCEL",
                    False,
                    "Command has already reached a terminal state",
                ),
            )
            return command.command_id, command.status
        command.status = "CANCEL_REQUESTED"
        for run in runs:
            if run.status == "QUEUED":
                run.status = "CANCELLED"
                run.finished_at = now
            elif run.status in {"RUNNING", "CANCEL_REQUESTED"}:
                run.status = "CANCEL_REQUESTED"
                run.cancel_requested = True
        self._record_event(
            session,
            "COMMAND",
            command.command_id,
            "CANCEL",
            "CANCEL_REQUESTED",
            actor["actorRef"],
            request_id,
            None,
        )
        self._refresh_command_status(session, command, now)
        return command.command_id, command.status

    def _retryable_runs(
        self, session: Session, target: dict[str, str]
    ) -> tuple[list[DataOperationRun], UUID]:
        """选择失败、部分成功或中断 run；排队和成功 run 不会被错误复制。"""
        resource_id = UUID(target["resourceId"])
        if target["resourceType"] == "RUN":
            run = session.get(DataOperationRun, resource_id)
            if run is None:
                raise OperationProblem(
                    status=404, code="run-not-found", detail="Data sync run is not found"
                )
            return (
                [run] if run.status in {"FAILED", "PARTIAL", "INTERRUPTED"} else []
            ), run.command_id
        command = session.get(DataOperationCommand, resource_id)
        if command is None:
            raise OperationProblem(
                status=404, code="command-not-found", detail="Data sync command is not found"
            )
        runs = session.scalars(
            select(DataOperationRun)
            .where(
                DataOperationRun.command_id == command.command_id,
                DataOperationRun.status.in_(("FAILED", "PARTIAL", "INTERRUPTED")),
            )
            .order_by(DataOperationRun.target_index)
        ).all()
        return list(runs), command.command_id

    def _command_receipt(
        self,
        session: Session,
        command_id: UUID,
        submission_id: UUID,
        *,
        target: dict[str, str],
        target_status: str | None,
    ) -> dict[str, Any]:
        """构建 submit/cancel/retry 共用收据，childRunIds 始终维持提交顺序。"""
        command = session.get(DataOperationCommand, command_id)
        assert command is not None
        children = session.scalars(
            select(DataOperationRun)
            .where(DataOperationRun.command_id == command_id)
            .order_by(DataOperationRun.target_index)
        ).all()
        resolved_target_status = target_status or self._target_status(session, target)
        queue_position = (
            children[0].queue_position
            if target["resourceType"] == "COMMAND" and command.status == "QUEUED" and children
            else None
        )
        return {
            "commandId": str(command.command_id),
            "submissionId": str(submission_id),
            "status": command.status,
            "target": target,
            "targetStatus": resolved_target_status,
            "childRunIds": [str(child.run_id) for child in children],
            "queuePosition": queue_position,
            "acceptedAt": self._iso(command.requested_at),
        }

    def _target_status(self, session: Session, target: dict[str, str]) -> str:
        """读取 action target 的实际状态，用于 receipt 而非推测。"""
        model: DataOperationCommand | DataOperationRun | None
        model = session.get(
            DataOperationCommand if target["resourceType"] == "COMMAND" else DataOperationRun,
            UUID(target["resourceId"]),
        )
        if model is None:
            raise OperationProblem(
                status=404, code="target-not-found", detail="Data operation target is not found"
            )
        return model.status

    def _command_for_target(self, session: Session, target: dict[str, str]) -> UUID:
        """将 run target 定位到所属 command，供幂等取消重放。"""
        if target["resourceType"] == "COMMAND":
            return UUID(target["resourceId"])
        run = session.get(DataOperationRun, UUID(target["resourceId"]))
        if run is None:
            raise OperationProblem(
                status=404, code="run-not-found", detail="Data sync run is not found"
            )
        return run.command_id

    def _idempotent_resource(
        self, session: Session, operation: str, key: str, request_hash: str
    ) -> DataOperationIdempotency | None:
        """读取同操作幂等记录，并拒绝相同键不同请求体。"""
        row = session.scalar(
            select(DataOperationIdempotency)
            .where(
                DataOperationIdempotency.operation == operation,
                DataOperationIdempotency.idempotency_key == key,
            )
            .with_for_update()
        )
        if row is None:
            return None
        if row.request_hash != request_hash:
            raise OperationProblem(
                status=409,
                code="idempotency-conflict",
                detail="Idempotency key was used with a different request",
            )
        return row

    def _record_idempotency(
        self,
        session: Session,
        operation: str,
        key: str,
        request_hash: str,
        resource_type: str,
        resource_id: UUID,
        response: dict[str, Any],
        now: datetime,
    ) -> None:
        """在创建权威资源同一事务记录幂等结果。"""
        session.add(
            DataOperationIdempotency(
                idempotency_id=uuid4(),
                operation=operation,
                idempotency_key=key,
                request_hash=request_hash,
                resource_type=resource_type,
                resource_id=resource_id,
                response_json=response,
                created_at=now,
            )
        )

    def _record_event(
        self,
        session: Session,
        resource_type: str,
        resource_id: UUID,
        action: str,
        result: str,
        actor_ref: str,
        request_id: str,
        error: dict[str, Any] | None,
    ) -> None:
        """追加不可变事件，绝不保存 provider 正文、栈或凭据。"""
        session.add(
            DataOperationEvent(
                event_id=uuid4(),
                resource_type=resource_type,
                resource_id=resource_id,
                action=action,
                result=result,
                actor_ref=actor_ref,
                request_id=request_id,
                error_json=error,
                occurred_at=self._now(),
            )
        )

    def _record_automatic_health_evaluation(
        self,
        session: Session,
        run: DataOperationRun,
        data_version: UUID | None,
        now: datetime,
    ) -> UUID | None:
        """仅在真实 publication/release 绑定存在时写入自动健康事实，绝不伪造版本 UUID。"""
        if data_version is None:
            return None
        binding = self._health_publication_binding(session, run.dataset_code, data_version)
        if binding is None:
            # 旧 publication 尚未迁入 immutable release 时，保留 run 成功与既有发布；健康保持
            # UNKNOWN，不能以随机 UUID 伪造一个可对账的 HealthEvaluation。
            return None
        evaluation = self._record_health_evaluation(
            session,
            binding=binding,
            health_check_id=None,
            now=now,
        )
        return evaluation.evaluation_id

    def _health_publication_binding(
        self,
        session: Session,
        dataset_code: str,
        data_version: UUID | None,
    ) -> PublicationBinding | None:
        """解析健康检查冻结版本到真实 immutable release；缺任一环即返回空而非降级猜测。"""
        publication = (
            self._publication_for_data_version(session, dataset_code, data_version)
            if data_version is not None
            else self._current_publication(session, dataset_code)
        )
        if publication is None or publication.release_id is None:
            return None
        release = session.get(DatasetRelease, publication.release_id)
        if release is None:
            return None
        canonical_dataset = session.get(CanonicalDataset, release.dataset_id)
        return PublicationBinding(
            publication=publication,
            release=release,
            canonical_dataset=canonical_dataset,
        )

    def _record_health_evaluation(
        self,
        session: Session,
        *,
        binding: PublicationBinding,
        health_check_id: UUID | None,
        now: datetime,
    ) -> DataOperationHealthEvaluation:
        """固化一份版本绑定的健康事实，并独立刷新当前开放问题投影。"""
        source_evaluation = session.scalar(
            select(QualityEvaluation)
            .where(QualityEvaluation.normalization_run_id == binding.release.normalization_run_id)
            .order_by(QualityEvaluation.evaluated_at.desc())
            .limit(1)
        )
        source_results = (
            session.scalars(
                select(QualityResult).where(
                    QualityResult.evaluation_id == source_evaluation.evaluation_id
                )
            ).all()
            if source_evaluation is not None
            else []
        )
        results = self._health_results(binding, source_results, now)
        evaluation = DataOperationHealthEvaluation(
            evaluation_id=uuid4(),
            health_check_id=health_check_id,
            dataset_code=binding.publication.dataset,
            data_version=binding.publication.data_version,
            release_id=binding.release.release_id,
            policy_code="data-operations-default",
            policy_version=1,
            status=self._health_status(results),
            score=self._health_score(results),
            results_json=results[:_HEALTH_RESULT_LIMIT],
            evaluated_at=now,
        )
        session.add(evaluation)
        # 问题投影可以随新评估开闭，但绝不改写上述 immutable results_json。
        self._project_health_issues(session, binding.publication.dataset, results, now)
        return evaluation

    def _health_results(
        self,
        binding: PublicationBinding,
        source_results: Sequence[QualityResult],
        now: datetime,
    ) -> list[dict[str, Any]]:
        """执行受控健康规则框架，所有样例均为有界聚合文本而非原始业务记录。"""
        definition = self._catalog.get(binding.publication.dataset)
        publication = binding.publication
        release = binding.release
        canonical = binding.canonical_dataset
        results: list[dict[str, Any]] = []
        if definition is not None and definition.model_only:
            results.append(
                self._health_result(
                    rule_code="freshness",
                    dimension="FRESHNESS",
                    severity="INFO",
                    status="SKIPPED",
                    expected=None,
                    observed=None,
                    affected_count=None,
                    message="MODEL_ONLY 数据集不适用 freshness 规则",
                )
            )
        else:
            age_minutes = max(0, int((now - publication.published_at).total_seconds() // 60))
            freshness_status = "PASSED" if age_minutes <= 1440 else "WARNED"
            if age_minutes > 4320:
                freshness_status = "FAILED"
            results.append(
                self._health_result(
                    rule_code="freshness",
                    dimension="FRESHNESS",
                    severity="WARN",
                    status=freshness_status,
                    expected="published within 4320 minutes",
                    observed=f"{age_minutes} minutes since publication",
                    affected_count=0 if freshness_status == "PASSED" else None,
                    message="基于冻结 publication 时间计算 freshness",
                )
            )
        results.extend(
            (
                self._health_result(
                    rule_code="publication-completeness",
                    dimension="COMPLETENESS",
                    severity="CRITICAL",
                    status=self._quality_status_or(
                        source_results,
                        ("complete", "coverage"),
                        "FAILED"
                        if publication.quality_status.casefold() == "partial"
                        else "PASSED",
                    ),
                    expected="non-negative immutable release record count",
                    observed=str(release.record_count),
                    affected_count=0 if release.record_count >= 0 else None,
                    message="发布版本绑定 immutable release 的记录计数",
                ),
                self._health_result(
                    rule_code="record-uniqueness",
                    dimension="UNIQUENESS",
                    severity="CRITICAL",
                    status=self._quality_status_or(
                        source_results,
                        ("unique", "duplicate"),
                        "PASSED"
                        if re.fullmatch(r"[0-9a-f]{64}", release.content_hash) is not None
                        else "FAILED",
                    ),
                    expected="immutable release has a SHA-256 content identity",
                    observed="content identity present"
                    if re.fullmatch(r"[0-9a-f]{64}", release.content_hash) is not None
                    else "content identity invalid",
                    affected_count=0,
                    message="使用 release 内容身份和领域质量结果核验唯一性",
                ),
                self._health_result(
                    rule_code="data-validity",
                    dimension="VALIDITY",
                    severity="CRITICAL",
                    status=self._quality_status_or(
                        source_results,
                        ("valid", "range", "value"),
                        "FAILED"
                        if publication.quality_status.casefold() == "partial"
                        else "WARNED"
                        if publication.quality_status.casefold() == "warned"
                        else "PASSED",
                    ),
                    expected="approved publication quality",
                    observed=publication.quality_status,
                    affected_count=0,
                    message="使用发布前质量结论核验值域和业务有效性",
                ),
                self._health_result(
                    rule_code="schema-compatible",
                    dimension="SCHEMA",
                    severity="CRITICAL",
                    status=self._quality_status_or(
                        source_results,
                        ("schema",),
                        "PASSED"
                        if canonical is not None and canonical.status == "production"
                        else "FAILED",
                    ),
                    expected="production canonical dataset and immutable release",
                    observed="production canonical dataset"
                    if canonical is not None and canonical.status == "production"
                    else "canonical dataset is unavailable",
                    affected_count=0,
                    message="校验 release 所属 canonical schema 的生产身份",
                ),
                self._health_result(
                    rule_code="temporal-order",
                    dimension="TEMPORAL",
                    severity="WARN",
                    status=self._quality_status_or(
                        source_results,
                        ("temporal", "date", "time"),
                        self._temporal_health_status(release),
                    ),
                    expected="fact range is ordered when dates apply",
                    observed=self._temporal_health_observation(release),
                    affected_count=0,
                    message="校验 immutable release 的业务日期边界",
                ),
                self._health_result(
                    rule_code="identity-valid",
                    dimension="IDENTITY",
                    severity="CRITICAL",
                    status=self._quality_status_or(
                        source_results,
                        ("identity",),
                        "PASSED"
                        if canonical is not None and canonical.code == publication.dataset
                        else "FAILED",
                    ),
                    expected="canonical dataset code matches publication dataset",
                    observed=canonical.code if canonical is not None else None,
                    affected_count=0,
                    message="校验消费者 publication 与 canonical 数据集身份一致",
                ),
                self._health_result(
                    rule_code="domain-invariants",
                    dimension="CONSISTENCY",
                    severity="CRITICAL",
                    status=self._quality_status_or(
                        source_results,
                        ("domain", "invariant", "consistency"),
                        "PASSED"
                        if definition is not None
                        and canonical is not None
                        and canonical.domain == definition.domain
                        else "FAILED",
                    ),
                    expected=definition.domain if definition is not None else None,
                    observed=canonical.domain if canonical is not None else None,
                    affected_count=0,
                    message="校验目录数据域与 canonical 数据域不发生混用",
                ),
            )
        )
        return results[:_HEALTH_RESULT_LIMIT]

    def _health_result(
        self,
        *,
        rule_code: str,
        dimension: str,
        severity: str,
        status: str,
        expected: str | None,
        observed: str | None,
        affected_count: int | None,
        message: str,
    ) -> dict[str, Any]:
        """构造单条有界脱敏规则结果，避免规则实现意外输出原始来源内容。"""
        return {
            "ruleCode": rule_code[:120],
            "dimension": dimension,
            "severity": severity,
            "status": status,
            "expected": expected[:300] if expected is not None else None,
            "observed": observed[:300] if observed is not None else None,
            "affectedCount": max(0, affected_count) if affected_count is not None else None,
            "sampleSummary": None,
            "message": message[:500],
        }

    def _quality_status_or(
        self,
        source_results: Sequence[QualityResult],
        keywords: tuple[str, ...],
        fallback: str,
    ) -> str:
        """优先采用同一 release 的领域质量结果，缺规则时才使用通用 immutable 不变量。"""
        matched = [
            item
            for item in source_results
            if any(keyword in item.rule_code.casefold() for keyword in keywords)
        ]
        if not matched:
            return fallback
        if any(not item.passed and item.severity == "blocking" for item in matched):
            return "FAILED"
        if any(not item.passed for item in matched):
            return "WARNED"
        return "PASSED"

    def _temporal_health_status(self, release: DatasetRelease) -> str:
        """根据 release 冻结的事实范围返回时序规则状态，不把无日期粒度误报为失败。"""
        if release.fact_min is None and release.fact_max is None:
            return "SKIPPED"
        if release.fact_min is None or release.fact_max is None:
            return "FAILED"
        return "PASSED" if release.fact_min <= release.fact_max else "FAILED"

    def _temporal_health_observation(self, release: DatasetRelease) -> str | None:
        """投影日期边界的安全聚合说明，不输出单条业务事实或来源样本。"""
        if release.fact_min is None and release.fact_max is None:
            return "not applicable"
        if release.fact_min is None or release.fact_max is None:
            return "incomplete fact range"
        return f"{release.fact_min.isoformat()}..{release.fact_max.isoformat()}"

    def _health_status(self, results: list[dict[str, Any]]) -> str:
        """按规则严重度聚合不可变健康状态，未知不会被误报为健康。"""
        if any(item["status"] == "FAILED" and item["severity"] == "CRITICAL" for item in results):
            return "CRITICAL"
        if any(item["status"] in {"FAILED", "WARNED"} for item in results):
            return "WARN"
        if any(item["status"] == "UNKNOWN" for item in results):
            return "UNKNOWN"
        return "HEALTHY"

    def _health_score(self, results: list[dict[str, Any]]) -> int | None:
        """生成可解释的零到一百分摘要；只有无法判断时才返回 null。"""
        if not results or all(item["status"] == "UNKNOWN" for item in results):
            return None
        critical = sum(
            1 for item in results if item["status"] == "FAILED" and item["severity"] == "CRITICAL"
        )
        warning = sum(1 for item in results if item["status"] in {"FAILED", "WARNED"})
        unknown = sum(1 for item in results if item["status"] == "UNKNOWN")
        return max(0, 100 - critical * 40 - warning * 10 - unknown * 5)

    def _project_health_issues(
        self,
        session: Session,
        dataset_code: str,
        results: list[dict[str, Any]],
        now: datetime,
    ) -> None:
        """只更新当前 issue 投影；历史 HealthEvaluation 的规则事实保持不可变。"""
        for result in results:
            statement = select(DataOperationHealthIssue).where(
                DataOperationHealthIssue.dataset_code == dataset_code,
                DataOperationHealthIssue.rule_code == result["ruleCode"],
                DataOperationHealthIssue.dimension == result["dimension"],
                DataOperationHealthIssue.status.in_(("OPEN", "ACKNOWLEDGED")),
            )
            issue = session.scalar(statement.with_for_update())
            status = result["status"]
            if status in {"FAILED", "WARNED"}:
                severity = (
                    "CRITICAL"
                    if status == "FAILED" and result["severity"] == "CRITICAL"
                    else "WARN"
                )
                if issue is None:
                    session.add(
                        DataOperationHealthIssue(
                            issue_id=uuid4(),
                            dataset_code=dataset_code,
                            rule_code=result["ruleCode"],
                            dimension=result["dimension"],
                            severity=severity,
                            status="OPEN",
                            first_detected_at=now,
                            last_detected_at=now,
                            affected_count=result["affectedCount"],
                            evidence_summary=result["message"][:500],
                        )
                    )
                else:
                    issue.severity = severity
                    issue.last_detected_at = now
                    issue.affected_count = result["affectedCount"]
                    issue.evidence_summary = result["message"][:500]
            elif status in {"PASSED", "SKIPPED"} and issue is not None:
                # RESOLVED 仅从 current projection 过滤，历史问题行和所有 evaluation 仍可审计。
                issue.status = "RESOLVED"
                issue.last_detected_at = now

    def _refresh_health_check_status(
        self, session: Session, check: DataOperationHealthCheck, now: datetime
    ) -> None:
        """从有序 target 的真实状态聚合批次终态，不把 CRITICAL 健康结论当执行失败。"""
        targets = session.scalars(
            select(DataOperationHealthCheckTarget)
            .where(DataOperationHealthCheckTarget.health_check_id == check.health_check_id)
            .order_by(DataOperationHealthCheckTarget.target_index)
        ).all()
        statuses = [item.status for item in targets]
        if not statuses:
            check.status = "REJECTED"
            check.finished_at = now
            return
        if "RUNNING" in statuses or (check.started_at is not None and "QUEUED" in statuses):
            check.status = "RUNNING"
            return
        if "QUEUED" in statuses:
            check.status = "QUEUED"
            return
        if all(status == "SUCCEEDED" for status in statuses):
            check.status = "SUCCEEDED"
        elif all(status == "REJECTED" for status in statuses):
            check.status = "REJECTED"
        elif all(status == "CANCELLED" for status in statuses):
            check.status = "CANCELLED"
        elif "SUCCEEDED" in statuses:
            check.status = "PARTIAL"
        else:
            check.status = "FAILED"
        check.finished_at = now

    def _health_rules(self, model_only: bool) -> list[dict[str, Any]]:
        """返回覆盖 freshness、完整性、唯一性、有效性、schema、时序、身份与领域不变量的规则框架。"""
        rules = [
            {
                "ruleCode": "publication-completeness",
                "dimension": "COMPLETENESS",
                "severity": "CRITICAL",
                "version": 1,
            },
            {
                "ruleCode": "record-uniqueness",
                "dimension": "UNIQUENESS",
                "severity": "CRITICAL",
                "version": 1,
            },
            {
                "ruleCode": "data-validity",
                "dimension": "VALIDITY",
                "severity": "CRITICAL",
                "version": 1,
            },
            {
                "ruleCode": "schema-compatible",
                "dimension": "SCHEMA",
                "severity": "CRITICAL",
                "version": 1,
            },
            {
                "ruleCode": "temporal-order",
                "dimension": "TEMPORAL",
                "severity": "WARN",
                "version": 1,
            },
            {
                "ruleCode": "identity-valid",
                "dimension": "IDENTITY",
                "severity": "CRITICAL",
                "version": 1,
            },
            {
                "ruleCode": "domain-invariants",
                "dimension": "CONSISTENCY",
                "severity": "CRITICAL",
                "version": 1,
            },
        ]
        if not model_only:
            rules.insert(
                0,
                {
                    "ruleCode": "freshness",
                    "dimension": "FRESHNESS",
                    "severity": "WARN",
                    "version": 1,
                },
            )
        return rules

    def _health_summary_for(
        self, session: Session, dataset_code: str, evaluation: DataOperationHealthEvaluation | None
    ) -> dict[str, Any]:
        """聚合最新不可变评估与当前开放问题，不回写评估事实。"""
        issues = session.scalars(
            select(DataOperationHealthIssue).where(
                DataOperationHealthIssue.dataset_code == dataset_code,
                DataOperationHealthIssue.status.in_(("OPEN", "ACKNOWLEDGED")),
            )
        ).all()
        warning = sum(1 for issue in issues if issue.severity == "WARN")
        critical = sum(1 for issue in issues if issue.severity == "CRITICAL")
        if evaluation is None:
            return {
                "status": "UNKNOWN",
                "score": None,
                "evaluatedAt": None,
                "evaluationId": None,
                "warningCount": warning,
                "criticalCount": critical,
                "openIssueCount": len(issues),
                "affectedRecordCount": None,
            }
        status = "CRITICAL" if critical else "WARN" if warning else evaluation.status
        return {
            "status": status,
            "score": evaluation.score,
            "evaluatedAt": self._iso(evaluation.evaluated_at),
            "evaluationId": str(evaluation.evaluation_id),
            "warningCount": warning,
            "criticalCount": critical,
            "openIssueCount": len(issues),
            "affectedRecordCount": sum(issue.affected_count or 0 for issue in issues)
            if issues
            else 0,
        }

    def _aggregate_health(self, summaries: list[dict[str, Any]]) -> dict[str, Any]:
        """从数据集健康摘要汇总总览健康，不杜撰未知评分。"""
        values = [item["healthSummary"] for item in summaries]
        critical = sum(item["criticalCount"] for item in values)
        warning = sum(item["warningCount"] for item in values)
        statuses = {str(item["status"]) for item in values}
        if critical:
            status = "CRITICAL"
        elif warning:
            status = "WARN"
        elif "UNKNOWN" in statuses:
            # 没有 immutable evaluation 的数据集不能因“尚未发现 issue”被伪装为健康。
            status = "UNKNOWN"
        else:
            status = "HEALTHY"
        return {
            "status": status,
            "score": None,
            "evaluatedAt": self._iso(self._now()),
            "evaluationId": None,
            "warningCount": warning,
            "criticalCount": critical,
            "openIssueCount": sum(item["openIssueCount"] for item in values),
            "affectedRecordCount": None,
        }

    def _health_summary(
        self, session: Session, evaluation: DataOperationHealthEvaluation
    ) -> dict[str, Any]:
        """投影评估列表摘要，结果明细仅在 detail 端点返回。"""
        issues = session.scalars(
            select(DataOperationHealthIssue).where(
                DataOperationHealthIssue.dataset_code == evaluation.dataset_code,
                DataOperationHealthIssue.status.in_(("OPEN", "ACKNOWLEDGED")),
            )
        ).all()
        results = evaluation.results_json
        return {
            "evaluationId": str(evaluation.evaluation_id),
            "healthCheckId": self._uuid_text(evaluation.health_check_id),
            "datasetCode": evaluation.dataset_code,
            "dataVersion": str(evaluation.data_version),
            "releaseId": str(evaluation.release_id),
            "policyCode": evaluation.policy_code,
            "policyVersion": evaluation.policy_version,
            "status": evaluation.status,
            "score": evaluation.score,
            "evaluatedAt": self._iso(evaluation.evaluated_at),
            "warningCount": sum(
                1
                for item in results
                if item.get("severity") == "WARN" and item.get("status") != "PASSED"
            ),
            "criticalCount": sum(
                1
                for item in results
                if item.get("severity") == "CRITICAL" and item.get("status") != "PASSED"
            ),
            "currentOpenIssueCount": len(issues),
            "issueProjectionAsOf": self._iso(self._now()),
            "affectedRecordCount": sum((item.get("affectedCount") or 0) for item in results),
        }

    def _health_evaluation_view(self, evaluation: DataOperationHealthEvaluation) -> dict[str, Any]:
        """投影完整不可变评估事实，结果保持有界与脱敏。"""
        return {
            "evaluationId": str(evaluation.evaluation_id),
            "healthCheckId": self._uuid_text(evaluation.health_check_id),
            "datasetCode": evaluation.dataset_code,
            "dataVersion": str(evaluation.data_version),
            "releaseId": str(evaluation.release_id),
            "policyCode": evaluation.policy_code,
            "policyVersion": evaluation.policy_version,
            "status": evaluation.status,
            "score": evaluation.score,
            "evaluatedAt": self._iso(evaluation.evaluated_at),
            "results": evaluation.results_json[:500],
        }

    def _issue_view(self, issue: DataOperationHealthIssue) -> dict[str, Any]:
        """投影当前问题，不输出原始记录或 provider payload。"""
        return {
            "issueId": str(issue.issue_id),
            "ruleCode": issue.rule_code,
            "dimension": issue.dimension,
            "severity": issue.severity,
            "status": issue.status,
            "firstDetectedAt": self._iso(issue.first_detected_at),
            "lastDetectedAt": self._iso(issue.last_detected_at),
            "affectedCount": issue.affected_count,
            "evidenceSummary": issue.evidence_summary,
        }

    def _health_target_view(self, target: DataOperationHealthCheckTarget) -> dict[str, Any]:
        """投影主动检查单目标结果，成功与错误字段遵守互斥空值语义。"""
        return {
            "target": {
                "datasetCode": target.dataset_code,
                "dataVersion": self._uuid_text(target.requested_data_version),
            },
            "resolvedDataVersion": self._uuid_text(target.resolved_data_version),
            "status": target.status,
            "evaluationId": self._uuid_text(target.evaluation_id),
            "error": target.error_json,
        }

    def _health_check_receipt(
        self, session: Session, health_check_id: UUID, submission_id: UUID
    ) -> dict[str, Any]:
        """返回幂等重放的健康检查受理收据。"""
        check = session.get(DataOperationHealthCheck, health_check_id)
        assert check is not None
        return {
            "healthCheckId": str(check.health_check_id),
            "submissionId": str(submission_id),
            "status": check.status,
            "acceptedAt": self._iso(check.requested_at),
        }

    def _validate_health_targets(self, raw_targets: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """验证主动健康检查目标数量、唯一 datasetCode 和可选 UUID 版本。"""
        if not 1 <= len(raw_targets) <= 100:
            raise OperationProblem(
                status=422,
                code="invalid-target-count",
                detail="Health check target count must be between 1 and 100",
            )
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in raw_targets:
            if not isinstance(raw, dict):
                raise OperationProblem(
                    status=400, code="validation-error", detail="Health check target is invalid"
                )
            code = self._require_string(raw, "datasetCode", max_length=160)
            self._definition(code)
            if code in seen:
                raise OperationProblem(
                    status=422,
                    code="duplicate-dataset-code",
                    detail="Dataset code must be unique in a batch",
                )
            seen.add(code)
            version = raw.get("dataVersion")
            if version is not None:
                self._parse_uuid_or_none(version)
            result.append({"datasetCode": code, "dataVersion": version})
        return result

    def _validate_schedule(
        self,
        definition: DatasetDefinition,
        mode: str,
        policy: dict[str, Any],
        frequency: dict[str, Any],
    ) -> dict[str, Any]:
        """校验目录允许策略和结构化频率，并返回不可含任意 cron 的副本。"""
        if definition.dataset_code == _MARKET_OVERVIEW_DATASET_CODE and mode != "INCREMENTAL":
            raise OperationProblem(
                status=400,
                code="market-overview-schedule-invalid",
                detail="Market overview schedule must use INCREMENTAL mode",
            )
        if (
            definition.model_only
            or not definition.config_enabled
            or not definition.dispatcher_ready
            or (not self._providers_for(definition) and not definition.providerless)
        ):
            raise OperationProblem(
                status=422,
                code="schedule-not-eligible",
                detail="Dataset is not currently eligible for schedules",
            )
        if mode not in definition.schedule_modes:
            raise OperationProblem(
                status=422,
                code="unsupported-schedule-mode",
                detail="Dataset does not support this schedule mode",
            )
        if set(policy) != {"policyVersion", "dateResolution"}:
            raise OperationProblem(
                status=422,
                code="invalid-schedule-policy",
                detail="Schedule policy shape is invalid",
            )
        policy_version = policy.get("policyVersion")
        if (
            not isinstance(policy_version, int)
            or isinstance(policy_version, bool)
            or policy_version < 1
        ):
            raise OperationProblem(
                status=422,
                code="invalid-schedule-policy",
                detail="Schedule policy version is invalid",
            )
        if definition.dataset_code == _MARKET_OVERVIEW_DATASET_CODE and policy != {
            "policyVersion": 1,
            "dateResolution": "NONE",
        }:
            raise OperationProblem(
                status=400,
                code="market-overview-schedule-invalid",
                detail="Market overview schedule target policy is fixed",
            )
        if not any(
            option["mode"] == mode and option["policy"] == policy
            for option in self._schedule_target_policy_options(definition)
        ):
            raise OperationProblem(
                status=422,
                code="invalid-schedule-policy",
                detail="Schedule policy is not allowed by the current dataset capability",
            )
        try:
            normalized_frequency = validate_frequency(frequency)
        except ScheduleFrequencyError as error:
            raise OperationProblem(
                status=422,
                code="invalid-schedule-frequency",
                detail="Schedule frequency is invalid",
            ) from error
        if definition.dataset_code == _MARKET_OVERVIEW_DATASET_CODE and (
            normalized_frequency["kind"] != "TRADING_DAY"
            or normalized_frequency["timezone"] != _SCHEDULE_CALENDAR_TIMEZONE
            or normalized_frequency["localTime"] != _MARKET_OVERVIEW_SCHEDULE_LOCAL_TIME
            or normalized_frequency["calendarCode"] != _SCHEDULE_CALENDAR_CODE
        ):
            # 个股资金流在 19:00 后才形成完整来源横截面；固定 19:20 可避免 17:20
            # EOD eligibility 被误当成全包抓取时刻并稳定产生缺组件 publication。
            raise OperationProblem(
                status=400,
                code="market-overview-schedule-invalid",
                detail=(
                    "Market overview schedule must use the registered Shanghai "
                    "trading calendar at 19:20"
                ),
            )
        if (
            normalized_frequency["kind"] == "TRADING_DAY"
            and normalized_frequency["calendarCode"] != _SCHEDULE_CALENDAR_CODE
        ):
            raise OperationProblem(
                status=422,
                code="invalid-schedule-calendar",
                detail="Schedule calendar is not registered",
            )
        if policy["dateResolution"] == "LATEST_COMPLETED_TRADING_DATE" and (
            normalized_frequency["kind"] != "TRADING_DAY"
            or normalized_frequency["calendarCode"] != _SCHEDULE_CALENDAR_CODE
            or normalized_frequency["timezone"] != _SCHEDULE_CALENDAR_TIMEZONE
        ):
            # 当前唯一日历端口只证明沪深 A 股当地收市后的完成日；不能把任意时区或
            # 自由 calendarCode 误绑定到该日历，再以“最近完成”名义提交 EOD。
            raise OperationProblem(
                status=422,
                code="invalid-schedule-calendar",
                detail="Latest completed trading date needs the registered market calendar",
            )
        if definition.dataset_code == "fund.etf.profile.reported" and (
            normalized_frequency["kind"] != "TRADING_DAY"
            or normalized_frequency["calendarCode"] != _SCHEDULE_CALENDAR_CODE
            or normalized_frequency["timezone"] != _SCHEDULE_CALENDAR_TIMEZONE
        ):
            raise OperationProblem(
                status=422,
                code="invalid-schedule-calendar",
                detail="ETF profile schedule requires the registered Shanghai trading calendar",
            )
        return normalized_frequency

    def _schedule_target_policy_options(
        self, definition: DatasetDefinition
    ) -> list[dict[str, Any]]:
        """为数据集生成唯一默认的版本化计划目标策略，供校验和目录共用。"""
        return [
            {
                "mode": mode,
                "policy": {
                    "policyVersion": 1,
                    "dateResolution": (
                        "NONE"
                        if mode != "OBSERVATION_DATE"
                        else "SCHEDULED_LOCAL_DATE"
                        if definition.dataset_code == "fund.etf.profile.reported"
                        else "LATEST_COMPLETED_TRADING_DATE"
                    ),
                },
                "isDefault": True,
            }
            for mode in definition.schedule_modes
        ]

    def _schedule_snapshot(self, schedule: DataOperationSchedule) -> dict[str, Any]:
        """复制当前计划可审计字段，避免 JSON ORM 对象在后续赋值后污染 before 摘要。"""
        return json.loads(
            json.dumps(
                {
                    "datasetCode": schedule.dataset_code,
                    "mode": schedule.mode,
                    "selector": schedule.selector_json,
                    "targetPolicy": schedule.target_policy_json,
                    "frequency": schedule.frequency_json,
                    "misfirePolicy": schedule.misfire_policy,
                    "coalesce": schedule.coalesce,
                    "enabled": schedule.enabled,
                    "version": schedule.version,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )

    def _append_schedule_revision(
        self,
        session: Session,
        *,
        schedule: DataOperationSchedule,
        change_kind: str,
        before_snapshot: dict[str, Any],
        actor_ref: str,
        request_id: str,
        now: datetime,
    ) -> None:
        """把当前计划完整冻结为 immutable revision，并保存本次变更前后安全摘要。"""
        after_snapshot = self._schedule_snapshot(schedule)
        session.add(
            DataOperationScheduleRevision(
                revision_id=schedule.revision_id,
                schedule_id=schedule.schedule_id,
                version=schedule.version,
                change_kind=change_kind,
                dataset_code=schedule.dataset_code,
                mode=schedule.mode,
                selector_json=after_snapshot["selector"],
                target_policy_json=after_snapshot["targetPolicy"],
                frequency_json=after_snapshot["frequency"],
                misfire_policy=schedule.misfire_policy,
                coalesce=schedule.coalesce,
                enabled=schedule.enabled,
                before_hash=self._hash(before_snapshot),
                after_hash=self._hash(after_snapshot),
                actor_ref=actor_ref,
                request_id=request_id,
                created_at=now,
            )
        )

    def _schedule_view(self, schedule: DataOperationSchedule) -> dict[str, Any]:
        """投影计划和至多五个未来 occurrence，不让客户端自行解释频率。"""
        summary = self._schedule_summary(schedule)
        return {
            "summary": summary,
            "datasetCode": schedule.dataset_code,
            "mode": schedule.mode,
            "selector": schedule.selector_json,
            "targetPolicy": schedule.target_policy_json,
            "misfirePolicy": schedule.misfire_policy,
            "coalesce": schedule.coalesce,
            "recentRunAt": self._iso(schedule.recent_run_at),
            "nextOccurrences": self._schedule_occurrences(schedule),
            "revisionId": str(schedule.revision_id),
            "updatedAt": self._iso(schedule.updated_at),
            "updatedByActorRef": schedule.updated_by_actor_ref,
        }

    def _schedule_summary(self, schedule: DataOperationSchedule) -> dict[str, Any]:
        """投影数据集摘要内可嵌套的轻量计划状态。"""
        return {
            "scheduleId": str(schedule.schedule_id),
            "enabled": schedule.enabled,
            "frequency": schedule.frequency_json,
            "nextRunAt": self._iso(schedule.next_run_at),
            "version": schedule.version,
        }

    def _next_schedule_occurrence(
        self, frequency: dict[str, Any], after: datetime
    ) -> datetime | None:
        """使用权威日历计算下一 fire；日历未知时返回空而不以周末规则猜测。"""
        try:
            return next_occurrence(
                frequency,
                after,
                calendar=getattr(self, "_trading_calendar", None),
            )
        except (ScheduleCalendarUnavailableError, ScheduleFrequencyError):
            return None

    def _schedule_occurrences(self, schedule: DataOperationSchedule) -> list[str]:
        """返回已持久化下次时刻及后续四次；日历未知时只返回已知时刻。"""
        if not schedule.enabled or schedule.next_run_at is None:
            return []
        values = [schedule.next_run_at]
        try:
            values.extend(
                next_occurrences(
                    schedule.frequency_json,
                    schedule.next_run_at,
                    calendar=getattr(self, "_trading_calendar", None),
                    count=4,
                )
            )
        except (ScheduleCalendarUnavailableError, ScheduleFrequencyError):
            pass
        occurrences: list[str] = []
        for value in values:
            serialized = self._iso(value)
            if serialized is not None:
                occurrences.append(serialized)
        return occurrences

    def _schedule_target(
        self,
        session: Session,
        revision: DataOperationScheduleRevision,
        scheduled_for: datetime,
    ) -> tuple[dict[str, Any], date | None, EtfUniverseSnapshot | None]:
        """从 immutable revision 与 scheduledFor 解析 target，绝不使用 tick 当前日期。"""
        selector = dict(revision.selector_json)
        etf_universe: EtfUniverseSnapshot | None = None
        if selector.get("kind") == "ETF" and selector.get("scope") == "ALL_ETFS":
            versions = resolve_current_etf_profile_data_versions(session)
            etf_universe = load_frozen_etf_universe(
                session,
                profile_data_versions=versions,
            )
            selector["profileDataVersions"] = {
                venue: str(etf_universe.profile_data_versions[venue]) for venue in ("SSE", "SZSE")
            }
        target: dict[str, Any] = {
            "datasetCode": revision.dataset_code,
            "mode": revision.mode,
            "selector": selector,
            "dateFrom": None,
            "dateTo": None,
            "observationDate": None,
        }
        resolved_observation_date: date | None = None
        if revision.mode == "OBSERVATION_DATE":
            resolved_observation_date = resolve_observation_date(
                revision.frequency_json,
                str(revision.target_policy_json["dateResolution"]),
                scheduled_for,
                calendar=getattr(self, "_trading_calendar", None),
            )
            target["observationDate"] = resolved_observation_date.isoformat()
        return target, resolved_observation_date, etf_universe

    def _schedule_fire_id(
        self, schedule_id: UUID, scheduled_for: datetime, schedule_version: int
    ) -> UUID:
        """按合同 UUIDv5(scheduleId, scheduledFor, scheduleVersion) 生成可重放 fire 键。"""
        return uuid5(
            NAMESPACE_URL,
            f"{schedule_id}:{scheduled_for.astimezone(UTC).isoformat()}:{schedule_version}",
        )

    def _record_schedule_skipped_fire(
        self,
        session: Session,
        *,
        schedule: DataOperationSchedule,
        revision: DataOperationScheduleRevision,
        scheduled_for: datetime,
        reason_code: str,
        coalesced_count: int,
        now: datetime,
    ) -> None:
        """持久化不创建 command 的漏跑事实，重复 tick 复用同一 UUIDv5 fire。"""
        fire_id = self._schedule_fire_id(schedule.schedule_id, scheduled_for, schedule.version)
        if session.get(DataOperationScheduleFire, fire_id) is not None:
            return
        request_id = f"schedule:{fire_id}"
        session.add(
            DataOperationScheduleFire(
                fire_id=fire_id,
                schedule_id=schedule.schedule_id,
                revision_id=revision.revision_id,
                schedule_version=schedule.version,
                scheduled_for=scheduled_for,
                selector_json=revision.selector_json,
                target_policy_json=revision.target_policy_json,
                target_policy_version=int(revision.target_policy_json["policyVersion"]),
                target_json=None,
                resolved_observation_date=None,
                command_id=None,
                outcome="SKIPPED",
                reason_code=reason_code,
                coalesced_count=coalesced_count,
                request_id=request_id,
                created_at=now,
            )
        )
        self._record_event(
            session,
            "SCHEDULE",
            schedule.schedule_id,
            "COALESCE" if coalesced_count else "FIRE",
            "SKIPPED",
            self._schedule_actor_ref(schedule.schedule_id),
            request_id,
            self._error(reason_code, "SCHEDULE", False, "Schedule fire was skipped"),
        )

    def _record_schedule_rejected_fire(
        self,
        session: Session,
        *,
        schedule: DataOperationSchedule,
        revision: DataOperationScheduleRevision,
        scheduled_for: datetime,
        reason_code: str,
        now: datetime,
    ) -> None:
        """记录无法安全解析的 fire，保证日历未知或频率损坏不触发同步。"""
        fire_id = self._schedule_fire_id(schedule.schedule_id, scheduled_for, schedule.version)
        if session.get(DataOperationScheduleFire, fire_id) is not None:
            return
        request_id = f"schedule:{fire_id}"
        retryable = reason_code == "schedule-calendar-unavailable"
        session.add(
            DataOperationScheduleFire(
                fire_id=fire_id,
                schedule_id=schedule.schedule_id,
                revision_id=revision.revision_id,
                schedule_version=schedule.version,
                scheduled_for=scheduled_for,
                selector_json=revision.selector_json,
                target_policy_json=revision.target_policy_json,
                target_policy_version=int(revision.target_policy_json["policyVersion"]),
                target_json=None,
                resolved_observation_date=None,
                command_id=None,
                outcome="REJECTED",
                reason_code=reason_code,
                coalesced_count=0,
                request_id=request_id,
                created_at=now,
            )
        )
        self._record_event(
            session,
            "SCHEDULE",
            schedule.schedule_id,
            "FIRE",
            "REJECTED",
            self._schedule_actor_ref(schedule.schedule_id),
            request_id,
            self._error(reason_code, "SCHEDULE", retryable, "Schedule fire could not be resolved"),
        )

    def _queue_schedule_fire(
        self,
        session: Session,
        *,
        schedule: DataOperationSchedule,
        revision: DataOperationScheduleRevision,
        scheduled_for: datetime,
        coalesced_count: int,
        now: datetime,
    ) -> int:
        """把一个已冻结 fire 原子写成 command、run、receipt 与审计事件。"""
        fire_id = self._schedule_fire_id(schedule.schedule_id, scheduled_for, schedule.version)
        if session.get(DataOperationScheduleFire, fire_id) is not None:
            return 0
        if (
            revision.dataset_code == "fund.etf.profile.reported"
            and scheduled_for.astimezone(_SHANGHAI).date() != now.astimezone(_SHANGHAI).date()
        ):
            self._record_schedule_rejected_fire(
                session,
                schedule=schedule,
                revision=revision,
                scheduled_for=scheduled_for,
                reason_code="etf-profile-current-snapshot-unrecoverable",
                now=now,
            )
            return 0
        try:
            target, resolved_observation_date, etf_universe = self._schedule_target(
                session,
                revision,
                scheduled_for,
            )
        except (ScheduleCalendarUnavailableError, ScheduleFrequencyError):
            self._record_schedule_rejected_fire(
                session,
                schedule=schedule,
                revision=revision,
                scheduled_for=scheduled_for,
                reason_code="schedule-calendar-unavailable",
                now=now,
            )
            return 0
        except EtfUniverseUnavailable as error:
            self._record_schedule_rejected_fire(
                session,
                schedule=schedule,
                revision=revision,
                scheduled_for=scheduled_for,
                reason_code=error.reason_code,
                now=now,
            )
            return 0
        request_id = f"schedule:{fire_id}"
        command = DataOperationCommand(
            command_id=uuid4(),
            submission_id=None,
            status="QUEUED",
            actor_ref=self._schedule_actor_ref(schedule.schedule_id),
            actor_role="SYSTEM",
            reason="自动计划触发",
            request_id=request_id,
            retry_of_command_id=None,
            error_json=None,
            requested_at=now,
            started_at=None,
            finished_at=None,
        )
        definition = self._definition(revision.dataset_code)
        execution_intent: dict[str, Any] = {
            "scheduleFireId": str(fire_id),
            "scheduleVersion": schedule.version,
            "targetPolicyVersion": int(revision.target_policy_json["policyVersion"]),
            "resolvedObservationDate": (
                resolved_observation_date.isoformat()
                if resolved_observation_date is not None
                else None
            ),
        }
        selector = target["selector"]
        equity_roster: tuple[dict[str, str], ...] | None = None
        if revision.dataset_code == "equity.share_capital.reported":
            equity_roster = self._freeze_share_capital_roster(
                session,
                target=target,
                identity_as_of=scheduled_for.astimezone(_SHANGHAI).date(),
            )
            execution_intent.update(
                {
                    "equityInstrumentRoster": list(equity_roster),
                    "equityInstrumentRosterHash": self._hash(equity_roster),
                }
            )
        if selector.get("kind") == "ETF" and selector.get("operation") != "MASTER":
            resolved_from, resolved_to = self._resolve_etf_window(
                target,
                anchor_date=scheduled_for.astimezone(_SHANGHAI).date(),
            )
            execution_intent.update(
                {
                    "etfResolvedDateFrom": resolved_from,
                    "etfResolvedDateTo": resolved_to,
                }
            )
        if etf_universe is not None:
            execution_intent.update(
                {
                    "etfUniverseCount": etf_universe.count,
                    "etfUniverseHash": etf_universe.universe_hash,
                }
            )
            if selector.get("operation") == "NAV":
                execution_intent.update(
                    {
                        "etfNavEligibleCount": etf_universe.nav_eligible_count,
                        "etfNavUnsupportedCount": etf_universe.nav_unsupported_count,
                    }
                )
        session.add(command)
        session.add(
            DataOperationRun(
                run_id=uuid4(),
                command_id=command.command_id,
                target_index=0,
                dataset_code=revision.dataset_code,
                mode=revision.mode,
                target_json=target,
                source_snapshot=self._source_snapshot(definition, target=target),
                execution_intent_json=execution_intent,
                status="QUEUED",
                queue_position=None,
                attempt=0,
                recovery_attempts=0,
                completed_partitions=0,
                total_partitions=(
                    etf_universe.count
                    if etf_universe is not None
                    else len(equity_roster)
                    if equity_roster is not None
                    else 2
                    if selector.get("scope") == "ALL_VENUES"
                    else 1
                ),
                processed_records=0,
                estimated_records=None,
                fencing_token=None,
                cancel_requested=False,
                error_json=None,
                quality_gate_json=self._not_evaluated_gate(),
                requested_at=now,
                started_at=None,
                finished_at=None,
            )
        )
        session.add(
            DataOperationScheduleFire(
                fire_id=fire_id,
                schedule_id=schedule.schedule_id,
                revision_id=revision.revision_id,
                schedule_version=schedule.version,
                scheduled_for=scheduled_for,
                selector_json=revision.selector_json,
                target_policy_json=revision.target_policy_json,
                target_policy_version=int(revision.target_policy_json["policyVersion"]),
                target_json=target,
                resolved_observation_date=resolved_observation_date,
                command_id=command.command_id,
                outcome="QUEUED",
                reason_code=None,
                coalesced_count=coalesced_count,
                request_id=request_id,
                created_at=now,
            )
        )
        self._record_idempotency(
            session,
            "schedule-fire",
            str(fire_id),
            self._hash(
                {
                    "scheduleId": str(schedule.schedule_id),
                    "scheduledFor": scheduled_for.astimezone(UTC).isoformat(),
                    "scheduleVersion": schedule.version,
                }
            ),
            "COMMAND",
            command.command_id,
            {"commandId": str(command.command_id), "scheduleFireId": str(fire_id)},
            now,
        )
        schedule.recent_run_at = now
        self._record_event(
            session,
            "SCHEDULE",
            schedule.schedule_id,
            "COALESCE" if coalesced_count else "FIRE",
            "QUEUED",
            self._schedule_actor_ref(schedule.schedule_id),
            request_id,
            None,
        )
        return 1

    def _schedule_actor_ref(self, schedule_id: UUID) -> str:
        """为计划触发生成可追踪的 SYSTEM actor，避免把不同自动来源混为 plain system。"""
        return f"system:schedule/{schedule_id}"

    def _run_summary(self, run: DataOperationRun) -> dict[str, Any]:
        """投影 run 列表安全字段，不泄漏 source cursor、checkpoint 或 provider 内容。"""
        return {
            "runId": str(run.run_id),
            "commandId": str(run.command_id),
            "datasetCode": run.dataset_code,
            "mode": run.mode,
            "status": run.status,
            "queuePosition": run.queue_position,
            "requestedAt": self._iso(run.requested_at),
            "startedAt": self._iso(run.started_at),
            "finishedAt": self._iso(run.finished_at),
            "progress": {
                "completedPartitions": run.completed_partitions,
                "totalPartitions": run.total_partitions,
                "processedRecords": run.processed_records,
                "estimatedRecords": run.estimated_records,
            },
            "error": run.error_json,
        }

    def _partition_view(self, partition: DataOperationPartition) -> dict[str, Any]:
        """投影 checkpoint 的定长摘要，不能返回真实 Provider position。"""
        checkpoint = (
            None
            if partition.checkpoint_hash is None
            else {
                "kind": partition.checkpoint_kind or "opaque",
                "positionHash": partition.checkpoint_hash,
                "updatedAt": self._iso(partition.checkpoint_updated_at),
            }
        )
        return {
            "partitionKey": partition.partition_key,
            "status": partition.status,
            "attempt": partition.attempt,
            "checkpoint": checkpoint,
            "error": partition.error_json,
        }

    def _event_view(self, event: DataOperationEvent) -> dict[str, Any]:
        """投影可向 API 传递的不可变事件字段。"""
        return {
            "eventId": str(event.event_id),
            "resourceType": event.resource_type,
            "resourceId": str(event.resource_id),
            "action": event.action,
            "result": event.result,
            "actorRef": event.actor_ref,
            "requestId": event.request_id,
            "occurredAt": self._iso(event.occurred_at),
            "error": event.error_json,
        }

    def _latest_publication(self, session: Session, dataset_code: str) -> DatasetPublication | None:
        """读取数据集最新真实 publication，不用运行完成时间替代消费者可见版本时间。"""
        publication_dataset_code = self._publication_dataset_code(dataset_code)
        return session.scalar(
            select(DatasetPublication)
            .where(DatasetPublication.dataset == publication_dataset_code)
            .order_by(
                DatasetPublication.published_at.desc(), DatasetPublication.publication_id.desc()
            )
            .limit(1)
        )

    def _current_publication(
        self, session: Session, dataset_code: str
    ) -> DatasetPublication | None:
        """为主动健康检查选择当前 production publication，null target 不会重新解释为新版本。"""
        publication_dataset_code = self._publication_dataset_code(dataset_code)
        return session.scalar(
            select(DatasetPublication)
            .where(
                DatasetPublication.dataset == publication_dataset_code,
                DatasetPublication.superseded_at.is_(None),
            )
            .order_by(
                DatasetPublication.published_at.desc(), DatasetPublication.publication_id.desc()
            )
            .limit(1)
        )

    def _publication_for_data_version(
        self, session: Session, dataset_code: str, data_version: UUID
    ) -> DatasetPublication | None:
        """按数据集和冻结 dataVersion 定位历史或当前 publication，禁止用最新版本替代。"""
        publication_dataset_code = self._publication_dataset_code(dataset_code)
        return session.scalar(
            select(DatasetPublication)
            .where(
                DatasetPublication.dataset == publication_dataset_code,
                DatasetPublication.data_version == data_version,
            )
            .limit(1)
        )

    def _publication_dataset_code(self, dataset_code: str) -> str:
        """将控制面 datasetCode 映射到实际消费者 publication 数据集。

        这避免把 source raw capability 错当作消费者版本键。
        """
        definition = getattr(self, "_catalog", {}).get(dataset_code)
        return (
            definition.publication_dataset_code
            if definition is not None and definition.publication_dataset_code is not None
            else dataset_code
        )

    def _publication_view(
        self, session: Session, publication: DatasetPublication | None
    ) -> dict[str, Any] | None:
        """仅在真实 release 与记录计数可验证时公开 latestPublication，历史兼容行安全返回 null。"""
        if publication is None or publication.release_id is None:
            return None
        release = session.get(DatasetRelease, publication.release_id)
        if release is None:
            return None
        return {
            "dataVersion": str(publication.data_version),
            "releaseId": str(release.release_id),
            "publishedAt": self._iso(publication.published_at),
            "rowCount": max(0, release.record_count),
        }

    def _publication_data_as_of(self, publication: DatasetPublication | None) -> str | None:
        """返回 publication 冻结的有效日期；缺失时交由 run target 的受理日期补充展示。"""
        return (
            publication.effective_as_of.isoformat()
            if publication and publication.effective_as_of
            else None
        )

    def _latest_error(self, session: Session, dataset_code: str) -> dict[str, Any] | None:
        """返回最近运行的脱敏错误摘要，不读取原始失败证据。"""
        run = session.scalar(
            select(DataOperationRun)
            .where(
                DataOperationRun.dataset_code == dataset_code,
                DataOperationRun.error_json.is_not(None),
            )
            .order_by(DataOperationRun.requested_at.desc())
            .limit(1)
        )
        return run.error_json if run else None

    def _freshness(self, latest_publication: DatasetPublication | None) -> tuple[str, str | None]:
        """按真实消费者 publication 时间计算 freshness，不把任务成功误当作数据发布。"""
        if latest_publication is None:
            return "UNKNOWN", "no-production-publication"
        lag = self._now() - latest_publication.published_at
        if lag <= timedelta(days=1):
            return "FRESH", None
        if lag <= timedelta(days=3):
            return "WARNING", "freshness-warning"
        return "STALE", "freshness-stale"

    def _freshness_lag(self, latest_publication: DatasetPublication | None) -> int:
        """返回最新消费者 publication 到当前的分钟差；未知发布不应调用。"""
        assert latest_publication is not None
        return max(0, int((self._now() - latest_publication.published_at).total_seconds() // 60))

    def _observation_state(
        self, latest: DataOperationRun | None, latest_publication: DatasetPublication | None
    ) -> tuple[str, str | None]:
        """区分真实已发布、合法空集、失败和未同步，避免由前端猜测观测状态。"""
        if latest_publication is not None:
            return "PRESENT", None
        if latest is None:
            return "NOT_YET_SYNCED", None
        if latest.status == "SUCCEEDED" and latest.processed_records == 0:
            return "EMPTY_VALID", "zero-record-publication"
        if latest.status in {"FAILED", "PARTIAL", "INTERRUPTED"}:
            return "UNKNOWN", "latest-run-not-successful"
        return "PRESENT", None

    def _data_as_of(self, run: DataOperationRun | None) -> str | None:
        """从冻结 target 返回服务端确认的数据截至日，不由前端估算。"""
        if run is None:
            return None
        return run.target_json.get("observationDate") or run.target_json.get("dateTo")

    def _target_date(self, run: DataOperationRun | None, field: str) -> str | None:
        """安全读取 target 内指定日期字段。"""
        return run.target_json.get(field) if run else None

    def _not_evaluated_gate(self) -> dict[str, Any]:
        """返回尚未执行发布前质量门的标准摘要。"""
        return {
            "disposition": "NOT_EVALUATED",
            "policyCode": None,
            "policyVersion": None,
            "affectedCount": None,
            "error": None,
        }

    def _passed_gate(self) -> dict[str, Any]:
        """返回允许新 publication 的发布前质量门摘要。"""
        return {
            "disposition": "PASSED",
            "policyCode": "data-operations-default",
            "policyVersion": 1,
            "affectedCount": 0,
            "error": None,
        }

    def _warned_gate(self) -> dict[str, Any]:
        """返回允许发布但需运营关注的质量门摘要，不能与 BLOCKED 混淆。"""
        return {
            "disposition": "WARNED",
            "policyCode": "data-operations-default",
            "policyVersion": 1,
            "affectedCount": 0,
            "error": None,
        }

    def _failed_gate(self, error: dict[str, Any] | None) -> dict[str, Any]:
        """返回阻止新 publication 的质量门失败摘要。"""
        return {
            "disposition": "BLOCKED",
            "policyCode": "data-operations-default",
            "policyVersion": 1,
            "affectedCount": None,
            "error": error,
        }

    def _action_target(self, request: dict[str, Any]) -> dict[str, str]:
        """校验 COMMAND/RUN action target 并规范化 UUID 文本。"""
        target = self._require_dict(request, "target")
        resource_type = self._require_string(target, "resourceType", max_length=24)
        if resource_type not in {"COMMAND", "RUN"}:
            raise OperationProblem(
                status=400, code="validation-error", detail="Action target type is invalid"
            )
        resource_id = self._uuid_field(target, "resourceId")
        return {"resourceType": resource_type, "resourceId": str(resource_id)}

    def _actor(self, request: dict[str, Any]) -> dict[str, str]:
        """校验不透明 actorRef、角色与强制操作原因。"""
        actor = self._require_dict(request, "actor")
        actor_ref = self._require_string(actor, "actorRef", max_length=128)
        role = self._require_string(actor, "role", max_length=24)
        reason = self._require_string(actor, "reason", max_length=500)
        if role not in {"ADMIN", "SUPER_ADMIN", "SYSTEM"}:
            raise OperationProblem(
                status=400, code="validation-error", detail="Actor role is invalid"
            )
        if len(reason) < 2:
            raise OperationProblem(
                status=400, code="validation-error", detail="Operation reason is required"
            )
        return {"actorRef": actor_ref, "role": role, "reason": reason}

    def _uuid_field(self, values: dict[str, Any], key: str) -> UUID:
        """读取必填 UUID 字段并映射格式错误为稳定参数问题。"""
        value = values.get(key)
        try:
            return UUID(str(value))
        except (TypeError, ValueError, AttributeError) as error:
            raise OperationProblem(
                status=400, code="validation-error", detail=f"{key} is invalid"
            ) from error

    def _parse_uuid_or_none(self, value: object) -> UUID | None:
        """解析可空 UUID，非空格式错误必须被拒绝。"""
        if value is None:
            return None
        try:
            return UUID(str(value))
        except (TypeError, ValueError, AttributeError) as error:
            raise OperationProblem(
                status=400, code="validation-error", detail="UUID is invalid"
            ) from error

    def _datetime_or_none(self, value: object, key: str) -> datetime | None:
        """解析合同允许的可空 RFC 3339 时间，并拒绝无时区的本地时间。"""
        if value is None:
            return None
        if not isinstance(value, str):
            raise OperationProblem(status=400, code="validation-error", detail=f"{key} is invalid")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise OperationProblem(
                status=400, code="validation-error", detail=f"{key} is invalid"
            ) from error
        if parsed.tzinfo is None:
            raise OperationProblem(status=400, code="validation-error", detail=f"{key} is invalid")
        return parsed.astimezone(UTC)

    def _require_string(self, values: dict[str, Any], key: str, *, max_length: int) -> str:
        """读取非空受限文本，避免错误消息中回显大输入或机密内容。"""
        value = values.get(key)
        if (
            not isinstance(value, str)
            or not (normalized := value.strip())
            or len(normalized) > max_length
        ):
            raise OperationProblem(status=400, code="validation-error", detail=f"{key} is invalid")
        return normalized

    def _require_list(self, values: dict[str, Any], key: str) -> list[dict[str, Any]]:
        """读取数组请求字段并保留项目顺序。"""
        value = values.get(key)
        if not isinstance(value, list):
            raise OperationProblem(status=400, code="validation-error", detail=f"{key} is invalid")
        return value

    def _require_dict(self, values: dict[str, Any], key: str) -> dict[str, Any]:
        """读取对象请求字段，禁止在接口层接受非结构化标量。"""
        value = values.get(key)
        if not isinstance(value, dict):
            raise OperationProblem(status=400, code="validation-error", detail=f"{key} is invalid")
        return value

    def _optional_text(self, value: object) -> str | None:
        """规范化可空短文本筛选条件。"""
        if value is None:
            return None
        if not isinstance(value, str) or not (normalized := value.strip()):
            raise OperationProblem(status=400, code="validation-error", detail="Query is invalid")
        return normalized

    def _validate_limit(self, value: object, *, maximum: int) -> int:
        """校验各分页端点统一的正整数上限。"""
        if not isinstance(value, int) or not 1 <= value <= maximum:
            raise OperationProblem(
                status=400, code="validation-error", detail="Page limit is invalid"
            )
        return value

    def _require_idempotency_key(self, value: str) -> None:
        """校验写请求内部幂等键长度；服务端不接受空或超长键。"""
        if not 16 <= len(value) <= 128:
            raise OperationProblem(
                status=400, code="validation-error", detail="Idempotency key is invalid"
            )

    def _hash(self, value: object) -> str:
        """生成排序 JSON 的 SHA-256，供预检与幂等冲突比较。"""
        return hashlib.sha256(
            json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
        ).hexdigest()

    def _encode_offset(self, offset: int) -> str:
        """生成无业务字段泄漏的短分页游标。"""
        return base64.urlsafe_b64encode(str(offset).encode()).decode().rstrip("=")

    def _decode_offset(self, cursor: object) -> int:
        """解析独立 cursor；损坏游标拒绝而不是静默从首页重放。"""
        if cursor is None:
            return 0
        if not isinstance(cursor, str) or len(cursor) > 1024:
            raise OperationProblem(status=400, code="validation-error", detail="Cursor is invalid")
        try:
            padding = "=" * (-len(cursor) % 4)
            value = int(base64.urlsafe_b64decode(cursor + padding).decode())
        except (ValueError, UnicodeDecodeError) as error:
            raise OperationProblem(
                status=400, code="validation-error", detail="Cursor is invalid"
            ) from error
        if value < 0:
            raise OperationProblem(status=400, code="validation-error", detail="Cursor is invalid")
        return value

    def _error(self, code: str, stage: str, retryable: bool, message: str) -> dict[str, Any]:
        """构造合同允许跨服务传递的脱敏错误摘要。"""
        return {"code": code, "stage": stage, "retryable": retryable, "message": message[:500]}

    def _iso(self, value: datetime | None) -> str | None:
        """将可空 UTC 时间转换为 RFC 3339 文本。"""
        return value.isoformat().replace("+00:00", "Z") if value is not None else None

    def _uuid_text(self, value: UUID | None) -> str | None:
        """将可空 UUID 投影为合同字符串或 null。"""
        return str(value) if value is not None else None
