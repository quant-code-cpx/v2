"""PostgreSQL 板块 EOD checkpoint、revision、排行与 raw replay 的集成测试。"""

from __future__ import annotations

import os
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from service_data_sync.application.ports.sector_eod import (
    PublishedSectorEodSnapshot,
    SectorEodQualityResult,
)
from service_data_sync.bootstrap.settings import load_settings
from service_data_sync.domain.sector import (
    SectorCatalogEntry,
    SectorEodQuote,
    SectorEodSort,
    SectorIdentifier,
    SectorScheme,
    SortOrder,
)
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.persistence.sector_eod_repository import (
    SqlAlchemySectorEodRepository,
)
from service_data_sync.infrastructure.persistence.sector_market_data_repository import (
    SqlAlchemySectorMarketDataRepository,
)


@pytest.mark.integration
def test_repository_publishes_checkpointed_eod_and_reuses_same_content_revision() -> None:
    """完整目录覆盖的 EOD 应原子发布；同内容新 source batch 只完成 checkpoint 不新增版本。"""
    if os.environ.get("DATA_SYNC_RUN_INTEGRATION") != "1":
        pytest.skip("set DATA_SYNC_RUN_INTEGRATION=1 after starting local infrastructure")
    database = DatabaseClient.from_settings(load_settings())
    catalog = SqlAlchemySectorMarketDataRepository(database)
    repository = SqlAlchemySectorEodRepository(database)
    scheme = SectorScheme.EASTMONEY_INDUSTRY
    trade_date = date(2026, 7, 27)
    observed_at = datetime(2026, 7, 27, 8, 20, tzinfo=UTC)
    cutoff_at = datetime(2026, 7, 27, 8, 15, tzinfo=UTC)
    quote = _quote()
    try:
        catalog.publish_catalog(
            scheme=scheme,
            entries=(SectorCatalogEntry(quote.identifier, quote.name),),
            provider_id="integration-fixture-catalog",
            source_payload_sha256="a" * 64,
            raw_uri="s3://integration-fixture/catalog.json",
            observed_at=observed_at,
        )
        first = _publish(
            repository,
            scheme=scheme,
            trade_date=trade_date,
            cutoff_at=cutoff_at,
            observed_at=observed_at,
            quote=quote,
            source_hash="b" * 64,
            raw_uri="s3://integration-fixture/eod-first.json",
        )
        ranked = repository.list_ranked_quotes(
            snapshot_id=first.snapshot.snapshot_id,
            sort=SectorEodSort.CHANGE_PERCENT,
            order=SortOrder.DESC,
            after_position=None,
            limit=10,
        )
        historical_reference = repository.get_historical_reference(
            scheme=scheme,
            before_trade_date=date(2026, 7, 28),
        )
        _quarantine(
            repository,
            scheme=scheme,
            trade_date=trade_date,
            cutoff_at=cutoff_at,
            observed_at=datetime(2026, 7, 27, 8, 23, tzinfo=UTC),
            quote=quote,
        )
        visible_after_quarantine = repository.get_published_snapshot(
            scheme=scheme,
            trade_date=trade_date,
        )
        repeated = _publish(
            repository,
            scheme=scheme,
            trade_date=trade_date,
            cutoff_at=cutoff_at,
            observed_at=datetime(2026, 7, 27, 8, 25, tzinfo=UTC),
            quote=quote,
            source_hash="c" * 64,
            raw_uri="s3://integration-fixture/eod-repeat.json",
        )
    finally:
        database.close()

    assert first.inserted is True
    assert ranked[0].quote.identifier == quote.identifier
    assert ranked[0].rank == 1
    assert historical_reference is not None
    assert historical_reference.trade_date == trade_date
    assert historical_reference.market_values[quote.identifier.code] == quote.market_value
    assert visible_after_quarantine is not None
    assert visible_after_quarantine.data_version == first.snapshot.data_version
    assert repeated.inserted is False
    assert repeated.snapshot.data_version == first.snapshot.data_version


def _publish(
    repository: SqlAlchemySectorEodRepository,
    *,
    scheme: SectorScheme,
    trade_date: date,
    cutoff_at: datetime,
    observed_at: datetime,
    quote: SectorEodQuote,
    source_hash: str,
    raw_uri: str,
) -> PublishedSectorEodSnapshot:
    """用真实 checkpoint/source batch 执行一次 EOD 发布，避免集成测试绕过 fencing 路径。"""
    run = repository.start_run(scheme=scheme, trade_date=trade_date, reuse_archived_raw=False)
    observation = repository.record_archived_observation(
        run=run,
        provider_id="integration-fixture-eod",
        source_payload_sha256=source_hash,
        raw_uri=raw_uri,
        observed_at=observed_at,
        adapter_version="integration-v1",
        schema_fingerprint="d" * 64,
    )
    repository.mark_normalized(run=run)
    return repository.publish_snapshot(
        scheme=scheme,
        trade_date=trade_date,
        source_cutoff_at=cutoff_at,
        observed_at=observed_at,
        quotes=(quote,),
        provider_id=observation.provider_id,
        source_payload_sha256=source_hash,
        raw_uri=observation.raw_uri,
        adapter_version=observation.adapter_version,
        schema_fingerprint=observation.schema_fingerprint,
        run=run,
        source_batch_id=observation.source_batch_id,
    )


def _quarantine(
    repository: SqlAlchemySectorEodRepository,
    *,
    scheme: SectorScheme,
    trade_date: date,
    cutoff_at: datetime,
    observed_at: datetime,
    quote: SectorEodQuote,
) -> None:
    """写入阻断质量候选，验证它不影响同分区已发布 consumer version。"""
    run = repository.start_run(scheme=scheme, trade_date=trade_date, reuse_archived_raw=False)
    observation = repository.record_archived_observation(
        run=run,
        provider_id="integration-fixture-eod",
        source_payload_sha256="e" * 64,
        raw_uri="s3://integration-fixture/eod-quarantined.json",
        observed_at=observed_at,
        adapter_version="integration-v1",
        schema_fingerprint="d" * 64,
    )
    repository.mark_normalized(run=run)
    repository.store_quarantined_snapshot(
        scheme=scheme,
        trade_date=trade_date,
        source_cutoff_at=cutoff_at,
        observed_at=observed_at,
        quotes=(quote,),
        provider_id=observation.provider_id,
        source_payload_sha256="e" * 64,
        raw_uri=observation.raw_uri,
        adapter_version=observation.adapter_version,
        schema_fingerprint=observation.schema_fingerprint,
        run=run,
        source_batch_id=observation.source_batch_id,
        quality_results=(
            SectorEodQualityResult(
                rule_code="cross-day-content-stale",
                severity="blocking",
                passed=False,
                actual={"previousTradeDate": "2026-07-26"},
                threshold={"mustDiffer": "true"},
            ),
        ),
    )
    repository.mark_failed(run=run, error_code="schema")


def _quote() -> SectorEodQuote:
    """构造覆盖完整率、可用率和动态排行的单条行业 EOD 标准报价。"""
    return SectorEodQuote(
        identifier=SectorIdentifier(SectorScheme.EASTMONEY_INDUSTRY, "BKEOD01"),
        name="EOD 集成测试板块",
        latest_value=Decimal("1000"),
        change_value=Decimal("10"),
        change_percent=Decimal("1"),
        market_value=Decimal("1000000"),
        turnover_percent=Decimal("3"),
        advancers=10,
        decliners=3,
        leader_name="测试证券",
        leader_change_percent=Decimal("5"),
    )
