"""申万 taxonomy、闭包、估值、发布与 checkpoint 的 SQLAlchemy 仓储。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, cast
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import and_, desc, func, insert, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from service_data_sync.application.ports.sw_sector import (
    SwCapability,
    SwCheckpoint,
    SwPublication,
    SwPublishedCapability,
    SwPublishResult,
    SwSectorRepository,
    SwSourceObservation,
    SwStoredNode,
    SwStoredValuation,
)
from service_data_sync.domain.sw_sector import (
    SwIndustryLevel,
    SwIndustryNode,
    SwIndustrySnapshot,
    SwIndustryValuation,
    SwMethodology,
)
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.database.models.publication.dataset_publication import (
    DatasetPublication,
)
from service_data_sync.infrastructure.database.models.sector.sw import (
    SwSectorValuationRevision,
)
from service_data_sync.infrastructure.database.models.sector.sw.sw_sector_closure import (
    SwSectorClosure,
)
from service_data_sync.infrastructure.database.models.sector.sw.sw_sector_methodology import (
    SwSectorMethodology,
)
from service_data_sync.infrastructure.database.models.sector.sw.sw_sector_node_revision import (
    SwSectorNodeRevision,
)
from service_data_sync.infrastructure.database.models.sector.sw.sw_sector_publication import (
    SwSectorPublication,
)
from service_data_sync.infrastructure.database.models.sector.sw.sw_sector_quality_result import (
    SwSectorQualityResult,
)
from service_data_sync.infrastructure.database.models.sector.sw.sw_sector_sync_checkpoint import (
    SwSectorSyncCheckpoint,
)
from service_data_sync.infrastructure.persistence.source_batch import record_source_observation

_RAW_CAPABILITY = "sector.sw.snapshot.raw"
_TAXONOMY_CAPABILITY: SwCapability = "sector.sw.taxonomy"
_VALUATION_CAPABILITY: SwCapability = "sector.sw.valuation"
_PARTITION_PREFIX = "sw.industry"


class SqlAlchemySwSectorRepository(SwSectorRepository):
    """以按日完整快照维护申万双时间修订和消费者发布。"""

    def __init__(self, database: DatabaseClient) -> None:
        """保存服务自有短生命周期 Session 工厂。"""
        self._database = database

    def publish_snapshot(
        self, *, snapshot: SwIndustrySnapshot, source: SwSourceObservation
    ) -> SwPublishResult:
        """原子写入来源、修订、闭包、质量、双发布和恢复 checkpoint。"""
        if source.capability != _RAW_CAPABILITY:
            raise ValueError("SW source capability is invalid")
        if source.observed_at.tzinfo is None:
            raise ValueError("SW observed_at must include a timezone")
        now = datetime.now(UTC)
        with self._database.transaction() as connection:
            methodology_id = self._methodology_id(
                connection, methodology=snapshot.methodology, now=now
            )
            source_batch_id = record_source_observation(
                connection,
                provider_id=source.provider_id,
                capability=source.capability,
                source_payload_sha256=source.source_payload_sha256,
                raw_uri=source.raw_uri,
                observed_at=source.observed_at,
                created_at=now,
                upstream_source=source.upstream_source,
                adapter_version=source.adapter_version,
                schema_fingerprint=source.schema_fingerprint,
            )
            node_inserted, node_unchanged = self._write_nodes(
                connection,
                snapshot=snapshot,
                methodology_id=methodology_id,
                source_batch_id=source_batch_id,
                source=source,
                now=now,
            )
            valuation_inserted, valuation_unchanged = self._write_valuations(
                connection,
                snapshot=snapshot,
                methodology_id=methodology_id,
                source_batch_id=source_batch_id,
                source=source,
                now=now,
            )
            taxonomy, taxonomy_created = self._publish_capability(
                connection,
                capability=_TAXONOMY_CAPABILITY,
                snapshot_date=snapshot.snapshot_date,
                methodology_id=methodology_id,
                content_sha256=snapshot.taxonomy_sha256(),
                row_count=len(snapshot.nodes),
                inserted_count=node_inserted,
                unchanged_count=node_unchanged,
                now=now,
            )
            if taxonomy_created:
                connection.execute(
                    insert(SwSectorClosure),
                    [
                        {
                            "data_version": taxonomy.data_version,
                            "ancestor_code": edge.ancestor_code,
                            "descendant_code": edge.descendant_code,
                            "depth": edge.depth,
                        }
                        for edge in snapshot.closure()
                    ],
                )
            valuation, _valuation_created = self._publish_capability(
                connection,
                capability=_VALUATION_CAPABILITY,
                snapshot_date=snapshot.snapshot_date,
                methodology_id=methodology_id,
                content_sha256=snapshot.valuation_sha256(),
                row_count=len(snapshot.valuations),
                inserted_count=valuation_inserted,
                unchanged_count=valuation_unchanged,
                now=now,
            )
            self._write_quality_results(
                connection,
                snapshot=snapshot,
                source_batch_id=source_batch_id,
                taxonomy=taxonomy,
                valuation=valuation,
                now=now,
            )
            self._checkpoint(
                connection,
                snapshot_date=snapshot.snapshot_date,
                source=source,
                taxonomy=taxonomy,
                now=now,
            )
        return SwPublishResult(taxonomy=taxonomy, valuation=valuation)

    def get_checkpoint(self, *, snapshot_date: date) -> SwCheckpoint | None:
        """读取精确观测日最近成功提交的中立 replay 位置和来源血缘。"""
        partition_key = _partition_key(snapshot_date)
        statement = select(
            SwSectorSyncCheckpoint.snapshot_date,
            SwSectorSyncCheckpoint.summary_sha256,
            SwSectorSyncCheckpoint.raw_sha256,
            SwSectorSyncCheckpoint.raw_uri,
            SwSectorSyncCheckpoint.normalized_uri,
            SwSectorSyncCheckpoint.provider_id,
            SwSectorSyncCheckpoint.upstream_source,
            SwSectorSyncCheckpoint.adapter_version,
            SwSectorSyncCheckpoint.schema_fingerprint,
            SwSectorSyncCheckpoint.observed_at,
            SwSectorSyncCheckpoint.last_data_version,
        ).where(
            SwSectorSyncCheckpoint.capability == _RAW_CAPABILITY,
            SwSectorSyncCheckpoint.partition_key == partition_key,
        )
        with self._database.session() as connection:
            row = connection.execute(statement).mappings().one_or_none()
        if row is None:
            return None
        return SwCheckpoint(
            snapshot_date=cast(date, row["snapshot_date"]),
            summary_sha256=str(row["summary_sha256"]),
            raw_sha256=str(row["raw_sha256"]),
            raw_uri=str(row["raw_uri"]),
            normalized_uri=str(row["normalized_uri"]),
            provider_id=str(row["provider_id"]),
            upstream_source=str(row["upstream_source"]),
            adapter_version=str(row["adapter_version"]),
            schema_fingerprint=str(row["schema_fingerprint"]),
            observed_at=cast(datetime, row["observed_at"]),
            last_data_version=UUID(str(row["last_data_version"])),
        )

    def get_publication(
        self, *, capability: SwCapability, snapshot_date: date | None
    ) -> SwPublication | None:
        """读取精确日期或最新日期仍有效的申万消费者发布。"""
        statement = (
            select(
                SwSectorPublication.capability,
                SwSectorPublication.data_version,
                SwSectorPublication.snapshot_date,
                SwSectorPublication.published_at,
                SwSectorPublication.row_count,
                SwSectorPublication.content_sha256,
                DatasetPublication.quality_status,
                SwSectorMethodology.code,
                SwSectorMethodology.version,
                SwSectorMethodology.status,
                SwSectorMethodology.upstream_source,
                SwSectorMethodology.semantic_spec_sha256,
            )
            .join(
                DatasetPublication,
                DatasetPublication.data_version == SwSectorPublication.data_version,
            )
            .join(
                SwSectorMethodology,
                SwSectorMethodology.methodology_id == SwSectorPublication.methodology_id,
            )
            .where(
                SwSectorPublication.capability == capability,
                DatasetPublication.superseded_at.is_(None),
            )
        )
        if snapshot_date is not None:
            statement = statement.where(SwSectorPublication.snapshot_date == snapshot_date)
        statement = statement.order_by(
            desc(SwSectorPublication.snapshot_date), desc(SwSectorPublication.published_at)
        ).limit(1)
        with self._database.session() as connection:
            row = connection.execute(statement).mappings().one_or_none()
        return None if row is None else _publication(row)

    def list_nodes(
        self,
        *,
        snapshot_date: date,
        level: int | None,
        parent_code: str | None,
        after_level: int | None,
        after_code: str | None,
        limit: int,
    ) -> Sequence[SwStoredNode]:
        """按层级与代码稳定分页读取指定观测日当前知识节点。"""
        if not 1 <= limit <= 501:
            raise ValueError("SW node limit must be from 1 to 501")
        if (after_level is None) != (after_code is None):
            raise ValueError("SW node cursor level and code must be supplied together")
        statement = select(
            SwSectorNodeRevision.sector_code,
            SwSectorNodeRevision.name,
            SwSectorNodeRevision.level,
            SwSectorNodeRevision.parent_code,
            SwSectorNodeRevision.component_count,
            SwSectorNodeRevision.revision,
        ).where(
            SwSectorNodeRevision.snapshot_date == snapshot_date,
            SwSectorNodeRevision.known_to.is_(None),
            SwSectorNodeRevision.quality_status.in_(("passed", "warned")),
        )
        if level is not None:
            statement = statement.where(SwSectorNodeRevision.level == level)
        if parent_code is not None:
            statement = statement.where(SwSectorNodeRevision.parent_code == parent_code)
        if after_level is not None and after_code is not None:
            statement = statement.where(
                or_(
                    SwSectorNodeRevision.level > after_level,
                    and_(
                        SwSectorNodeRevision.level == after_level,
                        SwSectorNodeRevision.sector_code > after_code,
                    ),
                )
            )
        statement = statement.order_by(
            SwSectorNodeRevision.level, SwSectorNodeRevision.sector_code
        ).limit(limit)
        with self._database.session() as connection:
            rows = connection.execute(statement).mappings().all()
        return tuple(_stored_node(row) for row in rows)

    def get_node(self, *, snapshot_date: date, code: str) -> SwStoredNode | None:
        """读取指定观测日一个通过质量门的当前知识节点。"""
        statement = select(
            SwSectorNodeRevision.sector_code,
            SwSectorNodeRevision.name,
            SwSectorNodeRevision.level,
            SwSectorNodeRevision.parent_code,
            SwSectorNodeRevision.component_count,
            SwSectorNodeRevision.revision,
        ).where(
            SwSectorNodeRevision.snapshot_date == snapshot_date,
            SwSectorNodeRevision.sector_code == code,
            SwSectorNodeRevision.known_to.is_(None),
            SwSectorNodeRevision.quality_status.in_(("passed", "warned")),
        )
        with self._database.session() as connection:
            row = connection.execute(statement).mappings().one_or_none()
        return None if row is None else _stored_node(row)

    def list_ancestors(
        self, *, data_version: UUID, snapshot_date: date, descendant_code: str
    ) -> Sequence[SwStoredNode]:
        """从冻结闭包按根到直接父级读取指定节点祖先。"""
        statement = (
            select(
                SwSectorNodeRevision.sector_code,
                SwSectorNodeRevision.name,
                SwSectorNodeRevision.level,
                SwSectorNodeRevision.parent_code,
                SwSectorNodeRevision.component_count,
                SwSectorNodeRevision.revision,
            )
            .join(
                SwSectorClosure,
                SwSectorClosure.ancestor_code == SwSectorNodeRevision.sector_code,
            )
            .where(
                SwSectorClosure.data_version == data_version,
                SwSectorClosure.descendant_code == descendant_code,
                SwSectorClosure.depth > 0,
                SwSectorNodeRevision.snapshot_date == snapshot_date,
                SwSectorNodeRevision.known_to.is_(None),
            )
            .order_by(desc(SwSectorClosure.depth))
        )
        with self._database.session() as connection:
            rows = connection.execute(statement).mappings().all()
        return tuple(_stored_node(row) for row in rows)

    def list_valuations(
        self,
        *,
        snapshot_date: date,
        level: int | None,
        after_code: str | None,
        limit: int,
    ) -> Sequence[SwStoredValuation]:
        """按代码分页读取指定观测日通过质量门的估值和节点身份。"""
        if not 1 <= limit <= 501:
            raise ValueError("SW valuation limit must be from 1 to 501")
        statement = (
            select(
                SwSectorNodeRevision.sector_code,
                SwSectorNodeRevision.name,
                SwSectorNodeRevision.level,
                SwSectorNodeRevision.parent_code,
                SwSectorNodeRevision.component_count,
                SwSectorNodeRevision.revision.label("node_revision"),
                SwSectorValuationRevision.static_pe,
                SwSectorValuationRevision.ttm_pe,
                SwSectorValuationRevision.pb,
                SwSectorValuationRevision.dividend_yield_ratio,
                SwSectorValuationRevision.revision.label("valuation_revision"),
            )
            .join(
                SwSectorValuationRevision,
                and_(
                    SwSectorValuationRevision.snapshot_date == SwSectorNodeRevision.snapshot_date,
                    SwSectorValuationRevision.sector_code == SwSectorNodeRevision.sector_code,
                    SwSectorValuationRevision.known_to.is_(None),
                ),
            )
            .where(
                SwSectorNodeRevision.snapshot_date == snapshot_date,
                SwSectorNodeRevision.known_to.is_(None),
                SwSectorNodeRevision.quality_status.in_(("passed", "warned")),
                SwSectorValuationRevision.quality_status.in_(("passed", "warned")),
            )
        )
        if level is not None:
            statement = statement.where(SwSectorNodeRevision.level == level)
        if after_code is not None:
            statement = statement.where(SwSectorNodeRevision.sector_code > after_code)
        statement = statement.order_by(SwSectorNodeRevision.sector_code).limit(limit)
        with self._database.session() as connection:
            rows = connection.execute(statement).mappings().all()
        return tuple(_stored_valuation(row, snapshot_date=snapshot_date) for row in rows)

    def _methodology_id(
        self, connection: Session, *, methodology: SwMethodology, now: datetime
    ) -> UUID:
        """取得或登记与载荷语义摘要完全一致的方法学版本。"""
        existing = (
            connection.execute(
                select(
                    SwSectorMethodology.methodology_id,
                    SwSectorMethodology.status,
                    SwSectorMethodology.upstream_source,
                    SwSectorMethodology.semantic_spec_sha256,
                ).where(
                    SwSectorMethodology.code == methodology.code,
                    SwSectorMethodology.version == methodology.version,
                )
            )
            .mappings()
            .one_or_none()
        )
        if existing is not None:
            if (
                str(existing["status"]) != methodology.status
                or str(existing["upstream_source"]) != methodology.upstream_source
                or str(existing["semantic_spec_sha256"]) != methodology.semantic_spec_sha256
            ):
                raise ValueError("SW methodology version conflicts with stored semantics")
            return UUID(str(existing["methodology_id"]))
        methodology_id = uuid5(
            NAMESPACE_URL, f"quant-v2:sw-methodology:{methodology.code}:{methodology.version}"
        )
        connection.execute(
            insert(SwSectorMethodology).values(
                methodology_id=methodology_id,
                code=methodology.code,
                version=methodology.version,
                status=methodology.status,
                upstream_source=methodology.upstream_source,
                semantic_spec_sha256=methodology.semantic_spec_sha256,
                created_at=now,
            )
        )
        return methodology_id

    def _write_nodes(
        self,
        connection: Session,
        *,
        snapshot: SwIndustrySnapshot,
        methodology_id: UUID,
        source_batch_id: UUID,
        source: SwSourceObservation,
        now: datetime,
    ) -> tuple[int, int]:
        """只为同日内容变化节点闭合知识区间并追加修订。"""
        inserted_count = 0
        unchanged_count = 0
        incoming_codes = {node.code for node in snapshot.nodes}
        # 每次输入都是完整快照；未闭合已消失节点会让新 publication 混入旧 taxonomy。
        connection.execute(
            update(SwSectorNodeRevision)
            .where(
                SwSectorNodeRevision.snapshot_date == snapshot.snapshot_date,
                SwSectorNodeRevision.sector_code.not_in(incoming_codes),
                SwSectorNodeRevision.known_to.is_(None),
            )
            .values(known_to=now)
        )
        for node in snapshot.nodes:
            content_sha256 = _node_sha256(node, methodology_id=methodology_id)
            current = (
                connection.execute(
                    select(
                        SwSectorNodeRevision.revision,
                        SwSectorNodeRevision.content_sha256,
                    ).where(
                        SwSectorNodeRevision.snapshot_date == snapshot.snapshot_date,
                        SwSectorNodeRevision.sector_code == node.code,
                        SwSectorNodeRevision.known_to.is_(None),
                    )
                )
                .mappings()
                .one_or_none()
            )
            if current is not None and str(current["content_sha256"]) == content_sha256:
                unchanged_count += 1
                continue
            revision = self._next_revision(
                connection,
                SwSectorNodeRevision.revision,
                SwSectorNodeRevision.snapshot_date == snapshot.snapshot_date,
                SwSectorNodeRevision.sector_code == node.code,
            )
            if current is not None:
                connection.execute(
                    update(SwSectorNodeRevision)
                    .where(
                        SwSectorNodeRevision.snapshot_date == snapshot.snapshot_date,
                        SwSectorNodeRevision.sector_code == node.code,
                        SwSectorNodeRevision.known_to.is_(None),
                    )
                    .values(known_to=now)
                )
            connection.execute(
                insert(SwSectorNodeRevision).values(
                    node_revision_id=uuid4(),
                    node_id=_node_id(node.code),
                    snapshot_date=snapshot.snapshot_date,
                    sector_code=node.code,
                    name=node.name,
                    level=node.level.value,
                    parent_code=node.parent_code,
                    component_count=node.component_count,
                    methodology_id=methodology_id,
                    revision=revision,
                    known_from=now,
                    known_to=None,
                    observed_at=source.observed_at,
                    source_batch_id=source_batch_id,
                    content_sha256=content_sha256,
                    quality_status="passed",
                    created_at=now,
                )
            )
            inserted_count += 1
        return inserted_count, unchanged_count

    def _write_valuations(
        self,
        connection: Session,
        *,
        snapshot: SwIndustrySnapshot,
        methodology_id: UUID,
        source_batch_id: UUID,
        source: SwSourceObservation,
        now: datetime,
    ) -> tuple[int, int]:
        """只为同日估值变化关闭当前知识行并追加新 revision。"""
        inserted_count = 0
        unchanged_count = 0
        incoming_codes = {valuation.code for valuation in snapshot.valuations}
        # taxonomy 与估值必须保持一一覆盖，完整快照缺失项不得继续对消费者可见。
        connection.execute(
            update(SwSectorValuationRevision)
            .where(
                SwSectorValuationRevision.snapshot_date == snapshot.snapshot_date,
                or_(
                    SwSectorValuationRevision.methodology_id != methodology_id,
                    SwSectorValuationRevision.sector_code.not_in(incoming_codes),
                ),
                SwSectorValuationRevision.known_to.is_(None),
            )
            .values(known_to=now)
        )
        for valuation in snapshot.valuations:
            content_sha256 = _valuation_sha256(
                valuation,
                methodology_id=methodology_id,
            )
            current = (
                connection.execute(
                    select(
                        SwSectorValuationRevision.revision,
                        SwSectorValuationRevision.content_sha256,
                    ).where(
                        SwSectorValuationRevision.snapshot_date == valuation.snapshot_date,
                        SwSectorValuationRevision.sector_code == valuation.code,
                        SwSectorValuationRevision.methodology_id == methodology_id,
                        SwSectorValuationRevision.known_to.is_(None),
                    )
                )
                .mappings()
                .one_or_none()
            )
            if current is not None and str(current["content_sha256"]) == content_sha256:
                unchanged_count += 1
                continue
            revision = self._next_revision(
                connection,
                SwSectorValuationRevision.revision,
                SwSectorValuationRevision.snapshot_date == valuation.snapshot_date,
                SwSectorValuationRevision.sector_code == valuation.code,
                SwSectorValuationRevision.methodology_id == methodology_id,
            )
            if current is not None:
                connection.execute(
                    update(SwSectorValuationRevision)
                    .where(
                        SwSectorValuationRevision.snapshot_date == valuation.snapshot_date,
                        SwSectorValuationRevision.sector_code == valuation.code,
                        SwSectorValuationRevision.methodology_id == methodology_id,
                        SwSectorValuationRevision.known_to.is_(None),
                    )
                    .values(known_to=now)
                )
            connection.execute(
                insert(SwSectorValuationRevision).values(
                    valuation_revision_id=uuid4(),
                    snapshot_date=valuation.snapshot_date,
                    node_id=_node_id(valuation.code),
                    sector_code=valuation.code,
                    methodology_id=methodology_id,
                    revision=revision,
                    static_pe=valuation.static_pe,
                    ttm_pe=valuation.ttm_pe,
                    pb=valuation.pb,
                    dividend_yield_ratio=valuation.dividend_yield_ratio,
                    finality="PROVIDER_OBSERVATION",
                    known_from=now,
                    known_to=None,
                    observed_at=source.observed_at,
                    source_batch_id=source_batch_id,
                    content_sha256=content_sha256,
                    quality_status="passed",
                    created_at=now,
                )
            )
            inserted_count += 1
        return inserted_count, unchanged_count

    def _publish_capability(
        self,
        connection: Session,
        *,
        capability: SwCapability,
        snapshot_date: date,
        methodology_id: UUID,
        content_sha256: str,
        row_count: int,
        inserted_count: int,
        unchanged_count: int,
        now: datetime,
    ) -> tuple[SwPublishedCapability, bool]:
        """按 capability 与观测日切换通用发布，并在内容未变时复用版本。"""
        partition_key = _partition_key(snapshot_date)
        existing = (
            connection.execute(
                select(
                    SwSectorPublication.data_version,
                    SwSectorPublication.published_at,
                    SwSectorPublication.row_count,
                    SwSectorPublication.content_sha256,
                )
                .join(
                    DatasetPublication,
                    DatasetPublication.data_version == SwSectorPublication.data_version,
                )
                .where(
                    SwSectorPublication.capability == capability,
                    SwSectorPublication.snapshot_date == snapshot_date,
                    DatasetPublication.dataset == capability,
                    DatasetPublication.partition_key == partition_key,
                    DatasetPublication.superseded_at.is_(None),
                )
            )
            .mappings()
            .one_or_none()
        )
        if existing is not None and str(existing["content_sha256"]) == content_sha256:
            return (
                SwPublishedCapability(
                    capability=capability,
                    data_version=UUID(str(existing["data_version"])),
                    snapshot_date=snapshot_date,
                    published_at=cast(datetime, existing["published_at"]),
                    inserted_count=0,
                    unchanged_count=row_count,
                    row_count=int(existing["row_count"]),
                    content_sha256=content_sha256,
                ),
                False,
            )
        connection.execute(
            update(DatasetPublication)
            .where(
                DatasetPublication.dataset == capability,
                DatasetPublication.partition_key == partition_key,
                DatasetPublication.superseded_at.is_(None),
            )
            .values(superseded_at=now)
        )
        data_version = uuid4()
        connection.execute(
            insert(DatasetPublication).values(
                publication_id=uuid4(),
                dataset=capability,
                partition_key=partition_key,
                data_version=data_version,
                quality_status="passed",
                published_at=now,
                superseded_at=None,
                effective_as_of=snapshot_date,
                knowledge_cutoff=now,
            )
        )
        connection.execute(
            insert(SwSectorPublication).values(
                data_version=data_version,
                capability=capability,
                snapshot_date=snapshot_date,
                methodology_id=methodology_id,
                row_count=row_count,
                content_sha256=content_sha256,
                published_at=now,
            )
        )
        return (
            SwPublishedCapability(
                capability=capability,
                data_version=data_version,
                snapshot_date=snapshot_date,
                published_at=now,
                inserted_count=inserted_count,
                unchanged_count=unchanged_count,
                row_count=row_count,
                content_sha256=content_sha256,
            ),
            True,
        )

    def _write_quality_results(
        self,
        connection: Session,
        *,
        snapshot: SwIndustrySnapshot,
        source_batch_id: UUID,
        taxonomy: SwPublishedCapability,
        valuation: SwPublishedCapability,
        now: datetime,
    ) -> None:
        """保存 schema、父级闭包、三级覆盖与估值覆盖的可审计通过证据。"""
        level_counts = {
            str(level.value): sum(1 for node in snapshot.nodes if node.level is level)
            for level in SwIndustryLevel
        }
        rules = (
            (
                taxonomy,
                "sw-taxonomy-three-levels",
                {"levelCounts": level_counts},
                {"requiredLevels": [1, 2, 3], "minimumPerLevel": 1},
            ),
            (
                taxonomy,
                "sw-taxonomy-parent-closure",
                {"edgeCount": len(snapshot.closure()), "orphanCount": 0},
                {"orphanCount": 0},
            ),
            (
                valuation,
                "sw-valuation-complete-coverage",
                {"nodeCount": len(snapshot.nodes), "valuationCount": len(snapshot.valuations)},
                {"difference": 0},
            ),
            (
                valuation,
                "sw-valuation-finite-or-null",
                {"invalidCount": 0},
                {"invalidCount": 0},
            ),
        )
        connection.execute(
            insert(SwSectorQualityResult),
            [
                {
                    "quality_result_id": uuid4(),
                    "source_batch_id": source_batch_id,
                    "data_version": publication.data_version,
                    "capability": publication.capability,
                    "snapshot_date": snapshot.snapshot_date,
                    "rule_code": rule_code,
                    "status": "passed",
                    "actual": actual,
                    "expected": expected,
                    "created_at": now,
                }
                for publication, rule_code, actual, expected in rules
            ],
        )

    def _checkpoint(
        self,
        connection: Session,
        *,
        snapshot_date: date,
        source: SwSourceObservation,
        taxonomy: SwPublishedCapability,
        now: datetime,
    ) -> None:
        """仅在同事务两项发布成功后推进精确日期 replay checkpoint。"""
        values = {
            "capability": _RAW_CAPABILITY,
            "partition_key": _partition_key(snapshot_date),
            "snapshot_date": snapshot_date,
            "summary_sha256": source.normalized_payload_sha256,
            "raw_sha256": source.source_payload_sha256,
            "raw_uri": source.raw_uri,
            "normalized_uri": source.normalized_uri,
            "provider_id": source.provider_id,
            "upstream_source": source.upstream_source,
            "adapter_version": source.adapter_version,
            "schema_fingerprint": source.schema_fingerprint,
            "observed_at": source.observed_at,
            "last_data_version": taxonomy.data_version,
            "last_success_at": now,
            "updated_at": now,
        }
        statement = pg_insert(SwSectorSyncCheckpoint).values(**values)
        connection.execute(
            statement.on_conflict_do_update(
                index_elements=[
                    SwSectorSyncCheckpoint.capability,
                    SwSectorSyncCheckpoint.partition_key,
                ],
                set_={
                    key: value
                    for key, value in values.items()
                    if key not in {"capability", "partition_key"}
                },
            )
        )

    def _next_revision(self, connection: Session, column: Any, *conditions: Any) -> int:
        """读取逻辑键历史最大修订号并返回严格递增后继。"""
        current = connection.execute(select(func.max(column)).where(*conditions)).scalar_one()
        return 1 if current is None else int(current) + 1


def _publication(row: Mapping[Any, Any]) -> SwPublication:
    """把发布、通用指针和方法学连接行投影为中立读取对象。"""
    return SwPublication(
        capability=cast(SwCapability, str(row["capability"])),
        data_version=UUID(str(row["data_version"])),
        snapshot_date=cast(date, row["snapshot_date"]),
        published_at=cast(datetime, row["published_at"]),
        quality_status=str(row["quality_status"]),
        row_count=int(row["row_count"]),
        content_sha256=str(row["content_sha256"]),
        methodology=SwMethodology(
            code=str(row["code"]),
            version=int(row["version"]),
            status=str(row["status"]),
            upstream_source=str(row["upstream_source"]),
            semantic_spec_sha256=str(row["semantic_spec_sha256"]),
        ),
    )


def _stored_node(row: Mapping[Any, Any]) -> SwStoredNode:
    """把数据库映射行转换为带 revision 的申万节点。"""
    return SwStoredNode(
        node=SwIndustryNode(
            code=str(row["sector_code"]),
            name=str(row["name"]),
            level=SwIndustryLevel(int(row["level"])),
            parent_code=None if row["parent_code"] is None else str(row["parent_code"]),
            component_count=int(row["component_count"]),
        ),
        revision=int(row["revision"]),
    )


def _stored_valuation(row: Mapping[Any, Any], *, snapshot_date: date) -> SwStoredValuation:
    """把节点连接估值行转换为不泄漏 ORM 的读取对象。"""
    node = _stored_node(
        {
            "sector_code": row["sector_code"],
            "name": row["name"],
            "level": row["level"],
            "parent_code": row["parent_code"],
            "component_count": row["component_count"],
            "revision": row["node_revision"],
        }
    )
    return SwStoredValuation(
        node=node.node,
        valuation=SwIndustryValuation(
            code=node.node.code,
            snapshot_date=snapshot_date,
            static_pe=_decimal_or_none(row["static_pe"]),
            ttm_pe=_decimal_or_none(row["ttm_pe"]),
            pb=_decimal_or_none(row["pb"]),
            dividend_yield_ratio=_decimal_or_none(row["dividend_yield_ratio"]),
        ),
        revision=int(row["valuation_revision"]),
    )


def _node_id(code: str) -> UUID:
    """从 scheme 与来源代码生成跨快照稳定 UUID。"""
    return uuid5(NAMESPACE_URL, f"quant-v2:sw-industry:{code}")


def _partition_key(snapshot_date: date) -> str:
    """生成按日独立、允许历史回补且不倒退最新日期的发布分区。"""
    return f"{_PARTITION_PREFIX}:{snapshot_date.isoformat()}"


def _node_sha256(node: SwIndustryNode, *, methodology_id: UUID) -> str:
    """计算包含方法学身份的单节点修订内容摘要。"""
    return _sha256(
        {
            "code": node.code,
            "name": node.name,
            "level": node.level.value,
            "parentCode": node.parent_code,
            "componentCount": node.component_count,
            "methodologyId": str(methodology_id),
        }
    )


def _valuation_sha256(
    valuation: SwIndustryValuation,
    *,
    methodology_id: UUID,
) -> str:
    """计算包含方法学身份的单行业同日估值修订内容摘要。"""
    return _sha256(
        {
            "code": valuation.code,
            "date": valuation.snapshot_date.isoformat(),
            "staticPe": _decimal_text(valuation.static_pe),
            "ttmPe": _decimal_text(valuation.ttm_pe),
            "pb": _decimal_text(valuation.pb),
            "dividendYieldRatio": _decimal_text(valuation.dividend_yield_ratio),
            "finality": "PROVIDER_OBSERVATION",
            "methodologyId": str(methodology_id),
        }
    )


def _sha256(value: object) -> str:
    """以稳定 JSON 编码计算 canonical 内容 SHA-256。"""
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _decimal_text(value: Decimal | None) -> str | None:
    """把可空精确小数稳定投影为摘要文本。"""
    return None if value is None else str(value)


def _decimal_or_none(value: object) -> Decimal | None:
    """把数据库可空 NUMERIC 转换为领域精确小数。"""
    return None if value is None else Decimal(str(value))
