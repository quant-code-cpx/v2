"""三周期行情窗口 coverage 在真实 PostgreSQL 上的发布、重放与回滚测试。"""

from __future__ import annotations

import hashlib
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, func, insert, select, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateSchema, DropSchema

from service_data_sync.application.ports.market_data import (
    EquitySourceObservation,
    PublishedDailyBars,
    PublishedEquityDataset,
)
from service_data_sync.domain.equity import (
    EquityBarPeriod,
    EquityDailyBar,
    EquityIdentifier,
    EquityPeriodBar,
)
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.database.fenced_execution import (
    FencedExecution,
    fenced_execution,
)
from service_data_sync.infrastructure.database.models.canonical import DatasetRelease
from service_data_sync.infrastructure.database.models.equity.identity.equity_identifier_version import (  # noqa: E501
    EquityIdentifierVersion,
)
from service_data_sync.infrastructure.database.models.equity.identity.equity_instrument import (
    EquityInstrument,
)
from service_data_sync.infrastructure.database.models.equity.market_data.equity_daily_bar import (
    EquityDailyBar as EquityDailyBarRevision,
)
from service_data_sync.infrastructure.database.models.equity.market_data.equity_monthly_bar import (
    EquityMonthlyBar,
)
from service_data_sync.infrastructure.database.models.equity.market_data.equity_weekly_bar import (
    EquityWeeklyBar,
)
from service_data_sync.infrastructure.database.models.execution.sync_run import SyncRun
from service_data_sync.infrastructure.database.models.provenance.source_batch import SourceBatch
from service_data_sync.infrastructure.database.models.publication.dataset_publication import (
    DatasetPublication,
)
from service_data_sync.infrastructure.database.models.publication.equity_bar_window_coverage import (  # noqa: E501
    EquityBarWindowCoverage,
)
from service_data_sync.infrastructure.persistence.bar_window_coverage import (
    publish_bar_window_coverage,
    resolve_bar_window_identity,
)
from service_data_sync.infrastructure.persistence.canonical_release_repository import (
    SqlAlchemyCanonicalReleaseRepository,
)
from service_data_sync.infrastructure.persistence.equity_market_data_repository import (
    SqlAlchemyEquityMarketDataRepository,
)
from service_data_sync.infrastructure.persistence.source_batch import (
    record_source_observation,
)

pytestmark = pytest.mark.integration

_START = date(2026, 7, 1)
_END = date(2026, 7, 31)
_IDENTIFIER = EquityIdentifier.parse("SSE.600519")


