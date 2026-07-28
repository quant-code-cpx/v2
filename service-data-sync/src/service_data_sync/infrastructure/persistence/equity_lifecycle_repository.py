"""使用 ORM-enabled 表达式写入显式上市生命周期的双时间修订。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, insert, literal, select, update
from sqlalchemy.orm import Session

from service_data_sync.application.ports.equity_lifecycle import (
    EquityLifecycleRepository,
    PublishedEquityLifecycle,
)
from service_data_sync.domain.equity import Exchange
from service_data_sync.domain.equity_master import (
    EquityLifecycleEntry,
    EquityLifecycleEvidenceKind,
    EquityLifecycleStatus,
)
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.persistence.source_batch import record_source_observation

from ..database.models.equity.identity.equity_identifier_version import (
    EquityIdentifierVersion,
)
from ..database.models.equity.identity.equity_instrument import (
    EquityInstrument,
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
from ..database.models.publication.dataset_publication import (
    DatasetPublication,
)

_CAPABILITY = "equity.lifecycle.explicit"
_DATASET = "equity.master.catalog"


class EquityLifecycleTransitionError(ValueError):
    """表示显式证据无法安全映射到当前双时间生命周期历史。"""


class SqlAlchemyEquityLifecycleRepository(EquityLifecycleRepository):
    """持久化显式生命周期批次；不从目录差集或行情缺席推断状态。"""

    def __init__(self, database: DatabaseClient) -> None:
        """保存 Session 工厂，应用层不直接依赖事务或 SQLAlchemy。"""
        self._database = database

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
                effective_as_of=target_date,
                published_at=now,
                business_changed=inserted_count > 0,
            )
        return PublishedEquityLifecycle(
            snapshot_id=snapshot_id,
            data_version=data_version,
            inserted_count=inserted_count,
            unchanged_count=unchanged_count,
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
        """解析唯一确认身份、校验状态机并追加或复用生命周期事实。"""
        identity_rows = (
            connection.execute(
                select(EquityIdentifierVersion.security_id)
                .where(
                    EquityIdentifierVersion.exchange == entry.identifier.exchange.value,
                    EquityIdentifierVersion.symbol == entry.identifier.symbol,
                    EquityIdentifierVersion.identity_state == "CONFIRMED",
                    EquityIdentifierVersion.effective_range.op("@>")(entry.effective_on),
                    EquityIdentifierVersion.knowledge_range.op("@>")(now),
                )
                .order_by(EquityIdentifierVersion.security_id)
                .with_for_update()
            )
            .mappings()
            .all()
        )
        if len(identity_rows) != 1:
            raise EquityLifecycleTransitionError("lifecycle identity is not uniquely confirmed")
        security_id = int(identity_rows[0]["security_id"])
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
            return security_id, False
        current_effective_from = current["effective_from"]
        if not isinstance(current_effective_from, date):
            raise EquityLifecycleTransitionError("current lifecycle effective date is invalid")
        if current_effective_from > entry.effective_on:
            raise EquityLifecycleTransitionError("lifecycle fact predates current status")
        is_knowledge_correction = (
            entry.evidence_kind is EquityLifecycleEvidenceKind.OFFICIAL_CORRECTION
        )
        if current_status is entry.status and not is_knowledge_correction:
            raise EquityLifecycleTransitionError(
                "same lifecycle status requires official correction"
            )
        if not _is_transition_allowed(current_status, entry.status, is_knowledge_correction):
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
        return security_id, True

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
                name=None,
                listed_on=entry.listed_on,
                candidate_status=entry.status.value,
                candidate_status_date=entry.effective_on,
                effective_date_precision="OFFICIAL_DATE",
                security_id=security_id,
                resolution_status="resolved",
                content_sha256=_entry_hash(entry),
            )
        )

    def _publish_exchange_version(
        self,
        connection: Session,
        *,
        exchange: Exchange,
        effective_as_of: date,
        published_at: datetime,
        business_changed: bool,
    ) -> UUID:
        """生命周期事实变化时替换该交易所目录切片，未变化时复用稳定版本。"""
        current = connection.execute(
            select(DatasetPublication.data_version).where(
                DatasetPublication.dataset == _DATASET,
                DatasetPublication.partition_key == exchange.value,
                DatasetPublication.superseded_at.is_(None),
            )
        ).scalar_one_or_none()
        if current is None:
            raise EquityLifecycleTransitionError("lifecycle requires a catalog publication")
        if not business_changed:
            return UUID(str(current))
        connection.execute(
            update(DatasetPublication)
            .where(
                DatasetPublication.dataset == _DATASET,
                DatasetPublication.partition_key == exchange.value,
                DatasetPublication.superseded_at.is_(None),
            )
            .values(superseded_at=published_at)
        )
        data_version = uuid4()
        connection.execute(
            insert(DatasetPublication).values(
                publication_id=uuid4(),
                dataset=_DATASET,
                partition_key=exchange.value,
                data_version=data_version,
                quality_status="passed",
                effective_as_of=effective_as_of,
                knowledge_cutoff=published_at,
                published_at=published_at,
                superseded_at=None,
            )
        )
        return data_version


def _is_transition_allowed(
    current: EquityLifecycleStatus, target: EquityLifecycleStatus, is_knowledge_correction: bool
) -> bool:
    """约束生命周期状态机，仅允许带审批的官方更正逆转终态。"""
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
    """判断新证据是否与开放知识版本等价，避免重复请求制造虚假修订。"""
    listed_on = entry.listed_on or current["listed_on"]
    return (
        str(current["status"]) == entry.status.value
        and current["listed_on"] == listed_on
        and current["delisted_on"] == entry.delisted_on
        and current["effective_from"] == entry.effective_on
        and current["effective_to"] is None
    )


def _business_hash(entries: tuple[EquityLifecycleEntry, ...]) -> bytes:
    """构造排序后生命周期事实哈希，排除每次均变化的观测元数据。"""
    return hashlib.sha256(
        json.dumps(
            [
                {
                    "exchange": entry.identifier.exchange.value,
                    "symbol": entry.identifier.symbol,
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
