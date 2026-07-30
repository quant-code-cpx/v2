"""持久化并查询互联互通 exact/latest readiness 的权威证据。"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Literal, Protocol
from uuid import UUID

from sqlalchemy import select

from service_data_sync.application.ports.data_source import ProviderPreflightComponent
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.database.models.market import (
    StockConnectBundlePublication,
    StockConnectOverviewPublication,
)
from service_data_sync.infrastructure.database.models.market.stock_connect_readiness import (
    StockConnectReadinessCalendarDay,
    StockConnectReadinessSnapshot,
)
from service_data_sync.infrastructure.database.models.operations import (
    DataOperationPartition,
    DataOperationRun,
)

_SNAPSHOT_SCHEMA = "quant-v2.stock-connect-readiness-snapshot.v1"
_RESPONSE_SCHEMA = "quant-v2.stock-connect-readiness.v1"
_EVIDENCE_SCHEMA = "quant-v2.stock-connect-readiness-evidence.v1"
_CHANNELS = {
    "SH_NORTHBOUND": ("SH", "NORTHBOUND"),
    "SZ_NORTHBOUND": ("SZ", "NORTHBOUND"),
    "SH_SOUTHBOUND": ("SH", "SOUTHBOUND"),
    "SZ_SOUTHBOUND": ("SZ", "SOUTHBOUND"),
}
_REASONS = {
    "BUNDLE_PUBLISHED",
    "OFFICIAL_CALENDAR_CLOSED",
    "CALENDAR_EVIDENCE_MISSING",
    "CALENDAR_SOURCE_MISSING",
    "DELIVERY_ENTITLEMENT_MISSING",
    "DELIVERY_OBJECT_MISSING",
    "STATUS_SOURCE_MISSING",
    "PREFLIGHT_PENDING",
    "PREFLIGHT_FAILED",
    "COMMAND_NOT_SUBMITTED",
    "EXECUTION_PENDING",
    "EXECUTION_SOURCE_MISSING",
    "EXECUTION_FAILED",
    "PUBLICATION_INCOMPLETE",
}
type _SnapshotStatus = Literal["PENDING", "FAILED", "SOURCE_MISSING"]


class StockConnectReadinessNotObserved(LookupError):
    """表示所选通道还没有任何可追溯 readiness 尝试。"""


@dataclass(frozen=True, slots=True)
class StockConnectReadinessResult:
    """返回 readiness 正文与必须写入响应头的表示版本。"""

    body: dict[str, object]
    data_version: str


@dataclass(frozen=True, slots=True)
class StockConnectReadinessProbeOutcome:
    """把细粒度 provider checks 收敛为稳定、无敏感信息的 snapshot 终态。"""

    status: _SnapshotStatus
    reason_code: str
    detail: str


class StockConnectReadinessRepository(Protocol):
    """声明控制面开始和单向终结 readiness snapshot 所需的最小写端口。"""

    def begin(
        self,
        *,
        snapshot_id: UUID,
        request_hash: str,
        selected_channels: Sequence[str],
        observed_at: datetime,
    ) -> None:
        """在远端探针前写入 PROBING。"""
        ...

    def finish(
        self,
        *,
        snapshot_id: UUID,
        outcome: StockConnectReadinessProbeOutcome,
        evidence: Mapping[str, object] | None,
        manifest_id: UUID | None,
        completed_at: datetime,
        request_hash: str,
    ) -> None:
        """写入日历证据并把 snapshot 推进为唯一终态。"""
        ...


@dataclass(frozen=True, slots=True)
class _CalendarEvidenceDay:
    """保存一条经过严格 schema 校验的 provider readiness 日历证据。"""

    calendar_date: date
    channel: str
    direction: str
    calendar_state: str
    source_file_sha256: str | None
    source_publication_at: datetime | None
    publication_availability: str
    source_observed_at: datetime | None


@dataclass(frozen=True, slots=True)
class _ParsedCalendarEvidence:
    """保存一份完整 readiness evidence 的版本、观察时间与逐日行。"""

    calendar_data_version: str
    calendar_manifest_sha256: str
    evidence_observed_at: datetime
    days: tuple[_CalendarEvidenceDay, ...]


def stock_connect_readiness_probe_outcome(
    components: Sequence[ProviderPreflightComponent],
) -> StockConnectReadinessProbeOutcome:
    """把拒绝组件按固定优先级映射为公开 readiness 状态和原因。"""
    rejected = tuple(item for item in components if not item.accepted)
    if not rejected:
        return StockConnectReadinessProbeOutcome(
            status="PENDING",
            reason_code="COMMAND_NOT_SUBMITTED",
            detail="Official deliveries are verified and publication has not completed",
        )
    if any(item.component == "hkex-calendar-https" for item in rejected):
        return _probe_outcome("SOURCE_MISSING", "CALENDAR_SOURCE_MISSING")
    if any(item.component == "hkex-sftp-entitlement-manifest" for item in rejected):
        return _probe_outcome("SOURCE_MISSING", "DELIVERY_ENTITLEMENT_MISSING")
    if any(
        item.component.startswith("status-")
        or item.component == "stock-connect-status-boundary-lock"
        for item in rejected
    ):
        return _probe_outcome("SOURCE_MISSING", "STATUS_SOURCE_MISSING")
    if any(
        item.component == "sftp-authentication"
        or item.component.startswith("hkex-daily-statistics")
        or item.component == "hkex-securities-master-deliveries"
        for item in rejected
    ):
        return _probe_outcome("SOURCE_MISSING", "DELIVERY_OBJECT_MISSING")
    return _probe_outcome("FAILED", "PREFLIGHT_FAILED")


class SqlAlchemyStockConnectReadinessRepository:
    """以先落盘探针尝试、后单向终结的方式提供 readiness 读写边界。"""

    def __init__(self, database: DatabaseClient) -> None:
        """保存同步服务自有数据库；查询和状态转换均使用短事务。"""
        self._database = database

    def begin(
        self,
        *,
        snapshot_id: UUID,
        request_hash: str,
        selected_channels: Sequence[str],
        observed_at: datetime,
    ) -> None:
        """在任何远端探针前持久化 PROBING，防止失败尝试完全消失。"""
        channels = _selected_channels(selected_channels)
        _require_sha256(request_hash, "readiness request")
        observed_at = _aware(observed_at, "readiness begin")
        with self._database.transaction() as session:
            session.add(
                StockConnectReadinessSnapshot(
                    snapshot_id=snapshot_id,
                    schema_version=_SNAPSHOT_SCHEMA,
                    request_hash=request_hash,
                    selected_channel_set=",".join(channels),
                    status="PROBING",
                    reason_code="PREFLIGHT_PENDING",
                    manifest_id=None,
                    calendar_data_version=None,
                    calendar_manifest_sha256=None,
                    failure_detail=None,
                    created_at=observed_at,
                    completed_at=None,
                )
            )

    def finish(
        self,
        *,
        snapshot_id: UUID,
        outcome: StockConnectReadinessProbeOutcome,
        evidence: Mapping[str, object] | None,
        manifest_id: UUID | None,
        completed_at: datetime,
        request_hash: str,
    ) -> None:
        """一次性写入逐日证据并把 PROBING 单向推进为待执行或失败终态。"""
        if outcome.reason_code not in _REASONS:
            raise ValueError("stock-connect readiness reason is unsupported")
        completed_at = _aware(completed_at, "readiness completion")
        request_hash = _require_sha256(request_hash, "readiness request")
        parsed = (
            _missing_calendar_evidence(
                request_hash=request_hash,
                evidence_observed_at=completed_at,
            )
            if evidence is None
            else _parse_calendar_evidence(evidence)
        )
        if outcome.status == "PENDING" and manifest_id is None:
            raise ValueError("pending stock-connect readiness requires a delivery manifest")
        if outcome.status != "PENDING" and manifest_id is not None:
            raise ValueError("failed stock-connect readiness cannot bind a delivery manifest")
        with self._database.transaction() as session:
            snapshot = session.get(
                StockConnectReadinessSnapshot,
                snapshot_id,
                with_for_update=True,
            )
            if snapshot is None or snapshot.status != "PROBING":
                raise ValueError("stock-connect readiness snapshot cannot be completed")
            if snapshot.request_hash != request_hash:
                raise ValueError("stock-connect readiness request hash differs")
            allowed = set(snapshot.selected_channel_set.split(","))
            evidence_channels = {f"{item.channel}_{item.direction}" for item in parsed.days}
            if not evidence_channels.issubset(allowed):
                raise ValueError("stock-connect readiness evidence exceeds requested channels")
            if parsed.days and evidence_channels != allowed:
                raise ValueError("stock-connect readiness evidence omits requested channels")
            session.add_all(
                [
                    StockConnectReadinessCalendarDay(
                        snapshot_id=snapshot_id,
                        calendar_date=item.calendar_date,
                        channel=item.channel,
                        direction=item.direction,
                        calendar_state=item.calendar_state,
                        source_file_sha256=item.source_file_sha256,
                        source_publication_at=item.source_publication_at,
                        publication_availability=item.publication_availability,
                        source_observed_at=item.source_observed_at,
                        evidence_observed_at=parsed.evidence_observed_at,
                    )
                    for item in parsed.days
                ]
            )
            snapshot.status = outcome.status
            snapshot.reason_code = outcome.reason_code
            snapshot.manifest_id = manifest_id
            snapshot.calendar_data_version = parsed.calendar_data_version
            snapshot.calendar_manifest_sha256 = parsed.calendar_manifest_sha256
            snapshot.failure_detail = outcome.detail if outcome.status != "PENDING" else None
            snapshot.completed_at = completed_at

    def query(
        self,
        *,
        mode: str,
        exact_date: date | None,
        selected_channels: Sequence[str],
    ) -> StockConnectReadinessResult:
        """按持久化日历、run/partition 与 bundle 生成 exact 或 latest readiness。"""
        channels = _selected_channels(selected_channels)
        if (mode == "EXACT") != (exact_date is not None) or mode not in {
            "EXACT",
            "LATEST",
        }:
            raise ValueError("stock-connect readiness date selection is invalid")
        with self._database.session() as session:
            snapshot, calendar_rows, candidate_date = _resolve_snapshot(
                session,
                channels=channels,
                mode=mode,
                exact_date=exact_date,
            )
            run = _snapshot_run(session, snapshot.snapshot_id)
            partitions = (
                {}
                if run is None
                else {
                    item.partition_key: item
                    for item in session.scalars(
                        select(DataOperationPartition).where(
                            DataOperationPartition.run_id == run.run_id
                        )
                    ).all()
                }
            )
            bundles = _candidate_bundles(
                session,
                candidate_date=candidate_date,
                channels=channels,
            )
            channel_views = [
                _channel_readiness(
                    channel=channel,
                    candidate_date=candidate_date,
                    calendar_row=calendar_rows.get(channel),
                    snapshot=snapshot,
                    run=run,
                    partition=partitions.get(
                        _partition_key(channel=channel, trade_date=candidate_date)
                    ),
                    bundle=bundles.get(channel),
                )
                for channel in channels
            ]
            ready_trade_date = _ready_trade_date(
                session,
                mode=mode,
                exact_date=exact_date,
                channels=channels,
                channel_views=channel_views,
            )
            observed_at = max(
                _parse_timestamp(str(item["evidenceObservedAt"]), "channel evidence")
                for item in channel_views
            )
            calendar = _calendar_view(snapshot=snapshot, rows=calendar_rows.values())
            body: dict[str, object] = {
                "schemaVersion": _RESPONSE_SCHEMA,
                "mode": mode,
                "selectedChannels": list(channels),
                "requestedExactDate": (exact_date.isoformat() if exact_date is not None else None),
                "candidateTradeDate": (
                    candidate_date.isoformat() if candidate_date is not None else None
                ),
                "readyTradeDate": (
                    ready_trade_date.isoformat() if ready_trade_date is not None else None
                ),
                "observedAt": _timestamp(observed_at),
                "calendar": calendar,
                "channels": channel_views,
            }
            data_version = _canonical_hash(body)
            body["dataVersion"] = data_version
            return StockConnectReadinessResult(body=body, data_version=data_version)


def _resolve_snapshot(
    session: Any,
    *,
    channels: tuple[str, ...],
    mode: str,
    exact_date: date | None,
) -> tuple[
    StockConnectReadinessSnapshot,
    dict[str, StockConnectReadinessCalendarDay],
    date | None,
]:
    """选择能覆盖所选通道的最新终态证据，并从其日历确定候选日期。"""
    snapshots = session.scalars(
        select(StockConnectReadinessSnapshot)
        .where(StockConnectReadinessSnapshot.status != "PROBING")
        .order_by(StockConnectReadinessSnapshot.completed_at.desc())
        .limit(1000)
    ).all()
    fallback: StockConnectReadinessSnapshot | None = None
    for snapshot in snapshots:
        if not set(channels).issubset(set(snapshot.selected_channel_set.split(","))):
            continue
        fallback = fallback or snapshot
        rows = session.scalars(
            select(StockConnectReadinessCalendarDay).where(
                StockConnectReadinessCalendarDay.snapshot_id == snapshot.snapshot_id
            )
        ).all()
        by_date: dict[date, dict[str, StockConnectReadinessCalendarDay]] = defaultdict(dict)
        for row in rows:
            code = f"{row.channel}_{row.direction}"
            if code in channels:
                by_date[row.calendar_date][code] = row
        if mode == "EXACT":
            assert exact_date is not None
            selected = by_date.get(exact_date, {})
            if set(selected) == set(channels):
                return snapshot, selected, exact_date
            continue
        complete_dates = [
            current for current, selected in by_date.items() if set(selected) == set(channels)
        ]
        candidates = [
            current
            for current in complete_dates
            if all(item.calendar_state == "OPEN" for item in by_date[current].values())
        ]
        if candidates:
            candidate = max(candidates)
            return snapshot, by_date[candidate], candidate
        if complete_dates:
            # LATEST 以最新尝试为权威；较新 UNKNOWN/CLOSED 不能被旧 OPEN 快照静默掩盖。
            evidence_date = max(complete_dates)
            return snapshot, by_date[evidence_date], None
        return snapshot, {}, None
    if fallback is None:
        raise StockConnectReadinessNotObserved(
            "stock-connect readiness has not been observed for selected channels"
        )
    return fallback, {}, None


def _snapshot_run(session: Any, snapshot_id: UUID) -> DataOperationRun | None:
    """按私有 execution intent 关联 snapshot 与实际运行，不暴露该内部引用。"""
    return session.scalars(
        select(DataOperationRun)
        .where(
            DataOperationRun.dataset_code == "market.stock_connect.overview.bundle",
            DataOperationRun.execution_intent_json["stockConnectReadinessSnapshotId"].as_string()
            == str(snapshot_id),
        )
        .order_by(DataOperationRun.requested_at.desc())
        .limit(1)
    ).first()


def _candidate_bundles(
    session: Any,
    *,
    candidate_date: date | None,
    channels: tuple[str, ...],
) -> dict[str, StockConnectBundlePublication]:
    """读取候选日所选通道的当前不可变 bundle，不从组件 revision 临时拼装。"""
    if candidate_date is None:
        return {}
    rows = session.scalars(
        select(StockConnectBundlePublication).where(
            StockConnectBundlePublication.trade_date == candidate_date,
            StockConnectBundlePublication.superseded_at.is_(None),
        )
    ).all()
    return {code: row for row in rows if (code := f"{row.channel}_{row.direction}") in channels}


def _channel_readiness(
    *,
    channel: str,
    candidate_date: date | None,
    calendar_row: StockConnectReadinessCalendarDay | None,
    snapshot: StockConnectReadinessSnapshot,
    run: DataOperationRun | None,
    partition: DataOperationPartition | None,
    bundle: StockConnectBundlePublication | None,
) -> dict[str, object]:
    """按固定优先级把日历、publication 和执行事实投影为一个通道状态。"""
    observed = snapshot.completed_at or snapshot.created_at
    calendar_state = "UNKNOWN" if calendar_row is None else calendar_row.calendar_state
    if calendar_row is not None:
        observed = max(observed, calendar_row.evidence_observed_at)
    if calendar_state == "CLOSED":
        return _channel_view(
            channel,
            calendar_state,
            "NOT_TRADING",
            "OFFICIAL_CALENDAR_CLOSED",
            None,
            observed,
        )
    if calendar_state == "OPEN" and bundle is not None:
        return _channel_view(
            channel,
            calendar_state,
            "READY",
            "BUNDLE_PUBLISHED",
            bundle.data_version,
            max(observed, bundle.published_at),
        )
    if calendar_state == "UNKNOWN":
        state = (
            "SOURCE_MISSING"
            if snapshot.status == "SOURCE_MISSING"
            else "FAILED"
            if snapshot.status == "FAILED"
            else "PENDING"
        )
        reason = (
            snapshot.reason_code
            if snapshot.reason_code in _REASONS
            else "CALENDAR_EVIDENCE_MISSING"
        )
        return _channel_view(channel, calendar_state, state, reason, None, observed)
    if snapshot.status in {"FAILED", "SOURCE_MISSING"}:
        return _channel_view(
            channel,
            calendar_state,
            snapshot.status,
            snapshot.reason_code,
            None,
            observed,
        )
    if run is None:
        return _channel_view(
            channel,
            calendar_state,
            "PENDING",
            "COMMAND_NOT_SUBMITTED",
            None,
            observed,
        )
    run_time = run.finished_at or run.started_at or run.requested_at
    observed = max(observed, run_time)
    if partition is not None:
        if partition.checkpoint_updated_at is not None:
            observed = max(observed, partition.checkpoint_updated_at)
        if partition.status == "SUCCEEDED":
            return _channel_view(
                channel,
                calendar_state,
                "FAILED",
                "PUBLICATION_INCOMPLETE",
                None,
                observed,
            )
        if partition.status in {"FAILED", "CANCELLED", "SKIPPED"}:
            source_missing = _source_missing_error(partition.error_json)
            return _channel_view(
                channel,
                calendar_state,
                "SOURCE_MISSING" if source_missing else "FAILED",
                "EXECUTION_SOURCE_MISSING" if source_missing else "EXECUTION_FAILED",
                None,
                observed,
            )
        return _channel_view(
            channel,
            calendar_state,
            "PENDING",
            "EXECUTION_PENDING",
            None,
            observed,
        )
    if run.status in {"FAILED", "CANCELLED", "SKIPPED"}:
        source_missing = _source_missing_error(run.error_json)
        return _channel_view(
            channel,
            calendar_state,
            "SOURCE_MISSING" if source_missing else "FAILED",
            "EXECUTION_SOURCE_MISSING" if source_missing else "EXECUTION_FAILED",
            None,
            observed,
        )
    if run.status == "SUCCEEDED":
        return _channel_view(
            channel,
            calendar_state,
            "FAILED",
            "PUBLICATION_INCOMPLETE",
            None,
            observed,
        )
    return _channel_view(
        channel,
        calendar_state,
        "PENDING",
        "EXECUTION_PENDING",
        None,
        observed,
    )


def _ready_trade_date(
    session: Any,
    *,
    mode: str,
    exact_date: date | None,
    channels: tuple[str, ...],
    channel_views: Sequence[Mapping[str, object]],
) -> date | None:
    """返回所选通道真正已发布的共同日期；exact 未全 READY 时保持空。"""
    if not all(item["state"] == "READY" for item in channel_views):
        return None
    if mode == "EXACT":
        return exact_date
    overview = session.scalars(
        select(StockConnectOverviewPublication)
        .where(
            StockConnectOverviewPublication.channel_set == ",".join(channels),
            StockConnectOverviewPublication.superseded_at.is_(None),
        )
        .order_by(StockConnectOverviewPublication.trade_date.desc())
        .limit(1)
    ).first()
    return None if overview is None else overview.trade_date


def _calendar_view(
    *,
    snapshot: StockConnectReadinessSnapshot,
    rows: Sequence[StockConnectReadinessCalendarDay] | Any,
) -> dict[str, object]:
    """投影候选日唯一官方文件语义，拒绝跨来源摘要或时间不一致。"""
    selected = list(rows)
    known = [item for item in selected if item.publication_availability != "SOURCE_MISSING"]
    if not known:
        return {
            "dataVersion": snapshot.calendar_data_version,
            "observedAt": None,
            "sourceFileSha256": None,
            "sourcePublicationAt": None,
            "publicationAvailability": "SOURCE_MISSING",
        }
    digests = {item.source_file_sha256 for item in known}
    observed = {item.source_observed_at for item in known}
    publications = {item.source_publication_at for item in known}
    availabilities = {item.publication_availability for item in known}
    if (
        len(digests) != 1
        or len(observed) != 1
        or len(publications) != 1
        or len(availabilities) != 1
    ):
        raise ValueError("stock-connect calendar candidate source identity is inconsistent")
    source_observed_at = next(iter(observed))
    source_publication_at = next(iter(publications))
    return {
        "dataVersion": snapshot.calendar_data_version,
        "observedAt": (None if source_observed_at is None else _timestamp(source_observed_at)),
        "sourceFileSha256": next(iter(digests)),
        "sourcePublicationAt": (
            None if source_publication_at is None else _timestamp(source_publication_at)
        ),
        "publicationAvailability": next(iter(availabilities)),
    }


def _parse_calendar_evidence(value: Mapping[str, object]) -> _ParsedCalendarEvidence:
    """严格解析 provider readiness evidence，拒绝未知键、重复日或摘要漂移。"""
    if (
        set(value)
        != {
            "schema",
            "calendarDataVersion",
            "calendarManifestSha256",
            "evidenceObservedAt",
            "days",
        }
        or value.get("schema") != _EVIDENCE_SCHEMA
    ):
        raise ValueError("stock-connect readiness evidence envelope is invalid")
    calendar_data_version = _require_sha256(
        value.get("calendarDataVersion"),
        "readiness calendar version",
    )
    calendar_manifest_sha256 = _require_sha256(
        value.get("calendarManifestSha256"),
        "readiness calendar manifest",
    )
    evidence_observed_at = _parse_timestamp(
        value.get("evidenceObservedAt"),
        "readiness evidence",
    )
    raw_days = value.get("days")
    if not isinstance(raw_days, list) or not raw_days:
        raise ValueError("stock-connect readiness evidence days are invalid")
    days = tuple(_parse_calendar_day(item) for item in raw_days)
    keys = {(item.calendar_date, item.channel, item.direction) for item in days}
    if len(keys) != len(days):
        raise ValueError("stock-connect readiness evidence days are duplicated")
    identity = {
        "calendarManifestSha256": calendar_manifest_sha256,
        "days": raw_days,
    }
    if _canonical_hash(identity) != calendar_data_version:
        raise ValueError("stock-connect readiness calendar data version differs")
    return _ParsedCalendarEvidence(
        calendar_data_version=calendar_data_version,
        calendar_manifest_sha256=calendar_manifest_sha256,
        evidence_observed_at=evidence_observed_at,
        days=days,
    )


def _parse_calendar_day(value: object) -> _CalendarEvidenceDay:
    """解析一条 exact channel/day 证据并执行 publication 空值不变量。"""
    keys = {
        "calendarDate",
        "channel",
        "direction",
        "calendarState",
        "sourceFileSha256",
        "sourcePublicationAt",
        "publicationAvailability",
        "sourceObservedAt",
    }
    if not isinstance(value, Mapping) or set(value) != keys:
        raise ValueError("stock-connect readiness calendar day is invalid")
    channel = value.get("channel")
    direction = value.get("direction")
    if f"{channel}_{direction}" not in _CHANNELS:
        raise ValueError("stock-connect readiness calendar channel is invalid")
    state = value.get("calendarState")
    availability = value.get("publicationAvailability")
    if state not in {"OPEN", "CLOSED", "UNKNOWN"} or availability not in {
        "REPORTED",
        "NOT_REPORTED",
        "SOURCE_MISSING",
    }:
        raise ValueError("stock-connect readiness calendar semantics are invalid")
    digest_value = value.get("sourceFileSha256")
    publication_value = value.get("sourcePublicationAt")
    observed_value = value.get("sourceObservedAt")
    if availability == "SOURCE_MISSING":
        if (
            state != "UNKNOWN"
            or digest_value is not None
            or publication_value is not None
            or observed_value is not None
        ):
            raise ValueError("missing stock-connect calendar source has reported fields")
        digest = None
        publication = None
        observed = None
    else:
        if state == "UNKNOWN":
            raise ValueError("known stock-connect calendar source cannot be unknown")
        digest = _require_sha256(digest_value, "readiness calendar source file")
        observed = _parse_timestamp(observed_value, "readiness calendar source observation")
        publication = (
            None
            if publication_value is None
            else _parse_timestamp(publication_value, "readiness calendar publication")
        )
        if (availability == "REPORTED") != (publication is not None):
            raise ValueError("stock-connect calendar publication availability differs")
    calendar_date_value = value.get("calendarDate")
    if not isinstance(calendar_date_value, str):
        raise ValueError("stock-connect readiness calendar date is invalid")
    return _CalendarEvidenceDay(
        calendar_date=date.fromisoformat(calendar_date_value),
        channel=str(channel),
        direction=str(direction),
        calendar_state=str(state),
        source_file_sha256=digest,
        source_publication_at=publication,
        publication_availability=str(availability),
        source_observed_at=observed,
    )


def _missing_calendar_evidence(
    *,
    request_hash: str,
    evidence_observed_at: datetime,
) -> _ParsedCalendarEvidence:
    """为 preflight 前置门禁失败生成无日历行、但可版本化的缺源证据。"""
    request_hash = _require_sha256(request_hash, "readiness request")
    identity = {"calendarManifestSha256": request_hash, "days": []}
    return _ParsedCalendarEvidence(
        calendar_data_version=_canonical_hash(identity),
        calendar_manifest_sha256=request_hash,
        evidence_observed_at=evidence_observed_at,
        days=(),
    )


def _selected_channels(values: Sequence[str]) -> tuple[str, ...]:
    """校验一至四个唯一公开通道并返回稳定排序。"""
    channels = tuple(values)
    if (
        not 1 <= len(channels) <= 4
        or len(set(channels)) != len(channels)
        or any(item not in _CHANNELS for item in channels)
    ):
        raise ValueError("stock-connect readiness channels are invalid")
    return tuple(sorted(channels))


def _partition_key(*, channel: str, trade_date: date | None) -> str:
    """重建执行器冻结的日包分区键；无候选日时返回不可能命中的哨兵。"""
    if trade_date is None:
        return "stock-connect:none"
    route, direction = _CHANNELS[channel]
    return f"stock-connect:{trade_date.isoformat()}:{route}:{direction}"


def _source_missing_error(value: Mapping[str, Any] | None) -> bool:
    """仅按稳定控制面错误码识别来源缺失，不分析供应商异常文本。"""
    if not isinstance(value, Mapping):
        return False
    return value.get("code") in {
        "source-unavailable",
        "DELIVERY_REVALIDATION_FAILED",
        "delivery-source-missing",
    }


def _channel_view(
    channel: str,
    calendar_state: str,
    state: str,
    reason: str,
    bundle_data_version: str | None,
    observed_at: datetime,
) -> dict[str, object]:
    """构造一个字段完备且原因枚举受控的通道 readiness。"""
    if reason not in _REASONS:
        raise ValueError("stock-connect readiness channel reason is unsupported")
    return {
        "channel": channel,
        "calendarState": calendar_state,
        "state": state,
        "reasonCode": reason,
        "bundleDataVersion": bundle_data_version,
        "evidenceObservedAt": _timestamp(observed_at),
    }


def _probe_outcome(status: _SnapshotStatus, reason: str) -> StockConnectReadinessProbeOutcome:
    """构造不含底层异常内容的固定 preflight 失败结果。"""
    return StockConnectReadinessProbeOutcome(
        status=status,
        reason_code=reason,
        detail="Official stock-connect readiness preflight did not complete",
    )


def _require_sha256(value: object, label: str) -> str:
    """读取小写 SHA-256，拒绝大小写或格式规范化造成的身份歧义。"""
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{label} digest is invalid")
    return value


def _parse_timestamp(value: object, label: str) -> datetime:
    """解析带时区时间；observedAt 与 publicationAt 绝不相互替代。"""
    if not isinstance(value, str):
        raise ValueError(f"{label} timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{label} timestamp is invalid") from error
    return _aware(parsed, label)


def _aware(value: datetime, label: str) -> datetime:
    """要求时间明确带时区并规范为 UTC。"""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} timestamp has no timezone")
    return value.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    """以 UTC RFC 3339 输出已持久化事件时间。"""
    return _aware(value, "readiness output").isoformat().replace("+00:00", "Z")


def _canonical_hash(value: object) -> str:
    """按合同指定的 UTF-8、递归键排序和无空白 JSON 计算 SHA-256。"""
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
