"""标准日线修订与发布 SQL 编排的单元测试。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from service_data_sync.application.ports.market_data import (
    EquityIdentityReadConflictError,
    EquitySourceObservation,
    StoredEquityInstrument,
)
from service_data_sync.domain.equity import (
    EquityAdjustmentFactor,
    EquityBarPeriod,
    EquityCompanyProfile,
    EquityCorporateAction,
    EquityDailyBar,
    EquityIdentifier,
    EquityPeriodBar,
)
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.database.models.equity.market_data.equity_weekly_bar import (
    EquityWeeklyBar,
)
from service_data_sync.infrastructure.persistence import (
    equity_market_data_repository as equity_market_repository,
)
from service_data_sync.infrastructure.persistence.bar_window_coverage import (
    BarWindowIdentity,
    PublishedBarWindowCoverage,
)
from service_data_sync.infrastructure.persistence.equity_market_data_repository import (
    PossibleCodeReuseError,
    SqlAlchemyEquityMarketDataRepository,
    _action_content_hash,
    _at_knowledge_time,
    _factor_content_hash,
    _period_bar_content_hash,
    _security_partition_key,
)
from service_data_sync.infrastructure.persistence.event_window_coverage import (
    EventCoverageIdentity,
)


class FakeResult:
    """在不使用真实数据库时模拟仓储所需的 SQLAlchemy 映射结果。"""

    def __init__(self, value: object) -> None:
        """保存一个排队的单行、可选行或多行响应。"""
        self._value = value

    def mappings(self) -> FakeResult:
        """排队值已是映射形字典，因此返回当前替身。"""
        return self

    def scalar_one(self) -> object:
        """模拟 ORM-enabled `RETURNING` 的单一标量结果。"""
        if isinstance(self._value, dict):
            for key in ("source_batch_id", "security_id", "data_version"):
                if key in self._value:
                    return self._value[key]
        return self._value

    def scalar_one_or_none(self) -> object | None:
        """模拟可无结果的 ORM 标量查询。"""
        if self._value is None:
            return None
        return self.scalar_one()

    def one_or_none(self) -> object:
        """为允许无匹配的 SELECT 返回可选排队行。"""
        return self._value

    def one(self) -> object:
        """为 `INSERT ... RETURNING` 返回必需的排队行。"""
        assert self._value is not None
        return self._value

    def all(self) -> list[object]:
        """为有界目录和日线查询返回排队行列表。"""
        assert isinstance(self._value, list)
        return self._value


def test_revision_visibility_can_be_pinned_to_publication_cutoff() -> None:
    """leaf 查询必须按 publication 截止点重放 revision，避免发布切换竞态。"""
    statement = _at_knowledge_time(
        select(EquityWeeklyBar),
        model=EquityWeeklyBar,
        known_at=datetime(2026, 7, 28, 8, tzinfo=UTC),
    )
    sql = str(statement)

    assert "equity_weekly_bar.valid_from <=" in sql
    assert "equity_weekly_bar.valid_to IS NULL" in sql
    assert "equity_weekly_bar.valid_to >" in sql


class FakeConnection:
    """按顺序回放 SQL 响应，并记录语句供断言使用。"""

    def __init__(self, responses: list[object]) -> None:
        """初始化由 `begin`/`connect` 上下文共享的确定性响应队列。"""
        self._responses = responses
        self.statements: list[str] = []
        self.statement_objects: list[object] = []

    def execute(self, statement: object, parameters: object = None) -> FakeResult:
        """记录 SQL 文本；调用方读取结果时才消费一项响应。"""
        self.statements.append(str(statement))
        self.statement_objects.append(statement)
        del parameters
        response = self._responses.pop(0) if self._responses else None
        return FakeResult(response)


class FakeEngine:
    """暴露兼容 SQLAlchemy 引擎接口的事务与读取上下文。"""

    def __init__(self, responses: list[object]) -> None:
        """创建共享替身连接，保证跨调用的响应顺序确定。"""
        self.connection = FakeConnection(responses)

    @contextmanager
    def begin(self) -> Iterator[FakeConnection]:
        """产出事务形连接，但不提交外部状态。"""
        yield self.connection

    @contextmanager
    def connect(self) -> Iterator[FakeConnection]:
        """产出读取形连接，但不打开真实套接字。"""
        yield self.connection


class FakeDatabase:
    """以短生命周期 Session 接口模拟 `DatabaseClient`。"""

    def __init__(self, engine: FakeEngine) -> None:
        """保存承载测试响应队列的连接替身。"""
        self._engine = engine

    @contextmanager
    def transaction(self) -> Iterator[FakeConnection]:
        """提供一次原子写入使用的模拟 Session。"""
        with self._engine.begin() as connection:
            yield connection

    @contextmanager
    def session(self) -> Iterator[FakeConnection]:
        """提供一次只读调用使用的模拟 Session。"""
        with self._engine.connect() as connection:
            yield connection


def test_repository_appends_changed_bar_and_advances_publication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """非空日线在同一事务中写事实、DATA publication 和窗口覆盖。"""
    instrument_id = uuid4()
    source_batch_id = uuid4()
    data_version = uuid4()
    coverage_version = uuid4()
    instrument = StoredEquityInstrument(
        security_id=1,
        instrument_id=instrument_id,
        identifier=EquityIdentifier.parse("SSE.600519"),
        name="贵州茅台",
        listing_status="LISTED",
    )
    identity = _bar_identity(
        instrument.identifier,
        start=date(2026, 6, 1),
        end=date(2026, 6, 30),
    )
    repository = _repository(FakeEngine([]))
    record_source = Mock(return_value=source_batch_id)
    write_revisions = Mock(return_value=(1, 0))
    publish = Mock(return_value=data_version)
    clear = Mock()
    publish_coverage = Mock(
        return_value=PublishedBarWindowCoverage(
            data_version=data_version,
            coverage_version=coverage_version,
            source_batch_id=source_batch_id,
            publication_kind="DATA",
            record_count=1,
        )
    )
    monkeypatch.setattr(repository, "_record_source_batch", record_source)
    monkeypatch.setattr(
        equity_market_repository,
        "resolve_bar_window_identity",
        Mock(return_value=identity),
    )
    monkeypatch.setattr(
        repository,
        "_bar_window_instrument_on_connection",
        Mock(return_value=instrument),
    )
    monkeypatch.setattr(repository, "_write_revisions", write_revisions)
    monkeypatch.setattr(repository, "_publish", publish)
    monkeypatch.setattr(repository, "_clear_daily_bar_availability_on_connection", clear)
    monkeypatch.setattr(
        equity_market_repository,
        "publish_bar_window_coverage",
        publish_coverage,
    )

    publication = repository.publish_daily_bars(
        identifier=instrument.identifier,
        bars=(_bar(),),
        source=_source("equity.bar.1d.raw"),
        start=date(2026, 6, 1),
        end=date(2026, 6, 30),
    )

    assert publication.instrument.instrument_id == instrument_id
    assert publication.data_version == data_version
    assert publication.coverage_version == coverage_version
    assert publication.source_batch_id == source_batch_id
    assert publication.publication_kind == "DATA"
    assert publication.inserted_count == 1
    assert publication.unchanged_count == 0
    assert record_source.call_args.kwargs["upstream_source"] == "test-upstream"
    assert publish_coverage.call_args.kwargs["record_count"] == 1
    assert publish_coverage.call_args.kwargs["data_publication_version"] == data_version
    clear.assert_called_once()


def test_repository_keeps_current_publication_when_all_bars_are_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """重复事实仍关联复用的 DATA publication，并形成精确窗口 coverage。"""
    identifier = EquityIdentifier.parse("SSE.600519")
    instrument = StoredEquityInstrument(
        security_id=1,
        instrument_id=uuid4(),
        identifier=identifier,
        name="贵州茅台",
        listing_status="LISTED",
    )
    source_batch_id = uuid4()
    data_version = uuid4()
    repository = _repository(FakeEngine([]))
    monkeypatch.setattr(repository, "_record_source_batch", Mock(return_value=source_batch_id))
    monkeypatch.setattr(
        equity_market_repository,
        "resolve_bar_window_identity",
        Mock(
            return_value=_bar_identity(
                identifier,
                start=date(2026, 6, 1),
                end=date(2026, 6, 30),
            )
        ),
    )
    monkeypatch.setattr(
        repository,
        "_bar_window_instrument_on_connection",
        Mock(return_value=instrument),
    )
    monkeypatch.setattr(repository, "_write_revisions", Mock(return_value=(0, 1)))
    monkeypatch.setattr(repository, "_publish", Mock(return_value=data_version))
    monkeypatch.setattr(repository, "_clear_daily_bar_availability_on_connection", Mock())
    monkeypatch.setattr(
        equity_market_repository,
        "publish_bar_window_coverage",
        Mock(
            return_value=PublishedBarWindowCoverage(
                data_version=data_version,
                coverage_version=uuid4(),
                source_batch_id=source_batch_id,
                publication_kind="DATA",
                record_count=1,
            )
        ),
    )

    publication = repository.publish_daily_bars(
        identifier=identifier,
        bars=(_bar(),),
        source=_source("equity.bar.1d.raw"),
        start=date(2026, 6, 1),
        end=date(2026, 6, 30),
    )

    assert publication.data_version == data_version
    assert publication.inserted_count == 0
    assert publication.unchanged_count == 1


def test_repository_rejects_daily_window_without_confirmed_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未确认或不能完整覆盖闭区间的代码不得创建 PENDING 行情成功结果。"""
    repository = _repository(FakeEngine([]))
    write_revisions = Mock()
    monkeypatch.setattr(repository, "_record_source_batch", Mock(return_value=uuid4()))
    monkeypatch.setattr(
        equity_market_repository,
        "resolve_bar_window_identity",
        Mock(side_effect=ValueError("confirmed identity required")),
    )
    monkeypatch.setattr(repository, "_write_revisions", write_revisions)

    with pytest.raises(ValueError, match="confirmed identity"):
        repository.publish_daily_bars(
            identifier=EquityIdentifier.parse("SZSE.000001"),
            bars=(_bar(),),
            source=_source("equity.bar.1d.raw"),
            start=date(2026, 6, 1),
            end=date(2026, 6, 30),
        )

    write_revisions.assert_not_called()


