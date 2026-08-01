"""资金流研究排行真实 PostgreSQL 落库集成测试。"""

from __future__ import annotations

import asyncio
import hashlib
import os
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select

from service_data_sync.application.money_flow.sync import MoneyFlowSyncService
from service_data_sync.application.ports.money_flow import MoneyFlowSourceObservation
from service_data_sync.bootstrap.settings import load_settings
from service_data_sync.domain.equity import Exchange
from service_data_sync.domain.money_flow import (
    MoneyFlowBucketDefinition,
    MoneyFlowFinality,
    MoneyFlowMeasure,
    MoneyFlowMethodology,
    MoneyFlowRankingItem,
    MoneyFlowRankingSnapshot,
    MoneyFlowScope,
    MoneyFlowScopeType,
    MoneyFlowSemanticFamily,
    MoneyFlowWindowType,
)
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.database.models.canonical import RawPayloadManifest
from service_data_sync.infrastructure.database.models.money_flow import (
    MoneyFlowRankingResearchItem,
    MoneyFlowRankingResearchMetric,
    MoneyFlowRankingResearchObservation,
)
from service_data_sync.infrastructure.persistence.money_flow_repository import (
    SqlAlchemyMoneyFlowRepository,
)
from service_data_sync.infrastructure.providers.akshare.eastmoney_http import (
    install_eastmoney_request_compatibility,
)
from service_data_sync.infrastructure.providers.akshare.money_flow import (
    AkshareEastmoneyMoneyFlowAdapter,
)


@pytest.mark.integration
def test_repository_persists_incomplete_ranking_as_research_with_unretained_manifests() -> None:
    """真实不完整排行必须入研究表和双摘要清单，且不得伪造公开 publication。"""
    if os.environ.get("DATA_SYNC_RUN_INTEGRATION") != "1":
        pytest.skip("set DATA_SYNC_RUN_INTEGRATION=1 after starting local infrastructure")
    database = DatabaseClient.from_settings(load_settings())
    repository = SqlAlchemyMoneyFlowRepository(database)
    observed_at = datetime(2026, 8, 1, 10, tzinfo=UTC)
    raw_digest = hashlib.sha256(b"integration-money-flow-research-raw").hexdigest()
    normalized_digest = hashlib.sha256(b"integration-money-flow-research-normalized").hexdigest()
    source = MoneyFlowSourceObservation(
        provider_id="integration-money-flow-research",
        capability="money_flow.trade_direction.ranking.equity.raw",
        source_payload_sha256=raw_digest,
        raw_uri=f"unretained://sha256/{raw_digest}",
        raw_content_type="application/json",
        raw_byte_size=36,
        normalized_payload_sha256=normalized_digest,
        normalized_uri=f"unretained://sha256/{normalized_digest}",
        normalized_content_type="application/json",
        normalized_byte_size=43,
        observed_at=observed_at,
        upstream_source="AKShare",
        adapter_version="integration-fixture-v1",
        schema_fingerprint=hashlib.sha256(b"quant-v2.money-flow-ranking.v1").hexdigest(),
    )
    methodology = MoneyFlowMethodology(
        public_key="integration-money-flow-research",
        version="1",
        status="research",
        production_enabled=False,
        adapter_provider="integration-money-flow-research",
        upstream_source="AKShare",
        source_dataset="integration-fixture",
        semantic_family=MoneyFlowSemanticFamily.TRADE_DIRECTION,
        scope_types=(MoneyFlowScopeType.EQUITY,),
        universe_ids=("provider-page",),
        windows=((MoneyFlowWindowType.SUPPLIER_DAY, 1, "供应商当日排行"),),
        buckets=(MoneyFlowBucketDefinition(code="all", label="全部资金"),),
        supported_measures=frozenset({MoneyFlowMeasure.NET_AMOUNT}),
        ratio_denominator="供应商未披露",
        direction_definition="供应商报告主动交易方向",
        finality=MoneyFlowFinality.UNKNOWN,
        currency=None,
        raw_amount_unit="source_unknown",
        standard_amount_unit=None,
        conversion_version=None,
    )
    snapshot = MoneyFlowRankingSnapshot(
        target_trade_date=date(2026, 7, 31),
        observed_at=observed_at,
        source_cutoff_at=observed_at,
        scope_type=MoneyFlowScopeType.EQUITY,
        universe_id="provider-page",
        window_type=MoneyFlowWindowType.SUPPLIER_DAY,
        window_size=1,
        ranking_bucket="all",
        ranking_basis="supplier_reported_order",
        completeness_basis="sdk_returned",
        is_complete=False,
        items=(
            MoneyFlowRankingItem(
                supplier_position=1,
                scope=MoneyFlowScope(
                    scope_type=MoneyFlowScopeType.EQUITY,
                    exchange=Exchange.SSE,
                    symbol="600000",
                    name="浦发银行",
                ),
                bucket="all",
                gross_inflow=None,
                gross_outflow=None,
                net_amount=Decimal("100000"),
                net_ratio=None,
            ),
        ),
    )
    try:
        result = repository.publish_ranking(
            methodology=methodology,
            snapshot=snapshot,
            source=source,
        )
        with database.session() as session:
            observation = session.execute(
                select(MoneyFlowRankingResearchObservation)
                .where(
                    MoneyFlowRankingResearchObservation.normalized_payload_sha256
                    == normalized_digest
                )
                .order_by(MoneyFlowRankingResearchObservation.created_at.desc())
                .limit(1)
            ).scalar_one()
            manifest_uris = (
                session.execute(
                    select(RawPayloadManifest.object_uri)
                    .where(RawPayloadManifest.source_batch_id == observation.source_batch_id)
                    .order_by(RawPayloadManifest.role)
                )
                .scalars()
                .all()
            )
            item = session.execute(
                select(MoneyFlowRankingResearchItem).where(
                    MoneyFlowRankingResearchItem.target_trade_date == observation.target_trade_date,
                    MoneyFlowRankingResearchItem.research_observation_id
                    == observation.research_observation_id,
                )
            ).scalar_one()
            metric = session.execute(
                select(MoneyFlowRankingResearchMetric).where(
                    MoneyFlowRankingResearchMetric.target_trade_date
                    == observation.target_trade_date,
                    MoneyFlowRankingResearchMetric.research_observation_id
                    == observation.research_observation_id,
                )
            ).scalar_one()
    finally:
        database.close()

    assert result.published is False
    assert result.data_version is None
    assert result.quality_status == "partial"
    assert observation.status == "research"
    assert observation.is_complete is False
    assert manifest_uris == [
        f"unretained://sha256/{normalized_digest}",
        f"unretained://sha256/{raw_digest}",
    ]
    assert item.source_exchange == "SSE"
    assert item.source_symbol == "600000"
    assert metric.net_amount == Decimal("100000")


