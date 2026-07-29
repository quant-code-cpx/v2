"""使用 `ORM` 写入财务 `canonical revision`、质量证据、检查点与消费者发布。

三大报表、供应商指标和历史估值分别规范化、质量检查并发布，不能因同一证券请求而被
混成一个版本。报告期、公告日、币种、数值域和空值原因均保留来源边界；相同内容的
重放复用稳定版本，来源或映射变化则在同一事务中追加血缘完整的新修订。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, cast
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import func, insert, select, update
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from service_data_sync.application.ports.financial_sync import (
    FinancialCapability,
    FinancialMetricInput,
    FinancialPublicationResult,
    FinancialReportInput,
    FinancialSourceObservation,
    FinancialSyncRepository,
    FinancialValuationInput,
)
from service_data_sync.domain.equity import Exchange
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.database.models.financial import (
    provider_financial_metric_revision,
    valuation_observation_revision,
)
from service_data_sync.infrastructure.database.models.financial.financial_change_checkpoint import (
    FinancialChangeCheckpoint,
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
from service_data_sync.infrastructure.database.models.financial.financial_quality_result import (
    FinancialQualityResult,
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
from service_data_sync.infrastructure.database.partition_manager import (
    ensure_financial_year_partitions,
)
from service_data_sync.infrastructure.persistence.equity_identity_resolver import (
    require_single_confirmed_identity_on_connection,
)
from service_data_sync.infrastructure.persistence.source_batch import record_source_observation

ProviderFinancialMetricRevision = provider_financial_metric_revision.ProviderFinancialMetricRevision
ValuationObservationRevision = valuation_observation_revision.ValuationObservationRevision

_METHODOLOGY: dict[FinancialCapability, str] = {
    "financial.report": "akshare.eastmoney.financial-report",
    "financial.provider-metric": "akshare.eastmoney.financial-provider-metric",
    "financial.valuation": "akshare.eastmoney.financial-valuation",
}
_SOURCE_CODE = "akshare.eastmoney"
_METHODOLOGY_VERSION = 1


class SqlAlchemyFinancialSyncRepository(FinancialSyncRepository):
    """以单证券、单能力、单方法学事务维护财务双时态 current view 和发布指针。"""

    def __init__(self, database: DatabaseClient) -> None:
        """保存服务自有事务工厂，不向应用层暴露 `SQLAlchemy` 会话。"""
        self._database = database

    def publish_reports(
        self,
        *,
        exchange: Exchange,
        symbol: str,
        reports: Sequence[FinancialReportInput],
        source: FinancialSourceObservation,
    ) -> FinancialPublicationResult:
        """写入报表头和行项目 revision；重放相同内容不制造伪修订或伪发布。"""
        if not reports:
            raise ValueError("reports must not be empty")
        now = datetime.now(UTC)
        with self._database.transaction() as connection:
            security_id = self._security_id(
                connection,
                exchange=exchange,
                symbol=symbol,
                fact_dates=tuple(report.report_period for report in reports),
                known_at=now,
            )
            methodology_id = self._methodology_id(
                connection, capability="financial.report", now=now
            )
            source_batch_id = self._source_batch(connection, source=source, now=now)
            inserted_count = 0
            for report in reports:
                ensure_financial_year_partitions(connection, report.report_period)
                inserted_count += self._write_report(
                    connection,
                    security_id=security_id,
                    methodology_id=methodology_id,
                    report=report,
                    source_batch_id=source_batch_id,
                    source=source,
                    now=now,
                )
            effective_as_of, row_count, content_sha256 = self._report_publication_state(
                connection,
                security_id=security_id,
                methodology_id=methodology_id,
            )
            result = self._publish(
                connection,
                capability="financial.report",
                security_id=security_id,
                methodology_id=methodology_id,
                effective_as_of=effective_as_of,
                source=source,
                changed_count=inserted_count,
                row_count=row_count,
                content_sha256=content_sha256,
                now=now,
            )
            self._checkpoint(connection, source=source, result=result, now=now)
            self._quality_result(
                connection,
                source_batch_id=source_batch_id,
                data_version=result.data_version,
                inserted_count=inserted_count,
                now=now,
            )
        return result

    def publish_provider_metrics(
        self,
        *,
        exchange: Exchange,
        symbol: str,
        metrics: Sequence[FinancialMetricInput],
        source: FinancialSourceObservation,
    ) -> FinancialPublicationResult:
        """写入供应商直接财务指标 revision，不把供应商派生口径混入披露行项目。"""
        if not metrics:
            raise ValueError("metrics must not be empty")
        now = datetime.now(UTC)
        with self._database.transaction() as connection:
            security_id = self._security_id(
                connection,
                exchange=exchange,
                symbol=symbol,
                fact_dates=tuple(metric.report_period for metric in metrics),
                known_at=now,
            )
            methodology_id = self._methodology_id(
                connection, capability="financial.provider-metric", now=now
            )
            source_batch_id = self._source_batch(connection, source=source, now=now)
            inserted_count = 0
            for metric in metrics:
                ensure_financial_year_partitions(connection, metric.report_period)
                inserted_count += self._write_provider_metric(
                    connection,
                    security_id=security_id,
                    methodology_id=methodology_id,
                    metric=metric,
                    source_batch_id=source_batch_id,
                    source=source,
                    now=now,
                )
            effective_as_of, row_count, content_sha256 = self._provider_metric_publication_state(
                connection,
                security_id=security_id,
                methodology_id=methodology_id,
            )
            result = self._publish(
                connection,
                capability="financial.provider-metric",
                security_id=security_id,
                methodology_id=methodology_id,
                effective_as_of=effective_as_of,
                source=source,
                changed_count=inserted_count,
                row_count=row_count,
                content_sha256=content_sha256,
                now=now,
            )
            self._checkpoint(connection, source=source, result=result, now=now)
            self._quality_result(
                connection,
                source_batch_id=source_batch_id,
                data_version=result.data_version,
                inserted_count=inserted_count,
                now=now,
            )
        return result

    def publish_valuations(
        self,
        *,
        exchange: Exchange,
        symbol: str,
        valuations: Sequence[FinancialValuationInput],
        source: FinancialSourceObservation,
    ) -> FinancialPublicationResult:
        """写入供应商日频估值观察 revision，并以独立 dataVersion 对消费者发布。"""
        if not valuations:
            raise ValueError("valuations must not be empty")
        now = datetime.now(UTC)
        with self._database.transaction() as connection:
            security_id = self._security_id(
                connection,
                exchange=exchange,
                symbol=symbol,
                fact_dates=tuple(valuation.observation_date for valuation in valuations),
                known_at=now,
            )
            methodology_id = self._methodology_id(
                connection, capability="financial.valuation", now=now
            )
            source_batch_id = self._source_batch(connection, source=source, now=now)
            inserted_count = 0
            for valuation in valuations:
                ensure_financial_year_partitions(connection, valuation.observation_date)
                inserted_count += self._write_valuation(
                    connection,
                    security_id=security_id,
                    methodology_id=methodology_id,
                    valuation=valuation,
                    source_batch_id=source_batch_id,
                    source=source,
                    now=now,
                )
            effective_as_of, row_count, content_sha256 = self._valuation_publication_state(
                connection,
                security_id=security_id,
                methodology_id=methodology_id,
            )
            result = self._publish(
                connection,
                capability="financial.valuation",
                security_id=security_id,
                methodology_id=methodology_id,
                effective_as_of=effective_as_of,
                source=source,
                changed_count=inserted_count,
                row_count=row_count,
                content_sha256=content_sha256,
                now=now,
            )
            self._checkpoint(connection, source=source, result=result, now=now)
            self._quality_result(
                connection,
                source_batch_id=source_batch_id,
                data_version=result.data_version,
                inserted_count=inserted_count,
                now=now,
            )
        return result

    def _report_publication_state(
        self,
        connection: Session,
        *,
        security_id: int,
        methodology_id: UUID,
    ) -> tuple[date, int, str]:
        """汇总全部当前可见报表 revision，避免部分重跑把旧报表从发布摘要中遗漏。"""
        rows = (
            connection.execute(
                select(
                    FinancialReport.report_ref,
                    FinancialReport.statement_type,
                    FinancialReport.report_period,
                    FinancialReport.period_basis,
                    FinancialReport.statement_scope,
                    FinancialReportRevision.effective_from,
                    FinancialReportRevision.revision,
                    FinancialReportRevision.content_sha256,
                )
                .select_from(FinancialReport)
                .join(
                    FinancialReportRevision,
                    FinancialReportRevision.financial_report_id
                    == FinancialReport.financial_report_id,
                )
                .where(
                    FinancialReport.security_id == security_id,
                    FinancialReport.methodology_id == methodology_id,
                    FinancialReportRevision.known_to.is_(None),
                    FinancialReportRevision.quality_status.in_(("passed", "warned")),
                )
                .order_by(
                    FinancialReport.report_period,
                    FinancialReport.statement_type,
                    FinancialReport.report_ref,
                )
            )
            .mappings()
            .all()
        )
        if not rows:
            raise ValueError("financial report publication has no visible revisions")
        canonical_rows = [dict(row) for row in rows]
        return (
            max(cast(date, row["effective_from"]) for row in rows),
            len(canonical_rows),
            _content_hash(canonical_rows),
        )

    def _provider_metric_publication_state(
        self,
        connection: Session,
        *,
        security_id: int,
        methodology_id: UUID,
    ) -> tuple[date, int, str]:
        """汇总全部当前供应商指标 revision，确保发布摘要对应完整而非仅本批输入。"""
        rows = (
            connection.execute(
                select(
                    ProviderFinancialMetricRevision.report_period,
                    ProviderFinancialMetricRevision.metric_id,
                    ProviderFinancialMetricRevision.period_basis,
                    ProviderFinancialMetricRevision.statement_scope,
                    ProviderFinancialMetricRevision.revision,
                    ProviderFinancialMetricRevision.content_sha256,
                )
                .where(
                    ProviderFinancialMetricRevision.security_id == security_id,
                    ProviderFinancialMetricRevision.methodology_id == methodology_id,
                    ProviderFinancialMetricRevision.known_to.is_(None),
                    ProviderFinancialMetricRevision.quality_status.in_(("passed", "warned")),
                )
                .order_by(
                    ProviderFinancialMetricRevision.report_period,
                    ProviderFinancialMetricRevision.metric_id,
                    ProviderFinancialMetricRevision.period_basis,
                    ProviderFinancialMetricRevision.statement_scope,
                )
            )
            .mappings()
            .all()
        )
        if not rows:
            raise ValueError("financial metric publication has no visible revisions")
        canonical_rows = [dict(row) for row in rows]
        return (
            max(cast(date, row["report_period"]) for row in rows),
            len(canonical_rows),
            _content_hash(canonical_rows),
        )

    def _valuation_publication_state(
        self,
        connection: Session,
        *,
        security_id: int,
        methodology_id: UUID,
    ) -> tuple[date, int, str]:
        """汇总全部当前估值 revision，防止增量同步产生只覆盖最新日期的错误发布摘要。"""
        rows = (
            connection.execute(
                select(
                    ValuationObservationRevision.observation_date,
                    ValuationObservationRevision.metric_id,
                    ValuationObservationRevision.revision,
                    ValuationObservationRevision.content_sha256,
                )
                .where(
                    ValuationObservationRevision.security_id == security_id,
                    ValuationObservationRevision.methodology_id == methodology_id,
                    ValuationObservationRevision.known_to.is_(None),
                    ValuationObservationRevision.quality_status.in_(("passed", "warned")),
                )
                .order_by(
                    ValuationObservationRevision.observation_date,
                    ValuationObservationRevision.metric_id,
                )
            )
            .mappings()
            .all()
        )
        if not rows:
            raise ValueError("financial valuation publication has no visible revisions")
        canonical_rows = [dict(row) for row in rows]
        return (
            max(cast(date, row["observation_date"]) for row in rows),
            len(canonical_rows),
            _content_hash(canonical_rows),
        )

    def _security_id(
        self,
        connection: Session,
        *,
        exchange: Exchange,
        symbol: str,
        fact_dates: Sequence[date],
        known_at: datetime,
    ) -> int:
        """按每个财务事实适用日解析永久键，拒绝未知身份和跨代码复用边界批次。"""
        return require_single_confirmed_identity_on_connection(
            connection,
            exchange=exchange,
            symbol=symbol,
            fact_dates=fact_dates,
            known_at=known_at,
        )

    def _methodology_id(
        self, connection: Session, *, capability: FinancialCapability, now: datetime
    ) -> UUID:
        """确保每种能力拥有一个已验证、可复现且固定版本的东财方法学记录。"""
        code = _METHODOLOGY[capability]
        existing = connection.execute(
            select(FinancialMethodology.methodology_id).where(
                FinancialMethodology.code == code,
                FinancialMethodology.version == _METHODOLOGY_VERSION,
            )
        ).scalar_one_or_none()
        if existing is not None:
            return UUID(str(existing))
        methodology_id = uuid5(NAMESPACE_URL, f"quant-v2:{code}:{_METHODOLOGY_VERSION}")
        semantic_spec_sha256 = hashlib.sha256(
            json.dumps(
                {"code": code, "capability": capability, "source": _SOURCE_CODE, "version": 1},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        connection.execute(
            insert(FinancialMethodology).values(
                methodology_id=methodology_id,
                code=code,
                version=_METHODOLOGY_VERSION,
                capability=capability,
                source_code=_SOURCE_CODE,
                status="validated",
                semantic_spec_sha256=semantic_spec_sha256,
                created_at=now,
            )
        )
        return methodology_id

    def _source_batch(
        self, connection: Session, *, source: FinancialSourceObservation, now: datetime
    ) -> UUID:
        """记录任何一次来源观察，即使内容不变也保留可审计 raw evidence 身份。"""
        return record_source_observation(
            connection,
            provider_id=source.provider_id,
            capability=source.capability,
            source_payload_sha256=source.source_payload_sha256,
            raw_uri=source.raw_uri,
            observed_at=source.observed_at,
            created_at=now,
            upstream_source=source.upstream_source,
            adapter_version=source.adapter_version,
            schema_fingerprint=source.schema_fingerprint,
        )

    def _write_report(
        self,
        connection: Session,
        *,
        security_id: int,
        methodology_id: UUID,
        report: FinancialReportInput,
        source_batch_id: UUID,
        source: FinancialSourceObservation,
        now: datetime,
    ) -> int:
        """仅当报表内容变化时闭合当前知识区间并追加新 revision 与同 revision 的行项目。"""
        report_id = self._report_id(
            connection,
            security_id=security_id,
            methodology_id=methodology_id,
            report=report,
        )
        content_sha256 = _content_hash(report)
        current = (
            connection.execute(
                select(
                    FinancialReportRevision.revision, FinancialReportRevision.content_sha256
                ).where(
                    FinancialReportRevision.report_period == report.report_period,
                    FinancialReportRevision.financial_report_id == report_id,
                    FinancialReportRevision.known_to.is_(None),
                )
            )
            .mappings()
            .one_or_none()
        )
        if current is not None and str(current["content_sha256"]) == content_sha256:
            return 0
        revision = self._next_revision(
            connection,
            FinancialReportRevision.revision,
            FinancialReportRevision.report_period == report.report_period,
            FinancialReportRevision.financial_report_id == report_id,
        )
        if current is not None:
            connection.execute(
                update(FinancialReportRevision)
                .where(
                    FinancialReportRevision.report_period == report.report_period,
                    FinancialReportRevision.financial_report_id == report_id,
                    FinancialReportRevision.known_to.is_(None),
                )
                .values(known_to=now)
            )
        revision_id = uuid4()
        connection.execute(
            insert(FinancialReportRevision).values(
                report_period=report.report_period,
                revision_id=revision_id,
                financial_report_id=report_id,
                revision=revision,
                announcement_date=report.announcement_date,
                provider_update_at=report.provider_update_at,
                audit_status=report.audit_status,
                effective_from=report.announcement_date or report.report_period,
                effective_to=None,
                known_from=now,
                known_to=None,
                knowledge_basis="OBSERVED_AT",
                knowledge_confidence="CONSERVATIVE",
                observed_at=source.observed_at,
                source_batch_id=source_batch_id,
                content_sha256=content_sha256,
                quality_status="passed",
                created_at=now,
            )
        )
        for fact in report.facts:
            metric_id = self._metric_id(
                connection,
                code=fact.code,
                label=fact.label,
                origin="statement_fact",
                statement_type=report.statement_type,
                value_domain=fact.value_domain,
                canonical_unit=fact.canonical_unit,
                currency_required=fact.currency is not None,
                sign_convention=fact.sign_convention,
            )
            connection.execute(
                insert(FinancialStatementFact).values(
                    report_period=report.report_period,
                    revision_id=revision_id,
                    metric_id=metric_id,
                    value=fact.value,
                    null_reason=fact.null_reason,
                    currency=fact.currency,
                    currency_null_reason=fact.currency_null_reason,
                    original_unit=fact.original_unit,
                    canonical_unit=fact.canonical_unit,
                    scale_factor=fact.scale_factor,
                    sign_convention=fact.sign_convention,
                )
            )
        return 1

    def _report_id(
        self,
        connection: Session,
        *,
        security_id: int,
        methodology_id: UUID,
        report: FinancialReportInput,
    ) -> int:
        """取得或创建报表逻辑身份；公开引用稳定，不因后续 revision 改变。"""
        statement = select(FinancialReport.financial_report_id).where(
            FinancialReport.security_id == security_id,
            FinancialReport.methodology_id == methodology_id,
            FinancialReport.statement_type == report.statement_type,
            FinancialReport.report_period == report.report_period,
            FinancialReport.period_basis == report.period_basis,
            FinancialReport.statement_scope == report.statement_scope,
            FinancialReport.currency == report.currency,
            FinancialReport.report_type == report.report_type,
        )
        existing = connection.execute(statement).scalar_one_or_none()
        if existing is not None:
            return int(existing)
        return int(
            connection.execute(
                insert(FinancialReport)
                .values(
                    report_ref=uuid4(),
                    security_id=security_id,
                    methodology_id=methodology_id,
                    statement_type=report.statement_type,
                    report_period=report.report_period,
                    period_basis=report.period_basis,
                    statement_scope=report.statement_scope,
                    currency=report.currency,
                    currency_null_reason=report.currency_null_reason,
                    report_type=report.report_type,
                    superseded_by=None,
                )
                .returning(FinancialReport.financial_report_id)
            ).scalar_one()
        )

    def _write_provider_metric(
        self,
        connection: Session,
        *,
        security_id: int,
        methodology_id: UUID,
        metric: FinancialMetricInput,
        source_batch_id: UUID,
        source: FinancialSourceObservation,
        now: datetime,
    ) -> int:
        """只为发生变化的供应商指标追加 revision，保证每个逻辑键只有一个当前知识版本。"""
        metric_id = self._metric_id(
            connection,
            code=metric.code,
            label=metric.label,
            origin="provider_reported",
            statement_type=None,
            value_domain=metric.value_domain,
            canonical_unit=metric.unit,
            currency_required=metric.currency is not None,
            sign_convention="provider_as_reported",
        )
        current_filter = (
            ProviderFinancialMetricRevision.report_period == metric.report_period,
            ProviderFinancialMetricRevision.security_id == security_id,
            ProviderFinancialMetricRevision.metric_id == metric_id,
            ProviderFinancialMetricRevision.methodology_id == methodology_id,
            ProviderFinancialMetricRevision.period_basis == metric.period_basis,
            ProviderFinancialMetricRevision.statement_scope == metric.statement_scope,
        )
        content_sha256 = _content_hash(metric)
        current = (
            connection.execute(
                select(
                    ProviderFinancialMetricRevision.revision,
                    ProviderFinancialMetricRevision.content_sha256,
                ).where(*current_filter, ProviderFinancialMetricRevision.known_to.is_(None))
            )
            .mappings()
            .one_or_none()
        )
        if current is not None and str(current["content_sha256"]) == content_sha256:
            return 0
        revision = self._next_revision(
            connection,
            ProviderFinancialMetricRevision.revision,
            *current_filter,
        )
        if current is not None:
            connection.execute(
                update(ProviderFinancialMetricRevision)
                .where(*current_filter, ProviderFinancialMetricRevision.known_to.is_(None))
                .values(known_to=now)
            )
        connection.execute(
            insert(ProviderFinancialMetricRevision).values(
                report_period=metric.report_period,
                metric_revision_id=uuid4(),
                security_id=security_id,
                metric_id=metric_id,
                methodology_id=methodology_id,
                period_basis=metric.period_basis,
                statement_scope=metric.statement_scope,
                value=metric.value,
                unit=metric.unit,
                currency=metric.currency,
                currency_null_reason=metric.currency_null_reason,
                effective_from=metric.report_period,
                effective_to=None,
                known_from=now,
                known_to=None,
                knowledge_basis="OBSERVED_AT",
                knowledge_confidence="CONSERVATIVE",
                observed_at=source.observed_at,
                source_batch_id=source_batch_id,
                revision=revision,
                content_sha256=content_sha256,
                quality_status="passed",
                created_at=now,
            )
        )
        return 1

    def _write_valuation(
        self,
        connection: Session,
        *,
        security_id: int,
        methodology_id: UUID,
        valuation: FinancialValuationInput,
        source_batch_id: UUID,
        source: FinancialSourceObservation,
        now: datetime,
    ) -> int:
        """只为变化的日频估值追加 provider observation revision，不改写历史观察。"""
        metric_id = self._metric_id(
            connection,
            code=valuation.code,
            label=valuation.label,
            origin="valuation",
            statement_type=None,
            value_domain=valuation.value_domain,
            canonical_unit=valuation.unit,
            currency_required=valuation.currency is not None,
            sign_convention="provider_as_reported",
        )
        current_filter = (
            ValuationObservationRevision.observation_date == valuation.observation_date,
            ValuationObservationRevision.security_id == security_id,
            ValuationObservationRevision.metric_id == metric_id,
            ValuationObservationRevision.methodology_id == methodology_id,
        )
        content_sha256 = _content_hash(valuation)
        current = (
            connection.execute(
                select(
                    ValuationObservationRevision.revision,
                    ValuationObservationRevision.content_sha256,
                ).where(*current_filter, ValuationObservationRevision.known_to.is_(None))
            )
            .mappings()
            .one_or_none()
        )
        if current is not None and str(current["content_sha256"]) == content_sha256:
            return 0
        revision = self._next_revision(
            connection,
            ValuationObservationRevision.revision,
            *current_filter,
        )
        if current is not None:
            connection.execute(
                update(ValuationObservationRevision)
                .where(*current_filter, ValuationObservationRevision.known_to.is_(None))
                .values(known_to=now)
            )
        connection.execute(
            insert(ValuationObservationRevision).values(
                observation_date=valuation.observation_date,
                valuation_revision_id=uuid4(),
                security_id=security_id,
                metric_id=metric_id,
                methodology_id=methodology_id,
                revision=revision,
                value=valuation.value,
                unit=valuation.unit,
                currency=valuation.currency,
                currency_null_reason=valuation.currency_null_reason,
                finality="PROVIDER_OBSERVATION",
                effective_from=valuation.observation_date,
                effective_to=None,
                known_from=now,
                known_to=None,
                knowledge_basis="OBSERVED_AT",
                knowledge_confidence="CONSERVATIVE",
                observed_at=source.observed_at,
                source_batch_id=source_batch_id,
                content_sha256=content_sha256,
                quality_status="passed",
                created_at=now,
            )
        )
        return 1

    def _metric_id(
        self,
        connection: Session,
        *,
        code: str,
        label: str,
        origin: str,
        statement_type: str | None,
        value_domain: str,
        canonical_unit: str,
        currency_required: bool,
        sign_convention: str,
    ) -> int:
        """取得或追加 active 字典字段；adapter 已生成稳定代码，重复运行不会创建重复定义。"""
        existing = connection.execute(
            select(FinancialMetricDefinition.metric_id).where(
                FinancialMetricDefinition.code == code,
                FinancialMetricDefinition.status == "active",
            )
        ).scalar_one_or_none()
        if existing is not None:
            return int(existing)
        return int(
            connection.execute(
                insert(FinancialMetricDefinition)
                .values(
                    code=code,
                    label=label,
                    origin=origin,
                    statement_type=statement_type,
                    value_domain=value_domain,
                    canonical_unit=canonical_unit,
                    currency_required=currency_required,
                    sign_convention=sign_convention,
                    dictionary_version=1,
                    status="active",
                )
                .returning(FinancialMetricDefinition.metric_id)
            ).scalar_one()
        )

    def _next_revision(
        self,
        connection: Session,
        revision_column: Any,
        *filters: ColumnElement[bool],
    ) -> int:
        """读取逻辑键的最大 revision 并递增，历史均已闭合时也不会从一重新编号。"""
        maximum = connection.execute(select(func.max(revision_column)).where(*filters)).scalar_one()
        return 1 if maximum is None else int(maximum) + 1

    def _publish(
        self,
        connection: Session,
        *,
        capability: FinancialCapability,
        security_id: int,
        methodology_id: UUID,
        effective_as_of: date,
        source: FinancialSourceObservation,
        changed_count: int,
        row_count: int,
        content_sha256: str,
        now: datetime,
    ) -> FinancialPublicationResult:
        """仅当前 canonical 视图变化时替换 dataset 指针，并写入不可变财务发布明细。"""
        partition_key = f"{security_id}:{methodology_id}"
        current_data_version = connection.execute(
            select(DatasetPublication.data_version).where(
                DatasetPublication.dataset == capability,
                DatasetPublication.partition_key == partition_key,
                DatasetPublication.superseded_at.is_(None),
            )
        ).scalar_one_or_none()
        if changed_count == 0 and current_data_version is not None:
            return FinancialPublicationResult(
                capability=capability,
                data_version=UUID(str(current_data_version)),
                inserted_count=0,
                unchanged_count=row_count,
            )
        connection.execute(
            update(DatasetPublication)
            .where(
                DatasetPublication.dataset == capability,
                DatasetPublication.partition_key == partition_key,
                DatasetPublication.superseded_at.is_(None),
            )
            .values(superseded_at=now)
        )
        data_version = uuid4()
        connection.execute(
            insert(DatasetPublication).values(
                publication_id=uuid4(),
                dataset=capability,
                partition_key=partition_key,
                data_version=data_version,
                quality_status="passed",
                published_at=now,
                superseded_at=None,
                effective_as_of=effective_as_of,
                knowledge_cutoff=now,
            )
        )
        connection.execute(
            insert(FinancialPublication).values(
                data_version=data_version,
                capability=capability,
                security_id=security_id,
                methodology_id=methodology_id,
                effective_as_of=effective_as_of,
                knowledge_cutoff=now,
                row_count=row_count,
                content_sha256=content_sha256,
                published_at=now,
            )
        )
        return FinancialPublicationResult(
            capability=capability,
            data_version=data_version,
            inserted_count=changed_count,
            unchanged_count=row_count - changed_count,
        )

    def _checkpoint(
        self,
        connection: Session,
        *,
        source: FinancialSourceObservation,
        result: FinancialPublicationResult,
        now: datetime,
    ) -> None:
        """更新能力级来源摘要 checkpoint；同一输入仍登记来源观察但不推进 dataVersion。"""
        partition_key = f"{result.capability}:{source.provider_id}"
        existing = connection.execute(
            select(FinancialChangeCheckpoint.capability).where(
                FinancialChangeCheckpoint.capability == result.capability,
                FinancialChangeCheckpoint.partition_key == partition_key,
            )
        ).scalar_one_or_none()
        values = {
            "summary_sha256": source.source_payload_sha256,
            "provider_watermark": None,
            "last_data_version": result.data_version,
            "last_success_at": now,
            "updated_at": now,
        }
        if existing is None:
            connection.execute(
                insert(FinancialChangeCheckpoint).values(
                    capability=result.capability,
                    partition_key=partition_key,
                    **values,
                )
            )
            return
        connection.execute(
            update(FinancialChangeCheckpoint)
            .where(
                FinancialChangeCheckpoint.capability == result.capability,
                FinancialChangeCheckpoint.partition_key == partition_key,
            )
            .values(**values)
        )

    def _quality_result(
        self,
        connection: Session,
        *,
        source_batch_id: UUID,
        data_version: UUID,
        inserted_count: int,
        now: datetime,
    ) -> None:
        """记录最小可观测质量结果；后续可追加覆盖率和会计勾稽规则而不改写历史。"""
        connection.execute(
            insert(FinancialQualityResult).values(
                quality_result_id=uuid4(),
                source_batch_id=source_batch_id,
                data_version=data_version,
                rule_code="CANONICAL_WRITE",
                rule_version=1,
                severity="info",
                status="passed",
                measured=Decimal(inserted_count),
                threshold=Decimal(0),
                dimension=None,
                created_at=now,
            )
        )


def _content_hash(value: object) -> str:
    """对 dataclass 或序列生成稳定 JSON 摘要，避免同内容重放产生伪 revision。"""
    return hashlib.sha256(
        json.dumps(
            value, default=_json_default, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


def _json_default(value: object) -> object:
    """将精确数值、日期和 dataclass 字段投影为稳定 JSON，拒绝未知可变对象。"""
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (date, datetime, UUID)):
        return value.isoformat() if not isinstance(value, UUID) else str(value)
    slots = getattr(value, "__slots__", None)
    if isinstance(slots, tuple):
        return {key: getattr(value, key) for key in slots}
    raise TypeError(f"unsupported canonical hash value: {type(value)!r}")