def test_repository_rejects_possible_code_reuse_after_explicit_delisting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """退市后同代码行情不得误绑旧证券或自行创建未经确认的新证券。"""
    repository = _repository(FakeEngine([]))
    write_revisions = Mock()
    monkeypatch.setattr(repository, "_record_source_batch", Mock(return_value=uuid4()))
    monkeypatch.setattr(
        equity_market_repository,
        "resolve_bar_window_identity",
        Mock(side_effect=PossibleCodeReuseError("possible code reuse after delisting")),
    )
    monkeypatch.setattr(repository, "_write_revisions", write_revisions)

    with pytest.raises(PossibleCodeReuseError, match="possible code reuse"):
        repository.publish_daily_bars(
            identifier=EquityIdentifier.parse("SSE.600519"),
            bars=(_bar(),),
            source=_source("equity.bar.1d.raw"),
            start=date(2026, 6, 1),
            end=date(2026, 6, 30),
        )

    write_revisions.assert_not_called()


def test_repository_reads_instrument_catalog_and_current_daily_bars() -> None:
    """将 SQL 行映射为供内部读取方使用的标准领域值。"""
    instrument_id = uuid4()
    engine = FakeEngine(
        [
            _instrument_row(instrument_id),
            [_instrument_row(instrument_id)],
            [
                {
                    "trade_date": date(2026, 6, 30),
                    "open_price": Decimal("10"),
                    "high_price": Decimal("11"),
                    "low_price": Decimal("9"),
                    "close_price": Decimal("10.5"),
                    "volume_shares": 1_000,
                    "amount_cny": Decimal("10500"),
                    "turnover_rate": None,
                    "revision": 1,
                    "is_final": True,
                }
            ],
        ]
    )
    repository = _repository(engine)

    stored = repository.get_instrument(instrument_id)

    assert stored is not None
    assert stored.identifier.qualified_symbol == "SSE.600519"
    assert repository.list_instruments(query="600", limit=10)[0].instrument_id == instrument_id
    bars = repository.list_daily_bars(
        instrument_id=instrument_id,
        start=date(2026, 6, 1),
        end=date(2026, 6, 30),
    )

    assert bars[0][0].close_price == Decimal("10.5")
    assert bars[0][1:] == (1, True)


