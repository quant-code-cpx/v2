"""证券事件窗口覆盖表 0015 迁移的真实 PostgreSQL 前滚兼容回归。"""

from __future__ import annotations

import os
from datetime import UTC, date, datetime
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, insert, select, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateSchema, DropSchema

from service_data_sync.infrastructure.database.models.equity.identity.equity_identifier_version import (  # noqa: E501
    EquityIdentifierVersion,
)
from service_data_sync.infrastructure.database.models.equity.identity.equity_instrument import (
    EquityInstrument,
)
from service_data_sync.infrastructure.database.models.execution.sync_run import SyncRun
from service_data_sync.infrastructure.database.models.provenance.source_batch import SourceBatch
from service_data_sync.infrastructure.database.models.publication.dataset_publication import (
    DatasetPublication,
)
from service_data_sync.infrastructure.database.models.publication.equity_event_window_coverage import (  # noqa: E501
    EquityEventWindowCoverage,
)

pytestmark = pytest.mark.integration


def test_downgrade_preserves_rows_and_reupgrade_validates_exact_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """0015 回退不删表不丢行，重新前滚在原表原 OID 上通过精确 schema 校验。"""
    database_url = _integration_database_url()
    schema = f"event_coverage_migration_{uuid4().hex}"
    admin_engine = create_engine(database_url, pool_pre_ping=True)
    migration_url = _schema_url(database_url, schema=schema)
    engine = create_engine(migration_url, pool_pre_ping=True)
    try:
        with admin_engine.begin() as connection:
            connection.execute(CreateSchema(schema))
        monkeypatch.setenv("DATA_SYNC_DATABASE_URL", migration_url)
        config = _migration_config(migration_url)
        command.upgrade(config, "202607300015")
        coverage_id = _seed_coverage_marker(engine)
        original_oid = _table_oid(engine)
        original_row = _coverage_snapshot(engine, coverage_id=coverage_id)

        command.downgrade(config, "202607300014")

        assert _table_oid(engine) == original_oid
        assert _coverage_snapshot(engine, coverage_id=coverage_id) == original_row
        assert _alembic_revision(engine) == "202607300014"

        command.upgrade(config, "202607300015")

        assert _table_oid(engine) == original_oid
        assert _coverage_snapshot(engine, coverage_id=coverage_id) == original_row
        assert _alembic_revision(engine) == "202607300015"
    finally:
        engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True, if_exists=True))
        admin_engine.dispose()


def test_reupgrade_rejects_preserved_schema_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """回退期若覆盖表索引被改坏，0015 重新前滚必须失败且 revision 保持不变。"""
    database_url = _integration_database_url()
    schema = f"event_coverage_drift_{uuid4().hex}"
    admin_engine = create_engine(database_url, pool_pre_ping=True)
    migration_url = _schema_url(database_url, schema=schema)
    engine = create_engine(migration_url, pool_pre_ping=True)
    try:
        with admin_engine.begin() as connection:
            connection.execute(CreateSchema(schema))
        monkeypatch.setenv("DATA_SYNC_DATABASE_URL", migration_url)
        config = _migration_config(migration_url)
        command.upgrade(config, "202607300015")
        command.downgrade(config, "202607300014")
        with engine.begin() as connection:
            connection.execute(text("DROP INDEX ix_equity_event_coverage_read"))

        with pytest.raises(RuntimeError, match="indexes have drifted"):
            command.upgrade(config, "202607300015")

        assert _alembic_revision(engine) == "202607300014"
    finally:
        engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True, if_exists=True))
        admin_engine.dispose()


def _integration_database_url() -> str:
    """读取真实集成数据库地址；未显式启用时跳过测试。"""
    if os.environ.get("DATA_SYNC_RUN_INTEGRATION") != "1":
        pytest.skip("requires DATA_SYNC_RUN_INTEGRATION=1")
    database_url = os.environ.get("DATA_SYNC_DATABASE_URL")
    if database_url is None:
        pytest.skip("requires DATA_SYNC_DATABASE_URL")
    return database_url


def _migration_config(migration_url: str) -> Config:
    """构造绑定独占 schema 的 Alembic 配置。"""
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", migration_url.replace("%", "%%"))
    return config


