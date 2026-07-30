"""证券事件 coverage 在真实 PostgreSQL 上的全市场批量写入回归。"""

from __future__ import annotations

import os
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, event, func, insert, select
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateSchema, DropSchema

from service_data_sync.application.ports.market_data import EquitySourceObservation
from service_data_sync.domain.equity import EquityCorporateAction, EquityIdentifier
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.database.models.canonical import (
    DatasetRelease,
    MethodologyVersion,
)
from service_data_sync.infrastructure.database.models.equity.identity.equity_identifier_version import (  # noqa: E501
    EquityIdentifierVersion,
)
from service_data_sync.infrastructure.database.models.equity.identity.equity_instrument import (
    EquityInstrument,
)
from service_data_sync.infrastructure.database.models.equity.market_data.equity_corporate_action_version import (  # noqa: E501
    EquityCorporateActionVersion,
)
from service_data_sync.infrastructure.database.models.execution.sync_run import SyncRun
from service_data_sync.infrastructure.database.models.provenance.source_batch import SourceBatch
from service_data_sync.infrastructure.database.models.publication.dataset_publication import (
    DatasetPublication,
)
from service_data_sync.infrastructure.database.models.publication.equity_event_window_coverage import (  # noqa: E501
    EquityEventWindowCoverage,
)
from service_data_sync.infrastructure.persistence import event_window_coverage as coverage
from service_data_sync.infrastructure.persistence.equity_market_data_repository import (
    SqlAlchemyEquityMarketDataRepository,
)
from service_data_sync.infrastructure.persistence.event_window_coverage import (
    EventCoverageIdentity,
)

pytestmark = pytest.mark.integration

_START = date(2026, 7, 1)
_END = date(2026, 7, 31)
_FIRST_OBSERVED = datetime(2026, 7, 30, 7, tzinfo=UTC)
_FIRST_CREATED = datetime(2026, 7, 30, 8, tzinfo=UTC)
_DATASET_FAMILIES = (
    ("equity.corporate_action", "CORPORATE_ACTION"),
    ("equity.corporate_event.earnings.reported", "EARNINGS_FORECAST"),
    ("equity.corporate_event.earnings.reported", "EARNINGS_EXPRESS"),
    ("equity.dragon_tiger.disclosure.reported", "DRAGON_TIGER"),
    ("equity.block_trade.execution.reported", "BLOCK_TRADE"),
)


