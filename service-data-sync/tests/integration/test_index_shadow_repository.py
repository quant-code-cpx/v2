"""指数 P0-A 影子观察仓储的 PostgreSQL 集成测试。"""

from __future__ import annotations

import os
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from service_data_sync.application.ports.index_shadow import (
    IndexCatalogObservationEntry,
    IndexObservedSnapshotItem,
    IndexShadowSourceObservation,
)
from service_data_sync.bootstrap.settings import load_settings
from service_data_sync.domain.index import IndexAdministrator, IndexIdentifier
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
    IndexDefinition,
    IndexObservedSnapshot,
)
from service_data_sync.infrastructure.database.models.index import (
    IndexObservedSnapshotItem as IndexObservedSnapshotItemModel,
)
from service_data_sync.infrastructure.database.models.provenance.source_batch import SourceBatch
from service_data_sync.infrastructure.database.models.publication.dataset_publication import (
    DatasetPublication,
)
from service_data_sync.infrastructure.persistence.index_shadow_repository import (
    SqlAlchemyIndexShadowRepository,
)


@pytest.mark.integration
def test_repository_persists_catalog_and_warned_weight_observation_with_full_source_chain() -> None:
    """真实 PostgreSQL 必须保留独立 batch、双 manifest、规范化、质量和观察行，且不创建发布版本。"""
    if os.environ.get("DATA_SYNC_RUN_INTEGRATION") != "1":
        pytest.skip("set DATA_SYNC_RUN_INTEGRATION=1 after starting local infrastructure")
    database = DatabaseClient.from_settings(load_settings())
    repository = SqlAlchemyIndexShadowRepository(database)
    observed_at = datetime(2026, 7, 29, 8, tzinfo=UTC)
    identifier = IndexIdentifier(IndexAdministrator.CNI, "399001")
    try:
        catalog = repository.record_catalog(
            administrator="CNI",
            entries=(
                IndexCatalogObservationEntry(
                    identifier=identifier,
                    name="深证成指",
                    full_name=None,
                    base_date=None,
                    base_value=None,
                    published_date=None,
                    constituent_count=500,
                ),
            ),
            source=_source("index.catalog.snapshot", observed_at),
        )
        snapshot = repository.record_snapshot(
            identifier=identifier,
            observation_kind="weight_snapshot",
            source_as_of_date=date(2026, 7, 28),
            items=(
                IndexObservedSnapshotItem(
                    source_symbol="000001",
                    source_name="平安银行",
                    source_exchange=None,
                    source_industry="银行",
                    weight_value=Decimal("0.025"),
                    weight_kind="observed",
                ),
            ),
            source=_source("index.weight.snapshot", observed_at),
        )
        with database.session() as session:
            assert session.execute(select(func.count()).select_from(SourceBatch)).scalar_one() >= 2
            assert (
                session.execute(select(func.count()).select_from(RawPayloadManifest)).scalar_one()
                >= 4
            )
            assert (
                session.execute(select(func.count()).select_from(NormalizationRun)).scalar_one()
                >= 2
            )
            assert (
                session.execute(select(func.count()).select_from(QualityEvaluation)).scalar_one()
                >= 2
            )
            stored = session.execute(
                select(IndexObservedSnapshot.quality_status).where(
                    IndexObservedSnapshot.snapshot_id == snapshot.observation_id
                )
            ).scalar_one()
            item = session.execute(
                select(IndexObservedSnapshotItemModel).where(
                    IndexObservedSnapshotItemModel.snapshot_id == snapshot.observation_id
                )
            ).scalar_one()
            assert (
                session.execute(
                    select(IndexCatalogObservation.catalog_observation_id).where(
                        IndexCatalogObservation.catalog_observation_id == catalog.observation_id
                    )
                ).scalar_one()
                == catalog.observation_id
            )
            catalog_normalization_run_id = session.execute(
                select(IndexCatalogObservation.normalization_run_id).where(
                    IndexCatalogObservation.catalog_observation_id == catalog.observation_id
                )
            ).scalar_one()
            catalog_quality = session.execute(
                select(QualityResult)
                .join(
                    QualityEvaluation,
                    QualityResult.evaluation_id == QualityEvaluation.evaluation_id,
                )
                .where(
                    QualityEvaluation.normalization_run_id == catalog_normalization_run_id,
                    QualityResult.rule_code == "index.shadow.catalog-non-empty",
                )
            ).scalar_one()
    finally:
        database.close()

    assert stored == "warned"
    assert item.source_exchange is None
    assert item.weight_value == Decimal("0.0250000000")
    assert catalog_quality.passed is True
    assert catalog_quality.actual_value == Decimal("1")
    assert catalog_quality.affected_count == 0


