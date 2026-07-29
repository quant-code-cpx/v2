"""PostgreSQL P0 日线修订与发布持久化的集成测试。"""

from __future__ import annotations

import os
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import delete

from service_data_sync.bootstrap.settings import load_settings
from service_data_sync.domain.equity import EquityDailyBar, EquityIdentifier
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.database.models.publication import (
    dataset_availability_observation,
)
from service_data_sync.infrastructure.persistence.equity_market_data_repository import (
    SqlAlchemyEquityMarketDataRepository,
)


@pytest.mark.integration
def test_repository_persists_and_reads_one_recent_month_bar() -> None:
    """将 2026 年 6 月日线写入日期分区，再读取其当前修订。"""
    if os.environ.get("DATA_SYNC_RUN_INTEGRATION") != "1":
        pytest.skip("set DATA_SYNC_RUN_INTEGRATION=1 after starting local infrastructure")
    database = DatabaseClient.from_settings(load_settings())
    repository = SqlAlchemyEquityMarketDataRepository(database)
    identifier = EquityIdentifier.parse("SSE.600519")
    bar = EquityDailyBar(
        trade_date=date(2026, 6, 30),
        open_price=Decimal("10"),
        high_price=Decimal("11"),
        low_price=Decimal("9"),
        close_price=Decimal("10.5"),
        volume_shares=1_000,
        amount_cny=Decimal("10500"),
        turnover_rate=None,
    )
    try:
        publication = repository.publish_daily_bars(
            identifier=identifier,
            bars=(bar,),
            provider_id="integration-fixture",
            source_payload_sha256="c" * 64,
            raw_uri="s3://integration-fixture/recent-month.json",
            observed_at=datetime(2026, 7, 1, tzinfo=UTC),
        )
        bars = repository.list_daily_bars(
            instrument_id=publication.instrument.instrument_id,
            start=date(2026, 6, 1),
            end=date(2026, 6, 30),
        )
    finally:
        database.close()

    assert publication.inserted_count + publication.unchanged_count == 1
    assert bars[-1][0].close_price == Decimal("10.5")
    assert bars[-1][1:] == (1, True)


@pytest.mark.integration
def test_repository_persists_current_empty_or_unavailable_daily_bar_observation() -> None:
    """空集和 AKShare 不可用状态应独立入库，且不需要伪造事实或证券身份。"""
    if os.environ.get("DATA_SYNC_RUN_INTEGRATION") != "1":
        pytest.skip("set DATA_SYNC_RUN_INTEGRATION=1 after starting local infrastructure")
    database = DatabaseClient.from_settings(load_settings())
    repository = SqlAlchemyEquityMarketDataRepository(database)
    identifier = EquityIdentifier.parse("SSE.000001")
    start = date(2030, 1, 1)
    end = date(2030, 1, 31)
    partition_key = "SSE.000001:2030-01-01:2030-01-31"
    try:
        repository.record_daily_bar_availability(
            identifier=identifier,
            start=start,
            end=end,
            availability="empty",
            reason_code="no_matching_facts",
            provider_id="integration-fixture",
            observed_at=datetime(2030, 2, 1, tzinfo=UTC),
        )
        repository.record_daily_bar_availability(
            identifier=identifier,
            start=start,
            end=end,
            availability="source_unavailable",
            reason_code="unavailable",
            provider_id="integration-fixture",
            observed_at=datetime(2030, 2, 2, tzinfo=UTC),
        )
        repository.record_daily_bar_availability(
            identifier=identifier,
            start=start,
            end=end,
            availability="source_unavailable",
            reason_code="unavailable",
            provider_id="integration-fixture",
            observed_at=datetime(2030, 2, 2, tzinfo=UTC),
        )
        repository.clear_daily_bar_availability(
            identifier=identifier,
            start=start,
            end=end,
            cleared_at=datetime(2030, 2, 3, tzinfo=UTC),
        )
        observation = repository.get_daily_bar_availability(
            identifier=identifier, start=start, end=end
        )
    finally:
        # 集成环境可能复用个人开发库；清理固定测试分区，避免留下运维假信号。
        with database.transaction() as connection:
            connection.execute(
                delete(dataset_availability_observation.DatasetAvailabilityObservation).where(
                    dataset_availability_observation.DatasetAvailabilityObservation.dataset
                    == "equity.bar.1d.raw",
                    dataset_availability_observation.DatasetAvailabilityObservation.partition_key
                    == partition_key,
                )
            )
        database.close()

    assert observation is None
