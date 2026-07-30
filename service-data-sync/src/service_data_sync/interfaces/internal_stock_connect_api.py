"""沪深港通中心内部五条 POST-only 查询路由。

路由只调用 publication-aware 只读仓储，成功响应的数据版本头与正文完全一致；请求失败使用
RFC 9457 兼容问题对象，不泄漏 SQL、官方文件路径、凭证或上游原文。
"""

from __future__ import annotations

import hmac
import re
import unicodedata
from collections.abc import Callable, Mapping
from datetime import date
from typing import Annotated, Any, NoReturn

from fastapi import Body, Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse

from service_data_sync.infrastructure.persistence.stock_connect_read_repository import (
    SqlAlchemyStockConnectReadRepository,
    StockConnectCursorInvalid,
    StockConnectCursorVersionMismatch,
    StockConnectExactDateNotPublished,
    StockConnectParentPublicationMismatch,
    StockConnectPublicationNotReady,
    StockConnectReadResult,
    StockConnectSecurityContextNotFound,
)
from service_data_sync.infrastructure.persistence.stock_connect_readiness_repository import (
    StockConnectReadinessNotObserved,
)

_CHANNELS = {
    "SH_NORTHBOUND",
    "SZ_NORTHBOUND",
    "SH_SOUTHBOUND",
    "SZ_SOUTHBOUND",
}
_RANKINGS = {"SOURCE_ACTIVE", "NET_BUY", "NET_SELL"}


class StockConnectProblem(Exception):
    """保存一项符合互联互通内部合同的安全问题详情。"""

    def __init__(self, *, status: int, code: str, detail: str) -> None:
        """构造稳定 HTTP 状态、机器码和无敏感信息说明。"""
        super().__init__(detail)
        self.status = status
        self.code = code
        self.detail = detail