def test_three_period_zero_data_replay_lineage_and_fault_rollback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """日周月均以真实来源发布零记录或 DATA，精确重放且任一后置故障全回滚。"""
    if os.environ.get("DATA_SYNC_RUN_INTEGRATION") != "1":
        pytest.skip("requires DATA_SYNC_RUN_INTEGRATION=1")
    database_url = os.environ.get("DATA_SYNC_DATABASE_URL")
    if database_url is None:
        pytest.skip("requires DATA_SYNC_DATABASE_URL")
    schema = f"bar_window_coverage_{uuid4().hex}"
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
        command.upgrade(config, "202607300017")
        security_id, identifier_version_id = _seed_identity(engine)
        repository = SqlAlchemyEquityMarketDataRepository(database)

        zero_results = {}
        for offset, period in enumerate(EquityBarPeriod):
            result = _publish(
                repository,
                period=period,
                bars=(),
                source=_source(
                    period,
                    f"zero-{period.value}",
                    observed_at=datetime(2026, 8, 1, 8, offset, tzinfo=UTC),
                ),
            )
            zero_results[period] = result
            assert result.publication_kind == "ZERO_RECORD_COVERAGE"
            assert result.inserted_count == 0

        data_sources = {
            period: _source(
                period,
                f"data-{period.value}",
                observed_at=datetime(2026, 8, 1, 9, offset, tzinfo=UTC),
            )
            for offset, period in enumerate(EquityBarPeriod)
        }
        data_results = {}
        for period in EquityBarPeriod:
            result = _publish(
                repository,
                period=period,
                bars=(_bar(period),),
                source=data_sources[period],
            )
            data_results[period] = result
            assert result.publication_kind == "DATA"
            assert result.inserted_count == 1
            assert result.coverage_version != zero_results[period].coverage_version

        for period in EquityBarPeriod:
            replay = _publish(
                repository,
                period=period,
                bars=(_bar(period),),
                source=data_sources[period],
            )
            assert replay.coverage_version == data_results[period].coverage_version
            assert replay.data_version == data_results[period].data_version
            assert replay.source_batch_id == data_results[period].source_batch_id
            assert replay.unchanged_count == 1

        def replay_day(_index: int) -> PublishedDailyBars | PublishedEquityDataset:
            """并发重放同一日线观察，验证事务级窗口锁与不可变版本复用。"""
            return _publish(
                repository,
                period=EquityBarPeriod.DAY_1,
                bars=(_bar(EquityBarPeriod.DAY_1),),
                source=data_sources[EquityBarPeriod.DAY_1],
            )

        with ThreadPoolExecutor(max_workers=4) as executor:
            concurrent_replays = tuple(executor.map(replay_day, range(4)))
        assert {result.coverage_version for result in concurrent_replays} == {
            data_results[EquityBarPeriod.DAY_1].coverage_version
        }
        assert {result.data_version for result in concurrent_replays} == {
            data_results[EquityBarPeriod.DAY_1].data_version
        }

        def unexpected_finalizer(_session: Session, _execution: FencedExecution) -> None:
            """本段只验证终态证据记录，未武装的 finalizer 不应执行。"""
            raise AssertionError("unarmed finalizer must not run")

        fenced = FencedExecution(
            database=database,
            run_id=uuid4(),
            fencing_token=7,
            finalizer=unexpected_finalizer,
        )
        replay_source = data_sources[EquityBarPeriod.DAY_1]
        with fenced_execution(fenced), Session(engine) as session, session.begin():
            replay_source_batch_id = record_source_observation(
                session,
                provider_id=replay_source.provider_id,
                capability=replay_source.capability,
                source_payload_sha256=replay_source.raw_payload_sha256,
                raw_uri=replay_source.raw_uri,
                observed_at=replay_source.observed_at,
                created_at=datetime.now(UTC),
                upstream_source=replay_source.upstream_source,
                adapter_version=replay_source.adapter_version,
                schema_fingerprint=replay_source.schema_fingerprint,
            )
            replay_identity = resolve_bar_window_identity(
                session,
                period=EquityBarPeriod.DAY_1,
                identifier=_IDENTIFIER,
                start=_START,
                end=_END,
            )
            fenced_replay = publish_bar_window_coverage(
                session,
                release_repository=SqlAlchemyCanonicalReleaseRepository(database),
                period=EquityBarPeriod.DAY_1,
                identity=replay_identity,
                source=replay_source,
                source_batch_id=replay_source_batch_id,
                record_count=1,
                data_publication_version=data_results[EquityBarPeriod.DAY_1].data_version,
                now=datetime.now(UTC),
            )
        assert fenced_replay.source_batch_id == data_results[EquityBarPeriod.DAY_1].source_batch_id
        assert set(fenced.source_batch_ids) == {
            replay_source_batch_id,
            fenced_replay.source_batch_id,
        }
        assert fenced.checkpoint_kind == "bar-coverage-version"
        assert fenced.checkpoint_position == str(fenced_replay.coverage_version)

        coverages = _coverage_rows(engine, security_id=security_id)
        assert len(coverages) == 6
        assert {row["period"] for row in coverages} == {"1d", "1w", "1mo"}
        assert {row["identifier_version_id"] for row in coverages} == {identifier_version_id}
        assert all(row["quality_status"] == "passed" for row in coverages)
        assert all(row["release_id"] is not None for row in coverages)
        assert all(row["identity_hash"] != row["universe_hash"] for row in coverages)
        assert all(row["coverage_from"] == _START for row in coverages)
        assert all(row["coverage_to"] == _END for row in coverages)
        assert {
            (row["period"], row["publication_kind"], row["record_count"]) for row in coverages
        } == {
            ("1d", "ZERO_RECORD_COVERAGE", 0),
            ("1w", "ZERO_RECORD_COVERAGE", 0),
            ("1mo", "ZERO_RECORD_COVERAGE", 0),
            ("1d", "DATA", 1),
            ("1w", "DATA", 1),
            ("1mo", "DATA", 1),
        }
        assert {row["provider_id"] for row in coverages if row["superseded_at"] is None} == {
            "integration-bar-provider"
        }
        assert all(
            row["upstream_source"] == "integration-upstream"
            and row["adapter_version"] == "integration-akshare-v1"
            and row["schema_fingerprint"] == "c" * 64
            for row in coverages
        )
        assert _fact_counts(engine, security_id=security_id) == (1, 1, 1)

        before_stale = _transaction_counts(engine)
        with pytest.raises(ValueError, match="source observation regresses"):
            _publish(
                repository,
                period=EquityBarPeriod.DAY_1,
                bars=(_bar(EquityBarPeriod.DAY_1),),
                source=_source(
                    EquityBarPeriod.DAY_1,
                    "stale-observation",
                    observed_at=datetime(2026, 8, 1, 8, 30, tzinfo=UTC),
                ),
            )
        assert _transaction_counts(engine) == before_stale

        before_mismatch = _transaction_counts(engine)
        original_record_source = repository._record_source_batch

        def return_wrong_source_batch(*args: object, **kwargs: object) -> UUID:
            """注入另一次观察的 SourceBatch，验证 coverage 拒绝任意来源拼接。"""
            del args, kwargs
            return data_results[EquityBarPeriod.DAY_1].source_batch_id

        monkeypatch.setattr(repository, "_record_source_batch", return_wrong_source_batch)
        with pytest.raises(ValueError, match="does not match exact source observation"):
            repository.publish_daily_bars(
                identifier=_IDENTIFIER,
                bars=(),
                source=_source(
                    EquityBarPeriod.DAY_1,
                    "mismatched-source",
                    observed_at=datetime(2026, 8, 3, tzinfo=UTC),
                ),
                start=date(2026, 8, 1),
                end=date(2026, 8, 31),
            )
        assert _transaction_counts(engine) == before_mismatch
        monkeypatch.setattr(repository, "_record_source_batch", original_record_source)

        before_failure = _transaction_counts(engine)

        def fail_coverage(*args: object, **kwargs: object) -> None:
            """在事实与 publication 写入后注入 coverage 故障。"""
            del args, kwargs
            raise RuntimeError("forced bar coverage failure")

        monkeypatch.setattr(
            "service_data_sync.infrastructure.persistence.equity_market_data_repository."
            "publish_bar_window_coverage",
            fail_coverage,
        )
        with pytest.raises(RuntimeError, match="forced bar coverage failure"):
            repository.publish_daily_bars(
                identifier=_IDENTIFIER,
                bars=(
                    EquityDailyBar(
                        trade_date=date(2026, 8, 3),
                        open_price=Decimal("12"),
                        high_price=Decimal("13"),
                        low_price=Decimal("11"),
                        close_price=Decimal("12.5"),
                        volume_shares=2_000,
                        amount_cny=Decimal("25000"),
                        turnover_rate=Decimal("0.02"),
                    ),
                ),
                source=_source(
                    EquityBarPeriod.DAY_1,
                    "rollback",
                    observed_at=datetime(2026, 8, 4, tzinfo=UTC),
                ),
                start=date(2026, 8, 1),
                end=date(2026, 8, 31),
            )
        assert _transaction_counts(engine) == before_failure

        original_oid = _table_oid(engine)
        original_snapshot = _coverage_snapshot(engine, security_id=security_id)
        command.downgrade(config, "202607300016")
        assert _table_oid(engine) == original_oid
        assert _coverage_snapshot(engine, security_id=security_id) == original_snapshot
        assert _alembic_revision(engine) == "202607300016"
        command.upgrade(config, "202607300017")
        assert _table_oid(engine) == original_oid
        assert _coverage_snapshot(engine, security_id=security_id) == original_snapshot

        command.downgrade(config, "202607300016")
        with engine.begin() as connection:
            connection.execute(text("DROP INDEX ix_equity_bar_coverage_read"))
        with pytest.raises(RuntimeError, match="indexes have drifted"):
            command.upgrade(config, "202607300017")
        assert _alembic_revision(engine) == "202607300016"
    finally:
        database.close()
        with admin_engine.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True))
        admin_engine.dispose()


