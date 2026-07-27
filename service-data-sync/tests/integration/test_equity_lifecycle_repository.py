"""显式上市生命周期双时间持久化的 PostgreSQL 集成测试。"""

from __future__ import annotations

import os
from datetime import UTC, date, datetime
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Connection

from service_data_sync.bootstrap.settings import load_settings
from service_data_sync.domain.equity import EquityIdentifier, Exchange
from service_data_sync.domain.equity_master import (
    EquityCatalogEntry,
    EquityLifecycleEntry,
    EquityLifecycleEvidenceKind,
    EquityLifecycleStatus,
)
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.persistence.equity_lifecycle_repository import (
    SqlAlchemyEquityLifecycleRepository,
)
from service_data_sync.infrastructure.persistence.equity_master_repository import (
    SqlAlchemyEquityMasterRepository,
)


@pytest.mark.integration
def test_repository_persists_explicit_delisting_as_bitemporal_revision() -> None:
    """明确退市应关闭 LISTED 有效期、追加 DELISTED 并推进交易所版本。"""
    if os.environ.get("DATA_SYNC_RUN_INTEGRATION") != "1":
        pytest.skip("set DATA_SYNC_RUN_INTEGRATION=1 after starting local infrastructure")
    database = DatabaseClient.from_settings(load_settings())
    catalog = SqlAlchemyEquityMasterRepository(database)
    lifecycle = SqlAlchemyEquityLifecycleRepository(database)
    identifier = EquityIdentifier.parse("SSE.600519")
    try:
        catalog.publish_catalog(
            exchange=Exchange.SSE,
            target_date=date(2026, 7, 1),
            entries=(
                EquityCatalogEntry(
                    identifier=identifier,
                    name="集成测试证券",
                    listed_on=date(2001, 8, 27),
                ),
            ),
            provider_id="integration-fixture-catalog",
            source_payload_sha256="a" * 64,
            raw_uri="s3://integration-fixture/catalog.json",
            observed_at=datetime(2026, 7, 1, tzinfo=UTC),
            upstream_source="integration-fixture",
            adapter_version="test-v1",
            schema_fingerprint="b" * 64,
        )
        publication = lifecycle.publish_lifecycle(
            exchange=Exchange.SSE,
            target_date=date(2026, 7, 2),
            entries=(
                EquityLifecycleEntry(
                    identifier=identifier,
                    status=EquityLifecycleStatus.DELISTED,
                    effective_on=date(2026, 7, 2),
                    evidence_kind=EquityLifecycleEvidenceKind.EXPLICIT_DELISTING,
                    listed_on=date(2001, 8, 27),
                    delisted_on=date(2026, 7, 2),
                ),
            ),
            provider_id="integration-fixture-lifecycle",
            source_payload_sha256="c" * 64,
            raw_uri="s3://integration-fixture/lifecycle.json",
            observed_at=datetime(2026, 7, 2, tzinfo=UTC),
            upstream_source="integration-fixture",
            adapter_version="test-v1",
            schema_fingerprint="d" * 64,
        )
        with database.engine.connect() as connection:
            rows = (
                connection.execute(
                    text(
                        """
                        SELECT status, effective_from, effective_to, delisted_on, evidence_kind
                        FROM equity_listing_status_version
                        WHERE security_id = (
                          SELECT security_id
                          FROM equity_instrument
                          WHERE instrument_id = :instrument_id
                        )
                        ORDER BY effective_from
                        """
                    ),
                    {"instrument_id": _instrument_id(connection, identifier)},
                )
                .mappings()
                .all()
            )
    finally:
        database.close()

    assert publication.inserted_count == 1
    assert [(row["status"], row["effective_to"]) for row in rows] == [
        ("LISTED", date(2026, 7, 2)),
        ("DELISTED", None),
    ]
    assert rows[-1]["delisted_on"] == date(2026, 7, 2)
    assert rows[-1]["evidence_kind"] == "EXPLICIT_DELISTING"


def _instrument_id(connection: Connection, identifier: EquityIdentifier) -> UUID:
    """读取集成目录刚建立的内部 UUID，避免测试猜测自增主键。"""
    row = (
        connection.execute(
            text(
                """
                SELECT instrument_id
                FROM equity_instrument
                WHERE exchange = :exchange AND symbol = :symbol
                """
            ),
            {"exchange": identifier.exchange.value, "symbol": identifier.symbol},
        )
        .mappings()
        .one()
    )
    return UUID(str(row["instrument_id"]))
