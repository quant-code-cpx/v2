"""使用 PostgreSQL 保存板块成分观测快照、半开区间和固定 release 清单。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import and_, func, insert, literal, or_, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.orm import Session

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

from ..database.connection import DatabaseClient
from ..database.models.equity.identity.equity_identifier_version import (
    EquityIdentifierVersion,
)
from ..database.models.equity.identity.equity_instrument import (
    EquityInstrument,
)
from ..database.models.equity.identity.equity_listing_status_version import (
    EquityListingStatusVersion,
)
from ..database.models.equity.identity.equity_name_version import (
    EquityNameVersion,
)
from ..database.models.execution.sync_partition import SyncPartition
from ..database.models.execution.sync_run import SyncRun
from ..database.models.publication.dataset_publication import (
    DatasetPublication,
)
from ..database.models.sector.catalog.sector_entity import (
    SectorEntity,
)
from ..database.models.sector.membership.sector_membership_interval import (
    SectorMembershipInterval,
)
from ..database.models.sector.membership.sector_membership_item import (
    SectorMembershipItem,
)
from ..database.models.sector.membership.sector_membership_pending import (
    SectorMembershipPending,
)
from ..database.models.sector.membership.sector_membership_quality_result import (
    SectorMembershipQualityResult,
)
from ..database.models.sector.membership.sector_membership_quarantine import (
    SectorMembershipQuarantine,
)
from ..database.models.sector.membership.sector_membership_release import (
    SectorMembershipRelease,
)
from ..database.models.sector.membership.sector_membership_release_sector import (
    SectorMembershipReleaseSector,
)
from ..database.models.sector.membership.sector_membership_snapshot import (
    SectorMembershipSnapshot,
)
from ..database.partition_manager import (
    ensure_sector_membership_item_partition,
)
from .equity_identity_resolver import (
    resolve_identity_on_connection,
)
from .source_batch import record_source_observation

_CAPABILITY = "sector.membership.snapshot.raw"
_DATASET = "sector.membership.release"
_LEASE_DURATION = timedelta(minutes=20)


class SqlAlchemySectorMembershipRepository(SectorMembershipRepository):
    """独占板块成分 canonical 存储；所有区间仅表达来源完整快照的观测边界。"""

    def __init__(self, database: DatabaseClient) -> None:
        """保存服务私有数据库会话工厂，调用方不接触 SQLAlchemy 或数据表。"""
        self._database = database

    def list_active_sectors(self, *, scheme: SectorScheme) -> Sequence[StoredSector]:
        """读取一个分类体系当前 ACTIVE 板块，供一次 scheme run 冻结分区集合。"""
        with self._database.session() as connection:
            rows = (
                connection.execute(
                    select(
                        SectorEntity.sector_key,
                        SectorEntity.sector_id,
                        SectorEntity.scheme,
                        SectorEntity.sector_code,
                        SectorEntity.name,
                        SectorEntity.status,
                    )
                    .where(SectorEntity.scheme == scheme.value, SectorEntity.status == "ACTIVE")
                    .order_by(SectorEntity.sector_code, SectorEntity.sector_id)
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
        with self._database.transaction() as connection:
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
        with self._database.transaction() as connection:
            self._lock_scheme(connection, scheme)
            requested_partitions = [
                _partition_key(sector.identifier, observation_date) for sector in sectors
            ]
            existing_run = select(SyncRun.run_id).where(SyncRun.request_key == request_key)
            leased_partitions = (
                connection.execute(
                    select(SyncPartition.partition_key)
                    .where(
                        SyncPartition.run_id == existing_run.scalar_subquery(),
                        SyncPartition.partition_key.in_(requested_partitions),
                        SyncPartition.lease_until > now,
                    )
                    .with_for_update()
                )
                .mappings()
                .all()
            )
            if leased_partitions:
                # 活跃租约代表另一个 worker 正在处理同一冻结集合。
                # 不能把该 worker 的 checkpoint 重置为新尝试。
                raise RuntimeError("sector membership run is already leased")
            insert_run = postgresql_insert(SyncRun).values(
                run_id=uuid4(),
                capability=_CAPABILITY,
                mode="manual",
                request_key=request_key,
                target_date=observation_date,
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
            for sector in sectors:
                insert_partition = postgresql_insert(SyncPartition).values(
                    run_id=run_id,
                    partition_key=_partition_key(sector.identifier, observation_date),
                    status="running",
                    attempt=1,
                    lease_owner=f"sector-membership:{run_id}",
                    lease_until=now + _LEASE_DURATION,
                    heartbeat_at=now,
                    next_retry_at=None,
                    checkpoint_json=None,
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
                            "error_code": None,
                            "updated_at": insert_partition.excluded.updated_at,
                        },
                    )
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
        with self._database.transaction() as connection:
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
        with self._database.transaction() as connection:
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
        with self._database.transaction() as connection:
            connection.execute(
                update(SyncRun)
                .where(SyncRun.run_id == run.run_id)
                .values(status=status, finished_at=datetime.now(UTC))
            )

    def publish_release(
        self, *, scheme: SectorScheme, observation_date: date
    ) -> PublishedSectorMembershipRelease | None:
        """汇总冻结的 ACTIVE 板块快照，满足质量覆盖门才原子切换 scheme release。"""
        now = datetime.now(UTC)
        with self._database.transaction() as connection:
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
                    update(SectorMembershipRelease)
                    .where(SectorMembershipRelease.release_id == current["release_id"])
                    .values(superseded_at=now)
                )
            release_id = uuid4()
            data_version = uuid4()
            release_as_of = max(snapshot["observed_at"] for _, snapshot, _ in components)
            coverage_start = min(snapshot["observed_at"] for _, snapshot, _ in components)
            connection.execute(
                insert(SectorMembershipRelease).values(
                    release_id=release_id,
                    scheme=scheme.value,
                    release_as_of=release_as_of,
                    coverage_start=coverage_start,
                    data_version=data_version,
                    quality_status=quality_status,
                    expected_sector_count=expected_count,
                    fresh_sector_count=fresh_count,
                    carried_forward_sector_count=carried_count,
                    identity_coverage_percent=100,
                    excluded_identity_count=0,
                    published_at=now,
                    superseded_at=None,
                )
            )
            for sector, snapshot, carried_forward in components:
                connection.execute(
                    insert(SectorMembershipReleaseSector).values(
                        release_id=release_id,
                        sector_key=sector.sector_key,
                        snapshot_id=snapshot["snapshot_id"],
                        carried_forward=carried_forward,
                        snapshot_observed_at=snapshot["observed_at"],
                    )
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
        statement = select(
            SectorMembershipRelease.release_id,
            SectorMembershipRelease.scheme,
            SectorMembershipRelease.release_as_of,
            SectorMembershipRelease.coverage_start,
            SectorMembershipRelease.data_version,
            SectorMembershipRelease.quality_status,
            SectorMembershipRelease.carried_forward_sector_count,
            SectorMembershipRelease.published_at,
        ).where(SectorMembershipRelease.scheme == scheme.value)
        if as_of is None:
            statement = statement.where(SectorMembershipRelease.superseded_at.is_(None))
        else:
            statement = (
                statement.where(SectorMembershipRelease.release_as_of <= as_of)
                .order_by(SectorMembershipRelease.release_as_of.desc())
                .limit(1)
            )
        with self._database.session() as connection:
            row = connection.execute(statement).mappings().one_or_none()
        return None if row is None else _stored_release(row, requested_as_of=as_of)

    def get_release_sector(
        self, *, release_id: UUID, identifier: SectorIdentifier
    ) -> tuple[StoredSector, datetime, bool] | None:
        """读取 release 固定板块快照，不能回退到随后变更的当前快照。"""
        with self._database.session() as connection:
            row = (
                connection.execute(
                    select(
                        SectorEntity.sector_key,
                        SectorEntity.sector_id,
                        SectorEntity.scheme,
                        SectorEntity.sector_code,
                        SectorEntity.name,
                        SectorEntity.status,
                        SectorMembershipReleaseSector.snapshot_observed_at,
                        SectorMembershipReleaseSector.carried_forward,
                    )
                    .join(
                        SectorEntity,
                        SectorEntity.sector_key == SectorMembershipReleaseSector.sector_key,
                    )
                    .where(
                        SectorMembershipReleaseSector.release_id == release_id,
                        SectorEntity.scheme == identifier.scheme.value,
                        SectorEntity.sector_code == identifier.code,
                    )
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
        statement = (
            select(
                EquityInstrument.instrument_id,
                EquityIdentifierVersion.exchange,
                EquityIdentifierVersion.symbol,
                EquityNameVersion.name,
                EquityListingStatusVersion.status,
                SectorMembershipInterval.observed_from,
                SectorMembershipInterval.observed_to,
            )
            .select_from(SectorMembershipReleaseSector)
            .join(SectorEntity, SectorEntity.sector_key == SectorMembershipReleaseSector.sector_key)
            .join(
                SectorMembershipItem,
                SectorMembershipItem.snapshot_id == SectorMembershipReleaseSector.snapshot_id,
            )
            .join(
                SectorMembershipInterval,
                and_(
                    SectorMembershipInterval.sector_key == SectorMembershipReleaseSector.sector_key,
                    SectorMembershipInterval.security_id == SectorMembershipItem.security_id,
                    SectorMembershipInterval.observation_range.contains(
                        SectorMembershipReleaseSector.snapshot_observed_at
                    ),
                ),
            )
            .join(
                SectorMembershipRelease,
                SectorMembershipRelease.release_id == SectorMembershipReleaseSector.release_id,
            )
            .join(
                EquityInstrument, EquityInstrument.security_id == SectorMembershipItem.security_id
            )
            .join(
                EquityIdentifierVersion,
                and_(
                    EquityIdentifierVersion.security_id == SectorMembershipItem.security_id,
                    EquityIdentifierVersion.identity_state == "CONFIRMED",
                    EquityIdentifierVersion.effective_range.contains(
                        func.date(SectorMembershipReleaseSector.snapshot_observed_at)
                    ),
                    EquityIdentifierVersion.knowledge_range.contains(
                        SectorMembershipRelease.published_at
                    ),
                ),
            )
            .join(
                EquityNameVersion,
                and_(
                    EquityNameVersion.security_id == SectorMembershipItem.security_id,
                    EquityNameVersion.effective_range.contains(
                        func.date(SectorMembershipReleaseSector.snapshot_observed_at)
                    ),
                    EquityNameVersion.knowledge_range.contains(
                        SectorMembershipRelease.published_at
                    ),
                ),
            )
            .join(
                EquityListingStatusVersion,
                and_(
                    EquityListingStatusVersion.security_id == SectorMembershipItem.security_id,
                    EquityListingStatusVersion.effective_range.contains(
                        func.date(SectorMembershipReleaseSector.snapshot_observed_at)
                    ),
                    EquityListingStatusVersion.knowledge_range.contains(
                        SectorMembershipRelease.published_at
                    ),
                ),
            )
            .where(
                SectorMembershipReleaseSector.release_id == release_id,
                SectorEntity.scheme == identifier.scheme.value,
                SectorEntity.sector_code == identifier.code,
            )
        )
        if after_exchange is not None:
            assert after_symbol is not None
            statement = statement.where(
                or_(
                    EquityIdentifierVersion.exchange > after_exchange.value,
                    and_(
                        EquityIdentifierVersion.exchange == after_exchange.value,
                        EquityIdentifierVersion.symbol > after_symbol,
                    ),
                )
            )
        with self._database.session() as connection:
            rows = (
                connection.execute(
                    statement.order_by(
                        EquityIdentifierVersion.exchange, EquityIdentifierVersion.symbol
                    ).limit(limit)
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
        with self._database.session() as connection:
            row = (
                connection.execute(
                    select(
                        EquityInstrument.instrument_id,
                        EquityIdentifierVersion.exchange,
                        EquityIdentifierVersion.symbol,
                        EquityNameVersion.name,
                        EquityListingStatusVersion.status,
                    )
                    .select_from(SectorMembershipRelease)
                    .join(
                        EquityIdentifierVersion,
                        and_(
                            EquityIdentifierVersion.exchange == exchange.value,
                            EquityIdentifierVersion.symbol == symbol,
                            EquityIdentifierVersion.identity_state == "CONFIRMED",
                            EquityIdentifierVersion.effective_range.contains(
                                func.date(SectorMembershipRelease.release_as_of)
                            ),
                            EquityIdentifierVersion.knowledge_range.contains(
                                SectorMembershipRelease.published_at
                            ),
                        ),
                    )
                    .join(
                        EquityInstrument,
                        EquityInstrument.security_id == EquityIdentifierVersion.security_id,
                    )
                    .join(
                        EquityNameVersion,
                        and_(
                            EquityNameVersion.security_id == EquityIdentifierVersion.security_id,
                            EquityNameVersion.effective_range.contains(
                                func.date(SectorMembershipRelease.release_as_of)
                            ),
                            EquityNameVersion.knowledge_range.contains(
                                SectorMembershipRelease.published_at
                            ),
                        ),
                    )
                    .join(
                        EquityListingStatusVersion,
                        and_(
                            EquityListingStatusVersion.security_id
                            == EquityIdentifierVersion.security_id,
                            EquityListingStatusVersion.effective_range.contains(
                                func.date(SectorMembershipRelease.release_as_of)
                            ),
                            EquityListingStatusVersion.knowledge_range.contains(
                                SectorMembershipRelease.published_at
                            ),
                        ),
                    )
                    .where(SectorMembershipRelease.release_id == release_id)
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
        statement = (
            select(
                SectorEntity.sector_key,
                SectorEntity.sector_id,
                SectorEntity.scheme,
                SectorEntity.sector_code,
                SectorEntity.name,
                SectorEntity.status,
                SectorMembershipInterval.observed_from,
                SectorMembershipInterval.observed_to,
                SectorMembershipReleaseSector.snapshot_observed_at,
                SectorMembershipReleaseSector.carried_forward,
            )
            .select_from(SectorMembershipReleaseSector)
            .join(SectorEntity, SectorEntity.sector_key == SectorMembershipReleaseSector.sector_key)
            .join(
                SectorMembershipItem,
                SectorMembershipItem.snapshot_id == SectorMembershipReleaseSector.snapshot_id,
            )
            .join(
                EquityInstrument, EquityInstrument.security_id == SectorMembershipItem.security_id
            )
            .join(
                SectorMembershipInterval,
                and_(
                    SectorMembershipInterval.sector_key == SectorMembershipReleaseSector.sector_key,
                    SectorMembershipInterval.security_id == SectorMembershipItem.security_id,
                    SectorMembershipInterval.observation_range.contains(
                        SectorMembershipReleaseSector.snapshot_observed_at
                    ),
                ),
            )
            .where(
                SectorMembershipReleaseSector.release_id == release_id,
                EquityInstrument.instrument_id == instrument_id,
            )
        )
        if after_sector_code is not None:
            statement = statement.where(SectorEntity.sector_code > after_sector_code)
        with self._database.session() as connection:
            rows = (
                connection.execute(statement.order_by(SectorEntity.sector_code).limit(limit))
                .mappings()
                .all()
            )
        return tuple(_stored_equity_membership(row) for row in rows)

    def _lock_sector(self, connection: Session, identifier: SectorIdentifier) -> None:
        """为单板块快照和区间差分获取事务级互斥，阻止并发任务双重闭合。"""
        connection.execute(
            select(
                func.pg_advisory_xact_lock(
                    func.hashtextextended(
                        literal(f"sector-membership:{identifier.qualified_key}"), 0
                    )
                )
            )
        )

    def _lock_scheme(self, connection: Session, scheme: SectorScheme) -> None:
        """为 scheme release reducer 获取事务级互斥，防止清单交叉切换。"""
        connection.execute(
            select(
                func.pg_advisory_xact_lock(
                    func.hashtextextended(literal(f"sector-membership-release:{scheme.value}"), 0)
                )
            )
        )

    def _update_partition(
        self,
        connection: Session,
        *,
        run: SectorMembershipRun,
        sector: StoredSector,
        status: str,
        checkpoint: str | None,
        error_code: str | None,
    ) -> None:
        """提交分区最终状态并清除 lease，避免故障 worker 永久占有同一逻辑分区。"""
        updated_at = datetime.now(UTC)
        connection.execute(
            update(SyncPartition)
            .where(
                SyncPartition.run_id == run.run_id,
                SyncPartition.partition_key
                == _partition_key(sector.identifier, run.observation_date),
            )
            .values(
                status=status,
                lease_owner=None,
                lease_until=None,
                heartbeat_at=updated_at,
                next_retry_at=None,
                checkpoint_json=None if checkpoint is None else json.loads(checkpoint),
                error_code=error_code,
                updated_at=updated_at,
            )
        )

    def _existing_snapshot(
        self, connection: Session, idempotency_key: str
    ) -> Mapping[Any, Any] | None:
        """读取同一逻辑分区的既有观测，重复执行仍保留新 source batch 但不重写事实。"""
        return (
            connection.execute(
                select(
                    SectorMembershipSnapshot.snapshot_id,
                    SectorMembershipSnapshot.observed_at,
                    SectorMembershipSnapshot.status,
                    SectorMembershipSnapshot.pending_count,
                    SectorMembershipSnapshot.quarantine_count,
                ).where(SectorMembershipSnapshot.idempotency_key == idempotency_key)
            )
            .mappings()
            .one_or_none()
        )

    def _resolve_candidates(
        self,
        connection: Session,
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
        connection: Session,
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
                select(SectorMembershipItem.security_id).where(
                    SectorMembershipItem.snapshot_id == previous["snapshot_id"]
                )
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
        connection: Session,
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
            insert(SectorMembershipSnapshot).values(
                snapshot_id=snapshot_id,
                sector_key=sector_key,
                source_batch_id=source_batch_id,
                observed_at=observed_at,
                observation_date=observation_date,
                status="COMPLETE" if complete else "QUARANTINED",
                quality_status=quality_status,
                member_count=len(candidates),
                verified_count=verified_count,
                pending_count=pending_count,
                quarantine_count=quarantine_count,
                content_sha256=content_hash,
                idempotency_key=idempotency_key,
            )
        )

    def _insert_pending(
        self,
        connection: Session,
        *,
        snapshot_id: UUID,
        rows: Sequence[tuple[int, SectorMembershipCandidate, Exchange | None, str]],
        now: datetime,
    ) -> None:
        """保存未确认身份的最小标准行，禁止将其写入正式成员表。"""
        for ordinal, candidate, exchange, reason in rows:
            connection.execute(
                insert(SectorMembershipPending).values(
                    snapshot_id=snapshot_id,
                    row_ordinal=ordinal,
                    source_symbol=candidate.source_symbol,
                    source_name=candidate.source_name,
                    inferred_exchange=None if exchange is None else exchange.value,
                    reason_code=reason,
                    created_at=now,
                )
            )

    def _insert_quarantine(
        self,
        connection: Session,
        *,
        snapshot_id: UUID,
        rows: Sequence[tuple[int, SectorMembershipCandidate, str]],
        now: datetime,
    ) -> None:
        """保存冲突或无法推断交易所的标准行，供 raw 重放和人工处置定位。"""
        for ordinal, candidate, reason in rows:
            connection.execute(
                insert(SectorMembershipQuarantine).values(
                    snapshot_id=snapshot_id,
                    row_ordinal=ordinal,
                    source_symbol=candidate.source_symbol,
                    source_name=candidate.source_name,
                    reason_code=reason,
                    created_at=now,
                )
            )

    def _insert_quality_results(
        self,
        connection: Session,
        *,
        snapshot_id: UUID,
        results: Sequence[tuple[str, str, str, int | None, int | None]],
        now: datetime,
    ) -> None:
        """持久化不含 raw 或敏感字段的质量判定，供恢复和告警查询。"""
        for rule_code, severity, disposition, actual_value, expected_value in results:
            connection.execute(
                insert(SectorMembershipQualityResult).values(
                    snapshot_id=snapshot_id,
                    rule_code=rule_code,
                    severity=severity,
                    disposition=disposition,
                    actual_value=actual_value,
                    expected_value=expected_value,
                    created_at=now,
                )
            )

    def _ensure_item_partition(self, connection: Session, snapshot_date: date) -> None:
        """按观测月份创建正式成员分区和反向读取索引，避免无界单表增长。"""
        ensure_sector_membership_item_partition(connection, snapshot_date)

    def _insert_items(
        self,
        connection: Session,
        *,
        snapshot_id: UUID,
        snapshot_date: date,
        verified: Sequence[tuple[int, SectorMembershipCandidate]],
    ) -> None:
        """仅把唯一 CONFIRMED 身份写入正式分区，保留来源代码名称与行级哈希。"""
        for security_id, candidate in verified:
            connection.execute(
                insert(SectorMembershipItem).values(
                    snapshot_date=snapshot_date,
                    snapshot_id=snapshot_id,
                    security_id=security_id,
                    source_symbol=candidate.source_symbol,
                    source_name=candidate.source_name,
                    content_sha256=_candidate_hash((candidate,)),
                )
            )

    def _advance_intervals(
        self,
        connection: Session,
        *,
        sector_key: int,
        snapshot_id: UUID,
        observed_at: datetime,
        security_ids: set[int],
    ) -> tuple[int, int]:
        """在完整快照下关闭明确缺席关系并打开新增关系，所有边界均使用本次观测时刻。"""
        rows = (
            connection.execute(
                select(SectorMembershipInterval.security_id)
                .where(
                    SectorMembershipInterval.sector_key == sector_key,
                    SectorMembershipInterval.observed_to.is_(None),
                )
                .with_for_update()
            )
            .mappings()
            .all()
        )
        open_ids = {int(row["security_id"]) for row in rows}
        for security_id in open_ids - security_ids:
            connection.execute(
                update(SectorMembershipInterval)
                .where(
                    SectorMembershipInterval.sector_key == sector_key,
                    SectorMembershipInterval.security_id == security_id,
                    SectorMembershipInterval.observed_to.is_(None),
                )
                .values(observed_to=observed_at, close_snapshot_id=snapshot_id)
            )
        for security_id in security_ids - open_ids:
            connection.execute(
                insert(SectorMembershipInterval).values(
                    sector_key=sector_key,
                    security_id=security_id,
                    observed_from=observed_at,
                    observed_to=None,
                    open_snapshot_id=snapshot_id,
                    close_snapshot_id=None,
                )
            )
        return len(security_ids - open_ids), len(open_ids - security_ids)

    def _active_sectors_on_connection(
        self, connection: Session, scheme: SectorScheme
    ) -> tuple[StoredSector, ...]:
        """在 release 事务内重读 ACTIVE 集合，避免目录变更和清单切换交错。"""
        rows = (
            connection.execute(
                select(
                    SectorEntity.sector_key,
                    SectorEntity.sector_id,
                    SectorEntity.scheme,
                    SectorEntity.sector_code,
                    SectorEntity.name,
                    SectorEntity.status,
                )
                .where(SectorEntity.scheme == scheme.value, SectorEntity.status == "ACTIVE")
                .order_by(SectorEntity.sector_code, SectorEntity.sector_id)
                .with_for_update(read=True)
            )
            .mappings()
            .all()
        )
        return tuple(_stored_sector(row) for row in rows)

    def _latest_complete_snapshot(
        self, connection: Session, sector_key: int
    ) -> Mapping[Any, Any] | None:
        """读取一板块最后完整快照；隔离或失败观测绝不覆盖此基线。"""
        return (
            connection.execute(
                select(
                    SectorMembershipSnapshot.snapshot_id,
                    SectorMembershipSnapshot.observed_at,
                    SectorMembershipSnapshot.observation_date,
                    SectorMembershipSnapshot.member_count,
                    SectorMembershipSnapshot.quality_status,
                )
                .where(
                    SectorMembershipSnapshot.sector_key == sector_key,
                    SectorMembershipSnapshot.status == "COMPLETE",
                )
                .order_by(SectorMembershipSnapshot.observed_at.desc())
                .limit(1)
            )
            .mappings()
            .one_or_none()
        )

    def _current_release(
        self, connection: Session, scheme: SectorScheme
    ) -> Mapping[Any, Any] | None:
        """读取 scheme 当前 release，供 manifest 比较和原子 supersede 使用。"""
        return (
            connection.execute(
                select(
                    SectorMembershipRelease.release_id,
                    SectorMembershipRelease.data_version,
                    SectorMembershipRelease.quality_status,
                    SectorMembershipRelease.fresh_sector_count,
                    SectorMembershipRelease.carried_forward_sector_count,
                    SectorMembershipRelease.published_at,
                )
                .where(
                    SectorMembershipRelease.scheme == scheme.value,
                    SectorMembershipRelease.superseded_at.is_(None),
                )
                .with_for_update()
            )
            .mappings()
            .one_or_none()
        )

    def _release_matches(
        self,
        connection: Session,
        release_id: UUID,
        snapshot_ids: tuple[UUID, ...],
        quality_status: str,
    ) -> bool:
        """比较固定 manifest，内容未变时复用现有稳定 dataVersion。"""
        rows = (
            connection.execute(
                select(SectorMembershipReleaseSector.snapshot_id)
                .where(SectorMembershipReleaseSector.release_id == release_id)
                .order_by(SectorMembershipReleaseSector.snapshot_id)
            )
            .mappings()
            .all()
        )
        existing_ids = tuple(UUID(str(row["snapshot_id"])) for row in rows)
        return existing_ids == tuple(
            sorted(snapshot_ids)
        ) and quality_status == self._release_quality(connection, release_id)

    def _release_quality(self, connection: Session, release_id: UUID) -> str:
        """读取已比较 release 的质量标签，防止 carry-forward 变化却复用旧版本。"""
        return str(
            connection.execute(
                select(SectorMembershipRelease.quality_status).where(
                    SectorMembershipRelease.release_id == release_id
                )
            ).scalar_one()
        )

    def _publish_dataset(
        self,
        connection: Session,
        *,
        scheme: SectorScheme,
        data_version: UUID,
        quality_status: str,
        effective_as_of: date,
        published_at: datetime,
    ) -> None:
        """同步维护通用数据集发布指针，供跨服务健康和版本追踪统一读取。"""
        connection.execute(
            update(DatasetPublication)
            .where(
                DatasetPublication.dataset == _DATASET,
                DatasetPublication.partition_key == scheme.value,
                DatasetPublication.superseded_at.is_(None),
            )
            .values(superseded_at=published_at)
        )
        connection.execute(
            insert(DatasetPublication).values(
                publication_id=uuid4(),
                dataset=_DATASET,
                partition_key=scheme.value,
                data_version=data_version,
                quality_status=quality_status,
                effective_as_of=effective_as_of,
                knowledge_cutoff=published_at,
                published_at=published_at,
                superseded_at=None,
            )
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