def test_full_roster_five_family_batch_replay_and_supersede(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """五千证券五事件族可批量新增、精确重放和同窗替代，不触发参数上限。"""
    if os.environ.get("DATA_SYNC_RUN_INTEGRATION") != "1":
        pytest.skip("requires DATA_SYNC_RUN_INTEGRATION=1")
    database_url = os.environ.get("DATA_SYNC_DATABASE_URL")
    if database_url is None:
        pytest.skip("requires DATA_SYNC_DATABASE_URL")
    schema = f"event_coverage_batch_{uuid4().hex}"
    admin_engine = create_engine(database_url, pool_pre_ping=True)
    migration_url = _schema_url(database_url, schema=schema)
    engine = create_engine(migration_url, pool_pre_ping=True)
    try:
        with admin_engine.begin() as connection:
            connection.execute(CreateSchema(schema))
        monkeypatch.setenv("DATA_SYNC_DATABASE_URL", migration_url)
        config = Config("alembic.ini")
        config.set_main_option("sqlalchemy.url", migration_url.replace("%", "%%"))
        command.upgrade(config, "202607300015")

        identities, source_batch_id, publication_ids = _seed_dependencies(engine)
        first_values = _coverage_values(
            identities=identities,
            source_batch_id=source_batch_id,
            publication_ids=publication_ids,
            observed_at=_FIRST_OBSERVED,
            created_at=_FIRST_CREATED,
            generation="first",
        )
        statements: list[str] = []
        statement_bind_counts: list[int] = []
        executemany_batch_sizes: list[int] = []

        def capture(
            _connection: object,
            _cursor: object,
            statement: str,
            _parameters: object,
            _context: object,
            _executemany: bool,
        ) -> None:
            """只记录 coverage 表 SQL，避免依赖驱动的参数展示格式。"""
            if "equity_event_window_coverage" in statement:
                statements.append(statement.lstrip().split(maxsplit=1)[0].upper())
                if _executemany and isinstance(_parameters, list):
                    executemany_batch_sizes.append(len(_parameters))
                    statement_bind_counts.append(
                        max((len(item) for item in _parameters), default=0)
                    )
                elif isinstance(_parameters, dict | tuple):
                    statement_bind_counts.append(len(_parameters))

        event.listen(engine, "before_cursor_execute", capture)
        try:
            with Session(engine) as session, session.begin():
                coverage._record_coverages(session, values=first_values)  # noqa: SLF001
            assert statements == ["SELECT"] * 6 + ["INSERT"] * 5

            statements.clear()
            with Session(engine) as session, session.begin():
                coverage._record_coverages(session, values=first_values)  # noqa: SLF001
            assert statements == ["SELECT"] * 5

            statements.clear()
            revised_values = _coverage_values(
                identities=identities,
                source_batch_id=source_batch_id,
                publication_ids=publication_ids,
                observed_at=_FIRST_OBSERVED + timedelta(hours=1),
                created_at=_FIRST_CREATED + timedelta(hours=1),
                generation="revised",
            )
            with Session(engine) as session, session.begin():
                coverage._record_coverages(session, values=revised_values)  # noqa: SLF001
            assert statements == (["SELECT"] * 6 + ["UPDATE"] * 5 + ["INSERT"] * 5)
        finally:
            event.remove(engine, "before_cursor_execute", capture)
        assert max(statement_bind_counts) <= 5_020
        assert max(executemany_batch_sizes) <= 5_000

        with Session(engine) as session:
            total, current, superseded, nonzero = session.execute(
                select(
                    func.count(),
                    func.count().filter(EquityEventWindowCoverage.superseded_at.is_(None)),
                    func.count().filter(EquityEventWindowCoverage.superseded_at.is_not(None)),
                    func.count().filter(
                        EquityEventWindowCoverage.record_count > 0,
                        EquityEventWindowCoverage.superseded_at.is_(None),
                    ),
                )
            ).one()
        assert (total, current, superseded) == (50_000, 25_000, 25_000)
        assert nonzero == 10
    finally:
        engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True))
        admin_engine.dispose()


