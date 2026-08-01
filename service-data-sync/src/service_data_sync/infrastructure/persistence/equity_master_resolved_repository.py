"""发布目录与显式生命周期组成的 A 股 resolved 主数据视图。

该视图不抓取 Provider，也不复制身份、名称或生命周期事实。它只把已经通过质量门的
目录与生命周期 `publication` 固定为一个可重放的组件清单，使消费者能在独立的
知识截止点下读取“目录身份/名称 + 官方生命周期优先、目录状态兜底”的组合。
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import insert, select
from sqlalchemy.orm import Session

from service_data_sync.application.ports.canonical_release import CanonicalLineageRecord
from service_data_sync.domain.equity import Exchange
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.database.fenced_execution import current_fenced_execution
from service_data_sync.infrastructure.database.models.canonical import CanonicalRecordLineage
from service_data_sync.infrastructure.database.models.provenance.source_batch import SourceBatch
from service_data_sync.infrastructure.database.models.publication.dataset_publication import (
    DatasetPublication,
)
from service_data_sync.infrastructure.persistence.canonical_release_repository import (
    SqlAlchemyCanonicalReleaseRepository,
)
from service_data_sync.infrastructure.persistence.legacy_canonical_release_bridge import (
    publish_legacy_snapshot,
)

from ..database.models.publication.dataset_publication_component import (
    DatasetPublicationComponent,
)

_CATALOG_DATASET = "equity.master.catalog"
_LIFECYCLE_DATASET = "equity.lifecycle.explicit"
_RESOLVED_DATASET = "equity.master.resolved"
_RESOLVED_AGGREGATE_PARTITION = "CN_A_STABLE"
_CATALOG_COMPONENT = "catalog"
_LIFECYCLE_COMPONENT = "lifecycle"


class ResolvedEquityMasterUnavailable(RuntimeError):
    """表示构建 resolved 主数据时缺少可安全固定的已发布输入组件。"""


@dataclass(frozen=True, slots=True)
class PublishedResolvedEquityMaster:
    """描述一次 providerless resolved 发布的聚合 data version 与组件数。"""

    data_version: UUID
    component_count: int


@dataclass(frozen=True, slots=True)
class _PublishedComponent:
    """保存已冻结输入 publication 的最小不可变元数据。"""

    component_key: str
    dataset: str
    partition_key: str
    data_version: UUID
    release_id: UUID
    effective_as_of: date
    knowledge_cutoff: datetime
    quality_status: str


@dataclass(frozen=True, slots=True)
class _ResolvedLeaf:
    """保存一所交易所的 resolved publication，供全市场聚合继续冻结。"""

    exchange: Exchange
    data_version: UUID
    release_id: UUID
    effective_as_of: date


class SqlAlchemyResolvedEquityMasterRepository:
    """用通用 release/publish 事务固定 resolved 主数据组件，而不旁路控制面。"""

    def __init__(self, database: DatabaseClient) -> None:
        """保存服务拥有的事务工厂和统一 canonical 发布仓储。"""
        self._database = database
        self._release_repository = SqlAlchemyCanonicalReleaseRepository(database)

    def publish(self) -> PublishedResolvedEquityMaster:
        """固定三所目录/生命周期输入，并发布一个全市场 resolved manifest。

        三所 leaf 与最终全市场 manifest 在一个事务中生成。若任意输入 publication、质量、
        release 或来源血缘不完整，整个事务回滚，旧 resolved publication 保持可见。
        """
        now = datetime.now(UTC)
        with self._database.transaction() as session:
            leaves = tuple(
                self._publish_exchange(session, exchange=exchange, now=now) for exchange in Exchange
            )
            aggregate = self._publish_aggregate(session, leaves=leaves, now=now)
        return PublishedResolvedEquityMaster(
            data_version=aggregate.data_version,
            component_count=len(leaves) * 2,
        )

    def _publish_exchange(
        self,
        session: Session,
        *,
        exchange: Exchange,
        now: datetime,
    ) -> _ResolvedLeaf:
        """将一所交易所的目录和生命周期 publication 固定为 leaf resolved 版本。"""
        components = self._current_exchange_components(session, exchange=exchange)
        records = _component_release_records(
            components=components, source_rows=self._source_rows(session, components=components)
        )
        published = publish_legacy_snapshot(
            session,
            release_repository=self._release_repository,
            dataset_code=_RESOLVED_DATASET,
            partition_key=exchange.value,
            domain="equity",
            grain="exchange catalog identity/name + explicit lifecycle resolved manifest",
            semantic_family="derived-equity-reference",
            mapping_version="equity-master-resolved-release-v1",
            source_batch_id=records[0].source_batch_id,
            records=records,
            fact_min=min(component.effective_as_of for component in components),
            fact_max=max(component.effective_as_of for component in components),
            now=now,
            publication_effective_as_of=min(component.effective_as_of for component in components),
            write_publication=lambda connection, publication_id, _data_version, _release_id: (
                _write_components(
                    connection,
                    publication_id=publication_id,
                    components=components,
                )
            ),
            # leaf 是同一 Data Operations run 的中间产物；只由最终全市场 publication
            # 记录控制面 data-version，避免一个 run 伪装成四次独立完成。
            record_fenced_progress=False,
        )
        return _ResolvedLeaf(
            exchange=exchange,
            data_version=published.data_version,
            release_id=published.release_id,
            effective_as_of=min(component.effective_as_of for component in components),
        )

    def _publish_aggregate(
        self,
        session: Session,
        *,
        leaves: tuple[_ResolvedLeaf, ...],
        now: datetime,
    ) -> _ResolvedLeaf:
        """把三所 leaf resolved 版本固化为稳定全市场 manifest 并收敛 fenced run。"""
        if {leaf.exchange for leaf in leaves} != set(Exchange):
            raise ResolvedEquityMasterUnavailable("all exchange resolved leaves are required")
        components = tuple(
            _PublishedComponent(
                component_key=leaf.exchange.value,
                dataset=_RESOLVED_DATASET,
                partition_key=leaf.exchange.value,
                data_version=leaf.data_version,
                release_id=leaf.release_id,
                effective_as_of=leaf.effective_as_of,
                # 全市场 manifest 不读取该字段；独立知识截止点由 leaf 的直接组件保留。
                knowledge_cutoff=now,
                quality_status="passed",
            )
            for leaf in leaves
        )
        records = _component_release_records(
            components=components, source_rows=self._source_rows(session, components=components)
        )
        execution = current_fenced_execution()
        if execution is not None:
            # 最后一个 canonical publication 的同一事务写入 run 成功终态，不能在执行器返回后
            # 再单独完成控制面记录。
            execution.arm_terminal_write()
        published = publish_legacy_snapshot(
            session,
            release_repository=self._release_repository,
            dataset_code=_RESOLVED_DATASET,
            partition_key=_RESOLVED_AGGREGATE_PARTITION,
            domain="equity",
            grain="CN A stable resolved master + exchange resolved release manifest",
            semantic_family="derived-equity-reference",
            mapping_version="equity-master-resolved-aggregate-release-v1",
            source_batch_id=records[0].source_batch_id,
            records=records,
            fact_min=min(component.effective_as_of for component in components),
            fact_max=max(component.effective_as_of for component in components),
            now=now,
            publication_effective_as_of=min(component.effective_as_of for component in components),
            write_publication=lambda connection, publication_id, _data_version, _release_id: (
                _write_components(
                    connection,
                    publication_id=publication_id,
                    components=components,
                )
            ),
        )
        return _ResolvedLeaf(
            exchange=Exchange.SSE,
            data_version=published.data_version,
            release_id=published.release_id,
            effective_as_of=min(component.effective_as_of for component in components),
        )

    @staticmethod
    def _current_exchange_components(
        session: Session,
        *,
        exchange: Exchange,
    ) -> tuple[_PublishedComponent, ...]:
        """读取同一交易所当前通过质量门的目录和显式生命周期输入。"""
        rows = (
            session.execute(
                select(
                    DatasetPublication.dataset,
                    DatasetPublication.partition_key,
                    DatasetPublication.data_version,
                    DatasetPublication.release_id,
                    DatasetPublication.effective_as_of,
                    DatasetPublication.knowledge_cutoff,
                    DatasetPublication.quality_status,
                ).where(
                    DatasetPublication.dataset.in_((_CATALOG_DATASET, _LIFECYCLE_DATASET)),
                    DatasetPublication.partition_key == exchange.value,
                    DatasetPublication.superseded_at.is_(None),
                    DatasetPublication.quality_status == "passed",
                    DatasetPublication.release_id.is_not(None),
                    DatasetPublication.effective_as_of.is_not(None),
                    DatasetPublication.knowledge_cutoff.is_not(None),
                )
            )
            .mappings()
            .all()
        )
        by_dataset = {str(row["dataset"]): row for row in rows}
        if set(by_dataset) != {_CATALOG_DATASET, _LIFECYCLE_DATASET}:
            raise ResolvedEquityMasterUnavailable(
                f"resolved input publication is unavailable for {exchange.value}"
            )
        components = (
            _component_from_row(_CATALOG_COMPONENT, by_dataset[_CATALOG_DATASET]),
            _component_from_row(_LIFECYCLE_COMPONENT, by_dataset[_LIFECYCLE_DATASET]),
        )
        return components

    @staticmethod
    def _source_rows(
        session: Session,
        *,
        components: Sequence[_PublishedComponent],
    ) -> Sequence[Mapping[Any, Any]]:
        """读取每个输入 release 的真实来源血缘，派生发布不制造虚构 Provider 批次。"""
        releases = {component.release_id for component in components}
        release_to_component = {
            component.release_id: component.component_key for component in components
        }
        rows = (
            session.execute(
                select(
                    CanonicalRecordLineage.release_id,
                    CanonicalRecordLineage.source_batch_id,
                    SourceBatch.created_at.label("source_created_at"),
                )
                .join(
                    SourceBatch,
                    SourceBatch.source_batch_id == CanonicalRecordLineage.source_batch_id,
                )
                .where(CanonicalRecordLineage.release_id.in_(tuple(releases)))
                .order_by(
                    CanonicalRecordLineage.release_id,
                    SourceBatch.created_at,
                    CanonicalRecordLineage.source_batch_id,
                )
            )
            .mappings()
            .all()
        )
        return tuple(
            {
                **row,
                "component_key": release_to_component[UUID(str(row["release_id"]))],
            }
            for row in rows
        )


def _component_from_row(component_key: str, row: Mapping[Any, Any]) -> _PublishedComponent:
    """将 publication 查询行转为经运行时类型校验的输入组件。"""
    effective_as_of = row["effective_as_of"]
    knowledge_cutoff = row["knowledge_cutoff"]
    release_id = row["release_id"]
    if (
        not isinstance(effective_as_of, date)
        or not isinstance(knowledge_cutoff, datetime)
        or knowledge_cutoff.tzinfo is None
        or release_id is None
    ):
        raise ResolvedEquityMasterUnavailable("resolved input publication metadata is invalid")
    return _PublishedComponent(
        component_key=component_key,
        dataset=str(row["dataset"]),
        partition_key=str(row["partition_key"]),
        data_version=UUID(str(row["data_version"])),
        release_id=UUID(str(release_id)),
        effective_as_of=effective_as_of,
        knowledge_cutoff=knowledge_cutoff,
        quality_status=str(row["quality_status"]),
    )


def _component_release_records(
    *,
    components: Sequence[_PublishedComponent],
    source_rows: Sequence[Mapping[Any, Any]],
) -> tuple[CanonicalLineageRecord, ...]:
    """将每个固定输入组件归约为一条确定性 derived release lineage 记录。"""
    expected = {component.component_key for component in components}
    selected_sources: dict[str, tuple[datetime, UUID]] = {}
    for row in source_rows:
        component_key = str(row["component_key"])
        if component_key not in expected:
            raise ResolvedEquityMasterUnavailable(
                "resolved source lineage has an unknown component"
            )
        created_at = row["source_created_at"]
        if not isinstance(created_at, datetime) or created_at.tzinfo is None:
            raise ResolvedEquityMasterUnavailable(
                "resolved source lineage requires timezone-aware creation time"
            )
        source_batch_id = UUID(str(row["source_batch_id"]))
        candidate = (created_at, source_batch_id)
        if component_key not in selected_sources or candidate < selected_sources[component_key]:
            selected_sources[component_key] = candidate
    if set(selected_sources) != expected:
        raise ResolvedEquityMasterUnavailable(
            "all resolved input releases require canonical source lineage"
        )
    transform_hash = hashlib.sha256(b"equity-master-resolved-release-v1").hexdigest()
    return tuple(
        CanonicalLineageRecord(
            record_key_hash=hashlib.sha256(
                (
                    f"resolved-component:{component.component_key}:{component.dataset}:"
                    f"{component.partition_key}:{component.data_version}"
                ).encode()
            ).hexdigest(),
            content_hash=hashlib.sha256(
                (
                    f"{component.component_key}:{component.dataset}:"
                    f"{component.partition_key}:{component.data_version}"
                ).encode()
            ).hexdigest(),
            source_batch_id=selected_sources[component.component_key][1],
            transform_hash=transform_hash,
            role="input",
        )
        for component in sorted(components, key=lambda value: value.component_key)
    )


def _write_components(
    session: Session,
    *,
    publication_id: UUID,
    components: Sequence[_PublishedComponent],
) -> None:
    """在 publication 切换的同一事务中写入不可变组件 data version 清单。"""
    session.execute(
        insert(DatasetPublicationComponent).values(
            [
                {
                    "aggregate_publication_id": publication_id,
                    "component_partition_key": component.component_key,
                    "component_data_version": component.data_version,
                }
                for component in components
            ]
        )
    )
