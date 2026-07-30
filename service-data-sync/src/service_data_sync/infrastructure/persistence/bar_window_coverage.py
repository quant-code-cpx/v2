"""个股日、周、月行情窗口 publication 与不可变覆盖证据。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import func, insert, select, update
from sqlalchemy.orm import Session

from service_data_sync.application.ports.market_data import EquitySourceObservation
from service_data_sync.domain.equity import EquityBarPeriod, EquityIdentifier
from service_data_sync.infrastructure.database.fenced_execution import current_fenced_execution
from service_data_sync.infrastructure.database.models.provenance.source_batch import SourceBatch
from service_data_sync.infrastructure.database.models.publication import (
    equity_bar_window_coverage as bar_coverage_model,
)
from service_data_sync.infrastructure.database.models.publication.dataset_publication import (
    DatasetPublication,
)
from service_data_sync.infrastructure.persistence.canonical_release_repository import (
    SqlAlchemyCanonicalReleaseRepository,
)
from service_data_sync.infrastructure.persistence.event_window_coverage import (
    resolve_event_coverage_identities,
)
from service_data_sync.infrastructure.persistence.legacy_canonical_release_bridge import (
    publish_legacy_snapshot,
)

_MAPPING_VERSION = "equity-bar-window-coverage-v1"
_EquityBarWindowCoverage = bar_coverage_model.EquityBarWindowCoverage


@dataclass(frozen=True, slots=True)
class BarWindowIdentity:
    """冻结一个请求闭区间内唯一已确认的证券代码身份版本。"""

    security_id: int
    identifier_version_id: UUID
    exchange: str
    symbol: str
    coverage_from: date
    coverage_to: date
    identity_hash: str
    universe_hash: str


@dataclass(frozen=True, slots=True)
class PublishedBarWindowCoverage:
    """返回数据或合法空窗 publication 及其精确来源覆盖版本。"""

    data_version: UUID
    coverage_version: UUID
    source_batch_id: UUID
    publication_kind: str
    record_count: int


def resolve_bar_window_identity(
    session: Session,
    *,
    period: EquityBarPeriod,
    identifier: EquityIdentifier,
    start: date,
    end: date,
) -> BarWindowIdentity:
    """要求一个确认身份版本完整覆盖请求闭区间，代码复用窗口必须先拆分。"""
    _lock_window(
        session,
        period=period,
        exchange=identifier.exchange.value,
        symbol=identifier.symbol,
        start=start,
        end=end,
    )
    identities, coverage_scope, universe_hash = resolve_event_coverage_identities(
        session,
        start=start,
        end=end,
        identifier=identifier,
    )
    if coverage_scope != "INSTRUMENT" or len(identities) != 1:
        raise ValueError("bar window must resolve to one confirmed instrument identity")
    identity = identities[0]
    return BarWindowIdentity(
        security_id=identity.security_id,
        identifier_version_id=identity.identifier_version_id,
        exchange=identity.exchange,
        symbol=identity.symbol,
        coverage_from=identity.coverage_from,
        coverage_to=identity.coverage_to,
        identity_hash=_sha256(
            {
                "securityId": identity.security_id,
                "identifierVersionId": str(identity.identifier_version_id),
                "exchange": identity.exchange,
                "symbol": identity.symbol,
            }
        ),
        universe_hash=universe_hash,
    )


def publish_bar_window_coverage(
    session: Session,
    *,
    release_repository: SqlAlchemyCanonicalReleaseRepository,
    period: EquityBarPeriod,
    identity: BarWindowIdentity,
    source: EquitySourceObservation,
    source_batch_id: UUID,
    record_count: int,
    data_publication_version: UUID | None,
    now: datetime,
) -> PublishedBarWindowCoverage:
    """关联非空 DATA publication，或创建通过质量门的零记录 coverage publication。

    `data_publication_version` 和 `record_count` 必须同时表达非空或合法空响应，不能把来源失败
    冒充零记录。coverage 行与 publication、SourceBatch、身份版本在调用方同一事务提交。
    """
    if source.observed_at.tzinfo is None or now.tzinfo is None:
        raise ValueError("bar coverage timestamps must include a timezone")
    if source.capability != period.capability:
        raise ValueError("bar coverage source capability does not match period")
    if record_count < 0:
        raise ValueError("bar coverage record count must be non-negative")
    if (record_count == 0) != (data_publication_version is None):
        raise ValueError("bar coverage publication kind does not match record count")
    _validate_source_batch(
        session,
        source=source,
        source_batch_id=source_batch_id,
    )
    if data_publication_version is None:
        publication = publish_legacy_snapshot(
            session,
            release_repository=release_repository,
            dataset_code=period.capability,
            partition_key=_zero_partition_key(
                identity=identity,
                period=period,
                provider_id=source.provider_id,
            ),
            domain="equity",
            grain="equity security + inclusive bar request window",
            semantic_family="reported-equity-bar-window-coverage",
            mapping_version=_MAPPING_VERSION,
            source_batch_id=source_batch_id,
            records=(),
            fact_min=None,
            fact_max=None,
            now=now,
            quality_status="passed",
            publication_effective_as_of=identity.coverage_to,
        )
        data_version = publication.data_version
        publication_kind = "ZERO_RECORD_COVERAGE"
    else:
        data_version = data_publication_version
        publication_kind = "DATA"
    publication_id = _publication_id(
        session,
        capability=period.capability,
        data_version=data_version,
        expected_security_id=(identity.security_id if publication_kind == "DATA" else None),
    )
    coverage_version = _coverage_version(
        period=period,
        identity=identity,
        source=source,
        publication_data_version=data_version,
        publication_kind=publication_kind,
        record_count=record_count,
    )
    recorded_source_batch_id = _record_coverage(
        session,
        coverage_version=coverage_version,
        period=period,
        identity=identity,
        publication_id=publication_id,
        source_batch_id=source_batch_id,
        publication_kind=publication_kind,
        record_count=record_count,
        observed_at=source.observed_at,
        created_at=now,
    )
    execution = current_fenced_execution()
    if execution is not None:
        # 精确重放会复用旧 coverage/sourceBatch；把该不可变血缘也加入本次执行，避免终态
        # 校验误把“新抓取批次”和“实际被复用的覆盖批次”混为同一件事。
        if recorded_source_batch_id not in execution.source_batch_ids:
            execution.record_source_batch(recorded_source_batch_id)
        # 覆盖版本是回填 child 的终态证据；publication dataVersion 仍可由 coverage 外键精确追溯。
        execution.record_checkpoint(
            kind="bar-coverage-version",
            position=str(coverage_version),
        )
    return PublishedBarWindowCoverage(
        data_version=data_version,
        coverage_version=coverage_version,
        source_batch_id=recorded_source_batch_id,
        publication_kind=publication_kind,
        record_count=record_count,
    )


def _validate_source_batch(
    session: Session,
    *,
    source: EquitySourceObservation,
    source_batch_id: UUID,
) -> None:
    """核对覆盖引用的确为本次完整来源观察，阻断任意 SourceBatch 拼接。"""
    row = (
        session.execute(
            select(
                SourceBatch.provider_id,
                SourceBatch.capability,
                SourceBatch.payload_sha256,
                SourceBatch.raw_uri,
                SourceBatch.observed_at,
                SourceBatch.upstream_source,
                SourceBatch.adapter_version,
                SourceBatch.schema_fingerprint,
            ).where(SourceBatch.source_batch_id == source_batch_id)
        )
        .mappings()
        .one_or_none()
    )
    expected = {
        "provider_id": source.provider_id,
        "capability": source.capability,
        "payload_sha256": source.raw_payload_sha256,
        "raw_uri": source.raw_uri,
        "observed_at": source.observed_at,
        "upstream_source": source.upstream_source,
        "adapter_version": source.adapter_version,
        "schema_fingerprint": source.schema_fingerprint,
    }
    if row is None or any(row[key] != value for key, value in expected.items()):
        raise ValueError("bar coverage source batch does not match exact source observation")


def _publication_id(
    session: Session,
    *,
    capability: str,
    data_version: UUID,
    expected_security_id: int | None,
) -> UUID:
    """读取精确 passed publication，并校验 DATA 分区确属目标证券。"""
    row = (
        session.execute(
            select(
                DatasetPublication.publication_id,
                DatasetPublication.partition_key,
                DatasetPublication.release_id,
                DatasetPublication.quality_status,
            ).where(
                DatasetPublication.dataset == capability,
                DatasetPublication.data_version == data_version,
            )
        )
        .mappings()
        .one()
    )
    if row["release_id"] is None or row["quality_status"] != "passed":
        raise ValueError("bar coverage requires one passed immutable canonical publication")
    if expected_security_id is not None and row["partition_key"] != (
        f"security:{expected_security_id}"
    ):
        raise ValueError("bar DATA publication does not belong to the covered security")
    return UUID(str(row["publication_id"]))


def _record_coverage(
    session: Session,
    *,
    coverage_version: UUID,
    period: EquityBarPeriod,
    identity: BarWindowIdentity,
    publication_id: UUID,
    source_batch_id: UUID,
    publication_kind: str,
    record_count: int,
    observed_at: datetime,
    created_at: datetime,
) -> UUID:
    """追加或幂等复用一个窗口覆盖；更新观察只关闭旧 current 行。"""
    # helper 可能被独立复用；重复获取同一事务级锁无副作用，并保证并发重放不撞唯一键。
    _lock_window(
        session,
        period=period,
        exchange=identity.exchange,
        symbol=identity.symbol,
        start=identity.coverage_from,
        end=identity.coverage_to,
    )
    replay = session.execute(
        select(_EquityBarWindowCoverage).where(
            _EquityBarWindowCoverage.coverage_version == coverage_version
        )
    ).scalar_one_or_none()
    if replay is not None:
        if not _same_coverage(
            replay,
            period=period,
            identity=identity,
            publication_id=publication_id,
            publication_kind=publication_kind,
            record_count=record_count,
            observed_at=observed_at,
        ):
            raise ValueError("bar coverage replay conflicts with immutable observation")
        return UUID(str(replay.source_batch_id))
    current = (
        session.execute(
            select(_EquityBarWindowCoverage)
            .where(
                _EquityBarWindowCoverage.capability == period.capability,
                _EquityBarWindowCoverage.security_id == identity.security_id,
                _EquityBarWindowCoverage.coverage_from == identity.coverage_from,
                _EquityBarWindowCoverage.coverage_to == identity.coverage_to,
                _EquityBarWindowCoverage.superseded_at.is_(None),
            )
            .with_for_update()
        )
        .scalars()
        .one_or_none()
    )
    if current is not None:
        if current.created_at > created_at:
            raise ValueError("bar coverage observation is older than current knowledge")
        if current.observed_at > observed_at:
            raise ValueError("bar coverage source observation regresses current knowledge")
        if current.observed_at == observed_at:
            raise ValueError("bar coverage observation time has conflicting content")
        session.execute(
            update(_EquityBarWindowCoverage)
            .where(_EquityBarWindowCoverage.coverage_id == current.coverage_id)
            .values(superseded_at=created_at)
        )
    session.execute(
        insert(_EquityBarWindowCoverage).values(
            coverage_id=uuid4(),
            coverage_version=coverage_version,
            period=period.value,
            capability=period.capability,
            security_id=identity.security_id,
            identifier_version_id=identity.identifier_version_id,
            coverage_from=identity.coverage_from,
            coverage_to=identity.coverage_to,
            publication_id=publication_id,
            source_batch_id=source_batch_id,
            publication_kind=publication_kind,
            quality_status="passed",
            record_count=record_count,
            identity_hash=identity.identity_hash,
            universe_hash=identity.universe_hash,
            universe_size=1,
            observed_at=observed_at,
            created_at=created_at,
            superseded_at=None,
        )
    )
    return source_batch_id


def _same_coverage(
    replay: bar_coverage_model.EquityBarWindowCoverage,
    *,
    period: EquityBarPeriod,
    identity: BarWindowIdentity,
    publication_id: UUID,
    publication_kind: str,
    record_count: int,
    observed_at: datetime,
) -> bool:
    """比较全部不可变业务字段；重放的新 SourceBatch 只作为额外抓取审计保留。"""
    return (
        replay.period == period.value
        and replay.capability == period.capability
        and replay.security_id == identity.security_id
        and replay.identifier_version_id == identity.identifier_version_id
        and replay.coverage_from == identity.coverage_from
        and replay.coverage_to == identity.coverage_to
        and replay.publication_id == publication_id
        and replay.publication_kind == publication_kind
        and replay.quality_status == "passed"
        and replay.record_count == record_count
        and replay.identity_hash == identity.identity_hash
        and replay.universe_hash == identity.universe_hash
        and replay.universe_size == 1
        and replay.observed_at == observed_at
    )


def _coverage_version(
    *,
    period: EquityBarPeriod,
    identity: BarWindowIdentity,
    source: EquitySourceObservation,
    publication_data_version: UUID,
    publication_kind: str,
    record_count: int,
) -> UUID:
    """稳定生成覆盖版本；相同来源观察重放复用，任何语义或来源漂移都会改变。"""
    payload = {
        "period": period.value,
        "capability": period.capability,
        "securityId": identity.security_id,
        "identifierVersionId": str(identity.identifier_version_id),
        "coverageFrom": identity.coverage_from.isoformat(),
        "coverageTo": identity.coverage_to.isoformat(),
        "publicationDataVersion": str(publication_data_version),
        "publicationKind": publication_kind,
        "recordCount": record_count,
        "identityHash": identity.identity_hash,
        "universeHash": identity.universe_hash,
        "providerId": source.provider_id,
        "upstreamSource": source.upstream_source,
        "adapterVersion": source.adapter_version,
        "schemaFingerprint": source.schema_fingerprint,
        "rawPayloadSha256": source.raw_payload_sha256,
        "normalizedPayloadSha256": source.normalized_payload_sha256,
        "observedAt": source.observed_at.astimezone(UTC).isoformat(),
    }
    return uuid5(NAMESPACE_URL, f"quant-v2:bar-coverage:{_sha256(payload)}")


def _zero_partition_key(
    *,
    identity: BarWindowIdentity,
    period: EquityBarPeriod,
    provider_id: str,
) -> str:
    """生成不会与消费者 DATA 指针冲突、长度有界的零记录窗口分区。"""
    provider_hash = hashlib.sha256(provider_id.encode()).hexdigest()[:16]
    return (
        f"security:{identity.security_id}:period:{period.value}:window:"
        f"{identity.coverage_from.isoformat()}:{identity.coverage_to.isoformat()}:"
        f"provider:{provider_hash}"
    )


def _lock_window(
    session: Session,
    *,
    period: EquityBarPeriod,
    exchange: str,
    symbol: str,
    start: date,
    end: date,
) -> None:
    """按证券代码、周期和闭区间取得事务级锁，串行化同一窗口的并发发布。"""
    payload = (
        f"quant-v2:bar-window:{exchange}:{symbol}:{period.value}:"
        f"{start.isoformat()}:{end.isoformat()}"
    )
    lock_key = int.from_bytes(
        hashlib.sha256(payload.encode()).digest()[:8],
        byteorder="big",
        signed=True,
    )
    session.execute(select(func.pg_advisory_xact_lock(lock_key))).scalar_one()


def _sha256(value: object) -> str:
    """对规范 JSON 计算稳定 SHA-256。"""
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
