"""指数 P0-A 影子观察仓储的 PostgreSQL 集成测试。"""

from __future__ import annotations

import os
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from service_data_sync.application.ports.index_shadow import (
    IndexCatalogObservationEntry,
    IndexObservedSnapshotItem,
    IndexShadowSourceObservation,
)
from service_data_sync.bootstrap.settings import load_settings
from service_data_sync.domain.index import IndexAdministrator, IndexIdentifier
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.database.models.canonical import (
    NormalizationRun,
    QualityEvaluation,
    QualityResult,
    RawPayloadManifest,
)
from service_data_sync.infrastructure.database.models.index import (
    IndexCatalogObservation,
    IndexObservedSnapshot,
)
from service_data_sync.infrastructure.database.models.index import (
    IndexObservedSnapshotItem as IndexObservedSnapshotItemModel,
)
from service_data_sync.infrastructure.database.models.provenance.source_batch import SourceBatch
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


def _source(capability: str, observed_at: datetime) -> IndexShadowSourceObservation:
    """构造关联到国证真实来源的最小双对象证据链。"""
    payload_hash = "a" * 64 if capability == "index.catalog.snapshot" else "b" * 64
    return IndexShadowSourceObservation(
        provider_id="integration-index",
        capability=capability,
        raw_payload_sha256=payload_hash,
        raw_uri=f"s3://integration/{capability}/raw.json",
        raw_content_type="application/json",
        raw_byte_size=100,
        normalized_payload_sha256="c" * 64,
        normalized_uri=f"s3://integration/{capability}/normalized.json",
        normalized_content_type="application/json",
        normalized_byte_size=80,
        observed_at=observed_at,
        upstream_source="cnindex",
        adapter_version="integration-v1",
        schema_fingerprint="d" * 64,
    )
