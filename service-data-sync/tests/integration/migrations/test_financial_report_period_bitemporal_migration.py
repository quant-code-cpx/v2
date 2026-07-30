"""财务指标报告期双时态逻辑键迁移回归。"""

from __future__ import annotations

import os
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, insert, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.schema import CreateSchema, DropSchema

from service_data_sync.application.ports.financial_read import FinancialPublicationSnapshot
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.database.models.equity.identity.equity_instrument import (
    EquityInstrument,
)
from service_data_sync.infrastructure.database.models.execution.sync_run import SyncRun
from service_data_sync.infrastructure.database.models.financial import (
    derived_financial_metric_revision,
    provider_financial_metric_revision,
)
from service_data_sync.infrastructure.database.models.financial.financial_methodology import (
    FinancialMethodology,
)
from service_data_sync.infrastructure.database.models.financial.financial_metric_definition import (
    FinancialMetricDefinition,
)
from service_data_sync.infrastructure.database.models.provenance.source_batch import SourceBatch
from service_data_sync.infrastructure.persistence.financial_read_repository import (
    SqlAlchemyFinancialReadRepository,
)

pytestmark = pytest.mark.integration

DerivedFinancialMetricRevision = derived_financial_metric_revision.DerivedFinancialMetricRevision
ProviderFinancialMetricRevision = provider_financial_metric_revision.ProviderFinancialMetricRevision


def test_financial_report_period_constraint_upgrade_downgrade_upgrade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """独占 schema 可升级、兼容回退、再升级，回退不恢复已知错误约束。"""
    if os.environ.get("DATA_SYNC_RUN_INTEGRATION") != "1":
        pytest.skip("requires DATA_SYNC_RUN_INTEGRATION=1")
    database_url = os.environ.get("DATA_SYNC_DATABASE_URL")
    if database_url is None:
        pytest.skip("requires DATA_SYNC_DATABASE_URL")
    schema = f"financial_report_period_{uuid4().hex}"
    admin_engine = create_engine(database_url, pool_pre_ping=True)
    migration_url = _schema_url(database_url, schema=schema)
    migration_engine = create_engine(migration_url, pool_pre_ping=True)
    try:
        with admin_engine.begin() as connection:
            connection.execute(CreateSchema(schema))
        monkeypatch.setenv("DATA_SYNC_DATABASE_URL", migration_url)
        config = Config("alembic.ini")
        config.set_main_option("sqlalchemy.url", migration_url.replace("%", "%%"))

        command.upgrade(config, "202607300013")
        old_definitions = _financial_constraint_definitions(migration_engine)
        assert old_definitions
        assert all("report_period WITH =" not in value for value in old_definitions)

        command.upgrade(config, "202607300014")
        upgraded_definitions = _financial_constraint_definitions(migration_engine)
        assert len(upgraded_definitions) == len(old_definitions)
        assert all("report_period WITH =" in value for value in upgraded_definitions)

        command.downgrade(config, "202607300013")
        downgraded_definitions = _financial_constraint_definitions(migration_engine)
        assert downgraded_definitions == upgraded_definitions

        command.upgrade(config, "202607300014")
        repeated_definitions = _financial_constraint_definitions(migration_engine)
        assert repeated_definitions == upgraded_definitions
    finally:
        migration_engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True))
        admin_engine.dispose()


