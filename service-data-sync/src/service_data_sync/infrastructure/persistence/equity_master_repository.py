"""使用 `SQLAlchemy Core` 发布证券目录快照和双时间主数据版本。

完整目录快照生成身份、名称和初始上市事实的双时间版本；已知上市日与仅观察到目录日
必须区别处理，不能把抓取日伪装为官方上市日。代码可能复用，因而快照差异只形成证据
和修订，不能单独推断退市；消费者只经原子 `publication` 读取稳定版本。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import case, exists, func, insert, literal, literal_column, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.orm import Session

from service_data_sync.application.ports.canonical_release import CanonicalLineageRecord
from service_data_sync.application.ports.equity_master import (
    EquityMasterRepository,
    PublishedCnAAggregate,
    PublishedEquityCatalog,
)
from service_data_sync.domain.equity import Exchange
from service_data_sync.domain.equity_master import (
    EquityCatalogCompletenessError,
    EquityCatalogEntry,
)

from ..database.connection import DatabaseClient
from ..database.fenced_execution import current_fenced_execution
from ..database.models.canonical import CanonicalRecordLineage
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
from ..database.models.equity.identity.equity_name_version import (
    EquityNameVersion,
)
from ..database.models.equity.identity.equity_presence_anomaly import (
    EquityPresenceAnomaly,
)
from ..database.models.provenance.source_batch import SourceBatch
from ..database.models.publication.dataset_publication import (
    DatasetPublication,
)
from ..database.models.publication.dataset_publication_component import (
    DatasetPublicationComponent,
)
from .canonical_release_repository import SqlAlchemyCanonicalReleaseRepository
from .legacy_canonical_release_bridge import publish_legacy_snapshot
from .source_batch import record_source_observation

_DATASET = "equity.master.catalog"
_CN_A_DATASET = "equity.master.cn-a"
_CN_A_PARTITION = "CN_A_STABLE"


class SqlAlchemyEquityMasterRepository(EquityMasterRepository):
    """发布完整目录快照，并维护仅供兼容展示的身份锚 current projection。"""

    def __init__(self, database: DatabaseClient) -> None:
        """使用服务自有数据库引擎，避免应用层接触 SQLAlchemy 细节。"""
        self._database = database
        self._release_repository = SqlAlchemyCanonicalReleaseRepository(database)

    def publish_catalog(
        self,
        *,
        exchange: Exchange,
        target_date: date,
        entries: tuple[EquityCatalogEntry, ...],
        provider_id: str,
        source_payload_sha256: str,
        raw_uri: str,
        observed_at: datetime,
        upstream_source: str | None,
        adapter_version: str,
        schema_fingerprint: str,
    ) -> PublishedEquityCatalog:
        """在单一事务中保存完整快照、确认占位身份并推进交易所版本。"""
        if not entries:
            raise ValueError("catalog entries must not be empty")
        if any(entry.identifier.exchange is not exchange for entry in entries):
            raise ValueError("catalog entry exchange must match publication exchange")
        now = datetime.now(UTC)
        business_hash = _catalog_business_hash(entries)
        with self._database.transaction() as connection:
            previous_catalog = self._latest_catalog(connection, exchange)
            self._validate_catalog_completeness(previous_catalog, current_count=len(entries))
            source_batch_id = record_source_observation(
                connection,
                provider_id=provider_id,
                capability=_DATASET,
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
                connection,
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
                changed, security_id = self._publish_entry(
                    connection,
                    entry=entry,
                    target_date=target_date,
                    source_batch_id=source_batch_id,
                    now=now,
                )
                self._insert_snapshot_member(
                    connection,
                    snapshot_id=snapshot_id,
                    ordinal=ordinal,
                    entry=entry,
                    target_date=target_date,
                    security_id=security_id,
                )
                if changed:
                    inserted_count += 1
                else:
                    unchanged_count += 1
            data_version = self._publish(
                connection,
                exchange=exchange,
                snapshot_id=snapshot_id,
                source_batch_id=source_batch_id,
                effective_as_of=target_date,
                published_at=now,
            )
            self._reconcile_presence_anomalies(
                connection,
                exchange=exchange,
                snapshot_id=snapshot_id,
                target_date=target_date,
                now=now,
            )
        return PublishedEquityCatalog(
            snapshot_id=snapshot_id,
            data_version=data_version,
            inserted_count=inserted_count,
            unchanged_count=unchanged_count,
        )

    def publish_cn_a_aggregate(self) -> PublishedCnAAggregate:
        """以当前三所交易所 child version 原子构成稳定全市场发布。"""
        published_at = datetime.now(UTC)
        with self._database.transaction() as connection:
            rows = (
                connection.execute(
                    select(
                        DatasetPublication.partition_key,
                        DatasetPublication.data_version,
                        DatasetPublication.release_id,
                        DatasetPublication.effective_as_of,
                        DatasetPublication.knowledge_cutoff,
                    )
                    .where(
                        DatasetPublication.dataset == _DATASET,
                        DatasetPublication.partition_key.in_(
                            [exchange.value for exchange in Exchange]
                        ),
                        DatasetPublication.superseded_at.is_(None),
                    )
                    .order_by(DatasetPublication.partition_key)
                )
                .mappings()
                .all()
            )
            if {str(row["partition_key"]) for row in rows} != {
                exchange.value for exchange in Exchange
            }:
                raise ValueError("all exchange catalog publications are required")
            if len({row["effective_as_of"] for row in rows}) != 1:
                raise ValueError("all exchange catalog publications must share a target date")
            if any(row["release_id"] is None for row in rows):
                raise ValueError("all exchange catalog publications require canonical releases")
            components = tuple(
                (str(row["partition_key"]), UUID(str(row["data_version"]))) for row in rows
            )
            effective_as_of = min(row["effective_as_of"] for row in rows)
            source_rows = (
                connection.execute(
                    select(
                        DatasetPublication.partition_key,
                        DatasetPublication.data_version,
                        CanonicalRecordLineage.source_batch_id,
                        SourceBatch.created_at.label("source_created_at"),
                    )
                    .distinct()
                    .join(
                        CanonicalRecordLineage,
                        CanonicalRecordLineage.release_id == DatasetPublication.release_id,
                    )
                    .join(
                        SourceBatch,
                        SourceBatch.source_batch_id == CanonicalRecordLineage.source_batch_id,
                    )
                    .where(
                        DatasetPublication.release_id.in_(
                            tuple(UUID(str(row["release_id"])) for row in rows)
                        )
                    )
                    .order_by(
                        DatasetPublication.partition_key,
                        DatasetPublication.data_version,
                        SourceBatch.created_at,
                        CanonicalRecordLineage.source_batch_id,
                    )
                )
                .mappings()
                .all()
            )
            records = _cn_a_aggregate_records(
                components=components,
                source_rows=source_rows,
            )

            def write_publication(
                session: Session,
                publication_id: UUID,
                data_version: UUID,
                release_id: UUID,
            ) -> None:
                """把三所 child dataVersion 和 aggregate release/publication 原子绑定。"""
                del data_version, release_id
                session.execute(
                    insert(DatasetPublicationComponent).values(
                        [
                            {
                                "aggregate_publication_id": publication_id,
                                "component_partition_key": partition_key,
                                "component_data_version": component_data_version,
                            }
                            for partition_key, component_data_version in components
                        ]
                    )
                )

            published = publish_legacy_snapshot(
                connection,
                release_repository=self._release_repository,
                dataset_code=_CN_A_DATASET,
                partition_key=_CN_A_PARTITION,
                domain="equity",
                grain="CN A stable aggregate + exchange catalog release manifest",
                semantic_family="derived-equity-reference",
                mapping_version="equity-master-cn-a-aggregate-release-v1",
                source_batch_id=records[0].source_batch_id,
                records=records,
                fact_min=effective_as_of,
                fact_max=effective_as_of,
                now=published_at,
                publication_effective_as_of=effective_as_of,
                write_publication=write_publication,
                # 三所 child catalog 已各自代表一个控制面分区；全市场 manifest 不是第四分区。
                record_fenced_progress=False,
            )
            _record_fenced_aggregate_checkpoint(published.data_version)
        return PublishedCnAAggregate(
            data_version=published.data_version,
            published_at=published.published_at,
        )

    def _latest_catalog(self, connection: Session, exchange: Exchange) -> Mapping[Any, Any] | None:
        """读取上一份稳定目录的哈希和行数，供版本与完整性门共用。"""
        row = (
            connection.execute(
                select(EquityMasterSnapshot.business_sha256, EquityMasterSnapshot.row_count)
                .where(
                    EquityMasterSnapshot.exchange == exchange.value,
                    EquityMasterSnapshot.snapshot_kind == "CATALOG",
                    EquityMasterSnapshot.quality_status == "passed",
                )
                .order_by(
                    EquityMasterSnapshot.observed_at.desc(), EquityMasterSnapshot.snapshot_id.desc()
                )
                .limit(1)
            )
            .mappings()
            .one_or_none()
        )
        return row

    def _validate_catalog_completeness(
        self,
        previous_catalog: Mapping[Any, Any] | None,
        *,
        current_count: int,
    ) -> None:
        """阻断相对稳定基线缩减超过百分之一的伪完整目录。"""
        if previous_catalog is None or previous_catalog.get("row_count") is None:
            return
        previous_count = int(previous_catalog["row_count"])
        # 用整数比较避免浮点误差：恰好下降 1% 可观察但不触发“超过 1%”硬门。
        if current_count * 100 < previous_count * 99:
            raise EquityCatalogCompletenessError(
                "equity catalog row count decreased by more than one percent "
                f"({previous_count} -> {current_count})"
            )

    def _reconcile_presence_anomalies(
        self,
        connection: Session,
        *,
        exchange: Exchange,
        snapshot_id: UUID,
        target_date: date,
        now: datetime,
    ) -> None:
        """记录目录缺席观测并关闭恢复条目，生命周期状态始终由显式证据维护。"""
        # `ON CONFLICT` 的目标行不是普通 SELECT 外层，显式列引用避免 SQLAlchemy
        # 把整张 anomaly 表加入标量子查询而在多条开放异常时返回多行。
        prior_snapshot = EquityMasterSnapshot.__table__.alias("prior_missing_snapshot")
        prior_snapshot_date = (
            select(prior_snapshot.c.target_date)
            .where(
                prior_snapshot.c.snapshot_id
                == literal_column("equity_presence_anomaly.last_missing_snapshot_id")
            )
            .scalar_subquery()
        )
        missing_identifiers = (
            select(
                # UUID 必须由 PostgreSQL 为 SELECT 的每个缺席证券逐行生成。
                func.gen_random_uuid(),
                EquityIdentifierVersion.security_id,
                EquityIdentifierVersion.exchange,
                EquityIdentifierVersion.symbol,
                literal(snapshot_id),
                literal(snapshot_id),
                literal(1),
                literal("open"),
                literal(None),
            )
            .select_from(
                EquityIdentifierVersion.__table__.join(
                    EquityListingStatusVersion.__table__,
                    EquityListingStatusVersion.security_id == EquityIdentifierVersion.security_id,
                )
            )
            .where(
                EquityIdentifierVersion.exchange == exchange.value,
                EquityIdentifierVersion.identity_state == "CONFIRMED",
                EquityIdentifierVersion.effective_range.contains(target_date),
                EquityIdentifierVersion.knowledge_range.contains(now),
                EquityListingStatusVersion.status == "LISTED",
                EquityListingStatusVersion.effective_range.contains(target_date),
                EquityListingStatusVersion.knowledge_range.contains(now),
                ~exists(
                    select(EquityMasterSnapshotMember.snapshot_id).where(
                        EquityMasterSnapshotMember.snapshot_id == snapshot_id,
                        EquityMasterSnapshotMember.security_id
                        == EquityIdentifierVersion.security_id,
                    )
                ),
            )
        )
        insert_missing = postgresql_insert(EquityPresenceAnomaly).from_select(
            [
                EquityPresenceAnomaly.anomaly_id,
                EquityPresenceAnomaly.security_id,
                EquityPresenceAnomaly.exchange,
                EquityPresenceAnomaly.symbol,
                EquityPresenceAnomaly.first_missing_snapshot_id,
                EquityPresenceAnomaly.last_missing_snapshot_id,
                EquityPresenceAnomaly.consecutive_count,
                EquityPresenceAnomaly.status,
                EquityPresenceAnomaly.resolved_at,
            ],
            missing_identifiers,
        )
        connection.execute(
            insert_missing.on_conflict_do_update(
                index_elements=[EquityPresenceAnomaly.security_id],
                index_where=EquityPresenceAnomaly.status == "open",
                set_={
                    "last_missing_snapshot_id": insert_missing.excluded.last_missing_snapshot_id,
                    "consecutive_count": case(
                        (
                            prior_snapshot_date == target_date,
                            EquityPresenceAnomaly.consecutive_count,
                        ),
                        else_=EquityPresenceAnomaly.consecutive_count + 1,
                    ),
                },
            )
        )
        connection.execute(
            update(EquityPresenceAnomaly)
            .where(
                EquityMasterSnapshotMember.snapshot_id == snapshot_id,
                EquityMasterSnapshotMember.security_id == EquityPresenceAnomaly.security_id,
                EquityPresenceAnomaly.exchange == exchange.value,
                EquityPresenceAnomaly.status == "open",
            )
            .values(status="resolved", resolved_at=now)
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
        """写入一次完整且已通过结构门的目录快照外壳。"""
        connection.execute(
            insert(EquityMasterSnapshot).values(
                snapshot_id=snapshot_id,
                exchange=exchange.value,
                snapshot_kind="CATALOG",
                target_date=target_date,
                source_batch_id=source_batch_id,
                observed_at=observed_at,
                row_count=row_count,
                schema_fingerprint=schema_fingerprint,
                completeness="COMPLETE",
                quality_status="passed",
                business_sha256=business_hash,
            )
        )

    def _publish_entry(
        self,
        connection: Session,
        *,
        entry: EquityCatalogEntry,
        target_date: date,
        source_batch_id: UUID,
        now: datetime,
    ) -> tuple[bool, int]:
        """确认一条目录身份，并只为身份或名称实际变化追加版本。"""
        current = self._current_identifier(connection, entry)
        if current is None:
            security_id = self._create_confirmed_instrument(
                connection,
                entry=entry,
                target_date=target_date,
                source_batch_id=source_batch_id,
                now=now,
            )
            return True, security_id
        security_id = int(current["security_id"])
        if str(current["identity_state"]) == "PENDING":
            self._confirm_pending_identifier(
                connection,
                security_id=security_id,
                entry=entry,
                target_date=target_date,
                source_batch_id=source_batch_id,
                now=now,
            )
            return True, security_id
        changed = self._append_name_if_changed(
            connection,
            security_id=security_id,
            entry=entry,
            target_date=target_date,
            source_batch_id=source_batch_id,
            now=now,
        )
        if changed:
            connection.execute(
                update(EquityInstrument)
                .where(EquityInstrument.security_id == security_id)
                .values(name=entry.name, updated_at=now)
            )
        return changed, security_id

    def _current_identifier(
        self, connection: Session, entry: EquityCatalogEntry
    ) -> Mapping[Any, Any] | None:
        """读取当前知识下的开放标识；PENDING 也是可被目录确认的稳定锚。"""
        return (
            connection.execute(
                select(
                    EquityIdentifierVersion.security_id, EquityIdentifierVersion.identity_state
                ).where(
                    EquityIdentifierVersion.exchange == entry.identifier.exchange.value,
                    EquityIdentifierVersion.symbol == entry.identifier.symbol,
                    EquityIdentifierVersion.effective_to.is_(None),
                    EquityIdentifierVersion.known_to.is_(None),
                )
            )
            .mappings()
            .one_or_none()
        )

    def _create_confirmed_instrument(
        self,
        connection: Session,
        *,
        entry: EquityCatalogEntry,
        target_date: date,
        source_batch_id: UUID,
        now: datetime,
    ) -> int:
        """为目录首次发现代码创建确认身份、名称与 LISTED 生命周期事实。"""
        instrument_id = uuid4()
        security_id = int(
            connection.execute(
                insert(EquityInstrument)
                .values(
                    instrument_id=instrument_id,
                    exchange=entry.identifier.exchange.value,
                    symbol=entry.identifier.symbol,
                    name=entry.name,
                    listing_status="LISTED",
                    master_confirmed_at=now,
                    current_master_version=uuid4(),
                    created_at=now,
                    updated_at=now,
                )
                .returning(EquityInstrument.security_id)
            ).scalar_one()
        )
        identifier_version_id = uuid4()
        self._insert_confirmed_identity(
            connection,
            version_id=identifier_version_id,
            security_id=security_id,
            entry=entry,
            target_date=target_date,
            source_batch_id=source_batch_id,
            now=now,
        )
        self._insert_name_version(
            connection,
            security_id=security_id,
            entry=entry,
            target_date=target_date,
            source_batch_id=source_batch_id,
            now=now,
        )
        self._insert_listed_status(
            connection,
            security_id=security_id,
            entry=entry,
            target_date=target_date,
            source_batch_id=source_batch_id,
            now=now,
        )
        connection.execute(
            update(EquityInstrument)
            .where(EquityInstrument.security_id == security_id)
            .values(current_master_version=identifier_version_id)
        )
        return security_id

    def _confirm_pending_identifier(
        self,
        connection: Session,
        *,
        security_id: int,
        entry: EquityCatalogEntry,
        target_date: date,
        source_batch_id: UUID,
        now: datetime,
    ) -> None:
        """关闭 PENDING 知识版本并追加可发布的确认身份与初始目录事实。"""
        connection.execute(
            update(EquityIdentifierVersion)
            .where(
                EquityIdentifierVersion.security_id == security_id,
                EquityIdentifierVersion.identity_state == "PENDING",
                EquityIdentifierVersion.known_to.is_(None),
            )
            .values(known_to=now)
        )
        version_id = uuid4()
        self._insert_confirmed_identity(
            connection,
            version_id=version_id,
            security_id=security_id,
            entry=entry,
            target_date=target_date,
            source_batch_id=source_batch_id,
            now=now,
        )
        self._insert_name_version(
            connection,
            security_id=security_id,
            entry=entry,
            target_date=target_date,
            source_batch_id=source_batch_id,
            now=now,
        )
        self._insert_listed_status(
            connection,
            security_id=security_id,
            entry=entry,
            target_date=target_date,
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
                current_master_version=version_id,
                updated_at=now,
            )
        )

    def _append_name_if_changed(
        self,
        connection: Session,
        *,
        security_id: int,
        entry: EquityCatalogEntry,
        target_date: date,
        source_batch_id: UUID,
        now: datetime,
    ) -> bool:
        """将目录中的名称变动按观测日追加，目录不具备回填改名生效日的证据。"""
        current = (
            connection.execute(
                select(
                    EquityNameVersion.version_id,
                    EquityNameVersion.name,
                    EquityNameVersion.effective_from,
                )
                .where(
                    EquityNameVersion.security_id == security_id,
                    EquityNameVersion.effective_to.is_(None),
                    EquityNameVersion.known_to.is_(None),
                )
                # 同一代码的并发目录发布必须串行，避免留下两个开放名称版本。
                .with_for_update()
            )
            .mappings()
            .one_or_none()
        )
        if current is not None and str(current["name"]) == entry.name:
            return False
        if current is not None:
            # 同日更正关闭知识时间；跨日改名关闭市场有效期，避免回写旧名称。
            if current["effective_from"] == target_date:
                connection.execute(
                    update(EquityNameVersion)
                    .where(EquityNameVersion.version_id == current["version_id"])
                    .values(known_to=now)
                )
            else:
                if current["effective_from"] > target_date:
                    raise ValueError("catalog target date predates current name fact")
                connection.execute(
                    update(EquityNameVersion)
                    .where(EquityNameVersion.version_id == current["version_id"])
                    .values(effective_to=target_date)
                )
        self._insert_name_version(
            connection,
            security_id=security_id,
            entry=entry,
            target_date=target_date,
            source_batch_id=source_batch_id,
            now=now,
            name_effective_from=target_date,
            name_effective_date_precision="OBSERVATION_DATE",
        )
        return True

    def _insert_confirmed_identity(
        self,
        connection: Session,
        *,
        version_id: UUID,
        security_id: int,
        entry: EquityCatalogEntry,
        target_date: date,
        source_batch_id: UUID,
        now: datetime,
    ) -> None:
        """追加可发布的确认标识版本，使用可靠上市日或观测日而不猜测历史。"""
        effective_from, precision = _effective_date(entry, target_date)
        connection.execute(
            insert(EquityIdentifierVersion).values(
                version_id=version_id,
                security_id=security_id,
                exchange=entry.identifier.exchange.value,
                symbol=entry.identifier.symbol,
                identity_state="CONFIRMED",
                effective_from=effective_from,
                effective_to=None,
                known_from=now,
                known_to=None,
                effective_date_precision=precision,
                source_batch_id=source_batch_id,
                content_sha256=_identity_hash(entry, effective_from, "CONFIRMED"),
            )
        )

    def _insert_name_version(
        self,
        connection: Session,
        *,
        security_id: int,
        entry: EquityCatalogEntry,
        target_date: date,
        source_batch_id: UUID,
        now: datetime,
        name_effective_from: date | None = None,
        name_effective_date_precision: str | None = None,
    ) -> None:
        """追加独立可修订的名称事实，目录改名不得借用证券上市日倒灌历史。"""
        if (name_effective_from is None) != (name_effective_date_precision is None):
            raise ValueError("catalog name effective date and precision must be supplied together")
        if name_effective_from is None:
            effective_from, precision = _effective_date(entry, target_date)
        else:
            effective_from = name_effective_from
            precision = name_effective_date_precision
        connection.execute(
            insert(EquityNameVersion).values(
                version_id=uuid4(),
                security_id=security_id,
                name=entry.name,
                effective_from=effective_from,
                effective_to=None,
                known_from=now,
                known_to=None,
                effective_date_precision=precision,
                source_batch_id=source_batch_id,
                content_sha256=_name_hash(entry, effective_from),
            )
        )

    def _insert_listed_status(
        self,
        connection: Session,
        *,
        security_id: int,
        entry: EquityCatalogEntry,
        target_date: date,
        source_batch_id: UUID,
        now: datetime,
    ) -> None:
        """首次确认时写入 LISTED；目录缺席和普通停牌绝不自动转换生命周期。"""
        effective_from, precision = _effective_date(entry, target_date)
        connection.execute(
            insert(EquityListingStatusVersion).values(
                version_id=uuid4(),
                security_id=security_id,
                status="LISTED",
                listed_on=entry.listed_on,
                delisted_on=None,
                effective_from=effective_from,
                effective_to=None,
                known_from=now,
                known_to=None,
                effective_date_precision=precision,
                evidence_kind="CATALOG",
                source_batch_id=source_batch_id,
                content_sha256=_listing_hash(entry, effective_from),
                correction_approval_reference=None,
            )
        )

    def _insert_snapshot_member(
        self,
        connection: Session,
        *,
        snapshot_id: UUID,
        ordinal: int,
        entry: EquityCatalogEntry,
        target_date: date,
        security_id: int,
    ) -> None:
        """记录完整快照中的解析结果，供差集、质量告警和审计回放读取。"""
        _, precision = _effective_date(entry, target_date)
        connection.execute(
            insert(EquityMasterSnapshotMember).values(
                snapshot_id=snapshot_id,
                row_ordinal=ordinal,
                exchange=entry.identifier.exchange.value,
                symbol=entry.identifier.symbol,
                name=entry.name,
                listed_on=entry.listed_on,
                candidate_status="LISTED",
                candidate_status_date=entry.listed_on,
                effective_date_precision=precision,
                security_id=security_id,
                resolution_status="resolved",
                content_sha256=_entry_hash(entry),
            )
        )

    def _publish(
        self,
        connection: Session,
        *,
        exchange: Exchange,
        snapshot_id: UUID,
        source_batch_id: UUID,
        effective_as_of: date,
        published_at: datetime,
    ) -> UUID:
        """把完整目录快照经统一 release 事务发布为带真实 lineage 的消费者版本。"""
        return self._publish_catalog_release(
            connection,
            exchange=exchange,
            snapshot_id=snapshot_id,
            source_batch_id=source_batch_id,
            effective_as_of=effective_as_of,
            published_at=published_at,
        )

    def _publish_catalog_release(
        self,
        connection: Session,
        *,
        exchange: Exchange,
        snapshot_id: UUID,
        source_batch_id: UUID,
        effective_as_of: date,
        published_at: datetime,
    ) -> UUID:
        """从已落库目录成员构造 release 候选并委托统一消费者可见性事务。"""
        records = _catalog_release_records(
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
            grain="exchange catalog snapshot + confirmed identity/name rows",
            semantic_family="reported-equity-reference",
            mapping_version="equity-master-catalog-release-v1",
            source_batch_id=source_batch_id,
            records=records,
            fact_min=effective_as_of,
            fact_max=effective_as_of,
            now=published_at,
            publication_effective_as_of=effective_as_of,
        )
        return published.data_version


def _catalog_release_records(
    connection: Session,
    *,
    snapshot_id: UUID,
    source_batch_id: UUID,
) -> tuple[CanonicalLineageRecord, ...]:
    """从刚写入的完整目录快照构造每行可复验的 canonical lineage。"""
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
        raise ValueError("catalog snapshot has no members for canonical release")
    transform_hash = hashlib.sha256(b"equity-master-catalog-release-v1").hexdigest()
    return tuple(
        CanonicalLineageRecord(
            record_key_hash=hashlib.sha256(
                f"catalog:{row['exchange']}:{row['symbol']}".encode()
            ).hexdigest(),
            content_hash=bytes(row["content_sha256"]).hex(),
            source_batch_id=source_batch_id,
            transform_hash=transform_hash,
            role="primary",
        )
        for row in rows
    )


def _cn_a_aggregate_records(
    *,
    components: tuple[tuple[str, UUID], ...],
    source_rows: Sequence[Mapping[Any, Any]],
) -> tuple[CanonicalLineageRecord, ...]:
    """将每个 child publication 归约为一条不受重复来源证据影响的聚合记录。"""
    expected_components = set(components)
    selected_sources: dict[tuple[str, UUID], tuple[datetime, UUID]] = {}
    for row in source_rows:
        component = (str(row["partition_key"]), UUID(str(row["data_version"])))
        if component not in expected_components:
            raise ValueError("aggregate source lineage does not match a current catalog component")
        source_created_at = row["source_created_at"]
        if not isinstance(source_created_at, datetime) or source_created_at.tzinfo is None:
            raise ValueError("aggregate source lineage requires a timezone-aware creation time")
        source_batch_id = UUID(str(row["source_batch_id"]))
        candidate = (source_created_at, source_batch_id)
        # child release 可累积相同目录内容的多次来源证据；选择最早登记证据只用于
        # 聚合 manifest 的确定性 lineage，完整证据仍可经 child release 审计。
        if component not in selected_sources or candidate < selected_sources[component]:
            selected_sources[component] = candidate
    if set(selected_sources) != expected_components:
        raise ValueError("all exchange catalog releases require canonical source lineage")
    transform_hash = hashlib.sha256(b"equity-master-cn-a-aggregate-release-v1").hexdigest()
    return tuple(
        CanonicalLineageRecord(
            # record key 和内容仅冻结 component 身份；新来源证据不能改变 aggregate release。
            record_key_hash=hashlib.sha256(
                f"cn-a-component:{partition_key}:{data_version}".encode()
            ).hexdigest(),
            content_hash=hashlib.sha256(f"{partition_key}:{data_version}".encode()).hexdigest(),
            source_batch_id=selected_sources[(partition_key, data_version)][1],
            transform_hash=transform_hash,
            role="input",
        )
        for partition_key, data_version in components
    )


def _record_fenced_aggregate_checkpoint(data_version: UUID) -> None:
    """把全市场身份聚合版本交给当前 fenced 控制面同事务落账。"""
    execution = current_fenced_execution()
    if execution is not None:
        execution.record_checkpoint(kind="data-version", position=str(data_version))


def _effective_date(entry: EquityCatalogEntry, target_date: date) -> tuple[date, str]:
    """将已知上市日与仅观察到的目录日期区分为不同时间精度。"""
    if entry.listed_on is not None:
        return entry.listed_on, "OFFICIAL_DATE"
    return target_date, "OBSERVATION_DATE"


def _catalog_business_hash(entries: tuple[EquityCatalogEntry, ...]) -> bytes:
    """对排序后的标准目录业务字段哈希，排除每次均变化的来源观测元数据。"""
    return hashlib.sha256(
        json.dumps(
            [
                {
                    "exchange": entry.identifier.exchange.value,
                    "symbol": entry.identifier.symbol,
                    "name": entry.name,
                    "listedOn": None if entry.listed_on is None else entry.listed_on.isoformat(),
                }
                for entry in entries
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
    ).digest()


def _identity_hash(entry: EquityCatalogEntry, effective_from: date, state: str) -> bytes:
    """生成标识版本的业务哈希，明确身份状态和市场有效日期。"""
    return _hash_json(
        {
            "exchange": entry.identifier.exchange.value,
            "symbol": entry.identifier.symbol,
            "effectiveFrom": effective_from.isoformat(),
            "state": state,
        }
    )


def _name_hash(entry: EquityCatalogEntry, effective_from: date) -> bytes:
    """生成名称版本的业务哈希，避免快照重放产生伪修订。"""
    return _hash_json({"name": entry.name, "effectiveFrom": effective_from.isoformat()})


def _listing_hash(entry: EquityCatalogEntry, effective_from: date) -> bytes:
    """生成初始 LISTED 生命周期事实的业务哈希。"""
    return _hash_json(
        {
            "status": "LISTED",
            "listedOn": None if entry.listed_on is None else entry.listed_on.isoformat(),
            "effectiveFrom": effective_from.isoformat(),
        }
    )


def _entry_hash(entry: EquityCatalogEntry) -> bytes:
    """生成快照成员业务哈希，供审计比较而不是身份推断。"""
    return _hash_json(
        {
            "exchange": entry.identifier.exchange.value,
            "symbol": entry.identifier.symbol,
            "name": entry.name,
            "listedOn": None if entry.listed_on is None else entry.listed_on.isoformat(),
        }
    )


def _hash_json(value: dict[str, object]) -> bytes:
    """以稳定 UTF-8 JSON 序列化标准业务值并返回 SHA-256 原始字节。"""
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    ).digest()
