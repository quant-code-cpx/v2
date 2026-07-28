"""已发布财务快照的 `SQLAlchemy` 选择器。"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal
from typing import cast
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import SQLAlchemyError

from service_data_sync.application.ports.financial_read import (
    FinancialCapability,
    FinancialPublicationSnapshot,
    FinancialReadRepository,
    FinancialReadUnavailable,
    PublishedFinancialMetric,
    PublishedFinancialReport,
    PublishedFinancialReportDetail,
    PublishedFinancialStatementFact,
    PublishedValuationObservation,
)
from service_data_sync.domain.equity import Exchange
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.database.models.equity.identity import (
    equity_identifier_version,
)
from service_data_sync.infrastructure.database.models.equity.identity.equity_instrument import (
    EquityInstrument,
)
from service_data_sync.infrastructure.database.models.financial import (
    derived_financial_metric_revision,
    provider_financial_metric_revision,
    valuation_observation_revision,
)
from service_data_sync.infrastructure.database.models.financial.financial_methodology import (
    FinancialMethodology,
)
from service_data_sync.infrastructure.database.models.financial.financial_metric_definition import (
    FinancialMetricDefinition,
)
from service_data_sync.infrastructure.database.models.financial.financial_publication import (
    FinancialPublication,
)
from service_data_sync.infrastructure.database.models.financial.financial_report import (
    FinancialReport,
)
from service_data_sync.infrastructure.database.models.financial.financial_report_revision import (
    FinancialReportRevision,
)
from service_data_sync.infrastructure.database.models.financial.financial_statement_fact import (
    FinancialStatementFact,
)
from service_data_sync.infrastructure.database.models.publication.dataset_publication import (
    DatasetPublication,
)

ProviderFinancialMetricRevision = provider_financial_metric_revision.ProviderFinancialMetricRevision
DerivedFinancialMetricRevision = derived_financial_metric_revision.DerivedFinancialMetricRevision
ValuationObservationRevision = valuation_observation_revision.ValuationObservationRevision
EquityIdentifierVersion = equity_identifier_version.EquityIdentifierVersion


class SqlAlchemyFinancialReadRepository(FinancialReadRepository):
    """只选择未被替代且方法学已验证的财务生产发布，不读取未发布 `revision`。"""

    def __init__(self, database: DatabaseClient) -> None:
        """保存服务自有数据库会话工厂，不向 `application` 层泄漏 `ORM` 对象。"""
        self._database = database

    def get_current_publication(
        self,
        *,
        exchange: Exchange,
        symbol: str,
        capability: FinancialCapability,
        methodology_code: str,
        methodology_version: int,
    ) -> FinancialPublicationSnapshot | None:
        """用完整 `publication` 身份选择唯一当前版本，多个候选时宁可失败也不猜测。"""
        statement = (
            select(
                FinancialPublication.data_version,
                FinancialPublication.security_id,
                EquityInstrument.instrument_id,
                FinancialPublication.methodology_id,
                FinancialPublication.capability,
                FinancialMethodology.code.label("methodology_code"),
                FinancialMethodology.version.label("methodology_version"),
                FinancialMethodology.source_code,
                FinancialPublication.published_at,
                FinancialPublication.effective_as_of,
                FinancialPublication.knowledge_cutoff,
                FinancialPublication.row_count,
                FinancialPublication.content_sha256,
            )
            .select_from(FinancialPublication)
            .join(
                DatasetPublication,
                DatasetPublication.data_version == FinancialPublication.data_version,
            )
            .join(
                FinancialMethodology,
                FinancialMethodology.methodology_id == FinancialPublication.methodology_id,
            )
            .join(
                EquityInstrument,
                EquityInstrument.security_id == FinancialPublication.security_id,
            )
            .join(
                EquityIdentifierVersion,
                and_(
                    EquityIdentifierVersion.security_id == FinancialPublication.security_id,
                    EquityIdentifierVersion.exchange == exchange.value,
                    EquityIdentifierVersion.symbol == symbol,
                    EquityIdentifierVersion.identity_state == "CONFIRMED",
                    EquityIdentifierVersion.effective_range.op("@>")(
                        FinancialPublication.effective_as_of
                    ),
                    EquityIdentifierVersion.knowledge_range.op("@>")(
                        FinancialPublication.knowledge_cutoff
                    ),
                ),
            )
            .where(
                FinancialPublication.capability == capability,
                FinancialMethodology.code == methodology_code,
                FinancialMethodology.version == methodology_version,
                # 已退役或仅草拟的方法学即使遗留发布行也不能对消费者继续可见。
                FinancialMethodology.status == "validated",
                # `dataset_publication` 是 data_version 的通用当前指针；被替代版本不能选中。
                DatasetPublication.superseded_at.is_(None),
            )
        )
        try:
            with self._database.session() as connection:
                row = connection.execute(statement).mappings().one_or_none()
        except SQLAlchemyError as error:
            raise FinancialReadUnavailable("financial publication is unavailable") from error
        if row is None:
            return None
        return _financial_publication_snapshot(dict(row))

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
        """按已冻结发布截点查询报表头，并拒绝跨筛选条件或版本的续页位置。"""
        _validate_report_cursor(
            after_report_period=after_report_period,
            after_statement_type=after_statement_type,
            after_report_ref=after_report_ref,
        )
        if not 1 <= limit <= 51:
            raise ValueError("limit must be from 1 to 51")
        statement = (
            select(
                FinancialReport.report_ref,
                FinancialReport.statement_type,
                FinancialReport.report_period,
                FinancialReport.period_basis,
                FinancialReport.statement_scope,
                FinancialReport.currency,
                FinancialReport.currency_null_reason,
                FinancialReport.report_type,
                FinancialReportRevision.audit_status,
                FinancialReportRevision.announcement_date,
                FinancialReportRevision.provider_update_at,
                FinancialReportRevision.effective_from,
                FinancialReportRevision.effective_to,
                FinancialReportRevision.known_from,
                FinancialReportRevision.known_to,
                FinancialReportRevision.knowledge_basis,
                FinancialReportRevision.knowledge_confidence,
                FinancialReportRevision.observed_at,
                FinancialReportRevision.revision,
                FinancialReportRevision.quality_status,
            )
            .select_from(FinancialReport)
            .join(
                FinancialReportRevision,
                and_(
                    FinancialReportRevision.financial_report_id
                    == FinancialReport.financial_report_id,
                    # 两表相同的报告期同时保证逻辑身份和物理分区行不会意外交叉。
                    FinancialReportRevision.report_period == FinancialReport.report_period,
                ),
            )
            .where(
                FinancialReport.security_id == publication.security_id,
                FinancialReport.methodology_id == publication.methodology_id,
                FinancialReportRevision.quality_status.in_(("passed", "warned")),
                FinancialReportRevision.effective_from <= as_of,
                or_(
                    FinancialReportRevision.effective_to.is_(None),
                    FinancialReportRevision.effective_to > as_of,
                ),
                FinancialReportRevision.known_from <= known_at,
                or_(
                    FinancialReportRevision.known_to.is_(None),
                    FinancialReportRevision.known_to > known_at,
                ),
            )
        )
        if statement_types:
            statement = statement.where(FinancialReport.statement_type.in_(statement_types))
        if period_bases:
            statement = statement.where(FinancialReport.period_basis.in_(period_bases))
        if statement_scope is not None:
            statement = statement.where(FinancialReport.statement_scope == statement_scope)
        if report_period_from is not None:
            statement = statement.where(FinancialReport.report_period >= report_period_from)
        if report_period_to is not None:
            statement = statement.where(FinancialReport.report_period <= report_period_to)
        if after_report_period is not None:
            assert after_statement_type is not None
            assert after_report_ref is not None
            statement = statement.where(
                or_(
                    FinancialReport.report_period < after_report_period,
                    and_(
                        FinancialReport.report_period == after_report_period,
                        FinancialReport.statement_type > after_statement_type,
                    ),
                    and_(
                        FinancialReport.report_period == after_report_period,
                        FinancialReport.statement_type == after_statement_type,
                        FinancialReport.report_ref > after_report_ref,
                    ),
                )
            )
        statement = statement.order_by(
            FinancialReport.report_period.desc(),
            FinancialReport.statement_type,
            FinancialReport.report_ref,
        ).limit(limit)
        try:
            with self._database.session() as connection:
                rows = connection.execute(statement).mappings().all()
        except SQLAlchemyError as error:
            raise FinancialReadUnavailable("financial reports are unavailable") from error
        return tuple(_published_financial_report(dict(row)) for row in rows)

    def get_current_report_publication(
        self,
        *,
        exchange: Exchange,
        symbol: str,
        report_ref: UUID,
    ) -> FinancialPublicationSnapshot | None:
        """解析报表公开引用所属的当前生产发布，不允许由调用方猜测方法学版本。"""
        statement = (
            select(
                FinancialPublication.data_version,
                FinancialPublication.security_id,
                EquityInstrument.instrument_id,
                FinancialPublication.methodology_id,
                FinancialPublication.capability,
                FinancialMethodology.code.label("methodology_code"),
                FinancialMethodology.version.label("methodology_version"),
                FinancialMethodology.source_code,
                FinancialPublication.published_at,
                FinancialPublication.effective_as_of,
                FinancialPublication.knowledge_cutoff,
                FinancialPublication.row_count,
                FinancialPublication.content_sha256,
            )
            .select_from(FinancialPublication)
            .join(
                DatasetPublication,
                DatasetPublication.data_version == FinancialPublication.data_version,
            )
            .join(
                FinancialMethodology,
                FinancialMethodology.methodology_id == FinancialPublication.methodology_id,
            )
            .join(
                EquityInstrument,
                EquityInstrument.security_id == FinancialPublication.security_id,
            )
            .join(
                EquityIdentifierVersion,
                and_(
                    EquityIdentifierVersion.security_id == FinancialPublication.security_id,
                    EquityIdentifierVersion.exchange == exchange.value,
                    EquityIdentifierVersion.symbol == symbol,
                    EquityIdentifierVersion.identity_state == "CONFIRMED",
                    EquityIdentifierVersion.effective_range.op("@>")(
                        FinancialPublication.effective_as_of
                    ),
                    EquityIdentifierVersion.knowledge_range.op("@>")(
                        FinancialPublication.knowledge_cutoff
                    ),
                ),
            )
            .join(
                FinancialReport,
                and_(
                    FinancialReport.security_id == FinancialPublication.security_id,
                    FinancialReport.methodology_id == FinancialPublication.methodology_id,
                ),
            )
            .where(
                FinancialReport.report_ref == report_ref,
                FinancialPublication.capability == "financial.report",
                FinancialMethodology.status == "validated",
                DatasetPublication.superseded_at.is_(None),
            )
        )
        try:
            with self._database.session() as connection:
                row = connection.execute(statement).mappings().one_or_none()
        except SQLAlchemyError as error:
            raise FinancialReadUnavailable("financial report publication is unavailable") from error
        return None if row is None else _financial_publication_snapshot(dict(row))

    def get_report_detail(
        self,
        *,
        publication: FinancialPublicationSnapshot,
        report_ref: UUID,
        as_of: date,
        known_at: datetime,
    ) -> PublishedFinancialReportDetail | None:
        """选择唯一可见 revision；隔离、过期或不属当前发布的报表不会对消费者可见。"""
        statement = (
            select(
                FinancialReport.report_ref,
                FinancialReport.statement_type,
                FinancialReport.report_period,
                FinancialReport.period_basis,
                FinancialReport.statement_scope,
                FinancialReport.currency,
                FinancialReport.currency_null_reason,
                FinancialReport.report_type,
                FinancialReportRevision.revision_id,
                FinancialReportRevision.audit_status,
                FinancialReportRevision.announcement_date,
                FinancialReportRevision.provider_update_at,
                FinancialReportRevision.effective_from,
                FinancialReportRevision.effective_to,
                FinancialReportRevision.known_from,
                FinancialReportRevision.known_to,
                FinancialReportRevision.knowledge_basis,
                FinancialReportRevision.knowledge_confidence,
                FinancialReportRevision.observed_at,
                FinancialReportRevision.revision,
                FinancialReportRevision.quality_status,
            )
            .select_from(FinancialReport)
            .join(
                FinancialReportRevision,
                and_(
                    FinancialReportRevision.financial_report_id
                    == FinancialReport.financial_report_id,
                    FinancialReportRevision.report_period == FinancialReport.report_period,
                ),
            )
            .where(
                FinancialReport.report_ref == report_ref,
                FinancialReport.security_id == publication.security_id,
                FinancialReport.methodology_id == publication.methodology_id,
                FinancialReportRevision.quality_status.in_(("passed", "warned")),
                FinancialReportRevision.effective_from <= as_of,
                or_(
                    FinancialReportRevision.effective_to.is_(None),
                    FinancialReportRevision.effective_to > as_of,
                ),
                FinancialReportRevision.known_from <= known_at,
                or_(
                    FinancialReportRevision.known_to.is_(None),
                    FinancialReportRevision.known_to > known_at,
                ),
            )
        )
        try:
            with self._database.session() as connection:
                row = connection.execute(statement).mappings().one_or_none()
        except SQLAlchemyError as error:
            raise FinancialReadUnavailable("financial report detail is unavailable") from error
        if row is None:
            return None
        return PublishedFinancialReportDetail(
            report=_published_financial_report(dict(row)),
            revision_id=UUID(str(row["revision_id"])),
        )

    def list_report_facts(
        self,
        *,
        detail: PublishedFinancialReportDetail,
        metric_codes: tuple[str, ...],
        after_metric_code: str | None,
        limit: int,
    ) -> tuple[PublishedFinancialStatementFact, ...]:
        """仅读取 active 字典认可的 statement_fact，拒绝退役、未知或跨表行项目。"""
        if not 1 <= limit <= 201:
            raise ValueError("limit must be from 1 to 201")
        statement = (
            select(
                FinancialMetricDefinition.code.label("metric_code"),
                FinancialMetricDefinition.label,
                FinancialStatementFact.value,
                FinancialStatementFact.null_reason,
                FinancialStatementFact.currency,
                FinancialStatementFact.currency_null_reason,
                FinancialStatementFact.original_unit,
                FinancialStatementFact.canonical_unit,
                FinancialStatementFact.scale_factor,
                FinancialStatementFact.sign_convention,
            )
            .select_from(FinancialStatementFact)
            .join(
                FinancialMetricDefinition,
                FinancialMetricDefinition.metric_id == FinancialStatementFact.metric_id,
            )
            .where(
                FinancialStatementFact.report_period == detail.report.report_period,
                FinancialStatementFact.revision_id == detail.revision_id,
                FinancialMetricDefinition.origin == "statement_fact",
                FinancialMetricDefinition.status == "active",
                FinancialMetricDefinition.statement_type == detail.report.statement_type,
            )
        )
        if metric_codes:
            statement = statement.where(FinancialMetricDefinition.code.in_(metric_codes))
        if after_metric_code is not None:
            statement = statement.where(FinancialMetricDefinition.code > after_metric_code)
        statement = statement.order_by(FinancialMetricDefinition.code).limit(limit)
        try:
            with self._database.session() as connection:
                rows = connection.execute(statement).mappings().all()
        except SQLAlchemyError as error:
            raise FinancialReadUnavailable("financial statement facts are unavailable") from error
        return tuple(_published_financial_statement_fact(dict(row)) for row in rows)

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
        """读取供应商直接指标的生产双时态视图；平台派生指标另有独立 revision 表。"""
        if not 1 <= limit <= 501:
            raise ValueError("limit must be from 1 to 501")
        if (after_report_period is None) != (after_metric_code is None):
            raise ValueError("metric cursor keys must be supplied together")
        statement = (
            select(
                FinancialMetricDefinition.code.label("metric_code"),
                FinancialMetricDefinition.label,
                ProviderFinancialMetricRevision.report_period,
                ProviderFinancialMetricRevision.period_basis,
                ProviderFinancialMetricRevision.statement_scope,
                ProviderFinancialMetricRevision.value,
                ProviderFinancialMetricRevision.unit,
                ProviderFinancialMetricRevision.currency,
                ProviderFinancialMetricRevision.currency_null_reason,
                ProviderFinancialMetricRevision.effective_from,
                ProviderFinancialMetricRevision.known_from,
                ProviderFinancialMetricRevision.knowledge_basis,
                ProviderFinancialMetricRevision.knowledge_confidence,
                ProviderFinancialMetricRevision.observed_at,
                ProviderFinancialMetricRevision.revision,
            )
            .select_from(ProviderFinancialMetricRevision)
            .join(
                FinancialMetricDefinition,
                FinancialMetricDefinition.metric_id == ProviderFinancialMetricRevision.metric_id,
            )
            .where(
                ProviderFinancialMetricRevision.security_id == publication.security_id,
                ProviderFinancialMetricRevision.methodology_id == publication.methodology_id,
                FinancialMetricDefinition.origin == "provider_reported",
                FinancialMetricDefinition.status == "active",
                ProviderFinancialMetricRevision.quality_status.in_(("passed", "warned")),
                ProviderFinancialMetricRevision.effective_from <= as_of,
                or_(
                    ProviderFinancialMetricRevision.effective_to.is_(None),
                    ProviderFinancialMetricRevision.effective_to > as_of,
                ),
                ProviderFinancialMetricRevision.known_from <= known_at,
                or_(
                    ProviderFinancialMetricRevision.known_to.is_(None),
                    ProviderFinancialMetricRevision.known_to > known_at,
                ),
            )
        )
        if metric_codes:
            statement = statement.where(FinancialMetricDefinition.code.in_(metric_codes))
        if period_bases:
            statement = statement.where(
                ProviderFinancialMetricRevision.period_basis.in_(period_bases)
            )
        if report_period_from is not None:
            statement = statement.where(
                ProviderFinancialMetricRevision.report_period >= report_period_from
            )
        if report_period_to is not None:
            statement = statement.where(
                ProviderFinancialMetricRevision.report_period <= report_period_to
            )
        if after_report_period is not None and after_metric_code is not None:
            statement = statement.where(
                or_(
                    ProviderFinancialMetricRevision.report_period > after_report_period,
                    and_(
                        ProviderFinancialMetricRevision.report_period == after_report_period,
                        FinancialMetricDefinition.code > after_metric_code,
                    ),
                )
            )
        statement = statement.order_by(
            ProviderFinancialMetricRevision.report_period, FinancialMetricDefinition.code
        ).limit(limit)
        try:
            with self._database.session() as connection:
                rows = connection.execute(statement).mappings().all()
        except SQLAlchemyError as error:
            raise FinancialReadUnavailable("financial provider metrics are unavailable") from error
        return tuple(_published_financial_metric(dict(row)) for row in rows)

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
        """读取平台公式结果的生产双时态视图，且只接受独立派生 publication。"""
        if publication.capability != "financial.derived-metric":
            raise ValueError("derived metric read requires a derived publication")
        if not 1 <= limit <= 501:
            raise ValueError("limit must be from 1 to 501")
        if (after_report_period is None) != (after_metric_code is None):
            raise ValueError("metric cursor keys must be supplied together")
        statement = (
            select(
                FinancialMetricDefinition.code.label("metric_code"),
                FinancialMetricDefinition.label,
                DerivedFinancialMetricRevision.report_period,
                DerivedFinancialMetricRevision.period_basis,
                DerivedFinancialMetricRevision.statement_scope,
                DerivedFinancialMetricRevision.value,
                DerivedFinancialMetricRevision.unit,
                DerivedFinancialMetricRevision.currency,
                DerivedFinancialMetricRevision.currency_null_reason,
                DerivedFinancialMetricRevision.formula_version,
                DerivedFinancialMetricRevision.effective_from,
                DerivedFinancialMetricRevision.known_from,
                DerivedFinancialMetricRevision.knowledge_basis,
                DerivedFinancialMetricRevision.knowledge_confidence,
                DerivedFinancialMetricRevision.observed_at,
                DerivedFinancialMetricRevision.revision,
            )
            .select_from(DerivedFinancialMetricRevision)
            .join(
                FinancialMetricDefinition,
                FinancialMetricDefinition.metric_id == DerivedFinancialMetricRevision.metric_id,
            )
            .where(
                DerivedFinancialMetricRevision.security_id == publication.security_id,
                DerivedFinancialMetricRevision.methodology_id == publication.methodology_id,
                FinancialMetricDefinition.origin == "platform_derived",
                FinancialMetricDefinition.status == "active",
                DerivedFinancialMetricRevision.quality_status.in_(("passed", "warned")),
                DerivedFinancialMetricRevision.effective_from <= as_of,
                or_(
                    DerivedFinancialMetricRevision.effective_to.is_(None),
                    DerivedFinancialMetricRevision.effective_to > as_of,
                ),
                DerivedFinancialMetricRevision.known_from <= known_at,
                or_(
                    DerivedFinancialMetricRevision.known_to.is_(None),
                    DerivedFinancialMetricRevision.known_to > known_at,
                ),
            )
        )
        if metric_codes:
            statement = statement.where(FinancialMetricDefinition.code.in_(metric_codes))
        if period_bases:
            statement = statement.where(
                DerivedFinancialMetricRevision.period_basis.in_(period_bases)
            )
        if report_period_from is not None:
            statement = statement.where(
                DerivedFinancialMetricRevision.report_period >= report_period_from
            )
        if report_period_to is not None:
            statement = statement.where(
                DerivedFinancialMetricRevision.report_period <= report_period_to
            )
        if after_report_period is not None and after_metric_code is not None:
            statement = statement.where(
                or_(
                    DerivedFinancialMetricRevision.report_period > after_report_period,
                    and_(
                        DerivedFinancialMetricRevision.report_period == after_report_period,
                        FinancialMetricDefinition.code > after_metric_code,
                    ),
                )
            )
        statement = statement.order_by(
            DerivedFinancialMetricRevision.report_period,
            FinancialMetricDefinition.code,
        ).limit(limit)
        try:
            with self._database.session() as connection:
                rows = connection.execute(statement).mappings().all()
        except SQLAlchemyError as error:
            raise FinancialReadUnavailable("financial derived metrics are unavailable") from error
        return tuple(_published_derived_financial_metric(dict(row)) for row in rows)

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
        """读取日频供应商估值观察的生产双时态视图，保留其非最终态标记。"""
        if not 1 <= limit <= 1001:
            raise ValueError("limit must be from 1 to 1001")
        if (after_observation_date is None) != (after_metric_code is None):
            raise ValueError("valuation cursor keys must be supplied together")
        statement = (
            select(
                ValuationObservationRevision.observation_date,
                FinancialMetricDefinition.code.label("metric_code"),
                ValuationObservationRevision.value,
                ValuationObservationRevision.unit,
                ValuationObservationRevision.currency,
                ValuationObservationRevision.currency_null_reason,
                ValuationObservationRevision.finality,
                ValuationObservationRevision.effective_from,
                ValuationObservationRevision.known_from,
                ValuationObservationRevision.knowledge_basis,
                ValuationObservationRevision.knowledge_confidence,
                ValuationObservationRevision.observed_at,
                ValuationObservationRevision.revision,
            )
            .select_from(ValuationObservationRevision)
            .join(
                FinancialMetricDefinition,
                FinancialMetricDefinition.metric_id == ValuationObservationRevision.metric_id,
            )
            .where(
                ValuationObservationRevision.security_id == publication.security_id,
                ValuationObservationRevision.methodology_id == publication.methodology_id,
                FinancialMetricDefinition.origin == "valuation",
                FinancialMetricDefinition.status == "active",
                ValuationObservationRevision.quality_status.in_(("passed", "warned")),
                ValuationObservationRevision.observation_date >= start,
                ValuationObservationRevision.observation_date <= end,
                ValuationObservationRevision.effective_from <= as_of,
                or_(
                    ValuationObservationRevision.effective_to.is_(None),
                    ValuationObservationRevision.effective_to > as_of,
                ),
                ValuationObservationRevision.known_from <= known_at,
                or_(
                    ValuationObservationRevision.known_to.is_(None),
                    ValuationObservationRevision.known_to > known_at,
                ),
            )
        )
        if metric_codes:
            statement = statement.where(FinancialMetricDefinition.code.in_(metric_codes))
        if after_observation_date is not None and after_metric_code is not None:
            statement = statement.where(
                or_(
                    ValuationObservationRevision.observation_date > after_observation_date,
                    and_(
                        ValuationObservationRevision.observation_date == after_observation_date,
                        FinancialMetricDefinition.code > after_metric_code,
                    ),
                )
            )
        statement = statement.order_by(
            ValuationObservationRevision.observation_date, FinancialMetricDefinition.code
        ).limit(limit)
        try:
            with self._database.session() as connection:
                rows = connection.execute(statement).mappings().all()
        except SQLAlchemyError as error:
            raise FinancialReadUnavailable("financial valuations are unavailable") from error
        return tuple(_published_valuation_observation(dict(row)) for row in rows)


def _validate_report_cursor(
    *,
    after_report_period: date | None,
    after_statement_type: str | None,
    after_report_ref: UUID | None,
) -> None:
    """要求报表续页复合排序键要么完整存在，要么全部缺失。"""
    values = (after_report_period, after_statement_type, after_report_ref)
    if any(value is not None for value in values) and not all(
        value is not None for value in values
    ):
        raise ValueError("report cursor keys must be supplied together")


def _published_financial_report(row: Mapping[str, object]) -> PublishedFinancialReport:
    """将已发布的报表头 SQL 行投影为不泄漏内部主键的端口对象。"""
    return PublishedFinancialReport(
        report_ref=UUID(str(row["report_ref"])),
        statement_type=str(row["statement_type"]),
        report_period=cast(date, row["report_period"]),
        period_basis=str(row["period_basis"]),
        statement_scope=str(row["statement_scope"]),
        currency=cast(str | None, row["currency"]),
        currency_null_reason=cast(str | None, row["currency_null_reason"]),
        report_type=str(row["report_type"]),
        audit_status=str(row["audit_status"]),
        announcement_date=cast(date | None, row["announcement_date"]),
        provider_update_at=cast(datetime | None, row["provider_update_at"]),
        effective_from=cast(date, row["effective_from"]),
        effective_to=cast(date | None, row["effective_to"]),
        known_from=cast(datetime, row["known_from"]),
        known_to=cast(datetime | None, row["known_to"]),
        knowledge_basis=str(row["knowledge_basis"]),
        knowledge_confidence=str(row["knowledge_confidence"]),
        observed_at=cast(datetime, row["observed_at"]),
        revision=_integer(row["revision"]),
        quality_status=str(row["quality_status"]),
    )


def _financial_publication_snapshot(row: Mapping[str, object]) -> FinancialPublicationSnapshot:
    """将当前生产发布 SQL 行转换为端口快照，保持所有选择器共享同一投影语义。"""
    return FinancialPublicationSnapshot(
        data_version=UUID(str(row["data_version"])),
        security_id=_integer(row["security_id"]),
        instrument_id=UUID(str(row["instrument_id"])),
        methodology_id=UUID(str(row["methodology_id"])),
        capability=cast(FinancialCapability, str(row["capability"])),
        methodology_code=str(row["methodology_code"]),
        methodology_version=_integer(row["methodology_version"]),
        source_code=str(row["source_code"]),
        published_at=cast(datetime, row["published_at"]),
        effective_as_of=cast(date, row["effective_as_of"]),
        knowledge_cutoff=cast(datetime, row["knowledge_cutoff"]),
        row_count=_integer(row["row_count"]),
        content_sha256=str(row["content_sha256"]),
    )


def _published_financial_statement_fact(
    row: Mapping[str, object],
) -> PublishedFinancialStatementFact:
    """投影已治理行项目，确保 `Decimal` 不在端口层退化为二进制浮点。"""
    return PublishedFinancialStatementFact(
        metric_code=str(row["metric_code"]),
        label=str(row["label"]),
        value=cast(Decimal | None, row["value"]),
        null_reason=cast(str | None, row["null_reason"]),
        currency=cast(str | None, row["currency"]),
        currency_null_reason=cast(str | None, row["currency_null_reason"]),
        original_unit=str(row["original_unit"]),
        canonical_unit=str(row["canonical_unit"]),
        scale_factor=cast(Decimal, row["scale_factor"]),
        sign_convention=str(row["sign_convention"]),
    )


def _published_financial_metric(row: Mapping[str, object]) -> PublishedFinancialMetric:
    """将供应商直接指标 SQL 行投影为不暴露内部键的生产读取对象。"""
    return PublishedFinancialMetric(
        metric_code=str(row["metric_code"]),
        label=str(row["label"]),
        origin="PROVIDER_REPORTED",
        report_period=cast(date, row["report_period"]),
        period_basis=str(row["period_basis"]),
        statement_scope=str(row["statement_scope"]),
        value=cast(Decimal, row["value"]),
        unit=str(row["unit"]),
        currency=cast(str | None, row["currency"]),
        currency_null_reason=cast(str | None, row["currency_null_reason"]),
        formula_version=None,
        effective_from=cast(date, row["effective_from"]),
        known_from=cast(datetime, row["known_from"]),
        knowledge_basis=str(row["knowledge_basis"]),
        knowledge_confidence=str(row["knowledge_confidence"]),
        observed_at=cast(datetime, row["observed_at"]),
        revision=_integer(row["revision"]),
    )


def _published_derived_financial_metric(
    row: Mapping[str, object],
) -> PublishedFinancialMetric:
    """将平台派生 SQL 行投影为保留公式版本且与供应商值隔离的读取对象。"""
    return PublishedFinancialMetric(
        metric_code=str(row["metric_code"]),
        label=str(row["label"]),
        origin="PLATFORM_DERIVED",
        report_period=cast(date, row["report_period"]),
        period_basis=str(row["period_basis"]),
        statement_scope=str(row["statement_scope"]),
        value=cast(Decimal, row["value"]),
        unit=str(row["unit"]),
        currency=cast(str | None, row["currency"]),
        currency_null_reason=cast(str | None, row["currency_null_reason"]),
        formula_version=_integer(row["formula_version"]),
        effective_from=cast(date, row["effective_from"]),
        known_from=cast(datetime, row["known_from"]),
        knowledge_basis=str(row["knowledge_basis"]),
        knowledge_confidence=str(row["knowledge_confidence"]),
        observed_at=cast(datetime, row["observed_at"]),
        revision=_integer(row["revision"]),
    )


def _integer(value: object) -> int:
    """将数据库整数投影为端口整数；读取映射的泛型边界不影响运行时精确值。"""
    return int(cast(int | str, value))


def _published_valuation_observation(row: Mapping[str, object]) -> PublishedValuationObservation:
    """将估值观察 SQL 行投影为保留 finality 的生产读取对象。"""
    return PublishedValuationObservation(
        observation_date=cast(date, row["observation_date"]),
        metric_code=str(row["metric_code"]),
        value=cast(Decimal, row["value"]),
        unit=str(row["unit"]),
        currency=cast(str | None, row["currency"]),
        currency_null_reason=cast(str | None, row["currency_null_reason"]),
        finality=str(row["finality"]),
        effective_from=cast(date, row["effective_from"]),
        known_from=cast(datetime, row["known_from"]),
        knowledge_basis=str(row["knowledge_basis"]),
        knowledge_confidence=str(row["knowledge_confidence"]),
        observed_at=cast(datetime, row["observed_at"]),
        revision=_integer(row["revision"]),
    )
