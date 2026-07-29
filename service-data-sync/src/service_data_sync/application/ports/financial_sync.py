"""财务来源批次归一化、双时态修订和消费者发布的持久化端口。

三表、指标和估值各自保持报告期事实与观测时间。
只有满足数据集自身质量门的 `revision` 才能形成消费者可见版本。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Literal, Protocol
from uuid import UUID

from service_data_sync.domain.equity import Exchange

FinancialCapability = Literal[
    "financial.report",
    "financial.provider-metric",
    "financial.valuation",
]
StatementType = Literal["BALANCE_SHEET", "INCOME_STATEMENT", "CASH_FLOW_STATEMENT"]
PeriodBasis = Literal["POINT_IN_TIME", "YEAR_TO_DATE", "SINGLE_QUARTER", "TTM"]
StatementScope = Literal["CONSOLIDATED", "PARENT", "UNKNOWN"]


@dataclass(frozen=True, slots=True)
class FinancialSourceObservation:
    """描述一份已归档 raw evidence 的来源观察及其 adapter 血缘。"""

    provider_id: str
    capability: str
    source_payload_sha256: str
    raw_uri: str
    observed_at: datetime
    upstream_source: str
    adapter_version: str
    schema_fingerprint: str


@dataclass(frozen=True, slots=True)
class FinancialFactInput:
    """描述一条可写入报表 revision 的受控行项目，空值永不以零代替。"""

    code: str
    label: str
    value: Decimal | None
    null_reason: str | None
    value_domain: str
    original_unit: str
    canonical_unit: str
    scale_factor: Decimal
    sign_convention: str
    currency: str | None
    currency_null_reason: str | None


@dataclass(frozen=True, slots=True)
class FinancialReportInput:
    """描述一个报表逻辑身份及其本次观察到的完整行项目集合。"""

    statement_type: StatementType
    report_period: date
    period_basis: PeriodBasis
    statement_scope: StatementScope
    currency: str | None
    currency_null_reason: str | None
    report_type: str
    announcement_date: date | None
    provider_update_at: datetime | None
    audit_status: str
    facts: tuple[FinancialFactInput, ...]


@dataclass(frozen=True, slots=True)
class FinancialMetricInput:
    """描述一条供应商报告期指标；它独立于三表披露事实保存和发布。"""

    code: str
    label: str
    report_period: date
    period_basis: PeriodBasis
    statement_scope: StatementScope
    value: Decimal
    value_domain: str
    unit: str
    currency: str | None
    currency_null_reason: str | None


@dataclass(frozen=True, slots=True)
class FinancialValuationInput:
    """描述一条日频供应商估值观察，不宣称其为交易所最终值。"""

    code: str
    label: str
    observation_date: date
    value: Decimal
    value_domain: str
    unit: str
    currency: str | None
    currency_null_reason: str | None


@dataclass(frozen=True, slots=True)
class FinancialPublicationResult:
    """描述一次能力写入的当前消费者版本及本次新增和未变化数量。"""

    capability: FinancialCapability
    data_version: UUID
    inserted_count: int
    unchanged_count: int


class FinancialSyncRepository(Protocol):
    """负责财务字典、canonical revision、质量证据、checkpoint 和 publication 的原子写入。"""

    def publish_reports(
        self,
        *,
        exchange: Exchange,
        symbol: str,
        reports: Sequence[FinancialReportInput],
        source: FinancialSourceObservation,
    ) -> FinancialPublicationResult:
        """追加三表变化 revision，并推进该证券报表能力的当前发布。"""
        ...

    def publish_provider_metrics(
        self,
        *,
        exchange: Exchange,
        symbol: str,
        metrics: Sequence[FinancialMetricInput],
        source: FinancialSourceObservation,
    ) -> FinancialPublicationResult:
        """追加供应商指标 revision，并推进独立于报表的指标发布。"""
        ...

    def publish_valuations(
        self,
        *,
        exchange: Exchange,
        symbol: str,
        valuations: Sequence[FinancialValuationInput],
        source: FinancialSourceObservation,
    ) -> FinancialPublicationResult:
        """追加日频估值观察 revision，并推进独立估值能力发布。"""
        ...
