"""资金流方法学、日序列和供应商排行的内部只读 HTTP 路由。

响应先公开方法学与单位，再返回固定 publication 内的个股、板块、市场或供应商原始排行；
接口不会把不同来源、不同统计口径或不同业务日的数据聚合成一个看似连续的数值序列。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import Depends, FastAPI, Header, Path, Query, Request, Response
from fastapi.responses import JSONResponse

from service_data_sync.application.ports.money_flow import (
    MoneyFlowDailyPage,
    MoneyFlowMethodologyPage,
    MoneyFlowRankingPage,
    MoneyFlowReadRepository,
)
from service_data_sync.domain.equity import Exchange
from service_data_sync.domain.money_flow import (
    MoneyFlowScope,
    MoneyFlowScopeType,
    MoneyFlowWindowType,
)
from service_data_sync.infrastructure.persistence.money_flow_read_repository import (
    MoneyFlowCursorMismatch,
    MoneyFlowIdentityBoundary,
    MoneyFlowReadUnavailable,
)

ExchangeCode = Literal["SSE", "SZSE", "BSE"]
SemanticFamily = Literal["trade_direction_flow", "order_size_flow"]
MethodologyStatus = Literal["unknown", "research", "validated", "retired"]
ScopeType = Literal["equity", "sector", "market"]
RankingScopeType = Literal["equity", "sector"]
RankingWindowType = Literal["supplier_day", "supplier_rolling"]
_PRIVATE_REVALIDATE = "private, max-age=0, must-revalidate"


def register_money_flow_routes(
    app: FastAPI,
    *,
    require_service_bearer: Callable[..., None],
    unavailable_problem: Callable[[], Exception],
    not_found_problem: Callable[[], Exception],
    validation_problem: Callable[[str], Exception],
    conflict_problem: Callable[[str], Exception],
    snapshot_problem: Callable[[], Exception],
    repository: MoneyFlowReadRepository | None,
) -> None:
    """注册 0015 的五条只读路径，并统一签名游标和条件响应语义。"""

    @app.get(
        "/internal/v1/money-flow/methodologies",
        dependencies=[Depends(require_service_bearer)],
    )
    def list_money_flow_methodologies(
        request: Request,
        semantic_family: Annotated[SemanticFamily | None, Query(alias="semanticFamily")] = None,
        methodology_status: Annotated[
            MethodologyStatus | None, Query(alias="methodologyStatus")
        ] = None,
        scope_type: Annotated[ScopeType | None, Query(alias="scopeType")] = None,
        cursor: Annotated[str | None, Query(min_length=1, max_length=2048)] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        if_none_match: Annotated[str | None, Header(alias="If-None-Match", max_length=256)] = None,
    ) -> Response:
        """列出已治理方法学；研究条目仅披露定义，不开放值读取。"""
        if repository is None:
            raise unavailable_problem()
        try:
            page = repository.list_methodologies(
                semantic_family=semantic_family,
                methodology_status=methodology_status,
                scope_type=(None if scope_type is None else MoneyFlowScopeType(scope_type)),
                cursor=cursor,
                limit=limit,
            )
        except MoneyFlowCursorMismatch as error:
            raise conflict_problem(str(error)) from error
        except (ValueError, MoneyFlowIdentityBoundary) as error:
            raise validation_problem(str(error)) from error
        except MoneyFlowReadUnavailable as error:
            raise unavailable_problem() from error
        if page is None:
            raise unavailable_problem()
        body = _methodology_page_body(page)
        return _conditional_response(
            request=request,
            if_none_match=if_none_match,
            data_version=str(page.data_version),
            representation={
                "path": "methodologies",
                "semanticFamily": semantic_family,
                "methodologyStatus": methodology_status,
                "scopeType": scope_type,
                "cursor": cursor,
                "limit": limit,
            },
            body=body,
        )

    @app.get(
        "/internal/v1/money-flow/methodologies/{methodologyId}"
        "/daily-series/equities/{exchange}/{symbol}",
        dependencies=[Depends(require_service_bearer)],
    )
    def list_equity_daily_money_flow(
        request: Request,
        methodology_id: Annotated[
            str,
            Path(
                alias="methodologyId",
                pattern=r"^[a-z][a-z0-9_.-]{2,79}$",
            ),
        ],
        exchange: Annotated[ExchangeCode, Path()],
        symbol: Annotated[str, Path(pattern=r"^[0-9]{6}$")],
        methodology_version: Annotated[
            str,
            Query(
                alias="methodologyVersion",
                min_length=1,
                max_length=64,
            ),
        ],
        bucket: Annotated[str, Query(pattern=r"^[a-z][a-z0-9_-]{0,63}$")],
        start: date,
        end: date,
        data_version: Annotated[UUID, Query(alias="dataVersion")],
        known_at: Annotated[datetime | None, Query(alias="knownAt")] = None,
        cursor: Annotated[str | None, Query(min_length=1, max_length=2048)] = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 200,
        if_none_match: Annotated[str | None, Header(alias="If-None-Match", max_length=256)] = None,
    ) -> Response:
        """读取一个证券强身份日序列，并拒绝跨代码复用边界。"""
        return _daily_response(
            request=request,
            repository=repository,
            methodology_id=methodology_id,
            methodology_version=methodology_version,
            scope=MoneyFlowScope(
                scope_type=MoneyFlowScopeType.EQUITY,
                exchange=Exchange(exchange),
                symbol=symbol,
            ),
            bucket=bucket,
            start=start,
            end=end,
            expected_data_version=data_version,
            known_at=known_at,
            cursor=cursor,
            limit=limit,
            if_none_match=if_none_match,
            unavailable_problem=unavailable_problem,
            not_found_problem=not_found_problem,
            validation_problem=validation_problem,
            conflict_problem=conflict_problem,
            snapshot_problem=snapshot_problem,
        )

    @app.get(
        "/internal/v1/money-flow/methodologies/{methodologyId}"
        "/daily-series/sectors/{scheme}/{sectorCode}",
        dependencies=[Depends(require_service_bearer)],
    )
    def list_sector_daily_money_flow(
        request: Request,
        methodology_id: Annotated[
            str,
            Path(
                alias="methodologyId",
                pattern=r"^[a-z][a-z0-9_.-]{2,79}$",
            ),
        ],
        scheme: Annotated[str, Path(min_length=1, max_length=64)],
        sector_code: Annotated[str, Path(alias="sectorCode", min_length=1, max_length=64)],
        methodology_version: Annotated[
            str,
            Query(
                alias="methodologyVersion",
                min_length=1,
                max_length=64,
            ),
        ],
        bucket: Annotated[str, Query(pattern=r"^[a-z][a-z0-9_-]{0,63}$")],
        start: date,
        end: date,
        known_at: Annotated[datetime | None, Query(alias="knownAt")] = None,
        cursor: Annotated[str | None, Query(min_length=1, max_length=2048)] = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 200,
        if_none_match: Annotated[str | None, Header(alias="If-None-Match", max_length=256)] = None,
    ) -> Response:
        """读取上游板块日序列，不由成分证券聚合替代。"""
        return _daily_response(
            request=request,
            repository=repository,
            methodology_id=methodology_id,
            methodology_version=methodology_version,
            scope=MoneyFlowScope(
                scope_type=MoneyFlowScopeType.SECTOR,
                sector_scheme=scheme,
                sector_code=sector_code,
            ),
            bucket=bucket,
            start=start,
            end=end,
            expected_data_version=None,
            known_at=known_at,
            cursor=cursor,
            limit=limit,
            if_none_match=if_none_match,
            unavailable_problem=unavailable_problem,
            not_found_problem=not_found_problem,
            validation_problem=validation_problem,
            conflict_problem=conflict_problem,
            snapshot_problem=snapshot_problem,
        )

    @app.get(
        "/internal/v1/money-flow/methodologies/{methodologyId}/daily-series/markets/{marketCode}",
        dependencies=[Depends(require_service_bearer)],
    )
    def list_market_daily_money_flow(
        request: Request,
        methodology_id: Annotated[
            str,
            Path(
                alias="methodologyId",
                pattern=r"^[a-z][a-z0-9_.-]{2,79}$",
            ),
        ],
        market_code: Annotated[
            str,
            Path(
                alias="marketCode",
                pattern=r"^[a-z][a-z0-9_-]{1,31}$",
            ),
        ],
        methodology_version: Annotated[
            str,
            Query(
                alias="methodologyVersion",
                min_length=1,
                max_length=64,
            ),
        ],
        bucket: Annotated[str, Query(pattern=r"^[a-z][a-z0-9_-]{0,63}$")],
        start: date,
        end: date,
        known_at: Annotated[datetime | None, Query(alias="knownAt")] = None,
        cursor: Annotated[str | None, Query(min_length=1, max_length=2048)] = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 200,
        if_none_match: Annotated[str | None, Header(alias="If-None-Match", max_length=256)] = None,
    ) -> Response:
        """读取供应商市场 scope 日序列，不从证券或板块求和。"""
        return _daily_response(
            request=request,
            repository=repository,
            methodology_id=methodology_id,
            methodology_version=methodology_version,
            scope=MoneyFlowScope(
                scope_type=MoneyFlowScopeType.MARKET,
                market_code=market_code,
                name=market_code,
            ),
            bucket=bucket,
            start=start,
            end=end,
            expected_data_version=None,
            known_at=known_at,
            cursor=cursor,
            limit=limit,
            if_none_match=if_none_match,
            unavailable_problem=unavailable_problem,
            not_found_problem=not_found_problem,
            validation_problem=validation_problem,
            conflict_problem=conflict_problem,
            snapshot_problem=snapshot_problem,
        )

    @app.get(
        "/internal/v1/money-flow/methodologies/{methodologyId}/supplier-rankings",
        dependencies=[Depends(require_service_bearer)],
    )
    def list_supplier_money_flow_ranking(
        request: Request,
        methodology_id: Annotated[
            str,
            Path(
                alias="methodologyId",
                pattern=r"^[a-z][a-z0-9_.-]{2,79}$",
            ),
        ],
        methodology_version: Annotated[
            str,
            Query(
                alias="methodologyVersion",
                min_length=1,
                max_length=64,
            ),
        ],
        scope_type: Annotated[RankingScopeType, Query(alias="scopeType")],
        universe: Annotated[
            str,
            Query(
                alias="universe",
                pattern=r"^[a-z][a-z0-9_.-]{1,99}$",
            ),
        ],
        window_type: Annotated[RankingWindowType, Query(alias="windowType")],
        window_size: Annotated[int, Query(alias="windowSize", ge=1, le=252)],
        bucket: Annotated[str, Query(pattern=r"^[a-z][a-z0-9_-]{0,63}$")],
        trade_date: Annotated[date | None, Query(alias="tradeDate")] = None,
        cursor: Annotated[str | None, Query(min_length=1, max_length=2048)] = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
        if_none_match: Annotated[str | None, Header(alias="If-None-Match", max_length=256)] = None,
    ) -> Response:
        """读取一份不可变供应商排行，并原样保留供应商位置。"""
        if repository is None:
            raise unavailable_problem()
        try:
            page = repository.list_ranking(
                methodology_id=methodology_id,
                methodology_version=methodology_version,
                scope_type=MoneyFlowScopeType(scope_type),
                universe=universe,
                window_type=MoneyFlowWindowType(window_type),
                window_size=window_size,
                bucket=bucket,
                trade_date=trade_date,
                cursor=cursor,
                limit=limit,
            )
        except MoneyFlowCursorMismatch as error:
            raise conflict_problem(str(error)) from error
        except (ValueError, MoneyFlowIdentityBoundary) as error:
            raise validation_problem(str(error)) from error
        except MoneyFlowReadUnavailable as error:
            raise unavailable_problem() from error
        if page is None:
            raise not_found_problem()
        body = _ranking_page_body(page)
        return _conditional_response(
            request=request,
            if_none_match=if_none_match,
            data_version=str(page.data_version),
            representation={
                "path": "ranking",
                "methodologyId": methodology_id,
                "methodologyVersion": methodology_version,
                "scopeType": scope_type,
                "universe": universe,
                "windowType": window_type,
                "windowSize": window_size,
                "bucket": bucket,
                "tradeDate": (None if trade_date is None else trade_date.isoformat()),
                "cursor": cursor,
                "limit": limit,
            },
            body=body,
        )


def _daily_response(
    *,
    request: Request,
    repository: MoneyFlowReadRepository | None,
    methodology_id: str,
    methodology_version: str,
    scope: MoneyFlowScope,
    bucket: str,
    start: date,
    end: date,
    expected_data_version: UUID | None,
    known_at: datetime | None,
    cursor: str | None,
    limit: int,
    if_none_match: str | None,
    unavailable_problem: Callable[[], Exception],
    not_found_problem: Callable[[], Exception],
    validation_problem: Callable[[str], Exception],
    conflict_problem: Callable[[str], Exception],
    snapshot_problem: Callable[[], Exception],
) -> Response:
    """执行三类日序列共有的校验、错误映射和条件响应。"""
    if repository is None:
        raise unavailable_problem()
    if known_at is not None and (known_at.tzinfo is None or known_at > datetime.now(UTC)):
        raise validation_problem("knownAt must include timezone and not be future")
    try:
        page = repository.list_daily(
            methodology_id=methodology_id,
            methodology_version=methodology_version,
            scope=scope,
            bucket=bucket,
            start=start,
            end=end,
            known_at=known_at,
            cursor=cursor,
            limit=limit,
        )
    except MoneyFlowCursorMismatch as error:
        raise conflict_problem(str(error)) from error
    except MoneyFlowIdentityBoundary as error:
        raise conflict_problem(str(error)) from error
    except ValueError as error:
        raise validation_problem(str(error)) from error
    except MoneyFlowReadUnavailable as error:
        raise unavailable_problem() from error
    if page is None:
        if expected_data_version is not None:
            raise snapshot_problem()
        raise not_found_problem()
    if expected_data_version is not None and page.data_version != expected_data_version:
        raise snapshot_problem()
    body = _daily_page_body(page)
    return _conditional_response(
        request=request,
        if_none_match=if_none_match,
        data_version=str(page.data_version),
        representation={
            "path": "daily",
            "methodologyId": methodology_id,
            "methodologyVersion": methodology_version,
            "scope": scope.scope_type.value,
            "bucket": bucket,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "dataVersion": (None if expected_data_version is None else str(expected_data_version)),
            "knownAt": (None if known_at is None else known_at.isoformat()),
            "cursor": cursor,
            "limit": limit,
        },
        body=body,
    )


def _methodology_page_body(
    page: MoneyFlowMethodologyPage,
) -> dict[str, object]:
    """投影内部方法学目录响应。"""
    return {
        "dataVersion": str(page.data_version),
        "publishedAt": _timestamp(page.published_at),
        "items": list(page.items),
        "nextCursor": page.next_cursor,
    }


def _daily_page_body(page: MoneyFlowDailyPage) -> dict[str, object]:
    """把端口日序列页展平为 0015 的强身份响应。"""
    return {
        "seriesId": str(page.series_id),
        **page.methodology,
        "scope": page.scope,
        "universe": page.universe,
        "bucket": page.bucket,
        "windowType": "daily_source",
        "windowSize": 1,
        "knownAtApplied": (
            None if page.known_at_applied is None else _timestamp(page.known_at_applied)
        ),
        "dataVersion": str(page.data_version),
        "publishedAt": _timestamp(page.published_at),
        "items": list(page.items),
        "nextCursor": page.next_cursor,
    }


def _ranking_page_body(page: MoneyFlowRankingPage) -> dict[str, object]:
    """把端口排行页展平为 0015 的不可变快照响应。"""
    return {
        **page.methodology,
        **page.snapshot,
        "dataVersion": str(page.data_version),
        "publishedAt": _timestamp(page.published_at),
        "items": list(page.items),
        "nextCursor": page.next_cursor,
    }


def _conditional_response(
    *,
    request: Request,
    if_none_match: str | None,
    data_version: str,
    representation: dict[str, object],
    body: dict[str, object],
) -> Response:
    """生成版本和完整查询绑定 ETag，相同表示返回标准 304。"""
    payload = json.dumps(
        {"dataVersion": data_version, **representation},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    etag = f'"{hashlib.sha256(payload).hexdigest()}"'
    headers = {
        "ETag": etag,
        "Cache-Control": _PRIVATE_REVALIDATE,
        "X-Data-Version": data_version,
    }
    if if_none_match == etag:
        return Response(status_code=304, headers=headers)
    return JSONResponse(
        content=body,
        status_code=200,
        headers=headers,
    )


def _timestamp(value: datetime) -> str:
    """把带时区时间规范化为 UTC ISO 8601。"""
    if value.tzinfo is None:
        raise MoneyFlowReadUnavailable("money-flow response timestamp lacks timezone")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


__all__ = ["register_money_flow_routes"]