def _publish(
    repository: SqlAlchemyEquityMarketDataRepository,
    *,
    period: EquityBarPeriod,
    bars: tuple[EquityDailyBar | EquityPeriodBar, ...],
    source: EquitySourceObservation,
) -> PublishedDailyBars | PublishedEquityDataset:
    """按周期调用真实仓储，并保留统一结果形状供测试断言。"""
    if period is EquityBarPeriod.DAY_1:
        return repository.publish_daily_bars(
            identifier=_IDENTIFIER,
            bars=tuple(bar for bar in bars if isinstance(bar, EquityDailyBar)),
            source=source,
            start=_START,
            end=_END,
        )
    return repository.publish_period_bars(
        identifier=_IDENTIFIER,
        period=period,
        bars=tuple(bar for bar in bars if isinstance(bar, EquityPeriodBar)),
        source=source,
        start=_START,
        end=_END,
    )


def _bar(period: EquityBarPeriod) -> EquityDailyBar | EquityPeriodBar:
    """构造一个落在测试闭区间内、单位可核对的周期事实。"""
    values = {
        "open_price": Decimal("10"),
        "high_price": Decimal("11"),
        "low_price": Decimal("9"),
        "close_price": Decimal("10.5"),
        "volume_shares": 1_000,
        "amount_cny": Decimal("10500"),
        "turnover_rate": Decimal("0.01"),
    }
    if period is EquityBarPeriod.DAY_1:
        return EquityDailyBar(trade_date=date(2026, 7, 15), **values)
    return EquityPeriodBar(
        period=period,
        period_end=date(2026, 7, 24) if period is EquityBarPeriod.WEEK_1 else _END,
        **values,
    )