@pytest.mark.integration
def test_repository_persists_real_alphanumeric_csi_catalog_identity() -> None:
    """真实中证目录已验证的 ``H00999`` 必须经完整观察仓储链路成功落库。"""
    if os.environ.get("DATA_SYNC_RUN_INTEGRATION") != "1":
        pytest.skip("set DATA_SYNC_RUN_INTEGRATION=1 after starting local infrastructure")
    database = DatabaseClient.from_settings(load_settings())
    repository = SqlAlchemyIndexShadowRepository(database)
    observed_at = datetime(2026, 8, 1, 8, tzinfo=UTC)
    identifier = IndexIdentifier(IndexAdministrator.CSI, "H00999")
    try:
        catalog = repository.record_catalog(
            administrator="CSI",
            entries=(
                IndexCatalogObservationEntry(
                    identifier=identifier,
                    name="中证A500",
                    full_name="中证A500指数",
                    base_date=None,
                    base_value=None,
                    published_date=None,
                    constituent_count=500,
                ),
            ),
            source=_source(
                "index.catalog.snapshot",
                observed_at,
                upstream_source="csindex",
            ),
        )
        with database.session() as session:
            stored_code = session.execute(
                select(IndexDefinition.source_index_code).where(
                    IndexDefinition.administrator_code == "CSI",
                    IndexDefinition.source_index_code == "H00999",
                )
            ).scalar_one()
            stored_count = session.execute(
                select(IndexCatalogObservation.record_count).where(
                    IndexCatalogObservation.catalog_observation_id == catalog.observation_id
                )
            ).scalar_one()
    finally:
        database.close()

    assert stored_code == "H00999"
    assert stored_count == 1


@pytest.mark.integration
def test_repository_reuses_normalization_and_quality_for_identical_snapshot_input() -> None:
    """相同输入重放应新增来源观察，但复用同一规范化和质量结论且零发布。"""
    if os.environ.get("DATA_SYNC_RUN_INTEGRATION") != "1":
        pytest.skip("set DATA_SYNC_RUN_INTEGRATION=1 after starting local infrastructure")
    database = DatabaseClient.from_settings(load_settings())
    repository = SqlAlchemyIndexShadowRepository(database)
    identifier = IndexIdentifier(IndexAdministrator.CNI, "399002")
    items = (
        IndexObservedSnapshotItem(
            source_symbol="000001",
            source_name="平安银行",
            source_exchange=None,
            source_industry="银行",
            weight_value=Decimal("0.025"),
            weight_kind="observed",
        ),
    )
    try:
        first = repository.record_snapshot(
            identifier=identifier,
            observation_kind="weight_snapshot",
            source_as_of_date=date(2026, 7, 28),
            items=items,
            source=_source(
                "index.weight.snapshot",
                datetime(2026, 8, 1, 8, tzinfo=UTC),
            ),
        )
        second = repository.record_snapshot(
            identifier=identifier,
            observation_kind="weight_snapshot",
            source_as_of_date=date(2026, 7, 28),
            items=items,
            source=_source(
                "index.weight.snapshot",
                datetime(2026, 8, 1, 8, 1, tzinfo=UTC),
            ),
        )
        with database.session() as session:
            first_snapshot = session.get(IndexObservedSnapshot, first.observation_id)
            second_snapshot = session.get(IndexObservedSnapshot, second.observation_id)
            assert first_snapshot is not None
            assert second_snapshot is not None
            source_batch_ids = (first_snapshot.source_batch_id, second_snapshot.source_batch_id)
            normalization_run_id = first_snapshot.normalization_run_id
            source_batch_count = session.execute(
                select(func.count())
                .select_from(SourceBatch)
                .where(SourceBatch.source_batch_id.in_(source_batch_ids))
            ).scalar_one()
            raw_manifest_count = session.execute(
                select(func.count())
                .select_from(RawPayloadManifest)
                .where(RawPayloadManifest.source_batch_id.in_(source_batch_ids))
            ).scalar_one()
            normalization_count = session.execute(
                select(func.count())
                .select_from(NormalizationRun)
                .where(NormalizationRun.normalization_run_id == normalization_run_id)
            ).scalar_one()
            quality_evaluation_count = session.execute(
                select(func.count())
                .select_from(QualityEvaluation)
                .where(QualityEvaluation.normalization_run_id == normalization_run_id)
            ).scalar_one()
            quality_result_count = session.execute(
                select(func.count())
                .select_from(QualityResult)
                .join(
                    QualityEvaluation,
                    QualityResult.evaluation_id == QualityEvaluation.evaluation_id,
                )
                .where(QualityEvaluation.normalization_run_id == normalization_run_id)
            ).scalar_one()
            publication_counts = _publication_boundary_counts(session)
    finally:
        database.close()

    assert first.observation_id != second.observation_id
    assert first_snapshot.source_batch_id != second_snapshot.source_batch_id
    assert first_snapshot.normalization_run_id == second_snapshot.normalization_run_id
    assert source_batch_count == 2
    # 每次真实抓取仍保留其专属 raw/normalized 证据，不能跨 `SourceBatch` 借用 manifest。
    assert raw_manifest_count == 4
    assert normalization_count == 1
    assert quality_evaluation_count == 1
    assert quality_result_count == 1
    assert publication_counts == (0, 0, 0, 0)