def register_stock_connect_routes(
    app: FastAPI,
    *,
    repository: SqlAlchemyStockConnectReadRepository,
    read_bearer_token: str,
) -> None:
    """登记五条受最小只读服务身份保护的 POST 路由。"""

    @app.exception_handler(StockConnectProblem)
    async def render_stock_connect_problem(
        request: Request, error: StockConnectProblem
    ) -> JSONResponse:
        """把业务失败渲染为带请求实例和关联标识的 RFC 9457 问题。"""
        request_id = _problem_request_id(request)
        headers = {"X-Request-Id": request_id, "Cache-Control": "no-store"}
        if error.status == 503:
            headers["Retry-After"] = "5"
        return JSONResponse(
            status_code=error.status,
            media_type="application/problem+json",
            headers=headers,
            content={
                "type": f"https://quant-v2.local/problems/{error.code}",
                "title": "Stock Connect request failed",
                "status": error.status,
                "detail": error.detail,
                "instance": request.url.path,
                "code": error.code,
                "requestId": request_id,
            },
        )

    def require_stock_connect_bearer(
        authorization: Annotated[str | None, Header()] = None,
    ) -> None:
        """恒时校验内部只读 bearer，运维写凭据不自动获得查询权限。"""
        expected = f"Bearer {read_bearer_token}"
        if authorization is None or not hmac.compare_digest(authorization, expected):
            raise StockConnectProblem(
                status=401,
                code="AUTHENTICATION_FAILED",
                detail="Service credential is invalid",
            )

    def require_request_id(request: Request) -> None:
        """要求边缘服务提供安全关联标识，data-sync 不生成或替换调用链 ID。"""
        _request_id(request)

    @app.post(
        "/internal/v1/stock-connect/overview/query",
        dependencies=[Depends(require_request_id), Depends(require_stock_connect_bearer)],
    )
    def query_overview(
        request: Request,
        body: Annotated[dict[str, Any], Body()],
    ) -> JSONResponse:
        """查询所选通道最后共同 publication 或精确日期总览。"""
        _require_keys(body, {"date", "channels", "trendTradingDays"}, "overview")
        mode, exact_date = _date_selection(body["date"])
        channels = _channels(body["channels"])
        trend_days = _bounded_int(body["trendTradingDays"], 1, 250, "trendTradingDays")
        return _execute(
            request,
            lambda: repository.overview(
                mode=mode,
                exact_date=exact_date,
                channels=channels,
                trend_trading_days=trend_days,
            ),
        )

    @app.post(
        "/internal/v1/stock-connect/readiness/query",
        dependencies=[Depends(require_request_id), Depends(require_stock_connect_bearer)],
    )
    def query_readiness(
        request: Request,
        body: Annotated[dict[str, Any], Body()],
    ) -> JSONResponse:
        """查询所选通道由日历、预检、执行与 publication 共同证明的 readiness。"""
        _require_keys(body, {"date", "channels"}, "readiness")
        mode, exact_date = _date_selection(body["date"])
        channels = _channels(body["channels"])
        return _execute_readiness(
            request,
            lambda: repository.readiness(
                mode=mode,
                exact_date=exact_date,
                channels=channels,
            ),
        )

    @app.post(
        "/internal/v1/stock-connect/channels/query",
        dependencies=[Depends(require_request_id), Depends(require_stock_connect_bearer)],
    )
    def query_channel(
        request: Request,
        body: Annotated[dict[str, Any], Body()],
    ) -> JSONResponse:
        """查询单通道统计、最终状态、额度和趋势。"""
        _require_keys(body, {"date", "channel", "trendTradingDays"}, "channel")
        mode, exact_date = _date_selection(body["date"])
        channel = _channel(body["channel"])
        trend_days = _bounded_int(body["trendTradingDays"], 1, 250, "trendTradingDays")
        return _execute(
            request,
            lambda: repository.channel(
                mode=mode,
                exact_date=exact_date,
                channel=channel,
                trend_trading_days=trend_days,
            ),
        )

    @app.post(
        "/internal/v1/stock-connect/active-securities/query",
        dependencies=[Depends(require_request_id), Depends(require_stock_connect_bearer)],
    )
    def query_active_securities(
        request: Request,
        body: Annotated[dict[str, Any], Body()],
    ) -> JSONResponse:
        """查询官方来源活跃榜或该榜内真正可派生的净额排行。"""
        _require_keys(
            body,
            {
                "date",
                "channel",
                "ranking",
                "cursor",
                "limit",
                "parentPublicationDataVersion",
            },
            "active securities",
        )
        mode, exact_date = _date_selection(body["date"])
        channel = _channel(body["channel"])
        ranking = body["ranking"]
        if not isinstance(ranking, str) or ranking not in _RANKINGS:
            _validation("ranking is invalid")
        cursor_value = body["cursor"]
        if cursor_value is not None and (
            not isinstance(cursor_value, str) or not 1 <= len(cursor_value) <= 1024
        ):
            _validation("cursor is invalid")
        limit = _bounded_int(body["limit"], 1, 100, "limit")
        parent_data_version = _data_version_text(
            body["parentPublicationDataVersion"],
            "parentPublicationDataVersion",
        )
        return _execute(
            request,
            lambda: repository.active_securities(
                mode=mode,
                exact_date=exact_date,
                channel=channel,
                ranking=ranking,
                cursor=cursor_value,
                limit=limit,
                parent_publication_data_version=parent_data_version,
            ),
        )

    @app.post(
        "/internal/v1/stock-connect/securities/context/query",
        dependencies=[Depends(require_request_id), Depends(require_stock_connect_bearer)],
    )
    def query_security_context(
        request: Request,
        body: Annotated[dict[str, Any], Body()],
    ) -> JSONResponse:
        """查询稳定证券引用在来源活跃榜中的版本化历史表现。"""
        _require_keys(
            body,
            {"instrumentEntityRef", "date", "channel", "historyTradingDays"},
            "security context",
        )
        instrument_ref = body["instrumentEntityRef"]
        if not isinstance(instrument_ref, str) or not 1 <= len(instrument_ref) <= 160:
            _validation("instrumentEntityRef is invalid")
        mode, exact_date = _date_selection(body["date"])
        channel_value = body["channel"]
        channel = None if channel_value is None else _channel(channel_value)
        history_days = _bounded_int(
            body["historyTradingDays"],
            1,
            250,
            "historyTradingDays",
        )
        return _execute(
            request,
            lambda: repository.security_context(
                instrument_entity_ref=instrument_ref,
                mode=mode,
                exact_date=exact_date,
                channel=channel,
                history_trading_days=history_days,
            ),
        )


def _execute(
    request: Request,
    operation: Callable[[], StockConnectReadResult],
) -> JSONResponse:
    """统一执行只读查询并保证版本头与正文 publication 完全一致。"""
    try:
        result = operation()
    except StockConnectExactDateNotPublished as error:
        raise StockConnectProblem(
            status=409,
            code="EXACT_DATE_NOT_PUBLISHED",
            detail=str(error),
        ) from error
    except StockConnectPublicationNotReady as error:
        raise StockConnectProblem(
            status=409,
            code="PUBLICATION_NOT_READY",
            detail=str(error),
        ) from error
    except StockConnectParentPublicationMismatch as error:
        raise StockConnectProblem(
            status=409,
            code="PARENT_PUBLICATION_MISMATCH",
            detail=str(error),
        ) from error
    except StockConnectCursorVersionMismatch as error:
        raise StockConnectProblem(
            status=409,
            code="CURSOR_VERSION_MISMATCH",
            detail=str(error),
        ) from error
    except StockConnectCursorInvalid as error:
        raise StockConnectProblem(
            status=400,
            code="VALIDATION_FAILED",
            detail=str(error),
        ) from error
    except StockConnectSecurityContextNotFound as error:
        raise StockConnectProblem(
            status=404,
            code="SECURITY_CONTEXT_NOT_FOUND",
            detail=str(error),
        ) from error
    except Exception as error:
        raise StockConnectProblem(
            status=503,
            code="INTERNAL_DEPENDENCY_FAILED",
            detail="Published stock-connect data cannot be read",
        ) from error
    publication = result.body.get("publication")
    if not isinstance(publication, Mapping) or publication.get("dataVersion") != (
        result.data_version
    ):
        raise StockConnectProblem(
            status=503,
            code="INTERNAL_DEPENDENCY_FAILED",
            detail="Published stock-connect version is inconsistent",
        )
    request_id = _request_id(request)
    return JSONResponse(
        status_code=200,
        content=result.body,
        headers={
            "X-Request-Id": request_id,
            "X-Data-Version": result.data_version,
            "Cache-Control": "private, max-age=0, must-revalidate",
        },
    )


