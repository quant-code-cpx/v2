"""指数 `P0-A` 目录与当前快照的 `SQLAlchemy` 研究态仓储。

中证、国证目录、成分和权重均作为来源观察保存，不能升级为指数公司的正式历史有效事实。
来源日期缺失或不一致时保持空值或隔离；权重、成员和目录各自按管理人、指数和观察日期
建版本，研究读取可追溯来源但不会为消费者伪造 `PIT` 组成。
"""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from service_data_sync.application.ports.index_shadow import (
    IndexCatalogObservationEntry,
    IndexObservedSnapshotItem,
    IndexShadowRepository,
    IndexShadowSourceObservation,
    StoredIndexShadowObservation,
)
from service_data_sync.domain.index import IndexIdentifier
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.database.models.canonical import (
    CanonicalDataset,
    DataSource,
    NormalizationRun,
    QualityEvaluation,
    QualityResult,
    RawPayloadManifest,
    SourceDataset,
)
from service_data_sync.infrastructure.database.models.index import (
    IndexCatalogObservation,
    IndexCatalogObservationItem,
    IndexDefinition,
    IndexObservedSnapshot,
)
from service_data_sync.infrastructure.database.models.index import (
    IndexObservedSnapshotItem as IndexObservedSnapshotItemModel,
)
from service_data_sync.infrastructure.database.models.provenance.source_batch import SourceBatch
from service_data_sync.infrastructure.persistence.source_batch import record_source_observation

_MAPPING_VERSION = "index-shadow-v1"
_QUALITY_POLICY = "index.shadow.observation"


