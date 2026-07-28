"""平台派生财务仓储的点时读取、双时态修订、血缘和发布单元测试。"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest
from sqlalchemy.orm import Session

from service_data_sync.application.ports.financial_derived import (
    DerivedFinancialMetricInput,
    FinancialDerivationSnapshot,
    FinancialDerivationUnavailable,
    ReportedFinancialFact,
)
from service_data_sync.domain.equity import Exchange
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.persistence import financial_derived_repository as repository


class StubResult:
    """模拟仓储所需的 SQLAlchemy 行、映射和标量读取接口。"""

    def __init__(
        self,
        *,
        one: object | None = None,
        rows: Sequence[object] = (),
        scalar: object | None = None,
    ) -> None:
        """保存一次 SQL 执行的确定性返回值。"""
        self._one = one
        self._rows = list(rows)
        self._scalar = scalar

    def mappings(self) -> StubResult:
        """保持同一结果对象，以支持 `mappings()` 链式调用。"""
        return self

    def one_or_none(self) -> object | None:
        """返回唯一行或空结果。"""
        return self._one

    def all(self) -> list[object]:
        """返回当前执行的全部行。"""
        return list(self._rows)

    def scalar_one_or_none(self) -> object | None:
        """返回唯一标量或空结果。"""
        return self._scalar

    def scalar_one(self) -> object | None:
        """返回测试安排的唯一标量。"""
        return self._scalar


class RecordingConnection:
    """按队列返回 SQL 结果，并保留每条语句用于业务断言。"""

    def __init__(self, responses: Sequence[StubResult] = ()) -> None:
        """初始化结果队列和空语句记录。"""
        self.responses = list(responses)
        self.executed: list[Any] = []

    def execute(self, statement: Any) -> StubResult:
        """记录语句并消费一个预先安排的 SQL 结果。"""
        self.executed.append(statement)
        if not self.responses:
            raise AssertionError(f"unexpected SQL execution: {statement}")
        return self.responses.pop(0)


class StubDatabase:
    """为仓储提供共享的无网络 Session 与事务上下文。"""

    def __init__(self, connection: RecordingConnection) -> None:
        """保存本测试使用的唯一连接替身。"""
        self.connection = connection

    @contextmanager
    def session(self) -> Iterator[Session]:
        """把记录连接作为只读 Session 暴露给仓储。"""
        yield cast(Session, self.connection)

    @contextmanager
    def transaction(self) -> Iterator[Session]:
        """把记录连接作为原子事务 Session 暴露给仓储。"""
        yield cast(Session, self.connection)


def test_load_inputs_uses_publication_cutoff_and_maps_complete_lineage() -> None:
    """输入读取必须按日期感知身份和 publication 截点返回完整报表血缘。"""
    snapshot = _snapshot()
    fact = _fact(date(2025, 6, 30), value="380")
    publication_row: Mapping[str, object] = {
        "data_version": snapshot.data_version,
        "security_id": snapshot.security_id,
        "methodology_id": snapshot.methodology_id,
        "effective_as_of": snapshot.effective_as_of,
        "knowledge_cutoff": snapshot.knowledge_cutoff,
    }
    fact_row: Mapping[str, object] = {
        "report_period": fact.report_period,
        "statement_scope": fact.statement_scope,
        "metric_id": fact.metric_id,
        "metric_code": fact.metric_code,
        "value": fact.value,
        "unit": fact.unit,
        "currency": fact.currency,
        "currency_null_reason": fact.currency_null_reason,
        "revision_id": fact.revision_id,
        "source_batch_id": fact.source_batch_id,
        "effective_from": fact.effective_from,
        "known_from": fact.known_from,
        "observed_at": fact.observed_at,
    }
    connection = RecordingConnection(
        (
            StubResult(one=publication_row),
            StubResult(rows=(fact_row,)),
        )
    )
    target = repository.SqlAlchemyFinancialDerivationRepository(
        cast(DatabaseClient, StubDatabase(connection))
    )

    loaded = target.load_inputs(exchange=Exchange.SSE, symbol="600519")

    assert loaded == replace(snapshot, facts=(fact,))
    assert "equity_identifier_version" in str(connection.executed[0])
    assert "financial_publication" in str(connection.executed[0])
    assert "financial_statement_fact" in str(connection.executed[1])
    assert "financial_metric_definition" in str(connection.executed[1])
    assert connection.responses == []


def test_load_inputs_rejects_missing_current_report_publication() -> None:
    """没有当前已验证报表 publication 时不得从未发布 revision 偷读输入。"""
    connection = RecordingConnection((StubResult(one=None),))
    target = repository.SqlAlchemyFinancialDerivationRepository(
        cast(DatabaseClient, StubDatabase(connection))
    )

    with pytest.raises(FinancialDerivationUnavailable, match="inputs are unavailable"):
        target.load_inputs(exchange=Exchange.SZSE, symbol="000001")

    assert len(connection.executed) == 1


def test_publish_orchestrates_guards_partitions_revisions_and_atomic_pointer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """发布必须先重验输入与运行账本，再汇总关闭、写入和复用数量。"""
    snapshot = _snapshot()
    metrics = (
        _metric(date(2025, 6, 30), period_basis="SINGLE_QUARTER"),
        _metric(
            date(2025, 9, 30),
            metric_code="platform.operating_revenue.ttm",
            period_basis="TTM",
            inputs=(
                _fact(date(2025, 9, 30), value="500"),
                _fact(date(2024, 12, 31), value="600"),
                _fact(date(2024, 9, 30), value="400"),
            ),
        ),
    )
    run_id = UUID("70000000-0000-4000-8000-000000000001")
    methodology_id = UUID("71000000-0000-4000-8000-000000000001")
    data_version = UUID("72000000-0000-4000-8000-000000000001")
    computed_at = datetime(2026, 7, 28, 9, tzinfo=UTC)
    connection = RecordingConnection()
    calls: list[tuple[str, object]] = []
    write_results = iter((True, False))

    def require_input(_connection: Session, received: FinancialDerivationSnapshot) -> None:
        """记录来源 publication 重验发生在任何写入之前。"""
        assert received is snapshot
        calls.append(("input", received.data_version))

    def require_run(_connection: Session, received: UUID) -> None:
        """记录派生运行账本锁定。"""
        assert received == run_id
        calls.append(("run", received))

    def require_methodology(_connection: Session) -> UUID:
        """返回 migration 预置方法学并记录读取顺序。"""
        calls.append(("methodology", methodology_id))
        return methodology_id

    def ensure_partition(_connection: Session, partition_date: date) -> None:
        """记录同一年多个指标只需要确保一个年度分区。"""
        calls.append(("partition", partition_date))

    def metric_ids(
        _connection: Session, received: Sequence[DerivedFinancialMetricInput]
    ) -> dict[str, int]:
        """把两个 migration 管理的指标代码映射为稳定字典主键。"""
        assert tuple(received) == metrics
        calls.append(("dictionary", len(received)))
        return {
            "platform.operating_revenue.single_quarter": 101,
            "platform.operating_revenue.ttm": 102,
        }

    def close_removed(
        _connection: Session,
        *,
        snapshot: FinancialDerivationSnapshot,
        methodology_id: UUID,
        target_keys: set[tuple[date, int, str, str, int]],
        computed_at: datetime,
    ) -> int:
        """模拟关闭一个已不能重算的旧指标，并验证完整目标键。"""
        assert snapshot.data_version == _snapshot().data_version
        assert methodology_id == UUID("71000000-0000-4000-8000-000000000001")
        assert computed_at == datetime(2026, 7, 28, 9, tzinfo=UTC)
        assert len(target_keys) == 2
        calls.append(("close", len(target_keys)))
        return 1

    def write_metric(
        _connection: Session,
        *,
        snapshot: FinancialDerivationSnapshot,
        metric: DerivedFinancialMetricInput,
        metric_id: int,
        methodology_id: UUID,
        derivation_run_id: UUID,
        computed_at: datetime,
    ) -> bool:
        """按安排返回一次新增和一次内容复用结果。"""
        assert snapshot.security_id == 8
        assert metric_id in {101, 102}
        assert methodology_id == UUID("71000000-0000-4000-8000-000000000001")
        assert derivation_run_id == run_id
        assert computed_at == datetime(2026, 7, 28, 9, tzinfo=UTC)
        calls.append(("write", metric.metric_code))
        return next(write_results)

    def publish_pointer(
        _connection: Session,
        *,
        snapshot: FinancialDerivationSnapshot,
        methodology_id: UUID,
        metrics: Sequence[DerivedFinancialMetricInput],
        changed_count: int,
        computed_at: datetime,
    ) -> UUID:
        """验证关闭旧值和新增 revision 都计入原子 publication 变更数。"""
        assert snapshot.security_id == 8
        assert methodology_id == UUID("71000000-0000-4000-8000-000000000001")
        assert tuple(metrics) == metrics_to_publish
        assert changed_count == 2
        assert computed_at == datetime(2026, 7, 28, 9, tzinfo=UTC)
        calls.append(("publish", changed_count))
        return data_version

    metrics_to_publish = metrics
    monkeypatch.setattr(repository, "_require_current_input_publication", require_input)
    monkeypatch.setattr(repository, "_require_derivation_run", require_run)
    monkeypatch.setattr(repository, "_require_derived_methodology", require_methodology)
    monkeypatch.setattr(repository, "ensure_financial_year_partitions", ensure_partition)
    monkeypatch.setattr(repository, "_metric_ids", metric_ids)
    monkeypatch.setattr(repository, "_close_removed_metrics", close_removed)
    monkeypatch.setattr(repository, "_write_metric", write_metric)
    monkeypatch.setattr(repository, "_publish", publish_pointer)
    target = repository.SqlAlchemyFinancialDerivationRepository(
        cast(DatabaseClient, StubDatabase(connection))
    )

    publication = target.publish(
        snapshot=snapshot,
        metrics=metrics,
        derivation_run_id=run_id,
        computed_at=computed_at,
    )

    assert publication.data_version == data_version
    assert publication.inserted_count == 2
    assert publication.unchanged_count == 1
    assert publication.row_count == 2
    assert calls[:4] == [
        ("input", snapshot.data_version),
        ("run", run_id),
        ("methodology", methodology_id),
        ("partition", date(2025, 1, 1)),
    ]
    assert calls[-1] == ("publish", 2)


def test_publish_rejects_naive_computation_time_before_opening_transaction() -> None:
    """无时区计算时刻无法定义双时态边界，必须在事务外立即拒绝。"""
    target = repository.SqlAlchemyFinancialDerivationRepository(
        cast(DatabaseClient, StubDatabase(RecordingConnection()))
    )

    with pytest.raises(ValueError, match="timezone"):
        target.publish(
            snapshot=_snapshot(),
            metrics=(),
            derivation_run_id=UUID("70000000-0000-4000-8000-000000000001"),
            computed_at=datetime(2026, 7, 28, 9),
        )


def test_publish_guards_require_current_source_running_ledger_and_seeded_methodology() -> None:
    """输入指针、派生运行和验证方法学任一失效都必须阻断写入。"""
    snapshot = _snapshot()
    run_id = UUID("70000000-0000-4000-8000-000000000001")
    methodology_id = repository._DERIVED_METHODOLOGY_ID

    current_connection = RecordingConnection((StubResult(scalar=snapshot.data_version),))
    repository._require_current_input_publication(cast(Session, current_connection), snapshot)

    stale_connection = RecordingConnection((StubResult(scalar=None),))
    with pytest.raises(FinancialDerivationUnavailable, match="changed during derivation"):
        repository._require_current_input_publication(cast(Session, stale_connection), snapshot)

    running_connection = RecordingConnection(
        (StubResult(one=SimpleNamespace(capability="financial.derived-metric", status="running")),)
    )
    repository._require_derivation_run(cast(Session, running_connection), run_id)

    wrong_run_connection = RecordingConnection(
        (StubResult(one=SimpleNamespace(capability="financial.report", status="succeeded")),)
    )
    with pytest.raises(FinancialDerivationUnavailable, match="run is unavailable"):
        repository._require_derivation_run(cast(Session, wrong_run_connection), run_id)

    methodology_connection = RecordingConnection((StubResult(scalar=methodology_id),))
    assert (
        repository._require_derived_methodology(cast(Session, methodology_connection))
        == methodology_id
    )

    missing_methodology_connection = RecordingConnection((StubResult(scalar=None),))
    with pytest.raises(FinancialDerivationUnavailable, match="methodology is unavailable"):
        repository._require_derived_methodology(cast(Session, missing_methodology_connection))


def test_metric_dictionary_requires_every_distinct_platform_code() -> None:
    """派生字典必须覆盖目标中的每个唯一代码，禁止运行时隐式造字段。"""
    first = _metric(date(2025, 6, 30), period_basis="SINGLE_QUARTER")
    second = replace(first, report_period=date(2025, 9, 30))
    complete_connection = RecordingConnection(
        (StubResult(rows=(("platform.operating_revenue.single_quarter", 101),)),)
    )

    assert repository._metric_ids(cast(Session, complete_connection), (first, second)) == {
        "platform.operating_revenue.single_quarter": 101
    }

    incomplete_connection = RecordingConnection((StubResult(rows=()),))
    with pytest.raises(FinancialDerivationUnavailable, match="dictionary is incomplete"):
        repository._metric_ids(cast(Session, incomplete_connection), (first,))

    empty_connection = RecordingConnection((StubResult(rows=()),))
    assert repository._metric_ids(cast(Session, empty_connection), ()) == {}


def test_close_removed_metrics_expires_only_absent_keys_and_enforces_monotonic_time() -> None:
    """新目标缺失的当前输出应关闭，且计算时刻必须晚于全部当前 revision。"""
    snapshot = _snapshot()
    metric = _metric(date(2025, 6, 30), period_basis="SINGLE_QUARTER")
    methodology_id = UUID("71000000-0000-4000-8000-000000000001")
    computed_at = datetime(2026, 7, 28, 9, tzinfo=UTC)
    retained_revision = UUID("73000000-0000-4000-8000-000000000001")
    removed_revision = UUID("73000000-0000-4000-8000-000000000002")
    rows = (
        (
            metric.report_period,
            retained_revision,
            101,
            metric.period_basis,
            metric.statement_scope,
            metric.formula_version,
            computed_at - timedelta(days=1),
        ),
        (
            date(2025, 3, 31),
            removed_revision,
            101,
            metric.period_basis,
            metric.statement_scope,
            metric.formula_version,
            computed_at - timedelta(days=2),
        ),
    )
    connection = RecordingConnection((StubResult(rows=rows), StubResult()))

    closed = repository._close_removed_metrics(
        cast(Session, connection),
        snapshot=snapshot,
        methodology_id=methodology_id,
        target_keys={repository._logical_key(metric=metric, metric_id=101)},
        computed_at=computed_at,
    )

    assert closed == 1
    assert len(connection.executed) == 2
    assert "UPDATE derived_financial_metric_revision" in str(connection.executed[1])

    collision_connection = RecordingConnection(
        (
            StubResult(
                rows=(
                    (
                        metric.report_period,
                        retained_revision,
                        101,
                        metric.period_basis,
                        metric.statement_scope,
                        metric.formula_version,
                        computed_at,
                    ),
                )
            ),
        )
    )
    with pytest.raises(FinancialDerivationUnavailable, match="after every current"):
        repository._close_removed_metrics(
            cast(Session, collision_connection),
            snapshot=snapshot,
            methodology_id=methodology_id,
            target_keys=set(),
            computed_at=computed_at,
        )


def test_write_metric_reuses_identical_content_and_appends_revision_with_lineage() -> None:
    """内容未变应复用；内容变化应追加 revision 并逐项保存输入角色和来源版本。"""
    snapshot = _snapshot()
    metric = _metric(
        date(2025, 6, 30),
        period_basis="SINGLE_QUARTER",
        inputs=(
            _fact(date(2025, 6, 30), value="380"),
            _fact(date(2025, 3, 31), value="180"),
        ),
    )
    methodology_id = UUID("71000000-0000-4000-8000-000000000001")
    run_id = UUID("70000000-0000-4000-8000-000000000001")
    computed_at = datetime(2026, 7, 28, 9, tzinfo=UTC)
    current_revision_id = UUID("73000000-0000-4000-8000-000000000001")

    unchanged_connection = RecordingConnection(
        (
            StubResult(
                one={
                    "metric_revision_id": current_revision_id,
                    "revision": 3,
                    "content_sha256": metric.content_sha256,
                    "known_from": computed_at - timedelta(days=1),
                }
            ),
        )
    )
    assert (
        repository._write_metric(
            cast(Session, unchanged_connection),
            snapshot=snapshot,
            metric=metric,
            metric_id=101,
            methodology_id=methodology_id,
            derivation_run_id=run_id,
            computed_at=computed_at,
        )
        is False
    )
    assert len(unchanged_connection.executed) == 1

    changed_connection = RecordingConnection(
        (
            StubResult(
                one={
                    "metric_revision_id": current_revision_id,
                    "revision": 3,
                    "content_sha256": "c" * 64,
                    "known_from": computed_at - timedelta(days=1),
                }
            ),
            StubResult(),
            StubResult(scalar=3),
            StubResult(),
            StubResult(),
        )
    )
    assert (
        repository._write_metric(
            cast(Session, changed_connection),
            snapshot=snapshot,
            metric=metric,
            metric_id=101,
            methodology_id=methodology_id,
            derivation_run_id=run_id,
            computed_at=computed_at,
        )
        is True
    )
    assert len(changed_connection.executed) == 5
    assert changed_connection.executed[3].compile().params["revision"] == 4
    assert "financial_derivation_input" in str(changed_connection.executed[4])

    first_revision_connection = RecordingConnection(
        (
            StubResult(one=None),
            StubResult(scalar=None),
            StubResult(),
            StubResult(),
        )
    )
    assert (
        repository._write_metric(
            cast(Session, first_revision_connection),
            snapshot=snapshot,
            metric=metric,
            metric_id=101,
            methodology_id=methodology_id,
            derivation_run_id=run_id,
            computed_at=computed_at,
        )
        is True
    )
    assert first_revision_connection.executed[2].compile().params["revision"] == 1


def test_write_metric_rejects_future_observation_and_non_monotonic_revision_time() -> None:
    """计算不得早于输入观测，也不得覆盖同刻或未来才生效的当前 revision。"""
    snapshot = _snapshot()
    methodology_id = UUID("71000000-0000-4000-8000-000000000001")
    run_id = UUID("70000000-0000-4000-8000-000000000001")
    computed_at = datetime(2026, 7, 28, 9, tzinfo=UTC)
    metric = _metric(date(2025, 6, 30), period_basis="SINGLE_QUARTER")
    future_metric = replace(metric, observed_at=computed_at + timedelta(seconds=1))

    future_connection = RecordingConnection((StubResult(one=None),))
    with pytest.raises(FinancialDerivationUnavailable, match="precedes"):
        repository._write_metric(
            cast(Session, future_connection),
            snapshot=snapshot,
            metric=future_metric,
            metric_id=101,
            methodology_id=methodology_id,
            derivation_run_id=run_id,
            computed_at=computed_at,
        )

    collision_connection = RecordingConnection(
        (
            StubResult(
                one={
                    "metric_revision_id": UUID("73000000-0000-4000-8000-000000000001"),
                    "revision": 3,
                    "content_sha256": "c" * 64,
                    "known_from": computed_at,
                }
            ),
        )
    )
    with pytest.raises(FinancialDerivationUnavailable, match="after the current"):
        repository._write_metric(
            cast(Session, collision_connection),
            snapshot=snapshot,
            metric=metric,
            metric_id=101,
            methodology_id=methodology_id,
            derivation_run_id=run_id,
            computed_at=computed_at,
        )


def test_input_roles_are_stable_for_single_quarter_and_ttm_manifests() -> None:
    """单季与 TTM 的一项和多项公式必须生成固定、可读的输入角色。"""
    current = _fact(date(2025, 6, 30), value="380")
    previous = _fact(date(2025, 3, 31), value="180")
    annual = _fact(date(2024, 12, 31), value="600")
    prior_same = _fact(date(2024, 6, 30), value="250")
    single = _metric(
        date(2025, 6, 30),
        period_basis="SINGLE_QUARTER",
        inputs=(current,),
    )

    assert repository._input_roles(single) == ("CURRENT_YTD",)
    assert repository._input_roles(replace(single, inputs=(current, previous))) == (
        "CURRENT_YTD",
        "PREVIOUS_YTD",
    )
    ttm = replace(single, period_basis="TTM")
    assert repository._input_roles(ttm) == ("CURRENT_YTD",)
    assert repository._input_roles(replace(ttm, inputs=(current, annual, prior_same))) == (
        "CURRENT_YTD",
        "PRIOR_ANNUAL",
        "PRIOR_SAME_QUARTER",
    )
    assert repository._source_observation_order_key(current) == (
        current.observed_at,
        str(current.source_batch_id),
    )


def test_publish_pointer_reuses_unchanged_version_and_replaces_changed_snapshot() -> None:
    """无变化时复用当前版本；有变化时原子替换指针并发布稳定内容摘要。"""
    snapshot = _snapshot()
    methodology_id = UUID("71000000-0000-4000-8000-000000000001")
    computed_at = datetime(2026, 7, 28, 9, tzinfo=UTC)
    current_version = UUID("72000000-0000-4000-8000-000000000001")

    reuse_connection = RecordingConnection((StubResult(scalar=current_version),))
    assert (
        repository._publish(
            cast(Session, reuse_connection),
            snapshot=snapshot,
            methodology_id=methodology_id,
            metrics=(),
            changed_count=0,
            computed_at=computed_at,
        )
        == current_version
    )
    assert len(reuse_connection.executed) == 1

    metric = _metric(date(2025, 6, 30), period_basis="SINGLE_QUARTER")
    replace_connection = RecordingConnection(
        (
            StubResult(scalar=current_version),
            StubResult(),
            StubResult(),
            StubResult(),
        )
    )
    new_version = repository._publish(
        cast(Session, replace_connection),
        snapshot=snapshot,
        methodology_id=methodology_id,
        metrics=(metric,),
        changed_count=1,
        computed_at=computed_at,
    )

    assert new_version != current_version
    assert len(replace_connection.executed) == 4
    dataset_parameters = replace_connection.executed[2].compile().params
    financial_parameters = replace_connection.executed[3].compile().params
    assert dataset_parameters["effective_as_of"] == metric.effective_from
    assert financial_parameters["row_count"] == 1
    assert len(cast(str, financial_parameters["content_sha256"])) == 64


def test_publish_pointer_creates_empty_target_when_no_current_version_exists() -> None:
    """首次空结果仍须建立 publication，才能明确表达当前没有可派生指标。"""
    snapshot = _snapshot()
    connection = RecordingConnection(
        (
            StubResult(scalar=None),
            StubResult(),
            StubResult(),
            StubResult(),
        )
    )

    data_version = repository._publish(
        cast(Session, connection),
        snapshot=snapshot,
        methodology_id=UUID("71000000-0000-4000-8000-000000000001"),
        metrics=(),
        changed_count=0,
        computed_at=datetime(2026, 7, 28, 9, tzinfo=UTC),
    )

    assert isinstance(data_version, UUID)
    assert connection.executed[2].compile().params["effective_as_of"] == snapshot.effective_as_of
    assert connection.executed[3].compile().params["row_count"] == 0


def _snapshot() -> FinancialDerivationSnapshot:
    """构造已冻结且没有事实的报表 publication 快照。"""
    return FinancialDerivationSnapshot(
        data_version=UUID("10000000-0000-4000-8000-000000000001"),
        security_id=8,
        methodology_id=UUID("20000000-0000-4000-8000-000000000001"),
        effective_as_of=date(2026, 3, 31),
        knowledge_cutoff=datetime(2026, 4, 28, 8, tzinfo=UTC),
        facts=(),
    )


def _fact(
    report_period: date,
    *,
    value: str,
    observed_at: datetime | None = None,
) -> ReportedFinancialFact:
    """构造带稳定 revision、来源批次和双时态信息的累计利润表事实。"""
    return ReportedFinancialFact(
        report_period=report_period,
        statement_scope="CONSOLIDATED",
        metric_id=1,
        metric_code="statement.income_statement.total-operate-income",
        value=Decimal(value),
        unit="yuan",
        currency="CNY",
        currency_null_reason=None,
        revision_id=uuid5(NAMESPACE_URL, f"test-financial-revision:{report_period}"),
        source_batch_id=uuid5(NAMESPACE_URL, f"test-financial-source:{report_period}"),
        effective_from=report_period,
        known_from=datetime(
            report_period.year,
            report_period.month,
            report_period.day,
            tzinfo=UTC,
        ),
        observed_at=observed_at
        or datetime(
            report_period.year,
            report_period.month,
            report_period.day,
            tzinfo=UTC,
        ),
    )


def _metric(
    report_period: date,
    *,
    metric_code: str = "platform.operating_revenue.single_quarter",
    period_basis: str,
    inputs: tuple[ReportedFinancialFact, ...] | None = None,
) -> DerivedFinancialMetricInput:
    """构造一个带可审计输入 manifest 的平台派生指标。"""
    selected_inputs = inputs or (_fact(report_period, value="380"),)
    return DerivedFinancialMetricInput(
        metric_code=metric_code,
        label="营业收入（派生）",
        report_period=report_period,
        period_basis=period_basis,
        statement_scope="CONSOLIDATED",
        value=Decimal("200"),
        unit="yuan",
        currency="CNY",
        currency_null_reason=None,
        formula_version=1,
        effective_from=report_period,
        observed_at=max(item.observed_at for item in selected_inputs),
        inputs=selected_inputs,
        input_manifest_sha256="a" * 64,
        content_sha256="b" * 64,
    )
