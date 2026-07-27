"""使用 PostgreSQL 保存板块成分观测快照、半开区间和固定 release 清单。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Engine, text
from sqlalchemy.engine import Connection

from service_data_sync.application.ports.sector_market_data import StoredSector
from service_data_sync.application.ports.sector_membership import (
    PublishedSectorMembershipRelease,
    PublishedSectorMembershipSnapshot,
    SectorMembershipRepository,
    SectorMembershipRun,
    StoredEquityMembership,
    StoredMembershipConstituent,
    StoredMembershipEquity,
    StoredSectorMembershipRelease,
)
from service_data_sync.domain.equity import Exchange
from service_data_sync.domain.equity_master import EquityIdentityResolutionStatus
from service_data_sync.domain.sector import (
    SectorIdentifier,
    SectorMembershipCandidate,
    SectorScheme,
)
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.persistence.equity_identity_resolver import (
    resolve_identity_on_connection,
)
from service_data_sync.infrastructure.persistence.source_batch import record_source_observation

_CAPABILITY = "sector.membership.snapshot.raw"
_DATASET = "sector.membership.release"
_LEASE_DURATION = timedelta(minutes=20)


class SqlAlchemySectorMembershipRepository(SectorMembershipRepository):
    """独占板块成分 canonical 存储；所有区间仅表达来源完整快照的观测边界。"""

    def __init__(self, database: DatabaseClient) -> None:
        """保存服务私有数据库引擎，调用方不接触 SQLAlchemy 或数据表。"""
        self._engine: Engine = database.engine

    def list_active_sectors(self, *, scheme: SectorScheme) -> Sequence[StoredSector]:
        """读取一个分类体系当前 ACTIVE 板块，供一次 scheme run 冻结分区集合。"""
        with self._engine.connect() as connection:
            rows = (
                connection.execute(
                    text(
                        """
                        SELECT sector_key, sector_id, scheme, sector_code, name, status
                        FROM sector_entity
                        WHERE scheme = :scheme AND status = 'ACTIVE'
                        ORDER BY sector_code, sector_id
                        """
                    ),
                    {"scheme": scheme.value},
                )
                .mappings()
                .all()
            )
        return tuple(_stored_sector(row) for row in rows)

    def publish_snapshot(
        self,
        *,
        sector: StoredSector,
        observation_date: date,
        candidates: Sequence[SectorMembershipCandidate],
        provider_id: str,
        source_payload_sha256: str,
        raw_uri: str,
        observed_at: datetime,
        upstream_source: str | None,
        adapter_version: str,
        schema_fingerprint: str,
        run_id: UUID,
        partition_key: str,
    ) -> PublishedSectorMembershipSnapshot:
        """保存独立来源观测，只有完整且身份全确认的快照才差分观测区间。"""
        if not candidates:
            raise ValueError("membership candidates must not be empty")
        if observed_at.tzinfo is None:
            raise ValueError("observed_at must include a timezone")
        if sector.status != "ACTIVE":
            raise ValueError("membership requires an active sector")
        if len({candidate.source_symbol for candidate in candidates}) != len(candidates):
            raise ValueError("membership candidates must have unique symbols")
        now = datetime.now(UTC)
        content_hash = _candidate_hash(candidates)
        idempotency_key = _idempotency_key(sector.identifier, observation_date)
        with self._engine.begin() as connection:
            self._lock_sector(connection, sector.identifier)
            source_batch_id = record_source_observation(
                connection,
                provider_id=provider_id,
                capability=_CAPABILITY,
                source_payload_sha256=source_payload_sha256,
                raw_uri=raw_uri,
                observed_at=observed_at,
                created_at=now,
                upstream_source=upstream_source,
                adapter_version=adapter_version,
                schema_fingerprint=schema_fingerprint,
                run_id=run_id,
                partition_key=partition_key,
            )
            existing = self._existing_snapshot(connection, idempotency_key)
            if existing is not None:
                return PublishedSectorMembershipSnapshot(
                    snapshot_id=UUID(str(existing["snapshot_id"])),
                    observed_at=existing["observed_at"],
                    complete=existing["status"] == "COMPLETE",
                    inserted_interval_count=0,
                    closed_interval_count=0,
                    pending_count=int(existing["pending_count"]),
                    quarantine_count=int(existing["quarantine_count"]),
                )
            verified, pending, quarantined = self._resolve_candidates(
                connection,
                candidates=candidates,
                fact_date=observation_date,
                known_at=now,
            )
            snapshot_id = uuid4()
            quality_results = self._quality_results(
                connection,
                sector_key=sector.sector_key,
                observation_date=observation_date,
                verified_security_ids={security_id for security_id, _ in verified},
                has_unresolved=bool(pending or quarantined),
            )
            complete = not any(severity == "error" for _, severity, _, _, _ in quality_results)
            quality_status = (
                "rejected"
                if not complete
                else "warned"
                if any(severity == "warn" for _, severity, _, _, _ in quality_results)
                else "passed"
            )
            self._insert_snapshot(
                connection,
                snapshot_id=snapshot_id,
                sector_key=sector.sector_key,
                source_batch_id=source_batch_id,
                observed_at=observed_at,
                observation_date=observation_date,
                candidates=candidates,
                verified_count=len(verified),
                pending_count=len(pending),
                quarantine_count=len(quarantined),
                content_hash=content_hash,
                idempotency_key=idempotency_key,
                complete=complete,
                quality_status=quality_status,
            )
            self._insert_pending(connection, snapshot_id=snapshot_id, rows=pending, now=now)
            self._insert_quarantine(connection, snapshot_id=snapshot_id, rows=quarantined, now=now)
            self._insert_quality_results(
                connection, snapshot_id=snapshot_id, results=quality_results, now=now
            )
            if not complete:
                return PublishedSectorMembershipSnapshot(
                    snapshot_id=snapshot_id,
                    observed_at=observed_at,
                    complete=False,
                    inserted_interval_count=0,
                    closed_interval_count=0,
                    pending_count=len(pending),
                    quarantine_count=len(quarantined),
                )
            self._ensure_item_partition(connection, observation_date)
            self._insert_items(
                connection,
                snapshot_id=snapshot_id,
                snapshot_date=observation_date,
                verified=verified,
            )
            inserted_count, closed_count = self._advance_intervals(
                connection,
                sector_key=sector.sector_key,
                snapshot_id=snapshot_id,
                observed_at=observed_at,
                security_ids={security_id for security_id, _ in verified},
            )
        return PublishedSectorMembershipSnapshot(
            snapshot_id=snapshot_id,
            observed_at=observed_at,
            complete=True,
            inserted_interval_count=inserted_count,
            closed_interval_count=closed_count,
            pending_count=0,
            quarantine_count=0,
        )

    def start_run(
        self,
        *,
        scheme: SectorScheme,
        observation_date: date,
        sectors: Sequence[StoredSector],
    ) -> SectorMembershipRun:
        """创建或恢复一个幂等 scheme run，并为冻结板块写入可回收的 PostgreSQL lease。"""
        if not sectors:
            raise ValueError("sector membership run requires at least one sector")
        if any(
            sector.identifier.scheme is not scheme or sector.status != "ACTIVE"
            for sector in sectors
        ):
            raise ValueError("sector membership run requires active sectors from one scheme")
        now = datetime.now(UTC)
        request_key = _run_request_key(scheme, observation_date)
        with self._engine.begin() as connection:
            self._lock_scheme(connection, scheme)
            leased_partitions = (
                connection.execute(
                    text(
                        """
                        SELECT partition_key
                        FROM sync_partition
                        WHERE run_id = (
                          SELECT run_id
                          FROM sync_run
                          WHERE request_key = :request_key
                        )
                          AND partition_key = ANY(CAST(:partition_keys AS VARCHAR[]))
                          AND lease_until > :now
                        FOR UPDATE
                        """
                    ),
                    {
                        "request_key": request_key,
                        "partition_keys": [
                            _partition_key(sector.identifier, observation_date)
                            for sector in sectors
                        ],
                        "now": now,
                    },
                )
                .mappings()
                .all()
            )
            if leased_partitions:
                # 活跃租约代表另一个 worker 正在处理同一冻结集合。
                # 不能把该 worker 的 checkpoint 重置为新尝试。
                raise RuntimeError("sector membership run is already leased")
            row = (
                connection.execute(
                    text(
                        """
                        INSERT INTO sync_run (
                          run_id, capability, mode, request_key, target_date, status,
                          requested_at, started_at, finished_at, created_at
                        ) VALUES (
                          :run_id, :capability, 'manual', :request_key, :target_date, 'running',
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
                        "target_date": observation_date,
                        "now": now,
                    },
                )
                .mappings()
                .one()
            )
            run_id = UUID(str(row["run_id"]))
            for sector in sectors:
                connection.execute(
                    text(
                        """
                        INSERT INTO sync_partition (
                          run_id, partition_key, status, attempt, lease_owner, lease_until,
                          heartbeat_at, next_retry_at, checkpoint_json, error_code, updated_at
                        ) VALUES (
                          :run_id, :partition_key, 'running', 1, :lease_owner,
                          :lease_until, :now, NULL, NULL, NULL, :now
                        )
                        ON CONFLICT (run_id, partition_key) DO UPDATE
                        SET status = 'running',
                            attempt = sync_partition.attempt + 1,
                            lease_owner = EXCLUDED.lease_owner,
                            lease_until = EXCLUDED.lease_until,
                            heartbeat_at = EXCLUDED.heartbeat_at,
                            next_retry_at = NULL,
                            error_code = NULL,
                            updated_at = EXCLUDED.updated_at
                        """
                    ),
                    {
                        "run_id": run_id,
                        "partition_key": _partition_key(sector.identifier, observation_date),
                        "lease_owner": f"sector-membership:{run_id}",
                        "lease_until": now + _LEASE_DURATION,
                        "now": now,
                    },
                )
        return SectorMembershipRun(run_id=run_id, scheme=scheme, observation_date=observation_date)

    def mark_partition_completed(
        self,
        *,
        run: SectorMembershipRun,
        sector: StoredSector,
        publication: PublishedSectorMembershipSnapshot,
    ) -> None:
        """保存提交后的 snapshot checkpoint，隔离结果保留 partial 状态且绝不记录为成功。"""
        status = "succeeded" if publication.complete else "partial"
        checkpoint = json.dumps(
            {
                "snapshotId": str(publication.snapshot_id),
                "observedAt": publication.observed_at.isoformat(),
                "complete": publication.complete,
                "pendingCount": publication.pending_count,
                "quarantineCount": publication.quarantine_count,
            },
            separators=(",", ":"),
        )
        with self._engine.begin() as connection:
            self._update_partition(
                connection,
                run=run,
                sector=sector,
                status=status,
                checkpoint=checkpoint,
                error_code=None,
            )

    def mark_partition_failed(
        self,
        *,
        run: SectorMembershipRun,
        sector: StoredSector,
        error_code: str,
    ) -> None:
        """记录已经耗尽重试的来源失败并释放 lease，后续重跑可复用同一 partition。"""
        with self._engine.begin() as connection:
            self._update_partition(
                connection,
                run=run,
                sector=sector,
                status="failed",
                checkpoint=None,
                error_code=error_code,
            )

    def finish_run(self, *, run: SectorMembershipRun, status: str) -> None:
        """结束 run 并保留各分区 checkpoint；不接受未定义状态避免审计语义漂移。"""
        if status not in {"succeeded", "partial", "failed"}:
            raise ValueError("sector membership run status is invalid")
        with self._engine.begin() as connection:
            connection.execute(
                text(
                    """
                    UPDATE sync_run
                    SET status = :status, finished_at = :finished_at
                    WHERE run_id = :run_id
                    """
                ),
                {"status": status, "finished_at": datetime.now(UTC), "run_id": run.run_id},
            )

    def publish_release(
        self, *, scheme: SectorScheme, observation_date: date
    ) -> PublishedSectorMembershipRelease | None:
        """汇总冻结的 ACTIVE 板块快照，满足质量覆盖门才原子切换 scheme release。"""
        now = datetime.now(UTC)
        with self._engine.begin() as connection:
            self._lock_scheme(connection, scheme)
            sectors = self._active_sectors_on_connection(connection, scheme)
            if not sectors:
                return None
            components: list[tuple[StoredSector, Mapping[Any, Any], bool]] = []
            for sector in sectors:
                snapshot = self._latest_complete_snapshot(connection, sector.sector_key)
                if snapshot is None:
                    return None
                carried_forward = snapshot["observation_date"] != observation_date
                components.append((sector, snapshot, carried_forward))
            fresh_count = sum(not carried_forward for _, _, carried_forward in components)
            expected_count = len(components)
            carried_count = expected_count - fresh_count
            if fresh_count / expected_count < 0.98:
                return None
            quality_status = (
                "warned"
                if carried_count > 0
                or any(str(snapshot["quality_status"]) == "warned" for _, snapshot, _ in components)
                else "passed"
            )
            current = self._current_release(connection, scheme)
            component_snapshot_ids = tuple(
                UUID(str(snapshot["snapshot_id"])) for _, snapshot, _ in components
            )
            if current is not None and self._release_matches(
                connection, UUID(str(current["release_id"])), component_snapshot_ids, quality_status
            ):
                return PublishedSectorMembershipRelease(
                    release_id=UUID(str(current["release_id"])),
                    data_version=UUID(str(current["data_version"])),
                    quality_status=str(current["quality_status"]),
                    fresh_sector_count=int(current["fresh_sector_count"]),
                    carried_forward_sector_count=int(current["carried_forward_sector_count"]),
                    published_at=current["published_at"],
                )
            if current is not None:
                connection.execute(
                    text(
                        """
                        UPDATE sector_membership_release
                        SET superseded_at = :published_at
                        WHERE release_id = :release_id
                        """
                    ),
                    {"published_at": now, "release_id": current["release_id"]},
                )
            release_id = uuid4()
            data_version = uuid4()
            release_as_of = max(snapshot["observed_at"] for _, snapshot, _ in components)
            coverage_start = min(snapshot["observed_at"] for _, snapshot, _ in components)
            connection.execute(
                text(
                    """
                    INSERT INTO sector_membership_release (
                      release_id, scheme, release_as_of, coverage_start, data_version,
                      quality_status,
                      expected_sector_count, fresh_sector_count, carried_forward_sector_count,
                      identity_coverage_percent, excluded_identity_count, published_at,
                      superseded_at
                    ) VALUES (
                      :release_id, :scheme, :release_as_of, :coverage_start, :data_version,
                      :quality_status, :expected_sector_count, :fresh_sector_count,
                      :carried_forward_sector_count, 100, 0, :published_at, NULL
                    )
                    """
                ),
                {
                    "release_id": release_id,
                    "scheme": scheme.value,
                    "release_as_of": release_as_of,
                    "coverage_start": coverage_start,
                    "data_version": data_version,
                    "quality_status": quality_status,
                    "expected_sector_count": expected_count,
                    "fresh_sector_count": fresh_count,
                    "carried_forward_sector_count": carried_count,
                    "published_at": now,
                },
            )
            for sector, snapshot, carried_forward in components:
                connection.execute(
                    text(
                        """
                        INSERT INTO sector_membership_release_sector (
                          release_id, sector_key, snapshot_id, carried_forward, snapshot_observed_at
                        ) VALUES (
                          :release_id, :sector_key, :snapshot_id, :carried_forward,
                          :snapshot_observed_at
                        )
                        """
                    ),
                    {
                        "release_id": release_id,
                        "sector_key": sector.sector_key,
                        "snapshot_id": snapshot["snapshot_id"],
                        "carried_forward": carried_forward,
                        "snapshot_observed_at": snapshot["observed_at"],
                    },
                )
            self._publish_dataset(
                connection,
                scheme=scheme,
                data_version=data_version,
                quality_status=quality_status,
                effective_as_of=observation_date,
                published_at=now,
            )
        return PublishedSectorMembershipRelease(
            release_id=release_id,
            data_version=data_version,
            quality_status=quality_status,
            fresh_sector_count=fresh_count,
            carried_forward_sector_count=carried_count,
            published_at=now,
        )

    def get_release(
        self, *, scheme: SectorScheme, as_of: datetime | None
    ) -> StoredSectorMembershipRelease | None:
        """读取当前或历史最近 release，始终只暴露不可变已发布清单。"""
        query = (
            """
            SELECT release_id, scheme, release_as_of, coverage_start, data_version, quality_status,
                   carried_forward_sector_count, published_at
            FROM sector_membership_release
            WHERE scheme = :scheme AND superseded_at IS NULL
            """
            if as_of is None
            else """
            SELECT release_id, scheme, release_as_of, coverage_start, data_version, quality_status,
                   carried_forward_sector_count, published_at
            FROM sector_membership_release
            WHERE scheme = :scheme AND release_as_of <= :as_of
            ORDER BY release_as_of DESC
            LIMIT 1
            """
        )
        parameters: dict[str, object] = {"scheme": scheme.value}
        if as_of is not None:
            parameters["as_of"] = as_of
        with self._engine.connect() as connection:
            row = connection.execute(text(query), parameters).mappings().one_or_none()
        return None if row is None else _stored_release(row, requested_as_of=as_of)

    def get_release_sector(
        self, *, release_id: UUID, identifier: SectorIdentifier
    ) -> tuple[StoredSector, datetime, bool] | None:
        """读取 release 固定板块快照，不能回退到随后变更的当前快照。"""
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    text(
                        """
                        SELECT entity.sector_key, entity.sector_id, entity.scheme,
                               entity.sector_code,
                               entity.name, entity.status, component.snapshot_observed_at,
                               component.carried_forward
                        FROM sector_membership_release_sector AS component
                        INNER JOIN sector_entity AS entity
                          ON entity.sector_key = component.sector_key
                        WHERE component.release_id = :release_id
                          AND entity.scheme = :scheme
                          AND entity.sector_code = :sector_code
                        """
                    ),
                    {
                        "release_id": release_id,
                        "scheme": identifier.scheme.value,
                        "sector_code": identifier.code,
                    },
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            return None
        return _stored_sector(row), row["snapshot_observed_at"], bool(row["carried_forward"])

    def list_constituents(
        self,
        *,
        release_id: UUID,
        identifier: SectorIdentifier,
        after_exchange: Exchange | None,
        after_symbol: str | None,
        limit: int,
    ) -> Sequence[StoredMembershipConstituent]:
        """按 release 固定快照和其历史区间读取成分，不掺入 PENDING 或当前代码投影。"""
        if not 1 <= limit <= 501:
            raise ValueError("constituent limit must be from 1 to 501")
        if (after_exchange is None) != (after_symbol is None):
            raise ValueError("constituent cursor values must be supplied together")
        statement = text(
            """
            SELECT instrument.instrument_id, identifier_version.exchange, identifier_version.symbol,
                   name_version.name, status_version.status, membership.observed_from,
                   membership.observed_to
            FROM sector_membership_release_sector AS component
            INNER JOIN sector_entity AS sector ON sector.sector_key = component.sector_key
            INNER JOIN sector_membership_item AS item ON item.snapshot_id = component.snapshot_id
            INNER JOIN sector_membership_interval AS membership
              ON membership.sector_key = component.sector_key
             AND membership.security_id = item.security_id
             AND membership.observation_range @> component.snapshot_observed_at
            INNER JOIN sector_membership_release AS release
              ON release.release_id = component.release_id
            INNER JOIN equity_instrument AS instrument ON instrument.security_id = item.security_id
            INNER JOIN equity_identifier_version AS identifier_version
              ON identifier_version.security_id = item.security_id
             AND identifier_version.identity_state = 'CONFIRMED'
             AND identifier_version.effective_range @> component.snapshot_observed_at::date
             AND identifier_version.knowledge_range @> release.published_at
            INNER JOIN equity_name_version AS name_version
              ON name_version.security_id = item.security_id
             AND name_version.effective_range @> component.snapshot_observed_at::date
             AND name_version.knowledge_range @> release.published_at
            INNER JOIN equity_listing_status_version AS status_version
              ON status_version.security_id = item.security_id
             AND status_version.effective_range @> component.snapshot_observed_at::date
             AND status_version.knowledge_range @> release.published_at
            WHERE component.release_id = :release_id
              AND sector.scheme = :scheme
              AND sector.sector_code = :sector_code
              AND (
                :after_exchange IS NULL
                OR identifier_version.exchange > :after_exchange
                OR (
                  identifier_version.exchange = :after_exchange
                  AND identifier_version.symbol > :after_symbol
                )
              )
            ORDER BY identifier_version.exchange, identifier_version.symbol
            LIMIT :limit
            """
        )
        with self._engine.connect() as connection:
            rows = (
                connection.execute(
                    statement,
                    {
                        "release_id": release_id,
                        "scheme": identifier.scheme.value,
                        "sector_code": identifier.code,
                        "after_exchange": None if after_exchange is None else after_exchange.value,
                        "after_symbol": after_symbol,
                        "limit": limit,
                    },
                )
                .mappings()
                .all()
            )
        return tuple(_stored_constituent(row) for row in rows)

    def get_release_equity(
        self,
        *,
        release_id: UUID,
        exchange: Exchange,
        symbol: str,
    ) -> StoredMembershipEquity | None:
        """在 release 冻结的市场日与知识时刻解析反向查询身份，不读当前锚列。"""
        with self._engine.connect() as connection:
            row = (
                connection.execute(
                    text(
                        """
                        SELECT instrument.instrument_id, identifier_version.exchange,
                               identifier_version.symbol, name_version.name, status_version.status
                        FROM sector_membership_release AS release
                        INNER JOIN equity_identifier_version AS identifier_version
                          ON identifier_version.exchange = :exchange
                         AND identifier_version.symbol = :symbol
                         AND identifier_version.identity_state = 'CONFIRMED'
                         AND identifier_version.effective_range @> release.release_as_of::date
                         AND identifier_version.knowledge_range @> release.published_at
                        INNER JOIN equity_instrument AS instrument
                          ON instrument.security_id = identifier_version.security_id
                        INNER JOIN equity_name_version AS name_version
                          ON name_version.security_id = identifier_version.security_id
                         AND name_version.effective_range @> release.release_as_of::date
                         AND name_version.knowledge_range @> release.published_at
                        INNER JOIN equity_listing_status_version AS status_version
                          ON status_version.security_id = identifier_version.security_id
                         AND status_version.effective_range @> release.release_as_of::date
                         AND status_version.knowledge_range @> release.published_at
                        WHERE release.release_id = :release_id
                        """
                    ),
                    {"release_id": release_id, "exchange": exchange.value, "symbol": symbol},
                )
                .mappings()
                .one_or_none()
            )
        return None if row is None else _stored_membership_equity(row)

    def list_equity_memberships(
        self,
        *,
        release_id: UUID,
        instrument_id: UUID,
        after_sector_code: str | None,
        limit: int,
    ) -> Sequence[StoredEquityMembership]:
        """读取 release 清单中的反向归属，并按板块代码维持稳定游标顺序。"""
        if not 1 <= limit <= 501:
            raise ValueError("membership limit must be from 1 to 501")
        with self._engine.connect() as connection:
            rows = (
                connection.execute(
                    text(
                        """
                        SELECT sector.sector_key, sector.sector_id, sector.scheme,
                               sector.sector_code,
                               sector.name, sector.status, membership.observed_from,
                               membership.observed_to, component.snapshot_observed_at,
                               component.carried_forward
                        FROM sector_membership_release_sector AS component
                        INNER JOIN sector_entity AS sector
                          ON sector.sector_key = component.sector_key
                        INNER JOIN sector_membership_item AS item
                          ON item.snapshot_id = component.snapshot_id
                        INNER JOIN equity_instrument AS instrument
                          ON instrument.security_id = item.security_id
                        INNER JOIN sector_membership_interval AS membership
                          ON membership.sector_key = component.sector_key
                         AND membership.security_id = item.security_id
                         AND membership.observation_range @> component.snapshot_observed_at
                        WHERE component.release_id = :release_id
                          AND instrument.instrument_id = :instrument_id
                          AND (
                            :after_sector_code IS NULL
                            OR sector.sector_code > :after_sector_code
                          )
                        ORDER BY sector.sector_code
                        LIMIT :limit
                        """
                    ),
                    {
                        "release_id": release_id,
                        "instrument_id": instrument_id,
                        "after_sector_code": after_sector_code,
                        "limit": limit,
                    },
                )
                .mappings()
                .all()
            )
        return tuple(_stored_equity_membership(row) for row in rows)

    def _lock_sector(self, connection: Connection, identifier: SectorIdentifier) -> None:
        """为单板块快照和区间差分获取事务级互斥，阻止并发任务双重闭合。"""
        connection.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {"lock_key": f"sector-membership:{identifier.qualified_key}"},
        )

    def _lock_scheme(self, connection: Connection, scheme: SectorScheme) -> None:
        """为 scheme release reducer 获取事务级互斥，防止清单交叉切换。"""
        connection.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {"lock_key": f"sector-membership-release:{scheme.value}"},
        )

    def _update_partition(
        self,
        connection: Connection,
        *,
        run: SectorMembershipRun,
        sector: StoredSector,
        status: str,
        checkpoint: str | None,
        error_code: str | None,
    ) -> None:
        """提交分区最终状态并清除 lease，避免故障 worker 永久占有同一逻辑分区。"""
        connection.execute(
            text(
                """
                UPDATE sync_partition
                SET status = :status,
                    lease_owner = NULL,
                    lease_until = NULL,
                    heartbeat_at = :updated_at,
                    next_retry_at = NULL,
                    checkpoint_json = CAST(:checkpoint_json AS JSONB),
                    error_code = :error_code,
                    updated_at = :updated_at
                WHERE run_id = :run_id AND partition_key = :partition_key
                """
            ),
            {
                "status": status,
                "updated_at": datetime.now(UTC),
                "checkpoint_json": checkpoint,
                "error_code": error_code,
                "run_id": run.run_id,
                "partition_key": _partition_key(sector.identifier, run.observation_date),
            },
        )

    def _existing_snapshot(
        self, connection: Connection, idempotency_key: str
    ) -> Mapping[Any, Any] | None:
        """读取同一逻辑分区的既有观测，重复执行仍保留新 source batch 但不重写事实。"""
        return (
            connection.execute(
                text(
                    """
                    SELECT snapshot_id, observed_at, status, pending_count, quarantine_count
                    FROM sector_membership_snapshot
                    WHERE idempotency_key = :idempotency_key
                    """
                ),
                {"idempotency_key": idempotency_key},
            )
            .mappings()
            .one_or_none()
        )

    def _resolve_candidates(
        self,
        connection: Connection,
        *,
        candidates: Sequence[SectorMembershipCandidate],
        fact_date: date,
        known_at: datetime,
    ) -> tuple[
        list[tuple[int, SectorMembershipCandidate]],
        list[tuple[int, SectorMembershipCandidate, Exchange | None, str]],
        list[tuple[int, SectorMembershipCandidate, str]],
    ]:
        """用 0014 双时间标识解析成分；未确认与冲突必须隔离而非按名称猜测。"""
        verified: list[tuple[int, SectorMembershipCandidate]] = []
        pending: list[tuple[int, SectorMembershipCandidate, Exchange | None, str]] = []
        quarantined: list[tuple[int, SectorMembershipCandidate, str]] = []
        for ordinal, candidate in enumerate(candidates, start=1):
            exchange = _infer_exchange(candidate.source_symbol)
            if exchange is None:
                quarantined.append((ordinal, candidate, "UNSUPPORTED_EXCHANGE"))
                continue
            resolution = resolve_identity_on_connection(
                connection,
                exchange=exchange,
                symbol=candidate.source_symbol,
                fact_date=fact_date,
                known_at=known_at,
            )
            if (
                resolution.status is EquityIdentityResolutionStatus.RESOLVED
                and resolution.identity_state == "CONFIRMED"
                and resolution.security_id is not None
            ):
                verified.append((resolution.security_id, candidate))
            elif resolution.status is EquityIdentityResolutionStatus.RESOLVED:
                pending.append((ordinal, candidate, exchange, "IDENTITY_PENDING"))
            elif resolution.status is EquityIdentityResolutionStatus.NOT_FOUND:
                pending.append((ordinal, candidate, exchange, "IDENTITY_NOT_FOUND"))
            else:
                quarantined.append((ordinal, candidate, "IDENTITY_CONFLICT"))
        return verified, pending, quarantined

    def _quality_results(
        self,
        connection: Connection,
        *,
        sector_key: int,
        observation_date: date,
        verified_security_ids: set[int],
        has_unresolved: bool,
    ) -> list[tuple[str, str, str, int | None, int | None]]:
        """执行身份、数量与 churn 质量门；只有 error 会阻止该快照差分。"""
        if has_unresolved:
            return [("IDENTITY_COVERAGE", "error", "quarantine", len(verified_security_ids), None)]
        previous = self._latest_complete_snapshot(connection, sector_key)
        if previous is None:
            return []
        if previous["observation_date"] >= observation_date:
            return [("OBSERVATION_ORDER", "error", "quarantine", None, None)]
        prior_rows = (
            connection.execute(
                text(
                    "SELECT security_id FROM sector_membership_item "
                    "WHERE snapshot_id = :snapshot_id"
                ),
                {"snapshot_id": previous["snapshot_id"]},
            )
            .mappings()
            .all()
        )
        previous_ids = {int(row["security_id"]) for row in prior_rows}
        if not previous_ids:
            return [("PREVIOUS_SNAPSHOT_EMPTY", "error", "quarantine", 0, None)]
        current_count = len(verified_security_ids)
        previous_count = len(previous_ids)
        results: list[tuple[str, str, str, int | None, int | None]] = []
        if current_count * 100 < previous_count * 70 or current_count * 100 > previous_count * 150:
            results.append(("COUNT_CHANGE", "error", "quarantine", current_count, previous_count))
        churn_count = len(verified_security_ids.symmetric_difference(previous_ids))
        if churn_count * 100 > previous_count * 25:
            results.append(("CHURN", "error", "quarantine", churn_count, previous_count))
        elif churn_count * 100 > previous_count * 10:
            results.append(("CHURN", "warn", "publish", churn_count, previous_count))
        return results

    def _insert_snapshot(
        self,
        connection: Connection,
        *,
        snapshot_id: UUID,
        sector_key: int,
        source_batch_id: UUID,
        observed_at: datetime,
        observation_date: date,
        candidates: Sequence[SectorMembershipCandidate],
        verified_count: int,
        pending_count: int,
        quarantine_count: int,
        content_hash: bytes,
        idempotency_key: str,
        complete: bool,
        quality_status: str,
    ) -> None:
        """先保存全部来源观测头和质量状态，坏快照也保留可重放定位证据。"""
        connection.execute(
            text(
                """
                INSERT INTO sector_membership_snapshot (
                  snapshot_id, sector_key, source_batch_id, observed_at, observation_date,
                  status, quality_status, member_count, verified_count, pending_count,
                  quarantine_count, content_sha256, idempotency_key
                ) VALUES (
                  :snapshot_id, :sector_key, :source_batch_id, :observed_at, :observation_date,
                  :status, :quality_status, :member_count, :verified_count, :pending_count,
                  :quarantine_count, :content_sha256, :idempotency_key
                )
                """
            ),
            {
                "snapshot_id": snapshot_id,
                "sector_key": sector_key,
                "source_batch_id": source_batch_id,
                "observed_at": observed_at,
                "observation_date": observation_date,
                "status": "COMPLETE" if complete else "QUARANTINED",
                "quality_status": quality_status,
                "member_count": len(candidates),
                "verified_count": verified_count,
                "pending_count": pending_count,
                "quarantine_count": quarantine_count,
                "content_sha256": content_hash,
                "idempotency_key": idempotency_key,
            },
        )

    def _insert_pending(
        self,
        connection: Connection,
        *,
        snapshot_id: UUID,
        rows: Sequence[tuple[int, SectorMembershipCandidate, Exchange | None, str]],
        now: datetime,
    ) -> None:
        """保存未确认身份的最小标准行，禁止将其写入正式成员表。"""
        for ordinal, candidate, exchange, reason in rows:
            connection.execute(
                text(
                    """
                    INSERT INTO sector_membership_pending (
                      snapshot_id, row_ordinal, source_symbol, source_name, inferred_exchange,
                      reason_code, created_at
                    ) VALUES (
                      :snapshot_id, :row_ordinal, :source_symbol, :source_name,
                      :inferred_exchange, :reason_code, :created_at
                    )
                    """
                ),
                {
                    "snapshot_id": snapshot_id,
                    "row_ordinal": ordinal,
                    "source_symbol": candidate.source_symbol,
                    "source_name": candidate.source_name,
                    "inferred_exchange": None if exchange is None else exchange.value,
                    "reason_code": reason,
                    "created_at": now,
                },
            )

    def _insert_quarantine(
        self,
        connection: Connection,
        *,
        snapshot_id: UUID,
        rows: Sequence[tuple[int, SectorMembershipCandidate, str]],
        now: datetime,
    ) -> None:
        """保存冲突或无法推断交易所的标准行，供 raw 重放和人工处置定位。"""
        for ordinal, candidate, reason in rows:
            connection.execute(
                text(
                    """
                    INSERT INTO sector_membership_quarantine (
                      snapshot_id, row_ordinal, source_symbol, source_name, reason_code, created_at
                    ) VALUES (
                      :snapshot_id, :row_ordinal, :source_symbol, :source_name,
                      :reason_code, :created_at
                    )
                    """
                ),
                {
                    "snapshot_id": snapshot_id,
                    "row_ordinal": ordinal,
                    "source_symbol": candidate.source_symbol,
                    "source_name": candidate.source_name,
                    "reason_code": reason,
                    "created_at": now,
                },
            )

    def _insert_quality_results(
        self,
        connection: Connection,
        *,
        snapshot_id: UUID,
        results: Sequence[tuple[str, str, str, int | None, int | None]],
        now: datetime,
    ) -> None:
        """持久化不含 raw 或敏感字段的质量判定，供恢复和告警查询。"""
        for rule_code, severity, disposition, actual_value, expected_value in results:
            connection.execute(
                text(
                    """
                    INSERT INTO sector_membership_quality_result (
                      snapshot_id, rule_code, severity, disposition, actual_value, expected_value,
                      created_at
                    ) VALUES (
                      :snapshot_id, :rule_code, :severity, :disposition, :actual_value,
                      :expected_value, :created_at
                    )
                    """
                ),
                {
                    "snapshot_id": snapshot_id,
                    "rule_code": rule_code,
                    "severity": severity,
                    "disposition": disposition,
                    "actual_value": actual_value,
                    "expected_value": expected_value,
                    "created_at": now,
                },
            )

    def _ensure_item_partition(self, connection: Connection, snapshot_date: date) -> None:
        """按观测月份创建正式成员分区和反向读取索引，避免无界单表增长。"""
        month_start = snapshot_date.replace(day=1)
        next_month = (
            date(month_start.year + 1, 1, 1)
            if month_start.month == 12
            else date(month_start.year, month_start.month + 1, 1)
        )
        suffix = month_start.strftime("%Y%m")
        table_name = f"sector_membership_item_{suffix}"
        connection.execute(
            text(
                f"""
                CREATE TABLE IF NOT EXISTS {table_name}
                PARTITION OF sector_membership_item
                FOR VALUES FROM ('{month_start.isoformat()}') TO ('{next_month.isoformat()}')
                """
            )
        )
        connection.execute(
            text(
                f"""
                CREATE INDEX IF NOT EXISTS ix_{table_name}_reverse
                ON {table_name} (security_id, snapshot_id)
                """
            )
        )

    def _insert_items(
        self,
        connection: Connection,
        *,
        snapshot_id: UUID,
        snapshot_date: date,
        verified: Sequence[tuple[int, SectorMembershipCandidate]],
    ) -> None:
        """仅把唯一 CONFIRMED 身份写入正式分区，保留来源代码名称与行级哈希。"""
        for security_id, candidate in verified:
            connection.execute(
                text(
                    """
                    INSERT INTO sector_membership_item (
                      snapshot_date, snapshot_id, security_id, source_symbol, source_name,
                      content_sha256
                    ) VALUES (
                      :snapshot_date, :snapshot_id, :security_id, :source_symbol, :source_name,
                      :content_sha256
                    )
                    """
                ),
                {
                    "snapshot_date": snapshot_date,
                    "snapshot_id": snapshot_id,
                    "security_id": security_id,
                    "source_symbol": candidate.source_symbol,
                    "source_name": candidate.source_name,
                    "content_sha256": _candidate_hash((candidate,)),
                },
            )

    def _advance_intervals(
        self,
        connection: Connection,
        *,
        sector_key: int,
        snapshot_id: UUID,
        observed_at: datetime,
        security_ids: set[int],
    ) -> tuple[int, int]:
        """在完整快照下关闭明确缺席关系并打开新增关系，所有边界均使用本次观测时刻。"""
        rows = (
            connection.execute(
                text(
                    """
                    SELECT security_id
                    FROM sector_membership_interval
                    WHERE sector_key = :sector_key AND observed_to IS NULL
                    FOR UPDATE
                    """
                ),
                {"sector_key": sector_key},
            )
            .mappings()
            .all()
        )
        open_ids = {int(row["security_id"]) for row in rows}
        for security_id in open_ids - security_ids:
            connection.execute(
                text(
                    """
                    UPDATE sector_membership_interval
                    SET observed_to = :observed_to, close_snapshot_id = :close_snapshot_id
                    WHERE sector_key = :sector_key
                      AND security_id = :security_id
                      AND observed_to IS NULL
                    """
                ),
                {
                    "observed_to": observed_at,
                    "close_snapshot_id": snapshot_id,
                    "sector_key": sector_key,
                    "security_id": security_id,
                },
            )
        for security_id in security_ids - open_ids:
            connection.execute(
                text(
                    """
                    INSERT INTO sector_membership_interval (
                      sector_key, security_id, observed_from, observed_to, open_snapshot_id,
                      close_snapshot_id
                    ) VALUES (
                      :sector_key, :security_id, :observed_from, NULL, :open_snapshot_id, NULL
                    )
                    """
                ),
                {
                    "sector_key": sector_key,
                    "security_id": security_id,
                    "observed_from": observed_at,
                    "open_snapshot_id": snapshot_id,
                },
            )
        return len(security_ids - open_ids), len(open_ids - security_ids)

    def _active_sectors_on_connection(
        self, connection: Connection, scheme: SectorScheme
    ) -> tuple[StoredSector, ...]:
        """在 release 事务内重读 ACTIVE 集合，避免目录变更和清单切换交错。"""
        rows = (
            connection.execute(
                text(
                    """
                    SELECT sector_key, sector_id, scheme, sector_code, name, status
                    FROM sector_entity
                    WHERE scheme = :scheme AND status = 'ACTIVE'
                    ORDER BY sector_code, sector_id
                    FOR SHARE
                    """
                ),
                {"scheme": scheme.value},
            )
            .mappings()
            .all()
        )
        return tuple(_stored_sector(row) for row in rows)

    def _latest_complete_snapshot(
        self, connection: Connection, sector_key: int
    ) -> Mapping[Any, Any] | None:
        """读取一板块最后完整快照；隔离或失败观测绝不覆盖此基线。"""
        return (
            connection.execute(
                text(
                    """
                    SELECT snapshot_id, observed_at, observation_date, member_count, quality_status
                    FROM sector_membership_snapshot
                    WHERE sector_key = :sector_key AND status = 'COMPLETE'
                    ORDER BY observed_at DESC
                    LIMIT 1
                    """
                ),
                {"sector_key": sector_key},
            )
            .mappings()
            .one_or_none()
        )

    def _current_release(
        self, connection: Connection, scheme: SectorScheme
    ) -> Mapping[Any, Any] | None:
        """读取 scheme 当前 release，供 manifest 比较和原子 supersede 使用。"""
        return (
            connection.execute(
                text(
                    """
                    SELECT release_id, data_version, quality_status, fresh_sector_count,
                           carried_forward_sector_count, published_at
                    FROM sector_membership_release
                    WHERE scheme = :scheme AND superseded_at IS NULL
                    FOR UPDATE
                    """
                ),
                {"scheme": scheme.value},
            )
            .mappings()
            .one_or_none()
        )

    def _release_matches(
        self,
        connection: Connection,
        release_id: UUID,
        snapshot_ids: tuple[UUID, ...],
        quality_status: str,
    ) -> bool:
        """比较固定 manifest，内容未变时复用现有稳定 dataVersion。"""
        rows = (
            connection.execute(
                text(
                    """
                    SELECT snapshot_id
                    FROM sector_membership_release_sector
                    WHERE release_id = :release_id
                    ORDER BY snapshot_id
                    """
                ),
                {"release_id": release_id},
            )
            .mappings()
            .all()
        )
        existing_ids = tuple(UUID(str(row["snapshot_id"])) for row in rows)
        return existing_ids == tuple(
            sorted(snapshot_ids)
        ) and quality_status == self._release_quality(connection, release_id)

    def _release_quality(self, connection: Connection, release_id: UUID) -> str:
        """读取已比较 release 的质量标签，防止 carry-forward 变化却复用旧版本。"""
        return str(
            connection.execute(
                text(
                    "SELECT quality_status FROM sector_membership_release "
                    "WHERE release_id = :release_id"
                ),
                {"release_id": release_id},
            )
            .mappings()
            .one()["quality_status"]
        )

    def _publish_dataset(
        self,
        connection: Connection,
        *,
        scheme: SectorScheme,
        data_version: UUID,
        quality_status: str,
        effective_as_of: date,
        published_at: datetime,
    ) -> None:
        """同步维护通用数据集发布指针，供跨服务健康和版本追踪统一读取。"""
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
            {"dataset": _DATASET, "partition_key": scheme.value, "published_at": published_at},
        )
        connection.execute(
            text(
                """
                INSERT INTO dataset_publication (
                  publication_id, dataset, partition_key, data_version, quality_status,
                  effective_as_of, knowledge_cutoff, published_at, superseded_at
                ) VALUES (
                  :publication_id, :dataset, :partition_key, :data_version, :quality_status,
                  :effective_as_of, :knowledge_cutoff, :published_at, NULL
                )
                """
            ),
            {
                "publication_id": uuid4(),
                "dataset": _DATASET,
                "partition_key": scheme.value,
                "data_version": data_version,
                "quality_status": quality_status,
                "effective_as_of": effective_as_of,
                "knowledge_cutoff": published_at,
                "published_at": published_at,
            },
        )