def test_provider_and_derived_multi_period_rows_remain_visible_at_current_as_of(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一指标多个报告期可同时写入，并在当前 `asOf` 下完整读取而非只剩一天。"""
    if os.environ.get("DATA_SYNC_RUN_INTEGRATION") != "1":
        pytest.skip("requires DATA_SYNC_RUN_INTEGRATION=1")
    database_url = os.environ.get("DATA_SYNC_DATABASE_URL")
    if database_url is None:
        pytest.skip("requires DATA_SYNC_DATABASE_URL")
    schema = f"financial_multi_period_{uuid4().hex}"
    admin_engine = create_engine(database_url, pool_pre_ping=True)
    migration_url = _schema_url(database_url, schema=schema)
    migration_engine = create_engine(migration_url, pool_pre_ping=True)
    database = DatabaseClient(migration_engine)
    try:
        with admin_engine.begin() as connection:
            connection.execute(CreateSchema(schema))
        monkeypatch.setenv("DATA_SYNC_DATABASE_URL", migration_url)
        config = Config("alembic.ini")
        config.set_main_option("sqlalchemy.url", migration_url.replace("%", "%%"))
        command.upgrade(config, "202607300014")

        provider_publication, derived_publication, known_at = _seed_multi_period_metrics(database)
        repository = SqlAlchemyFinancialReadRepository(database)
        expected_periods = [date(2025, 3, 31), date(2025, 6, 30)]
        assert _visible_periods(
            repository,
            provider_publication=provider_publication,
            derived_publication=derived_publication,
            known_at=known_at,
        ) == (expected_periods, expected_periods)
        upgraded_definitions = _financial_constraint_definitions(migration_engine)

        command.downgrade(config, "202607300013")
        assert _financial_constraint_definitions(migration_engine) == upgraded_definitions
        assert _metric_interval_counts(migration_engine) == (2, 2, 0)
        assert _visible_periods(
            repository,
            provider_publication=provider_publication,
            derived_publication=derived_publication,
            known_at=known_at,
        ) == (expected_periods, expected_periods)

        command.upgrade(config, "202607300014")
        assert _financial_constraint_definitions(migration_engine) == upgraded_definitions
        assert _metric_interval_counts(migration_engine) == (2, 2, 0)
        assert _visible_periods(
            repository,
            provider_publication=provider_publication,
            derived_publication=derived_publication,
            known_at=known_at,
        ) == (expected_periods, expected_periods)
    finally:
        database.close()
        with admin_engine.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True))
        admin_engine.dispose()


def _seed_multi_period_metrics(
    database: DatabaseClient,
) -> tuple[FinancialPublicationSnapshot, FinancialPublicationSnapshot, datetime]:
    """写入同证券、同指标、同年两个开放报告期 revision，并返回只读 publication 快照。"""
    known_at = datetime(2026, 7, 30, 8, tzinfo=UTC)
    observed_at = known_at - timedelta(minutes=1)
    security_id = 8_000_000_000 + uuid4().int % 900_000_000
    instrument_id = uuid4()
    provider_methodology_id = uuid4()
    derived_methodology_id = uuid4()
    provider_metric_id = 8_000_000_000 + uuid4().int % 400_000_000
    derived_metric_id = 8_500_000_000 + uuid4().int % 400_000_000
    source_run_id = uuid4()
    derived_run_id = uuid4()
    source_batch_id = uuid4()
    provider_code = f"integration.provider.{uuid4().hex[:20]}"
    derived_code = f"integration.derived.{uuid4().hex[:20]}"
    periods = (date(2025, 3, 31), date(2025, 6, 30))
    with database.transaction() as session:
        session.execute(
            insert(EquityInstrument).values(
                security_id=security_id,
                instrument_id=instrument_id,
                exchange="SSE",
                symbol=f"{uuid4().int % 1_000_000:06d}",
                name="多报告期读取集成样本",
                listing_status="LISTED",
                created_at=known_at,
                updated_at=known_at,
                master_confirmed_at=known_at,
                current_master_version=None,
            )
        )
        session.execute(
            insert(SyncRun),
            [
                {
                    "run_id": source_run_id,
                    "capability": "financial.provider-metric",
                    "mode": "manual",
                    "request_key": f"integration.provider.{source_run_id}",
                    "target_date": None,
                    "status": "succeeded",
                    "requested_at": known_at,
                    "started_at": known_at,
                    "finished_at": known_at,
                    "created_at": known_at,
                },
                {
                    "run_id": derived_run_id,
                    "capability": "financial.derived-metric",
                    "mode": "manual",
                    "request_key": f"integration.derived.{derived_run_id}",
                    "target_date": None,
                    "status": "succeeded",
                    "requested_at": known_at,
                    "started_at": known_at,
                    "finished_at": known_at,
                    "created_at": known_at,
                },
            ],
        )
        session.execute(
            insert(SourceBatch).values(
                source_batch_id=source_batch_id,
                provider_id="integration-financial",
                capability="financial.provider-metric",
                payload_sha256="a" * 64,
                raw_uri=f"s3://integration/{source_batch_id}",
                observed_at=observed_at,
                created_at=known_at,
                run_id=source_run_id,
                partition_key=f"security:{security_id}",
                observation_seq=1,
                upstream_source="integration-financial",
                adapter_version="integration-v1",
                schema_fingerprint="b" * 64,
            )
        )
        session.execute(
            insert(FinancialMethodology),
            [
                {
                    "methodology_id": provider_methodology_id,
                    "code": provider_code,
                    "version": 1,
                    "capability": "financial.provider-metric",
                    "source_code": "integration-financial",
                    "status": "validated",
                    "semantic_spec_sha256": "c" * 64,
                    "created_at": known_at,
                },
                {
                    "methodology_id": derived_methodology_id,
                    "code": derived_code,
                    "version": 1,
                    "capability": "financial.derived-metric",
                    "source_code": "platform-formula",
                    "status": "validated",
                    "semantic_spec_sha256": "d" * 64,
                    "created_at": known_at,
                },
            ],
        )
        session.execute(
            insert(FinancialMetricDefinition),
            [
                {
                    "metric_id": provider_metric_id,
                    "code": provider_code,
                    "label": "供应商多期指标",
                    "origin": "provider_reported",
                    "statement_type": None,
                    "value_domain": "ratio",
                    "canonical_unit": "ratio",
                    "currency_required": False,
                    "sign_convention": "source",
                    "dictionary_version": 1,
                    "status": "active",
                },
                {
                    "metric_id": derived_metric_id,
                    "code": derived_code,
                    "label": "平台多期指标",
                    "origin": "platform_derived",
                    "statement_type": None,
                    "value_domain": "ratio",
                    "canonical_unit": "ratio",
                    "currency_required": False,
                    "sign_convention": "platform",
                    "dictionary_version": 1,
                    "status": "active",
                },
            ],
        )
        session.execute(
            insert(ProviderFinancialMetricRevision),
            [
                _provider_metric_row(
                    period=period,
                    ordinal=ordinal,
                    security_id=security_id,
                    metric_id=provider_metric_id,
                    methodology_id=provider_methodology_id,
                    source_batch_id=source_batch_id,
                    known_at=known_at,
                    observed_at=observed_at,
                )
                for ordinal, period in enumerate(periods, start=1)
            ],
        )
        session.execute(
            insert(DerivedFinancialMetricRevision),
            [
                _derived_metric_row(
                    period=period,
                    ordinal=ordinal,
                    security_id=security_id,
                    metric_id=derived_metric_id,
                    methodology_id=derived_methodology_id,
                    derivation_run_id=derived_run_id,
                    source_batch_id=source_batch_id,
                    known_at=known_at,
                    observed_at=observed_at,
                )
                for ordinal, period in enumerate(periods, start=1)
            ],
        )
    common = {
        "security_id": security_id,
        "instrument_id": instrument_id,
        "methodology_version": 1,
        "source_code": "integration-financial",
        "published_at": known_at,
        "effective_as_of": periods[-1],
        "knowledge_cutoff": known_at,
        "row_count": 2,
        "content_sha256": "e" * 64,
    }
    return (
        FinancialPublicationSnapshot(
            data_version=uuid4(),
            methodology_id=provider_methodology_id,
            capability="financial.provider-metric",
            methodology_code=provider_code,
            **common,
        ),
        FinancialPublicationSnapshot(
            data_version=uuid4(),
            methodology_id=derived_methodology_id,
            capability="financial.derived-metric",
            methodology_code=derived_code,
            **common,
        ),
        known_at,
    )


def _provider_metric_row(
    *,
    period: date,
    ordinal: int,
    security_id: int,
    metric_id: int,
    methodology_id: object,
    source_batch_id: object,
    known_at: datetime,
    observed_at: datetime,
) -> dict[str, object]:
    """构造一个供应商开放有效区间指标 revision。"""
    return {
        "report_period": period,
        "metric_revision_id": uuid4(),
        "security_id": security_id,
        "metric_id": metric_id,
        "methodology_id": methodology_id,
        "period_basis": "YEAR_TO_DATE",
        "statement_scope": "CONSOLIDATED",
        "value": Decimal(ordinal),
        "unit": "ratio",
        "currency": None,
        "currency_null_reason": "NOT_APPLICABLE",
        # 两个报告期故意共享有效起点，覆盖旧错误约束无法表达的真实合法状态。
        "effective_from": date(2025, 1, 1),
        "effective_to": None,
        "known_from": known_at,
        "known_to": None,
        "knowledge_basis": "OBSERVED_AT",
        "knowledge_confidence": "CONSERVATIVE",
        "observed_at": observed_at,
        "source_batch_id": source_batch_id,
        "revision": 1,
        "content_sha256": f"{ordinal}" * 64,
        "quality_status": "passed",
        "created_at": known_at,
    }


def _derived_metric_row(
    *,
    period: date,
    ordinal: int,
    security_id: int,
    metric_id: int,
    methodology_id: object,
    derivation_run_id: object,
    source_batch_id: object,
    known_at: datetime,
    observed_at: datetime,
) -> dict[str, object]:
    """构造一个平台派生开放有效区间指标 revision。"""
    return {
        **_provider_metric_row(
            period=period,
            ordinal=ordinal,
            security_id=security_id,
            metric_id=metric_id,
            methodology_id=methodology_id,
            source_batch_id=source_batch_id,
            known_at=known_at,
            observed_at=observed_at,
        ),
        "formula_version": 1,
        "input_manifest_sha256": f"{ordinal + 2}" * 64,
        "derivation_run_id": derivation_run_id,
        "computed_at": known_at,
    }


def _financial_constraint_definitions(engine: Engine) -> tuple[str, ...]:
    """读取供应商与平台派生年度分区的规范化排斥约束定义。"""
    with engine.connect() as connection:
        return tuple(
            connection.execute(
                text(
                    """
                    SELECT pg_get_constraintdef(con.oid)
                    FROM pg_constraint AS con
                    JOIN pg_class AS child ON child.oid = con.conrelid
                    JOIN pg_namespace AS namespace ON namespace.oid = child.relnamespace
                    WHERE namespace.nspname = current_schema()
                      AND con.contype = 'x'
                      AND (
                        child.relname LIKE 'provider_financial_metric_revision_%'
                        OR child.relname LIKE 'derived_financial_metric_revision_%'
                      )
                    ORDER BY child.relname
                    """
                )
            ).scalars()
        )


def _visible_periods(
    repository: SqlAlchemyFinancialReadRepository,
    *,
    provider_publication: FinancialPublicationSnapshot,
    derived_publication: FinancialPublicationSnapshot,
    known_at: datetime,
) -> tuple[list[date], list[date]]:
    """按当前业务时点读取两个数据集的完整报告期集合。"""
    provider_rows = repository.list_provider_metrics(
        publication=provider_publication,
        as_of=date(2026, 7, 30),
        known_at=known_at,
        metric_codes=(),
        period_bases=(),
        report_period_from=None,
        report_period_to=None,
        after_report_period=None,
        after_metric_code=None,
        limit=10,
    )
    derived_rows = repository.list_derived_metrics(
        publication=derived_publication,
        as_of=date(2026, 7, 30),
        known_at=known_at,
        metric_codes=(),
        period_bases=(),
        report_period_from=None,
        report_period_to=None,
        after_report_period=None,
        after_metric_code=None,
        limit=10,
    )
    return (
        [row.report_period for row in provider_rows],
        [row.report_period for row in derived_rows],
    )


def _metric_interval_counts(engine: Engine) -> tuple[int, int, int]:
    """读取两类 revision 行数及被错误收窄的有效区间数量。"""
    with engine.connect() as connection:
        provider_count = connection.scalar(text("SELECT count(*) FROM provider_financial_metric_revision"))
        derived_count = connection.scalar(text("SELECT count(*) FROM derived_financial_metric_revision"))
        narrowed_count = connection.scalar(
            text(
                """
                SELECT
                  (SELECT count(*) FROM provider_financial_metric_revision
                   WHERE effective_to IS NOT NULL)
                  +
                  (SELECT count(*) FROM derived_financial_metric_revision
                   WHERE effective_to IS NOT NULL)
                """
            )
        )
    assert provider_count is not None
    assert derived_count is not None
    assert narrowed_count is not None
    return int(provider_count), int(derived_count), int(narrowed_count)


def _schema_url(database_url: str, *, schema: str) -> str:
    """把 Alembic 和迁移断言限制在一次性 schema，避免触碰共享数据。"""
    url = make_url(database_url)
    query = dict(url.query)
    query["options"] = f"-csearch_path={schema}"
    return url.set(query=query).render_as_string(hide_password=False)