def test_corporate_action_zero_nonzero_replay_correction_and_rollback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """公司行动事实、release 与 coverage 同事务打通，并保留来源方法学和失败回滚。"""
    if os.environ.get("DATA_SYNC_RUN_INTEGRATION") != "1":
        pytest.skip("requires DATA_SYNC_RUN_INTEGRATION=1")
    database_url = os.environ.get("DATA_SYNC_DATABASE_URL")
    if database_url is None:
        pytest.skip("requires DATA_SYNC_DATABASE_URL")
    schema = f"corporate_action_coverage_{uuid4().hex}"
    admin_engine = create_engine(database_url, pool_pre_ping=True)
    migration_url = _schema_url(database_url, schema=schema)
    engine = create_engine(migration_url, pool_pre_ping=True)
    database = DatabaseClient(engine)
    try:
        with admin_engine.begin() as connection:
            connection.execute(CreateSchema(schema))
        monkeypatch.setenv("DATA_SYNC_DATABASE_URL", migration_url)
        config = Config("alembic.ini")
        config.set_main_option("sqlalchemy.url", migration_url.replace("%", "%%"))
        command.upgrade(config, "202607300015")
        identifier = EquityIdentifier.parse("SSE.600519")
        security_id, identifier_version_id = _seed_action_identity(engine)
        repository = SqlAlchemyEquityMarketDataRepository(database)

        zero_source = _action_source("zero", observed_at=datetime(2026, 2, 1, tzinfo=UTC))
        repository.publish_corporate_actions(
            identifier=identifier,
            actions=(),
            source=zero_source,
            start=date(2026, 1, 1),
            end=date(2026, 1, 31),
        )
        first_action = _action(cash="10", status="实施")
        first_source = _action_source("first", observed_at=datetime(2026, 7, 1, tzinfo=UTC))
        repository.publish_corporate_actions(
            identifier=identifier,
            actions=(first_action,),
            source=first_source,
            start=date(2026, 6, 1),
            end=date(2026, 6, 30),
        )
        repository.publish_corporate_actions(
            identifier=identifier,
            actions=(first_action,),
            source=first_source,
            start=date(2026, 6, 1),
            end=date(2026, 6, 30),
        )
        corrected_action = _action(cash="11", status="实施更正")
        corrected_source = _action_source(
            "corrected",
            observed_at=datetime(2026, 7, 2, tzinfo=UTC),
        )
        repository.publish_corporate_actions(
            identifier=identifier,
            actions=(corrected_action,),
            source=corrected_source,
            start=date(2026, 6, 1),
            end=date(2026, 6, 30),
        )

        coverages = _action_coverages(engine, security_id=security_id)
        assert [
            (
                row["coverage_from"],
                row["coverage_to"],
                row["record_count"],
                row["superseded_at"] is None,
            )
            for row in coverages
        ] == [
            (date(2026, 1, 1), date(2026, 1, 31), 0, True),
            (date(2026, 6, 1), date(2026, 6, 30), 1, False),
            (date(2026, 6, 1), date(2026, 6, 30), 1, True),
        ]
        assert {row["methodology_code"] for row in coverages} == {
            "equity.corporate_action.release-bridge"
        }
        assert {row["provider_id"] for row in coverages} == {"integration-corporate-action"}
        assert {row["upstream_source"] for row in coverages} == {"eastmoney-share-bonus"}
        assert {row["identifier_version_id"] for row in coverages} == {identifier_version_id}
        assert len({row["coverage_version"] for row in coverages}) == 3
        assert _action_revision_counts(engine, security_id=security_id) == (2, 1)

        before_failure = _action_transaction_counts(engine)

        def fail_coverage(*args: object, **kwargs: object) -> None:
            """模拟 coverage 持久化失败，验证整个公司行动事务回滚。"""
            del args, kwargs
            raise RuntimeError("forced coverage failure")

        monkeypatch.setattr(
            "service_data_sync.infrastructure.persistence.equity_market_data_repository."
            "publish_event_window_coverages",
            fail_coverage,
        )
        with pytest.raises(RuntimeError, match="forced coverage failure"):
            repository.publish_corporate_actions(
                identifier=identifier,
                actions=(
                    EquityCorporateAction(
                        source_event_key="failure-only",
                        report_period=date(2026, 8, 31),
                        status="预案",
                        announcement_date=date(2026, 8, 1),
                        record_date=None,
                        ex_date=None,
                        cash_dividend_per_10=Decimal("1"),
                        bonus_shares_per_10=None,
                        transfer_shares_per_10=None,
                    ),
                ),
                source=_action_source(
                    "failure",
                    observed_at=datetime(2026, 8, 2, tzinfo=UTC),
                ),
                start=date(2026, 8, 1),
                end=date(2026, 8, 31),
            )
        assert _action_transaction_counts(engine) == before_failure
    finally:
        database.close()
        with admin_engine.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True))
        admin_engine.dispose()


def _seed_action_identity(engine: Engine) -> tuple[int, UUID]:
    """写入一个由真实来源观察支撑、完整覆盖测试窗口的确认身份。"""
    run_id = uuid4()
    source_batch_id = uuid4()
    security_id = 9_200_000_001
    identifier_version_id = uuid4()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    with Session(engine) as session, session.begin():
        session.execute(
            insert(SyncRun).values(
                run_id=run_id,
                capability="integration.equity-master",
                mode="backfill",
                request_key=f"integration.equity-master.{run_id}",
                target_date=date(2026, 1, 1),
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
                provider_id="integration-equity-master",
                capability="equity.master",
                source_dataset_id=None,
                payload_sha256="e" * 64,
                raw_uri=f"s3://integration/{source_batch_id}",
                observed_at=now,
                created_at=now,
                run_id=run_id,
                partition_key="integration:SSE.600519",
                observation_seq=1,
                upstream_source="sse-official",
                adapter_version="integration-v1",
                schema_fingerprint="f" * 64,
            )
        )
        session.execute(
            insert(EquityInstrument).values(
                security_id=security_id,
                instrument_id=uuid4(),
                exchange="SSE",
                symbol="600519",
                name="公司行动集成样本",
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
                content_sha256=b"g" * 32,
            )
        )
    return security_id, identifier_version_id


