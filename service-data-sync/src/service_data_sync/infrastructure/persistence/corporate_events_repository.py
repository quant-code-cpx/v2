"""公司公告、业绩预告和业绩快报 `P0` 的原子 `canonical` 发布仓储。

发布以同批已获批准的公告文档为证据，预告区间指标与快报单值指标物理隔离。仓储按
公告来源键而非标题识别事件，因此标题修订会产生新的 `revision`，不会把不同发行人
或不同文档的指标拼成一个业绩事实。证券身份、来源权利和事实日期任一不确定即拒绝。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from service_data_sync.application.ports.canonical_release import (
    CanonicalLineageRecord,
    CanonicalQualityDecision,
    CanonicalQualityRule,
    CanonicalReleaseCandidate,
)
from service_data_sync.application.ports.corporate_events import (
    CorporateEventsRepository,
    CorporateSourceObservation,
    PublishedCorporateEvents,
)
from service_data_sync.domain.corporate import (
    DisclosureDocument as CorporateDocument,
)
from service_data_sync.domain.corporate import (
    EarningsExpressMetric,
    EarningsGuidanceMetric,
)
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.database.models.canonical import CanonicalCheckpoint
from service_data_sync.infrastructure.database.models.equity.identity.equity_identifier_version import (  # noqa: E501
    EquityIdentifierVersion,
)
from service_data_sync.infrastructure.database.models.market import (
    CorporateEarningsValue,
    CorporateEvent,
    CorporateEventRevision,
    DisclosureDocument,
)
from service_data_sync.infrastructure.persistence.canonical_release_repository import (
    SqlAlchemyCanonicalReleaseRepository,
)
from service_data_sync.infrastructure.persistence.typed_p0_support import (
    TypedP0SourceApproval,
    ensure_dataset,
    ensure_methodology,
    ensure_source_dataset,
    record_manifest,
    record_normalization_run,
    record_source_batch,
)

_DATASET = "equity.corporate_event.earnings.reported"
_METHODOLOGY = "corporate-earnings-disclosure-reported"
_MAPPING_VERSION = "corporate-earnings-events-v1"


@dataclass(frozen=True, slots=True)
class CorporateSourceApproval(TypedP0SourceApproval):
    """标识已通过公告权利、留存和内部使用审查的真实上游来源。"""


@dataclass(frozen=True, slots=True)
class _PreparedEvent:
    """封装一个公告派生事件 revision、指标集合、摘要与事实日期。"""

    event_id: UUID
    document_id: UUID
    source_event_key: str
    kind: str
    report_period: date
    public_usable_at: datetime
    source_visible_at: datetime | None
    source_time_precision: str
    metrics: tuple[EarningsGuidanceMetric | EarningsExpressMetric, ...]
    content_hash: str
    revision_no: int


class SqlAlchemyCorporateEventsRepository(CorporateEventsRepository):
    """以同批官方公告为证据发布业绩预告和快报，未知来源或证券身份一律拒绝。"""

    def __init__(
        self,
        database: DatabaseClient,
        *,
        approved_sources: Mapping[str, CorporateSourceApproval] | None = None,
    ) -> None:
        """保存事务工厂与显式来源批准表；空批准表确保生产发布 fail-closed。"""
        self._database = database
        self._approved_sources = dict(approved_sources or {})
        self._release_repository = SqlAlchemyCanonicalReleaseRepository(database)

    def publish(
        self,
        *,
        documents: Sequence[CorporateDocument],
        guidance_metrics: Sequence[EarningsGuidanceMetric],
        express_metrics: Sequence[EarningsExpressMetric],
        source: CorporateSourceObservation,
    ) -> PublishedCorporateEvents:
        """原子固化同批文档与其 P0 指标；重放不因来源对象 URI 变化制造新事件版本。"""
        document_values = tuple(documents)
        guidance_values = tuple(guidance_metrics)
        express_values = tuple(express_metrics)
        if not document_values:
            raise ValueError("corporate publication requires disclosure documents")
        if len({item.source_document_id for item in document_values}) != len(document_values):
            raise ValueError("corporate documents must be unique by source document identity")
        approval = self._approved_sources.get(source.provider_id)
        if approval is None:
            raise ValueError("corporate source provider is not approved for publication")
        prepared: list[_PreparedEvent] = []
        source_batch_id: UUID | None = None

        def prepare(session: Session) -> CanonicalReleaseCandidate:
            """在单事务内登记证据、解析文档身份并构造按来源分区的完整 release。"""
            nonlocal source_batch_id
            now = datetime.now(UTC)
            dataset_id = ensure_dataset(
                session,
                code=_DATASET,
                domain="corporate_events",
                grain="source document + earnings event + metric",
                now=now,
            )
            methodology_id = ensure_methodology(
                session,
                code=_METHODOLOGY,
                semantic_family="reported-corporate-earnings-disclosure",
                mapping_version=_MAPPING_VERSION,
                documentation_ref="docs/service-data-sync/0024-corporate-events/index.html",
            )
            source_dataset_id = ensure_source_dataset(
                session,
                approval=approval,
                capability=source.capability,
                native_grain="disclosure document + earnings metric",
            )
            source_batch_id = record_source_batch(
                session, source=source, source_dataset_id=source_dataset_id, now=now
            )
            source_id = _source_id(approval)
            documents_by_source_id = {item.source_document_id: item for item in document_values}
            _validate_metric_documents(documents_by_source_id, guidance_values, express_values)
            document_ids = _upsert_documents(
                session,
                source_id=source_id,
                documents=document_values,
                source_batch_id=source_batch_id,
            )
            groups = _event_groups(guidance_values, express_values)
            current = _current_events(
                session, source_id=source_id, methodology_version_id=methodology_id
            )
            prepared[:] = []
            for source_event_key, kind, metrics in groups:
                document = documents_by_source_id[metrics[0].source_document_id]
                document_id = document_ids[document.source_document_id]
                event_id = _ensure_event(
                    session,
                    source_id=source_id,
                    source_event_key=source_event_key,
                    security_id=_resolve_security_id(
                        session,
                        source_code=document.source_security_code,
                        fact_date=document.announced_on,
                    ),
                    event_family="EARNINGS_GUIDANCE" if kind == "GUIDANCE" else "EARNINGS_EXPRESS",
                    now=now,
                )
                event = _prepared_event(
                    event_id=event_id,
                    document_id=document_id,
                    source_event_key=source_event_key,
                    kind=kind,
                    document=document,
                    metrics=metrics,
                    revision_no=current[source_event_key].revision_no + 1
                    if source_event_key in current
                    else 1,
                )
                if (
                    source_event_key not in current
                    or event.content_hash != current[source_event_key].content_hash
                ):
                    prepared.append(event)
            partition_key = f"source:{source_id}"
            normalization_run_id = record_normalization_run(
                session,
                dataset_id=dataset_id,
                partition_key=partition_key,
                source=source,
                source_batch_id=source_batch_id,
                mapping_version=_MAPPING_VERSION,
                now=now,
            )
            return _candidate(
                session,
                dataset_id=dataset_id,
                methodology_version_id=methodology_id,
                normalization_run_id=normalization_run_id,
                partition_key=partition_key,
                current=current,
                changed={item.source_event_key: item for item in prepared},
                source_batch_id=source_batch_id,
                source_observed_at=source.observed_at,
                now=now,
            )

        def write(session: Session, candidate: CanonicalReleaseCandidate, release_id: UUID) -> None:
            """关闭变化事件的旧知识版本，并追加文档绑定的 revision 与所有指标行。"""
            if source_batch_id is None:
                raise AssertionError("corporate preparation did not resolve source batch")
            for item in prepared:
                session.execute(
                    update(CorporateEventRevision)
                    .where(
                        CorporateEventRevision.event_id == item.event_id,
                        CorporateEventRevision.methodology_version_id
                        == candidate.methodology_version_id,
                        CorporateEventRevision.known_to.is_(None),
                    )
                    .values(known_to=candidate.created_at)
                )
                revision_id = uuid4()
                session.execute(
                    insert(CorporateEventRevision).values(
                        event_revision_id=revision_id,
                        event_id=item.event_id,
                        stage="PLAN" if item.kind == "GUIDANCE" else "RESULT",
                        status=_event_status(item),
                        report_period=item.report_period,
                        event_date=None,
                        effective_date=None,
                        primary_document_id=item.document_id,
                        methodology_version_id=candidate.methodology_version_id,
                        release_id=release_id,
                        revision_no=item.revision_no,
                        source_batch_id=source_batch_id,
                        source_published_at=item.source_visible_at,
                        source_time_precision=item.source_time_precision,
                        public_usable_at=item.public_usable_at,
                        availability_basis="OFFICIAL_DISCLOSURE",
                        known_from=candidate.created_at,
                        known_to=None,
                        content_hash=item.content_hash,
                        quality_status="passed",
                    )
                )
                session.execute(
                    insert(CorporateEarningsValue).values(
                        [_metric_row(revision_id, item.kind, metric) for metric in item.metrics]
                    )
                )
                record_manifest(
                    session,
                    normalization_run_id=candidate.normalization_run_id,
                    record_key_hash=_record_key(item.source_event_key),
                    canonical_table=CorporateEventRevision.__tablename__,
                    canonical_pk={"eventRevisionId": str(revision_id)},
                    content_hash=item.content_hash,
                )

        publication = self._release_repository.publish_prepared(
            prepare_candidate=prepare, write_facts=write
        )
        return PublishedCorporateEvents(
            data_version=publication.data_version,
            inserted_count=len(prepared),
            unchanged_count=len(_event_groups(guidance_values, express_values)) - len(prepared),
        )


def _source_id(approval: CorporateSourceApproval) -> UUID:
    """从已批准真实来源代码稳定导出共享来源 UUID，避免 adapter 名称成为业务身份。"""
    return uuid5(NAMESPACE_URL, f"quant-v2:data-source:{approval.source_code}")


def _validate_metric_documents(
    documents: Mapping[str, CorporateDocument],
    guidance: Sequence[EarningsGuidanceMetric],
    express: Sequence[EarningsExpressMetric],
) -> None:
    """确保同批指标引用存在且同证券的公告，禁止跨文档或跨发行人拼接业绩事实。"""
    for metric in (*guidance, *express):
        document = documents.get(metric.source_document_id)
        if document is None or document.source_security_code != metric.source_security_code:
            raise ValueError("corporate metric document evidence is missing or has another issuer")


def _upsert_documents(
    session: Session,
    *,
    source_id: UUID,
    documents: Sequence[CorporateDocument],
    source_batch_id: UUID,
) -> dict[str, UUID]:
    """登记或更新官方文档定位与最新已验证内容摘要；事件 revision 始终保留旧证据版本。"""
    result: dict[str, UUID] = {}
    for document in documents:
        document_id = uuid5(
            NAMESPACE_URL, f"quant-v2:disclosure-document:{source_id}:{document.source_document_id}"
        )
        security_id = _resolve_security_id(
            session, source_code=document.source_security_code, fact_date=document.announced_on
        )
        published_precision = (
            document.visible_time_precision
            if document.visible_time_precision in {"EXACT", "DATE_ONLY"}
            else "UNKNOWN"
        )
        session.execute(
            pg_insert(DisclosureDocument)
            .values(
                document_id=document_id,
                source_id=source_id,
                source_batch_id=source_batch_id,
                source_document_id=document.source_document_id,
                issuer_security_id=security_id,
                title=document.title,
                document_type=document.category,
                announced_on=document.announced_on,
                published_at=document.source_visible_at,
                published_precision=published_precision,
                official_url=document.official_url,
                content_hash=document.content_sha256,
                withdrawn_at=None,
            )
            .on_conflict_do_update(
                constraint="uq_disclosure_document_source_key",
                set_={
                    "source_batch_id": source_batch_id,
                    "issuer_security_id": security_id,
                    "title": document.title,
                    "document_type": document.category,
                    "announced_on": document.announced_on,
                    "published_at": document.source_visible_at,
                    "published_precision": published_precision,
                    "official_url": document.official_url,
                    "content_hash": document.content_sha256,
                },
            )
        )
        result[document.source_document_id] = document_id
    return result


def _resolve_security_id(session: Session, *, source_code: str, fact_date: date) -> int:
    """按事实日期解析唯一已确认股票身份；公告 schema 未带交易所时歧义必须隔离。"""
    rows = (
        session.execute(
            select(EquityIdentifierVersion.security_id).where(
                EquityIdentifierVersion.symbol == source_code,
                EquityIdentifierVersion.identity_state == "CONFIRMED",
                EquityIdentifierVersion.effective_from <= fact_date,
                (EquityIdentifierVersion.effective_to.is_(None))
                | (EquityIdentifierVersion.effective_to > fact_date),
                EquityIdentifierVersion.known_to.is_(None),
            )
        )
        .scalars()
        .all()
    )
    candidates = {int(row) for row in rows}
    if len(candidates) != 1:
        raise ValueError("corporate issuer security identity is missing or ambiguous")
    return candidates.pop()


def _event_groups(
    guidance: Sequence[EarningsGuidanceMetric], express: Sequence[EarningsExpressMetric]
) -> tuple[tuple[str, str, tuple[EarningsGuidanceMetric | EarningsExpressMetric, ...]], ...]:
    """按文档和业绩类型聚合指标，单个事件 revision 不能混入预告和快报口径。"""
    grouped: dict[str, tuple[str, list[EarningsGuidanceMetric | EarningsExpressMetric]]] = {}
    for kind, metrics in (("GUIDANCE", guidance), ("EXPRESS", express)):
        for metric in metrics:
            key = f"{kind}:{metric.source_document_id}"
            existing = grouped.setdefault(key, (kind, []))
            existing[1].append(metric)
    return tuple(
        (key, kind, tuple(sorted(metrics, key=lambda item: item.metric_code)))
        for key, (kind, metrics) in sorted(grouped.items())
    )


def _ensure_event(
    session: Session,
    *,
    source_id: UUID,
    source_event_key: str,
    security_id: int,
    event_family: str,
    now: datetime,
) -> UUID:
    """幂等登记公告事件永久身份，来源文档键而非标题承担去重职责。"""
    event_id = uuid5(NAMESPACE_URL, f"quant-v2:corporate-event:{source_id}:{source_event_key}")
    session.execute(
        pg_insert(CorporateEvent)
        .values(
            event_id=event_id,
            security_id=security_id,
            event_family=event_family,
            source_id=source_id,
            source_event_key=source_event_key,
            created_at=now,
        )
        .on_conflict_do_nothing(constraint="uq_corporate_event_source_key")
    )
    return event_id


def _current_events(
    session: Session, *, source_id: UUID, methodology_version_id: UUID
) -> dict[str, CorporateEventRevision]:
    """读取该真实来源当前事件知识，来源键重复时拒绝构造不确定 release。"""
    rows = session.execute(
        select(CorporateEvent.source_event_key, CorporateEventRevision)
        .join(CorporateEventRevision, CorporateEventRevision.event_id == CorporateEvent.event_id)
        .where(
            CorporateEvent.source_id == source_id,
            CorporateEventRevision.methodology_version_id == methodology_version_id,
            CorporateEventRevision.known_to.is_(None),
        )
    ).all()
    result = {str(source_event_key): revision for source_event_key, revision in rows}
    if len(result) != len(rows):
        raise ValueError("corporate current event revision is ambiguous")
    return result


def _prepared_event(
    *,
    event_id: UUID,
    document_id: UUID,
    source_event_key: str,
    kind: str,
    document: CorporateDocument,
    metrics: tuple[EarningsGuidanceMetric | EarningsExpressMetric, ...],
    revision_no: int,
) -> _PreparedEvent:
    """将一个同文档指标组投影为不可变事件 revision，并把文档摘要纳入内容身份。"""
    report_periods = {metric.report_period for metric in metrics}
    if len(report_periods) != 1:
        raise ValueError("corporate document event cannot mix report periods")
    payload = {
        "sourceEventKey": source_event_key,
        "kind": kind,
        "documentHash": document.content_sha256,
        "documentId": str(document_id),
        "reportPeriod": next(iter(report_periods)).isoformat(),
        "metrics": [_metric_payload(kind, metric) for metric in metrics],
    }
    return _PreparedEvent(
        event_id=event_id,
        document_id=document_id,
        source_event_key=source_event_key,
        kind=kind,
        report_period=next(iter(report_periods)),
        public_usable_at=document.public_usable_at,
        source_visible_at=document.source_visible_at,
        source_time_precision=document.visible_time_precision,
        metrics=metrics,
        content_hash=_hash_payload(payload),
        revision_no=revision_no,
    )


def _event_status(item: _PreparedEvent) -> str:
    """保留预告类别或快报初步状态，不用通用成功状态抹平来源语义。"""
    first = item.metrics[0]
    return (
        first.guidance_type
        if isinstance(first, EarningsGuidanceMetric)
        else first.preliminary_status
    )


def _metric_row(
    revision_id: UUID, kind: str, metric: EarningsGuidanceMetric | EarningsExpressMetric
) -> dict[str, object]:
    """将领域指标映射为强类型持久行，预告同比区间和快报单值保持物理隔离。"""
    if isinstance(metric, EarningsGuidanceMetric):
        return {
            "event_revision_id": revision_id,
            "metric_code": metric.metric_code,
            "value_low": metric.amount_low,
            "value_high": metric.amount_high,
            "value_single": None,
            "prior_value": metric.prior_period_value,
            "currency": metric.currency,
            "amount_unit": "CNY",
            "metric_unit": "CURRENCY",
            "change_ratio": None,
            "change_ratio_low": metric.yoy_low,
            "change_ratio_high": metric.yoy_high,
            "value_kind": kind,
            "preliminary_status": None,
        }
    return {
        "event_revision_id": revision_id,
        "metric_code": metric.metric_code,
        "value_low": None,
        "value_high": None,
        "value_single": metric.current_value,
        "prior_value": metric.prior_value,
        "currency": metric.currency,
        "amount_unit": metric.unit,
        "metric_unit": metric.unit,
        "change_ratio": None,
        "change_ratio_low": None,
        "change_ratio_high": None,
        "value_kind": kind,
        "preliminary_status": metric.preliminary_status,
    }


def _metric_payload(
    kind: str, metric: EarningsGuidanceMetric | EarningsExpressMetric
) -> dict[str, object]:
    """生成指标摘要投影，保留所有领域字段以便内容变化触发 revision。"""
    if isinstance(metric, EarningsGuidanceMetric):
        return {
            "kind": kind,
            "metricCode": metric.metric_code,
            "guidanceType": metric.guidance_type,
            "amountLow": _decimal(metric.amount_low),
            "amountHigh": _decimal(metric.amount_high),
            "yoyLow": _decimal(metric.yoy_low),
            "yoyHigh": _decimal(metric.yoy_high),
            "priorPeriodValue": _decimal(metric.prior_period_value),
            "currency": metric.currency,
        }
    return {
        "kind": kind,
        "metricCode": metric.metric_code,
        "currentValue": _decimal(metric.current_value),
        "priorValue": _decimal(metric.prior_value),
        "unit": metric.unit,
        "currency": metric.currency,
        "preliminaryStatus": metric.preliminary_status,
    }


def _candidate(
    session: Session,
    *,
    dataset_id: UUID,
    methodology_version_id: UUID,
    normalization_run_id: UUID,
    partition_key: str,
    current: Mapping[str, CorporateEventRevision],
    changed: Mapping[str, _PreparedEvent],
    source_batch_id: UUID,
    source_observed_at: datetime,
    now: datetime,
) -> CanonicalReleaseCandidate:
    """合成来源分区当前事件 release，未变化事件继续引用原始来源批次与内容摘要。"""
    records: list[CanonicalLineageRecord] = []
    fact_dates: list[date] = []
    for source_event_key in sorted({*current, *changed}):
        item = changed.get(source_event_key)
        if item is None:
            existing = current[source_event_key]
            content_hash = existing.content_hash
            batch_id = UUID(str(existing.source_batch_id))
            if existing.report_period is not None:
                fact_dates.append(existing.report_period)
        else:
            content_hash = item.content_hash
            batch_id = source_batch_id
            fact_dates.append(item.report_period)
        records.append(
            CanonicalLineageRecord(
                record_key_hash=_record_key(source_event_key),
                content_hash=content_hash,
                source_batch_id=batch_id,
                transform_hash=hashlib.sha256(_MAPPING_VERSION.encode()).hexdigest(),
            )
        )
    fencing_token = session.execute(
        select(CanonicalCheckpoint.fencing_token)
        .where(
            CanonicalCheckpoint.dataset_id == dataset_id,
            CanonicalCheckpoint.partition_key == partition_key,
            CanonicalCheckpoint.checkpoint_kind == "published",
        )
        .with_for_update()
    ).scalar_one_or_none()
    return CanonicalReleaseCandidate(
        dataset_id=dataset_id,
        dataset_code=_DATASET,
        partition_key=partition_key,
        methodology_version_id=methodology_version_id,
        normalization_run_id=normalization_run_id,
        records=tuple(records),
        quality=CanonicalQualityDecision(
            status="passed",
            policy_code="corporate.earnings-disclosure.quality",
            policy_version=1,
            rules=(CanonicalQualityRule("same-batch-document-evidence", "blocking", True),),
        ),
        fact_min=min(fact_dates) if fact_dates else None,
        fact_max=max(fact_dates) if fact_dates else None,
        checkpoint_kind="published",
        checkpoint_position={"observedAt": source_observed_at.isoformat()},
        expected_fencing_token=0 if fencing_token is None else int(fencing_token),
        created_at=now,
    )


def _record_key(source_event_key: str) -> str:
    """计算来源事件键摘要，标题变化不改变事件身份但会通过内容摘要形成 revision。"""
    return hashlib.sha256(source_event_key.encode()).hexdigest()


def _hash_payload(payload: Mapping[str, object]) -> str:
    """使用规范 JSON 生成 SHA-256，Decimal 先投影为文本以防显示格式造成伪修订。"""
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _decimal(value: object) -> str | None:
    """把可选精确数值稳定投影为文本，真实零与未披露空值不会混淆。"""
    return None if value is None else str(value)
