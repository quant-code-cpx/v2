"""平台派生财务指标的点时输入选择、revision、血缘与 publication 仓储。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import cast
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import and_, func, insert, or_, select, tuple_, update
from sqlalchemy.orm import Session
from sqlalchemy.sql import Select

from service_data_sync.application.ports.financial_derived import (
    DerivedFinancialMetricInput,
    FinancialDerivationRepository,
    FinancialDerivationSnapshot,
    FinancialDerivationUnavailable,
    FinancialDerivedPublication,
    ReportedFinancialFact,
)
from service_data_sync.domain.equity import Exchange
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.database.models.equity.identity import (
    equity_identifier_version,
)
from service_data_sync.infrastructure.database.models.execution.sync_run import SyncRun
from service_data_sync.infrastructure.database.models.financial import (
    derived_financial_metric_revision,
)
from service_data_sync.infrastructure.database.models.financial.financial_derivation_input import (
    FinancialDerivationInput,
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
from service_data_sync.infrastructure.database.partition_manager import (
    ensure_financial_year_partitions,
)

DerivedFinancialMetricRevision = derived_financial_metric_revision.DerivedFinancialMetricRevision
EquityIdentifierVersion = equity_identifier_version.EquityIdentifierVersion

_REPORT_METHODOLOGY_CODE = "akshare.eastmoney.financial-report"
_REPORT_METHODOLOGY_VERSION = 1
_DERIVED_METHODOLOGY_CODE = "platform.financial-derivation"
_DERIVED_METHODOLOGY_VERSION = 1
_DERIVED_METHODOLOGY_ID = uuid5(
    NAMESPACE_URL,
    f"quant-v2:{_DERIVED_METHODOLOGY_CODE}:{_DERIVED_METHODOLOGY_VERSION}",
)
_FORMULA_INPUT_CODES = (
    "statement.income_statement.total-operate-income",
    "statement.income_statement.parent-netprofit",
)


class SqlAlchemyFinancialDerivationRepository(FinancialDerivationRepository):
    """以已发布报表点时视图为唯一输入，禁止直读未发布或隔离 revision。"""

    def __init__(self, database: DatabaseClient) -> None:
        """保存服务自有短生命周期 Session 工厂。"""
        self._database = database

    def load_inputs(self, *, exchange: Exchange, symbol: str) -> FinancialDerivationSnapshot:
        """按 publication 的 asOf/knownAt 与日期感知身份读取唯一报表输入快照。"""
        with self._database.session() as connection:
            publication_row = (
                connection.execute(_report_publication_statement(exchange=exchange, symbol=symbol))
                .mappings()
                .one_or_none()
            )
            if publication_row is None:
                raise FinancialDerivationUnavailable(
                    "published financial report inputs are unavailable"
                )
            snapshot = _snapshot_without_facts(dict(publication_row))
            fact_rows = connection.execute(_input_fact_statement(snapshot)).mappings().all()
        facts = tuple(_reported_fact(dict(row)) for row in fact_rows)
        return FinancialDerivationSnapshot(
            data_version=snapshot.data_version,
            security_id=snapshot.security_id,
            methodology_id=snapshot.methodology_id,
            effective_as_of=snapshot.effective_as_of,
            knowledge_cutoff=snapshot.knowledge_cutoff,
            facts=facts,
        )

    def publish(
        self,
        *,
        snapshot: FinancialDerivationSnapshot,
        metrics: Sequence[DerivedFinancialMetricInput],
        derivation_run_id: UUID,
        computed_at: datetime,
    ) -> FinancialDerivedPublication:
        """重验报表版本，追加变化 revision/逐项 manifest，并原子替换派生发布。"""
        if computed_at.tzinfo is None:
            raise ValueError("computed_at must include a timezone")
        with self._database.transaction() as connection:
            _require_current_input_publication(connection, snapshot)
            _require_derivation_run(connection, derivation_run_id)
            methodology_id = _require_derived_methodology(connection)
            for partition_date in sorted(
                {metric.report_period.replace(month=1, day=1) for metric in metrics}
            ):
                ensure_financial_year_partitions(connection, partition_date)
            metric_ids = _metric_ids(connection, metrics)
            target_keys = {
                _logical_key(metric=metric, metric_id=metric_ids[metric.metric_code])
                for metric in metrics
            }
            changed_count = _close_removed_metrics(
                connection,
                snapshot=snapshot,
                methodology_id=methodology_id,
                target_keys=target_keys,
                computed_at=computed_at,
            )
            unchanged_count = 0
            for metric in metrics:
                changed = _write_metric(
                    connection,
                    snapshot=snapshot,
                    metric=metric,
                    metric_id=metric_ids[metric.metric_code],
                    methodology_id=methodology_id,
                    derivation_run_id=derivation_run_id,
                    computed_at=computed_at,
                )
                changed_count += int(changed)
                unchanged_count += int(not changed)
            data_version = _publish(
                connection,
                snapshot=snapshot,
                methodology_id=methodology_id,
                metrics=metrics,
                changed_count=changed_count,
                computed_at=computed_at,
            )
        return FinancialDerivedPublication(
            data_version=data_version,
            inserted_count=changed_count,
            unchanged_count=unchanged_count,
            row_count=len(metrics),
        )


def _report_publication_statement(
    *, exchange: Exchange, symbol: str
) -> Select[tuple[UUID, int, UUID, date, datetime]]:
    """构造按 publication 截点解析证券身份的唯一报表输入选择器。"""
    return (
        select(
            FinancialPublication.data_version,
            FinancialPublication.security_id,
            FinancialPublication.methodology_id,
            FinancialPublication.effective_as_of,
            FinancialPublication.knowledge_cutoff,
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
            FinancialPublication.capability == "financial.report",
            FinancialMethodology.code == _REPORT_METHODOLOGY_CODE,
            FinancialMethodology.version == _REPORT_METHODOLOGY_VERSION,
            FinancialMethodology.status == "validated",
            DatasetPublication.superseded_at.is_(None),
        )
    )


def _snapshot_without_facts(row: Mapping[str, object]) -> FinancialDerivationSnapshot:
    """把 publication 行投影为待补充事实的端口快照。"""
    return FinancialDerivationSnapshot(
        data_version=UUID(str(row["data_version"])),
        security_id=int(cast(int | str, row["security_id"])),
        methodology_id=UUID(str(row["methodology_id"])),
        effective_as_of=cast(date, row["effective_as_of"]),
        knowledge_cutoff=cast(datetime, row["knowledge_cutoff"]),
        facts=(),
    )


def _input_fact_statement(
    snapshot: FinancialDerivationSnapshot,
) -> Select[
    tuple[
        date,
        str,
        int,
        str,
        Decimal,
        str,
        str | None,
        str | None,
        UUID,
        UUID,
        date,
        datetime,
        datetime,
    ]
]:
    """构造 publication 截点内非空、治理且可追溯的累计利润表事实查询。"""
    return (
        select(
            FinancialReport.report_period,
            FinancialReport.statement_scope,
            FinancialMetricDefinition.metric_id,
            FinancialMetricDefinition.code.label("metric_code"),
            FinancialStatementFact.value,
            FinancialStatementFact.canonical_unit.label("unit"),
            FinancialStatementFact.currency,
            FinancialStatementFact.currency_null_reason,
            FinancialReportRevision.revision_id,
            FinancialReportRevision.source_batch_id,
            FinancialReportRevision.effective_from,
            FinancialReportRevision.known_from,
            FinancialReportRevision.observed_at,
        )
        .select_from(FinancialReport)
        .join(
            FinancialReportRevision,
            and_(
                FinancialReportRevision.financial_report_id == FinancialReport.financial_report_id,
                FinancialReportRevision.report_period == FinancialReport.report_period,
            ),
        )
        .join(
            FinancialStatementFact,
            and_(
                FinancialStatementFact.report_period == FinancialReportRevision.report_period,
                FinancialStatementFact.revision_id == FinancialReportRevision.revision_id,
            ),
        )
        .join(
            FinancialMetricDefinition,
            FinancialMetricDefinition.metric_id == FinancialStatementFact.metric_id,
        )
        .where(
            FinancialReport.security_id == snapshot.security_id,
            FinancialReport.methodology_id == snapshot.methodology_id,
            FinancialReport.statement_type == "INCOME_STATEMENT",
            FinancialReport.period_basis == "YEAR_TO_DATE",
            FinancialMetricDefinition.code.in_(_FORMULA_INPUT_CODES),
            FinancialMetricDefinition.origin == "statement_fact",
            FinancialMetricDefinition.status == "active",
            FinancialStatementFact.value.is_not(None),
            FinancialReportRevision.quality_status.in_(("passed", "warned")),
            FinancialReportRevision.effective_from <= snapshot.effective_as_of,
            or_(
                FinancialReportRevision.effective_to.is_(None),
                FinancialReportRevision.effective_to > snapshot.effective_as_of,
            ),
            FinancialReportRevision.known_from <= snapshot.knowledge_cutoff,
            or_(
                FinancialReportRevision.known_to.is_(None),
                FinancialReportRevision.known_to > snapshot.knowledge_cutoff,
            ),
        )
        .order_by(
            FinancialMetricDefinition.code,
            FinancialReport.statement_scope,
            FinancialReport.report_period,
            FinancialReportRevision.revision_id,
        )
    )


def _reported_fact(row: Mapping[str, object]) -> ReportedFinancialFact:
    """把 SQL 行转换为精确输入，保留报表 revision、source batch 和时间血缘。"""
    return ReportedFinancialFact(
        report_period=cast(date, row["report_period"]),
        statement_scope=str(row["statement_scope"]),
        metric_id=int(cast(int | str, row["metric_id"])),
        metric_code=str(row["metric_code"]),
        value=cast(Decimal, row["value"]),
        unit=str(row["unit"]),
        currency=cast(str | None, row["currency"]),
        currency_null_reason=cast(str | None, row["currency_null_reason"]),
        revision_id=UUID(str(row["revision_id"])),
        source_batch_id=UUID(str(row["source_batch_id"])),
        effective_from=cast(date, row["effective_from"]),
        known_from=cast(datetime, row["known_from"]),
        observed_at=cast(datetime, row["observed_at"]),
    )


def _require_current_input_publication(
    connection: Session, snapshot: FinancialDerivationSnapshot
) -> None:
    """锁定输入 publication 并拒绝计算期间已经被替换的报表版本。"""
    current = connection.execute(
        select(DatasetPublication.data_version)
        .where(
            DatasetPublication.data_version == snapshot.data_version,
            DatasetPublication.superseded_at.is_(None),
        )
        .with_for_update()
    ).scalar_one_or_none()
    if current is None:
        raise FinancialDerivationUnavailable(
            "financial report publication changed during derivation"
        )


def _require_derivation_run(connection: Session, derivation_run_id: UUID) -> None:
    """锁定派生运行账本，确保 revision 不引用不存在或其他能力的任务。"""
    run = connection.execute(
        select(SyncRun.capability, SyncRun.status)
        .where(SyncRun.run_id == derivation_run_id)
        .with_for_update()
    ).one_or_none()
    if run is None or run.capability != "financial.derived-metric" or run.status != "running":
        raise FinancialDerivationUnavailable("financial derivation run is unavailable")


def _require_derived_methodology(connection: Session) -> UUID:
    """读取 migration 登记的唯一验证方法学，缺失时拒绝隐式创建。"""
    methodology_id = connection.execute(
        select(FinancialMethodology.methodology_id).where(
            FinancialMethodology.methodology_id == _DERIVED_METHODOLOGY_ID,
            FinancialMethodology.code == _DERIVED_METHODOLOGY_CODE,
            FinancialMethodology.version == _DERIVED_METHODOLOGY_VERSION,
            FinancialMethodology.capability == "financial.derived-metric",
            FinancialMethodology.status == "validated",
        )
    ).scalar_one_or_none()
    if methodology_id is None:
        raise FinancialDerivationUnavailable("validated derivation methodology is unavailable")
    return UUID(str(methodology_id))


def _metric_ids(
    connection: Session, metrics: Sequence[DerivedFinancialMetricInput]
) -> dict[str, int]:
    """解析 migration 管理的派生字段字典，任何缺失或重复都阻断发布。"""
    codes = tuple(sorted({metric.metric_code for metric in metrics}))
    rows = connection.execute(
        select(FinancialMetricDefinition.code, FinancialMetricDefinition.metric_id).where(
            FinancialMetricDefinition.code.in_(codes),
            FinancialMetricDefinition.origin == "platform_derived",
            FinancialMetricDefinition.status == "active",
        )
    ).all()
    result = {str(code): int(metric_id) for code, metric_id in rows}
    if set(result) != set(codes):
        raise FinancialDerivationUnavailable("derived financial metric dictionary is incomplete")
    return result


def _logical_key(
    *, metric: DerivedFinancialMetricInput, metric_id: int
) -> tuple[date, int, str, str, int]:
    """构造当前派生 revision 的稳定业务键。"""
    return (
        metric.report_period,
        metric_id,
        metric.period_basis,
        metric.statement_scope,
        metric.formula_version,
    )


def _close_removed_metrics(
    connection: Session,
    *,
    snapshot: FinancialDerivationSnapshot,
    methodology_id: UUID,
    target_keys: set[tuple[date, int, str, str, int]],
    computed_at: datetime,
) -> int:
    """关闭新输入快照已无法重算的旧输出，避免旧值残留在新 publication。"""
    rows = connection.execute(
        select(
            DerivedFinancialMetricRevision.report_period,
            DerivedFinancialMetricRevision.metric_revision_id,
            DerivedFinancialMetricRevision.metric_id,
            DerivedFinancialMetricRevision.period_basis,
            DerivedFinancialMetricRevision.statement_scope,
            DerivedFinancialMetricRevision.formula_version,
            DerivedFinancialMetricRevision.known_from,
        ).where(
            DerivedFinancialMetricRevision.security_id == snapshot.security_id,
            DerivedFinancialMetricRevision.methodology_id == methodology_id,
            DerivedFinancialMetricRevision.known_to.is_(None),
        )
    ).all()
    removed = [
        (report_period, metric_revision_id)
        for (
            report_period,
            metric_revision_id,
            metric_id,
            period_basis,
            statement_scope,
            formula_version,
            known_from,
        ) in rows
        if (
            report_period,
            int(metric_id),
            str(period_basis),
            str(statement_scope),
            int(formula_version),
        )
        not in target_keys
    ]
    if any(cast(datetime, known_from) >= computed_at for *_, known_from in rows):
        raise FinancialDerivationUnavailable(
            "computed_at must be after every current derived revision"
        )
    if removed:
        connection.execute(
            update(DerivedFinancialMetricRevision)
            .where(
                tuple_(
                    DerivedFinancialMetricRevision.report_period,
                    DerivedFinancialMetricRevision.metric_revision_id,
                ).in_(removed)
            )
            .values(known_to=computed_at)
        )
    return len(removed)


def _write_metric(
    connection: Session,
    *,
    snapshot: FinancialDerivationSnapshot,
    metric: DerivedFinancialMetricInput,
    metric_id: int,
    methodology_id: UUID,
    derivation_run_id: UUID,
    computed_at: datetime,
) -> bool:
    """同内容复用当前 revision；公式、值或输入 manifest 变化时追加并写完整血缘。"""
    current = (
        connection.execute(
            select(
                DerivedFinancialMetricRevision.metric_revision_id,
                DerivedFinancialMetricRevision.revision,
                DerivedFinancialMetricRevision.content_sha256,
                DerivedFinancialMetricRevision.known_from,
            ).where(
                DerivedFinancialMetricRevision.report_period == metric.report_period,
                DerivedFinancialMetricRevision.security_id == snapshot.security_id,
                DerivedFinancialMetricRevision.metric_id == metric_id,
                DerivedFinancialMetricRevision.methodology_id == methodology_id,
                DerivedFinancialMetricRevision.period_basis == metric.period_basis,
                DerivedFinancialMetricRevision.statement_scope == metric.statement_scope,
                DerivedFinancialMetricRevision.formula_version == metric.formula_version,
                DerivedFinancialMetricRevision.known_to.is_(None),
            )
        )
        .mappings()
        .one_or_none()
    )
    if current is not None and current["content_sha256"] == metric.content_sha256:
        return False
    if metric.observed_at > computed_at:
        raise FinancialDerivationUnavailable("computed_at precedes a derivation input observation")
    if current is not None:
        if cast(datetime, current["known_from"]) >= computed_at:
            raise FinancialDerivationUnavailable(
                "computed_at must be after the current derived revision"
            )
        connection.execute(
            update(DerivedFinancialMetricRevision)
            .where(
                DerivedFinancialMetricRevision.report_period == metric.report_period,
                DerivedFinancialMetricRevision.metric_revision_id == current["metric_revision_id"],
            )
            .values(known_to=computed_at)
        )
    revision = _next_revision(
        connection,
        snapshot=snapshot,
        metric=metric,
        metric_id=metric_id,
        methodology_id=methodology_id,
    )
    metric_revision_id = uuid4()
    trigger_source_batch_id = max(metric.inputs, key=_source_observation_order_key).source_batch_id
    connection.execute(
        insert(DerivedFinancialMetricRevision).values(
            report_period=metric.report_period,
            metric_revision_id=metric_revision_id,
            security_id=snapshot.security_id,
            metric_id=metric_id,
            methodology_id=methodology_id,
            period_basis=metric.period_basis,
            statement_scope=metric.statement_scope,
            value=metric.value,
            unit=metric.unit,
            currency=metric.currency,
            currency_null_reason=metric.currency_null_reason,
            formula_version=metric.formula_version,
            input_manifest_sha256=metric.input_manifest_sha256,
            derivation_run_id=derivation_run_id,
            computed_at=computed_at,
            effective_from=metric.effective_from,
            effective_to=None,
            known_from=computed_at,
            known_to=None,
            knowledge_basis="OBSERVED_AT",
            knowledge_confidence="CONSERVATIVE",
            observed_at=metric.observed_at,
            source_batch_id=trigger_source_batch_id,
            revision=revision,
            content_sha256=metric.content_sha256,
            quality_status="passed",
            created_at=computed_at,
        )
    )
    connection.execute(
        insert(FinancialDerivationInput).values(
            [
                {
                    "derived_report_period": metric.report_period,
                    "derived_metric_revision_id": metric_revision_id,
                    "input_sequence": sequence,
                    "input_role": role,
                    "input_report_period": input_fact.report_period,
                    "input_revision_id": input_fact.revision_id,
                    "input_metric_id": input_fact.metric_id,
                    "input_source_batch_id": input_fact.source_batch_id,
                    "input_data_version": snapshot.data_version,
                    "input_value": input_fact.value,
                    "input_unit": input_fact.unit,
                    "input_currency": input_fact.currency,
                    "input_currency_null_reason": input_fact.currency_null_reason,
                    "created_at": computed_at,
                }
                for sequence, (role, input_fact) in enumerate(
                    zip(_input_roles(metric), metric.inputs, strict=True), start=1
                )
            ]
        )
    )
    return True


def _source_observation_order_key(item: ReportedFinancialFact) -> tuple[datetime, str]:
    """按观测时间和来源批次稳定选择触发派生 revision 的最近证据。"""
    return item.observed_at, str(item.source_batch_id)


def _input_roles(metric: DerivedFinancialMetricInput) -> tuple[str, ...]:
    """按单季或 TTM 输入顺序返回固定角色，供 manifest 可读审计。"""
    if metric.period_basis == "SINGLE_QUARTER":
        return (
            ("CURRENT_YTD",)
            if len(metric.inputs) == 1
            else (
                "CURRENT_YTD",
                "PREVIOUS_YTD",
            )
        )
    return (
        ("CURRENT_YTD",)
        if len(metric.inputs) == 1
        else (
            "CURRENT_YTD",
            "PRIOR_ANNUAL",
            "PRIOR_SAME_QUARTER",
        )
    )


def _next_revision(
    connection: Session,
    *,
    snapshot: FinancialDerivationSnapshot,
    metric: DerivedFinancialMetricInput,
    metric_id: int,
    methodology_id: UUID,
) -> int:
    """计算同一派生逻辑键的下一个不可变 revision 序号。"""
    maximum = connection.execute(
        select(func.max(DerivedFinancialMetricRevision.revision)).where(
            DerivedFinancialMetricRevision.report_period == metric.report_period,
            DerivedFinancialMetricRevision.security_id == snapshot.security_id,
            DerivedFinancialMetricRevision.metric_id == metric_id,
            DerivedFinancialMetricRevision.methodology_id == methodology_id,
            DerivedFinancialMetricRevision.period_basis == metric.period_basis,
            DerivedFinancialMetricRevision.statement_scope == metric.statement_scope,
            DerivedFinancialMetricRevision.formula_version == metric.formula_version,
        )
    ).scalar_one()
    return 1 if maximum is None else int(maximum) + 1


def _publish(
    connection: Session,
    *,
    snapshot: FinancialDerivationSnapshot,
    methodology_id: UUID,
    metrics: Sequence[DerivedFinancialMetricInput],
    changed_count: int,
    computed_at: datetime,
) -> UUID:
    """按完整目标结果摘要复用或替换派生数据集当前指针。"""
    partition_key = f"{snapshot.security_id}:{methodology_id}"
    current_data_version = connection.execute(
        select(DatasetPublication.data_version).where(
            DatasetPublication.dataset == "financial.derived-metric",
            DatasetPublication.partition_key == partition_key,
            DatasetPublication.superseded_at.is_(None),
        )
    ).scalar_one_or_none()
    if changed_count == 0 and current_data_version is not None:
        return UUID(str(current_data_version))
    connection.execute(
        update(DatasetPublication)
        .where(
            DatasetPublication.dataset == "financial.derived-metric",
            DatasetPublication.partition_key == partition_key,
            DatasetPublication.superseded_at.is_(None),
        )
        .values(superseded_at=computed_at)
    )
    data_version = uuid4()
    content_sha256 = hashlib.sha256(
        json.dumps(
            [
                {
                    "metricCode": metric.metric_code,
                    "reportPeriod": metric.report_period.isoformat(),
                    "basis": metric.period_basis,
                    "scope": metric.statement_scope,
                    "contentSha256": metric.content_sha256,
                }
                for metric in metrics
            ],
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    effective_as_of = max(
        (metric.effective_from for metric in metrics),
        default=snapshot.effective_as_of,
    )
    connection.execute(
        insert(DatasetPublication).values(
            publication_id=uuid4(),
            dataset="financial.derived-metric",
            partition_key=partition_key,
            data_version=data_version,
            quality_status="passed",
            published_at=computed_at,
            superseded_at=None,
            effective_as_of=effective_as_of,
            knowledge_cutoff=computed_at,
        )
    )
    connection.execute(
        insert(FinancialPublication).values(
            data_version=data_version,
            capability="financial.derived-metric",
            security_id=snapshot.security_id,
            methodology_id=methodology_id,
            effective_as_of=effective_as_of,
            knowledge_cutoff=computed_at,
            row_count=len(metrics),
            content_sha256=content_sha256,
            published_at=computed_at,
        )
    )
    return data_version
