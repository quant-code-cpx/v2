"""统一事件 coverage、累计事实视图、部分成功与条件读取专项测试。"""

from __future__ import annotations

from contextlib import AbstractContextManager
from datetime import UTC, date, datetime
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from service_data_sync.infrastructure.database.models.canonical import MethodologyVersion
from service_data_sync.infrastructure.database.models.provenance.source_batch import SourceBatch
from service_data_sync.infrastructure.database.models.publication.dataset_publication import (
    DatasetPublication,
)
from service_data_sync.infrastructure.database.models.publication.equity_event_window_coverage import (  # noqa: E501
    EquityEventWindowCoverage,
)
from service_data_sync.interfaces import internal_equity_workspace_api as reader
from service_data_sync.interfaces.internal_sector_api import InternalProblem


class _NoopSession:
    """供全部持久化 helper 已替换时占位的 Session。"""


class _Rows:
    """提供事件 SQL 专项测试所需的空结果。"""

    def all(self) -> list[Any]:
        """返回空事件集合。"""
        return []


class _CaptureSession:
    """记录事件 reader 生成的 SQL，不返回事实行。"""

    def __init__(self) -> None:
        """初始化 SQL 记录。"""
        self.statements: list[Any] = []

    def execute(self, statement: Any) -> _Rows:
        """记录集合 SQL并返回空行。"""
        self.statements.append(statement)
        return _Rows()

    def scalars(self, statement: Any) -> _Rows:
        """记录标量集合 SQL 并返回空行。"""
        self.statements.append(statement)
        return _Rows()


class _SessionContext(AbstractContextManager[_NoopSession]):
    """提供事件测试使用的 Session 上下文。"""

    def __enter__(self) -> _NoopSession:
        """返回无状态 Session。"""
        return _NoopSession()

    def __exit__(self, *_args: object) -> None:
        """退出时无需释放资源。"""
        return None


class _Database:
    """提供事件路由注册需要的数据库外观。"""

    def session(self) -> _SessionContext:
        """创建事件测试 Session 上下文。"""
        return _SessionContext()


def _require_bearer(authorization: str | None = Header(default=None)) -> None:
    """仅允许测试内部 Bearer。"""
    if authorization != "Bearer reader-secret":
        raise HTTPException(status_code=401)


def _app() -> FastAPI:
    """构造仅用于事件条件读取的测试应用。"""
    app = FastAPI()

    @app.exception_handler(InternalProblem)
    async def render_problem(request: Request, error: InternalProblem) -> JSONResponse:
        """渲染 reader 稳定问题。"""
        return JSONResponse(
            status_code=error.status,
            content={"code": error.code},
            headers={"X-Request-Id": request.headers.get("X-Request-Id", "generated")},
        )

    reader.register_equity_workspace_routes(
        app,
        database=_Database(),  # type: ignore[arg-type]
        require_service_bearer=_require_bearer,
        cursor_secret=b"reader-cursor-secret",
    )
    return app


def _publication(
    *,
    dataset: str,
    data_version: UUID | None = None,
    quality_status: str = "passed",
    knowledge_cutoff: datetime | None = None,
) -> DatasetPublication:
    """构造事件 coverage 绑定的 publication。"""
    published_at = datetime(2026, 1, 2, 8, tzinfo=UTC)
    return cast(
        DatasetPublication,
        SimpleNamespace(
            publication_id=uuid4(),
            release_id=uuid4(),
            dataset=dataset,
            partition_key="provider:test",
            data_version=data_version or uuid4(),
            quality_status=quality_status,
            published_at=published_at,
            superseded_at=None,
            effective_as_of=date(2026, 1, 1),
            knowledge_cutoff=knowledge_cutoff or published_at,
        ),
    )


