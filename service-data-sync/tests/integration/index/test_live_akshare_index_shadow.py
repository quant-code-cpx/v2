"""真实 AKShare 指数影子观察六能力的端到端持久化验收。"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass
from typing import Protocol

import pytest
from sqlalchemy import func, select

from service_data_sync.application.index.shadow_sync import (
    IndexShadowSyncResult,
    IndexShadowSyncService,
)
from service_data_sync.application.ports.data_source import ProviderBatch, SourceRequest
from service_data_sync.bootstrap.settings import load_settings
from service_data_sync.domain.index import IndexAdministrator, IndexCapability, IndexIdentifier
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.database.models.canonical import (
    CanonicalCheckpoint,
    CanonicalRecordLineage,
    DatasetRelease,
    NormalizationRun,
    QualityEvaluation,
    QualityResult,
    RawPayloadManifest,
)
from service_data_sync.infrastructure.database.models.index import (
    IndexCatalogObservation,
    IndexObservedSnapshot,
)
from service_data_sync.infrastructure.database.models.provenance.source_batch import SourceBatch
from service_data_sync.infrastructure.database.models.publication.dataset_publication import (
    DatasetPublication,
)
from service_data_sync.infrastructure.object_storage.client import ObjectStorageClient
from service_data_sync.infrastructure.object_storage.raw_payload_store import S3RawPayloadStore
from service_data_sync.infrastructure.persistence.index_shadow_repository import (
    SqlAlchemyIndexShadowRepository,
)
from service_data_sync.infrastructure.providers.akshare.cnindex_index_snapshot import (
    AkshareCnindexIndexSnapshotAdapter,
)
from service_data_sync.infrastructure.providers.akshare.csindex_index_snapshot import (
    AkshareCsindexIndexSnapshotAdapter,
)

pytestmark = pytest.mark.integration

_LIVE_GATE = "DATA_SYNC_RUN_AKSHARE_LIVE_INDEX_SHADOW"
_REQUEST_TIMEOUT_SECONDS = 90
_PAUSE_SECONDS = 1.0


class _IndexSnapshotSource(Protocol):
    """描述指数影子同步所需的最小真实 adapter 能力。"""

    provider_id: str

    def capabilities(self) -> frozenset[str]:
        """返回当前 adapter 已声明的 provider-neutral 能力。"""
        ...

    async def fetch(self, request: SourceRequest) -> ProviderBatch:
        """按应用端口请求返回一批真实上游标准载荷。"""
        ...


@dataclass(frozen=True, slots=True)
class _LiveIndexCase:
    """描述一项必须由真实 AKShare 响应驱动的管理人和能力组合。"""

    administrator: IndexAdministrator
    capability: IndexCapability
    index_code: str | None

    @property
    def label(self) -> str:
        """生成稳定且可读的 pytest 参数名称，便于逐项报告真实失败。"""
        return ":".join(
            (
                self.administrator.value,
                self.capability.value,
                self.index_code or "catalog",
            )
        )


@dataclass(frozen=True, slots=True)
class _PersistedChain:
    """汇总一次真实观察的来源、规范化、质量与不可发布边界证据。"""

    source_batch_id: str
    raw_manifest_count: int
    normalized_manifest_count: int
    normalization_status: str
    quality_status: str
    quality_rule_count: int
    dataset_release_count: int
    dataset_publication_count: int
    canonical_lineage_count: int
    canonical_checkpoint_count: int


_CASES = (
    _LiveIndexCase(IndexAdministrator.CSI, IndexCapability.CATALOG_SNAPSHOT, None),
    _LiveIndexCase(IndexAdministrator.CSI, IndexCapability.CONSTITUENT_SNAPSHOT, "000300"),
    _LiveIndexCase(IndexAdministrator.CSI, IndexCapability.WEIGHT_SNAPSHOT, "000300"),
    _LiveIndexCase(IndexAdministrator.CNI, IndexCapability.CATALOG_SNAPSHOT, None),
    _LiveIndexCase(IndexAdministrator.CNI, IndexCapability.CONSTITUENT_SNAPSHOT, "399001"),
    _LiveIndexCase(IndexAdministrator.CNI, IndexCapability.WEIGHT_SNAPSHOT, "399001"),
)


def _require_live_gate() -> None:
    """默认跳过外网与真实数据库写入，必须由两个环境门显式授权。"""
    if os.environ.get("DATA_SYNC_RUN_INTEGRATION") != "1":
        pytest.skip("set DATA_SYNC_RUN_INTEGRATION=1 after starting isolated PostgreSQL")
    if os.environ.get(_LIVE_GATE) != "1":
        pytest.skip(f"set {_LIVE_GATE}=1 to run real AKShare index-shadow acceptance")


def _source_for(administrator: IndexAdministrator) -> _IndexSnapshotSource:
    """按管理人创建生产 adapter，不允许在中证和国证之间回退。"""
    if administrator is IndexAdministrator.CSI:
        return AkshareCsindexIndexSnapshotAdapter(request_timeout_seconds=_REQUEST_TIMEOUT_SECONDS)
    return AkshareCnindexIndexSnapshotAdapter(request_timeout_seconds=_REQUEST_TIMEOUT_SECONDS)


async def _sync_real_case(
    *,
    case: _LiveIndexCase,
    source: _IndexSnapshotSource,
    repository: SqlAlchemyIndexShadowRepository,
    raw_payload_store: S3RawPayloadStore,
) -> IndexShadowSyncResult:
    """通过真实 adapter、应用服务和 SQL 仓储同步一项指数影子观察。"""
    service = IndexShadowSyncService(
        source=source,
        repository=repository,
        raw_payload_store=raw_payload_store,
    )
    if case.capability is IndexCapability.CATALOG_SNAPSHOT:
        return await service.sync_catalog(administrator=case.administrator)
    assert case.index_code is not None
    return await service.sync_snapshot(
        identifier=IndexIdentifier(case.administrator, case.index_code),
        capability=case.capability,
    )


def _read_persisted_chain(
    *,
    database: DatabaseClient,
    case: _LiveIndexCase,
    result: IndexShadowSyncResult,
    expected_provider_id: str,
) -> _PersistedChain:
    """从隔离 PostgreSQL 回读完整研究态证据链和所有发布边界计数。"""
    with database.session() as session:
        if case.capability is IndexCapability.CATALOG_SNAPSHOT:
            observation = session.execute(
                select(IndexCatalogObservation).where(
                    IndexCatalogObservation.catalog_observation_id
                    == result.observation.observation_id
                )
            ).scalar_one()
            item_count = observation.record_count
        else:
            observation = session.execute(
                select(IndexObservedSnapshot).where(
                    IndexObservedSnapshot.snapshot_id == result.observation.observation_id
                )
            ).scalar_one()
            item_count = observation.item_count

        source_batch = session.get(SourceBatch, observation.source_batch_id)
        normalization = session.get(NormalizationRun, observation.normalization_run_id)
        manifests = (
            session.execute(
                select(RawPayloadManifest)
                .where(RawPayloadManifest.source_batch_id == observation.source_batch_id)
                .order_by(RawPayloadManifest.role)
            )
            .scalars()
            .all()
        )
        evaluation = session.execute(
            select(QualityEvaluation).where(
                QualityEvaluation.normalization_run_id == observation.normalization_run_id
            )
        ).scalar_one()
        quality_rules = (
            session.execute(
                select(QualityResult).where(QualityResult.evaluation_id == evaluation.evaluation_id)
            )
            .scalars()
            .all()
        )
        dataset_release_count = session.execute(
            select(func.count()).select_from(DatasetRelease)
        ).scalar_one()
        dataset_publication_count = session.execute(
            select(func.count()).select_from(DatasetPublication)
        ).scalar_one()
        canonical_lineage_count = session.execute(
            select(func.count()).select_from(CanonicalRecordLineage)
        ).scalar_one()
        canonical_checkpoint_count = session.execute(
            select(func.count()).select_from(CanonicalCheckpoint)
        ).scalar_one()

    assert source_batch is not None
    assert normalization is not None
    assert result.capability == case.capability.value
    assert result.observation.item_count == item_count
    assert source_batch.provider_id == expected_provider_id
    assert source_batch.capability == case.capability.value
    assert source_batch.upstream_source == (
        "csindex" if case.administrator is IndexAdministrator.CSI else "cnindex"
    )
    assert normalization.status == "passed"
    assert len(manifests) == 2
    assert {manifest.role for manifest in manifests} == {"raw", "normalized"}
    assert all(manifest.byte_size > 0 for manifest in manifests)
    assert all(manifest.object_uri.startswith("unretained://sha256/") for manifest in manifests)
    assert evaluation.status == result.observation.quality_status
    assert quality_rules
    assert dataset_release_count == 0
    assert dataset_publication_count == 0
    assert canonical_lineage_count == 0
    assert canonical_checkpoint_count == 0
    return _PersistedChain(
        source_batch_id=str(source_batch.source_batch_id),
        raw_manifest_count=sum(manifest.role == "raw" for manifest in manifests),
        normalized_manifest_count=sum(manifest.role == "normalized" for manifest in manifests),
        normalization_status=normalization.status,
        quality_status=evaluation.status,
        quality_rule_count=len(quality_rules),
        dataset_release_count=dataset_release_count,
        dataset_publication_count=dataset_publication_count,
        canonical_lineage_count=canonical_lineage_count,
        canonical_checkpoint_count=canonical_checkpoint_count,
    )


@pytest.mark.parametrize("case", _CASES, ids=lambda case: case.label)
def test_live_akshare_index_shadow_writes_only_research_observations(
    case: _LiveIndexCase,
) -> None:
    """六项真实指数接口均须经 adapter、应用服务和 SQL 证据链入库且零发布。"""
    _require_live_gate()
    settings = load_settings()
    database = DatabaseClient.from_settings(settings)
    object_storage = ObjectStorageClient.from_settings(settings)
    source = _source_for(case.administrator)
    try:
        result = asyncio.run(
            _sync_real_case(
                case=case,
                source=source,
                repository=SqlAlchemyIndexShadowRepository(database),
                raw_payload_store=S3RawPayloadStore(
                    object_storage,
                    retention_mode="MANIFEST_ONLY",
                ),
            )
        )
        chain = _read_persisted_chain(
            database=database,
            case=case,
            result=result,
            expected_provider_id=source.provider_id,
        )
    finally:
        object_storage.close()
        database.close()

    print(
        json.dumps(
            {
                "administrator": case.administrator.value,
                "capability": case.capability.value,
                "indexCode": case.index_code,
                "itemCount": result.observation.item_count,
                "qualityStatus": result.observation.quality_status,
                "sourceBatchId": chain.source_batch_id,
                "rawManifestCount": chain.raw_manifest_count,
                "normalizedManifestCount": chain.normalized_manifest_count,
                "normalizationStatus": chain.normalization_status,
                "qualityRuleCount": chain.quality_rule_count,
                "datasetReleaseCount": chain.dataset_release_count,
                "datasetPublicationCount": chain.dataset_publication_count,
                "canonicalRecordLineageCount": chain.canonical_lineage_count,
                "canonicalCheckpointCount": chain.canonical_checkpoint_count,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    # 串行小间隔避免实测批次短时间连续撞击同一第三方指数服务。
    time.sleep(_PAUSE_SECONDS)