class SqlAlchemyIndexShadowRepository(IndexShadowRepository):
    """保存研究态指数观察与完整来源链路，永不创建 release、publication 或 PIT 有效事实。"""

    def __init__(self, database: DatabaseClient) -> None:
        """保存服务拥有的数据库会话工厂，不把 ORM 细节泄漏给应用层。"""
        self._database = database

    def record_catalog(
        self,
        *,
        administrator: str,
        entries: tuple[IndexCatalogObservationEntry, ...],
        source: IndexShadowSourceObservation,
    ) -> StoredIndexShadowObservation:
        """原子登记非空目录观察、来源对象清单、规范化运行和研究态质量结果。"""
        if administrator not in {"CSI", "CNI"} or not entries:
            raise ValueError("index catalog observation is invalid")
        if any(entry.identifier.administrator.value != administrator for entry in entries):
            raise ValueError("catalog entries must share one administrator")
        now = datetime.now(UTC)
        with self._database.transaction() as session:
            dataset_id = _ensure_dataset(
                session,
                code="index.catalog",
                domain="index",
                grain="administrator + current observed index catalog item",
            )
            source_batch_id, run_id = _record_source_evidence(
                session, source=source, partition_key=f"index.catalog:{administrator}", now=now
            )
            normalization_run_id, normalization_created = _record_normalization(
                session,
                dataset_id=dataset_id,
                run_id=run_id,
                partition_key=f"index.catalog:{administrator}",
                source=source,
                now=now,
            )
            ids = {
                entry.identifier: _ensure_index_definition(session, entry.identifier, now)
                for entry in entries
            }
            observation_id = uuid4()
            content_hash = _catalog_hash(entries)
            session.execute(
                insert(IndexCatalogObservation).values(
                    catalog_observation_id=observation_id,
                    administrator_code=administrator,
                    source_batch_id=source_batch_id,
                    normalization_run_id=normalization_run_id,
                    observed_at=source.observed_at,
                    record_count=len(entries),
                    content_hash=content_hash,
                )
            )
            session.execute(
                insert(IndexCatalogObservationItem).values(
                    [
                        {
                            "catalog_observation_id": observation_id,
                            "index_id": ids[entry.identifier],
                            "source_name": entry.name,
                            "source_full_name": entry.full_name,
                            "source_base_date": entry.base_date,
                            "source_base_value": entry.base_value,
                            "source_published_date": entry.published_date,
                            "constituent_count": entry.constituent_count,
                        }
                        for entry in entries
                    ]
                )
            )
            if normalization_created:
                # 相同输入重放必须复用原有质量结论，避免同一规则在一个运行上重复写入。
                _record_quality(
                    session,
                    dataset_id=dataset_id,
                    partition_key=f"index.catalog:{administrator}",
                    normalization_run_id=normalization_run_id,
                    status="passed",
                    rule_code="index.shadow.catalog-non-empty",
                    severity="blocking",
                    passed=True,
                    actual_value=len(entries),
                    threshold_value=1,
                    affected_count=0,
                    now=now,
                )
        return StoredIndexShadowObservation(observation_id, len(entries), "passed")

    def record_snapshot(
        self,
        *,
        identifier: IndexIdentifier,
        observation_kind: str,
        source_as_of_date: date | None,
        items: tuple[IndexObservedSnapshotItem, ...],
        source: IndexShadowSourceObservation,
    ) -> StoredIndexShadowObservation:
        """登记当前成分或权重观察；缺交易所只降低研究态质量，不尝试按代码猜测。"""
        if observation_kind not in {"constituent_current", "weight_snapshot"} or not items:
            raise ValueError("index observed snapshot is invalid")
        if len({item.source_symbol for item in items}) != len(items):
            raise ValueError("index observed snapshot symbols must be unique")
        now = datetime.now(UTC)
        dataset_code = (
            "index.constituent.observed_snapshot"
            if observation_kind == "constituent_current"
            else "index.weight.observed_snapshot"
        )
        partition_key = f"{identifier.qualified_key}:{observation_kind}"
        quality_status = (
            "warned" if any(item.source_exchange is None for item in items) else "passed"
        )
        with self._database.transaction() as session:
            dataset_id = _ensure_dataset(
                session,
                code=dataset_code,
                domain="index",
                grain="index + observed source snapshot item",
            )
            source_batch_id, run_id = _record_source_evidence(
                session, source=source, partition_key=partition_key, now=now
            )
            normalization_run_id, normalization_created = _record_normalization(
                session,
                dataset_id=dataset_id,
                run_id=run_id,
                partition_key=partition_key,
                source=source,
                now=now,
            )
            index_id = _ensure_index_definition(session, identifier, now)
            snapshot_id = uuid4()
            session.execute(
                insert(IndexObservedSnapshot).values(
                    snapshot_id=snapshot_id,
                    index_id=index_id,
                    dataset_id=dataset_id,
                    source_batch_id=source_batch_id,
                    normalization_run_id=normalization_run_id,
                    observation_kind=observation_kind,
                    source_as_of_date=source_as_of_date,
                    observed_at=source.observed_at,
                    item_count=len(items),
                    quality_status=quality_status,
                    content_hash=_snapshot_hash(items),
                )
            )
            session.execute(
                insert(IndexObservedSnapshotItemModel).values(
                    [
                        {
                            "snapshot_id": snapshot_id,
                            "source_symbol": item.source_symbol,
                            "source_name": item.source_name,
                            "source_exchange": item.source_exchange,
                            "source_industry": item.source_industry,
                            "weight_value": item.weight_value,
                            "weight_kind": item.weight_kind,
                        }
                        for item in items
                    ]
                )
            )
            if normalization_created:
                # 质量结论由确定性输入唯一决定；新来源观察只回链既有运行，不能再插入一份规则结果。
                _record_quality(
                    session,
                    dataset_id=dataset_id,
                    partition_key=partition_key,
                    normalization_run_id=normalization_run_id,
                    status=quality_status,
                    rule_code="index.shadow.source-exchange",
                    severity="warn",
                    passed=not any(item.source_exchange is None for item in items),
                    actual_value=sum(item.source_exchange is None for item in items),
                    threshold_value=0,
                    affected_count=sum(item.source_exchange is None for item in items),
                    now=now,
                )
        return StoredIndexShadowObservation(snapshot_id, len(items), quality_status)


def _ensure_dataset(session: Session, *, code: str, domain: str, grain: str) -> UUID:
    """幂等登记研究态 dataset，避免每个指数能力自行复制 dataset 生命周期定义。"""
    dataset_id = uuid5(NAMESPACE_URL, f"quant-v2:canonical-dataset:{code}:1")
    session.execute(
        pg_insert(CanonicalDataset)
        .values(
            dataset_id=dataset_id,
            code=code,
            schema_version=1,
            domain=domain,
            grain=grain,
            status="research",
            owner_service="service-data-sync",
            created_at=datetime.now(UTC),
        )
        .on_conflict_do_nothing(index_elements=("code", "schema_version"))
    )
    return UUID(
        str(
            session.execute(
                select(CanonicalDataset.dataset_id).where(CanonicalDataset.code == code)
            ).scalar_one()
        )
    )