def _stored_sector(row: Mapping[Any, Any]) -> StoredSector:
    """将数据库板块锚投影为不含 SQL 类型的应用端口值。"""
    return StoredSector(
        sector_key=int(row["sector_key"]),
        sector_id=UUID(str(row["sector_id"])),
        identifier=SectorIdentifier(
            scheme=SectorScheme(str(row["scheme"])), code=str(row["sector_code"])
        ),
        name=None if row["name"] is None else str(row["name"]),
        status=str(row["status"]),
    )


def _stored_release(
    row: Mapping[Any, Any], *, requested_as_of: datetime | None
) -> StoredSectorMembershipRelease:
    """将固定 release 头投影为 API 可复验的版本上下文。"""
    return StoredSectorMembershipRelease(
        release_id=UUID(str(row["release_id"])),
        scheme=SectorScheme(str(row["scheme"])),
        requested_as_of=requested_as_of,
        resolved_as_of=row["release_as_of"],
        coverage_start=row["coverage_start"],
        data_version=UUID(str(row["data_version"])),
        quality_status=str(row["quality_status"]),
        carried_forward_sector_count=int(row["carried_forward_sector_count"]),
        published_at=row["published_at"],
    )


def _stored_constituent(row: Mapping[Any, Any]) -> StoredMembershipConstituent:
    """投影一条 verified 成分，不向调用方暴露 security_id 或 source batch。"""
    return StoredMembershipConstituent(
        instrument_id=UUID(str(row["instrument_id"])),
        exchange=Exchange(str(row["exchange"])),
        symbol=str(row["symbol"]),
        name=str(row["name"]),
        listing_status=str(row["status"]),
        observed_from=row["observed_from"],
        observed_to=row["observed_to"],
    )