def _seed_identity(engine: Engine) -> tuple[int, UUID]:
    """写入一个有真实来源批次且完整覆盖测试窗口的确认证券身份。"""
    run_id = uuid4()
    source_batch_id = uuid4()
    security_id = 9_300_000_001
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
                name="三周期集成样本",
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


def _source(
    period: EquityBarPeriod,
    suffix: str,
    *,
    observed_at: datetime,
) -> EquitySourceObservation:
    """构造 raw 与 normalized 双对象齐全的可复验行情来源。"""
    raw_digest = hashlib.sha256(f"raw:{suffix}".encode()).hexdigest()
    normalized_digest = hashlib.sha256(f"normalized:{suffix}".encode()).hexdigest()
    return EquitySourceObservation(
        provider_id="integration-bar-provider",
        capability=period.capability,
        raw_payload_sha256=raw_digest,
        raw_uri=f"s3://integration/raw/{suffix}.json",
        raw_content_type="application/json",
        raw_byte_size=128,
        normalized_payload_sha256=normalized_digest,
        normalized_uri=f"s3://integration/normalized/{suffix}.json",
        normalized_content_type="application/vnd.quant-v2.equity-bar+json",
        normalized_byte_size=96,
        observed_at=observed_at,
        upstream_source="integration-upstream",
        adapter_version="integration-akshare-v1",
        schema_fingerprint="c" * 64,
    )