def test_repository_resolves_one_confirmed_security_for_the_fact_window() -> None:
    """读取身份必须同时约束事实窗口和当前知识区间。"""
    instrument_id = uuid4()
    row = {**_instrument_row(instrument_id), "identity_state": "CONFIRMED"}
    engine = FakeEngine([[row]])
    repository = _repository(engine)

    instrument = repository.get_instrument_by_identifier(
        EquityIdentifier.parse("SSE.600519"),
        fact_start=date(2020, 1, 1),
        fact_end=date(2026, 7, 28),
    )

    assert instrument is not None and instrument.instrument_id == instrument_id
    statement = engine.connection.statements[0]
    assert "knowledge_range @>" in statement
    assert "effective_from <=" in statement
    assert "effective_to >" in statement


def test_repository_rejects_reused_or_pending_identity_windows() -> None:
    """代码复用或 PENDING 区间都不能被读取方任选一个 `security_id`。"""
    first = {**_instrument_row(uuid4()), "identity_state": "CONFIRMED"}
    second = {
        **first,
        "security_id": 2,
        "instrument_id": uuid4(),
    }
    reused = _repository(FakeEngine([[first, second]]))
    pending = _repository(FakeEngine([[{**_instrument_row(uuid4()), "identity_state": "PENDING"}]]))

    with pytest.raises(EquityIdentityReadConflictError):
        reused.get_instrument_by_identifier(
            EquityIdentifier.parse("SSE.600519"),
            fact_start=None,
            fact_end=date(2026, 7, 28),
        )
    with pytest.raises(EquityIdentityReadConflictError):
        pending.get_instrument_by_identifier(
            EquityIdentifier.parse("SSE.600519"),
            fact_start=date(2026, 7, 28),
            fact_end=date(2026, 7, 28),
        )


