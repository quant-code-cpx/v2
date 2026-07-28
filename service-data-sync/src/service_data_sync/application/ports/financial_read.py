"""已发布财务数据的 `provider-neutral` 内部读取端口。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Literal, Protocol
from uuid import UUID

from service_data_sync.domain.equity import Exchange

# 定义财务发布选择器允许的受控能力，不允许调用方传入任意数据集名称。
FinancialCapability = Literal[
    "financial.report",
    "financial.provider-metric",
    "financial.derived-metric",
    "financial.valuation",
]


class FinancialReadUnavailable(RuntimeError):
    """表示生产财务发布存储暂时不可读，调用方不能安全降级到 `research` 数据。"""


@dataclass(frozen=True, slots=True)
class FinancialPublicationSnapshot:
    """描述一个证券、能力与方法学组合当前可读取的不可变发布版本。"""

    data_version: UUID
    security_id: int
    instrument_id: UUID
    methodology_id: UUID
    capability: FinancialCapability
    methodology_code: str
    methodology_version: int
    source_code: str
    published_at: datetime
    effective_as_of: date
    knowledge_cutoff: datetime
    row_count: int
    content_sha256: str


@dataclass(frozen=True, slots=True)
class PublishedFinancialReport:
    """描述一个已发布双时态报表头，不包含可能很宽的行项目内容。"""

    report_ref: UUID
    statement_type: str
    report_period: date
    period_basis: str
    statement_scope: str
    currency: str | None
    currency_null_reason: str | None
    report_type: str
    audit_status: str
    announcement_date: date | None
    provider_update_at: datetime | None
    effective_from: date
    effective_to: date | None
    known_from: datetime
    known_to: datetime | None
    knowledge_basis: str
    knowledge_confidence: str
    observed_at: datetime
    revision: int
    quality_status: str


@dataclass(frozen=True, slots=True)
class PublishedFinancialReportDetail:
    """描述一个在指定双时态视图中可读取的报表头及其内部 revision 身份。"""

    report: PublishedFinancialReport
    revision_id: UUID


@dataclass(frozen=True, slots=True)
class PublishedFinancialStatementFact:
    """描述一个已治理行项目，保留精确数值、空值、单位与符号语义。"""

    metric_code: str
    label: str
    value: Decimal | None
    null_reason: str | None
    currency: str | None
    currency_null_reason: str | None
    original_unit: str
    canonical_unit: str
    scale_factor: Decimal
    sign_convention: str


@dataclass(frozen=True, slots=True)
class PublishedFinancialMetric:
    """描述一个在生产 publication 范围内可读取的供应商或平台财务指标 revision。"""

    metric_code: str
    label: str
    origin: str
    report_period: date
    period_basis: str
    statement_scope: str
    value: Decimal
    unit: str
    currency: str | None
    currency_null_reason: str | None
    formula_version: int | None
    effective_from: date
    known_from: datetime
    knowledge_basis: str
    knowledge_confidence: str
    observed_at: datetime
    revision: int


@dataclass(frozen=True, slots=True)
class PublishedValuationObservation:
    """描述一个在生产 publication 范围内可读取的日频供应商估值观察 revision。"""

    observation_date: date
    metric_code: str
    value: Decimal
    unit: str
    currency: str | None
    currency_null_reason: str | None
    finality: str
    effective_from: date
    known_from: datetime
    knowledge_basis: str
    knowledge_confidence: str
    observed_at: datetime
    revision: int


class FinancialReadRepository(Protocol):
    """只解析生产发布快照，不允许接口层绕过它直读 `canonical` 修订。"""

    def get_current_publication(
        self,
        *,
        exchange: Exchange,
        symbol: str,
        capability: FinancialCapability,
        methodology_code: str,
        methodology_version: int,
    ) -> FinancialPublicationSnapshot | None:
        """返回精确证券、能力和已验证方法学的当前发布；不存在时返回空值。"""
        ...

    def list_reports(
        self,
        *,
        publication: FinancialPublicationSnapshot,
        as_of: date,
        known_at: datetime,
        statement_types: tuple[str, ...],
        period_bases: tuple[str, ...],
        statement_scope: str | None,
        report_period_from: date | None,
        report_period_to: date | None,
        after_report_period: date | None,
        after_statement_type: str | None,
        after_report_ref: UUID | None,
        limit: int,
    ) -> tuple[PublishedFinancialReport, ...]:
        """按报告期倒序读取已发布报表头，并使用完整复合键稳定续页。"""
        ...

    def get_current_report_publication(
        self,
        *,
        exchange: Exchange,
        symbol: str,
        report_ref: UUID,
    ) -> FinancialPublicationSnapshot | None:
        """返回该公开报表引用所属的当前已验证生产发布；未发布时返回空值。"""
        ...

    def get_report_detail(
        self,
        *,
        publication: FinancialPublicationSnapshot,
        report_ref: UUID,
        as_of: date,
        known_at: datetime,
    ) -> PublishedFinancialReportDetail | None:
        """读取在双时态视图可见的唯一报表 revision；不存在或不可见时返回空值。"""
        ...

    def list_report_facts(
        self,
        *,
        detail: PublishedFinancialReportDetail,
        metric_codes: tuple[str, ...],
        after_metric_code: str | None,
        limit: int,
    ) -> tuple[PublishedFinancialStatementFact, ...]:
        """按字段代码升序读取已治理行项目，并以字段代码执行稳定续页。"""
        ...

    def list_provider_metrics(
        self,
        *,
        publication: FinancialPublicationSnapshot,
        as_of: date,
        known_at: datetime,
        metric_codes: tuple[str, ...],
        period_bases: tuple[str, ...],
        report_period_from: date | None,
        report_period_to: date | None,
        after_report_period: date | None,
        after_metric_code: str | None,
        limit: int,
    ) -> tuple[PublishedFinancialMetric, ...]:
        """按报告期和字段代码升序读取供应商直接指标的可见 revision。"""
        ...

    def list_derived_metrics(
        self,
        *,
        publication: FinancialPublicationSnapshot,
        as_of: date,
        known_at: datetime,
        metric_codes: tuple[str, ...],
        period_bases: tuple[str, ...],
        report_period_from: date | None,
        report_period_to: date | None,
        after_report_period: date | None,
        after_metric_code: str | None,
        limit: int,
    ) -> tuple[PublishedFinancialMetric, ...]:
        """按报告期和字段代码升序读取平台派生指标的可见 revision。"""
        ...

    def list_valuations(
        self,
        *,
        publication: FinancialPublicationSnapshot,
        as_of: date,
        known_at: datetime,
        metric_codes: tuple[str, ...],
        start: date,
        end: date,
        after_observation_date: date | None,
        after_metric_code: str | None,
        limit: int,
    ) -> tuple[PublishedValuationObservation, ...]:
        """按日期和字段代码升序读取生产发布范围内的估值观察 revision。"""
        ...