def _ensure_index_definition(session: Session, identifier: IndexIdentifier, now: datetime) -> UUID:
    """以管理人和当前代码建立暂定观察身份，代码延续只能由未来官方事件流程处理。"""
    index_id = uuid5(NAMESPACE_URL, f"quant-v2:index-observed:{identifier.qualified_key}")
    session.execute(
        pg_insert(IndexDefinition)
        .values(
            index_id=index_id,
            administrator_code=identifier.administrator.value,
            source_index_code=identifier.code,
            status="observed",
            created_at=now,
        )
        .on_conflict_do_nothing(index_elements=("administrator_code", "source_index_code"))
    )
    return UUID(
        str(
            session.execute(
                select(IndexDefinition.index_id).where(
                    IndexDefinition.administrator_code == identifier.administrator.value,
                    IndexDefinition.source_index_code == identifier.code,
                )
            ).scalar_one()
        )
    )


def _record_source_evidence(
    session: Session,
    *,
    source: IndexShadowSourceObservation,
    partition_key: str,
    now: datetime,
) -> tuple[UUID, UUID]:
    """登记真实来源产品、两个不可变对象 manifest 和独立 source batch。"""
    source_dataset_id = _ensure_source_dataset(session, source=source)
    source_batch_id = record_source_observation(
        session,
        provider_id=source.provider_id,
        capability=source.capability,
        source_payload_sha256=source.raw_payload_sha256,
        raw_uri=source.raw_uri,
        observed_at=source.observed_at,
        created_at=now,
        upstream_source=source.upstream_source,
        adapter_version=source.adapter_version,
        schema_fingerprint=source.schema_fingerprint,
        source_dataset_id=source_dataset_id,
    )
    session.execute(
        insert(RawPayloadManifest).values(
            [
                {
                    "raw_payload_id": uuid4(),
                    "source_batch_id": source_batch_id,
                    "sequence_no": 1,
                    "role": "raw",
                    "object_uri": source.raw_uri,
                    "sha256": source.raw_payload_sha256,
                    "content_type": source.raw_content_type,
                    "byte_size": source.raw_byte_size,
                    "fetched_at": source.observed_at,
                },
                {
                    "raw_payload_id": uuid4(),
                    "source_batch_id": source_batch_id,
                    "sequence_no": 1,
                    "role": "normalized",
                    "object_uri": source.normalized_uri,
                    "sha256": source.normalized_payload_sha256,
                    "content_type": source.normalized_content_type,
                    "byte_size": source.normalized_byte_size,
                    "fetched_at": source.observed_at,
                },
            ]
        )
    )
    run_id = session.execute(
        select(SourceBatch.run_id).where(SourceBatch.source_batch_id == source_batch_id)
    ).scalar_one()
    return source_batch_id, UUID(str(run_id))


def _ensure_source_dataset(session: Session, *, source: IndexShadowSourceObservation) -> UUID:
    """将中证和国证真实上游登记为 research-only 产品，缺许可时不能被误标生产可发布。"""
    details = {
        "csindex": ("中证指数官网", "csindex"),
        "cnindex": ("国证指数网", "cnindex"),
    }.get(source.upstream_source)
    if details is None:
        raise ValueError("index shadow upstream source is not approved")
    legal_name, source_code = details
    source_id = uuid5(NAMESPACE_URL, f"quant-v2:data-source:{source_code}")
    dataset_code = f"{source_code}:{source.capability}"
    source_dataset_id = uuid5(NAMESPACE_URL, f"quant-v2:source-dataset:{dataset_code}")
    session.execute(
        pg_insert(DataSource)
        .values(
            source_id=source_id,
            code=source_code,
            legal_name=legal_name,
            source_kind="official",
            timezone="Asia/Shanghai",
            rights_status="research",
            rights_evidence_ref=None,
        )
        .on_conflict_do_nothing(index_elements=("code",))
    )
    session.execute(
        pg_insert(SourceDataset)
        .values(
            source_dataset_id=source_dataset_id,
            source_id=source_id,
            code=dataset_code,
            capability=source.capability,
            native_grain="provider response",
            native_unit_json={},
            history_from=None,
            history_to=None,
            license_scope="research_only",
            active=True,
        )
        .on_conflict_do_nothing(index_elements=("source_id", "code"))
    )
    return UUID(
        str(
            session.execute(
                select(SourceDataset.source_dataset_id).where(
                    SourceDataset.source_id == source_id, SourceDataset.code == dataset_code
                )
            ).scalar_one()
        )
    )


