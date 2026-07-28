"""财务生产发布选择器的 PostgreSQL 集成测试。"""

from __future__ import annotations

import os
from datetime import UTC, date, datetime
from uuid import uuid4

import pytest
from sqlalchemy import insert

from service_data_sync.bootstrap.settings import load_settings
from service_data_sync.domain.equity import Exchange
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.database.models.equity.identity import (
    equity_identifier_version,
)
from service_data_sync.infrastructure.database.models.equity.identity.equity_instrument import (
    EquityInstrument,
)
from service_data_sync.infrastructure.database.models.execution.sync_run import SyncRun
from service_data_sync.infrastructure.database.models.financial.financial_methodology import (
    FinancialMethodology,
)
from service_data_sync.infrastructure.database.models.financial.financial_publication import (
    FinancialPublication,
)
from service_data_sync.infrastructure.database.models.provenance.source_batch import SourceBatch
from service_data_sync.infrastructure.database.models.publication.dataset_publication import (
    DatasetPublication,
)
from service_data_sync.infrastructure.persistence.financial_read_repository import (
    SqlAlchemyFinancialReadRepository,
)

EquityIdentifierVersion = equity_identifier_version.EquityIdentifierVersion


@pytest.mark.integration
def test_repository_selects_persisted_current_financial_publication() -> None:
    """真实 PostgreSQL 只返回未替代、方法学已验证且身份精确匹配的 `publication`。"""
    if os.environ.get("DATA_SYNC_RUN_INTEGRATION") != "1":
        pytest.skip("set DATA_SYNC_RUN_INTEGRATION=1 after starting local infrastructure")
    database = DatabaseClient.from_settings(load_settings())
    repository = SqlAlchemyFinancialReadRepository(database)
    now = datetime(2026, 7, 28, 8, tzinfo=UTC)
    security_id = 9_000_000_000 + uuid4().int % 900_000_000
    symbol = f"{uuid4().int % 1_000_000:06d}"
    instrument_id = uuid4()
    methodology_id = uuid4()
    run_id = uuid4()
    source_batch_id = uuid4()
    publication_id = uuid4()
    data_version = uuid4()
    methodology_code = f"integration.financial.{uuid4().hex[:24]}"
    try:
        with database.transaction() as session:
            session.execute(
                insert(EquityInstrument).values(
                    security_id=security_id,
                    instrument_id=instrument_id,
                    exchange="SSE",
                    symbol=symbol,
                    name="财务读取集成样本",
                    listing_status="LISTED",
                    created_at=now,
                    updated_at=now,
                    master_confirmed_at=now,
                    current_master_version=None,
                )
            )
            session.execute(
                insert(SyncRun).values(
                    run_id=run_id,
                    capability="equity.identity",
                    mode="manual",
                    request_key=f"integration.financial.identity.{run_id}",
                    target_date=date(2026, 7, 27),
                    status="succeeded",
                    requested_at=now,
                    started_at=now,
                    finished_at=now,
                    created_at=now,
                )
            )
            session.execute(
                insert(SourceBatch).values(
                    source_batch_id=source_batch_id,
                    provider_id="integration-fixture",
                    capability="equity.identity",
                    payload_sha256="c" * 64,
                    raw_uri=f"s3://integration/{source_batch_id}",
                    observed_at=now,
                    created_at=now,
                    run_id=run_id,
                    partition_key=f"SSE.{symbol}",
                    observation_seq=1,
                    upstream_source="integration-fixture",
                    adapter_version="integration-v1",
                    schema_fingerprint="d" * 64,
                )
            )
            session.execute(
                insert(EquityIdentifierVersion).values(
                    version_id=uuid4(),
                    security_id=security_id,
                    exchange="SSE",
                    symbol=symbol,
                    identity_state="CONFIRMED",
                    effective_from=date(2000, 1, 1),
                    effective_to=None,
                    known_from=now,
                    known_to=None,
                    effective_date_precision="OFFICIAL_DATE",
                    source_batch_id=source_batch_id,
                    content_sha256=b"i" * 32,
                )
            )
            session.execute(
                insert(FinancialMethodology).values(
                    methodology_id=methodology_id,
                    code=methodology_code,
                    version=1,
                    capability="financial.report",
                    source_code="integration-fixture",
                    status="validated",
                    semantic_spec_sha256="a" * 64,
                    created_at=now,
                )
            )
            session.execute(
                insert(DatasetPublication).values(
                    publication_id=publication_id,
                    dataset="integration.financial.report",
                    partition_key=f"SSE.{symbol}.{methodology_id}",
                    data_version=data_version,
                    quality_status="passed",
                    published_at=now,
                    superseded_at=None,
                    effective_as_of=date(2026, 7, 27),
                    knowledge_cutoff=now,
                )
            )
            session.execute(
                insert(FinancialPublication).values(
                    data_version=data_version,
                    capability="financial.report",
                    security_id=security_id,
                    methodology_id=methodology_id,
                    effective_as_of=date(2026, 7, 27),
                    knowledge_cutoff=now,
                    row_count=12,
                    content_sha256="b" * 64,
                    published_at=now,
                )
            )

        publication = repository.get_current_publication(
            exchange=Exchange.SSE,
            symbol=symbol,
            capability="financial.report",
            methodology_code=methodology_code,
            methodology_version=1,
        )
        missing = repository.get_current_publication(
            exchange=Exchange.SSE,
            symbol=symbol,
            capability="financial.report",
            methodology_code=methodology_code,
            methodology_version=2,
        )
    finally:
        database.close()

    assert publication is not None
    assert publication.data_version == data_version
    assert publication.methodology_id == methodology_id
    assert publication.security_id == security_id
    assert publication.content_sha256 == "b" * 64
    assert missing is None