@pytest.mark.integration
def test_repository_creates_new_normalization_and_quality_for_changed_snapshot_input() -> None:
    """任一载荷摘要变化必须产生新的研究态规范化和质量结论，仍不能发布。"""
    if os.environ.get("DATA_SYNC_RUN_INTEGRATION") != "1":
        pytest.skip("set DATA_SYNC_RUN_INTEGRATION=1 after starting local infrastructure")
    database = DatabaseClient.from_settings(load_settings())
    repository = SqlAlchemyIndexShadowRepository(database)
    identifier = IndexIdentifier(IndexAdministrator.CSI, "000300")
    items = (
        IndexObservedSnapshotItem(
            source_symbol="600000",
            source_name="浦发银行",
            source_exchange="SSE",
            source_industry="银行",
            weight_value=None,
            weight_kind=None,
        ),
    )
    try:
        first = repository.record_snapshot(
            identifier=identifier,
            observation_kind="constituent_current",
            source_as_of_date=None,
            items=items,
            source=_source(
                "index.constituent.snapshot",
                datetime(2026, 8, 1, 8, tzinfo=UTC),
                upstream_source="csindex",
                raw_payload_sha256="e" * 64,
                normalized_payload_sha256="f" * 64,
            ),
        )
        second = repository.record_snapshot(
            identifier=identifier,
            observation_kind="constituent_current",
            source_as_of_date=None,
            items=items,
            source=_source(
                "index.constituent.snapshot",
                datetime(2026, 8, 1, 8, 1, tzinfo=UTC),
                upstream_source="csindex",
                raw_payload_sha256="1" * 64,
                normalized_payload_sha256="2" * 64,
            ),
        )
        with database.session() as session:
            first_snapshot = session.get(IndexObservedSnapshot, first.observation_id)
            second_snapshot = session.get(IndexObservedSnapshot, second.observation_id)
            assert first_snapshot is not None
            assert second_snapshot is not None
            source_batch_ids = (first_snapshot.source_batch_id, second_snapshot.source_batch_id)
            normalization_run_ids = (
                first_snapshot.normalization_run_id,
                second_snapshot.normalization_run_id,
            )
            source_batch_count = session.execute(
                select(func.count())
                .select_from(SourceBatch)
                .where(SourceBatch.source_batch_id.in_(source_batch_ids))
            ).scalar_one()
            raw_manifest_count = session.execute(
                select(func.count())
                .select_from(RawPayloadManifest)
                .where(RawPayloadManifest.source_batch_id.in_(source_batch_ids))
            ).scalar_one()
            normalization_count = session.execute(
                select(func.count())
                .select_from(NormalizationRun)
                .where(NormalizationRun.normalization_run_id.in_(normalization_run_ids))
            ).scalar_one()
            quality_evaluation_count = session.execute(
                select(func.count())
                .select_from(QualityEvaluation)
                .where(QualityEvaluation.normalization_run_id.in_(normalization_run_ids))
            ).scalar_one()
            quality_result_count = session.execute(
                select(func.count())
                .select_from(QualityResult)
                .join(
                    QualityEvaluation,
                    QualityResult.evaluation_id == QualityEvaluation.evaluation_id,
                )
                .where(QualityEvaluation.normalization_run_id.in_(normalization_run_ids))
            ).scalar_one()
            publication_counts = _publication_boundary_counts(session)
    finally:
        database.close()

    assert first_snapshot.normalization_run_id != second_snapshot.normalization_run_id
    assert source_batch_count == 2
    assert raw_manifest_count == 4
    assert normalization_count == 2
    assert quality_evaluation_count == 2
    assert quality_result_count == 2
    assert publication_counts == (0, 0, 0, 0)


def _source(
    capability: str,
    observed_at: datetime,
    *,
    upstream_source: str = "cnindex",
    raw_payload_sha256: str | None = None,
    normalized_payload_sha256: str | None = None,
) -> IndexShadowSourceObservation:
    """构造关联到指定真实指数来源的最小双对象证据链。"""
    payload_hash = "a" * 64 if capability == "index.catalog.snapshot" else "b" * 64
    return IndexShadowSourceObservation(
        provider_id="integration-index",
        capability=capability,
        raw_payload_sha256=raw_payload_sha256 or payload_hash,
        raw_uri=f"s3://integration/{capability}/raw.json",
        raw_content_type="application/json",
        raw_byte_size=100,
        normalized_payload_sha256=normalized_payload_sha256 or "c" * 64,
        normalized_uri=f"s3://integration/{capability}/normalized.json",
        normalized_content_type="application/json",
        normalized_byte_size=80,
        observed_at=observed_at,
        upstream_source=upstream_source,
        adapter_version="integration-v1",
        schema_fingerprint="d" * 64,
    )


def _publication_boundary_counts(session: Session) -> tuple[int, int, int, int]:
    """回读 release、publication、canonical 血缘和 checkpoint，证明影子观察未越界。"""
    return (
        session.execute(select(func.count()).select_from(DatasetRelease)).scalar_one(),
        session.execute(select(func.count()).select_from(DatasetPublication)).scalar_one(),
        session.execute(select(func.count()).select_from(CanonicalRecordLineage)).scalar_one(),
        session.execute(select(func.count()).select_from(CanonicalCheckpoint)).scalar_one(),
    )