def _execute_readiness(
    request: Request,
    operation: Callable[[], StockConnectReadResult],
) -> JSONResponse:
    """执行独立 readiness 查询，并校验正文版本与 `X-Data-Version` 完全一致。"""
    try:
        result = operation()
    except StockConnectReadinessNotObserved as error:
        raise StockConnectProblem(
            status=409,
            code="READINESS_NOT_OBSERVED",
            detail=str(error),
        ) from error
    except Exception as error:
        raise StockConnectProblem(
            status=503,
            code="INTERNAL_DEPENDENCY_FAILED",
            detail="Stock-connect readiness evidence cannot be read",
        ) from error
    if result.body.get("dataVersion") != result.data_version:
        raise StockConnectProblem(
            status=503,
            code="INTERNAL_DEPENDENCY_FAILED",
            detail="Stock-connect readiness version is inconsistent",
        )
    request_id = _request_id(request)
    return JSONResponse(
        status_code=200,
        content=result.body,
        headers={
            "X-Request-Id": request_id,
            "X-Data-Version": result.data_version,
            "Cache-Control": "private, max-age=0, must-revalidate",
        },
    )


def _date_selection(value: object) -> tuple[str, date | None]:
    """校验 latest/exact 互斥日期对象，禁止把抓取时间或任意文本当交易日。"""
    if not isinstance(value, dict) or set(value) != {"mode", "exactDate"}:
        _validation("date selection is invalid")
    mode = value.get("mode")
    exact = value.get("exactDate")
    if mode == "LATEST" and exact is None:
        return "LATEST", None
    if mode == "EXACT" and isinstance(exact, str):
        try:
            return "EXACT", date.fromisoformat(exact)
        except ValueError:
            pass
    _validation("date selection is invalid")


def _channels(value: object) -> tuple[str, ...]:
    """读取一至四个唯一通道并按稳定顺序返回。"""
    if not isinstance(value, list) or not 1 <= len(value) <= 4:
        _validation("channels are invalid")
    channels = tuple(_channel(item) for item in value)
    if len(set(channels)) != len(channels):
        _validation("channels must be unique")
    return tuple(sorted(channels))


def _channel(value: object) -> str:
    """读取合同固定通道枚举。"""
    if not isinstance(value, str) or value not in _CHANNELS:
        _validation("channel is invalid")
    return value


def _bounded_int(value: object, minimum: int, maximum: int, field: str) -> int:
    """读取排除 bool 的有界整数。"""
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        _validation(f"{field} is invalid")
    return value


def _data_version_text(value: object, field: str) -> str:
    """读取可演进的有界 dataVersion，并拒绝所有 Unicode 控制或格式字符。"""
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 160
        or any(unicodedata.category(character).startswith("C") for character in value)
    ):
        _validation(f"{field} is invalid")
    return value


def _require_keys(value: object, expected: set[str], location: str) -> None:
    """严格执行 additionalProperties=false 和必填字段集合。"""
    if not isinstance(value, dict) or set(value) != expected:
        _validation(f"{location} request fields are invalid")


def _validation(detail: str) -> NoReturn:
    """抛出合同统一参数错误。"""
    raise StockConnectProblem(status=400, code="VALIDATION_FAILED", detail=detail)


def _request_id(request: Request) -> str:
    """精确回显安全请求标识；缺失或非法时拒绝，不在同步服务生成替代值。"""
    supplied = request.headers.get("X-Request-Id")
    if supplied is not None and re.fullmatch(r"[A-Za-z0-9._:/-]{1,128}", supplied):
        return supplied
    raise StockConnectProblem(
        status=400,
        code="VALIDATION_FAILED",
        detail="X-Request-Id is required and contains invalid characters",
    )


def _problem_request_id(request: Request) -> str:
    """问题响应回显合法链路 ID；非法输入只返回固定哨兵而不伪造新链路。"""
    try:
        return _request_id(request)
    except StockConnectProblem:
        return "invalid-request-id"