def test_publication_prefers_security_partition_and_guards_legacy_fallback() -> None:
    """旧代码分区只在未复用时兼容读取，并保留已通过质量状态和旧行默认值。"""
    publication_row = {
        "data_version": uuid4(),
        "published_at": datetime(2026, 7, 28, tzinfo=UTC),
        "quality_status": "passed",
    }
    instrument = StoredEquityInstrument(
        security_id=1,
        instrument_id=uuid4(),
        identifier=EquityIdentifier.parse("SSE.600519"),
        name="贵州茅台",
        listing_status="LISTED",
    )
    stable_engine = FakeEngine([publication_row])
    legacy_engine = FakeEngine([None, [{"security_id": 1}], publication_row])
    reused_engine = FakeEngine([None, [{"security_id": 1}, {"security_id": 2}]])
    compatibility_engine = FakeEngine(
        [
            {
                "data_version": uuid4(),
                "published_at": datetime(2026, 7, 28, tzinfo=UTC),
            }
        ]
    )

    stable = _repository(stable_engine).get_current_publication(
        dataset="equity.profile",
        instrument=instrument,
    )
    legacy = _repository(legacy_engine).get_current_publication(
        dataset="equity.profile",
        instrument=instrument,
    )
    reused = _repository(reused_engine).get_current_publication(
        dataset="equity.profile",
        instrument=instrument,
    )
    compatibility = _repository(compatibility_engine).get_current_publication(
        dataset="equity.profile",
        instrument=instrument,
    )

    assert _security_partition_key(1) == "security:1"
    assert stable is not None
    assert legacy is not None
    assert reused is None
    assert compatibility is not None
    assert stable.quality_status == "passed"
    assert legacy.quality_status == "passed"
    assert compatibility.quality_status == "passed"
    assert "dataset_publication.quality_status" in stable_engine.connection.statements[0]
    assert "dataset_publication.quality_status =" in stable_engine.connection.statements[0]
    assert len(stable_engine.connection.statements) == 1
    assert len(legacy_engine.connection.statements) == 3
    assert len(reused_engine.connection.statements) == 2


def test_publication_source_requires_one_release_bound_observation() -> None:
    """来源投影只接受 release 规范化运行唯一绑定的批次，避免任取同运行的其他观察。"""
    source_batch_id = uuid4()
    source_row = {
        "source_batch_id": source_batch_id,
        "provider_id": "akshare-sina-adjustment-factor",
        "upstream_source": "sina-hfq-factor",
        "adapter_version": "akshare-1.18.81-v1",
    }
    data_version = uuid4()
    exact_engine = FakeEngine([[source_row]])
    ambiguous_engine = FakeEngine([[source_row, source_row]])

    exact = _repository(exact_engine).get_publication_source(
        dataset="equity.adjustment_factor",
        data_version=data_version,
    )
    ambiguous = _repository(ambiguous_engine).get_publication_source(
        dataset="equity.adjustment_factor",
        data_version=data_version,
    )

    assert exact is not None
    assert exact.source_batch_id == source_batch_id
    assert exact.provider_id == "akshare-sina-adjustment-factor"
    assert ambiguous is None
    statement = exact_engine.connection.statements[0]
    assert "dataset_release" in statement
    assert "normalization_run" in statement
    assert "source_batch" in statement