@pytest.mark.integration
def test_live_akshare_equity_ranking_reaches_research_repository_without_success_s3_write() -> None:
    """真实东财个股排行必须进入私有 research 表，成功路径只写摘要清单而不写 S3 字节。"""
    requested_batches = {
        value.strip().lower()
        for value in os.environ.get("DATA_SYNC_AKSHARE_LIVE_BATCH", "").split(",")
        if value.strip()
    }
    if os.environ.get("DATA_SYNC_RUN_INTEGRATION") != "1" or not (
        "all" in requested_batches or "money-flow" in requested_batches
    ):
        pytest.skip(
            "set DATA_SYNC_RUN_INTEGRATION=1 and "
            "DATA_SYNC_AKSHARE_LIVE_BATCH=money-flow after starting local infrastructure"
        )
    settings = load_settings()
    database = DatabaseClient.from_settings(settings)
    repository = SqlAlchemyMoneyFlowRepository(database)
    install_eastmoney_request_compatibility(
        request_timeout_seconds=settings.akshare_request_timeout_seconds
    )
    service = MoneyFlowSyncService(
        source=AkshareEastmoneyMoneyFlowAdapter(
            request_timeout_seconds=settings.akshare_request_timeout_seconds
        ),
        repository=repository,
    )
    target_date = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    while target_date.weekday() >= 5:
        target_date -= timedelta(days=1)
    try:
        result = asyncio.run(
            service.sync(
                capability="money_flow.order_size.ranking.equity.raw",
                parameters=(("indicator", "今日"), ("targetDate", target_date.isoformat())),
            )
        )
        with database.session() as session:
            raw_manifest = session.execute(
                select(RawPayloadManifest)
                .where(
                    RawPayloadManifest.object_uri
                    == f"unretained://sha256/{result.source_payload_sha256}"
                )
                .order_by(RawPayloadManifest.fetched_at.desc())
                .limit(1)
            ).scalar_one()
            observation = session.execute(
                select(MoneyFlowRankingResearchObservation).where(
                    MoneyFlowRankingResearchObservation.source_batch_id
                    == raw_manifest.source_batch_id
                )
            ).scalar_one()
            manifest_uris = (
                session.execute(
                    select(RawPayloadManifest.object_uri)
                    .where(RawPayloadManifest.source_batch_id == raw_manifest.source_batch_id)
                    .order_by(RawPayloadManifest.role)
                )
                .scalars()
                .all()
            )
            item_count = session.execute(
                select(func.count())
                .select_from(MoneyFlowRankingResearchItem)
                .where(
                    MoneyFlowRankingResearchItem.target_trade_date == observation.target_trade_date,
                    MoneyFlowRankingResearchItem.research_observation_id
                    == observation.research_observation_id,
                )
            ).scalar_one()
            empty_metric_count = session.execute(
                select(func.count())
                .select_from(MoneyFlowRankingResearchMetric)
                .where(
                    MoneyFlowRankingResearchMetric.target_trade_date
                    == observation.target_trade_date,
                    MoneyFlowRankingResearchMetric.research_observation_id
                    == observation.research_observation_id,
                    MoneyFlowRankingResearchMetric.gross_inflow.is_(None),
                    MoneyFlowRankingResearchMetric.gross_outflow.is_(None),
                    MoneyFlowRankingResearchMetric.net_amount.is_(None),
                    MoneyFlowRankingResearchMetric.net_ratio.is_(None),
                )
            ).scalar_one()
    finally:
        database.close()

    assert result.publication.published is False
    assert result.publication.data_version is None
    assert result.publication.quality_status == "partial"
    assert observation.status == "research"
    assert observation.is_complete is False
    assert item_count > 0
    assert empty_metric_count == 0
    assert len(manifest_uris) == 2
    assert all(uri.startswith("unretained://sha256/") for uri in manifest_uris)