def _record_normalization(
    session: Session,
    *,
    dataset_id: UUID,
    run_id: UUID,
    partition_key: str,
    source: IndexShadowSourceObservation,
    now: datetime,
) -> tuple[UUID, bool]:
    """建立或复用确定性规范化运行，并返回是否首次写入其质量结论。

    每次上游抓取仍保留独立 `SourceBatch` 和该批次的双 `RawPayloadManifest`，因为观察时间本身
    是审计证据；但相同数据集、分区、输入摘要和映射版本只能共享一个规范化运行与一套质量结果。
    这样重试不会触发唯一约束或虚增质量记录，任一 raw 或标准载荷摘要变化仍会创建新研究态运行。
    """
    input_set_hash = _normalization_input_set_hash(source)
    inserted = session.execute(
        pg_insert(NormalizationRun)
        .values(
            normalization_run_id=uuid4(),
            dataset_id=dataset_id,
            partition_key=partition_key,
            run_id=run_id,
            adapter_version=source.adapter_version,
            schema_fingerprint=source.schema_fingerprint,
            mapping_version=_MAPPING_VERSION,
            input_set_hash=input_set_hash,
            status="passed",
            started_at=now,
            finished_at=now,
        )
        .on_conflict_do_nothing(
            index_elements=("dataset_id", "partition_key", "input_set_hash", "mapping_version")
        )
        .returning(NormalizationRun.normalization_run_id)
    ).scalar_one_or_none()
    if inserted is not None:
        return UUID(str(inserted)), True
    existing = session.execute(
        select(NormalizationRun.normalization_run_id).where(
            NormalizationRun.dataset_id == dataset_id,
            NormalizationRun.partition_key == partition_key,
            NormalizationRun.input_set_hash == input_set_hash,
            NormalizationRun.mapping_version == _MAPPING_VERSION,
        )
    ).scalar_one()
    return UUID(str(existing)), False


def _normalization_input_set_hash(source: IndexShadowSourceObservation) -> str:
    """计算 raw 与标准化载荷共同决定的重放身份，避免只看单侧摘要错误复用。"""
    return hashlib.sha256(
        f"{source.raw_payload_sha256}:{source.normalized_payload_sha256}".encode()
    ).hexdigest()


def _record_quality(
    session: Session,
    *,
    dataset_id: UUID,
    partition_key: str,
    normalization_run_id: UUID,
    status: str,
    rule_code: str,
    severity: str,
    passed: bool,
    actual_value: int,
    threshold_value: int,
    affected_count: int,
    now: datetime,
) -> None:
    """按规则记录研究态质量结论，任何 warn 都不会被升级为 production 发布。"""
    evaluation_id = uuid4()
    session.execute(
        insert(QualityEvaluation).values(
            evaluation_id=evaluation_id,
            dataset_id=dataset_id,
            partition_key=partition_key,
            normalization_run_id=normalization_run_id,
            policy_code=_QUALITY_POLICY,
            policy_version=1,
            status=status,
            score=None,
            evaluated_at=now,
        )
    )
    session.execute(
        insert(QualityResult).values(
            evaluation_id=evaluation_id,
            rule_code=rule_code,
            severity=severity,
            passed=passed,
            actual_value=actual_value,
            threshold_value=threshold_value,
            sample_json=None,
            affected_count=affected_count,
        )
    )


def _catalog_hash(entries: tuple[IndexCatalogObservationEntry, ...]) -> str:
    """计算目录业务内容摘要，供观察审计而非身份或代码延续判断使用。"""
    material = "|".join(
        f"{entry.identifier.qualified_key}:{entry.name}:{entry.constituent_count}"
        for entry in entries
    )
    return hashlib.sha256(material.encode()).hexdigest()


def _snapshot_hash(items: tuple[IndexObservedSnapshotItem, ...]) -> str:
    """计算当前观察内容摘要，保证相同来源行可被独立审计而非静默覆盖。"""
    material = "|".join(
        f"{item.source_symbol}:{item.source_exchange}:{item.weight_value}:{item.weight_kind}"
        for item in items
    )
    return hashlib.sha256(material.encode()).hexdigest()