def test_repository_orchestrates_all_equity_extension_publications(monkeypatch) -> None:
    """四种扩展数据在单事务内写来源、修订、发布和独立 checkpoint。"""
    repository = _repository(FakeEngine([]))
    instrument = StoredEquityInstrument(
        security_id=1,
        instrument_id=uuid4(),
        identifier=EquityIdentifier.parse("SSE.600519"),
        name="贵州茅台",
        listing_status="LISTED",
    )
    data_version = uuid4()
    record_source = Mock(return_value=uuid4())
    ensure_instrument = Mock(return_value=instrument)
    confirmed_instrument = Mock(return_value=instrument)
    write_period = Mock(return_value=(1, 0))
    write_factors = Mock(return_value=(1, 0))
    write_actions = Mock(return_value=(1, 0))
    write_profile = Mock(return_value=(1, 0))
    publish = Mock(return_value=data_version)
    advance = Mock()
    event_coverage_identity = EventCoverageIdentity(
        security_id=instrument.security_id,
        identifier_version_id=uuid4(),
        exchange=instrument.identifier.exchange.value,
        symbol=instrument.identifier.symbol,
        coverage_from=date(2026, 1, 1),
        coverage_to=date(2026, 7, 28),
    )
    bar_coverage_identity = _bar_identity(instrument.identifier)
    bar_source_batch_id = uuid4()
    publish_event_coverage = Mock()
    publish_bar_coverage = Mock(
        return_value=PublishedBarWindowCoverage(
            data_version=data_version,
            coverage_version=uuid4(),
            source_batch_id=bar_source_batch_id,
            publication_kind="DATA",
            record_count=1,
        )
    )
    monkeypatch.setattr(repository, "_record_source_batch", record_source)
    monkeypatch.setattr(repository, "_ensure_instrument", ensure_instrument)
    monkeypatch.setattr(repository, "_confirmed_instrument_on_connection", confirmed_instrument)
    monkeypatch.setattr(repository, "_write_period_revisions", write_period)
    monkeypatch.setattr(repository, "_write_factor_revisions", write_factors)
    monkeypatch.setattr(repository, "_write_action_revisions", write_actions)
    monkeypatch.setattr(repository, "_write_profile_revision", write_profile)
    monkeypatch.setattr(repository, "_publish", publish)
    monkeypatch.setattr(repository, "_advance_checkpoint", advance)
    monkeypatch.setattr(
        repository,
        "_bar_window_instrument_on_connection",
        Mock(return_value=instrument),
    )
    monkeypatch.setattr(
        equity_market_repository,
        "resolve_bar_window_identity",
        Mock(return_value=bar_coverage_identity),
    )
    monkeypatch.setattr(
        equity_market_repository,
        "publish_bar_window_coverage",
        publish_bar_coverage,
    )
    monkeypatch.setattr(
        equity_market_repository,
        "resolve_event_coverage_identities",
        Mock(return_value=((event_coverage_identity,), "INSTRUMENT", "b" * 64)),
    )
    monkeypatch.setattr(
        equity_market_repository,
        "_release_bridge_context",
        Mock(return_value=(uuid4(), uuid4())),
    )
    monkeypatch.setattr(
        equity_market_repository,
        "publish_event_window_coverages",
        publish_event_coverage,
    )
    source = _source()
    weekly_source = _source("equity.bar.1w.raw")
    identifier = instrument.identifier

    weekly = repository.publish_period_bars(
        identifier=identifier,
        period=EquityBarPeriod.WEEK_1,
        bars=(_period_bar(EquityBarPeriod.WEEK_1, date(2026, 7, 24)),),
        source=weekly_source,
        start=date(2026, 1, 1),
        end=date(2026, 7, 28),
    )
    factors = repository.publish_adjustment_factors(
        identifier=identifier,
        factors=(
            EquityAdjustmentFactor(
                effective_date=date(2026, 1, 1),
                cumulative_factor=Decimal("2"),
            ),
        ),
        source=source,
        window_end=date(2026, 7, 28),
    )
    actions = repository.publish_corporate_actions(
        identifier=identifier,
        actions=(_action(),),
        source=source,
        start=date(2026, 1, 1),
        end=date(2026, 7, 28),
    )
    profile = repository.publish_company_profile(
        identifier=identifier,
        profile=_profile(),
        source=source,
    )

    assert {
        weekly.data_version,
        factors.data_version,
        actions.data_version,
        profile.data_version,
    } == {data_version}
    assert record_source.call_count == 4
    assert advance.call_count == 4
    publish_event_coverage.assert_called_once()
    publish_bar_coverage.assert_called_once()
    with pytest.raises(ValueError, match="weekly or monthly"):
        repository.publish_period_bars(
            identifier=identifier,
            period=EquityBarPeriod.DAY_1,
            bars=(),
            source=_source("equity.bar.1d.raw"),
            start=date(2026, 1, 1),
            end=date(2026, 7, 28),
        )
    with pytest.raises(ValueError, match="match requested"):
        repository.publish_period_bars(
            identifier=identifier,
            period=EquityBarPeriod.MONTH_1,
            bars=(_period_bar(EquityBarPeriod.WEEK_1, date(2026, 7, 24)),),
            source=_source("equity.bar.1mo.raw"),
            start=date(2026, 1, 1),
            end=date(2026, 7, 28),
        )
    empty_factors = repository.publish_adjustment_factors(
        identifier=identifier,
        factors=(),
        source=source,
        window_end=date(2026, 7, 28),
    )
    assert empty_factors.data_version == data_version
    assert empty_factors.inserted_count == 0
    assert empty_factors.unchanged_count == 0
    assert empty_factors.source_batch_id is not None
    assert record_source.call_count == 5
    assert advance.call_count == 5
    assert write_factors.call_count == 1
    assert any(
        call.kwargs["fact_dates"] == (date(2026, 7, 28),)
        for call in confirmed_instrument.call_args_list
    )