def _stored_membership_equity(row: Mapping[Any, Any]) -> StoredMembershipEquity:
    """投影 release 下可反向查询的唯一证券，不使用当前身份锚作为真相。"""
    return StoredMembershipEquity(
        instrument_id=UUID(str(row["instrument_id"])),
        exchange=Exchange(str(row["exchange"])),
        symbol=str(row["symbol"]),
        name=str(row["name"]),
        listing_status=str(row["status"]),
    )


def _stored_equity_membership(row: Mapping[Any, Any]) -> StoredEquityMembership:
    """投影 release manifest 中的一条反向板块归属及其快照元数据。"""
    return StoredEquityMembership(
        sector=_stored_sector(row),
        observed_from=row["observed_from"],
        observed_to=row["observed_to"],
        snapshot_observed_at=row["snapshot_observed_at"],
        carried_forward=bool(row["carried_forward"]),
    )


def _infer_exchange(symbol: str) -> Exchange | None:
    """按已定义 A 股代码段推断交易所；无法证明时返回空并进入隔离。"""
    if symbol.startswith(("60", "68")):
        return Exchange.SSE
    if symbol.startswith(("00", "30")):
        return Exchange.SZSE
    if symbol.startswith(("4", "8", "92")):
        return Exchange.BSE
    return None


