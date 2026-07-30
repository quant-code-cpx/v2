"""互联互通 readiness 持久化与 exact/latest 查询的 PostgreSQL 集成测试。"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, date, datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import DBAPIError

from service_data_sync.application.ports.delivery_manifest import (
    DeliveryManifestTradeDate,
    build_immutable_delivery_manifest,
)
from service_data_sync.bootstrap.settings import load_settings
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.database.models.market.stock_connect_readiness import (
    StockConnectReadinessCalendarDay,
    StockConnectReadinessSnapshot,
)
from service_data_sync.infrastructure.persistence.delivery_manifest_repository import (
    SqlAlchemyDeliveryManifestRepository,
)
from service_data_sync.infrastructure.persistence.stock_connect_readiness_repository import (
    SqlAlchemyStockConnectReadinessRepository,
    StockConnectReadinessProbeOutcome,
)

_CHANNELS = ("SH_NORTHBOUND", "SZ_NORTHBOUND")
_REQUEST_HASH = "a" * 64
_CALENDAR_MANIFEST_HASH = "b" * 64
_SOURCE_FILE_HASH = "c" * 64


@pytest.mark.integration
def test_readiness_begin_is_visible_and_finish_is_one_way() -> None:
    """PROBING 必须先独立可见，终结后应用层和数据库触发器都拒绝再次改写。"""
    database = _database()
    repository = SqlAlchemyStockConnectReadinessRepository(database)
    snapshot_id = uuid4()
    started_at = datetime.now(UTC)
    completed_at = started_at + timedelta(seconds=1)
    evidence = _calendar_evidence(
        evidence_observed_at=completed_at,
        rows=((date.today(), "SH_NORTHBOUND", "UNKNOWN"),),
    )
    outcome = _source_missing_outcome()
    try:
        repository.begin(
            snapshot_id=snapshot_id,
            request_hash=_REQUEST_HASH,
            selected_channels=("SH_NORTHBOUND",),
            observed_at=started_at,
        )

        with database.session() as session:
            probing = session.get(StockConnectReadinessSnapshot, snapshot_id)
            assert probing is not None
            assert probing.status == "PROBING"
            assert probing.reason_code == "PREFLIGHT_PENDING"
            assert probing.completed_at is None
            assert (
                session.scalar(
                    select(StockConnectReadinessCalendarDay).where(
                        StockConnectReadinessCalendarDay.snapshot_id == snapshot_id
                    )
                )
                is None
            )

        repository.finish(
            snapshot_id=snapshot_id,
            outcome=outcome,
            evidence=evidence,
            manifest_id=None,
            completed_at=completed_at,
            request_hash=_REQUEST_HASH,
        )

        with database.session() as session:
            terminal = session.get(StockConnectReadinessSnapshot, snapshot_id)
            assert terminal is not None
            assert terminal.status == "SOURCE_MISSING"
            assert terminal.reason_code == "CALENDAR_SOURCE_MISSING"
            assert terminal.completed_at == completed_at
            assert (
                session.scalar(
                    select(StockConnectReadinessCalendarDay).where(
                        StockConnectReadinessCalendarDay.snapshot_id == snapshot_id
                    )
                )
                is not None
            )

        with pytest.raises(
            ValueError,
            match="cannot be completed",
        ):
            repository.finish(
                snapshot_id=snapshot_id,
                outcome=outcome,
                evidence=evidence,
                manifest_id=None,
                completed_at=completed_at + timedelta(seconds=1),
                request_hash=_REQUEST_HASH,
            )

        with pytest.raises(DBAPIError):
            with database.transaction() as session:
                session.execute(
                    update(StockConnectReadinessSnapshot)
                    .where(StockConnectReadinessSnapshot.snapshot_id == snapshot_id)
                    .values(
                        status="FAILED",
                        reason_code="PREFLIGHT_FAILED",
                    )
                )
    finally:
        database.close()


@pytest.mark.integration
def test_readiness_finish_rejects_mismatched_request_hash() -> None:
    """终结请求必须匹配 begin 冻结的目标摘要，失败时 snapshot 保持 PROBING。"""
    database = _database()
    repository = SqlAlchemyStockConnectReadinessRepository(database)
    snapshot_id = uuid4()
    started_at = datetime.now(UTC)
    try:
        repository.begin(
            snapshot_id=snapshot_id,
            request_hash=_REQUEST_HASH,
            selected_channels=("SH_NORTHBOUND",),
            observed_at=started_at,
        )

        with pytest.raises(
            ValueError,
            match="request hash differs",
        ):
            repository.finish(
                snapshot_id=snapshot_id,
                outcome=_source_missing_outcome(),
                evidence=None,
                manifest_id=None,
                completed_at=started_at + timedelta(seconds=1),
                request_hash="d" * 64,
            )

        with database.session() as session:
            snapshot = session.get(StockConnectReadinessSnapshot, snapshot_id)
            assert snapshot is not None
            assert snapshot.status == "PROBING"
            assert snapshot.completed_at is None
            assert snapshot.calendar_data_version is None
    finally:
        database.close()


@pytest.mark.integration
def test_readiness_exact_latest_calendar_and_canonical_representation() -> None:
    """真实持久化行决定 exact/latest 候选日，并生成可复算的唯一响应版本。"""
    database = _database()
    repository = SqlAlchemyStockConnectReadinessRepository(database)
    snapshot_id = uuid4()
    started_at = datetime.now(UTC)
    completed_at = started_at + timedelta(seconds=1)
    open_date = date.today() - timedelta(days=2)
    mixed_date = date.today() - timedelta(days=1)
    unknown_date = date.today()
    evidence = _calendar_evidence(
        evidence_observed_at=completed_at,
        rows=(
            (open_date, "SH_NORTHBOUND", "OPEN"),
            (open_date, "SZ_NORTHBOUND", "OPEN"),
            (mixed_date, "SH_NORTHBOUND", "OPEN"),
            (mixed_date, "SZ_NORTHBOUND", "CLOSED"),
            (unknown_date, "SH_NORTHBOUND", "UNKNOWN"),
            (unknown_date, "SZ_NORTHBOUND", "UNKNOWN"),
        ),
    )
    try:
        repository.begin(
            snapshot_id=snapshot_id,
            request_hash=_REQUEST_HASH,
            selected_channels=tuple(reversed(_CHANNELS)),
            observed_at=started_at,
        )
        repository.finish(
            snapshot_id=snapshot_id,
            outcome=_source_missing_outcome(),
            evidence=evidence,
            manifest_id=None,
            completed_at=completed_at,
            request_hash=_REQUEST_HASH,
        )

        latest = repository.query(
            mode="LATEST",
            exact_date=None,
            selected_channels=tuple(reversed(_CHANNELS)),
        )
        assert latest.body["selectedChannels"] == list(_CHANNELS)
        assert latest.body["candidateTradeDate"] == open_date.isoformat()
        assert latest.body["readyTradeDate"] is None
        assert latest.body["requestedExactDate"] is None
        latest_channels = _channel_map(latest.body)
        assert latest_channels["SH_NORTHBOUND"]["calendarState"] == "OPEN"
        assert latest_channels["SZ_NORTHBOUND"]["calendarState"] == "OPEN"
        assert latest_channels["SH_NORTHBOUND"]["state"] == "SOURCE_MISSING"
        assert latest_channels["SH_NORTHBOUND"]["reasonCode"] == "CALENDAR_SOURCE_MISSING"

        mixed = repository.query(
            mode="EXACT",
            exact_date=mixed_date,
            selected_channels=_CHANNELS,
        )
        assert mixed.body["candidateTradeDate"] == mixed_date.isoformat()
        assert mixed.body["requestedExactDate"] == mixed_date.isoformat()
        assert mixed.body["readyTradeDate"] is None
        mixed_channels = _channel_map(mixed.body)
        assert mixed_channels["SH_NORTHBOUND"]["calendarState"] == "OPEN"
        assert mixed_channels["SH_NORTHBOUND"]["state"] == "SOURCE_MISSING"
        assert mixed_channels["SZ_NORTHBOUND"]["calendarState"] == "CLOSED"
        assert mixed_channels["SZ_NORTHBOUND"]["state"] == "NOT_TRADING"
        assert mixed_channels["SZ_NORTHBOUND"]["reasonCode"] == "OFFICIAL_CALENDAR_CLOSED"

        unknown = repository.query(
            mode="EXACT",
            exact_date=unknown_date,
            selected_channels=_CHANNELS,
        )
        assert unknown.body["candidateTradeDate"] == unknown_date.isoformat()
        assert unknown.body["readyTradeDate"] is None
        assert unknown.body["calendar"] == {
            "dataVersion": cast(dict[str, object], evidence)["calendarDataVersion"],
            "observedAt": None,
            "sourceFileSha256": None,
            "sourcePublicationAt": None,
            "publicationAvailability": "SOURCE_MISSING",
        }
        unknown_channels = _channel_map(unknown.body)
        assert {
            (
                item["calendarState"],
                item["state"],
                item["reasonCode"],
            )
            for item in unknown_channels.values()
        } == {("UNKNOWN", "SOURCE_MISSING", "CALENDAR_SOURCE_MISSING")}

        for result in (latest, mixed, unknown):
            assert result.body["dataVersion"] == result.data_version
            assert len(result.data_version) == 64
            assert result.data_version == _body_data_version(result.body)
    finally:
        database.close()


@pytest.mark.integration
def test_readiness_latest_does_not_hide_new_source_failure_with_old_open_snapshot() -> None:
    """较新 UNKNOWN 失败是当前尝试，LATEST 不得回退旧 OPEN 证据掩盖故障。"""
    database = _database()
    repository = SqlAlchemyStockConnectReadinessRepository(database)
    older_snapshot_id = uuid4()
    newer_snapshot_id = uuid4()
    older_started_at = datetime.now(UTC)
    older_completed_at = older_started_at + timedelta(seconds=1)
    newer_started_at = older_completed_at + timedelta(seconds=1)
    newer_completed_at = newer_started_at + timedelta(seconds=1)
    older_open_date = date.today() - timedelta(days=1)
    manifest_id = _eligible_manifest(
        database,
        trade_date=older_open_date,
        created_at=older_started_at,
    )
    try:
        repository.begin(
            snapshot_id=older_snapshot_id,
            request_hash=_REQUEST_HASH,
            selected_channels=("SH_NORTHBOUND",),
            observed_at=older_started_at,
        )
        repository.finish(
            snapshot_id=older_snapshot_id,
            outcome=StockConnectReadinessProbeOutcome(
                status="PENDING",
                reason_code="COMMAND_NOT_SUBMITTED",
                detail="Official deliveries are verified and publication has not completed",
            ),
            evidence=_calendar_evidence(
                evidence_observed_at=older_completed_at,
                rows=((older_open_date, "SH_NORTHBOUND", "OPEN"),),
            ),
            manifest_id=manifest_id,
            completed_at=older_completed_at,
            request_hash=_REQUEST_HASH,
        )
        repository.begin(
            snapshot_id=newer_snapshot_id,
            request_hash=_REQUEST_HASH,
            selected_channels=("SH_NORTHBOUND",),
            observed_at=newer_started_at,
        )
        repository.finish(
            snapshot_id=newer_snapshot_id,
            outcome=_source_missing_outcome(),
            evidence=_calendar_evidence(
                evidence_observed_at=newer_completed_at,
                rows=((date.today(), "SH_NORTHBOUND", "UNKNOWN"),),
            ),
            manifest_id=None,
            completed_at=newer_completed_at,
            request_hash=_REQUEST_HASH,
        )

        latest = repository.query(
            mode="LATEST",
            exact_date=None,
            selected_channels=("SH_NORTHBOUND",),
        )

        assert latest.body["candidateTradeDate"] is None
        assert latest.body["readyTradeDate"] is None
        channel = _channel_map(latest.body)["SH_NORTHBOUND"]
        assert channel["calendarState"] == "UNKNOWN"
        assert channel["state"] == "SOURCE_MISSING"
        assert channel["reasonCode"] == "CALENDAR_SOURCE_MISSING"
        assert channel["evidenceObservedAt"] == _timestamp(newer_completed_at)
    finally:
        database.close()


def _database() -> DatabaseClient:
    """连接显式启用的隔离 PostgreSQL；宿主机不执行 Python 测试。"""
    if os.environ.get("DATA_SYNC_RUN_INTEGRATION") != "1":
        pytest.skip("set DATA_SYNC_RUN_INTEGRATION=1 after starting local infrastructure")
    return DatabaseClient.from_settings(load_settings())


def _eligible_manifest(
    database: DatabaseClient,
    *,
    trade_date: date,
    created_at: datetime,
) -> UUID:
    """通过正式构造器和仓储生成旧成功预检绑定的完整不可变交付清单。"""
    manifest = build_immutable_delivery_manifest(
        manifest_id=uuid4(),
        dataset_code="market.stock_connect.overview.bundle",
        provider_id="official-stock-connect",
        request_hash=_REQUEST_HASH,
        status="ELIGIBLE",
        available_until=created_at + timedelta(hours=1),
        minimum_remaining_seconds=60,
        created_at=created_at,
        days=(
            DeliveryManifestTradeDate(
                trade_date=trade_date,
                target_count=1,
                evidence={
                    "channel": "SH_NORTHBOUND",
                    "tradeDate": trade_date.isoformat(),
                },
            ),
        ),
    )
    return SqlAlchemyDeliveryManifestRepository(database).persist(manifest).manifest_id


def _source_missing_outcome() -> StockConnectReadinessProbeOutcome:
    """构造不含供应商原文的稳定日历来源缺失终态。"""
    return StockConnectReadinessProbeOutcome(
        status="SOURCE_MISSING",
        reason_code="CALENDAR_SOURCE_MISSING",
        detail="Official stock-connect readiness preflight did not complete",
    )


def _calendar_evidence(
    *,
    evidence_observed_at: datetime,
    rows: tuple[tuple[date, str, str], ...],
) -> dict[str, object]:
    """生成同时覆盖已知开闭市与真实缺源 UNKNOWN 的严格 evidence。"""
    days = [_calendar_day(day, channel, state) for day, channel, state in rows]
    identity: dict[str, object] = {
        "calendarManifestSha256": _CALENDAR_MANIFEST_HASH,
        "days": days,
    }
    return {
        "schema": "quant-v2.stock-connect-readiness-evidence.v1",
        "calendarDataVersion": _canonical_hash(identity),
        "calendarManifestSha256": _CALENDAR_MANIFEST_HASH,
        "evidenceObservedAt": _timestamp(evidence_observed_at),
        "days": days,
    }


def _calendar_day(day: date, channel_code: str, state: str) -> dict[str, object]:
    """构造一条来源字段与 OPEN/CLOSED/UNKNOWN 空值语义一致的日历行。"""
    channel, direction = channel_code.split("_", maxsplit=1)
    missing = state == "UNKNOWN"
    return {
        "calendarDate": day.isoformat(),
        "channel": channel,
        "direction": direction,
        "calendarState": state,
        "sourceFileSha256": None if missing else _SOURCE_FILE_HASH,
        "sourcePublicationAt": None if missing else "2026-07-30T01:00:00Z",
        "publicationAvailability": "SOURCE_MISSING" if missing else "REPORTED",
        "sourceObservedAt": None if missing else "2026-07-30T01:05:00Z",
    }


def _channel_map(body: dict[str, object]) -> dict[str, dict[str, object]]:
    """按通道代码索引 readiness 正文中的逐通道对象。"""
    channels = cast(list[dict[str, object]], body["channels"])
    return {cast(str, item["channel"]): item for item in channels}


def _body_data_version(body: dict[str, object]) -> str:
    """按合同移除顶层 dataVersion 后重算正文 SHA-256。"""
    canonical_body = {key: value for key, value in body.items() if key != "dataVersion"}
    return _canonical_hash(canonical_body)


def _canonical_hash(value: object) -> str:
    """按 UTF-8、递归键排序、数组保序和保留 null 的 JSON 规则计算 SHA-256。"""
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _timestamp(value: datetime) -> str:
    """把带时区测试时间规范为 UTC RFC 3339。"""
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
