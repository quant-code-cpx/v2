"""AKShare 港通市场统计 research-only 真实持久化与可选 live 探针测试。"""

from __future__ import annotations

import asyncio
import hashlib
import os
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select

from service_data_sync.application.ports.stock_connect_market_stat_research import (
    StockConnectMarketStatResearchRecord,
    StockConnectMarketStatResearchSourceObservation,
)
from service_data_sync.application.stock_connect_research.market_stat_sync import (
    StockConnectMarketStatResearchSyncService,
)
from service_data_sync.bootstrap.settings import load_settings
from service_data_sync.domain.stock_connect import StockConnectChannel
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.database.models.canonical import (
    CanonicalDataset,
    DatasetRelease,
    NormalizationRun,
    NormalizedRecordManifest,
    QualityEvaluation,
    QualityResult,
    RawPayloadManifest,
)
from service_data_sync.infrastructure.database.models.market import (
    StockConnectMarketStatResearchBatch,
    StockConnectMarketStatResearchObservation,
)
from service_data_sync.infrastructure.database.models.provenance.source_batch import SourceBatch
from service_data_sync.infrastructure.database.models.publication.dataset_publication import (
    DatasetPublication,
)
from service_data_sync.infrastructure.object_storage.client import ObjectStorageClient
from service_data_sync.infrastructure.object_storage.raw_payload_store import S3RawPayloadStore
from service_data_sync.infrastructure.persistence import (
    stock_connect_market_stat_research_repository as market_stat_research_repository,
)
from service_data_sync.infrastructure.providers.akshare.p0_market_data import (
    AkshareP0MarketDataAdapter,
)

_CAPABILITY = "market.stock_connect.market_stat.reported"
_DATASET_CODE = "market.stock_connect.market_stat.research"
_LIVE_PROBE_ENV = "DATA_SYNC_RUN_AKSHARE_PROBE"


@pytest.mark.integration
def test_repository_persists_real_probe_shape_as_research_without_publication() -> None:
    """真实 PostgreSQL 必须保存实际 AKShare 字段形状，且 research 永不产生公开版本。"""
    if os.environ.get("DATA_SYNC_RUN_INTEGRATION") != "1":
        pytest.skip("set DATA_SYNC_RUN_INTEGRATION=1 after starting local infrastructure")
    database = DatabaseClient.from_settings(load_settings())
    repository = market_stat_research_repository.SqlAlchemyStockConnectMarketStatResearchRepository(
        database
    )
    raw_digest = hashlib.sha256(b"stock-connect-market-stat-research-raw").hexdigest()
    normalized_digest = hashlib.sha256(b"stock-connect-market-stat-research-normalized").hexdigest()
    source = StockConnectMarketStatResearchSourceObservation(
        provider_id="integration-akshare",
        capability=_CAPABILITY,
        raw_payload_sha256=raw_digest,
        raw_uri=f"unretained://sha256/{raw_digest}",
        raw_content_type="application/json",
        raw_byte_size=39,
        normalized_payload_sha256=normalized_digest,
        normalized_uri=f"unretained://sha256/{normalized_digest}",
        normalized_content_type="application/vnd.quant-v2.stock-connect-market-daily.v1+json",
        normalized_byte_size=46,
        observed_at=datetime(2026, 8, 1, 10, tzinfo=UTC),
        upstream_source="eastmoney.stock-connect",
        adapter_version="integration-akshare-market-stat-v1",
        schema_fingerprint=hashlib.sha256(b"quant-v2.stock-connect-market-daily.v1").hexdigest(),
    )
    record = StockConnectMarketStatResearchRecord(
        trade_date=date(2026, 7, 31),
        buy_amount=Decimal("123.45"),
        sell_amount=Decimal("100.00"),
        turnover_amount=None,
        net_buy_amount=Decimal("23.45"),
        quota_balance=Decimal("4976.55"),
        currency="CNY",
        availability_status="COMPLETE",
        field_availability=None,
    )
    try:
        result = repository.record_market_statistics(
            channel=StockConnectChannel("SH", "NORTHBOUND"),
            records=(record,),
            source=source,
        )
        with database.session() as session:
            batch = session.execute(
                select(StockConnectMarketStatResearchBatch).where(
                    StockConnectMarketStatResearchBatch.research_batch_id
                    == result.research_batch_id
                )
            ).scalar_one()
            observation = session.execute(
                select(StockConnectMarketStatResearchObservation).where(
                    StockConnectMarketStatResearchObservation.research_batch_id
                    == result.research_batch_id
                )
            ).scalar_one()
            source_batch = session.execute(
                select(SourceBatch).where(SourceBatch.source_batch_id == result.source_batch_id)
            ).scalar_one()
            manifests = (
                session.execute(
                    select(RawPayloadManifest.object_uri)
                    .where(RawPayloadManifest.source_batch_id == result.source_batch_id)
                    .order_by(RawPayloadManifest.role)
                )
                .scalars()
                .all()
            )
            normalization = session.execute(
                select(NormalizationRun).where(
                    NormalizationRun.normalization_run_id == batch.normalization_run_id
                )
            ).scalar_one()
            normalized_records = (
                session.execute(
                    select(NormalizedRecordManifest).where(
                        NormalizedRecordManifest.normalization_run_id == batch.normalization_run_id
                    )
                )
                .scalars()
                .all()
            )
            evaluation = session.execute(
                select(QualityEvaluation).where(
                    QualityEvaluation.normalization_run_id == batch.normalization_run_id
                )
            ).scalar_one()
            quality_results = (
                session.execute(
                    select(QualityResult).where(
                        QualityResult.evaluation_id == evaluation.evaluation_id
                    )
                )
                .scalars()
                .all()
            )
            dataset = session.execute(
                select(CanonicalDataset).where(CanonicalDataset.dataset_id == batch.dataset_id)
            ).scalar_one()
            release_ids = (
                session.execute(
                    select(DatasetRelease.release_id).where(
                        DatasetRelease.dataset_id == batch.dataset_id
                    )
                )
                .scalars()
                .all()
            )
            publication_ids = (
                session.execute(
                    select(DatasetPublication.publication_id).where(
                        DatasetPublication.dataset == _DATASET_CODE
                    )
                )
                .scalars()
                .all()
            )
    finally:
        database.close()

    assert result.inserted_count == 1
    assert result.quality_status == "passed"
    assert batch.status == "research"
    assert batch.quality_status == "passed"
    assert dataset.code == _DATASET_CODE
    assert dataset.status == "research"
    assert source_batch.raw_uri == f"unretained://sha256/{raw_digest}"
    assert manifests == [
        f"unretained://sha256/{normalized_digest}",
        f"unretained://sha256/{raw_digest}",
    ]
    assert normalization.status == "passed"
    assert len(normalized_records) == 1
    assert evaluation.status == "passed"
    assert {quality_result.passed for quality_result in quality_results} == {True}
    assert observation.turnover_amount is None
    assert observation.field_availability is None
    assert observation.net_buy_amount == Decimal("23.450000")
    assert release_ids == []
    assert publication_ids == []


