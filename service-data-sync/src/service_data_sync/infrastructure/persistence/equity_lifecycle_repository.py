"""使用 `ORM` 表达式写入显式上市生命周期的双时间修订。

仓储只接受交易所明确给出的上市、退市或经批准更正证据；不会从目录差集、行情缺席或
当前代码状态推断生命周期。每所交易所的发布在事务锁内串行，身份解析、状态机、来源
快照、`revision` 与检查点共同提交，防止并发同步写出互相矛盾的历史。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, insert, literal, select, update
from sqlalchemy.orm import Session

from service_data_sync.application.ports.canonical_release import CanonicalLineageRecord
from service_data_sync.application.ports.equity_lifecycle import (
    EquityLifecycleReplayCheckpoint,
    EquityLifecycleRepository,
    PublishedEquityLifecycle,
)
from service_data_sync.domain.equity import Exchange
from service_data_sync.domain.equity_master import (
    EquityIdentityResolutionStatus,
    EquityLifecycleEntry,
    EquityLifecycleEvidenceKind,
    EquityLifecycleStatus,
)
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.database.fenced_execution import (
    current_fenced_execution,
)
from service_data_sync.infrastructure.persistence.canonical_release_repository import (
    SqlAlchemyCanonicalReleaseRepository,
)
from service_data_sync.infrastructure.persistence.equity_identity_resolver import (
    resolve_identity_on_connection,
)
from service_data_sync.infrastructure.persistence.legacy_canonical_release_bridge import (
    publish_legacy_snapshot,
)
from service_data_sync.infrastructure.persistence.source_batch import record_source_observation

from ..database.models.equity.identity.equity_identifier_version import (
    EquityIdentifierVersion,
)
from ..database.models.equity.identity.equity_instrument import (
    EquityInstrument,
)
from ..database.models.equity.identity.equity_lifecycle_checkpoint import (
    EquityLifecycleCheckpoint,
)
from ..database.models.equity.identity.equity_listing_status_version import (
    EquityListingStatusVersion,
)
from ..database.models.equity.identity.equity_master_snapshot import (
    EquityMasterSnapshot,
)
from ..database.models.equity.identity.equity_master_snapshot_member import (
    EquityMasterSnapshotMember,
)
from ..database.models.equity.identity.equity_name_version import EquityNameVersion

_CAPABILITY = "equity.lifecycle.explicit"
_DATASET = "equity.lifecycle.explicit"


class EquityLifecycleTransitionError(ValueError):
    """表示显式证据无法安全映射到当前双时间生命周期历史。"""


class SqlAlchemyEquityLifecycleRepository(EquityLifecycleRepository):
    """持久化显式生命周期批次；不从目录差集或行情缺席推断状态。"""

    def __init__(self, database: DatabaseClient) -> None:
        """保存 Session 工厂，应用层不直接依赖事务或 SQLAlchemy。"""
        self._database = database
        self._release_repository = SqlAlchemyCanonicalReleaseRepository(database)

    def publish_lifecycle(
        self,
        *,
        exchange: Exchange,
        target_date: date,
        entries: tuple[EquityLifecycleEntry, ...],
        provider_id: str,
        source_payload_sha256: str,
        raw_uri: str,
        observed_at: datetime,
        upstream_source: str | None,
        adapter_version: str,
        schema_fingerprint: str,
        normalized_uri: str | None = None,
    ) -> PublishedEquityLifecycle:
        """在交易所锁内写入完整显式证据批次并在事实变化时推进发布版本。"""
        if not entries:
            raise ValueError("lifecycle entries must not be empty")
        if any(entry.identifier.exchange is not exchange for entry in entries):
            raise ValueError("lifecycle entry exchange must match publication exchange")
        now = datetime.now(UTC)
        business_hash = _business_hash(entries)
        with self._database.transaction() as session:
            self._lock_exchange(session, exchange)
            source_batch_id = record_source_observation(
                session,
                provider_id=provider_id,
                capability=_CAPABILITY,
                source_payload_sha256=source_payload_sha256,
                raw_uri=raw_uri,
                observed_at=observed_at,
                created_at=now,
                upstream_source=upstream_source,
                adapter_version=adapter_version,
                schema_fingerprint=schema_fingerprint,
            )
            snapshot_id = uuid4()
            self._insert_snapshot(
                session,
                snapshot_id=snapshot_id,
                exchange=exchange,
                target_date=target_date,
                source_batch_id=source_batch_id,
                observed_at=observed_at,
                row_count=len(entries),
                schema_fingerprint=schema_fingerprint,
                business_hash=business_hash,
            )
            inserted_count = 0
            unchanged_count = 0
            for ordinal, entry in enumerate(entries, start=1):
                security_id, changed = self._apply_entry(
                    session,
                    entry=entry,
                    source_batch_id=source_batch_id,
                    now=now,
                )
                self._insert_snapshot_member(
                    session,
                    snapshot_id=snapshot_id,
                    ordinal=ordinal,
                    entry=entry,
                    security_id=security_id,
                )
                if changed:
                    inserted_count += 1
                else:
                    unchanged_count += 1
            data_version = self._publish_exchange_version(
                session,
                exchange=exchange,
                snapshot_id=snapshot_id,
                source_batch_id=source_batch_id,
                effective_as_of=target_date,
                published_at=now,
            )
            self._advance_checkpoint(
                session,
                exchange=exchange,
                target_date=target_date,
                data_version=data_version,
                snapshot_id=snapshot_id,
                source_batch_id=source_batch_id,
                raw_uri=raw_uri,
                normalized_uri=normalized_uri or raw_uri,
                provider_id=provider_id,
                upstream_source=upstream_source or provider_id,
                adapter_version=adapter_version,
                schema_fingerprint=schema_fingerprint,
                observed_at=observed_at,
                updated_at=now,
            )
            _record_fenced_lifecycle_publication(
                data_version=data_version,
                record_count=len(entries),
            )
        return PublishedEquityLifecycle(
            snapshot_id=snapshot_id,
            data_version=data_version,
            inserted_count=inserted_count,
            unchanged_count=unchanged_count,
        )

    def get_replay_checkpoint(
        self, *, exchange: Exchange
    ) -> EquityLifecycleReplayCheckpoint | None:
        """读取最后成功检查点的标准证据位置和来源血缘。"""
        statement = select(
            EquityLifecycleCheckpoint.exchange,
            EquityLifecycleCheckpoint.target_date,
            EquityLifecycleCheckpoint.data_version,
            EquityLifecycleCheckpoint.snapshot_id,
            EquityLifecycleCheckpoint.raw_uri,
            EquityLifecycleCheckpoint.normalized_uri,
            EquityLifecycleCheckpoint.provider_id,
            EquityLifecycleCheckpoint.upstream_source,
            EquityLifecycleCheckpoint.adapter_version,
            EquityLifecycleCheckpoint.schema_fingerprint,
            EquityLifecycleCheckpoint.observed_at,
        ).where(EquityLifecycleCheckpoint.exchange == exchange.value)
        with self._database.session() as session:
            row = session.execute(statement).mappings().one_or_none()
        if row is None:
            return None
        return EquityLifecycleReplayCheckpoint(
            exchange=Exchange(str(row["exchange"])),
            target_date=row["target_date"],
            data_version=UUID(str(row["data_version"])),
            snapshot_id=UUID(str(row["snapshot_id"])),
            raw_uri=str(row["raw_uri"]),
            normalized_uri=str(row["normalized_uri"]),
            provider_id=str(row["provider_id"]),
            upstream_source=str(row["upstream_source"]),
            adapter_version=str(row["adapter_version"]),
            schema_fingerprint=str(row["schema_fingerprint"]),
            observed_at=row["observed_at"],
        )

    def _lock_exchange(self, connection: Session, exchange: Exchange) -> None:
        """为同一交易所目录与生命周期发布建立事务级互斥，避免双提交。"""
        connection.execute(
            select(
                func.pg_advisory_xact_lock(
                    func.hashtextextended(literal(f"equity-master:{exchange.value}"), literal(0))
                )
            )
        )

    def _insert_snapshot(
        self,
        connection: Session,
        *,
        snapshot_id: UUID,
        exchange: Exchange,
        target_date: date,
        source_batch_id: UUID,
        observed_at: datetime,
        row_count: int,
        schema_fingerprint: str,
        business_hash: bytes,
    ) -> None:
        """保存局部生命周期证据批次；它不能被当成全量目录用于缺席推断。"""
        connection.execute(
            insert(EquityMasterSnapshot).values(
                snapshot_id=snapshot_id,
                exchange=exchange.value,
                snapshot_kind="LIFECYCLE",
                target_date=target_date,
                source_batch_id=source_batch_id,
                observed_at=observed_at,
                row_count=row_count,
                schema_fingerprint=schema_fingerprint,
                completeness="PARTIAL",
                quality_status="passed",
                business_sha256=business_hash,
            )
        )

    def _apply_entry(
        self,
        connection: Session,
        *,
        entry: EquityLifecycleEntry,
        source_batch_id: UUID,
        now: datetime,
    ) -> tuple[int, bool]:
        """解析唯一确认身份；首次官方历史证据可安全冷启动或回填同一身份。"""
        resolution = resolve_identity_on_connection(
            connection,
            exchange=entry.identifier.exchange,
            symbol=entry.identifier.symbol,
            fact_date=entry.effective_on,
            known_at=now,
        )
        if resolution.status is EquityIdentityResolutionStatus.CONFLICT:
            raise EquityLifecycleTransitionError(
                "lifecycle identity conflicts at the official effective date"
            )
        if (
            resolution.status is EquityIdentityResolutionStatus.RESOLVED
            and resolution.identity_state == "CONFIRMED"
            and resolution.security_id is not None
        ):
            security_id = resolution.security_id
        elif resolution.status is EquityIdentityResolutionStatus.NOT_FOUND:
            security_id = self._bootstrap_missing_identity(
                connection,
                entry=entry,
                source_batch_id=source_batch_id,
                now=now,
            )
            return security_id, True
        else:
            raise EquityLifecycleTransitionError("lifecycle identity is not uniquely confirmed")
        current = (
            connection.execute(
                select(
                    EquityListingStatusVersion.version_id,
                    EquityListingStatusVersion.status,
                    EquityListingStatusVersion.listed_on,
                    EquityListingStatusVersion.delisted_on,
                    EquityListingStatusVersion.effective_from,
                    EquityListingStatusVersion.effective_to,
                    EquityListingStatusVersion.evidence_kind,
                )
                .where(
                    EquityListingStatusVersion.security_id == security_id,
                    EquityListingStatusVersion.effective_to.is_(None),
                    EquityListingStatusVersion.known_to.is_(None),
                )
                .order_by(EquityListingStatusVersion.effective_from.desc())
                .with_for_update()
            )
            .mappings()
            .one_or_none()
        )
        if current is None:
            raise EquityLifecycleTransitionError("confirmed identity has no current listing status")
        current_status = EquityLifecycleStatus(str(current["status"]))
        if _matches_current_entry(current, entry):
            if entry.status is EquityLifecycleStatus.DELISTED:
                self._close_identifier_after_delisting(
                    connection,
                    security_id=security_id,
                    entry=entry,
                )
            return security_id, False
        current_effective_from = current["effective_from"]
        if not isinstance(current_effective_from, date):
            raise EquityLifecycleTransitionError("current lifecycle effective date is invalid")
        if current_effective_from > entry.effective_on:
            raise EquityLifecycleTransitionError("lifecycle fact predates current status")
        is_knowledge_correction = (
            entry.evidence_kind is EquityLifecycleEvidenceKind.OFFICIAL_CORRECTION
        )
        is_catalog_evidence_upgrade = _is_catalog_evidence_upgrade(current, entry)
        if (
            current_status is entry.status
            and not is_knowledge_correction
            and not is_catalog_evidence_upgrade
        ):
            raise EquityLifecycleTransitionError(
                "same lifecycle status requires official correction"
            )
        if current_status is not entry.status and not _is_transition_allowed(
            current_status, entry.status, is_knowledge_correction
        ):
            raise EquityLifecycleTransitionError("lifecycle status transition is not allowed")
        if is_knowledge_correction and current_effective_from != entry.effective_on:
            raise EquityLifecycleTransitionError(
                "official correction must use the current lifecycle effective date"
            )
        closes_knowledge_only = current_effective_from == entry.effective_on
        if closes_knowledge_only:
            # 同一市场有效日的官方更正关闭旧知识版本，保留何时得到旧结论的审计链。
            connection.execute(
                update(EquityListingStatusVersion)
                .where(EquityListingStatusVersion.version_id == current["version_id"])
                .values(known_to=now)
            )
        else:
            connection.execute(
                update(EquityListingStatusVersion)
                .where(EquityListingStatusVersion.version_id == current["version_id"])
                .values(effective_to=entry.effective_on)
            )
        version_id = uuid4()
        listed_on = entry.listed_on or current["listed_on"]
        effective_to = current["effective_to"] if closes_knowledge_only else None
        connection.execute(
            insert(EquityListingStatusVersion).values(
                version_id=version_id,
                security_id=security_id,
                status=entry.status.value,
                listed_on=listed_on,
                delisted_on=entry.delisted_on,
                effective_from=entry.effective_on,
                effective_to=effective_to,
                known_from=now,
                known_to=None,
                effective_date_precision="OFFICIAL_DATE",
                evidence_kind=entry.evidence_kind.value,
                correction_approval_reference=entry.correction_approval_reference,
                source_batch_id=source_batch_id,
                content_sha256=_entry_hash(entry),
            )
        )
        connection.execute(
            update(EquityInstrument)
            .where(EquityInstrument.security_id == security_id)
            .values(
                listing_status=entry.status.value,
                current_master_version=version_id,
                updated_at=now,
            )
        )
        if entry.status is EquityLifecycleStatus.DELISTED:
            self._close_identifier_after_delisting(
                connection,
                security_id=security_id,
                entry=entry,
            )
        elif (
            current_status is EquityLifecycleStatus.DELISTED
            and entry.status is EquityLifecycleStatus.LISTED
            and is_knowledge_correction
        ):
            self._reopen_identifier_after_correction(
                connection,
                security_id=security_id,
                entry=entry,
            )
        return security_id, True

    def _bootstrap_missing_identity(
        self,
        connection: Session,
        *,
        entry: EquityLifecycleEntry,
        source_batch_id: UUID,
        now: datetime,
    ) -> int:
        """按官方日期冷启动历史退市身份，或回填当前在册身份的观测日边界。

        名称和上市日必须来自交易所行；缺少任一语义字段就拒绝创建永久身份。普通暂停、
        复牌和更正必须先命中既有身份，不能借此入口猜测代码归属。
        """
        if entry.name is None or entry.listed_on is None:
            raise EquityLifecycleTransitionError(
                "lifecycle identity bootstrap requires official name and listed_on"
            )
        if (
            entry.status is EquityLifecycleStatus.DELISTED
            and entry.evidence_kind is EquityLifecycleEvidenceKind.EXPLICIT_DELISTING
        ):
            return self._bootstrap_historical_delisted_identity(
                connection,
                entry=entry,
                source_batch_id=source_batch_id,
                now=now,
            )
        if (
            entry.status is EquityLifecycleStatus.LISTED
            and entry.evidence_kind is EquityLifecycleEvidenceKind.EXPLICIT_LISTING
        ):
            return self._backfill_or_create_current_listing_identity(
                connection,
                entry=entry,
                source_batch_id=source_batch_id,
                now=now,
            )
        raise EquityLifecycleTransitionError(
            "lifecycle transition cannot bootstrap an unknown identity"
        )

    def _bootstrap_historical_delisted_identity(
        self,
        connection: Session,
        *,
        entry: EquityLifecycleEntry,
        source_batch_id: UUID,
        now: datetime,
    ) -> int:
        """从官方上市/退市日期创建历史证券，并保留代码复用后的当前身份。"""
        if (
            entry.name is None
            or entry.listed_on is None
            or entry.delisted_on is None
            or entry.delisted_on != entry.effective_on
            or entry.listed_on >= entry.delisted_on
        ):
            raise EquityLifecycleTransitionError(
                "historical delisting bootstrap requires ordered official dates"
            )
        try:
            identifier_end = entry.delisted_on + timedelta(days=1)
        except OverflowError as error:
            raise EquityLifecycleTransitionError(
                "historical identifier boundary exceeds date range"
            ) from error
        conflict = connection.execute(
            select(EquityIdentifierVersion.version_id)
            .where(
                EquityIdentifierVersion.exchange == entry.identifier.exchange.value,
                EquityIdentifierVersion.symbol == entry.identifier.symbol,
                EquityIdentifierVersion.identity_state == "CONFIRMED",
                EquityIdentifierVersion.known_to.is_(None),
                EquityIdentifierVersion.effective_range.op("&&")(
                    func.daterange(entry.listed_on, identifier_end, "[)")
                ),
            )
            .limit(1)
            .with_for_update()
        ).scalar_one_or_none()
        if conflict is not None:
            raise EquityLifecycleTransitionError(
                "historical lifecycle evidence overlaps an existing identity"
            )
        security_id = self._insert_instrument(
            connection,
            entry=entry,
            listing_status="DELISTED",
            now=now,
        )
        identifier_version_id = uuid4()
        delisted_version_id = uuid4()
        self._insert_identifier_version(
            connection,
            version_id=identifier_version_id,
            security_id=security_id,
            entry=entry,
            effective_from=entry.listed_on,
            effective_to=identifier_end,
            source_batch_id=source_batch_id,
            now=now,
        )
        self._insert_name_version(
            connection,
            security_id=security_id,
            entry=entry,
            effective_from=entry.listed_on,
            effective_to=identifier_end,
            source_batch_id=source_batch_id,
            now=now,
        )
        self._insert_listing_version(
            connection,
            version_id=uuid4(),
            security_id=security_id,
            entry=entry,
            status=EquityLifecycleStatus.LISTED,
            effective_from=entry.listed_on,
            effective_to=entry.delisted_on,
            evidence_kind=EquityLifecycleEvidenceKind.EXPLICIT_LISTING,
            source_batch_id=source_batch_id,
            now=now,
        )
        self._insert_listing_version(
            connection,
            version_id=delisted_version_id,
            security_id=security_id,
            entry=entry,
            status=EquityLifecycleStatus.DELISTED,
            effective_from=entry.delisted_on,
            effective_to=None,
            evidence_kind=EquityLifecycleEvidenceKind.EXPLICIT_DELISTING,
            source_batch_id=source_batch_id,
            now=now,
        )
        connection.execute(
            update(EquityInstrument)
            .where(EquityInstrument.security_id == security_id)
            .values(current_master_version=delisted_version_id)
        )
        return security_id

    def _backfill_or_create_current_listing_identity(
        self,
        connection: Session,
        *,
        entry: EquityLifecycleEntry,
        source_batch_id: UUID,
        now: datetime,
    ) -> int:
        """用官方上市日修订当前目录的观测日知识版本，不创建重叠永久身份。"""
        if entry.name is None or entry.listed_on is None or entry.listed_on != entry.effective_on:
            raise EquityLifecycleTransitionError(
                "listing bootstrap requires an official effective listed_on"
            )
        current = (
            connection.execute(
                select(
                    EquityIdentifierVersion.version_id,
                    EquityIdentifierVersion.security_id,
                    EquityIdentifierVersion.identity_state,
                    EquityIdentifierVersion.effective_from,
                    EquityIdentifierVersion.effective_to,
                )
                .where(
                    EquityIdentifierVersion.exchange == entry.identifier.exchange.value,
                    EquityIdentifierVersion.symbol == entry.identifier.symbol,
                    EquityIdentifierVersion.effective_to.is_(None),
                    EquityIdentifierVersion.known_to.is_(None),
                )
                .with_for_update()
            )
            .mappings()
            .one_or_none()
        )
        if current is None:
            security_id = self._insert_instrument(
                connection,
                entry=entry,
                listing_status="LISTED",
                now=now,
            )
        else:
            if str(current["identity_state"]) != "CONFIRMED":
                raise EquityLifecycleTransitionError("current listing identity is not confirmed")
            if current["effective_from"] <= entry.listed_on:
                raise EquityLifecycleTransitionError(
                    "current listing identity cannot be backfilled safely"
                )
            security_id = int(current["security_id"])
            self._close_single_observation_version(
                connection,
                model=EquityNameVersion,
                security_id=security_id,
                expected_effective_from=current["effective_from"],
                now=now,
            )
            self._close_single_observation_version(
                connection,
                model=EquityListingStatusVersion,
                security_id=security_id,
                expected_effective_from=current["effective_from"],
                now=now,
            )
            connection.execute(
                update(EquityIdentifierVersion)
                .where(EquityIdentifierVersion.version_id == current["version_id"])
                .values(known_to=now)
            )
        identifier_version_id = uuid4()
        listing_version_id = uuid4()
        self._insert_identifier_version(
            connection,
            version_id=identifier_version_id,
            security_id=security_id,
            entry=entry,
            effective_from=entry.listed_on,
            effective_to=None,
            source_batch_id=source_batch_id,
            now=now,
        )
        self._insert_name_version(
            connection,
            security_id=security_id,
            entry=entry,
            effective_from=entry.listed_on,
            effective_to=None,
            source_batch_id=source_batch_id,
            now=now,
        )
        self._insert_listing_version(
            connection,
            version_id=listing_version_id,
            security_id=security_id,
            entry=entry,
            status=EquityLifecycleStatus.LISTED,
            effective_from=entry.listed_on,
            effective_to=None,
            evidence_kind=EquityLifecycleEvidenceKind.EXPLICIT_LISTING,
            source_batch_id=source_batch_id,
            now=now,
        )
        connection.execute(
            update(EquityInstrument)
            .where(EquityInstrument.security_id == security_id)
            .values(
                name=entry.name,
                listing_status="LISTED",
                master_confirmed_at=now,
                current_master_version=listing_version_id,
                updated_at=now,
            )
        )
        return security_id

    @staticmethod
    def _close_single_observation_version(
        connection: Session,
        *,
        model: type[EquityNameVersion] | type[EquityListingStatusVersion],
        security_id: int,
        expected_effective_from: date,
        now: datetime,
    ) -> None:
        """只关闭首次目录创建的单一观测知识版本，拒绝覆盖已有历史修订。"""
        rows = (
            connection.execute(
                select(model.version_id, model.effective_from, model.effective_to)
                .where(
                    model.security_id == security_id,
                    model.known_to.is_(None),
                )
                .with_for_update()
            )
            .mappings()
            .all()
        )
        if (
            len(rows) != 1
            or rows[0]["effective_from"] != expected_effective_from
            or rows[0]["effective_to"] is not None
        ):
            raise EquityLifecycleTransitionError(
                "official listing backfill conflicts with existing history"
            )
        connection.execute(
            update(model).where(model.version_id == rows[0]["version_id"]).values(known_to=now)
        )

    @staticmethod
    def _insert_instrument(
        connection: Session,
        *,
        entry: EquityLifecycleEntry,
        listing_status: str,
        now: datetime,
    ) -> int:
        """创建由官方生命周期证据确认的永久证券锚。"""
        return int(
            connection.execute(
                insert(EquityInstrument)
                .values(
                    instrument_id=uuid4(),
                    exchange=entry.identifier.exchange.value,
                    symbol=entry.identifier.symbol,
                    name=entry.name,
                    listing_status=listing_status,
                    created_at=now,
                    updated_at=now,
                    master_confirmed_at=now,
                    current_master_version=uuid4(),
                )
                .returning(EquityInstrument.security_id)
            ).scalar_one()
        )

    @staticmethod
    def _insert_identifier_version(
        connection: Session,
        *,
        version_id: UUID,
        security_id: int,
        entry: EquityLifecycleEntry,
        effective_from: date,
        effective_to: date | None,
        source_batch_id: UUID,
        now: datetime,
    ) -> None:
        """写入带官方日期精度的确认代码版本。"""
        connection.execute(
            insert(EquityIdentifierVersion).values(
                version_id=version_id,
                security_id=security_id,
                exchange=entry.identifier.exchange.value,
                symbol=entry.identifier.symbol,
                identity_state="CONFIRMED",
                effective_from=effective_from,
                effective_to=effective_to,
                known_from=now,
                known_to=None,
                effective_date_precision="OFFICIAL_DATE",
                source_batch_id=source_batch_id,
                content_sha256=_identity_hash(entry, effective_from, effective_to),
            )
        )

    @staticmethod
    def _insert_name_version(
        connection: Session,
        *,
        security_id: int,
        entry: EquityLifecycleEntry,
        effective_from: date,
        effective_to: date | None,
        source_batch_id: UUID,
        now: datetime,
    ) -> None:
        """写入交易所官方简称的有效区间，不用代码或当前目录名称补位。"""
        if entry.name is None:
            raise EquityLifecycleTransitionError("official lifecycle name is unavailable")
        connection.execute(
            insert(EquityNameVersion).values(
                version_id=uuid4(),
                security_id=security_id,
                name=entry.name,
                effective_from=effective_from,
                effective_to=effective_to,
                known_from=now,
                known_to=None,
                effective_date_precision="OFFICIAL_DATE",
                source_batch_id=source_batch_id,
                content_sha256=_name_hash(entry, effective_from, effective_to),
            )
        )

    @staticmethod
    def _insert_listing_version(
        connection: Session,
        *,
        version_id: UUID,
        security_id: int,
        entry: EquityLifecycleEntry,
        status: EquityLifecycleStatus,
        effective_from: date,
        effective_to: date | None,
        evidence_kind: EquityLifecycleEvidenceKind,
        source_batch_id: UUID,
        now: datetime,
    ) -> None:
        """写入由同一官方批次支持的上市或退市状态区间。"""
        delisted_on = entry.delisted_on if status is EquityLifecycleStatus.DELISTED else None
        connection.execute(
            insert(EquityListingStatusVersion).values(
                version_id=version_id,
                security_id=security_id,
                status=status.value,
                listed_on=entry.listed_on,
                delisted_on=delisted_on,
                effective_from=effective_from,
                effective_to=effective_to,
                known_from=now,
                known_to=None,
                effective_date_precision="OFFICIAL_DATE",
                evidence_kind=evidence_kind.value,
                correction_approval_reference=None,
                source_batch_id=source_batch_id,
                content_sha256=_listing_hash(
                    entry,
                    status,
                    effective_from,
                    effective_to,
                    evidence_kind,
                ),
            )
        )

    @staticmethod
    def _close_identifier_after_delisting(
        connection: Session,
        *,
        security_id: int,
        entry: EquityLifecycleEntry,
    ) -> None:
        """在退市日次日关闭旧代码，保留退市事实日本身仍可解析到旧证券。"""
        if entry.delisted_on is None:
            raise EquityLifecycleTransitionError("delisting identifier boundary is unavailable")
        try:
            boundary = entry.delisted_on + timedelta(days=1)
        except OverflowError as error:
            raise EquityLifecycleTransitionError(
                "delisting identifier boundary exceeds date range"
            ) from error
        current = (
            connection.execute(
                select(
                    EquityIdentifierVersion.version_id,
                    EquityIdentifierVersion.effective_from,
                    EquityIdentifierVersion.effective_to,
                )
                .where(
                    EquityIdentifierVersion.security_id == security_id,
                    EquityIdentifierVersion.exchange == entry.identifier.exchange.value,
                    EquityIdentifierVersion.symbol == entry.identifier.symbol,
                    EquityIdentifierVersion.identity_state == "CONFIRMED",
                    EquityIdentifierVersion.known_to.is_(None),
                )
                .order_by(EquityIdentifierVersion.known_from.desc())
                .with_for_update()
            )
            .mappings()
            .one_or_none()
        )
        if current is None or current["effective_from"] >= boundary:
            raise EquityLifecycleTransitionError("delisting cannot close identifier history")
        if current["effective_to"] is None:
            connection.execute(
                update(EquityIdentifierVersion)
                .where(EquityIdentifierVersion.version_id == current["version_id"])
                .values(effective_to=boundary)
            )
        elif current["effective_to"] != boundary:
            raise EquityLifecycleTransitionError(
                "delisting conflicts with existing identifier boundary"
            )

    @staticmethod
    def _reopen_identifier_after_correction(
        connection: Session,
        *,
        security_id: int,
        entry: EquityLifecycleEntry,
    ) -> None:
        """撤销错误退市边界；若代码已被另一证券复用则拒绝覆盖历史。"""
        current = (
            connection.execute(
                select(
                    EquityIdentifierVersion.version_id,
                    EquityIdentifierVersion.effective_to,
                )
                .where(
                    EquityIdentifierVersion.security_id == security_id,
                    EquityIdentifierVersion.exchange == entry.identifier.exchange.value,
                    EquityIdentifierVersion.symbol == entry.identifier.symbol,
                    EquityIdentifierVersion.identity_state == "CONFIRMED",
                    EquityIdentifierVersion.known_to.is_(None),
                )
                .order_by(EquityIdentifierVersion.known_from.desc())
                .with_for_update()
            )
            .mappings()
            .one_or_none()
        )
        if current is None:
            raise EquityLifecycleTransitionError("corrected identifier history is unavailable")
        if current["effective_to"] is None:
            return
        conflict = connection.execute(
            select(EquityIdentifierVersion.version_id)
            .where(
                EquityIdentifierVersion.security_id != security_id,
                EquityIdentifierVersion.exchange == entry.identifier.exchange.value,
                EquityIdentifierVersion.symbol == entry.identifier.symbol,
                EquityIdentifierVersion.identity_state == "CONFIRMED",
                EquityIdentifierVersion.known_to.is_(None),
                EquityIdentifierVersion.effective_range.op("&&")(
                    func.daterange(current["effective_to"], None, "[)")
                ),
            )
            .limit(1)
        ).scalar_one_or_none()
        if conflict is not None:
            raise EquityLifecycleTransitionError(
                "official correction conflicts with a reused identifier"
            )
        connection.execute(
            update(EquityIdentifierVersion)
            .where(EquityIdentifierVersion.version_id == current["version_id"])
            .values(effective_to=None)
        )

    def _insert_snapshot_member(
        self,
        connection: Session,
        *,
        snapshot_id: UUID,
        ordinal: int,
        entry: EquityLifecycleEntry,
        security_id: int,
    ) -> None:
        """将每条显式证据与解析到的身份绑定，供审计和质量回放读取。"""
        connection.execute(
            insert(EquityMasterSnapshotMember).values(
                snapshot_id=snapshot_id,
                row_ordinal=ordinal,
                exchange=entry.identifier.exchange.value,
                symbol=entry.identifier.symbol,
                name=entry.name,
                listed_on=entry.listed_on,
                candidate_status=entry.status.value,
                candidate_status_date=entry.effective_on,
                effective_date_precision="OFFICIAL_DATE",
                security_id=security_id,
                resolution_status="resolved",
                content_sha256=_entry_hash(entry),
            )
        )

    def _advance_checkpoint(
        self,
        connection: Session,
        *,
        exchange: Exchange,
        target_date: date,
        data_version: UUID,
        snapshot_id: UUID,
        source_batch_id: UUID,
        raw_uri: str,
        normalized_uri: str,
        provider_id: str,
        upstream_source: str,
        adapter_version: str,
        schema_fingerprint: str,
        observed_at: datetime,
        updated_at: datetime,
    ) -> None:
        """与 lifecycle 修订和 publication 同事务推进恢复位置，失败时不越过旧证据。"""
        values = {
            "target_date": target_date,
            "data_version": data_version,
            "snapshot_id": snapshot_id,
            "source_batch_id": source_batch_id,
            "raw_uri": raw_uri,
            "normalized_uri": normalized_uri,
            "provider_id": provider_id,
            "upstream_source": upstream_source,
            "adapter_version": adapter_version,
            "schema_fingerprint": schema_fingerprint,
            "observed_at": observed_at,
            "updated_at": updated_at,
        }
        exists = connection.execute(
            select(EquityLifecycleCheckpoint.exchange).where(
                EquityLifecycleCheckpoint.exchange == exchange.value
            )
        ).scalar_one_or_none()
        if exists is None:
            connection.execute(
                insert(EquityLifecycleCheckpoint).values(
                    exchange=exchange.value,
                    **values,
                )
            )
            return
        connection.execute(
            update(EquityLifecycleCheckpoint)
            .where(EquityLifecycleCheckpoint.exchange == exchange.value)
            .values(**values)
        )

    def _publish_exchange_version(
        self,
        connection: Session,
        *,
        exchange: Exchange,
        snapshot_id: UUID,
        source_batch_id: UUID,
        effective_as_of: date,
        published_at: datetime,
    ) -> UUID:
        """把显式生命周期快照通过统一 release 发布器固化为可恢复消费者版本。"""
        return self._publish_lifecycle_release(
            connection,
            exchange=exchange,
            snapshot_id=snapshot_id,
            source_batch_id=source_batch_id,
            effective_as_of=effective_as_of,
            published_at=published_at,
        )

    def _publish_lifecycle_release(
        self,
        connection: Session,
        *,
        exchange: Exchange,
        snapshot_id: UUID,
        source_batch_id: UUID,
        effective_as_of: date,
        published_at: datetime,
    ) -> UUID:
        """从已落库显式证据成员构造 release 候选，避免旁路统一 publication。"""
        records = _lifecycle_release_records(
            connection,
            snapshot_id=snapshot_id,
            source_batch_id=source_batch_id,
        )
        published = publish_legacy_snapshot(
            connection,
            release_repository=self._release_repository,
            dataset_code=_DATASET,
            partition_key=exchange.value,
            domain="equity",
            grain="exchange lifecycle evidence snapshot + explicit status rows",
            semantic_family="reported-equity-reference",
            mapping_version="equity-lifecycle-explicit-release-v1",
            source_batch_id=source_batch_id,
            records=records,
            fact_min=effective_as_of,
            fact_max=effective_as_of,
            now=published_at,
            publication_effective_as_of=effective_as_of,
        )
        return published.data_version


def _lifecycle_release_records(
    connection: Session,
    *,
    snapshot_id: UUID,
    source_batch_id: UUID,
) -> tuple[CanonicalLineageRecord, ...]:
    """从刚写入的显式生命周期快照构造 release 的逐条来源血缘。"""
    rows = (
        connection.execute(
            select(
                EquityMasterSnapshotMember.exchange,
                EquityMasterSnapshotMember.symbol,
                EquityMasterSnapshotMember.content_sha256,
            )
            .where(EquityMasterSnapshotMember.snapshot_id == snapshot_id)
            .order_by(
                EquityMasterSnapshotMember.exchange,
                EquityMasterSnapshotMember.symbol,
            )
        )
        .mappings()
        .all()
    )
    if not rows:
        raise ValueError("lifecycle snapshot has no members for canonical release")
    transform_hash = hashlib.sha256(b"equity-lifecycle-explicit-release-v1").hexdigest()
    return tuple(
        CanonicalLineageRecord(
            record_key_hash=hashlib.sha256(
                f"lifecycle:{row['exchange']}:{row['symbol']}".encode()
            ).hexdigest(),
            content_hash=bytes(row["content_sha256"]).hex(),
            source_batch_id=source_batch_id,
            transform_hash=transform_hash,
            role="primary",
        )
        for row in rows
    )


def _record_fenced_lifecycle_publication(*, data_version: UUID, record_count: int) -> None:
    """把生命周期分区进度和版本交给当前 fenced 控制面同事务落账。"""
    execution = current_fenced_execution()
    if execution is not None:
        execution.record_publication_progress(record_count=record_count)
        execution.record_checkpoint(kind="data-version", position=str(data_version))


def _is_transition_allowed(
    current: EquityLifecycleStatus, target: EquityLifecycleStatus, is_knowledge_correction: bool
) -> bool:
    """约束生命周期状态机，仅允许带来源证据的官方更正逆转终态。"""
    if (
        is_knowledge_correction
        and current is EquityLifecycleStatus.DELISTED
        and target is EquityLifecycleStatus.LISTED
    ):
        return True
    return (
        target
        in {
            EquityLifecycleStatus.LISTED: {
                EquityLifecycleStatus.SUSPENDED,
                EquityLifecycleStatus.DELISTED,
            },
            EquityLifecycleStatus.SUSPENDED: {
                EquityLifecycleStatus.LISTED,
                EquityLifecycleStatus.DELISTED,
            },
            EquityLifecycleStatus.DELISTED: set(),
        }[current]
    )


def _matches_current_entry(current: Mapping[Any, Any], entry: EquityLifecycleEntry) -> bool:
    """判断新证据是否与开放知识版本完全等价，避免重复请求制造虚假修订。

    目录 `CATALOG` 与交易所显式证据即使业务字段相同，也不是同一审计结论；后者必须有
    独立知识版本，才能让 resolved 视图在不改变事实日期的前提下升级来源等级。
    """
    return (
        _matches_current_business_fields(current, entry)
        and str(current["evidence_kind"]) == entry.evidence_kind.value
    )


def _matches_current_business_fields(
    current: Mapping[Any, Any], entry: EquityLifecycleEntry
) -> bool:
    """比较不含来源等级的生命周期业务字段，供受控证据升级复用。"""
    listed_on = entry.listed_on or current["listed_on"]
    return (
        str(current["status"]) == entry.status.value
        and current["listed_on"] == listed_on
        and current["delisted_on"] == entry.delisted_on
        and current["effective_from"] == entry.effective_on
        and current["effective_to"] is None
    )


def _is_catalog_evidence_upgrade(current: Mapping[Any, Any], entry: EquityLifecycleEntry) -> bool:
    """判断同一事实能否由目录证据安全升级为交易所显式证据。

    仅允许开放的 `CATALOG` 版本被同一业务事实替换。不同日期、状态或字段仍须走既有
    状态机/官方更正约束，避免把来源升级误当成事实改写。
    """
    return str(current["evidence_kind"]) == "CATALOG" and _matches_current_business_fields(
        current, entry
    )


def _business_hash(entries: tuple[EquityLifecycleEntry, ...]) -> bytes:
    """构造排序后生命周期事实哈希，排除每次均变化的观测元数据。"""
    return hashlib.sha256(
        json.dumps(
            [
                {
                    "exchange": entry.identifier.exchange.value,
                    "symbol": entry.identifier.symbol,
                    "name": entry.name,
                    "status": entry.status.value,
                    "effectiveOn": entry.effective_on.isoformat(),
                    "evidenceKind": entry.evidence_kind.value,
                    "listedOn": None if entry.listed_on is None else entry.listed_on.isoformat(),
                    "delistedOn": None
                    if entry.delisted_on is None
                    else entry.delisted_on.isoformat(),
                    "correctionApprovalReference": entry.correction_approval_reference,
                }
                for entry in entries
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
    ).digest()


def _entry_hash(entry: EquityLifecycleEntry) -> bytes:
    """生成单条显式证据业务哈希，便于审计修订而不暴露原始载荷。"""
    return _business_hash((entry,))


def _identity_hash(
    entry: EquityLifecycleEntry,
    effective_from: date,
    effective_to: date | None,
) -> bytes:
    """生成官方生命周期身份版本的稳定内容哈希。"""
    return _stable_hash(
        {
            "exchange": entry.identifier.exchange.value,
            "symbol": entry.identifier.symbol,
            "effectiveFrom": effective_from.isoformat(),
            "effectiveTo": None if effective_to is None else effective_to.isoformat(),
            "identityState": "CONFIRMED",
        }
    )


def _name_hash(
    entry: EquityLifecycleEntry,
    effective_from: date,
    effective_to: date | None,
) -> bytes:
    """生成官方简称版本的稳定内容哈希。"""
    return _stable_hash(
        {
            "name": entry.name,
            "effectiveFrom": effective_from.isoformat(),
            "effectiveTo": None if effective_to is None else effective_to.isoformat(),
        }
    )


def _listing_hash(
    entry: EquityLifecycleEntry,
    status: EquityLifecycleStatus,
    effective_from: date,
    effective_to: date | None,
    evidence_kind: EquityLifecycleEvidenceKind,
) -> bytes:
    """生成官方上市状态区间的稳定内容哈希。"""
    return _stable_hash(
        {
            "status": status.value,
            "listedOn": None if entry.listed_on is None else entry.listed_on.isoformat(),
            "delistedOn": None if entry.delisted_on is None else entry.delisted_on.isoformat(),
            "effectiveFrom": effective_from.isoformat(),
            "effectiveTo": None if effective_to is None else effective_to.isoformat(),
            "evidenceKind": evidence_kind.value,
        }
    )


def _stable_hash(value: Mapping[str, object]) -> bytes:
    """把版本业务字段序列化为确定性 SHA-256 摘要。"""
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).digest()
