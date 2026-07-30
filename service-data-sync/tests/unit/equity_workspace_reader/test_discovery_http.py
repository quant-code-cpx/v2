"""股票中心发现内部路由的认证、条件读取和无发布专项测试。"""

from __future__ import annotations

from contextlib import AbstractContextManager
from datetime import UTC, date, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from service_data_sync.interfaces import internal_equity_workspace_api as reader
from service_data_sync.interfaces.internal_sector_api import InternalProblem


class _Rows:
    """提供 SQLAlchemy 结果对象在 reader 中需要的最小行为。"""

    def __init__(self, values: list[Any]) -> None:
        """保存一次查询将返回的行。"""
        self._values = values

    def all(self) -> list[Any]:
        """返回全部测试行。"""
        return list(self._values)


class _Session:
    """按调用顺序返回发现页行与空 availability。"""

    def __init__(self, rows: list[Any]) -> None:
        """保存发现页候选并初始化标量查询次数。"""
        self._rows = rows
        self._scalar_calls = 0

    def scalars(self, _statement: Any) -> _Rows:
        """首次返回页面行，后续返回空辅助结果。"""
        self._scalar_calls += 1
        return _Rows(self._rows if self._scalar_calls == 1 else [])

    def execute(self, _statement: Any) -> _Rows:
        """组件去重查询返回空集合。"""
        return _Rows([])


class _SessionContext(AbstractContextManager[_Session]):
    """把测试 Session 暴露为数据库上下文管理器。"""

    def __init__(self, session: _Session) -> None:
        """保存待返回的测试 Session。"""
        self._session = session

    def __enter__(self) -> _Session:
        """进入上下文并返回测试 Session。"""
        return self._session

    def __exit__(self, *_args: object) -> None:
        """退出上下文时无需清理外部资源。"""
        return None


class _Database:
    """提供 reader 注册所需的最小数据库外观。"""

    def __init__(self, rows: list[Any]) -> None:
        """保存每个请求可读取的页面行。"""
        self._rows = rows

    def session(self) -> _SessionContext:
        """为每个请求创建独立查询计数的 Session。"""
        return _SessionContext(_Session(self._rows))


def _require_bearer(authorization: str | None = Header(default=None)) -> None:
    """仅接受专项测试固定的内部 Bearer。"""
    if authorization != "Bearer reader-secret":
        raise HTTPException(status_code=401)


def _app(rows: list[Any]) -> FastAPI:
    """构造仅挂载股票中心 reader 的测试应用。"""
    app = FastAPI()

    @app.exception_handler(InternalProblem)
    async def render_problem(request: Request, error: InternalProblem) -> JSONResponse:
        """把 reader 稳定问题投影为可断言的最小响应。"""
        request_id = request.headers.get("X-Request-Id", "generated")
        return JSONResponse(
            status_code=error.status,
            content={"code": error.code, "detail": error.detail},
            headers={"X-Request-Id": request_id},
        )

    reader.register_equity_workspace_routes(
        app,
        database=_Database(rows),  # type: ignore[arg-type]
        require_service_bearer=_require_bearer,
        cursor_secret=b"reader-cursor-secret",
    )
    return app


def _row() -> SimpleNamespace:
    """构造只投影基础身份列的冻结发现行。"""
    return SimpleNamespace(
        release_id=uuid4(),
        security_id=17,
        exchange="SSE",
        symbol="600519",
        name="贵州茅台",
        lifecycle_status="LISTED",
        trading_status="TRADED",
        trading_status_reason=None,
        listed_on=date(2001, 8, 27),
        delisted_on=None,
    )


def _publication(*, quality_status: str = "passed") -> SimpleNamespace:
    """构造 discovery publication 元数据。"""
    return SimpleNamespace(
        publication_id=uuid4(),
        release_id=uuid4(),
        dataset="equity.discovery.eod",
        partition_key="CN_A",
        data_version=UUID("11111111-1111-4111-8111-111111111111"),
        quality_status=quality_status,
        published_at=datetime(2026, 7, 30, 8, tzinfo=UTC),
        superseded_at=None,
        effective_as_of=date(2026, 7, 29),
        knowledge_cutoff=datetime(2026, 7, 30, 8, tzinfo=UTC),
    )


def test_discovery_requires_internal_bearer() -> None:
    """未认证请求必须在访问数据库前返回 401。"""
    response = TestClient(_app([_row()])).post(
        "/internal/v1/equity-discovery/query",
        json={"limit": 10, "columns": ["symbol"]},
    )

    assert response.status_code == 401


def test_discovery_returns_200_then_304_with_request_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一 release 与请求必须复用强 ETag，并在命中时返回 304。"""
    publication = _publication()

    def current_publication(*_args: object, **_kwargs: object) -> Any:
        """返回固定 discovery publication。"""
        return publication

    monkeypatch.setattr(reader, "_discovery_publication", current_publication)
    client = TestClient(_app([_row()]))
    headers = {
        "Authorization": "Bearer reader-secret",
        "X-Request-Id": "reader-request-1",
    }
    first = client.post(
        "/internal/v1/equity-discovery/query",
        headers=headers,
        json={"limit": 10, "columns": ["symbol"]},
    )

    assert first.status_code == 200
    assert first.headers["x-request-id"] == "reader-request-1"
    assert first.headers["x-data-version"] == str(publication.data_version)
    assert first.json()["records"][0]["identity"]["identityAsOf"] == "2026-07-29"

    second = client.post(
        "/internal/v1/equity-discovery/query",
        headers={**headers, "If-None-Match": first.headers["etag"]},
        json={"limit": 10, "columns": ["symbol"]},
    )

    assert second.status_code == 304
    assert second.content == b""
    assert second.headers["x-request-id"] == "reader-request-1"


def test_discovery_surfaces_partial_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """带告警 publication 必须明确投影 PARTIAL，而不是伪装全量。"""

    def warned_publication(*_args: object, **_kwargs: object) -> Any:
        """返回带告警 discovery publication。"""
        return _publication(quality_status="warned")

    monkeypatch.setattr(reader, "_discovery_publication", warned_publication)
    response = TestClient(_app([_row()])).post(
        "/internal/v1/equity-discovery/query",
        headers={"Authorization": "Bearer reader-secret"},
        json={"limit": 10, "columns": ["symbol"]},
    )

    assert response.status_code == 200
    assert response.json()["release"]["completeness"] == "PARTIAL"


def test_discovery_without_publication_is_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """没有 discovery publication 时内部 reader 必须返回稳定 503。"""

    def unavailable(*_args: object, **_kwargs: object) -> None:
        """模拟无可消费 publication。"""
        raise reader._publication_unavailable("Equity discovery publication is unavailable")

    monkeypatch.setattr(reader, "_discovery_publication", unavailable)
    response = TestClient(_app([])).post(
        "/internal/v1/equity-discovery/query",
        headers={
            "Authorization": "Bearer reader-secret",
            "X-Request-Id": "reader-no-publication",
        },
        json={"limit": 10, "columns": ["symbol"]},
    )

    assert response.status_code == 503
    assert response.json()["code"] == "publication-unavailable"
    assert response.headers["x-request-id"] == "reader-no-publication"