@pytest.mark.integration
def test_live_akshare_adapter_reaches_research_repository_without_success_s3_write() -> None:
    """真实 AKShare 批次须经 adapter、应用服务和 PostgreSQL，成功时不增加任何 S3 对象。"""
    if os.environ.get("DATA_SYNC_RUN_INTEGRATION") != "1":
        pytest.skip("set DATA_SYNC_RUN_INTEGRATION=1 after starting local infrastructure")
    if os.environ.get(_LIVE_PROBE_ENV) != "1":
        pytest.skip(f"set {_LIVE_PROBE_ENV}=1 to run the external AKShare research probe")
    settings = load_settings()
    database = DatabaseClient.from_settings(settings)
    object_storage = ObjectStorageClient.from_settings(settings)
    try:
        object_keys_before = _object_keys(object_storage)
        result = asyncio.run(
            StockConnectMarketStatResearchSyncService(
                source=AkshareP0MarketDataAdapter(request_timeout_seconds=90),
                repository=market_stat_research_repository.SqlAlchemyStockConnectMarketStatResearchRepository(
                    database
                ),
                failure_evidence_store=S3RawPayloadStore(
                    object_storage,
                    retention_mode="MANIFEST_ONLY",
                ),
            ).sync(
                channel=StockConnectChannel("SH", "NORTHBOUND"),
                start=date(2026, 7, 31),
                end=date(2026, 7, 31),
            )
        )
        object_keys_after = _object_keys(object_storage)
        with database.session() as session:
            observation = session.execute(
                select(StockConnectMarketStatResearchObservation).where(
                    StockConnectMarketStatResearchObservation.research_batch_id
                    == result.batch.research_batch_id
                )
            ).scalar_one()
            publication_ids = (
                session.execute(
                    select(DatasetPublication.publication_id).where(
                        DatasetPublication.dataset == _DATASET_CODE
                    )
                )
                .scalars()
                .all()
            )
    finally:
        object_storage.close()
        database.close()

    assert result.capability == _CAPABILITY
    assert result.batch.inserted_count == 1
    assert result.batch.quality_status == "passed"
    assert observation.trade_date == date(2026, 7, 31)
    assert observation.turnover_amount is None
    assert observation.field_availability is None
    assert object_keys_after == object_keys_before
    assert publication_ids == []


def _object_keys(object_storage: ObjectStorageClient) -> set[str]:
    """读取测试桶现有对象键，验证成功路径不会留下 raw、normalized 或失败清单。"""
    response = object_storage.client.list_objects_v2(Bucket=object_storage.bucket)
    contents = response.get("Contents", [])
    if not isinstance(contents, list):
        raise ValueError("S3 list_objects_v2 contents is invalid")
    return {
        key
        for item in contents
        if isinstance(item, dict) and isinstance((key := item.get("Key")), str)
    }