def _candidate_hash(candidates: Sequence[SectorMembershipCandidate]) -> bytes:
    """以排序后的标准候选构造稳定业务哈希，忽略来源行顺序和观测元数据。"""
    serialized = json.dumps(
        [
            {"sourceSymbol": candidate.source_symbol, "sourceName": candidate.source_name}
            for candidate in sorted(candidates, key=lambda item: item.source_symbol)
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(serialized).digest()


def _idempotency_key(identifier: SectorIdentifier, observation_date: date) -> str:
    """生成同板块、日期和 schema 的稳定请求键，重复命令只复用既有逻辑快照。"""
    return hashlib.sha256(
        f"{_CAPABILITY}|{identifier.qualified_key}|{observation_date.isoformat()}|v1".encode()
    ).hexdigest()


def _run_request_key(scheme: SectorScheme, observation_date: date) -> str:
    """生成 scheme 级可恢复运行键，同日重跑复用 run 但不复用来源证据。"""
    return f"sector-membership:{scheme.value}:{observation_date.isoformat()}:v1"


def _partition_key(identifier: SectorIdentifier, observation_date: date) -> str:
    """生成单板块市场日分区键，供 lease、checkpoint 和 source batch 关联。"""
    return f"{identifier.qualified_key}:{observation_date.isoformat()}"
