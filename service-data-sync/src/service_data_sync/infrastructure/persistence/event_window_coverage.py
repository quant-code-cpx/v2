"""证券事件逐证券窗口 manifest publication 与覆盖证据公共实现。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import insert, or_, select, tuple_, update
from sqlalchemy.orm import Session

from service_data_sync.application.ports.canonical_release import (
    CanonicalLineageRecord,
    CanonicalQualityDecision,
    CanonicalQualityRule,
    CanonicalReleaseCandidate,
)
from service_data_sync.domain.equity import EquityIdentifier
from service_data_sync.infrastructure.database.fenced_execution import current_fenced_execution
from service_data_sync.infrastructure.database.models.canonical import CanonicalCheckpoint
from service_data_sync.infrastructure.database.models.equity.identity.equity_identifier_version import (  # noqa: E501
    EquityIdentifierVersion,
)
from service_data_sync.infrastructure.database.models.equity.identity.equity_instrument import (
    EquityInstrument,
)
from service_data_sync.infrastructure.database.models.publication.dataset_publication import (
    DatasetPublication,
)
from service_data_sync.infrastructure.database.models.publication.equity_event_window_coverage import (  # noqa: E501
    EquityEventWindowCoverage,
)
from service_data_sync.infrastructure.persistence.canonical_release_repository import (
    SqlAlchemyCanonicalReleaseRepository,
)
from service_data_sync.infrastructure.persistence.typed_p0_support import (
    TypedP0SourceObservation,
    record_normalization_run,
)

_COVERAGE_SQL_BATCH_SIZE = 5_000


class EquityWindowIdentityUnavailable(ValueError):
    """表示指定证券在完整事实窗口内没有唯一已确认身份版本。

    这不是来源、网络或数据库暂态失败。调用方必须先补齐生命周期主数据，或把请求拆分到
    单一身份版本覆盖的日期窗口，不能把当前目录身份倒灌到历史行情或事件。
    """


@dataclass(frozen=True, slots=True)
class EventCoverageIdentity:
    """冻结一次成功来源响应实际覆盖的证券身份版本与业务日期分段。"""

    security_id: int
    identifier_version_id: UUID
    exchange: str
    symbol: str
    coverage_from: date
    coverage_to: date


@dataclass(frozen=True, slots=True)
class EventCoverageRecords:
    """提供一个证券、事件族和窗口在当前知识快照中的真实事件血缘。"""

    records: tuple[CanonicalLineageRecord, ...]
    fact_dates: tuple[date, ...]


@dataclass(frozen=True, slots=True)
class PublishedEventCoverages:
    """返回本次覆盖 manifest 的聚合可见版本和真实窗口事件数。"""

    data_version: UUID
    record_count: int
    coverage_count: int


EventCoverageRecordLoader = Callable[
    [Session, Sequence[EventCoverageIdentity], str],
    Mapping[EventCoverageIdentity, EventCoverageRecords],
]


@dataclass(frozen=True, slots=True)
class _EventCoverageWrite:
    """封装一次待批量校验和写入的不可变覆盖观察。"""

    coverage_version: UUID
    dataset: str
    family: str
    identity: EventCoverageIdentity
    publication_id: UUID
    source_batch_id: UUID
    record_count: int
    coverage_scope: str
    universe_hash: str
    universe_size: int
    observed_at: datetime
    created_at: datetime


def resolve_event_coverage_identities(
    session: Session,
    *,
    start: date,
    end: date,
    identifier: EquityIdentifier | None,
) -> tuple[tuple[EventCoverageIdentity, ...], str, str]:
    """解析单证券完整身份或全市场实际身份分段，并冻结可审计 roster 摘要。

    单证券空窗没有事实日期可辅助解析，因此要求同一已确认代码版本完整覆盖闭区间。
    全市场响应则枚举所有与窗口相交的已确认身份版本，并按版本有效区间裁剪；消费者只有在
    这些分段的并集无缺口时才会声明更长请求窗口可用。
    """
    if start > end:
        raise ValueError("event coverage window is invalid")
    base = (
        select(EquityIdentifierVersion)
        .join(
            EquityInstrument,
            EquityInstrument.security_id == EquityIdentifierVersion.security_id,
        )
        .where(
            EquityIdentifierVersion.identity_state == "CONFIRMED",
            EquityIdentifierVersion.known_to.is_(None),
            EquityInstrument.master_confirmed_at.is_not(None),
            EquityInstrument.listing_status != "PENDING",
        )
    )
    if identifier is not None:
        rows = (
            session.execute(
                base.where(
                    EquityIdentifierVersion.exchange == identifier.exchange.value,
                    EquityIdentifierVersion.symbol == identifier.symbol,
                    EquityIdentifierVersion.effective_from <= start,
                    or_(
                        EquityIdentifierVersion.effective_to.is_(None),
                        EquityIdentifierVersion.effective_to > end,
                    ),
                ).with_for_update()
            )
            .scalars()
            .all()
        )
        if len(rows) != 1:
            raise EquityWindowIdentityUnavailable(
                "event instrument identity must uniquely cover the complete requested window"
            )
        row = rows[0]
        identities = (
            EventCoverageIdentity(
                security_id=int(row.security_id),
                identifier_version_id=UUID(str(row.version_id)),
                exchange=str(row.exchange),
                symbol=str(row.symbol),
                coverage_from=start,
                coverage_to=end,
            ),
        )
        scope = "INSTRUMENT"
    else:
        rows = (
            session.execute(
                base.where(
                    EquityIdentifierVersion.effective_from <= end,
                    or_(
                        EquityIdentifierVersion.effective_to.is_(None),
                        EquityIdentifierVersion.effective_to > start,
                    ),
                ).order_by(
                    EquityIdentifierVersion.security_id,
                    EquityIdentifierVersion.effective_from,
                    EquityIdentifierVersion.version_id,
                )
            )
            .scalars()
            .all()
        )
        identities = tuple(
            EventCoverageIdentity(
                security_id=int(row.security_id),
                identifier_version_id=UUID(str(row.version_id)),
                exchange=str(row.exchange),
                symbol=str(row.symbol),
                coverage_from=max(start, row.effective_from),
                coverage_to=min(
                    end,
                    (row.effective_to - timedelta(days=1) if row.effective_to is not None else end),
                ),
            )
            for row in rows
        )
        if not identities:
            raise EquityWindowIdentityUnavailable(
                "global event coverage has no confirmed identity roster"
            )
        scope = "GLOBAL"
    roster_payload = [
        {
            "securityId": item.security_id,
            "identifierVersionId": str(item.identifier_version_id),
            "exchange": item.exchange,
            "symbol": item.symbol,
            "coverageFrom": item.coverage_from.isoformat(),
            "coverageTo": item.coverage_to.isoformat(),
        }
        for item in identities
    ]
    roster_hash = _sha256(roster_payload)
    return identities, scope, roster_hash


def publish_event_window_coverages(
    session: Session,
    *,
    release_repository: SqlAlchemyCanonicalReleaseRepository,
    dataset_id: UUID,
    dataset_code: str,
    methodology_version_id: UUID,
    mapping_version: str,
    source: TypedP0SourceObservation,
    source_batch_id: UUID,
    identities: Sequence[EventCoverageIdentity],
    coverage_scope: str,
    universe_hash: str,
    families: Sequence[str],
    records_for: EventCoverageRecordLoader,
    now: datetime,
) -> PublishedEventCoverages:
    """发布逐证券、逐族、逐窗口 manifest，并在同事务写入覆盖知识版本。

    manifest release 的 `record_count` 等于窗口真实事件数，合法空窗严格为零；覆盖行不是
    canonical 事实，不会为了改变内容摘要而制造伪事件。全局累积事实 release 与这些消费者
    窗口 publication 分离，控制面只按本批窗口事实总数累计一次进度。
    """
    identity_values = tuple(identities)
    family_values = tuple(families)
    if not identity_values or not family_values:
        raise ValueError("event coverage publication requires identities and families")
    if len(set(identity_values)) != len(identity_values):
        raise ValueError("event coverage publication contains duplicate identity windows")
    coverage_versions: list[UUID] = []
    coverage_writes: list[_EventCoverageWrite] = []
    total_records = 0
    for family in family_values:
        loaded_records = records_for(session, identity_values, family)
        if set(loaded_records) != set(identity_values):
            raise ValueError("event coverage loader did not return the exact frozen roster")
        records_by_identity = tuple(
            (identity, loaded_records[identity]) for identity in identity_values
        )
        manifest_records = EventCoverageRecords(
            records=tuple(
                record
                for _identity, identity_records in records_by_identity
                for record in identity_records.records
            ),
            fact_dates=tuple(
                fact_date
                for _identity, identity_records in records_by_identity
                for fact_date in identity_records.fact_dates
            ),
        )
        partition_key = _manifest_partition_key(
            provider_id=source.provider_id,
            identities=identity_values,
            coverage_scope=coverage_scope,
            family=family,
            universe_hash=universe_hash,
        )
        normalization_run_id = record_normalization_run(
            session,
            dataset_id=dataset_id,
            partition_key=partition_key,
            source=source,
            source_batch_id=source_batch_id,
            mapping_version=f"{mapping_version}:coverage-v1",
            now=now,
        )
        candidate = _coverage_candidate(
            session,
            dataset_id=dataset_id,
            dataset_code=dataset_code,
            methodology_version_id=methodology_version_id,
            normalization_run_id=normalization_run_id,
            partition_key=partition_key,
            family=family,
            records=manifest_records,
            source=source,
            now=now,
            coverage_to=max(item.coverage_to for item in identity_values),
        )
        publication = release_repository.publish_in_session(
            session=session,
            candidate=candidate,
            record_fenced_progress=False,
        )
        publication_id = _publication_id(
            session,
            dataset=dataset_code,
            partition_key=partition_key,
            data_version=publication.data_version,
        )
        for identity, window_records in records_by_identity:
            coverage_version = _coverage_version(
                dataset=dataset_code,
                family=family,
                identity=identity,
                publication_data_version=publication.data_version,
                normalized_payload_sha256=source.normalized_payload_sha256,
                observed_at=source.observed_at,
                record_count=len(window_records.records),
                universe_hash=universe_hash,
            )
            coverage_writes.append(
                _EventCoverageWrite(
                    coverage_version=coverage_version,
                    dataset=dataset_code,
                    family=family,
                    identity=identity,
                    publication_id=publication_id,
                    source_batch_id=source_batch_id,
                    record_count=len(window_records.records),
                    coverage_scope=coverage_scope,
                    universe_hash=universe_hash,
                    universe_size=len(identity_values),
                    observed_at=source.observed_at,
                    created_at=now,
                )
            )
            coverage_versions.append(coverage_version)
            total_records += len(window_records.records)
    _record_coverages(session, values=coverage_writes)
    aggregate = uuid5(
        NAMESPACE_URL,
        "quant-v2:event-coverage-bundle:"
        + ":".join(sorted(str(value) for value in coverage_versions)),
    )
    execution = current_fenced_execution()
    if execution is not None:
        # coverage aggregate 不是消费者可读的 `dataVersion`。普通公司行动同步已由
        # `_publish` 写入真实 data-version checkpoint；回填则在全部窗口封印后由
        # `finalize_equity_event_partitions` 明确写入 event-coverage-version。
        execution.record_publication_progress(record_count=total_records)
    return PublishedEventCoverages(
        data_version=aggregate,
        record_count=total_records,
        coverage_count=len(coverage_versions),
    )


def _coverage_candidate(
    session: Session,
    *,
    dataset_id: UUID,
    dataset_code: str,
    methodology_version_id: UUID,
    normalization_run_id: UUID,
    partition_key: str,
    family: str,
    records: EventCoverageRecords,
    source: TypedP0SourceObservation,
    now: datetime,
    coverage_to: date,
) -> CanonicalReleaseCandidate:
    """构造窗口 manifest release；空 records 仍形成 record_count 为零的真实 publication。"""
    fencing_token = session.execute(
        select(CanonicalCheckpoint.fencing_token)
        .where(
            CanonicalCheckpoint.dataset_id == dataset_id,
            CanonicalCheckpoint.partition_key == partition_key,
            CanonicalCheckpoint.checkpoint_kind == "event-window",
        )
        .with_for_update()
    ).scalar_one_or_none()
    return CanonicalReleaseCandidate(
        dataset_id=dataset_id,
        dataset_code=dataset_code,
        partition_key=partition_key,
        methodology_version_id=methodology_version_id,
        normalization_run_id=normalization_run_id,
        records=records.records,
        quality=CanonicalQualityDecision(
            status="passed",
            policy_code=f"{dataset_code}.window-coverage",
            policy_version=1,
            rules=(CanonicalQualityRule(f"{family.casefold()}-complete-window", "blocking", True),),
        ),
        fact_min=min(records.fact_dates) if records.fact_dates else None,
        fact_max=max(records.fact_dates) if records.fact_dates else None,
        checkpoint_kind="event-window",
        checkpoint_position={
            "coverageTo": coverage_to.isoformat(),
            "observedAt": source.observed_at.isoformat(),
        },
        expected_fencing_token=0 if fencing_token is None else int(fencing_token),
        created_at=now,
        publication_effective_as_of=coverage_to,
    )


def _record_coverages(session: Session, *, values: Sequence[_EventCoverageWrite]) -> None:
    """按稳定业务键分块追加 coverage，限制单条 SQL 参数、锁集合和驱动批次。"""
    pending_values = tuple(sorted(values, key=_coverage_sort_key))
    if not pending_values:
        return
    if len({value.created_at for value in pending_values}) > 1:
        raise ValueError("event coverage batch must share one knowledge timestamp")
    versions = [value.coverage_version for value in pending_values]
    if len(set(versions)) != len(versions):
        raise ValueError("event coverage batch contains duplicate immutable versions")
    replay_rows: list[EquityEventWindowCoverage] = []
    for version_batch in _chunks(versions):
        replay_rows.extend(
            session.execute(
                select(EquityEventWindowCoverage).where(
                    EquityEventWindowCoverage.coverage_version.in_(version_batch)
                )
            )
            .scalars()
            .all()
        )
    replay_by_version = {UUID(str(row.coverage_version)): row for row in replay_rows}
    if len(replay_by_version) != len(replay_rows):
        raise ValueError("event coverage immutable version is ambiguous")

    insert_values: list[_EventCoverageWrite] = []
    for value in pending_values:
        replay = replay_by_version.get(value.coverage_version)
        if replay is None:
            insert_values.append(value)
            continue
        if not _same_coverage_observation(replay, value):
            raise ValueError("event coverage replay conflicts with immutable observation")
    if not insert_values:
        return

    dataset_families = sorted({(value.dataset, value.family) for value in insert_values})
    security_ids = sorted({value.identity.security_id for value in insert_values})
    coverage_from_values = [value.identity.coverage_from for value in insert_values]
    coverage_to_values = [value.identity.coverage_to for value in insert_values]
    current_rows: list[EquityEventWindowCoverage] = []
    for security_batch in _chunks(security_ids):
        current_rows.extend(
            session.execute(
                select(EquityEventWindowCoverage)
                .where(
                    tuple_(
                        EquityEventWindowCoverage.dataset,
                        EquityEventWindowCoverage.event_family,
                    ).in_(dataset_families),
                    EquityEventWindowCoverage.security_id.in_(security_batch),
                    EquityEventWindowCoverage.coverage_from >= min(coverage_from_values),
                    EquityEventWindowCoverage.coverage_from <= max(coverage_from_values),
                    EquityEventWindowCoverage.coverage_to >= min(coverage_to_values),
                    EquityEventWindowCoverage.coverage_to <= max(coverage_to_values),
                    EquityEventWindowCoverage.superseded_at.is_(None),
                )
                .with_for_update()
            )
            .scalars()
            .all()
        )
    current_by_key = {
        (
            row.dataset,
            row.event_family,
            int(row.security_id),
            row.coverage_from,
            row.coverage_to,
        ): row
        for row in current_rows
    }
    if len(current_by_key) != len(current_rows):
        raise ValueError("event coverage current window is ambiguous")

    superseded_ids: list[UUID] = []
    for value in insert_values:
        key = (
            value.dataset,
            value.family,
            value.identity.security_id,
            value.identity.coverage_from,
            value.identity.coverage_to,
        )
        current = current_by_key.get(key)
        if current is None:
            continue
        if current.created_at > value.created_at:
            raise ValueError("event coverage observation is older than current knowledge")
        if current.observed_at == value.observed_at:
            raise ValueError("event coverage observation time has conflicting content")
        superseded_ids.append(UUID(str(current.coverage_id)))
    for superseded_batch in _chunks(sorted(superseded_ids, key=str)):
        session.execute(
            update(EquityEventWindowCoverage)
            .where(EquityEventWindowCoverage.coverage_id.in_(superseded_batch))
            .values(superseded_at=insert_values[0].created_at)
        )
    for insert_batch in _chunks(insert_values):
        session.execute(
            insert(EquityEventWindowCoverage),
            [_coverage_insert_row(value) for value in insert_batch],
        )


def _coverage_sort_key(value: _EventCoverageWrite) -> tuple[object, ...]:
    """生成跨 worker 一致的锁定与插入顺序，降低并发全市场回填死锁概率。"""
    return (
        value.dataset,
        value.family,
        value.identity.security_id,
        value.identity.coverage_from,
        value.identity.coverage_to,
        str(value.coverage_version),
    )


def _chunks[CoverageValue](
    values: Sequence[CoverageValue],
) -> Iterator[tuple[CoverageValue, ...]]:
    """以固定上限切分 SQL 参数和驱动批次，规模增长不会形成无界单语句。"""
    for offset in range(0, len(values), _COVERAGE_SQL_BATCH_SIZE):
        yield tuple(values[offset : offset + _COVERAGE_SQL_BATCH_SIZE])


def _same_coverage_observation(
    replay: EquityEventWindowCoverage,
    value: _EventCoverageWrite,
) -> bool:
    """比较重放行的全部不可变业务字段，避免 UUID 碰撞静默覆盖语义。"""
    return (
        replay.observed_at == value.observed_at
        and replay.dataset == value.dataset
        and replay.event_family == value.family
        and replay.security_id == value.identity.security_id
        and replay.identifier_version_id == value.identity.identifier_version_id
        and replay.coverage_from == value.identity.coverage_from
        and replay.coverage_to == value.identity.coverage_to
        and replay.publication_id == value.publication_id
        and replay.record_count == value.record_count
        and replay.coverage_scope == value.coverage_scope
        and replay.universe_hash == value.universe_hash
        and replay.universe_size == value.universe_size
    )


def _coverage_insert_row(value: _EventCoverageWrite) -> dict[str, object]:
    """将覆盖观察投影为批量插入行，所有行共享同一事务知识时点。"""
    return {
        "coverage_id": uuid4(),
        "coverage_version": value.coverage_version,
        "dataset": value.dataset,
        "event_family": value.family,
        "security_id": value.identity.security_id,
        "identifier_version_id": value.identity.identifier_version_id,
        "coverage_from": value.identity.coverage_from,
        "coverage_to": value.identity.coverage_to,
        "publication_id": value.publication_id,
        "source_batch_id": value.source_batch_id,
        "record_count": value.record_count,
        "coverage_scope": value.coverage_scope,
        "universe_hash": value.universe_hash,
        "universe_size": value.universe_size,
        "observed_at": value.observed_at,
        "created_at": value.created_at,
        "superseded_at": None,
    }


def _publication_id(
    session: Session,
    *,
    dataset: str,
    partition_key: str,
    data_version: UUID,
) -> UUID:
    """从已选 manifest dataVersion 取得精确 publication 主键，禁止关联任意全局版本。"""
    return UUID(
        str(
            session.execute(
                select(DatasetPublication.publication_id).where(
                    DatasetPublication.dataset == dataset,
                    DatasetPublication.partition_key == partition_key,
                    DatasetPublication.data_version == data_version,
                )
            ).scalar_one()
        )
    )


def _manifest_partition_key(
    *,
    provider_id: str,
    identities: Sequence[EventCoverageIdentity],
    coverage_scope: str,
    family: str,
    universe_hash: str,
) -> str:
    """生成受长度约束的 manifest 分区；全市场每族只创建一个 publication。"""
    provider_hash = hashlib.sha256(provider_id.encode()).hexdigest()[:16]
    start = min(item.coverage_from for item in identities)
    end = max(item.coverage_to for item in identities)
    entity = (
        f"security:{identities[0].security_id}"
        if coverage_scope == "INSTRUMENT"
        else f"global:{universe_hash[:16]}"
    )
    return (
        f"{entity}:family:{family}:window:{start.isoformat()}:{end.isoformat()}:"
        f"provider:{provider_hash}"
    )


def _coverage_version(
    *,
    dataset: str,
    family: str,
    identity: EventCoverageIdentity,
    publication_data_version: UUID,
    normalized_payload_sha256: str,
    observed_at: datetime,
    record_count: int,
    universe_hash: str,
) -> UUID:
    """稳定生成覆盖知识版本；相同来源观察重放复用，新增证券或窗口必然改变版本。"""
    payload = {
        "dataset": dataset,
        "family": family,
        "securityId": identity.security_id,
        "identifierVersionId": str(identity.identifier_version_id),
        "coverageFrom": identity.coverage_from.isoformat(),
        "coverageTo": identity.coverage_to.isoformat(),
        "publicationDataVersion": str(publication_data_version),
        "normalizedPayloadSha256": normalized_payload_sha256,
        "observedAt": observed_at.isoformat(),
        "recordCount": record_count,
        "universeHash": universe_hash,
    }
    return uuid5(NAMESPACE_URL, f"quant-v2:event-coverage:{_sha256(payload)}")


def _sha256(value: object) -> str:
    """对规范 JSON 计算 SHA-256，确保 roster 和版本身份不依赖数据库返回顺序。"""
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
