"""板块 EOD 横截面、质量证据和确定性排行的 PostgreSQL 实现。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import case, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.orm import Session

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
from service_data_sync.infrastructure.database.models.execution.sync_partition import SyncPartition
from service_data_sync.infrastructure.database.models.execution.sync_run import SyncRun
from service_data_sync.infrastructure.database.models.provenance.source_batch import SourceBatch
from service_data_sync.infrastructure.database.models.publication.dataset_publication import (
    DatasetPublication,
)
from service_data_sync.infrastructure.database.models.sector.catalog.sector_entity import (
    SectorEntity,
)
from service_data_sync.infrastructure.database.models.sector.eod.sector_eod_quality_result import (
    SectorEodQualityResult as SectorEodQualityResultModel,
)
from service_data_sync.infrastructure.database.models.sector.eod.sector_eod_quote import (
    SectorEodQuote as SectorEodQuoteModel,
)
from service_data_sync.infrastructure.database.models.sector.eod.sector_eod_snapshot import (
    SectorEodSnapshot as SectorEodSnapshotModel,
)
from service_data_sync.infrastructure.database.models.sector.eod.sector_eod_sync_partition import (
    SectorEodSyncPartition,
)
from service_data_sync.infrastructure.persistence.source_batch import record_source_observation

_DATASET = "sector.quote.eod.snapshot"
_CAPABILITY = "sector.quote.eod.snapshot.raw"
_NORMALIZER_VERSION = "sector-eod-v1"
_LEASE_DURATION = timedelta(minutes=5)


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
        with self._database.transaction() as connection:
            existing = (
                connection.execute(
                    select(
                        SectorEodSyncPartition.status,
                        SectorEodSyncPartition.lease_expires_at,
                        SectorEodSyncPartition.last_source_batch_id,
                    )
                    .where(
                        SectorEodSyncPartition.scheme == scheme.value,
                        SectorEodSyncPartition.trade_date == trade_date,
                    )
                    .with_for_update()
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
            insert_run = postgresql_insert(SyncRun).values(
                run_id=uuid4(),
                capability=_CAPABILITY,
                mode="manual",
                request_key=request_key,
                target_date=trade_date,
                status="running",
                requested_at=now,
                started_at=now,
                finished_at=None,
                created_at=now,
            )
            run_id = UUID(
                str(
                    connection.execute(
                        insert_run.on_conflict_do_update(
                            index_elements=[SyncRun.request_key],
                            set_={
                                "status": "running",
                                "started_at": insert_run.excluded.started_at,
                                "finished_at": None,
                            },
                        ).returning(SyncRun.run_id)
                    ).scalar_one()
                )
            )
            owner = f"sector-eod:{run_id}:{lease_token}"
            lease_expires_at = now + _LEASE_DURATION
            insert_partition = postgresql_insert(SyncPartition).values(
                run_id=run_id,
                partition_key=partition_key,
                status="running",
                attempt=1,
                lease_owner=owner,
                lease_until=lease_expires_at,
                heartbeat_at=now,
                next_retry_at=None,
                checkpoint_json={"stage": "raw_archived" if reuse_archived_raw else "requested"},
                error_code=None,
                updated_at=now,
            )
            connection.execute(
                insert_partition.on_conflict_do_update(
                    index_elements=[SyncPartition.run_id, SyncPartition.partition_key],
                    set_={
                        "status": "running",
                        "attempt": SyncPartition.attempt + 1,
                        "lease_owner": insert_partition.excluded.lease_owner,
                        "lease_until": insert_partition.excluded.lease_until,
                        "heartbeat_at": insert_partition.excluded.heartbeat_at,
                        "next_retry_at": None,
                        "checkpoint_json": insert_partition.excluded.checkpoint_json,
                        "error_code": None,
                        "updated_at": insert_partition.excluded.updated_at,
                    },
                )
            )
            insert_eod_partition = postgresql_insert(SectorEodSyncPartition).values(
                scheme=scheme.value,
                trade_date=trade_date,
                run_id=run_id,
                status="running",
                stage="raw_archived" if reuse_archived_raw else "requested",
                attempt=1,
                lease_owner=owner,
                lease_token=lease_token,
                lease_expires_at=lease_expires_at,
                last_source_batch_id=last_source_batch_id,
                last_error_code=None,
                updated_at=now,
            )
            connection.execute(
                insert_eod_partition.on_conflict_do_update(
                    index_elements=[
                        SectorEodSyncPartition.scheme,
                        SectorEodSyncPartition.trade_date,
                    ],
                    set_={
                        "run_id": insert_eod_partition.excluded.run_id,
                        "status": "running",
                        "stage": insert_eod_partition.excluded.stage,
                        "attempt": SectorEodSyncPartition.attempt + 1,
                        "lease_owner": insert_eod_partition.excluded.lease_owner,
                        "lease_token": insert_eod_partition.excluded.lease_token,
                        "lease_expires_at": insert_eod_partition.excluded.lease_expires_at,
                        "last_source_batch_id": insert_eod_partition.excluded.last_source_batch_id,
                        "last_error_code": None,
                        "updated_at": insert_eod_partition.excluded.updated_at,
                    },
                )
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
        with self._database.transaction() as connection:
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
        with self._database.transaction() as connection:
            _assert_active_run(connection, run=run, now=now)
            row = (
                connection.execute(
                    select(
                        SourceBatch.source_batch_id,
                        SourceBatch.raw_uri,
                        SourceBatch.provider_id,
                        SourceBatch.observed_at,
                        SourceBatch.adapter_version,
                        SourceBatch.schema_fingerprint,
                    )
                    .select_from(SectorEodSyncPartition)
                    .join(
                        SourceBatch,
                        SourceBatch.source_batch_id == SectorEodSyncPartition.last_source_batch_id,
                    )
                    .where(
                        SectorEodSyncPartition.scheme == run.scheme.value,
                        SectorEodSyncPartition.trade_date == run.trade_date,
                        SectorEodSyncPartition.lease_token == run.lease_token,
                    )
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
        with self._database.session() as connection:
            value = connection.execute(
                select(
                    select(SectorEodSyncPartition.scheme)
                    .where(
                        SectorEodSyncPartition.scheme == scheme.value,
                        SectorEodSyncPartition.trade_date == trade_date,
                        SectorEodSyncPartition.last_source_batch_id.is_not(None),
                    )
                    .exists()
                )
            ).scalar_one()
        return bool(value)

    def get_historical_reference(
        self, *, scheme: SectorScheme, before_trade_date: date
    ) -> SectorEodHistoricalReference | None:
        """读取目标日之前最近 current published 快照及市值字段，隔离跨日质量查询。"""
        with self._database.session() as connection:
            snapshot = (
                connection.execute(
                    select(
                        SectorEodSnapshotModel.snapshot_id,
                        SectorEodSnapshotModel.trade_date,
                        SectorEodSnapshotModel.content_sha256,
                    )
                    .where(
                        SectorEodSnapshotModel.scheme == scheme.value,
                        SectorEodSnapshotModel.trade_date < before_trade_date,
                        SectorEodSnapshotModel.state == "published",
                        SectorEodSnapshotModel.superseded_at.is_(None),
                    )
                    .order_by(SectorEodSnapshotModel.trade_date.desc())
                    .limit(1)
                )
                .mappings()
                .one_or_none()
            )
            if snapshot is None:
                return None
            quote_rows = (
                connection.execute(
                    select(SectorEntity.sector_code, SectorEodQuoteModel.market_value)
                    .select_from(SectorEodQuoteModel)
                    .join(SectorEntity, SectorEntity.sector_key == SectorEodQuoteModel.sector_key)
                    .where(SectorEodQuoteModel.snapshot_id == snapshot["snapshot_id"])
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
        with self._database.transaction() as connection:
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
        with self._database.transaction() as connection:
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
        with self._database.transaction() as connection:
            result = connection.execute(
                update(SectorEodSyncPartition)
                .where(
                    SectorEodSyncPartition.scheme == run.scheme.value,
                    SectorEodSyncPartition.trade_date == run.trade_date,
                    SectorEodSyncPartition.run_id == run.run_id,
                    SectorEodSyncPartition.lease_token == run.lease_token,
                    SectorEodSyncPartition.status == "running",
                    SectorEodSyncPartition.lease_expires_at > now,
                )
                .values(lease_expires_at=lease_expires_at, updated_at=now)
            )
            if getattr(result, "rowcount", 1) == 0:
                raise RuntimeError("sector eod lease is no longer active")
            connection.execute(
                update(SyncPartition)
                .where(
                    SyncPartition.run_id == run.run_id,
                    SyncPartition.partition_key == _partition_key(run.scheme, run.trade_date),
                )
                .values(lease_until=lease_expires_at, heartbeat_at=now, updated_at=now)
            )

    def requeue_expired_leases(self, *, now: datetime) -> int:
        """将崩溃 worker 遗留的分区改回 queued，原始 checkpoint 和 source batch 保持不变。"""
        if now.tzinfo is None:
            raise ValueError("sector eod reaper time must include a timezone")
        with self._database.transaction() as connection:
            expired_rows = (
                connection.execute(
                    update(SectorEodSyncPartition)
                    .where(
                        SectorEodSyncPartition.status == "running",
                        SectorEodSyncPartition.lease_expires_at < now,
                    )
                    .values(
                        status="queued",
                        lease_owner=None,
                        lease_token=None,
                        lease_expires_at=None,
                        last_error_code="lease-expired",
                        updated_at=now,
                    )
                    .returning(
                        SectorEodSyncPartition.run_id,
                        SectorEodSyncPartition.scheme,
                        SectorEodSyncPartition.trade_date,
                        SectorEodSyncPartition.stage,
                        SectorEodSyncPartition.last_source_batch_id,
                    )
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
                    update(SyncPartition)
                    .where(
                        SyncPartition.run_id == run_id, SyncPartition.partition_key == partition_key
                    )
                    .values(
                        status="queued",
                        lease_owner=None,
                        lease_until=None,
                        heartbeat_at=now,
                        next_retry_at=now,
                        checkpoint_json={
                            "stage": str(row["stage"]),
                            "sourceBatchId": None
                            if row["last_source_batch_id"] is None
                            else str(row["last_source_batch_id"]),
                            "errorCode": "lease-expired",
                        },
                        error_code="lease-expired",
                        updated_at=now,
                    )
                )
                connection.execute(
                    update(SyncRun)
                    .where(SyncRun.run_id == run_id)
                    .values(status="queued", finished_at=None)
                )
        return len(expired_rows)

    def list_queued_runs(self) -> Sequence[QueuedSectorEodRun]:
        """读取当前 queued 分区的稳定 scheme/date，reaper 不直接解释 checkpoint 或来源字段。"""
        with self._database.session() as connection:
            rows = (
                connection.execute(
                    select(SectorEodSyncPartition.scheme, SectorEodSyncPartition.trade_date)
                    .where(SectorEodSyncPartition.status == "queued")
                    .order_by(SectorEodSyncPartition.trade_date, SectorEodSyncPartition.scheme)
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
        with self._database.transaction() as connection:
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
                update(SyncRun)
                .where(SyncRun.run_id == run.run_id)
                .values(status="failed", finished_at=now)
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
        with self._database.transaction() as connection:
            _assert_active_run(connection, run=run, now=datetime.now(UTC))
            _ensure_source_batch_belongs_to_run(
                connection, source_batch_id=source_batch_id, run=run
            )
            active_sectors = _active_sectors_for_update(connection, scheme=scheme)
            _require_complete_coverage(active_sectors=active_sectors, quotes=quotes)
            snapshot_id = uuid4()
            connection.execute(
                insert(SectorEodSnapshotModel).values(
                    snapshot_id=snapshot_id,
                    data_version=uuid4(),
                    scheme=scheme.value,
                    trade_date=trade_date,
                    revision=_next_revision(connection, scheme=scheme, trade_date=trade_date),
                    source_cutoff_at=source_cutoff_at,
                    observed_at=observed_at,
                    finality="post_close_observation",
                    state="quarantined",
                    quality_status="quarantined",
                    record_count=len(quotes),
                    expected_count=len(active_sectors),
                    coverage_ratio=1,
                    normalizer_version=_NORMALIZER_VERSION,
                    content_sha256=_snapshot_content_hash(quotes),
                    source_batch_id=source_batch_id,
                    created_at=observed_at,
                    published_at=None,
                    superseded_at=None,
                )
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
            with self._database.transaction() as connection:
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
        with self._database.transaction() as connection:
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
                insert(SectorEodSnapshotModel).values(
                    snapshot_id=snapshot_id,
                    data_version=data_version,
                    scheme=scheme.value,
                    trade_date=trade_date,
                    revision=revision,
                    source_cutoff_at=source_cutoff_at,
                    observed_at=observed_at,
                    finality="post_close_observation",
                    state="published",
                    quality_status=quality_status,
                    record_count=len(quotes),
                    expected_count=len(active_sectors),
                    coverage_ratio=1,
                    normalizer_version=_NORMALIZER_VERSION,
                    content_sha256=content_sha256,
                    source_batch_id=source_batch_id,
                    created_at=published_at,
                    published_at=published_at,
                    superseded_at=None,
                )
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
        with self._database.transaction() as connection:
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
                insert(SectorEodSnapshotModel).values(
                    snapshot_id=snapshot_id,
                    data_version=data_version,
                    scheme=scheme.value,
                    trade_date=trade_date,
                    revision=_next_revision(connection, scheme=scheme, trade_date=trade_date),
                    source_cutoff_at=source_cutoff_at,
                    observed_at=observed_at,
                    finality="post_close_observation",
                    state="candidate",
                    quality_status=quality_status,
                    record_count=len(quotes),
                    expected_count=len(active_sectors),
                    coverage_ratio=1,
                    normalizer_version=_NORMALIZER_VERSION,
                    content_sha256=content_sha256,
                    source_batch_id=source_batch_id,
                    created_at=observed_at,
                    published_at=None,
                    superseded_at=None,
                )
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
        statement = select(
            SectorEodSnapshotModel.snapshot_id,
            SectorEodSnapshotModel.data_version,
            SectorEodSnapshotModel.scheme,
            SectorEodSnapshotModel.trade_date,
            SectorEodSnapshotModel.source_cutoff_at,
            SectorEodSnapshotModel.observed_at,
            SectorEodSnapshotModel.finality,
            SectorEodSnapshotModel.quality_status,
            SectorEodSnapshotModel.published_at,
        ).where(
            SectorEodSnapshotModel.scheme == scheme.value,
            SectorEodSnapshotModel.state == "published",
            SectorEodSnapshotModel.superseded_at.is_(None),
        )
        if trade_date is None:
            statement = statement.order_by(SectorEodSnapshotModel.trade_date.desc())
        else:
            statement = statement.where(SectorEodSnapshotModel.trade_date == trade_date)
        with self._database.session() as connection:
            row = connection.execute(statement.limit(1)).mappings().one_or_none()
        return None if row is None else _snapshot(row)

    def rollback_published_snapshot(
        self, *, scheme: SectorScheme, trade_date: date, revision: int
    ) -> SectorEodSnapshot:
        """原子恢复指定通过 revision；candidate、quarantine、raw 与较新历史一律保留。"""
        if revision < 1:
            raise ValueError("sector eod rollback revision must be positive")
        now = datetime.now(UTC)
        with self._database.transaction() as connection:
            current = _current_snapshot_for_update(
                connection,
                scheme=scheme,
                trade_date=trade_date,
            )
            if current is None:
                raise ValueError("sector eod rollback requires a current published snapshot")
            target = (
                connection.execute(
                    select(
                        SectorEodSnapshotModel.snapshot_id,
                        SectorEodSnapshotModel.data_version,
                        SectorEodSnapshotModel.scheme,
                        SectorEodSnapshotModel.trade_date,
                        SectorEodSnapshotModel.source_cutoff_at,
                        SectorEodSnapshotModel.observed_at,
                        SectorEodSnapshotModel.finality,
                        SectorEodSnapshotModel.quality_status,
                        SectorEodSnapshotModel.published_at,
                        SectorEodSnapshotModel.normalizer_version,
                        SectorEodSnapshotModel.content_sha256,
                    )
                    .where(
                        SectorEodSnapshotModel.scheme == scheme.value,
                        SectorEodSnapshotModel.trade_date == trade_date,
                        SectorEodSnapshotModel.revision == revision,
                        SectorEodSnapshotModel.state == "superseded",
                        SectorEodSnapshotModel.quality_status.in_(["passed", "warned"]),
                    )
                    .with_for_update()
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
                update(SectorEodSnapshotModel)
                .where(SectorEodSnapshotModel.snapshot_id == target["snapshot_id"])
                .values(state="published", superseded_at=None)
            )
            connection.execute(
                update(DatasetPublication)
                .where(
                    DatasetPublication.dataset == _DATASET,
                    DatasetPublication.partition_key == _partition_key(scheme, trade_date),
                    DatasetPublication.data_version == target["data_version"],
                )
                .values(superseded_at=None)
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
        sort_column = {
            SectorEodSort.CHANGE_PERCENT: SectorEodQuoteModel.change_percent,
            SectorEodSort.TURNOVER_PERCENT: SectorEodQuoteModel.turnover_percent,
            SectorEodSort.MARKET_VALUE: SectorEodQuoteModel.market_value,
            SectorEodSort.LATEST_VALUE: SectorEodQuoteModel.latest_value,
            SectorEodSort.ADVANCERS: SectorEodQuoteModel.advancers,
            SectorEodSort.DECLINERS: SectorEodQuoteModel.decliners,
            SectorEodSort.LEADER_CHANGE_PERCENT: SectorEodQuoteModel.leader_change_percent,
            SectorEodSort.CODE: SectorEntity.sector_code.collate("C"),
        }[sort]
        ordered = (
            sort_column.asc().nulls_last()
            if order is SortOrder.ASC
            else sort_column.desc().nulls_last()
        )
        rank_value = func.rank().over(order_by=ordered)
        rank = (
            rank_value
            if sort is SectorEodSort.CODE
            else case((sort_column.is_(None), None), else_=rank_value)
        )
        ranked = (
            select(
                SectorEntity.sector_id,
                SectorEntity.scheme,
                SectorEntity.sector_code,
                SectorEodQuoteModel.sector_name,
                SectorEodQuoteModel.latest_value,
                SectorEodQuoteModel.change_value,
                SectorEodQuoteModel.change_percent,
                SectorEodQuoteModel.market_value,
                SectorEodQuoteModel.turnover_percent,
                SectorEodQuoteModel.advancers,
                SectorEodQuoteModel.decliners,
                SectorEodQuoteModel.leader_name,
                SectorEodQuoteModel.leader_change_percent,
                rank.label("rank"),
                func.row_number()
                .over(
                    order_by=(
                        ordered,
                        SectorEntity.sector_code.collate("C").asc(),
                        SectorEntity.sector_id.asc(),
                    )
                )
                .label("position"),
            )
            .select_from(SectorEodQuoteModel)
            .join(SectorEntity, SectorEntity.sector_key == SectorEodQuoteModel.sector_key)
            .where(SectorEodQuoteModel.snapshot_id == snapshot_id)
            .cte("ranked")
        )
        with self._database.session() as connection:
            rows = (
                connection.execute(
                    select(ranked)
                    .where(ranked.c.position > (after_position or 0))
                    .order_by(ranked.c.position)
                    .limit(limit)
                )
                .mappings()
                .all()
            )
        return tuple(_ranked_quote(row) for row in rows)

    def get_snapshot_quote(
        self, *, snapshot_id: UUID, identifier: SectorIdentifier
    ) -> RankedSectorEodQuote | None:
        """读取快照中一个板块的原始报价；单资源响应不引入排名字段。"""
        with self._database.session() as connection:
            row = (
                connection.execute(
                    select(
                        SectorEntity.sector_id,
                        SectorEntity.scheme,
                        SectorEntity.sector_code,
                        SectorEodQuoteModel.sector_name,
                        SectorEodQuoteModel.latest_value,
                        SectorEodQuoteModel.change_value,
                        SectorEodQuoteModel.change_percent,
                        SectorEodQuoteModel.market_value,
                        SectorEodQuoteModel.turnover_percent,
                        SectorEodQuoteModel.advancers,
                        SectorEodQuoteModel.decliners,
                        SectorEodQuoteModel.leader_name,
                        SectorEodQuoteModel.leader_change_percent,
                    )
                    .select_from(SectorEodQuoteModel)
                    .join(SectorEntity, SectorEntity.sector_key == SectorEodQuoteModel.sector_key)
                    .where(
                        SectorEodQuoteModel.snapshot_id == snapshot_id,
                        SectorEntity.scheme == identifier.scheme.value,
                        SectorEntity.sector_code == identifier.code,
                    )
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else _quote_from_row(row, rank=None, position=1)


def _assert_active_run(connection: Session, *, run: SectorEodRun, now: datetime) -> None:
    """验证 scheme/date、fencing token 与未过期租约，阻止僵尸 worker 提交。"""
    row = (
        connection.execute(
            select(SectorEodSyncPartition.run_id)
            .where(
                SectorEodSyncPartition.scheme == run.scheme.value,
                SectorEodSyncPartition.trade_date == run.trade_date,
                SectorEodSyncPartition.run_id == run.run_id,
                SectorEodSyncPartition.lease_token == run.lease_token,
                SectorEodSyncPartition.lease_expires_at > now,
            )
            .with_for_update()
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise RuntimeError("sector eod lease is no longer active")


def _ensure_source_batch_belongs_to_run(
    connection: Session, *, source_batch_id: UUID, run: SectorEodRun
) -> None:
    """确认待发布证据属于当前 run/partition，避免跨分区 raw 注入或错误 replay。"""
    row = (
        connection.execute(
            select(SourceBatch.source_batch_id).where(
                SourceBatch.source_batch_id == source_batch_id,
                SourceBatch.run_id == run.run_id,
                SourceBatch.partition_key == _partition_key(run.scheme, run.trade_date),
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise ValueError("sector eod source batch does not belong to the fenced run")


def _update_run_checkpoint(
    connection: Session,
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
    values: dict[str, object] = {"status": status, "last_error_code": error_code, "updated_at": now}
    if stage is not None:
        values["stage"] = stage
    if source_batch_id is not None:
        values["last_source_batch_id"] = source_batch_id
    if release_lease:
        values.update(lease_owner=None, lease_token=None, lease_expires_at=None)
    result = connection.execute(
        update(SectorEodSyncPartition)
        .where(
            SectorEodSyncPartition.scheme == run.scheme.value,
            SectorEodSyncPartition.trade_date == run.trade_date,
            SectorEodSyncPartition.run_id == run.run_id,
            SectorEodSyncPartition.lease_token == run.lease_token,
        )
        .values(**values)
    )
    if getattr(result, "rowcount", 1) == 0:
        return
    partition_values: dict[str, object] = {
        "status": status,
        "heartbeat_at": now,
        "checkpoint_json": {
            "stage": stage,
            "sourceBatchId": None if source_batch_id is None else str(source_batch_id),
            "errorCode": error_code,
        },
        "error_code": error_code,
        "updated_at": now,
    }
    if release_lease:
        partition_values.update(lease_owner=None, lease_until=None)
    connection.execute(
        update(SyncPartition)
        .where(
            SyncPartition.run_id == run.run_id,
            SyncPartition.partition_key == _partition_key(run.scheme, run.trade_date),
        )
        .values(**partition_values)
    )


def _complete_run(
    connection: Session, *, run: SectorEodRun, now: datetime, stage: str = "published"
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
        update(SyncRun)
        .where(SyncRun.run_id == run.run_id)
        .values(status="succeeded", finished_at=now)
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


def _active_sectors_for_update(connection: Session, *, scheme: SectorScheme) -> dict[str, int]:
    """冻结运行开始时的 ACTIVE 目录，阻止 EOD 用行情行猜测新增或退役。"""
    rows = (
        connection.execute(
            select(SectorEntity.sector_key, SectorEntity.sector_code)
            .where(
                SectorEntity.scheme == scheme.value,
                SectorEntity.status == "ACTIVE",
                SectorEntity.name.is_not(None),
            )
            .with_for_update(read=True)
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
    connection: Session, *, scheme: SectorScheme, trade_date: date
) -> Mapping[Any, Any] | None:
    """锁定分区当前版本，使同日修订仅能串行替换一次。"""
    return (
        connection.execute(
            select(
                SectorEodSnapshotModel.snapshot_id,
                SectorEodSnapshotModel.data_version,
                SectorEodSnapshotModel.scheme,
                SectorEodSnapshotModel.trade_date,
                SectorEodSnapshotModel.source_cutoff_at,
                SectorEodSnapshotModel.observed_at,
                SectorEodSnapshotModel.finality,
                SectorEodSnapshotModel.quality_status,
                SectorEodSnapshotModel.published_at,
                SectorEodSnapshotModel.normalizer_version,
                SectorEodSnapshotModel.content_sha256,
            )
            .where(
                SectorEodSnapshotModel.scheme == scheme.value,
                SectorEodSnapshotModel.trade_date == trade_date,
                SectorEodSnapshotModel.state == "published",
                SectorEodSnapshotModel.superseded_at.is_(None),
            )
            .with_for_update()
        )
        .mappings()
        .one_or_none()
    )


def _shadow_snapshot_for_update(
    connection: Session,
    *,
    scheme: SectorScheme,
    trade_date: date,
    content_sha256: bytes,
) -> Mapping[Any, Any] | None:
    """锁定相同内容的候选，避免 shadow 重试为同一观察制造伪 revision。"""
    return (
        connection.execute(
            select(
                SectorEodSnapshotModel.snapshot_id,
                SectorEodSnapshotModel.data_version,
                SectorEodSnapshotModel.scheme,
                SectorEodSnapshotModel.trade_date,
                SectorEodSnapshotModel.source_cutoff_at,
                SectorEodSnapshotModel.observed_at,
                SectorEodSnapshotModel.finality,
                SectorEodSnapshotModel.quality_status,
                SectorEodSnapshotModel.published_at,
                SectorEodSnapshotModel.normalizer_version,
                SectorEodSnapshotModel.content_sha256,
            )
            .where(
                SectorEodSnapshotModel.scheme == scheme.value,
                SectorEodSnapshotModel.trade_date == trade_date,
                SectorEodSnapshotModel.state == "candidate",
                SectorEodSnapshotModel.normalizer_version == _NORMALIZER_VERSION,
                SectorEodSnapshotModel.content_sha256 == content_sha256,
            )
            .order_by(SectorEodSnapshotModel.revision.desc())
            .limit(1)
            .with_for_update()
        )
        .mappings()
        .one_or_none()
    )


def _next_revision(connection: Session, *, scheme: SectorScheme, trade_date: date) -> int:
    """分配目标分区下一个单调 revision，不复用已 superseded 历史编号。"""
    value = connection.execute(
        select(func.coalesce(func.max(SectorEodSnapshotModel.revision), 0) + 1).where(
            SectorEodSnapshotModel.scheme == scheme.value,
            SectorEodSnapshotModel.trade_date == trade_date,
        )
    ).scalar_one()
    return int(value)


def _supersede_current_snapshot(
    connection: Session,
    *,
    existing_snapshot_id: UUID,
    scheme: SectorScheme,
    trade_date: date,
    superseded_at: datetime,
) -> None:
    """关闭旧快照和旧 publication；错误 revision 与 raw 均保留供审计和回滚。"""
    connection.execute(
        update(SectorEodSnapshotModel)
        .where(SectorEodSnapshotModel.snapshot_id == existing_snapshot_id)
        .values(state="superseded", superseded_at=superseded_at)
    )
    connection.execute(
        update(DatasetPublication)
        .where(
            DatasetPublication.dataset == _DATASET,
            DatasetPublication.partition_key == _partition_key(scheme, trade_date),
            DatasetPublication.superseded_at.is_(None),
        )
        .values(superseded_at=superseded_at)
    )


def _insert_quotes(
    connection: Session,
    *,
    snapshot_id: UUID,
    quotes: Sequence[SectorEodQuote],
    active_sectors: Mapping[str, int],
) -> None:
    """写入不可变报价行及行级摘要，不保存供应商默认排名或证券外键。"""
    for quote in quotes:
        row_sha256 = _quote_content_hash(quote)
        connection.execute(
            insert(SectorEodQuoteModel).values(
                snapshot_id=snapshot_id,
                sector_key=active_sectors[quote.identifier.code],
                sector_name=quote.name,
                latest_value=quote.latest_value,
                latest_value_unit="provider_native",
                change_value=quote.change_value,
                change_percent=quote.change_percent,
                market_value=quote.market_value,
                market_value_unit="provider_native",
                turnover_percent=quote.turnover_percent,
                advancers=quote.advancers,
                decliners=quote.decliners,
                leader_name=quote.leader_name,
                leader_change_percent=quote.leader_change_percent,
                row_sha256=row_sha256,
            )
        )


def _insert_quality_results(
    connection: Session,
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
            insert(SectorEodQualityResultModel).values(
                quality_result_id=uuid4(),
                snapshot_id=snapshot_id,
                rule_code=result.rule_code,
                severity=result.severity,
                passed=result.passed,
                actual=result.actual,
                threshold=result.threshold,
                created_at=func.current_timestamp(),
            )
        )


def _record_noop_quality(connection: Session, *, snapshot_id: UUID) -> None:
    """同内容重放只记录观察已复验，避免创建新的 canonical revision 或 dataVersion。"""
    insert_quality = postgresql_insert(SectorEodQualityResultModel).values(
        quality_result_id=uuid4(),
        snapshot_id=snapshot_id,
        rule_code="repeat-content",
        severity="info",
        passed=True,
        actual={"result": "same-content"},
        threshold={"required": "no-new-revision"},
        created_at=func.current_timestamp(),
    )
    connection.execute(
        insert_quality.on_conflict_do_nothing(
            index_elements=[
                SectorEodQualityResultModel.snapshot_id,
                SectorEodQualityResultModel.rule_code,
            ]
        )
    )


def _publish_dataset(
    connection: Session,
    *,
    scheme: SectorScheme,
    trade_date: date,
    data_version: UUID,
    published_at: datetime,
    quality_status: str,
) -> None:
    """推进当前 dataset publication，使消费者只读到完整的新 EOD 版本。"""
    connection.execute(
        update(DatasetPublication)
        .where(
            DatasetPublication.dataset == _DATASET,
            DatasetPublication.partition_key == _partition_key(scheme, trade_date),
            DatasetPublication.superseded_at.is_(None),
        )
        .values(superseded_at=published_at)
    )
    connection.execute(
        insert(DatasetPublication).values(
            publication_id=uuid4(),
            dataset=_DATASET,
            partition_key=_partition_key(scheme, trade_date),
            data_version=data_version,
            quality_status=quality_status,
            published_at=published_at,
            superseded_at=None,
            effective_as_of=trade_date,
            knowledge_cutoff=published_at,
        )
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
