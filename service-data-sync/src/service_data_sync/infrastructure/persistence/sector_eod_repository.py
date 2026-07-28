"""板块 EOD 横截面、质量证据和确定性排行的 PostgreSQL 实现。"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.engine import Connection

from service_data_sync.application.ports.sector_eod import (
    SECTOR_EOD_QUALITY_POLICY_VERSION,
    ArchivedSectorEodObservation,
    PublishedSectorEodSnapshot,
    QueuedSectorEodRun,
    RankedSectorEodQuote,
    SectorEodHistoricalReference,
    SectorEodQualityResult,
    SectorEodRepository,
    SectorEodRun,
)
from service_data_sync.domain.sector import (
    SectorEodFinality,
    SectorEodQuote,
    SectorEodSnapshot,
    SectorEodSort,
    SectorIdentifier,
    SectorScheme,
    SortOrder,
    sector_eod_snapshot_content_sha256,
)
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.persistence.source_batch import record_source_observation

_DATASET = "sector.quote.eod.snapshot"
_CAPABILITY = "sector.quote.eod.snapshot.raw"
_NORMALIZER_VERSION = "sector-eod-v1"
_LEASE_DURATION = timedelta(minutes=5)
_SORT_COLUMNS = {
    SectorEodSort.CHANGE_PERCENT: "quote.change_percent",
    SectorEodSort.TURNOVER_PERCENT: "quote.turnover_percent",
    SectorEodSort.MARKET_VALUE: "quote.market_value",
    SectorEodSort.LATEST_VALUE: "quote.latest_value",
    SectorEodSort.ADVANCERS: "quote.advancers",
    SectorEodSort.DECLINERS: "quote.decliners",
    SectorEodSort.LEADER_CHANGE_PERCENT: "quote.leader_change_percent",
    SectorEodSort.CODE: 'sector.sector_code COLLATE "C"',
}
_SORT_DIRECTIONS = {SortOrder.ASC: "ASC", SortOrder.DESC: "DESC"}


class SqlAlchemySectorEodRepository(SectorEodRepository):
    """以 PostgreSQL 事务维护完整 EOD 快照，读取时动态计算排行。"""

    def __init__(self, database: DatabaseClient) -> None:
        """接收组合根创建的数据库客户端，不直接读取环境配置。"""
        self._database = database

    def start_run(
        self, *, scheme: SectorScheme, trade_date: date, reuse_archived_raw: bool
    ) -> SectorEodRun:
        """获取目标 EOD 分区租约，并在 replay 时只允许继续已登记的 raw 观察。"""
        now = datetime.now(UTC)
        partition_key = _partition_key(scheme, trade_date)
        request_key = f"sector-eod:{partition_key}"
        lease_token = uuid4()
        with self._database.engine.begin() as connection:
            existing = (
                connection.execute(
                    text(
                        """
                        SELECT status, lease_expires_at, last_source_batch_id
                        FROM sector_eod_sync_partition
                        WHERE scheme = :scheme AND trade_date = :trade_date
                        FOR UPDATE
                        """
                    ),
                    {"scheme": scheme.value, "trade_date": trade_date},
                )
                .mappings()
                .one_or_none()
            )
            if (
                existing is not None
                and existing["lease_expires_at"] is not None
                and existing["lease_expires_at"] > now
            ):
                raise RuntimeError("sector eod partition is already leased")
            last_source_batch_id: object | None = None
            if reuse_archived_raw:
                if existing is None or existing["last_source_batch_id"] is None:
                    raise ValueError("sector eod replay requires an archived source observation")
                last_source_batch_id = existing["last_source_batch_id"]
            run_row = (
                connection.execute(
                    text(
                        """
                        INSERT INTO sync_run (
                          run_id, capability, mode, request_key, target_date, status,
                          requested_at, started_at, finished_at, created_at
                        ) VALUES (
                          :run_id, :capability, 'manual', :request_key, :trade_date, 'running',
                          :now, :now, NULL, :now
                        )
                        ON CONFLICT (request_key) DO UPDATE
                        SET status = 'running', started_at = EXCLUDED.started_at, finished_at = NULL
                        RETURNING run_id
                        """
                    ),
                    {
                        "run_id": uuid4(),
                        "capability": _CAPABILITY,
                        "request_key": request_key,
                        "trade_date": trade_date,
                        "now": now,
                    },
                )
                .mappings()
                .one()
            )
            run_id = UUID(str(run_row["run_id"]))
            owner = f"sector-eod:{run_id}:{lease_token}"
            lease_expires_at = now + _LEASE_DURATION
            connection.execute(
                text(
                    """
                    INSERT INTO sync_partition (
                      run_id, partition_key, status, attempt, lease_owner, lease_until,
                      heartbeat_at, next_retry_at, checkpoint_json, error_code, updated_at
                    ) VALUES (
                      :run_id, :partition_key, 'running', 1, :owner, :lease_expires_at,
                      :now, NULL, :checkpoint, NULL, :now
                    )
                    ON CONFLICT (run_id, partition_key) DO UPDATE
                    SET status = 'running', attempt = sync_partition.attempt + 1,
                        lease_owner = EXCLUDED.lease_owner, lease_until = EXCLUDED.lease_until,
                        heartbeat_at = EXCLUDED.heartbeat_at, next_retry_at = NULL,
                        checkpoint_json = EXCLUDED.checkpoint_json, error_code = NULL,
                        updated_at = EXCLUDED.updated_at
                    """
                ),
                {
                    "run_id": run_id,
                    "partition_key": partition_key,
                    "owner": owner,
                    "lease_expires_at": lease_expires_at,
                    "now": now,
                    "checkpoint": json.dumps(
                        {"stage": "raw_archived" if reuse_archived_raw else "requested"},
                        separators=(",", ":"),
                    ),
                },
            )
            connection.execute(
                text(
                    """
                    INSERT INTO sector_eod_sync_partition (
                      scheme, trade_date, run_id, status, stage, attempt, lease_owner, lease_token,
                      lease_expires_at, last_source_batch_id, last_error_code, updated_at
                    ) VALUES (
                      :scheme, :trade_date, :run_id, 'running', :stage, 1, :owner, :lease_token,
                      :lease_expires_at, :last_source_batch_id, NULL, :now
                    )
                    ON CONFLICT (scheme, trade_date) DO UPDATE
                    SET run_id = EXCLUDED.run_id, status = 'running', stage = EXCLUDED.stage,
                        attempt = sector_eod_sync_partition.attempt + 1,
                        lease_owner = EXCLUDED.lease_owner, lease_token = EXCLUDED.lease_token,
                        lease_expires_at = EXCLUDED.lease_expires_at,
                        last_source_batch_id = EXCLUDED.last_source_batch_id,
                        last_error_code = NULL, updated_at = EXCLUDED.updated_at
                    """
                ),
                {
                    "scheme": scheme.value,
                    "trade_date": trade_date,
                    "run_id": run_id,
                    "stage": "raw_archived" if reuse_archived_raw else "requested",
                    "owner": owner,
                    "lease_token": lease_token,
                    "lease_expires_at": lease_expires_at,
                    "last_source_batch_id": last_source_batch_id,
                    "now": now,
                },
            )
        return SectorEodRun(
            run_id=run_id,
            lease_token=lease_token,
            scheme=scheme,
            trade_date=trade_date,
        )

    def record_archived_observation(
        self,
        *,
        run: SectorEodRun,
        provider_id: str,
        source_payload_sha256: str,
        raw_uri: str,
        observed_at: datetime,
        adapter_version: str,
        schema_fingerprint: str,
        upstream_source: str | None = None,
    ) -> ArchivedSectorEodObservation:
        """登记 S3 已完成的 raw 观察并推进 checkpoint，供 DB 故障后重放。"""
        now = datetime.now(UTC)
        with self._database.engine.begin() as connection:
            _assert_active_run(connection, run=run, now=now)
            source_batch_id = record_source_observation(
                connection,
                provider_id=provider_id,
                capability=_CAPABILITY,
                source_payload_sha256=source_payload_sha256,
                raw_uri=raw_uri,
                observed_at=observed_at,
                created_at=now,
                upstream_source=upstream_source or provider_id,
                adapter_version=adapter_version,
                schema_fingerprint=schema_fingerprint,
                run_id=run.run_id,
                partition_key=_partition_key(run.scheme, run.trade_date),
            )
            _update_run_checkpoint(
                connection,
                run=run,
                stage="raw_archived",
                status="running",
                source_batch_id=source_batch_id,
                error_code=None,
                now=now,
            )
        return ArchivedSectorEodObservation(
            source_batch_id=source_batch_id,
            raw_uri=raw_uri,
            provider_id=provider_id,
            observed_at=observed_at,
            adapter_version=adapter_version,
            schema_fingerprint=schema_fingerprint,
        )

    def get_archived_observation(self, *, run: SectorEodRun) -> ArchivedSectorEodObservation:
        """读取 checkpoint 指向的唯一 raw 来源观察，replay 绝不重访 provider。"""
        now = datetime.now(UTC)
        with self._database.engine.begin() as connection:
            _assert_active_run(connection, run=run, now=now)
            row = (
                connection.execute(
                    text(
                        """
                        SELECT batch.source_batch_id, batch.raw_uri, batch.provider_id,
                               batch.observed_at, batch.adapter_version, batch.schema_fingerprint
                        FROM sector_eod_sync_partition checkpoint
                        JOIN source_batch batch
                          ON batch.source_batch_id = checkpoint.last_source_batch_id
                        WHERE checkpoint.scheme = :scheme
                          AND checkpoint.trade_date = :trade_date
                          AND checkpoint.lease_token = :lease_token
                        """
                    ),
                    {
                        "scheme": run.scheme.value,
                        "trade_date": run.trade_date,
                        "lease_token": run.lease_token,
                    },
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise ValueError("sector eod replay source observation is unavailable")
        return ArchivedSectorEodObservation(
            source_batch_id=UUID(str(row["source_batch_id"])),
            raw_uri=str(row["raw_uri"]),
            provider_id=str(row["provider_id"]),
            observed_at=row["observed_at"],
            adapter_version=str(row["adapter_version"]),
            schema_fingerprint=str(row["schema_fingerprint"]),
        )

    def has_archived_observation(self, *, scheme: SectorScheme, trade_date: date) -> bool:
        """只检查 checkpoint 是否已绑定 source batch，任务恢复前不读取 raw 内容或触发 provider。"""
        with self._database.engine.connect() as connection:
            value = connection.execute(
                text(
                    """
                    SELECT EXISTS (
                      SELECT 1
                      FROM sector_eod_sync_partition
                      WHERE scheme = :scheme
                        AND trade_date = :trade_date
                        AND last_source_batch_id IS NOT NULL
                    )
                    """
                ),
                {"scheme": scheme.value, "trade_date": trade_date},
            ).scalar_one()
        return bool(value)

    def get_historical_reference(
        self, *, scheme: SectorScheme, before_trade_date: date
    ) -> SectorEodHistoricalReference | None:
        """读取目标日之前最近 current published 快照及市值字段，隔离跨日质量查询。"""
        with self._database.engine.connect() as connection:
            snapshot = (
                connection.execute(
                    text(
                        """
                        SELECT snapshot_id, trade_date, content_sha256
                        FROM sector_eod_snapshot
                        WHERE scheme = :scheme
                          AND trade_date < :before_trade_date
                          AND state = 'published'
                          AND superseded_at IS NULL
                        ORDER BY trade_date DESC
                        LIMIT 1
                        """
                    ),
                    {"scheme": scheme.value, "before_trade_date": before_trade_date},
                )
                .mappings()
                .one_or_none()
            )
            if snapshot is None:
                return None
            quote_rows = (
                connection.execute(
                    text(
                        """
                        SELECT sector.sector_code, quote.market_value
                        FROM sector_eod_quote quote
                        JOIN sector_entity sector ON sector.sector_key = quote.sector_key
                        WHERE quote.snapshot_id = :snapshot_id
                        """
                    ),
                    {"snapshot_id": snapshot["snapshot_id"]},
                )
                .mappings()
                .all()
            )
        return SectorEodHistoricalReference(
            trade_date=snapshot["trade_date"],
            content_sha256=bytes(snapshot["content_sha256"]),
            market_values={
                str(row["sector_code"]): _decimal_or_none(row["market_value"]) for row in quote_rows
            },
        )

    def mark_normalized(self, *, run: SectorEodRun) -> None:
        """在标准载荷已成功解析后推进 checkpoint，旧 fencing token 无法覆盖新 owner。"""
        now = datetime.now(UTC)
        with self._database.engine.begin() as connection:
            _assert_active_run(connection, run=run, now=now)
            _update_run_checkpoint(
                connection,
                run=run,
                stage="normalized",
                status="running",
                source_batch_id=None,
                error_code=None,
                now=now,
            )

    def mark_fetched(self, *, run: SectorEodRun) -> None:
        """记录 provider 已返回，后续 raw 归档失败可从稳定失败码重新调度。"""
        now = datetime.now(UTC)
        with self._database.engine.begin() as connection:
            _assert_active_run(connection, run=run, now=now)
            _update_run_checkpoint(
                connection,
                run=run,
                stage="fetched",
                status="running",
                source_batch_id=None,
                error_code=None,
                now=now,
            )

    def renew_lease(self, *, run: SectorEodRun) -> None:
        """仅允许当前 fencing token 延长未过期租约，避免旧 worker 在接管后继续落库。"""
        now = datetime.now(UTC)
        lease_expires_at = now + _LEASE_DURATION
        with self._database.engine.begin() as connection:
            result = connection.execute(
                text(
                    """
                    UPDATE sector_eod_sync_partition
                    SET lease_expires_at = :lease_expires_at, updated_at = :now
                    WHERE scheme = :scheme
                      AND trade_date = :trade_date
                      AND run_id = :run_id
                      AND lease_token = :lease_token
                      AND status = 'running'
                      AND lease_expires_at > :now
                    """
                ),
                {
                    "lease_expires_at": lease_expires_at,
                    "now": now,
                    "scheme": run.scheme.value,
                    "trade_date": run.trade_date,
                    "run_id": run.run_id,
                    "lease_token": run.lease_token,
                },
            )
            if getattr(result, "rowcount", 1) == 0:
                raise RuntimeError("sector eod lease is no longer active")
            connection.execute(
                text(
                    """
                    UPDATE sync_partition
                    SET lease_until = :lease_expires_at, heartbeat_at = :now, updated_at = :now
                    WHERE run_id = :run_id AND partition_key = :partition_key
                    """
                ),
                {
                    "lease_expires_at": lease_expires_at,
                    "now": now,
                    "run_id": run.run_id,
                    "partition_key": _partition_key(run.scheme, run.trade_date),
                },
            )

    def requeue_expired_leases(self, *, now: datetime) -> int:
        """将崩溃 worker 遗留的分区改回 queued，原始 checkpoint 和 source batch 保持不变。"""
        if now.tzinfo is None:
            raise ValueError("sector eod reaper time must include a timezone")
        with self._database.engine.begin() as connection:
            expired_rows = (
                connection.execute(
                    text(
                        """
                        UPDATE sector_eod_sync_partition
                        SET status = 'queued', lease_owner = NULL, lease_token = NULL,
                            lease_expires_at = NULL, last_error_code = 'lease-expired',
                            updated_at = :now
                        WHERE status = 'running' AND lease_expires_at < :now
                        RETURNING run_id, scheme, trade_date, stage, last_source_batch_id
                        """
                    ),
                    {"now": now},
                )
                .mappings()
                .all()
            )
            for row in expired_rows:
                run_id = UUID(str(row["run_id"]))
                partition_key = _partition_key(
                    SectorScheme(str(row["scheme"])),
                    row["trade_date"],
                )
                connection.execute(
                    text(
                        """
                        UPDATE sync_partition
                        SET status = 'queued', lease_owner = NULL, lease_until = NULL,
                            heartbeat_at = :now, next_retry_at = :now,
                            checkpoint_json = :checkpoint, error_code = 'lease-expired',
                            updated_at = :now
                        WHERE run_id = :run_id AND partition_key = :partition_key
                        """
                    ),
                    {
                        "now": now,
                        "checkpoint": json.dumps(
                            {
                                "stage": str(row["stage"]),
                                "sourceBatchId": (
                                    None
                                    if row["last_source_batch_id"] is None
                                    else str(row["last_source_batch_id"])
                                ),
                                "errorCode": "lease-expired",
                            },
                            separators=(",", ":"),
                        ),
                        "run_id": run_id,
                        "partition_key": partition_key,
                    },
                )
                connection.execute(
                    text(
                        """
                        UPDATE sync_run
                        SET status = 'queued', finished_at = NULL
                        WHERE run_id = :run_id
                        """
                    ),
                    {"run_id": run_id},
                )
        return len(expired_rows)

    def list_queued_runs(self) -> Sequence[QueuedSectorEodRun]:
        """读取当前 queued 分区的稳定 scheme/date，reaper 不直接解释 checkpoint 或来源字段。"""
        with self._database.engine.connect() as connection:
            rows = (
                connection.execute(
                    text(
                        """
                        SELECT scheme, trade_date
                        FROM sector_eod_sync_partition
                        WHERE status = 'queued'
                        ORDER BY trade_date ASC, scheme ASC
                        """
                    )
                )
                .mappings()
                .all()
            )
        return tuple(
            QueuedSectorEodRun(
                scheme=SectorScheme(str(row["scheme"])),
                trade_date=row["trade_date"],
            )
            for row in rows
        )

    def mark_failed(self, *, run: SectorEodRun, error_code: str) -> None:
        """保存稳定错误码并释放当前租约，raw archived 后可由新 owner 接管。"""
        now = datetime.now(UTC)
        with self._database.engine.begin() as connection:
            _update_run_checkpoint(
                connection,
                run=run,
                stage=None,
                status="failed",
                source_batch_id=None,
                error_code=error_code,
                now=now,
                release_lease=True,
            )
            connection.execute(
                text(
                    """
                    UPDATE sync_run
                    SET status = 'failed', finished_at = :now
                    WHERE run_id = :run_id
                    """
                ),
                {"run_id": run.run_id, "now": now},
            )

    def store_quarantined_snapshot(
        self,
        *,
        scheme: SectorScheme,
        trade_date: date,
        source_cutoff_at: datetime,
        observed_at: datetime,
        quotes: Sequence[SectorEodQuote],
        provider_id: str,
        source_payload_sha256: str,
        raw_uri: str,
        adapter_version: str,
        schema_fingerprint: str,
        run: SectorEodRun,
        source_batch_id: UUID,
        quality_results: Sequence[SectorEodQualityResult],
    ) -> None:
        """保存完整阻断候选及质量证据，既不 supersede 旧版本也不创建 publication。"""
        _validate_publish_input(
            scheme=scheme,
            source_cutoff_at=source_cutoff_at,
            observed_at=observed_at,
            quotes=quotes,
            source_payload_sha256=source_payload_sha256,
            schema_fingerprint=schema_fingerprint,
        )
        _validate_quarantined_quality_results(quality_results)
        with self._database.engine.begin() as connection:
            _assert_active_run(connection, run=run, now=datetime.now(UTC))
            _ensure_source_batch_belongs_to_run(
                connection, source_batch_id=source_batch_id, run=run
            )
            active_sectors = _active_sectors_for_update(connection, scheme=scheme)
            _require_complete_coverage(active_sectors=active_sectors, quotes=quotes)
            snapshot_id = uuid4()
            connection.execute(
                text(
                    """
                    INSERT INTO sector_eod_snapshot (
                      snapshot_id, data_version, scheme, trade_date, revision, source_cutoff_at,
                      observed_at, finality, state, quality_status, record_count, expected_count,
                      coverage_ratio, normalizer_version, content_sha256, source_batch_id,
                      created_at, published_at, superseded_at
                    ) VALUES (
                      :snapshot_id, :data_version, :scheme, :trade_date, :revision,
                      :source_cutoff_at, :observed_at, 'post_close_observation', 'quarantined',
                      'quarantined', :record_count, :expected_count, 1, :normalizer_version,
                      :content_sha256, :source_batch_id, :created_at, NULL, NULL
                    )
                    """
                ),
                {
                    "snapshot_id": snapshot_id,
                    "data_version": uuid4(),
                    "scheme": scheme.value,
                    "trade_date": trade_date,
                    "revision": _next_revision(connection, scheme=scheme, trade_date=trade_date),
                    "source_cutoff_at": source_cutoff_at,
                    "observed_at": observed_at,
                    "record_count": len(quotes),
                    "expected_count": len(active_sectors),
                    "normalizer_version": _NORMALIZER_VERSION,
                    "content_sha256": _snapshot_content_hash(quotes),
                    "source_batch_id": source_batch_id,
                    "created_at": observed_at,
                },
            )
            _insert_quotes(
                connection,
                snapshot_id=snapshot_id,
                quotes=quotes,
                active_sectors=active_sectors,
            )
            _insert_quality_results(
                connection,
                snapshot_id=snapshot_id,
                extra_results=quality_results,
                record_count=len(quotes),
                expected_count=len(active_sectors),
            )

    def publish_snapshot(
        self,
        *,
        scheme: SectorScheme,
        trade_date: date,
        source_cutoff_at: datetime,
        observed_at: datetime,
        quotes: Sequence[SectorEodQuote],
        provider_id: str,
        source_payload_sha256: str,
        raw_uri: str,
        adapter_version: str,
        schema_fingerprint: str,
        run: SectorEodRun | None = None,
        source_batch_id: UUID | None = None,
        quality_status: str = "passed",
        quality_results: Sequence[SectorEodQualityResult] = (),
    ) -> PublishedSectorEodSnapshot:
        """记录来源观察并在同一分区内容变化且覆盖完整时原子替换发布。"""
        _validate_publish_input(
            scheme=scheme,
            source_cutoff_at=source_cutoff_at,
            observed_at=observed_at,
            quotes=quotes,
            source_payload_sha256=source_payload_sha256,
            schema_fingerprint=schema_fingerprint,
        )
        _validate_quality_results(status=quality_status, results=quality_results)
        if source_batch_id is None:
            # 兼容旧手工调用；生产运行先由 checkpoint 登记 raw，再传入 source batch。
            with self._database.engine.begin() as connection:
                source_batch_id = record_source_observation(
                    connection,
                    provider_id=provider_id,
                    capability=_CAPABILITY,
                    source_payload_sha256=source_payload_sha256,
                    raw_uri=raw_uri,
                    observed_at=observed_at,
                    created_at=observed_at,
                    upstream_source=provider_id,
                    adapter_version=adapter_version,
                    schema_fingerprint=schema_fingerprint,
                )
        elif run is None:
            raise ValueError("sector eod source batch requires a fenced run")
        with self._database.engine.begin() as connection:
            if run is not None:
                _assert_active_run(connection, run=run, now=datetime.now(UTC))
                _ensure_source_batch_belongs_to_run(
                    connection, source_batch_id=source_batch_id, run=run
                )
            active_sectors = _active_sectors_for_update(connection, scheme=scheme)
            _require_complete_coverage(active_sectors=active_sectors, quotes=quotes)
            if run is not None:
                _update_run_checkpoint(
                    connection,
                    run=run,
                    stage="quality_passed",
                    status="running",
                    source_batch_id=None,
                    error_code=None,
                    now=observed_at,
                )
            content_sha256 = _snapshot_content_hash(quotes)
            existing = _current_snapshot_for_update(
                connection, scheme=scheme, trade_date=trade_date
            )
            if (
                existing is not None
                and bytes(existing["content_sha256"]) == content_sha256
                and str(existing["normalizer_version"]) == _NORMALIZER_VERSION
            ):
                _record_noop_quality(connection, snapshot_id=UUID(str(existing["snapshot_id"])))
                if run is not None:
                    _complete_run(connection, run=run, now=observed_at)
                return PublishedSectorEodSnapshot(snapshot=_snapshot(existing), inserted=False)
            revision = _next_revision(connection, scheme=scheme, trade_date=trade_date)
            snapshot_id = uuid4()
            data_version = uuid4()
            published_at = observed_at
            if existing is not None:
                _supersede_current_snapshot(
                    connection,
                    existing_snapshot_id=UUID(str(existing["snapshot_id"])),
                    scheme=scheme,
                    trade_date=trade_date,
                    superseded_at=published_at,
                )
            connection.execute(
                text(
                    """
                    INSERT INTO sector_eod_snapshot (
                      snapshot_id, data_version, scheme, trade_date, revision, source_cutoff_at,
                      observed_at, finality, state, quality_status, record_count, expected_count,
                      coverage_ratio, normalizer_version, content_sha256, source_batch_id,
                      created_at, published_at, superseded_at
                    ) VALUES (
                      :snapshot_id, :data_version, :scheme, :trade_date, :revision,
                      :source_cutoff_at, :observed_at, 'post_close_observation', 'published',
                      :quality_status, :record_count, :expected_count, 1, :normalizer_version,
                      :content_sha256, :source_batch_id, :created_at, :published_at, NULL
                    )
                    """
                ),
                {
                    "snapshot_id": snapshot_id,
                    "data_version": data_version,
                    "scheme": scheme.value,
                    "trade_date": trade_date,
                    "revision": revision,
                    "source_cutoff_at": source_cutoff_at,
                    "observed_at": observed_at,
                    "quality_status": quality_status,
                    "record_count": len(quotes),
                    "expected_count": len(active_sectors),
                    "normalizer_version": _NORMALIZER_VERSION,
                    "content_sha256": content_sha256,
                    "source_batch_id": source_batch_id,
                    "created_at": published_at,
                    "published_at": published_at,
                },
            )
            _insert_quotes(
                connection,
                snapshot_id=snapshot_id,
                quotes=quotes,
                active_sectors=active_sectors,
            )
            _insert_quality_results(
                connection,
                snapshot_id=snapshot_id,
                extra_results=quality_results,
                record_count=len(quotes),
                expected_count=len(active_sectors),
            )
            _publish_dataset(
                connection,
                scheme=scheme,
                trade_date=trade_date,
                data_version=data_version,
                published_at=published_at,
                quality_status=quality_status,
            )
            if run is not None:
                _complete_run(connection, run=run, now=published_at)
            return PublishedSectorEodSnapshot(
                snapshot=SectorEodSnapshot(
                    snapshot_id=snapshot_id,
                    data_version=data_version,
                    scheme=scheme,
                    trade_date=trade_date,
                    source_cutoff_at=source_cutoff_at,
                    observed_at=observed_at,
                    finality=SectorEodFinality.POST_CLOSE_OBSERVATION,
                    quality_status=quality_status,
                    published_at=published_at,
                ),
                inserted=True,
            )

    def store_shadow_snapshot(
        self,
        *,
        scheme: SectorScheme,
        trade_date: date,
        source_cutoff_at: datetime,
        observed_at: datetime,
        quotes: Sequence[SectorEodQuote],
        provider_id: str,
        source_payload_sha256: str,
        raw_uri: str,
        adapter_version: str,
        schema_fingerprint: str,
        run: SectorEodRun,
        source_batch_id: UUID,
        quality_status: str,
        quality_results: Sequence[SectorEodQualityResult],
    ) -> PublishedSectorEodSnapshot:
        """保存通过质量门的 shadow candidate，不创建或改变任何消费者可见 publication。"""
        _validate_publish_input(
            scheme=scheme,
            source_cutoff_at=source_cutoff_at,
            observed_at=observed_at,
            quotes=quotes,
            source_payload_sha256=source_payload_sha256,
            schema_fingerprint=schema_fingerprint,
        )
        _validate_quality_results(status=quality_status, results=quality_results)
        with self._database.engine.begin() as connection:
            _assert_active_run(connection, run=run, now=datetime.now(UTC))
            _ensure_source_batch_belongs_to_run(
                connection, source_batch_id=source_batch_id, run=run
            )
            active_sectors = _active_sectors_for_update(connection, scheme=scheme)
            _require_complete_coverage(active_sectors=active_sectors, quotes=quotes)
            _update_run_checkpoint(
                connection,
                run=run,
                stage="quality_passed",
                status="running",
                source_batch_id=None,
                error_code=None,
                now=observed_at,
            )
            content_sha256 = _snapshot_content_hash(quotes)
            existing = _shadow_snapshot_for_update(
                connection,
                scheme=scheme,
                trade_date=trade_date,
                content_sha256=content_sha256,
            )
            if existing is not None:
                _record_noop_quality(connection, snapshot_id=UUID(str(existing["snapshot_id"])))
                _complete_run(connection, run=run, now=observed_at, stage="quality_passed")
                return PublishedSectorEodSnapshot(snapshot=_snapshot(existing), inserted=False)
            snapshot_id = uuid4()
            data_version = uuid4()
            connection.execute(
                text(
                    """
                    INSERT INTO sector_eod_snapshot (
                      snapshot_id, data_version, scheme, trade_date, revision, source_cutoff_at,
                      observed_at, finality, state, quality_status, record_count, expected_count,
                      coverage_ratio, normalizer_version, content_sha256, source_batch_id,
                      created_at, published_at, superseded_at
                    ) VALUES (
                      :snapshot_id, :data_version, :scheme, :trade_date, :revision,
                      :source_cutoff_at, :observed_at, 'post_close_observation', 'candidate',
                      :quality_status, :record_count, :expected_count, 1, :normalizer_version,
                      :content_sha256, :source_batch_id, :created_at, NULL, NULL
                    )
                    """
                ),
                {
                    "snapshot_id": snapshot_id,
                    "data_version": data_version,
                    "scheme": scheme.value,
                    "trade_date": trade_date,
                    "revision": _next_revision(connection, scheme=scheme, trade_date=trade_date),
                    "source_cutoff_at": source_cutoff_at,
                    "observed_at": observed_at,
                    "quality_status": quality_status,
                    "record_count": len(quotes),
                    "expected_count": len(active_sectors),
                    "normalizer_version": _NORMALIZER_VERSION,
                    "content_sha256": content_sha256,
                    "source_batch_id": source_batch_id,
                    "created_at": observed_at,
                },
            )
            _insert_quotes(
                connection,
                snapshot_id=snapshot_id,
                quotes=quotes,
                active_sectors=active_sectors,
            )
            _insert_quality_results(
                connection,
                snapshot_id=snapshot_id,
                extra_results=quality_results,
                record_count=len(quotes),
                expected_count=len(active_sectors),
            )
            _complete_run(connection, run=run, now=observed_at, stage="quality_passed")
        return PublishedSectorEodSnapshot(
            snapshot=SectorEodSnapshot(
                snapshot_id=snapshot_id,
                data_version=data_version,
                scheme=scheme,
                trade_date=trade_date,
                source_cutoff_at=source_cutoff_at,
                observed_at=observed_at,
                finality=SectorEodFinality.POST_CLOSE_OBSERVATION,
                quality_status=quality_status,
                published_at=None,
            ),
            inserted=True,
        )

    def get_published_snapshot(
        self, *, scheme: SectorScheme, trade_date: date | None
    ) -> SectorEodSnapshot | None:
        """读取最新或精确日期的当前 published 快照，不在指定日期缺失时回退。"""
        date_filter = "AND trade_date = :trade_date" if trade_date is not None else ""
        order = "" if trade_date is not None else "ORDER BY trade_date DESC"
        with self._database.engine.connect() as connection:
            row = (
                connection.execute(
                    text(
                        f"""
                        SELECT snapshot_id, data_version, scheme, trade_date, source_cutoff_at,
                               observed_at, finality, quality_status, published_at
                        FROM sector_eod_snapshot
                        WHERE scheme = :scheme
                          AND state = 'published'
                          AND superseded_at IS NULL
                          {date_filter}
                        {order}
                        LIMIT 1
                        """
                    ),
                    {"scheme": scheme.value, "trade_date": trade_date},
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else _snapshot(row)

    def rollback_published_snapshot(
        self, *, scheme: SectorScheme, trade_date: date, revision: int
    ) -> SectorEodSnapshot:
        """原子恢复指定通过 revision；candidate、quarantine、raw 与较新历史一律保留。"""
        if revision < 1:
            raise ValueError("sector eod rollback revision must be positive")
        now = datetime.now(UTC)
        with self._database.engine.begin() as connection:
            current = _current_snapshot_for_update(
                connection,
                scheme=scheme,
                trade_date=trade_date,
            )
            if current is None:
                raise ValueError("sector eod rollback requires a current published snapshot")
            target = (
                connection.execute(
                    text(
                        """
                        SELECT snapshot_id, data_version, scheme, trade_date, source_cutoff_at,
                               observed_at, finality, quality_status, published_at,
                               normalizer_version, content_sha256
                        FROM sector_eod_snapshot
                        WHERE scheme = :scheme
                          AND trade_date = :trade_date
                          AND revision = :revision
                          AND state = 'superseded'
                          AND quality_status IN ('passed', 'warned')
                        FOR UPDATE
                        """
                    ),
                    {
                        "scheme": scheme.value,
                        "trade_date": trade_date,
                        "revision": revision,
                    },
                )
                .mappings()
                .one_or_none()
            )
            if target is None:
                raise ValueError("sector eod rollback target is not a superseded passed revision")
            _supersede_current_snapshot(
                connection,
                existing_snapshot_id=UUID(str(current["snapshot_id"])),
                scheme=scheme,
                trade_date=trade_date,
                superseded_at=now,
            )
            connection.execute(
                text(
                    """
                    UPDATE sector_eod_snapshot
                    SET state = 'published', superseded_at = NULL
                    WHERE snapshot_id = :snapshot_id
                    """
                ),
                {"snapshot_id": target["snapshot_id"]},
            )
            connection.execute(
                text(
                    """
                    UPDATE dataset_publication
                    SET superseded_at = NULL
                    WHERE dataset = :dataset
                      AND partition_key = :partition_key
                      AND data_version = :data_version
                    """
                ),
                {
                    "dataset": _DATASET,
                    "partition_key": _partition_key(scheme, trade_date),
                    "data_version": target["data_version"],
                },
            )
        return _snapshot(target)

    def list_ranked_quotes(
        self,
        *,
        snapshot_id: UUID,
        sort: SectorEodSort,
        order: SortOrder,
        after_position: int | None,
        limit: int,
    ) -> Sequence[RankedSectorEodQuote]:
        """使用封闭字段映射计算 null-last、competition rank 和稳定页面位置。"""
        sort_column = _SORT_COLUMNS[sort]
        direction = _SORT_DIRECTIONS[order]
        rank_expression = (
            f"RANK() OVER (ORDER BY {sort_column} {direction} NULLS LAST)"
            if sort is SectorEodSort.CODE
            else (
                f"CASE WHEN {sort_column} IS NULL THEN NULL ELSE "
                f"RANK() OVER (ORDER BY {sort_column} {direction} NULLS LAST) END"
            )
        )
        with self._database.engine.connect() as connection:
            rows = (
                connection.execute(
                    text(
                        f"""
                        WITH ranked AS (
                          SELECT
                            sector.sector_id,
                            sector.scheme,
                            sector.sector_code,
                            quote.sector_name,
                            quote.latest_value,
                            quote.change_value,
                            quote.change_percent,
                            quote.market_value,
                            quote.turnover_percent,
                            quote.advancers,
                            quote.decliners,
                            quote.leader_name,
                            quote.leader_change_percent,
                            {rank_expression} AS rank,
                            ROW_NUMBER() OVER (
                              ORDER BY {sort_column} {direction} NULLS LAST,
                                       sector.sector_code COLLATE "C" ASC,
                                       sector.sector_id ASC
                            ) AS position
                          FROM sector_eod_quote quote
                          JOIN sector_entity sector ON sector.sector_key = quote.sector_key
                          WHERE quote.snapshot_id = :snapshot_id
                        )
                        SELECT * FROM ranked
                        WHERE position > :after_position
                        ORDER BY position ASC
                        LIMIT :limit
                        """
                    ),
                    {
                        "snapshot_id": snapshot_id,
                        "after_position": after_position or 0,
                        "limit": limit,
                    },
                )
                .mappings()
                .all()
            )
        return tuple(_ranked_quote(row) for row in rows)

    def get_snapshot_quote(
        self, *, snapshot_id: UUID, identifier: SectorIdentifier
    ) -> RankedSectorEodQuote | None:
        """读取快照中一个板块的原始报价；单资源响应不引入排名字段。"""
        with self._database.engine.connect() as connection:
            row = (
                connection.execute(
                    text(
                        """
                        SELECT
                          sector.sector_id, sector.scheme, sector.sector_code, quote.sector_name,
                          quote.latest_value, quote.change_value, quote.change_percent,
                          quote.market_value, quote.turnover_percent, quote.advancers,
                          quote.decliners, quote.leader_name, quote.leader_change_percent
                        FROM sector_eod_quote quote
                        JOIN sector_entity sector ON sector.sector_key = quote.sector_key
                        WHERE quote.snapshot_id = :snapshot_id
                          AND sector.scheme = :scheme
                          AND sector.sector_code = :sector_code
                        """
                    ),
                    {
                        "snapshot_id": snapshot_id,
                        "scheme": identifier.scheme.value,
                        "sector_code": identifier.code,
                    },
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else _quote_from_row(row, rank=None, position=1)


def _assert_active_run(connection: Connection, *, run: SectorEodRun, now: datetime) -> None:
    """验证 scheme/date、fencing token 与未过期租约，阻止僵尸 worker 提交。"""
    row = (
        connection.execute(
            text(
                """
                SELECT run_id
                FROM sector_eod_sync_partition
                WHERE scheme = :scheme
                  AND trade_date = :trade_date
                  AND run_id = :run_id
                  AND lease_token = :lease_token
                  AND lease_expires_at > :now
                FOR UPDATE
                """
            ),
            {
                "scheme": run.scheme.value,
                "trade_date": run.trade_date,
                "run_id": run.run_id,
                "lease_token": run.lease_token,
                "now": now,
            },
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise RuntimeError("sector eod lease is no longer active")


def _ensure_source_batch_belongs_to_run(
    connection: Connection, *, source_batch_id: UUID, run: SectorEodRun
) -> None:
    """确认待发布证据属于当前 run/partition，避免跨分区 raw 注入或错误 replay。"""
    row = (
        connection.execute(
            text(
                """
                SELECT source_batch_id
                FROM source_batch
                WHERE source_batch_id = :source_batch_id
                  AND run_id = :run_id
                  AND partition_key = :partition_key
                """
            ),
            {
                "source_batch_id": source_batch_id,
                "run_id": run.run_id,
                "partition_key": _partition_key(run.scheme, run.trade_date),
            },
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise ValueError("sector eod source batch does not belong to the fenced run")


def _update_run_checkpoint(
    connection: Connection,
    *,
    run: SectorEodRun,
    stage: str | None,
    status: str,
    source_batch_id: UUID | None,
    error_code: str | None,
    now: datetime,
    release_lease: bool = False,
) -> None:
    """以 fencing token 原子更新专用与通用 checkpoint，防止旧 worker 覆盖接管者。"""
    result = connection.execute(
        text(
            """
            UPDATE sector_eod_sync_partition
            SET status = :status,
                stage = COALESCE(:stage, stage),
                lease_owner = CASE WHEN :release_lease THEN NULL ELSE lease_owner END,
                lease_token = CASE WHEN :release_lease THEN NULL ELSE lease_token END,
                lease_expires_at = CASE WHEN :release_lease THEN NULL ELSE lease_expires_at END,
                last_source_batch_id = COALESCE(:source_batch_id, last_source_batch_id),
                last_error_code = :error_code,
                updated_at = :now
            WHERE scheme = :scheme
              AND trade_date = :trade_date
              AND run_id = :run_id
              AND lease_token = :lease_token
            """
        ),
        {
            "status": status,
            "stage": stage,
            "release_lease": release_lease,
            "source_batch_id": source_batch_id,
            "error_code": error_code,
            "now": now,
            "scheme": run.scheme.value,
            "trade_date": run.trade_date,
            "run_id": run.run_id,
            "lease_token": run.lease_token,
        },
    )
    if getattr(result, "rowcount", 1) == 0:
        return
    connection.execute(
        text(
            """
            UPDATE sync_partition
            SET status = :status,
                lease_owner = CASE WHEN :release_lease THEN NULL ELSE lease_owner END,
                lease_until = CASE WHEN :release_lease THEN NULL ELSE lease_until END,
                heartbeat_at = :now,
                checkpoint_json = :checkpoint,
                error_code = :error_code,
                updated_at = :now
            WHERE run_id = :run_id AND partition_key = :partition_key
            """
        ),
        {
            "status": status,
            "release_lease": release_lease,
            "now": now,
            "checkpoint": json.dumps(
                {
                    "stage": stage,
                    "sourceBatchId": None if source_batch_id is None else str(source_batch_id),
                    "errorCode": error_code,
                },
                separators=(",", ":"),
            ),
            "error_code": error_code,
            "run_id": run.run_id,
            "partition_key": _partition_key(run.scheme, run.trade_date),
        },
    )


def _complete_run(
    connection: Connection, *, run: SectorEodRun, now: datetime, stage: str = "published"
) -> None:
    """在 candidate 或 canonical 写入事务末尾关闭 checkpoint 与 run，保留真实完成阶段。"""
    _update_run_checkpoint(
        connection,
        run=run,
        stage=stage,
        status="succeeded",
        source_batch_id=None,
        error_code=None,
        now=now,
        release_lease=True,
    )
    connection.execute(
        text(
            """
            UPDATE sync_run
            SET status = 'succeeded', finished_at = :now
            WHERE run_id = :run_id
            """
        ),
        {"run_id": run.run_id, "now": now},
    )


def _validate_publish_input(
    *,
    scheme: SectorScheme,
    source_cutoff_at: datetime,
    observed_at: datetime,
    quotes: Sequence[SectorEodQuote],
    source_payload_sha256: str,
    schema_fingerprint: str,
) -> None:
    """在开启事务前校验跨层不变量，避免无效候选占用目录锁。"""
    if source_cutoff_at.tzinfo is None or observed_at.tzinfo is None:
        raise ValueError("sector eod timestamps must include a timezone")
    if observed_at < source_cutoff_at:
        raise ValueError("sector eod observation must not precede source cutoff")
    if not quotes or len(quotes) > 2000:
        raise ValueError("sector eod quote count must be from 1 to 2000")
    if len(source_payload_sha256) != 64 or len(schema_fingerprint) != 64:
        raise ValueError("sector eod digests must be SHA-256 hex strings")
    if any(quote.identifier.scheme is not scheme for quote in quotes):
        raise ValueError("sector eod quotes must all use the requested scheme")
    if len({quote.identifier.code for quote in quotes}) != len(quotes):
        raise ValueError("sector eod quotes must have unique codes")


def _validate_quality_results(*, status: str, results: Sequence[SectorEodQualityResult]) -> None:
    """阻断失败只能 quarantine，warning 失败必须显式反映为 warned 发布质量。"""
    if status not in {"passed", "warned"}:
        raise ValueError("sector eod published quality status is invalid")
    if any(not result.passed and result.severity == "blocking" for result in results):
        raise ValueError("sector eod blocking quality rule failed")
    if any(not result.passed and result.severity == "warning" for result in results):
        if status != "warned":
            raise ValueError("sector eod warning quality result requires warned status")


def _validate_quarantined_quality_results(results: Sequence[SectorEodQualityResult]) -> None:
    """只有至少一条阻断失败才能创建 quarantine，避免把正常 revision 错误隔离。"""
    if not any(not result.passed and result.severity == "blocking" for result in results):
        raise ValueError("sector eod quarantine requires a blocking quality result")


def _active_sectors_for_update(connection: Connection, *, scheme: SectorScheme) -> dict[str, int]:
    """冻结运行开始时的 ACTIVE 目录，阻止 EOD 用行情行猜测新增或退役。"""
    rows = (
        connection.execute(
            text(
                """
                SELECT sector_key, sector_code
                FROM sector_entity
                WHERE scheme = :scheme AND status = 'ACTIVE' AND name IS NOT NULL
                FOR SHARE
                """
            ),
            {"scheme": scheme.value},
        )
        .mappings()
        .all()
    )
    return {str(row["sector_code"]): int(row["sector_key"]) for row in rows}


def _require_complete_coverage(
    *, active_sectors: Mapping[str, int], quotes: Sequence[SectorEodQuote]
) -> None:
    """要求候选和冻结 ACTIVE catalog 完全等集，禁止 EOD partial 或未知代码发布。"""
    actual_codes = {quote.identifier.code for quote in quotes}
    if not active_sectors:
        raise ValueError("sector catalog has no active sectors")
    if actual_codes != set(active_sectors):
        raise ValueError("sector eod snapshot does not completely cover active catalog")


def _current_snapshot_for_update(
    connection: Connection, *, scheme: SectorScheme, trade_date: date
) -> Mapping[Any, Any] | None:
    """锁定分区当前版本，使同日修订仅能串行替换一次。"""
    return (
        connection.execute(
            text(
                """
                SELECT snapshot_id, data_version, scheme, trade_date, source_cutoff_at, observed_at,
                       finality, quality_status, published_at, normalizer_version, content_sha256
                FROM sector_eod_snapshot
                WHERE scheme = :scheme
                  AND trade_date = :trade_date
                  AND state = 'published'
                  AND superseded_at IS NULL
                FOR UPDATE
                """
            ),
            {"scheme": scheme.value, "trade_date": trade_date},
        )
        .mappings()
        .one_or_none()
    )


def _shadow_snapshot_for_update(
    connection: Connection,
    *,
    scheme: SectorScheme,
    trade_date: date,
    content_sha256: bytes,
) -> Mapping[Any, Any] | None:
    """锁定相同内容的候选，避免 shadow 重试为同一观察制造伪 revision。"""
    return (
        connection.execute(
            text(
                """
                SELECT snapshot_id, data_version, scheme, trade_date, source_cutoff_at, observed_at,
                       finality, quality_status, published_at, normalizer_version, content_sha256
                FROM sector_eod_snapshot
                WHERE scheme = :scheme
                  AND trade_date = :trade_date
                  AND state = 'candidate'
                  AND normalizer_version = :normalizer_version
                  AND content_sha256 = :content_sha256
                ORDER BY revision DESC
                LIMIT 1
                FOR UPDATE
                """
            ),
            {
                "scheme": scheme.value,
                "trade_date": trade_date,
                "normalizer_version": _NORMALIZER_VERSION,
                "content_sha256": content_sha256,
            },
        )
        .mappings()
        .one_or_none()
    )


def _next_revision(connection: Connection, *, scheme: SectorScheme, trade_date: date) -> int:
    """分配目标分区下一个单调 revision，不复用已 superseded 历史编号。"""
    value = connection.execute(
        text(
            """
            SELECT COALESCE(MAX(revision), 0) + 1
            FROM sector_eod_snapshot
            WHERE scheme = :scheme AND trade_date = :trade_date
            """
        ),
        {"scheme": scheme.value, "trade_date": trade_date},
    ).scalar_one()
    return int(value)


def _supersede_current_snapshot(
    connection: Connection,
    *,
    existing_snapshot_id: UUID,
    scheme: SectorScheme,
    trade_date: date,
    superseded_at: datetime,
) -> None:
    """关闭旧快照和旧 publication；错误 revision 与 raw 均保留供审计和回滚。"""
    connection.execute(
        text(
            """
            UPDATE sector_eod_snapshot
            SET state = 'superseded', superseded_at = :superseded_at
            WHERE snapshot_id = :snapshot_id
            """
        ),
        {"snapshot_id": existing_snapshot_id, "superseded_at": superseded_at},
    )
    connection.execute(
        text(
            """
            UPDATE dataset_publication
            SET superseded_at = :superseded_at
            WHERE dataset = :dataset
              AND partition_key = :partition_key
              AND superseded_at IS NULL
            """
        ),
        {
            "superseded_at": superseded_at,
            "dataset": _DATASET,
            "partition_key": _partition_key(scheme, trade_date),
        },
    )


def _insert_quotes(
    connection: Connection,
    *,
    snapshot_id: UUID,
    quotes: Sequence[SectorEodQuote],
    active_sectors: Mapping[str, int],
) -> None:
    """写入不可变报价行及行级摘要，不保存供应商默认排名或证券外键。"""
    for quote in quotes:
        row_sha256 = _quote_content_hash(quote)
        connection.execute(
            text(
                """
                INSERT INTO sector_eod_quote (
                  snapshot_id, sector_key, sector_name, latest_value, latest_value_unit,
                  change_value, change_percent, market_value, market_value_unit, turnover_percent,
                  advancers, decliners, leader_name, leader_change_percent, row_sha256
                ) VALUES (
                  :snapshot_id, :sector_key, :sector_name, :latest_value, 'provider_native',
                  :change_value, :change_percent, :market_value, 'provider_native',
                  :turnover_percent, :advancers, :decliners, :leader_name,
                  :leader_change_percent, :row_sha256
                )
                """
            ),
            {
                "snapshot_id": snapshot_id,
                "sector_key": active_sectors[quote.identifier.code],
                "sector_name": quote.name,
                "latest_value": quote.latest_value,
                "change_value": quote.change_value,
                "change_percent": quote.change_percent,
                "market_value": quote.market_value,
                "turnover_percent": quote.turnover_percent,
                "advancers": quote.advancers,
                "decliners": quote.decliners,
                "leader_name": quote.leader_name,
                "leader_change_percent": quote.leader_change_percent,
                "row_sha256": row_sha256,
            },
        )


def _insert_quality_results(
    connection: Connection,
    *,
    snapshot_id: UUID,
    extra_results: Sequence[SectorEodQualityResult],
    record_count: int,
    expected_count: int,
) -> None:
    """保存覆盖、记录数及应用质量门的结构化证据，不把 raw 响应复制到 PostgreSQL。"""
    baseline_results = (
        SectorEodQualityResult(
            rule_code="quality-policy-version",
            severity="info",
            passed=True,
            actual={"version": SECTOR_EOD_QUALITY_POLICY_VERSION},
            threshold={"frozen": "true"},
        ),
        SectorEodQualityResult(
            rule_code="catalog-coverage",
            severity="blocking",
            passed=True,
            actual={"covered": record_count},
            threshold={"expected": expected_count},
        ),
        SectorEodQualityResult(
            rule_code="record-count",
            severity="blocking",
            passed=True,
            actual={"count": record_count},
            threshold={"minimum": 1, "maximum": 2000},
        ),
    )
    unique_results = {result.rule_code: result for result in (*baseline_results, *extra_results)}
    for result in unique_results.values():
        connection.execute(
            text(
                """
                INSERT INTO sector_eod_quality_result (
                  quality_result_id, snapshot_id, rule_code, severity, passed, actual, threshold,
                  created_at
                ) VALUES (
                  :quality_result_id, :snapshot_id, :rule_code, :severity, :passed,
                  :actual, :threshold, CURRENT_TIMESTAMP
                )
                """
            ),
            {
                "quality_result_id": uuid4(),
                "snapshot_id": snapshot_id,
                "rule_code": result.rule_code,
                "severity": result.severity,
                "passed": result.passed,
                "actual": json.dumps(result.actual),
                "threshold": json.dumps(result.threshold),
            },
        )


def _record_noop_quality(connection: Connection, *, snapshot_id: UUID) -> None:
    """同内容重放只记录观察已复验，避免创建新的 canonical revision 或 dataVersion。"""
    connection.execute(
        text(
            """
            INSERT INTO sector_eod_quality_result (
              quality_result_id, snapshot_id, rule_code, severity, passed, actual, threshold,
              created_at
            ) VALUES (
              :quality_result_id, :snapshot_id, 'repeat-content', 'info', TRUE,
              :actual, :threshold, CURRENT_TIMESTAMP
            ) ON CONFLICT (snapshot_id, rule_code) DO NOTHING
            """
        ),
        {
            "quality_result_id": uuid4(),
            "snapshot_id": snapshot_id,
            "actual": json.dumps({"result": "same-content"}),
            "threshold": json.dumps({"required": "no-new-revision"}),
        },
    )


def _publish_dataset(
    connection: Connection,
    *,
    scheme: SectorScheme,
    trade_date: date,
    data_version: UUID,
    published_at: datetime,
    quality_status: str,
) -> None:
    """推进当前 dataset publication，使消费者只读到完整的新 EOD 版本。"""
    connection.execute(
        text(
            """
            UPDATE dataset_publication
            SET superseded_at = :published_at
            WHERE dataset = :dataset
              AND partition_key = :partition_key
              AND superseded_at IS NULL
            """
        ),
        {
            "published_at": published_at,
            "dataset": _DATASET,
            "partition_key": _partition_key(scheme, trade_date),
        },
    )
    connection.execute(
        text(
            """
            INSERT INTO dataset_publication (
              publication_id, dataset, partition_key, data_version, quality_status, published_at,
              superseded_at, effective_as_of, knowledge_cutoff
            ) VALUES (
              :publication_id, :dataset, :partition_key, :data_version, :quality_status,
              :published_at,
              NULL, :effective_as_of, :knowledge_cutoff
            )
            """
        ),
        {
            "publication_id": uuid4(),
            "dataset": _DATASET,
            "partition_key": _partition_key(scheme, trade_date),
            "data_version": data_version,
            "quality_status": quality_status,
            "published_at": published_at,
            "effective_as_of": trade_date,
            "knowledge_cutoff": published_at,
        },
    )


def _partition_key(scheme: SectorScheme, trade_date: date) -> str:
    """生成 dataset publication 和运维日志共用的稳定 EOD 分区键。"""
    return f"{scheme.value}:{trade_date.isoformat()}"


def _snapshot(row: Mapping[Any, Any]) -> SectorEodSnapshot:
    """将 published 快照 SQL 行转换为带时间与终态约束的领域对象。"""
    return SectorEodSnapshot(
        snapshot_id=UUID(str(row["snapshot_id"])),
        data_version=UUID(str(row["data_version"])),
        scheme=SectorScheme(str(row["scheme"])),
        trade_date=row["trade_date"],
        source_cutoff_at=row["source_cutoff_at"],
        observed_at=row["observed_at"],
        finality=SectorEodFinality(str(row["finality"])),
        quality_status=str(row["quality_status"]),
        published_at=row["published_at"],
    )


def _ranked_quote(row: Mapping[Any, Any]) -> RankedSectorEodQuote:
    """将含窗口函数结果的 SQL 行映射为带稳定位置的中立报价。"""
    return _quote_from_row(
        row,
        rank=None if row["rank"] is None else int(row["rank"]),
        position=int(row["position"]),
    )


def _quote_from_row(
    row: Mapping[Any, Any], *, rank: int | None, position: int
) -> RankedSectorEodQuote:
    """将通用报价 SQL 行投影为领域对象，不泄漏数据库数值主键。"""
    return RankedSectorEodQuote(
        sector_id=UUID(str(row["sector_id"])),
        quote=SectorEodQuote(
            identifier=SectorIdentifier(
                scheme=SectorScheme(str(row["scheme"])), code=str(row["sector_code"])
            ),
            name=str(row["sector_name"]),
            latest_value=_decimal_or_none(row["latest_value"]),
            change_value=_decimal_or_none(row["change_value"]),
            change_percent=_decimal_or_none(row["change_percent"]),
            market_value=_decimal_or_none(row["market_value"]),
            turnover_percent=_decimal_or_none(row["turnover_percent"]),
            advancers=None if row["advancers"] is None else int(row["advancers"]),
            decliners=None if row["decliners"] is None else int(row["decliners"]),
            leader_name=None if row["leader_name"] is None else str(row["leader_name"]),
            leader_change_percent=_decimal_or_none(row["leader_change_percent"]),
        ),
        rank=rank,
        position=position,
    )


def _snapshot_content_hash(quotes: Sequence[SectorEodQuote]) -> bytes:
    """对排序无关的完整业务横截面计算摘要，用于同内容 no-op 判断。"""
    return sector_eod_snapshot_content_sha256(quotes)


def _quote_content_hash(quote: SectorEodQuote) -> bytes:
    """为单行保存稳定内容摘要，支持审计差异而不复用供应商排名。"""
    return _snapshot_content_hash((quote,))


def _decimal_or_none(value: object) -> Decimal | None:
    """将数据库可空 NUMERIC 转回领域精确小数，保持 `null` 语义。"""
    return None if value is None else Decimal(str(value))
