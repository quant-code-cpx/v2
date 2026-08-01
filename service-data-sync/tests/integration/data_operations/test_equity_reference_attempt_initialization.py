"""股票中心引用 attempt 初始状态的 PostgreSQL 回归测试。"""

from __future__ import annotations

import os
from datetime import UTC, date, datetime
from typing import cast
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, select
from sqlalchemy.engine import make_url
from sqlalchemy.schema import CreateSchema, DropSchema

from service_data_sync.application.ports.trading_calendar import TradingCalendarPort
from service_data_sync.infrastructure.data_operations.control_plane import (
    DataOperationsControlPlane,
)
from service_data_sync.infrastructure.data_operations.equity_reference_bundle import (
    EquityReferenceBundleOrchestrator,
)
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.database.models.equity.backfill import (
    EquityReferenceGenerationAttempt,
    EquityReferenceGenerationStep,
)


@pytest.mark.integration
def test_building_reference_attempt_persists_sql_null_for_unsealed_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实 PostgreSQL 中创建 `BUILDING` attempt 时必须满足未封印状态约束。"""
    if os.environ.get("DATA_SYNC_RUN_INTEGRATION") != "1":
        pytest.skip("set DATA_SYNC_RUN_INTEGRATION=1 after starting isolated infrastructure")
    database_url = os.environ.get("DATA_SYNC_DATABASE_URL")
    if database_url is None:
        pytest.skip("requires DATA_SYNC_DATABASE_URL")
    schema = f"equity_reference_attempt_{uuid4().hex}"
    admin_engine = create_engine(database_url, pool_pre_ping=True)
    migration_url = _schema_url(database_url, schema=schema)
    database = DatabaseClient(create_engine(migration_url, pool_pre_ping=True))
    try:
        with admin_engine.begin() as connection:
            connection.execute(CreateSchema(schema))
        monkeypatch.setenv("DATA_SYNC_DATABASE_URL", migration_url)
        config = Config("alembic.ini")
        config.set_main_option("sqlalchemy.url", migration_url.replace("%", "%%"))
        command.upgrade(config, "head")
        clock = datetime(2026, 8, 1, 9, tzinfo=UTC)
        orchestrator = EquityReferenceBundleOrchestrator(
            database=database,
            control_plane=cast(DataOperationsControlPlane, object()),
            trading_calendar=cast(TradingCalendarPort, object()),
            # 固定时钟使初始状态与日期边界在数据库断言中可重复。
            now=lambda: clock,
            poll_interval_seconds=0,
        )

        attempt = orchestrator._ensure_attempt(
            campaign_key=f"integration-reference-attempt-{uuid4()}",
            snapshot_observed_on=date(2026, 8, 1),
            market_as_of=date(2026, 7, 31),
        )

        with database.session() as session:
            persisted = session.get(EquityReferenceGenerationAttempt, attempt.attempt_id)
            attempt_nulls = session.execute(
                select(
                    EquityReferenceGenerationAttempt.manifest_json.is_(None),
                    EquityReferenceGenerationAttempt.source_batch_ids_json.is_(None),
                    EquityReferenceGenerationAttempt.last_error_json.is_(None),
                ).where(EquityReferenceGenerationAttempt.attempt_id == attempt.attempt_id)
            ).one()
            step_nulls = session.execute(
                select(
                    EquityReferenceGenerationStep.last_error_json.is_(None),
                    EquityReferenceGenerationStep.output_publications_json.is_(None),
                    EquityReferenceGenerationStep.source_batch_ids_json.is_(None),
                ).where(EquityReferenceGenerationStep.attempt_id == attempt.attempt_id)
            ).all()

        assert persisted is not None
        assert persisted.status == "BUILDING"
        assert attempt_nulls == (True, True, True)
        assert len(step_nulls) == 7
        assert all(values == (True, True, True) for values in step_nulls)
    finally:
        database.close()
        with admin_engine.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True))
        admin_engine.dispose()


def _schema_url(database_url: str, *, schema: str) -> str:
    """把迁移和 attempt 写入一次性 schema，避免向共享集成库遗留不可变账本。"""
    url = make_url(database_url)
    query = dict(url.query)
    query["options"] = f"-csearch_path={schema}"
    return url.set(query=query).render_as_string(hide_password=False)