def _coverage_rows(engine: Engine, *, security_id: int) -> list[dict[str, object]]:
    """读取 coverage、publication 和精确 SourceBatch 的联合血缘。"""
    with Session(engine) as session:
        rows = (
            session.execute(
                select(
                    EquityBarWindowCoverage.period,
                    EquityBarWindowCoverage.identifier_version_id,
                    EquityBarWindowCoverage.coverage_from,
                    EquityBarWindowCoverage.coverage_to,
                    EquityBarWindowCoverage.publication_kind,
                    EquityBarWindowCoverage.quality_status,
                    EquityBarWindowCoverage.record_count,
                    EquityBarWindowCoverage.identity_hash,
                    EquityBarWindowCoverage.universe_hash,
                    EquityBarWindowCoverage.superseded_at,
                    DatasetPublication.release_id,
                    SourceBatch.provider_id,
                    SourceBatch.upstream_source,
                    SourceBatch.adapter_version,
                    SourceBatch.schema_fingerprint,
                )
                .join(
                    DatasetPublication,
                    DatasetPublication.publication_id == EquityBarWindowCoverage.publication_id,
                )
                .join(
                    SourceBatch,
                    SourceBatch.source_batch_id == EquityBarWindowCoverage.source_batch_id,
                )
                .where(EquityBarWindowCoverage.security_id == security_id)
                .order_by(
                    EquityBarWindowCoverage.period,
                    EquityBarWindowCoverage.created_at,
                )
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


def _fact_counts(engine: Engine, *, security_id: int) -> tuple[int, int, int]:
    """返回三个物理行情表的历史 revision 数量。"""
    with Session(engine) as session:
        return (
            int(
                session.scalar(
                    select(func.count())
                    .select_from(EquityDailyBarRevision)
                    .where(EquityDailyBarRevision.security_id == security_id)
                )
                or 0
            ),
            int(
                session.scalar(
                    select(func.count())
                    .select_from(EquityWeeklyBar)
                    .where(EquityWeeklyBar.security_id == security_id)
                )
                or 0
            ),
            int(
                session.scalar(
                    select(func.count())
                    .select_from(EquityMonthlyBar)
                    .where(EquityMonthlyBar.security_id == security_id)
                )
                or 0
            ),
        )


def _transaction_counts(engine: Engine) -> tuple[int, int, int, int, int]:
    """统计故障调用会触及的事实、release、publication、coverage 和来源行。"""
    with Session(engine) as session:
        fact_total = sum(_fact_counts(engine, security_id=9_300_000_001))
        return (
            fact_total,
            int(session.scalar(select(func.count()).select_from(DatasetRelease)) or 0),
            int(session.scalar(select(func.count()).select_from(DatasetPublication)) or 0),
            int(session.scalar(select(func.count()).select_from(EquityBarWindowCoverage)) or 0),
            int(session.scalar(select(func.count()).select_from(SourceBatch)) or 0),
        )


def _coverage_snapshot(engine: Engine, *, security_id: int) -> tuple[tuple[object, ...], ...]:
    """读取全部不可变覆盖字段，验证降级与重新前滚未重建或改写行。"""
    with Session(engine) as session:
        rows = session.execute(
            select(
                EquityBarWindowCoverage.coverage_id,
                EquityBarWindowCoverage.coverage_version,
                EquityBarWindowCoverage.period,
                EquityBarWindowCoverage.capability,
                EquityBarWindowCoverage.identifier_version_id,
                EquityBarWindowCoverage.coverage_from,
                EquityBarWindowCoverage.coverage_to,
                EquityBarWindowCoverage.publication_id,
                EquityBarWindowCoverage.source_batch_id,
                EquityBarWindowCoverage.publication_kind,
                EquityBarWindowCoverage.record_count,
                EquityBarWindowCoverage.identity_hash,
                EquityBarWindowCoverage.universe_hash,
                EquityBarWindowCoverage.observed_at,
                EquityBarWindowCoverage.created_at,
                EquityBarWindowCoverage.superseded_at,
            )
            .where(EquityBarWindowCoverage.security_id == security_id)
            .order_by(EquityBarWindowCoverage.coverage_version)
        ).all()
    return tuple(tuple(row) for row in rows)


def _table_oid(engine: Engine) -> int:
    """读取覆盖表 OID，证明降级和重新前滚保留同一物理表。"""
    with engine.connect() as connection:
        value = connection.scalar(text("SELECT 'equity_bar_window_coverage'::regclass::oid"))
    assert isinstance(value, int)
    return value


def _alembic_revision(engine: Engine) -> str:
    """读取独占 schema 的当前 Alembic revision。"""
    with engine.connect() as connection:
        value = connection.scalar(text("SELECT version_num FROM alembic_version"))
    assert isinstance(value, str)
    return value


def _schema_url(database_url: str, *, schema: str) -> str:
    """把迁移与集成写入限制在一次性 schema，避免触碰共享数据。"""
    url = make_url(database_url)
    query = dict(url.query)
    query["options"] = f"-csearch_path={schema}"
    return url.set(query=query).render_as_string(hide_password=False)