def _action_source(suffix: str, *, observed_at: datetime) -> EquitySourceObservation:
    """构造包含真实 raw/normalized 元数据的公司行动来源观察。"""
    return EquitySourceObservation(
        provider_id="integration-corporate-action",
        capability="equity.corporate_action",
        raw_payload_sha256=(suffix[0] if suffix[0] in "abcdef" else "a") * 64,
        raw_uri=f"s3://integration/raw/{suffix}.json",
        raw_content_type="application/json",
        raw_byte_size=128,
        normalized_payload_sha256=(suffix[-1] if suffix[-1] in "abcdef" else "b") * 64,
        normalized_uri=f"s3://integration/normalized/{suffix}.json",
        normalized_content_type="application/vnd.quant-v2.equity-corporate-action+json",
        normalized_byte_size=96,
        observed_at=observed_at,
        upstream_source="eastmoney-share-bonus",
        adapter_version="integration-v1",
        schema_fingerprint="c" * 64,
    )


def _action(*, cash: str, status: str) -> EquityCorporateAction:
    """构造一个六月除权日的可修订现金分红事件。"""
    return EquityCorporateAction(
        source_event_key="2025-12-31",
        report_period=date(2025, 12, 31),
        status=status,
        announcement_date=date(2026, 6, 1),
        record_date=date(2026, 6, 29),
        ex_date=date(2026, 6, 30),
        cash_dividend_per_10=Decimal(cash),
        bonus_shares_per_10=None,
        transfer_shares_per_10=None,
    )