def _candidate(
    *,
    family: str,
    start: date,
    end: date,
    created_at: datetime,
    coverage_version: UUID | None = None,
    publication: DatasetPublication | None = None,
    provider_id: str = "eastmoney",
    upstream_source: str = "Eastmoney",
    methodology_id: UUID | None = None,
    record_count: int = 0,
) -> reader._EventCoverageCandidate:
    """构造一条带来源与方法学的窗口覆盖候选。"""
    dataset = reader._EVENT_DATASETS[family]
    method_id = methodology_id or UUID("10000000-0000-4000-8000-000000000001")
    coverage = cast(
        EquityEventWindowCoverage,
        SimpleNamespace(
            coverage_id=uuid4(),
            coverage_version=coverage_version or uuid4(),
            dataset=dataset,
            event_family=family,
            security_id=7,
            identifier_version_id=UUID("20000000-0000-4000-8000-000000000001"),
            coverage_from=start,
            coverage_to=end,
            publication_id=uuid4(),
            source_batch_id=uuid4(),
            record_count=record_count,
            created_at=created_at,
            superseded_at=None,
        ),
    )
    methodology = cast(
        MethodologyVersion,
        SimpleNamespace(
            methodology_version_id=method_id,
            code="eastmoney.event.reported",
            version=1,
        ),
    )
    source_batch = cast(
        SourceBatch,
        SimpleNamespace(
            source_batch_id=coverage.source_batch_id,
            provider_id=provider_id,
            upstream_source=upstream_source,
        ),
    )
    return reader._EventCoverageCandidate(
        coverage=coverage,
        publication=publication or _publication(dataset=dataset),
        methodology=methodology,
        source_batch=source_batch,
    )


def _selection(
    *,
    family: str = "CORPORATE_ACTION",
    record_count: int = 0,
) -> reader._EventCoverageSelection:
    """构造一个完整单窗选择结果。"""
    identifier_version_id = UUID("20000000-0000-4000-8000-000000000001")
    candidate = _candidate(
        family=family,
        start=date(2026, 1, 1),
        end=date(2026, 1, 31),
        created_at=datetime(2026, 2, 1, 8, tzinfo=UTC),
        record_count=record_count,
    )
    result = reader._select_event_coverage(
        (candidate,),
        family=family,
        dataset=reader._EVENT_DATASETS[family],
        security_id=7,
        identifier_version_id=identifier_version_id,
        start=date(2026, 1, 1),
        end=date(2026, 1, 31),
    )
    assert result is not None
    return result


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"start": "2026-01-01"},
        {"end": "2026-01-31"},
    ],
)
def test_event_query_requires_both_finite_window_boundaries(body: dict[str, str]) -> None:
    """无界或半界事件请求必须在进入数据库前稳定返回四百。"""
    response = TestClient(_app()).post(
        "/internal/v1/equities/SSE/600519/events/query",
        headers={"Authorization": "Bearer reader-secret"},
        json=body,
    )

    assert response.status_code == 400
    assert response.json()["code"] == "validation-error"


def test_continuous_coverage_uses_latest_committed_observation_cutoff() -> None:
    """跨月窗口必须连续，且二月较晚证据中的非空事实不能按一月旧截止点读取。"""
    identifier_version_id = UUID("20000000-0000-4000-8000-000000000001")
    reused_publication = _publication(
        dataset=reader._EVENT_DATASETS["EARNINGS_EXPRESS"],
        knowledge_cutoff=datetime(2026, 1, 15, 8, tzinfo=UTC),
    )
    january = _candidate(
        family="EARNINGS_EXPRESS",
        start=date(2026, 1, 1),
        end=date(2026, 1, 31),
        created_at=datetime(2026, 2, 1, 8, tzinfo=UTC),
        publication=reused_publication,
    )
    february = _candidate(
        family="EARNINGS_EXPRESS",
        start=date(2026, 2, 1),
        end=date(2026, 2, 28),
        created_at=datetime(2026, 3, 1, 8, tzinfo=UTC),
        publication=reused_publication,
        record_count=1,
    )

    selected = reader._select_event_coverage(
        (january, february),
        family="EARNINGS_EXPRESS",
        dataset=reader._EVENT_DATASETS["EARNINGS_EXPRESS"],
        security_id=7,
        identifier_version_id=identifier_version_id,
        start=date(2026, 1, 1),
        end=date(2026, 2, 28),
    )

    assert selected is not None
    assert selected.view_cutoff == datetime(2026, 3, 1, 8, tzinfo=UTC)
    assert [item.evidence.coverage.coverage_version for item in selected.segments] == [
        january.coverage.coverage_version,
        february.coverage.coverage_version,
    ]


def test_one_calendar_day_gap_rejects_event_family() -> None:
    """任一自然日 coverage 缺口都不能由相邻 publication 或已有事实补造。"""
    identifier_version_id = UUID("20000000-0000-4000-8000-000000000001")
    candidates = (
        _candidate(
            family="DRAGON_TIGER",
            start=date(2026, 1, 1),
            end=date(2026, 1, 31),
            created_at=datetime(2026, 2, 1, tzinfo=UTC),
        ),
        _candidate(
            family="DRAGON_TIGER",
            start=date(2026, 2, 2),
            end=date(2026, 2, 28),
            created_at=datetime(2026, 3, 1, tzinfo=UTC),
        ),
    )

    selected = reader._select_event_coverage(
        candidates,
        family="DRAGON_TIGER",
        dataset=reader._EVENT_DATASETS["DRAGON_TIGER"],
        security_id=7,
        identifier_version_id=identifier_version_id,
        start=date(2026, 1, 1),
        end=date(2026, 2, 28),
    )

    assert selected is None


