"""PostgreSQL 板块三周期持久化的集成测试。"""

from __future__ import annotations

import os
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from service_data_sync.bootstrap.settings import load_settings
from service_data_sync.domain.sector import SectorBar, SectorIdentifier, SectorPeriod, SectorScheme
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.persistence.sector_market_data_repository import (
    SqlAlchemySectorMarketDataRepository,
)


@pytest.mark.integration
@pytest.mark.parametrize("period", tuple(SectorPeriod))
def test_repository_persists_each_direct_upstream_period(period: SectorPeriod) -> None:
    """日、周、月应分别写入其物理表并能从同周期读取当前修订。"""
    if os.environ.get("DATA_SYNC_RUN_INTEGRATION") != "1":
        pytest.skip("set DATA_SYNC_RUN_INTEGRATION=1 after starting local infrastructure")
    database = DatabaseClient.from_settings(load_settings())
    repository = SqlAlchemySectorMarketDataRepository(database)
    identifier = SectorIdentifier(SectorScheme.EASTMONEY_INDUSTRY, f"BK{period.value}")
    bar = SectorBar(
        period_end=date(2026, 6, 30),
        open_price=Decimal("10"),
        high_price=Decimal("11"),
        low_price=Decimal("9"),
        close_price=Decimal("10.5"),
        volume_value=Decimal("1000"),
        volume_unit="provider_native",
        amount_cny=Decimal("10500"),
        amplitude_percent=Decimal("20"),
        change_percent=Decimal("5"),
        change_amount=Decimal("0.5"),
        turnover_percent=Decimal("3"),
    )
    source_payload_sha256 = {
        SectorPeriod.DAY_1: "a",
        SectorPeriod.WEEK_1: "b",
        SectorPeriod.MONTH_1: "c",
    }[period] * 64
    normalized_payload_sha256 = {
        SectorPeriod.DAY_1: "d",
        SectorPeriod.WEEK_1: "e",
        SectorPeriod.MONTH_1: "f",
    }[period] * 64
    try:
        publication = repository.publish_bars(
            identifier=identifier,
            period=period,
            bars=(bar,),
            provider_id="integration-fixture",
            source_payload_sha256=source_payload_sha256,
            raw_uri=f"unretained://sha256/{source_payload_sha256}",
            raw_content_type="application/json",
            raw_byte_size=128,
            normalized_payload_sha256=normalized_payload_sha256,
            normalized_uri=f"unretained://sha256/{normalized_payload_sha256}",
            normalized_content_type="application/json",
            normalized_byte_size=96,
            observed_at=datetime(2026, 7, 1, tzinfo=UTC),
        )
        bars = repository.list_bars(
            sector_id=publication.sector.sector_id,
            period=period,
            start=date(2026, 6, 1),
            end=date(2026, 6, 30),
        )
    finally:
        database.close()

    assert publication.inserted_count + publication.unchanged_count == 1
    assert bars[-1][0].close_price == Decimal("10.5")
    assert bars[-1][1:] == (1, True)
