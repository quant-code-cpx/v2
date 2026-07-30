"""0015 内部资金流路由、鉴权和条件响应测试。"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import cast
from uuid import UUID

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from service_data_sync.application.ports.money_flow import (
    MoneyFlowDailyPage,
    MoneyFlowMethodologyPage,
    MoneyFlowRankingPage,
    MoneyFlowReadRepository,
)
from service_data_sync.domain.money_flow import MoneyFlowScope, MoneyFlowScopeType
from service_data_sync.infrastructure.persistence.money_flow_read_repository import (
    MoneyFlowCursorMismatch,
    MoneyFlowIdentityBoundary,
    MoneyFlowReadUnavailable,
)
from service_data_sync.interfaces.internal_money_flow_api import (
    register_money_flow_routes,
)

_DAILY_VERSION = UUID("00000000-0000-4000-8000-000000000102")


class FakeReadRepository:
    """返回三类冻结页面，并支持注入仓储错误。"""

    def __init__(
        self,
        *,
        failure: Exception | None = None,
        return_none: bool = False,
    ) -> None:
        """保存错误注入、空结果策略和收到的 scope。"""
        self.failure = failure
        self.return_none = return_none
        self.daily_calls: list[dict[str, object]] = []
        self.ranking_calls: list[dict[str, object]] = []

    def list_methodologies(self, **_: object) -> MoneyFlowMethodologyPage | None:
        """返回一个可公开的 validated 方法学目录页。"""
        if self.failure is not None:
            raise self.failure
        if self.return_none:
            return None
        return MoneyFlowMethodologyPage(
            data_version=UUID("00000000-0000-4000-8000-000000000015"),
            published_at=datetime(2026, 7, 24, 10, tzinfo=UTC),
            items=({"methodologyId": "fixture-money-flow"},),
            next_cursor="next-methodology",
        )

    def list_daily(self, **kwargs: object) -> MoneyFlowDailyPage | None:
        """返回调用 scope 对应的强身份日序列。"""
        if self.failure is not None:
            raise self.failure
        if self.return_none:
            return None
        self.daily_calls.append(kwargs)
        scope = cast(MoneyFlowScope, kwargs["scope"])
        known_at = cast(datetime | None, kwargs["known_at"])
        return MoneyFlowDailyPage(
            series_id=UUID("00000000-0000-4000-8000-000000000101"),
            data_version=_DAILY_VERSION,
            published_at=datetime(2026, 7, 24, 11, tzinfo=UTC),
            methodology={
                "methodologyId": kwargs["methodology_id"],
                "methodologyVersion": kwargs["methodology_version"],
            },
            scope={"scopeType": scope.scope_type.value},
            universe="cn-a",
            bucket=str(kwargs["bucket"]),
            known_at_applied=known_at,
            items=(
                {
                    "tradeDate": "2026-07-24",
                    "netAmount": "1",
                    "netRatio": "0.01",
                },
            ),
            next_cursor=None,
        )

    def list_ranking(self, **kwargs: object) -> MoneyFlowRankingPage | None:
        """返回保留 supplier position 的不可变排行页。"""
        if self.failure is not None:
            raise self.failure
        if self.return_none:
            return None
        self.ranking_calls.append(kwargs)
        scope_type = cast(MoneyFlowScopeType, kwargs["scope_type"])
        return MoneyFlowRankingPage(
            data_version=UUID("00000000-0000-4000-8000-000000000103"),
            published_at=datetime(2026, 7, 24, 12, tzinfo=UTC),
            methodology={
                "methodologyId": kwargs["methodology_id"],
                "methodologyVersion": kwargs["methodology_version"],
            },
            snapshot={
                "snapshotId": "00000000-0000-4000-8000-000000000104",
                "scopeType": scope_type.value,
            },
            items=(
                {
                    "supplierPosition": 1,
                    "netAmount": "1",
                },
            ),
            next_cursor=None,
        )


def _app(repository: MoneyFlowReadRepository | None = None) -> FastAPI:
    """构造带稳定问题映射的最小内部应用。"""
    app = FastAPI()

    def require_service_bearer() -> None:
        """测试依赖代表已通过的服务凭据。"""

    def unavailable_problem() -> Exception:
        """构造依赖不可用问题。"""
        return HTTPException(status_code=503, detail="unavailable")

    def not_found_problem() -> Exception:
        """构造未找到问题。"""
        return HTTPException(status_code=404, detail="not found")

    def validation_problem(detail: str) -> Exception:
        """构造参数校验问题。"""
        return HTTPException(status_code=400, detail=detail)

    def conflict_problem(detail: str) -> Exception:
        """构造游标或身份冲突问题。"""
        return HTTPException(status_code=409, detail=detail)

    def snapshot_problem() -> Exception:
        """构造 publication 已切换问题。"""
        return HTTPException(status_code=409, detail="snapshot expired")

    register_money_flow_routes(
        app,
        require_service_bearer=require_service_bearer,
        unavailable_problem=unavailable_problem,
        not_found_problem=not_found_problem,
        validation_problem=validation_problem,
        conflict_problem=conflict_problem,
        snapshot_problem=snapshot_problem,
        repository=repository or FakeReadRepository(),
    )
    return app


def _app_without_repository() -> FastAPI:
    """构造未配置读取仓储的应用，用于验证 503 fail-closed。"""
    app = FastAPI()

    def require_service_bearer() -> None:
        """测试依赖代表已通过的服务凭据。"""

    def unavailable_problem() -> Exception:
        """构造依赖不可用问题。"""
        return HTTPException(status_code=503, detail="unavailable")

    def not_found_problem() -> Exception:
        """构造未找到问题。"""
        return HTTPException(status_code=404, detail="not found")

    def validation_problem(detail: str) -> Exception:
        """构造参数校验问题。"""
        return HTTPException(status_code=400, detail=detail)

    def conflict_problem(detail: str) -> Exception:
        """构造游标或身份冲突问题。"""
        return HTTPException(status_code=409, detail=detail)

    def snapshot_problem() -> Exception:
        """构造 publication 已切换问题。"""
        return HTTPException(status_code=409, detail="snapshot expired")

    register_money_flow_routes(
        app,
        require_service_bearer=require_service_bearer,
        unavailable_problem=unavailable_problem,
        not_found_problem=not_found_problem,
        validation_problem=validation_problem,
        conflict_problem=conflict_problem,
        snapshot_problem=snapshot_problem,
        repository=None,
    )
    return app


def test_methodology_route_returns_versioned_etag_and_304() -> None:
    """验证内部 GET 带完整缓存头，命中后没有响应体。"""
    client = TestClient(_app())

    first = client.get("/internal/v1/money-flow/methodologies")
    second = client.get(
        "/internal/v1/money-flow/methodologies",
        headers={"If-None-Match": first.headers["etag"]},
    )

    assert first.status_code == 200
    assert first.headers["x-data-version"] == first.json()["dataVersion"]
    assert second.status_code == 304
    assert second.content == b""


def test_all_money_flow_internal_routes_are_get_only() -> None:
    """验证内部接口只提供只读 GET，公开 POST 由 service-api 独占。"""
    money_flow_routes = [
        route
        for route in _app().routes
        if getattr(route, "path", "").startswith("/internal/v1/money-flow")
    ]

    assert len(money_flow_routes) == 5
    assert all(getattr(route, "methods", set()) == {"GET"} for route in money_flow_routes)


def test_daily_routes_preserve_equity_sector_and_market_scope() -> None:
    """三类日序列路由分别构造强身份，不互相聚合或 fallback。"""
    repository = FakeReadRepository()
    client = TestClient(_app(repository))
    query = "?methodologyVersion=1&bucket=main&start=2026-07-01&end=2026-07-24&limit=20"
    responses = (
        client.get(
            "/internal/v1/money-flow/methodologies/fixture-money-flow"
            f"/daily-series/equities/SSE/600000{query}&dataVersion={_DAILY_VERSION}"
        ),
        client.get(
            "/internal/v1/money-flow/methodologies/fixture-money-flow"
            f"/daily-series/sectors/eastmoney.industry/BK0475{query}"
        ),
        client.get(
            "/internal/v1/money-flow/methodologies/fixture-money-flow"
            f"/daily-series/markets/cn-a{query}"
        ),
    )

    assert [response.status_code for response in responses] == [200, 200, 200]
    assert [response.json()["scope"]["scopeType"] for response in responses] == [
        "equity",
        "sector",
        "market",
    ]
    call_scopes = [
        cast(MoneyFlowScope, call["scope"]).scope_type.value for call in repository.daily_calls
    ]
    assert call_scopes == [
        "equity",
        "sector",
        "market",
    ]
    assert all(response.headers["cache-control"].startswith("private") for response in responses)


def test_ranking_route_preserves_supplier_position_and_conditional_etag() -> None:
    """排行响应原样返回 supplier position，ETag 命中后返回 304。"""
    repository = FakeReadRepository()
    client = TestClient(_app(repository))
    path = (
        "/internal/v1/money-flow/methodologies/fixture-money-flow/supplier-rankings"
        "?methodologyVersion=1&scopeType=equity&universe=cn-a"
        "&windowType=supplier_day&windowSize=1&bucket=main"
        "&tradeDate=2026-07-24"
    )
    first = client.get(path)
    second = client.get(path, headers={"If-None-Match": first.headers["etag"]})

    assert first.status_code == 200
    assert first.json()["items"][0]["supplierPosition"] == 1
    assert second.status_code == 304
    trade_date = cast(date, repository.ranking_calls[0]["trade_date"])
    assert trade_date.isoformat() == "2026-07-24"


def test_internal_routes_map_repository_failures_to_stable_statuses() -> None:
    """游标、身份、参数、依赖和空结果分别映射 409、400、503、404。"""
    methodology_path = "/internal/v1/money-flow/methodologies"
    daily_path = (
        "/internal/v1/money-flow/methodologies/fixture-money-flow"
        "/daily-series/equities/SSE/600000"
        f"?methodologyVersion=1&bucket=main&start=2026-07-01&end=2026-07-24"
        f"&dataVersion={_DAILY_VERSION}"
    )
    ranking_path = (
        "/internal/v1/money-flow/methodologies/fixture-money-flow/supplier-rankings"
        "?methodologyVersion=1&scopeType=equity&universe=cn-a"
        "&windowType=supplier_day&windowSize=1&bucket=main"
    )

    cursor = TestClient(_app(FakeReadRepository(failure=MoneyFlowCursorMismatch("bad cursor"))))
    identity = TestClient(
        _app(FakeReadRepository(failure=MoneyFlowIdentityBoundary("identity boundary")))
    )
    validation = TestClient(_app(FakeReadRepository(failure=ValueError("bad filter"))))
    unavailable = TestClient(_app(FakeReadRepository(failure=MoneyFlowReadUnavailable("database"))))
    missing = TestClient(_app(FakeReadRepository(return_none=True)))

    assert cursor.get(methodology_path).status_code == 409
    assert identity.get(daily_path).status_code == 409
    assert validation.get(daily_path).status_code == 400
    assert unavailable.get(methodology_path).status_code == 503
    assert missing.get(daily_path).status_code == 409
    assert missing.get(ranking_path).status_code == 404
    assert missing.get(methodology_path).status_code == 503
    assert TestClient(_app_without_repository()).get(methodology_path).status_code == 503


def test_daily_route_rejects_future_known_at_before_repository_read() -> None:
    """未来知识时点必须在仓储访问前以 400 拒绝。"""
    repository = FakeReadRepository()
    response = TestClient(_app(repository)).get(
        "/internal/v1/money-flow/methodologies/fixture-money-flow"
        "/daily-series/equities/SSE/600000"
        "?methodologyVersion=1&bucket=main&start=2026-07-01&end=2026-07-24"
        f"&dataVersion={_DAILY_VERSION}"
        "&knownAt=2099-01-01T00%3A00%3A00Z"
    )

    assert response.status_code == 400
    assert repository.daily_calls == []


def test_equity_daily_route_rejects_mismatched_status_data_version() -> None:
    """个股资金流 publication 漂移必须返回 409，禁止静默读取最新序列。"""
    wrong_version = UUID("00000000-0000-4000-8000-000000000199")
    response = TestClient(_app()).get(
        "/internal/v1/money-flow/methodologies/fixture-money-flow"
        "/daily-series/equities/SSE/600000"
        "?methodologyVersion=1&bucket=main&start=2026-07-01&end=2026-07-24"
        f"&dataVersion={wrong_version}"
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "snapshot expired"