def test_reused_publication_new_coverage_invalidates_composite_version() -> None:
    """复用零记录 publication 的新覆盖观察仍必须改变事件版本和 ETag 输入。"""
    identifier_version_id = UUID("20000000-0000-4000-8000-000000000001")
    publication = _publication(dataset=reader._EVENT_DATASETS["BLOCK_TRADE"])
    original = _candidate(
        family="BLOCK_TRADE",
        start=date(2026, 1, 1),
        end=date(2026, 1, 31),
        created_at=datetime(2026, 2, 1, tzinfo=UTC),
        publication=publication,
    )
    refreshed = _candidate(
        family="BLOCK_TRADE",
        start=date(2026, 1, 10),
        end=date(2026, 1, 20),
        created_at=datetime(2026, 2, 2, tzinfo=UTC),
        publication=publication,
    )
    first = reader._select_event_coverage(
        (original,),
        family="BLOCK_TRADE",
        dataset=reader._EVENT_DATASETS["BLOCK_TRADE"],
        security_id=7,
        identifier_version_id=identifier_version_id,
        start=date(2026, 1, 1),
        end=date(2026, 1, 31),
    )
    second = reader._select_event_coverage(
        (original, refreshed),
        family="BLOCK_TRADE",
        dataset=reader._EVENT_DATASETS["BLOCK_TRADE"],
        security_id=7,
        identifier_version_id=identifier_version_id,
        start=date(2026, 1, 1),
        end=date(2026, 1, 31),
    )

    assert first is not None
    assert second is not None
    assert second.view_cutoff == refreshed.coverage.created_at
    assert first.data_version != second.data_version
    assert refreshed.coverage.coverage_version in {
        item.evidence.coverage.coverage_version for item in second.segments
    }


def test_multiple_complete_source_methodology_groups_fail_closed() -> None:
    """两个来源方法学都声称完整覆盖时必须拒绝任意选择。"""
    identifier_version_id = UUID("20000000-0000-4000-8000-000000000001")
    first = _candidate(
        family="CORPORATE_ACTION",
        start=date(2026, 1, 1),
        end=date(2026, 1, 31),
        created_at=datetime(2026, 2, 1, tzinfo=UTC),
    )
    second = _candidate(
        family="CORPORATE_ACTION",
        start=date(2026, 1, 1),
        end=date(2026, 1, 31),
        created_at=datetime(2026, 2, 1, tzinfo=UTC),
        provider_id="other-provider",
        upstream_source="Other",
        methodology_id=UUID("30000000-0000-4000-8000-000000000001"),
    )

    selected = reader._select_event_coverage(
        (first, second),
        family="CORPORATE_ACTION",
        dataset=reader._EVENT_DATASETS["CORPORATE_ACTION"],
        security_id=7,
        identifier_version_id=identifier_version_id,
        start=date(2026, 1, 1),
        end=date(2026, 1, 31),
    )

    assert selected is None


def test_event_release_is_independent_and_contains_coverage_components() -> None:
    """事件复合版本必须只由实际 coverage 组成，并在缺族时标记 warning。"""
    action = _selection(family="CORPORATE_ACTION")
    dragon = _selection(family="DRAGON_TIGER")
    first = reader._event_release(
        {"CORPORATE_ACTION": action},
        requested={"CORPORATE_ACTION", "DRAGON_TIGER"},
        requested_end=date(2026, 1, 31),
    )
    second = reader._event_release(
        {"CORPORATE_ACTION": action, "DRAGON_TIGER": dragon},
        requested={"CORPORATE_ACTION", "DRAGON_TIGER"},
        requested_end=date(2026, 1, 31),
    )

    assert first["dataset"] == "equity.events.composite"
    assert first["qualityStatus"] == "warning"
    assert first["dataVersion"] != second["dataVersion"]
    assert second["qualityStatus"] == "passed"
    assert second["effectiveAsOf"] == "2026-01-31"