def test_repository_writes_extension_revisions_and_checkpoints() -> None:
    """扩展修订辅助逻辑区分未变化、变化、首次写入及 checkpoint 更新。"""
    repository = _repository(FakeEngine([]))
    source_batch_id = uuid4()
    observed_at = datetime(2026, 7, 28, tzinfo=UTC)
    weekly_unchanged = _period_bar(EquityBarPeriod.WEEK_1, date(2026, 7, 17))
    weekly_changed = _period_bar(EquityBarPeriod.WEEK_1, date(2026, 7, 24))
    period_connection = FakeConnection(
        [
            {
                "revision": 1,
                "content_sha256": _period_bar_content_hash(weekly_unchanged),
            },
            {"revision": 1, "content_sha256": b"changed"},
            None,
            None,
        ]
    )

    assert repository._write_period_revisions(
        cast(Any, period_connection),
        model=EquityWeeklyBar,
        security_id=1,
        bars=(weekly_unchanged, weekly_changed),
        source_batch_id=source_batch_id,
        observed_at=observed_at,
    ) == (1, 1)

    factor_unchanged = EquityAdjustmentFactor(
        effective_date=date(2025, 1, 1),
        cumulative_factor=Decimal("1"),
    )
    factor_changed = EquityAdjustmentFactor(
        effective_date=date(2026, 1, 1),
        cumulative_factor=Decimal("2"),
    )
    factor_connection = FakeConnection(
        [
            {
                "revision": 1,
                "content_sha256": _factor_content_hash(factor_unchanged),
            },
            {"revision": 2, "content_sha256": b"changed"},
            None,
            None,
        ]
    )
    assert repository._write_factor_revisions(
        cast(Any, factor_connection),
        security_id=1,
        factors=(factor_unchanged, factor_changed),
        factor_version=uuid4(),
        source_batch_id=source_batch_id,
        observed_at=observed_at,
    ) == (1, 1)

    unchanged_action = _action()
    changed_action = EquityCorporateAction(
        source_event_key="2024",
        report_period=date(2024, 12, 31),
        status="实施",
        announcement_date=None,
        record_date=None,
        ex_date=None,
        cash_dividend_per_10=None,
        bonus_shares_per_10=None,
        transfer_shares_per_10=None,
    )
    action_connection = FakeConnection(
        [
            {
                "revision": 1,
                "content_sha256": _action_content_hash(unchanged_action),
            },
            {"revision": 1, "content_sha256": b"changed"},
            None,
            None,
        ]
    )
    assert repository._write_action_revisions(
        cast(Any, action_connection),
        identifier=EquityIdentifier.parse("SSE.600519"),
        security_id=1,
        actions=(unchanged_action, changed_action),
        source_batch_id=source_batch_id,
        observed_at=observed_at,
    ) == (1, 1)

    profile_connection = FakeConnection([None, None])
    assert repository._write_profile_revision(
        cast(Any, profile_connection),
        security_id=1,
        profile=_profile(),
        source_batch_id=source_batch_id,
        observed_at=observed_at,
    ) == (1, 0)
    checkpoint_insert = FakeConnection([None, None])
    checkpoint_update = FakeConnection(["equity.profile", None])
    repository._advance_checkpoint(
        cast(Any, checkpoint_insert),
        capability="equity.profile",
        identifier=EquityIdentifier.parse("SSE.600519"),
        window_end=None,
        data_version=uuid4(),
        updated_at=observed_at,
    )
    repository._advance_checkpoint(
        cast(Any, checkpoint_update),
        capability="equity.profile",
        identifier=EquityIdentifier.parse("SSE.600519"),
        window_end=None,
        data_version=uuid4(),
        updated_at=observed_at,
    )
    assert "INSERT INTO equity_sync_checkpoint" in checkpoint_insert.statements[1]
    assert "UPDATE equity_sync_checkpoint" in checkpoint_update.statements[1]


