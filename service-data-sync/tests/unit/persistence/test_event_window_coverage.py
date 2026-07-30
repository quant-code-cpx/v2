"""证券事件覆盖批量装载与写入的查询复杂度回归。"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime
from types import SimpleNamespace
from typing import cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session
from sqlalchemy.sql import ClauseElement

from service_data_sync.infrastructure.persistence import event_window_coverage as coverage
from service_data_sync.infrastructure.persistence.canonical_release_repository import (
    SqlAlchemyCanonicalReleaseRepository,
)
from service_data_sync.infrastructure.persistence.event_window_coverage import (
    EventCoverageIdentity,
    EventCoverageRecords,
)
from service_data_sync.infrastructure.persistence.typed_p0_support import (
    TypedP0SourceObservation,
)


class _RowsResult:
    """提供覆盖批量写入所需的最小 SQLAlchemy 结果接口。"""

    def __init__(self, rows: list[object]) -> None:
        """保存一次查询返回的 ORM 行。"""
        self._rows = rows

    def scalars(self) -> _RowsResult:
        """模拟 ORM 标量结果视图。"""
        return self

    def all(self) -> list[object]:
        """返回当前响应的全部行。"""
        return self._rows


class _RecordingSession:
    """记录每次 SQL 执行，并按顺序提供预置查询行。"""

    def __init__(self, responses: list[list[object]]) -> None:
        """保存查询响应和已执行语句。"""
        self._responses = list(responses)
        self.calls: list[tuple[object, object | None]] = []

    def execute(self, statement: object, params: object | None = None) -> _RowsResult:
        """记录 SQL；没有预置查询响应时返回空写结果。"""
        self.calls.append((statement, params))
        rows = self._responses.pop(0) if self._responses else []
        return _RowsResult(rows)


@pytest.mark.parametrize("roster_size", [1, 500])
def test_coverage_writer_query_count_is_independent_of_roster_size(roster_size: int) -> None:
    """新 coverage 批次无论证券数量均只执行两次读取和一次批量插入。"""
    session = _RecordingSession([[], []])

    coverage._record_coverages(  # noqa: SLF001
        cast(Session, session),
        values=tuple(_coverage_write(index) for index in range(roster_size)),
    )

    assert len(session.calls) == 3
    assert isinstance(session.calls[-1][1], list)
    assert len(cast(list[object], session.calls[-1][1])) == roster_size


def test_coverage_loader_runs_once_per_family_for_full_frozen_roster(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """manifest 生成器按事件族批量装载，而不是对 roster 中每只证券单独查询。"""
    identities = tuple(_identity(index) for index in range(500))
    loader_calls: list[tuple[int, str]] = []
    written: list[object] = []
    data_version = uuid4()

    def records_for(
        session: Session,
        frozen_identities: Sequence[EventCoverageIdentity],
        family: str,
    ) -> dict[EventCoverageIdentity, EventCoverageRecords]:
        """记录批量 loader 的调用大小并返回真实空窗映射。"""
        del session
        loader_calls.append((len(frozen_identities), family))
        return {
            identity: EventCoverageRecords(records=(), fact_dates=())
            for identity in frozen_identities
        }

    class _ReleaseRepository:
        """返回稳定 publication 版本的最小 release 仓储。"""

        def publish_in_session(self, **kwargs: object) -> SimpleNamespace:
            """忽略候选细节并返回 publication dataVersion。"""
            del kwargs
            return SimpleNamespace(data_version=data_version)

    monkeypatch.setattr(coverage, "record_normalization_run", lambda *args, **kwargs: uuid4())
    monkeypatch.setattr(coverage, "_coverage_candidate", lambda *args, **kwargs: object())
    monkeypatch.setattr(coverage, "_publication_id", lambda *args, **kwargs: uuid4())
    monkeypatch.setattr(
        coverage,
        "_record_coverages",
        lambda session, *, values: written.extend(values),
    )

    result = coverage.publish_event_window_coverages(
        cast(Session, object()),
        release_repository=cast(
            SqlAlchemyCanonicalReleaseRepository,
            _ReleaseRepository(),
        ),
        dataset_id=uuid4(),
        dataset_code="equity.corporate_event.earnings.reported",
        methodology_version_id=uuid4(),
        mapping_version="test-v1",
        source=cast(TypedP0SourceObservation, _Source()),
        source_batch_id=uuid4(),
        identities=identities,
        coverage_scope="GLOBAL",
        universe_hash="a" * 64,
        families=("EARNINGS_FORECAST", "EARNINGS_EXPRESS"),
        records_for=records_for,
        now=datetime(2026, 7, 30, 8, tzinfo=UTC),
    )

    assert loader_calls == [
        (500, "EARNINGS_FORECAST"),
        (500, "EARNINGS_EXPRESS"),
    ]
    assert len(written) == 1_000
    assert result.coverage_count == 1_000


def test_five_family_full_roster_current_query_stays_below_postgresql_bind_limit() -> None:
    """五族五千证券的 current 查询只按 roster 绑定，不会形成 12.5 万参数。"""
    session = _RecordingSession([[], []])
    values = tuple(
        _coverage_write(security_index, family_index=family_index)
        for security_index in range(5_000)
        for family_index in range(5)
    )

    coverage._record_coverages(cast(Session, session), values=values)  # noqa: SLF001

    current_statement = next(
        statement for statement, _params in session.calls if "FOR UPDATE" in str(statement)
    )
    compiled = cast(ClauseElement, current_statement).compile(
        dialect=postgresql.dialect(),
        compile_kwargs={"render_postcompile": True},
    )
    assert len(compiled.params or {}) < 6_000
    assert len(session.calls) == 11
    for statement, params in session.calls:
        if isinstance(params, list):
            assert len(params) <= 5_000
            continue
        compiled_statement = cast(ClauseElement, statement).compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"render_postcompile": True},
        )
        assert len(compiled_statement.params or {}) < 6_000


class _Source:
    """提供 coverage publication 所需的最小真实来源观察字段。"""

    provider_id = "integration-provider"
    capability = "integration.event-coverage"
    raw_payload_sha256 = "a" * 64
    raw_uri = "s3://integration/raw/event-coverage.json"
    raw_content_type = "application/json"
    raw_byte_size = 128
    normalized_payload_sha256 = "b" * 64
    normalized_uri = "s3://integration/normalized/event-coverage.json"
    normalized_content_type = "application/json"
    normalized_byte_size = 96
    observed_at = datetime(2026, 7, 30, 7, tzinfo=UTC)
    upstream_source = "integration-provider"
    adapter_version = "integration-v1"
    schema_fingerprint = "c" * 64


def _coverage_write(
    index: int,
    *,
    family_index: int = 3,
) -> coverage._EventCoverageWrite:  # noqa: SLF001
    """构造一个可批量插入的零记录覆盖观察。"""
    identity = _identity(index)
    dataset, family = (
        ("equity.corporate_action", "CORPORATE_ACTION"),
        ("equity.corporate_event.earnings.reported", "EARNINGS_FORECAST"),
        ("equity.corporate_event.earnings.reported", "EARNINGS_EXPRESS"),
        ("equity.dragon_tiger.disclosure.reported", "DRAGON_TIGER"),
        ("equity.block_trade.execution.reported", "BLOCK_TRADE"),
    )[family_index]
    return coverage._EventCoverageWrite(  # noqa: SLF001
        coverage_version=uuid4(),
        dataset=dataset,
        family=family,
        identity=identity,
        publication_id=uuid4(),
        source_batch_id=uuid4(),
        record_count=0,
        coverage_scope="GLOBAL",
        universe_hash="a" * 64,
        universe_size=500,
        observed_at=datetime(2026, 7, 30, 7, tzinfo=UTC),
        created_at=datetime(2026, 7, 30, 8, tzinfo=UTC),
    )


def _identity(index: int) -> EventCoverageIdentity:
    """构造 roster 中一个互不重复的冻结身份窗口。"""
    return EventCoverageIdentity(
        security_id=10_000 + index,
        identifier_version_id=UUID(f"10000000-0000-4000-8000-{index:012d}"),
        exchange="SSE",
        symbol=f"{index:06d}",
        coverage_from=date(2026, 7, 1),
        coverage_to=date(2026, 7, 31),
    )