def test_event_partial_response_supports_304(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """部分事件族仍返回二百，且相同 coverage 与请求可条件命中三零四。"""
    action = _selection(family="CORPORATE_ACTION")
    identity = SimpleNamespace(security_id=7)
    identifier = SimpleNamespace(version_id=action.identifier_version_id)

    def resolve_identity(*_args: object, **_kwargs: object) -> tuple[Any, Any]:
        """返回固定双时态身份。"""
        return identity, identifier

    def select_coverages(
        *_args: object,
        **_kwargs: object,
    ) -> dict[str, reader._EventCoverageSelection]:
        """仅让公司行动族具备完整 coverage。"""
        return {"CORPORATE_ACTION": action}

    def no_events(*_args: object, **_kwargs: object) -> list[dict[str, Any]]:
        """模拟已覆盖但证券没有事件的合法空页。"""
        return []

    monkeypatch.setattr(reader, "_identity_for_event_window", resolve_identity)
    monkeypatch.setattr(reader, "_event_coverages", select_coverages)
    monkeypatch.setattr(reader, "_event_rows", no_events)
    client = TestClient(_app())
    request_body = {
        "families": ["CORPORATE_ACTION", "DRAGON_TIGER"],
        "asOf": "2026-01-15",
        "start": "2026-01-01",
        "end": "2026-01-31",
        "limit": 20,
    }
    headers = {
        "Authorization": "Bearer reader-secret",
        "X-Request-Id": "event-request-1",
    }
    first = client.post(
        "/internal/v1/equities/SSE/600519/events/query",
        headers=headers,
        json=request_body,
    )

    assert first.status_code == 200
    assert first.json()["reasonCode"] == "EVENT_FAMILY_PARTIAL"
    assert first.headers["x-data-version"] == first.json()["release"]["dataVersion"]

    second = client.post(
        "/internal/v1/equities/SSE/600519/events/query",
        headers={**headers, "If-None-Match": first.headers["etag"]},
        json=request_body,
    )

    assert second.status_code == 304
    assert second.content == b""


def test_event_facts_use_coverage_cutoff_and_no_publication_cross_join() -> None:
    """累计事实只按一个 coverage 截止点读取，公告日过滤且不跨 publication 连接。"""
    earnings = _selection(family="EARNINGS_EXPRESS")
    dragon = _selection(family="DRAGON_TIGER")
    block = _selection(family="BLOCK_TRADE")
    session = _CaptureSession()

    reader._append_earnings_events(
        session,
        events=[],
        security_id=7,
        family="EARNINGS_EXPRESS",
        coverage=earnings,
    )
    reader._append_dragon_events(
        session,
        [],
        7,
        coverage=dragon,
    )
    reader._append_block_events(
        session,
        [],
        7,
        coverage=block,
    )

    earnings_sql, dragon_sql, block_sql = (str(statement) for statement in session.statements)
    assert "disclosure_document.announced_on" in earnings_sql
    assert "corporate_event_revision.methodology_version_id" in earnings_sql
    assert "dragon_tiger_event_revision.methodology_version_id" in dragon_sql
    assert "block_trade_execution_revision.methodology_version_id" in block_sql
    assert "dataset_publication" not in earnings_sql
    assert "dataset_publication" not in dragon_sql
    assert "dataset_publication" not in block_sql
    assert earnings.view_cutoff in session.statements[0].compile().params.values()


def test_event_fact_sql_uses_each_selected_slice_cutoff_without_cross_window_leak() -> None:
    """一月纠错不能把其四月知识截止点用于二月事实，SQL 必须绑定逐片日期与截止点。"""
    identifier_version_id = UUID("20000000-0000-4000-8000-000000000001")
    january_cutoff = datetime(2026, 2, 1, tzinfo=UTC)
    february_cutoff = datetime(2026, 3, 1, tzinfo=UTC)
    correction_cutoff = datetime(2026, 4, 1, tzinfo=UTC)
    january = _candidate(
        family="BLOCK_TRADE",
        start=date(2026, 1, 1),
        end=date(2026, 1, 31),
        created_at=january_cutoff,
    )
    february = _candidate(
        family="BLOCK_TRADE",
        start=date(2026, 2, 1),
        end=date(2026, 2, 28),
        created_at=february_cutoff,
    )
    correction = _candidate(
        family="BLOCK_TRADE",
        start=date(2026, 1, 15),
        end=date(2026, 1, 15),
        created_at=correction_cutoff,
    )
    selection = reader._select_event_coverage(
        (january, february, correction),
        family="BLOCK_TRADE",
        dataset=reader._EVENT_DATASETS["BLOCK_TRADE"],
        security_id=7,
        identifier_version_id=identifier_version_id,
        start=date(2026, 1, 1),
        end=date(2026, 2, 28),
    )
    assert selection is not None
    session = _CaptureSession()

    reader._append_block_events(
        session,
        [],
        7,
        coverage=selection,
    )

    statement = session.statements[0]
    params = list(statement.compile().params.values())
    assert january_cutoff in params
    assert february_cutoff in params
    assert correction_cutoff in params
    assert str(statement).count("block_trade_execution_revision.known_from <=") == 4
    assert selection.view_cutoff == correction_cutoff


def test_event_release_reports_latest_cutoff_used_by_any_family() -> None:
    """复合 release 必须声明正文实际使用的最晚 slice 知识时刻。"""
    action = _selection(family="CORPORATE_ACTION")
    late_candidate = _candidate(
        family="DRAGON_TIGER",
        start=date(2026, 1, 1),
        end=date(2026, 1, 31),
        created_at=datetime(2026, 4, 1, 8, tzinfo=UTC),
    )
    late = reader._select_event_coverage(
        (late_candidate,),
        family="DRAGON_TIGER",
        dataset=reader._EVENT_DATASETS["DRAGON_TIGER"],
        security_id=7,
        identifier_version_id=action.identifier_version_id,
        start=date(2026, 1, 1),
        end=date(2026, 1, 31),
    )
    assert late is not None

    release = reader._event_release(
        {"CORPORATE_ACTION": action, "DRAGON_TIGER": late},
        requested={"CORPORATE_ACTION", "DRAGON_TIGER"},
        requested_end=date(2026, 1, 31),
    )

    assert release["knowledgeCutoff"] == "2026-04-01T08:00:00Z"


def test_latest_status_prefers_latest_business_date_over_late_old_correction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """晚提交的旧日纠错不得让未指定 asOf 的数据状态从当前窗口倒退。"""
    old_correction = _candidate(
        family="DRAGON_TIGER",
        start=date(2020, 1, 2),
        end=date(2020, 1, 2),
        created_at=datetime(2026, 4, 2, tzinfo=UTC),
    )
    current = _candidate(
        family="DRAGON_TIGER",
        start=date(2026, 3, 1),
        end=date(2026, 3, 31),
        created_at=datetime(2026, 4, 1, tzinfo=UTC),
    )

    def candidates(*_args: object, **_kwargs: object) -> tuple[Any, ...]:
        """返回提交较晚的旧窗纠错和业务日期更晚的当前窗。"""
        return old_correction, current

    monkeypatch.setattr(reader, "_event_coverage_candidates", candidates)
    selected = reader._latest_event_coverage_selection(
        object(),
        family="DRAGON_TIGER",
        security_id=7,
        identifier_version_id=current.coverage.identifier_version_id,
        known_at=None,
    )

    assert selected is not None
    assert selected.coverage_from == selected.coverage_to == date(2026, 3, 31)
    assert selected.segments[0].evidence == current


def test_event_dataset_status_uses_coverage_empty_and_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """成功零记录 coverage 必须返回 EMPTY，并暴露真实来源方法学和覆盖版本。"""
    selection = _selection(family="EARNINGS_FORECAST", record_count=0)

    def select_point(*_args: object, **_kwargs: object) -> reader._EventCoverageSelection:
        """返回已证明当前日期的零记录 coverage。"""
        return selection

    def no_facts(*_args: object, **_kwargs: object) -> list[dict[str, Any]]:
        """返回与 coverage 一致的精确日期空事实集。"""
        return []

    monkeypatch.setattr(reader, "_event_coverage_selection", select_point)
    monkeypatch.setattr(reader, "_event_rows", no_facts)
    status = reader._event_dataset_status(
        object(),
        family="EARNINGS_FORECAST",
        security_id=7,
        identifier_version_id=selection.identifier_version_id,
        as_of=date(2026, 1, 15),
        known_at=datetime(2026, 2, 2, tzinfo=UTC),
    )

    assert status["availability"] == "EMPTY"
    assert status["reasonCode"] == "NO_EVENTS"
    assert status["dataVersion"] == str(selection.data_version)
    assert status["sourceLabel"] == "Eastmoney"
    assert status["methodology"] == {
        "code": "eastmoney.event.reported",
        "version": "1",
    }


def test_corporate_action_status_uses_leaf_fact_publication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """公司行动 status 必须返回 `/corporate-actions` 能严格验证的事实版本。"""
    selection = _selection(family="CORPORATE_ACTION", record_count=0)
    fact_publication = _publication(
        dataset=reader._EVENT_DATASETS["CORPORATE_ACTION"],
        data_version=UUID("40000000-0000-4000-8000-000000000001"),
    )

    def select_point(*_args: object, **_kwargs: object) -> reader._EventCoverageSelection:
        """返回已证明当前日期的公司行动零记录 coverage。"""
        return selection

    def no_facts(*_args: object, **_kwargs: object) -> list[dict[str, Any]]:
        """返回与 coverage 一致的精确日期空事实集。"""
        return []

    monkeypatch.setattr(reader, "_event_coverage_selection", select_point)
    monkeypatch.setattr(reader, "_event_rows", no_facts)
    monkeypatch.setattr(
        reader, "_event_fact_publication", lambda *_args, **_kwargs: fact_publication
    )

    status = reader._event_dataset_status(
        object(),
        family="CORPORATE_ACTION",
        security_id=7,
        identifier_version_id=selection.identifier_version_id,
        as_of=date(2026, 1, 15),
        known_at=datetime(2026, 2, 2, tzinfo=UTC),
    )

    assert status["availability"] == "EMPTY"
    assert status["dataVersion"] == str(fact_publication.data_version)


def test_corporate_action_status_fails_closed_without_leaf_fact_publication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """coverage 即使完整，缺少与之同知识切片的事实 publication 也不能给出伪版本。"""
    selection = _selection(family="CORPORATE_ACTION", record_count=0)

    def select_point(*_args: object, **_kwargs: object) -> reader._EventCoverageSelection:
        """返回已证明当前日期的公司行动零记录 coverage。"""
        return selection

    monkeypatch.setattr(reader, "_event_coverage_selection", select_point)
    monkeypatch.setattr(reader, "_event_fact_publication", lambda *_args, **_kwargs: None)

    status = reader._event_dataset_status(
        object(),
        family="CORPORATE_ACTION",
        security_id=7,
        identifier_version_id=selection.identifier_version_id,
        as_of=date(2026, 1, 15),
        known_at=datetime(2026, 2, 2, tzinfo=UTC),
    )

    assert status["availability"] == "SOURCE_UNAVAILABLE"
    assert status["reasonCode"] == "FACT_PUBLICATION_UNAVAILABLE"


def test_event_dataset_status_uses_same_fact_view_for_overlapping_observations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """旧宽空窗与新窄非空窗重叠时，状态必须跟随累计事实而非贪心证明段。"""
    identifier_version_id = UUID("20000000-0000-4000-8000-000000000001")
    wide = _candidate(
        family="BLOCK_TRADE",
        start=date(2026, 1, 1),
        end=date(2026, 1, 31),
        created_at=datetime(2026, 2, 1, tzinfo=UTC),
        record_count=0,
    )
    narrow = _candidate(
        family="BLOCK_TRADE",
        start=date(2026, 1, 15),
        end=date(2026, 1, 15),
        created_at=datetime(2026, 2, 2, tzinfo=UTC),
        record_count=1,
    )
    selection = reader._select_event_coverage(
        (wide, narrow),
        family="BLOCK_TRADE",
        dataset=reader._EVENT_DATASETS["BLOCK_TRADE"],
        security_id=7,
        identifier_version_id=identifier_version_id,
        start=date(2026, 1, 15),
        end=date(2026, 1, 15),
    )
    assert selection is not None

    def select_point(*_args: object, **_kwargs: object) -> reader._EventCoverageSelection:
        """返回宽空窗与新窄非空窗的共同可见选择。"""
        return selection

    def one_fact(*_args: object, **_kwargs: object) -> list[dict[str, Any]]:
        """模拟同一累计截止点在该日实际读取到一笔事实。"""
        return [{"eventId": "block-trade:test"}]

    monkeypatch.setattr(reader, "_event_coverage_selection", select_point)
    monkeypatch.setattr(reader, "_event_rows", one_fact)
    status = reader._event_dataset_status(
        object(),
        family="BLOCK_TRADE",
        security_id=7,
        identifier_version_id=identifier_version_id,
        as_of=date(2026, 1, 15),
        known_at=datetime(2026, 2, 3, tzinfo=UTC),
    )

    assert selection.segments[0].evidence == narrow
    assert status["availability"] == "AVAILABLE"
    assert status["reasonCode"] is None