def test_repository_reads_all_extension_resources() -> None:
    """读取方法从各自物理表映射行情、因子、事件、概况、质量和来源血缘。"""
    data_version = uuid4()
    source_batch_id = uuid4()
    published_at = datetime(2026, 7, 28, tzinfo=UTC)
    action_id = uuid4()
    instrument = StoredEquityInstrument(
        security_id=1,
        instrument_id=uuid4(),
        identifier=EquityIdentifier.parse("SSE.600519"),
        name="贵州茅台",
        listing_status="LISTED",
    )
    profile_row = SimpleNamespace(
        revision=2,
        company_name="贵州茅台酒股份有限公司",
        english_name=None,
        industry="白酒",
        legal_representative=None,
        established_on=date(1999, 11, 20),
        website=None,
        email=None,
        phone=None,
        registered_address="贵州",
        office_address=None,
        main_business="白酒",
        business_scope=None,
        summary=None,
        source_batch_id=source_batch_id,
    )
    bar_row = {
        "period_end": date(2026, 7, 24),
        "open_price": Decimal("10"),
        "high_price": Decimal("11"),
        "low_price": Decimal("9"),
        "close_price": Decimal("10.5"),
        "volume_shares": 1_000,
        "amount_cny": Decimal("10500"),
        "turnover_rate": Decimal("0.01"),
        "revision": 2,
        "is_final": True,
    }
    engine = FakeEngine(
        [
            {
                "data_version": data_version,
                "published_at": published_at,
                "quality_status": "passed",
            },
            [bar_row],
            [bar_row],
            [
                {
                    "effective_date": date(2026, 1, 1),
                    "cumulative_factor": Decimal("2"),
                    "revision": 1,
                    "factor_version": data_version,
                    "source_batch_id": source_batch_id,
                }
            ],
            [
                {
                    "action_id": action_id,
                    "revision": 1,
                    "source_event_key": "2025",
                    "report_period": date(2025, 12, 31),
                    "status": "实施",
                    "announcement_date": None,
                    "record_date": None,
                    "ex_date": date(2026, 6, 30),
                    "cash_dividend_per_10": Decimal("10"),
                    "bonus_shares_per_10": None,
                    "transfer_shares_per_10": None,
                    "source_batch_id": source_batch_id,
                }
            ],
            profile_row,
            None,
            [{"security_id": 1}],
            None,
            None,
        ]
    )
    repository = _repository(engine)

    publication = repository.get_current_publication(
        dataset="equity.bar.1w.raw",
        instrument=instrument,
    )
    weekly = repository.list_bars(
        security_id=1,
        period=EquityBarPeriod.WEEK_1,
        start=date(2026, 1, 1),
        end=date(2026, 7, 28),
    )
    daily = repository.list_bars(
        security_id=1,
        period=EquityBarPeriod.DAY_1,
        start=date(2026, 1, 1),
        end=date(2026, 7, 28),
    )
    factors = repository.list_adjustment_factors(
        security_id=1,
        end=date(2026, 7, 28),
    )
    actions = repository.list_corporate_actions(
        security_id=1,
        start=date(2025, 1, 1),
        end=date(2026, 7, 28),
    )
    profile = repository.get_company_profile(security_id=1)

    assert publication is not None and publication.data_version == data_version
    assert publication.quality_status == "passed"
    assert cast(EquityPeriodBar, weekly[0].bar).period is EquityBarPeriod.WEEK_1
    assert cast(EquityDailyBar, daily[0].bar).trade_date == date(2026, 7, 24)
    assert factors[0].factor.cumulative_factor == Decimal("2")
    assert factors[0].source_batch_id == source_batch_id
    assert actions[0].action_id == action_id
    assert actions[0].source_batch_id == source_batch_id
    assert profile is not None and profile.profile.industry == "白酒"
    assert profile is not None and profile.source_batch_id == source_batch_id
    assert "equity_adjustment_factor.source_batch_id" in engine.connection.statements[3]
    assert "equity_corporate_action_version.source_batch_id" in engine.connection.statements[4]
    assert (
        repository.get_current_publication(
            dataset="equity.profile",
            instrument=instrument,
        )
        is None
    )
    assert repository.get_company_profile(security_id=2) is None
    with pytest.raises(ValueError, match="start must not be after end"):
        repository.list_bars(
            security_id=1,
            period=EquityBarPeriod.DAY_1,
            start=date(2026, 7, 28),
            end=date(2026, 1, 1),
        )