def _seed_coverage_marker(engine: Engine) -> UUID:
    """写入一行带完整真实外键链的覆盖标记，供升降级数据保留断言。"""
    now = datetime(2026, 7, 30, 8, tzinfo=UTC)
    coverage_id = uuid4()
    coverage_version = uuid4()
    run_id = uuid4()
    source_batch_id = uuid4()
    publication_id = uuid4()
    identifier_version_id = uuid4()
    security_id = 9_300_000_001
    with Session(engine) as session, session.begin():
        session.execute(
            insert(SyncRun).values(
                run_id=run_id,
                capability="integration.event-coverage-migration",
                mode="backfill",
                request_key=f"integration.event-coverage-migration.{run_id}",
                target_date=date(2026, 7, 31),
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
                provider_id="integration-event-coverage-migration",
                capability="integration.event-coverage-migration",
                source_dataset_id=None,
                payload_sha256="a" * 64,
                raw_uri=f"s3://integration/{source_batch_id}",
                observed_at=now,
                created_at=now,
                run_id=run_id,
                partition_key="integration:event-coverage-migration",
                observation_seq=1,
                upstream_source="integration-provider",
                adapter_version="integration-v1",
                schema_fingerprint="b" * 64,
            )
        )
        session.execute(
            insert(EquityInstrument).values(
                security_id=security_id,
                instrument_id=uuid4(),
                exchange="SSE",
                symbol="600519",
                name="覆盖迁移集成样本",
                listing_status="LISTED",
                created_at=now,
                updated_at=now,
                master_confirmed_at=now,
                current_master_version=None,
            )
        )
        session.execute(
            insert(EquityIdentifierVersion).values(
                version_id=identifier_version_id,
                security_id=security_id,
                exchange="SSE",
                symbol="600519",
                identity_state="CONFIRMED",
                effective_from=date(2001, 1, 1),
                effective_to=None,
                known_from=now,
                known_to=None,
                effective_date_precision="OFFICIAL_DATE",
                source_batch_id=source_batch_id,
                content_sha256=b"c" * 32,
            )
        )
        session.execute(
            insert(DatasetPublication).values(
                publication_id=publication_id,
                dataset="equity.corporate_action",
                partition_key="integration:SSE.600519",
                data_version=uuid4(),
                release_id=None,
                quality_status="passed",
                published_at=now,
                superseded_at=None,
                effective_as_of=date(2026, 7, 31),
                knowledge_cutoff=now,
            )
        )
        session.execute(
            insert(EquityEventWindowCoverage).values(
                coverage_id=coverage_id,
                coverage_version=coverage_version,
                dataset="equity.corporate_action",
                event_family="CORPORATE_ACTION",
                security_id=security_id,
                identifier_version_id=identifier_version_id,
                coverage_from=date(2026, 7, 1),
                coverage_to=date(2026, 7, 31),
                publication_id=publication_id,
                source_batch_id=source_batch_id,
                record_count=0,
                coverage_scope="INSTRUMENT",
                universe_hash="d" * 64,
                universe_size=1,
                observed_at=now,
                created_at=now,
                superseded_at=None,
            )
        )
    return coverage_id


def _coverage_snapshot(engine: Engine, *, coverage_id: UUID) -> tuple[object, ...]:
    """读取标记行全部冻结业务字段，验证升降级没有重建或改写数据。"""
    with Session(engine) as session:
        row = session.execute(
            select(
                EquityEventWindowCoverage.coverage_id,
                EquityEventWindowCoverage.coverage_version,
                EquityEventWindowCoverage.dataset,
                EquityEventWindowCoverage.event_family,
                EquityEventWindowCoverage.security_id,
                EquityEventWindowCoverage.identifier_version_id,
                EquityEventWindowCoverage.coverage_from,
                EquityEventWindowCoverage.coverage_to,
                EquityEventWindowCoverage.publication_id,
                EquityEventWindowCoverage.source_batch_id,
                EquityEventWindowCoverage.record_count,
                EquityEventWindowCoverage.coverage_scope,
                EquityEventWindowCoverage.universe_hash,
                EquityEventWindowCoverage.universe_size,
                EquityEventWindowCoverage.observed_at,
                EquityEventWindowCoverage.created_at,
                EquityEventWindowCoverage.superseded_at,
            ).where(EquityEventWindowCoverage.coverage_id == coverage_id)
        ).one()
    return tuple(row)


def _table_oid(engine: Engine) -> int:
    """读取覆盖表 PostgreSQL OID，用于证明回退和重升没有替换物理表。"""
    with engine.connect() as connection:
        value = connection.scalar(text("SELECT 'equity_event_window_coverage'::regclass::oid"))
    assert isinstance(value, int)
    return value


def _alembic_revision(engine: Engine) -> str:
    """读取当前独占 schema 的 Alembic revision。"""
    with engine.connect() as connection:
        value = connection.scalar(text("SELECT version_num FROM alembic_version"))
    assert isinstance(value, str)
    return value


def _schema_url(database_url: str, *, schema: str) -> str:
    """把迁移限制在一次性 schema，避免触碰共享测试表。"""
    url = make_url(database_url)
    query = dict(url.query)
    query["options"] = f"-csearch_path={schema}"
    return url.set(query=query).render_as_string(hide_password=False)