def _action_coverages(engine: Engine, *, security_id: int) -> list[dict[str, object]]:
    """读取公司行动 coverage 及其 publication 方法学和真实来源。"""
    with Session(engine) as session:
        rows = (
            session.execute(
                select(
                    EquityEventWindowCoverage.coverage_version,
                    EquityEventWindowCoverage.identifier_version_id,
                    EquityEventWindowCoverage.coverage_from,
                    EquityEventWindowCoverage.coverage_to,
                    EquityEventWindowCoverage.record_count,
                    EquityEventWindowCoverage.superseded_at,
                    MethodologyVersion.code.label("methodology_code"),
                    SourceBatch.provider_id,
                    SourceBatch.upstream_source,
                )
                .join(
                    DatasetPublication,
                    DatasetPublication.publication_id == EquityEventWindowCoverage.publication_id,
                )
                .join(
                    DatasetRelease,
                    DatasetRelease.release_id == DatasetPublication.release_id,
                )
                .join(
                    MethodologyVersion,
                    MethodologyVersion.methodology_version_id
                    == DatasetRelease.methodology_version_id,
                )
                .join(
                    SourceBatch,
                    SourceBatch.source_batch_id == EquityEventWindowCoverage.source_batch_id,
                )
                .where(
                    EquityEventWindowCoverage.security_id == security_id,
                    EquityEventWindowCoverage.event_family == "CORPORATE_ACTION",
                )
                .order_by(
                    EquityEventWindowCoverage.coverage_from,
                    EquityEventWindowCoverage.created_at,
                )
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


def _action_revision_counts(engine: Engine, *, security_id: int) -> tuple[int, int]:
    """返回公司行动历史 revision 与当前 revision 数。"""
    with Session(engine) as session:
        total, current = session.execute(
            select(
                func.count(),
                func.count().filter(EquityCorporateActionVersion.valid_to.is_(None)),
            ).where(EquityCorporateActionVersion.security_id == security_id)
        ).one()
    return int(total), int(current)


def _action_transaction_counts(engine: Engine) -> tuple[int, int, int, int]:
    """统计失败调用可能触及的事实、publication、coverage 和来源观察。"""
    with Session(engine) as session:
        return (
            int(
                session.scalar(select(func.count()).select_from(EquityCorporateActionVersion)) or 0
            ),
            int(session.scalar(select(func.count()).select_from(DatasetPublication)) or 0),
            int(session.scalar(select(func.count()).select_from(EquityEventWindowCoverage)) or 0),
            int(session.scalar(select(func.count()).select_from(SourceBatch)) or 0),
        )


def _seed_dependencies(
    engine: Engine,
) -> tuple[tuple[EventCoverageIdentity, ...], UUID, dict[str, UUID]]:
    """批量写入五千确认身份、一个真实来源观察和五个 publication 外键。"""
    run_id = uuid4()
    source_batch_id = uuid4()
    now = _FIRST_CREATED
    identities = tuple(
        EventCoverageIdentity(
            security_id=9_100_000_000 + index,
            identifier_version_id=uuid5(
                NAMESPACE_URL,
                f"quant-v2:integration:event-coverage:identifier:{index}",
            ),
            exchange="SSE",
            symbol=f"{index + 1:06d}",
            coverage_from=_START,
            coverage_to=_END,
        )
        for index in range(5_000)
    )
    publication_ids = {family: uuid4() for _dataset, family in _DATASET_FAMILIES}
    with Session(engine) as session, session.begin():
        session.execute(
            insert(SyncRun).values(
                run_id=run_id,
                capability="integration.event-coverage",
                mode="backfill",
                request_key=f"integration.event-coverage.{run_id}",
                target_date=_END,
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
                provider_id="integration-event-provider",
                capability="integration.event-coverage",
                source_dataset_id=None,
                payload_sha256="a" * 64,
                raw_uri=f"s3://integration/{source_batch_id}",
                observed_at=_FIRST_OBSERVED,
                created_at=now,
                run_id=run_id,
                partition_key="integration:event-coverage",
                observation_seq=1,
                upstream_source="integration-provider",
                adapter_version="integration-v1",
                schema_fingerprint="b" * 64,
            )
        )
        session.execute(
            insert(EquityInstrument),
            [
                {
                    "security_id": identity.security_id,
                    "instrument_id": uuid5(
                        NAMESPACE_URL,
                        f"quant-v2:integration:event-coverage:instrument:{identity.security_id}",
                    ),
                    "exchange": identity.exchange,
                    "symbol": identity.symbol,
                    "name": f"覆盖测试{identity.symbol}",
                    "listing_status": "LISTED",
                    "created_at": now,
                    "updated_at": now,
                    "master_confirmed_at": now,
                    "current_master_version": None,
                }
                for identity in identities
            ],
        )
        session.execute(
            insert(EquityIdentifierVersion),
            [
                {
                    "version_id": identity.identifier_version_id,
                    "security_id": identity.security_id,
                    "exchange": identity.exchange,
                    "symbol": identity.symbol,
                    "identity_state": "CONFIRMED",
                    "effective_from": _START,
                    "effective_to": None,
                    "known_from": now,
                    "known_to": None,
                    "effective_date_precision": "OFFICIAL_DATE",
                    "source_batch_id": source_batch_id,
                    "content_sha256": b"c" * 32,
                }
                for identity in identities
            ],
        )
        session.execute(
            insert(DatasetPublication),
            [
                {
                    "publication_id": publication_ids[family],
                    "dataset": dataset,
                    "partition_key": f"integration:{family.casefold()}",
                    "data_version": uuid4(),
                    "release_id": None,
                    "quality_status": "passed",
                    "published_at": now,
                    "superseded_at": None,
                    "effective_as_of": _END,
                    "knowledge_cutoff": now,
                }
                for dataset, family in _DATASET_FAMILIES
            ],
        )
    return identities, source_batch_id, publication_ids


def _coverage_values(
    *,
    identities: tuple[EventCoverageIdentity, ...],
    source_batch_id: UUID,
    publication_ids: dict[str, UUID],
    observed_at: datetime,
    created_at: datetime,
    generation: str,
) -> tuple[coverage._EventCoverageWrite, ...]:  # noqa: SLF001
    """构造五族 coverage；每族前两只证券为真实非零，其余为真实空窗。"""
    return tuple(
        coverage._EventCoverageWrite(  # noqa: SLF001
            coverage_version=uuid5(
                NAMESPACE_URL,
                (
                    "quant-v2:integration:event-coverage:"
                    f"{generation}:{family}:{identity.security_id}"
                ),
            ),
            dataset=dataset,
            family=family,
            identity=identity,
            publication_id=publication_ids[family],
            source_batch_id=source_batch_id,
            record_count=1 if index < 2 else 0,
            coverage_scope="GLOBAL",
            universe_hash="d" * 64,
            universe_size=len(identities),
            observed_at=observed_at,
            created_at=created_at,
        )
        for dataset, family in _DATASET_FAMILIES
        for index, identity in enumerate(identities)
    )


def _schema_url(database_url: str, *, schema: str) -> str:
    """把迁移和批量写入限制在一次性 schema，避免触碰共享数据。"""
    url = make_url(database_url)
    query = dict(url.query)
    query["options"] = f"-csearch_path={schema}"
    return url.set(query=query).render_as_string(hide_password=False)