def _repository(engine: FakeEngine) -> SqlAlchemyEquityMarketDataRepository:
    """围绕带短 Session 边界的替身数据库构造仓储。"""
    return SqlAlchemyEquityMarketDataRepository(cast(DatabaseClient, FakeDatabase(engine)))


def _instrument_row(instrument_id: UUID) -> dict[str, Any]:
    """返回仓储查询替身使用的一条稳定标准证券映射。"""
    return {
        "security_id": 1,
        "instrument_id": instrument_id,
        "exchange": "SSE",
        "symbol": "600519",
        "name": "贵州茅台",
        "listing_status": "LISTED",
    }


def _bar() -> EquityDailyBar:
    """构造供修订和哈希断言使用的一条有效当前日线。"""
    return EquityDailyBar(
        trade_date=date(2026, 6, 30),
        open_price=Decimal("10"),
        high_price=Decimal("11"),
        low_price=Decimal("9"),
        close_price=Decimal("10.5"),
        volume_shares=1_000,
        amount_cny=Decimal("10500"),
        turnover_rate=None,
    )


def _period_bar(period: EquityBarPeriod, period_end: date) -> EquityPeriodBar:
    """构造一条指定周期的上游原生行情。"""
    return EquityPeriodBar(
        period=period,
        period_end=period_end,
        open_price=Decimal("10"),
        high_price=Decimal("11"),
        low_price=Decimal("9"),
        close_price=Decimal("10.5"),
        volume_shares=1_000,
        amount_cny=Decimal("10500"),
        turnover_rate=Decimal("0.01"),
    )


def _source(capability: str = "equity.test") -> EquitySourceObservation:
    """构造扩展同步使用的已归档来源观测。"""
    return EquitySourceObservation(
        provider_id="test-provider",
        capability=capability,
        raw_payload_sha256="a" * 64,
        raw_uri="s3://test/raw.json",
        raw_content_type="application/json",
        raw_byte_size=2,
        normalized_payload_sha256="b" * 64,
        normalized_uri="s3://test/normalized.json",
        normalized_content_type="application/json",
        normalized_byte_size=2,
        observed_at=datetime(2026, 7, 28, tzinfo=UTC),
        upstream_source="test-upstream",
        adapter_version="test-v1",
        schema_fingerprint="c" * 64,
    )


def _bar_identity(
    identifier: EquityIdentifier,
    *,
    start: date = date(2026, 1, 1),
    end: date = date(2026, 7, 28),
) -> BarWindowIdentity:
    """构造完整覆盖 2026 年测试窗口的已确认行情身份。"""
    return BarWindowIdentity(
        security_id=1,
        identifier_version_id=uuid4(),
        exchange=identifier.exchange.value,
        symbol=identifier.symbol,
        coverage_from=start,
        coverage_to=end,
        identity_hash="d" * 64,
        universe_hash="e" * 64,
    )


def _action() -> EquityCorporateAction:
    """构造一条可修订现金分红事件。"""
    return EquityCorporateAction(
        source_event_key="2025",
        report_period=date(2025, 12, 31),
        status="实施",
        announcement_date=date(2026, 6, 1),
        record_date=date(2026, 6, 29),
        ex_date=date(2026, 6, 30),
        cash_dividend_per_10=Decimal("10"),
        bonus_shares_per_10=None,
        transfer_shares_per_10=None,
    )


def _profile() -> EquityCompanyProfile:
    """构造一份带真实空值的公司概况。"""
    return EquityCompanyProfile(
        company_name="贵州茅台酒股份有限公司",
        english_name=None,
        industry="白酒",
        legal_representative=None,
        established_on=date(1999, 11, 20),
        website=None,
        email=None,
        phone=None,
        registered_address="贵州",
        office_address=None,
        main_business="白酒",
        business_scope=None,
        summary=None,
    )
