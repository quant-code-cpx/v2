"""供受信任服务读取已发布板块行情与证券主数据的内部 HTTP 应用。

板块目录、三种原生周期行情和资金流依赖均通过各自 publication 投影；请求被认证、分页
范围被签名，且不会泄漏供应商字段、数据库键、`PENDING` 身份或未通过质量门的版本。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, Header, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response

from service_data_sync.application.ports.equity_master_read import EquityMasterReadRepository
from service_data_sync.application.ports.financial_read import FinancialReadRepository
from service_data_sync.application.ports.market_data import EquityMarketDataRepository
from service_data_sync.application.ports.market_data_access import (
    MarketDataAccessRepository,
)
from service_data_sync.application.ports.money_flow import MoneyFlowReadRepository
from service_data_sync.application.ports.sector_eod import SectorEodRepository
from service_data_sync.application.ports.sector_market_data import (
    DatasetPublication,
    SectorMarketDataRepository,
    StoredSector,
)
from service_data_sync.application.ports.sector_membership import SectorMembershipRepository
from service_data_sync.application.ports.sw_sector import SwSectorRepository
from service_data_sync.bootstrap.container import ServiceContainer, build_container
from service_data_sync.bootstrap.settings import Settings, load_settings
from service_data_sync.domain.sector import SectorBar, SectorIdentifier, SectorPeriod, SectorScheme
from service_data_sync.infrastructure.persistence.equity_market_data_repository import (
    SqlAlchemyEquityMarketDataRepository,
)
from service_data_sync.infrastructure.persistence.equity_master_read_repository import (
    SqlAlchemyEquityMasterReadRepository,
)
from service_data_sync.infrastructure.persistence.financial_read_repository import (
    SqlAlchemyFinancialReadRepository,
)
from service_data_sync.infrastructure.persistence.market_data_access_repository import (
    CatalogMarketDataAccessRepository,
)
from service_data_sync.infrastructure.persistence.money_flow_read_repository import (
    SqlAlchemyMoneyFlowReadRepository,
)
from service_data_sync.infrastructure.persistence.sector_eod_repository import (
    SqlAlchemySectorEodRepository,
)
from service_data_sync.infrastructure.persistence.sector_market_data_repository import (
    SqlAlchemySectorMarketDataRepository,
)
from service_data_sync.infrastructure.persistence.sector_membership_repository import (
    SqlAlchemySectorMembershipRepository,
)
from service_data_sync.infrastructure.persistence.sqlalchemy_market_data_access_repository import (
    SqlAlchemyMarketDataAccessRepository,
)
from service_data_sync.infrastructure.persistence.sw_sector_repository import (
    SqlAlchemySwSectorRepository,
)

_CATALOG_DATASET = "sector.catalog.raw"
_PRIVATE_REVALIDATE = "private, max-age=0, must-revalidate"


class InternalProblem(Exception):
    """保存可安全回传给受信任调用方的内部 API 问题详情。"""

    def __init__(self, *, status: int, code: str, detail: str) -> None:
        """以稳定状态码、机器码和不含内部细节的说明构造问题。"""
        super().__init__(detail)
        self.status = status
        self.code = code
        self.detail = detail


def create_app(
    *,
    settings: Settings | None = None,
    repository: SectorMarketDataRepository | None = None,
    eod_repository: SectorEodRepository | None = None,
    equity_repository: EquityMasterReadRepository | None = None,
    equity_market_repository: EquityMarketDataRepository | None = None,
    membership_repository: SectorMembershipRepository | None = None,
    financial_repository: FinancialReadRepository | None = None,
    sw_repository: SwSectorRepository | None = None,
    money_flow_repository: MoneyFlowReadRepository | None = None,
    market_data_repository: MarketDataAccessRepository | None = None,
) -> FastAPI:
    """构造共享只读内部应用；运行时独占 `canonical` 数据读取与服务凭据校验。"""
    resolved_settings = settings or load_settings()
    container: ServiceContainer | None = None
    if repository is None:
        container = build_container(resolved_settings)
        repository = SqlAlchemySectorMarketDataRepository(container.database)
        if eod_repository is None:
            eod_repository = SqlAlchemySectorEodRepository(container.database)
        if equity_repository is None:
            equity_repository = SqlAlchemyEquityMasterReadRepository(container.database)
        if equity_market_repository is None:
            equity_market_repository = SqlAlchemyEquityMarketDataRepository(container.database)
        if membership_repository is None:
            membership_repository = SqlAlchemySectorMembershipRepository(container.database)
        if financial_repository is None:
            financial_repository = SqlAlchemyFinancialReadRepository(container.database)
        if sw_repository is None:
            sw_repository = SqlAlchemySwSectorRepository(container.database)
        if money_flow_repository is None:
            money_flow_repository = SqlAlchemyMoneyFlowReadRepository(
                container.database,
                cursor_secret=resolved_settings.internal_api_bearer_token.get_secret_value().encode(),
            )
    credential = resolved_settings.internal_api_bearer_token.get_secret_value()
    # 正常运行使用 release-aware typed reader；注入假 repository 的测试仍可无数据库运行。
    # 目录可发现但没有合格发布时必须 503，禁止回退到 raw 或研究态表。
    resolved_market_data_repository = market_data_repository or (
        SqlAlchemyMarketDataAccessRepository(container.database)
        if container is not None
        else CatalogMarketDataAccessRepository()
    )
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.exception_handler(InternalProblem)
    async def render_internal_problem(request: Request, error: InternalProblem) -> JSONResponse:
        """将预期业务失败投影为不泄漏配置、SQL 或供应商细节的问题响应。"""
        request_id = _request_id(request)
        headers = {"X-Request-Id": request_id, "Cache-Control": "no-store"}
        # 依赖故障必须提供有界退避提示，避免内部调用方立即形成重试风暴。
        if error.status == 503:
            headers["Retry-After"] = "5"
        return JSONResponse(
            status_code=error.status,
            content=_problem_payload(
                status=error.status,
                code=error.code,
                detail=error.detail,
                request_id=request_id,
            ),
            media_type="application/problem+json",
            headers=headers,
        )

    @app.exception_handler(RequestValidationError)
    async def render_validation_problem(
        request: Request, _error: RequestValidationError
    ) -> JSONResponse:
        """将框架参数校验统一映射为 v1 合同约定的 400 问题响应。"""
        request_id = _request_id(request)
        return JSONResponse(
            status_code=400,
            content=_problem_payload(
                status=400,
                code="validation-error",
                detail="Request parameters are invalid",
                request_id=request_id,
            ),
            media_type="application/problem+json",
            headers={"X-Request-Id": request_id, "Cache-Control": "no-store"},
        )

    def require_service_bearer(
        authorization: Annotated[str | None, Header()] = None,
    ) -> None:
        """仅接受完全匹配的内部 Bearer 凭据，避免匿名或前缀匹配绕过。"""
        expected = f"Bearer {credential}"
        if authorization is None or not hmac.compare_digest(authorization, expected):
            raise InternalProblem(
                status=401, code="unauthorized", detail="Service credential is invalid"
            )

    @app.get("/health", include_in_schema=False)
    def health() -> dict[str, str]:
        """返回进程存活状态；该探针仅位于内部网络且不读取业务数据。"""
        return {"status": "ok"}

    def financial_unavailable_problem() -> InternalProblem:
        """返回未发布财务数据的脱敏依赖失败，绝不把 `research` 或隔离数据降级暴露。"""
        return InternalProblem(
            status=503,
            code="financial-publication-unavailable",
            detail="Financial data is not published",
        )

    def financial_validation_problem(detail: str) -> InternalProblem:
        """将财务暗发布的业务范围校验统一映射为 `v1` 参数问题。"""
        return InternalProblem(status=400, code="validation-error", detail=detail)

    def financial_report_not_found_problem() -> InternalProblem:
        """返回已发布视图中不可见报表的脱敏缺失问题，不暴露 revision 或质量细节。"""
        return InternalProblem(
            status=404,
            code="financial-report-not-found",
            detail="Financial report is not found",
        )

    def financial_cursor_problem() -> InternalProblem:
        """将财务游标与当前请求不一致统一映射为不可续页问题。"""
        return InternalProblem(
            status=409,
            code="cursor-mismatch",
            detail="Financial cursor does not match request",
        )

    def money_flow_unavailable_problem() -> InternalProblem:
        """返回没有可消费技术发布的资金流依赖失败。"""
        return InternalProblem(
            status=503,
            code="money-flow-publication-unavailable",
            detail="Money-flow data is not published",
        )

    def money_flow_not_found_problem() -> InternalProblem:
        """返回当前发布中不存在的资金流资源。"""
        return InternalProblem(
            status=404,
            code="money-flow-not-found",
            detail="Money-flow resource is not found",
        )

    def money_flow_validation_problem(detail: str) -> InternalProblem:
        """把资金流查询范围错误映射为稳定参数问题。"""
        return InternalProblem(status=400, code="validation-error", detail=detail)

    def money_flow_conflict_problem(detail: str) -> InternalProblem:
        """把游标不匹配或证券身份边界映射为稳定冲突。"""
        return InternalProblem(status=409, code="query-conflict", detail=detail)

    from service_data_sync.interfaces.internal_money_flow_api import (
        register_money_flow_routes,
    )

    register_money_flow_routes(
        app,
        require_service_bearer=require_service_bearer,
        unavailable_problem=money_flow_unavailable_problem,
        not_found_problem=money_flow_not_found_problem,
        validation_problem=money_flow_validation_problem,
        conflict_problem=money_flow_conflict_problem,
        repository=money_flow_repository,
    )

    # 新市场数据合同与旧 GET 路由并行；尚无 typed reader 的数据集保持 fail-closed。
    from service_data_sync.interfaces.internal_market_data_api import register_market_data_routes

    register_market_data_routes(
        app,
        repository=resolved_market_data_repository,
        require_service_bearer=require_service_bearer,
        cursor_secret=credential.encode(),
    )

    # 财务路由只在精确 `publication` 已存在时读取，
    # 避免消费者误读 `research` 或半成品 `revision`。
    from service_data_sync.interfaces.internal_financial_api import register_financial_routes

    register_financial_routes(
        app,
        require_service_bearer=require_service_bearer,
        unavailable_problem=financial_unavailable_problem,
        not_found_problem=financial_report_not_found_problem,
        validation_problem=financial_validation_problem,
        cursor_problem=financial_cursor_problem,
        # 内部认证凭据只作为游标完整性密钥，游标本身不承载认证能力。
        cursor_secret=credential.encode(),
        repository=financial_repository,
    )

    if sw_repository is not None:
        from service_data_sync.interfaces.internal_sw_sector_api import (
            register_sw_sector_routes,
        )

        register_sw_sector_routes(
            app,
            repository=sw_repository,
            require_service_bearer=require_service_bearer,
            cursor_secret=credential.encode(),
        )

    if equity_repository is not None:
        # 延迟导入避免证券路由模块在应用工厂完成定义前反向加载本模块。
        from service_data_sync.interfaces.internal_equity_api import register_equity_routes

        register_equity_routes(
            app,
            repository=equity_repository,
            require_service_bearer=require_service_bearer,
            cursor_secret=credential.encode(),
        )

    if equity_market_repository is not None:
        # 行情、因子、事件和概况独立于主数据双时态路由注册，但共用服务认证。
        from service_data_sync.interfaces.internal_equity_market_api import (
            register_equity_market_routes,
        )

        register_equity_market_routes(
            app,
            repository=equity_market_repository,
            require_service_bearer=require_service_bearer,
            cursor_secret=credential.encode(),
        )

    if membership_repository is not None:
        # 成分路由独立注册，避免原板块目录/K 线端口依赖成分实现细节。
        from service_data_sync.interfaces.internal_sector_membership_api import (
            register_sector_membership_routes,
        )

        register_sector_membership_routes(
            app,
            repository=membership_repository,
            require_service_bearer=require_service_bearer,
        )

    if eod_repository is not None:
        # EOD 静态路径必须先注册，避免被通用 `/{scheme}/{sectorCode}` 路由抢占。
        from service_data_sync.interfaces.internal_sector_eod_api import register_sector_eod_routes

        register_sector_eod_routes(
            app,
            repository=eod_repository,
            require_service_bearer=require_service_bearer,
            cursor_secret=credential.encode(),
        )

    @app.get("/internal/v1/sectors", dependencies=[Depends(require_service_bearer)])
    def list_sectors(
        request: Request,
        scheme: Annotated[str, Query(min_length=1)],
        query: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
        cursor: Annotated[str | None, Query(max_length=1024)] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 100,
        if_none_match: Annotated[str | None, Header()] = None,
    ) -> Response:
        """分页读取一个分类体系的 ACTIVE 目录，并使游标绑定筛选条件。"""
        sector_scheme = _scheme_or_problem(scheme)
        normalized_query = _normalized_query(query)
        after_code, after_sector_id = _catalog_cursor_or_problem(
            cursor, scheme=sector_scheme, query=normalized_query
        )
        publication = _catalog_publication_or_problem(repository, sector_scheme)
        rows = repository.list_active_sectors(
            scheme=sector_scheme,
            query=normalized_query,
            after_code=after_code,
            after_sector_id=after_sector_id,
            limit=limit + 1,
        )
        visible_rows = tuple(rows[:limit])
        next_cursor = (
            _encode_catalog_cursor(
                scheme=sector_scheme, query=normalized_query, sector=visible_rows[-1]
            )
            if len(rows) > limit and visible_rows
            else None
        )
        body = {
            "items": [_sector_resource(row, publication) for row in visible_rows],
            "nextCursor": next_cursor,
            "dataVersion": str(publication.data_version),
            "publishedAt": _timestamp(publication.published_at),
        }
        return _conditional_json_response(
            request=request,
            if_none_match=if_none_match,
            etag=_catalog_etag(publication, normalized_query, cursor, limit),
            body=body,
        )

    @app.get(
        "/internal/v1/sectors/{scheme}/{sector_code}",
        dependencies=[Depends(require_service_bearer)],
    )
    def get_sector(
        request: Request,
        scheme: str,
        sector_code: Annotated[str, Path(min_length=1, max_length=64)],
        if_none_match: Annotated[str | None, Header()] = None,
    ) -> Response:
        """读取一个已发布板块身份，不把 PENDING 或供应商行暴露给调用方。"""
        sector_scheme = _scheme_or_problem(scheme)
        identifier = _identifier_or_problem(sector_scheme, sector_code)
        publication = _catalog_publication_or_problem(repository, sector_scheme)
        sector = _published_sector_or_problem(repository, identifier)
        body = _sector_resource(sector, publication)
        return _conditional_json_response(
            request=request,
            if_none_match=if_none_match,
            etag=_resource_etag("sector", publication.data_version, identifier.qualified_key),
            body=body,
        )

    @app.get(
        "/internal/v1/sectors/{scheme}/{sector_code}/bars",
        dependencies=[Depends(require_service_bearer)],
    )
    def list_sector_bars(
        request: Request,
        scheme: str,
        sector_code: Annotated[str, Path(min_length=1, max_length=64)],
        period: Annotated[str, Query(min_length=1)],
        start: date,
        end: date,
        cursor: Annotated[str | None, Query(max_length=1024)] = None,
        limit: Annotated[int, Query(ge=1, le=1000)] = 1000,
        if_none_match: Annotated[str | None, Header()] = None,
    ) -> Response:
        """读取一段直接上游日、周或月 K 线，游标失效时拒绝混合发布快照。"""
        if start > end:
            raise InternalProblem(
                status=400, code="validation-error", detail="start must not be after end"
            )
        sector_scheme = _scheme_or_problem(scheme)
        identifier = _identifier_or_problem(sector_scheme, sector_code)
        sector_period = _period_or_problem(period)
        sector = _published_sector_or_problem(repository, identifier)
        publication = repository.get_current_publication(
            dataset=sector_period.capability, partition_key=identifier.qualified_key
        )
        if publication is None:
            raise InternalProblem(
                status=404, code="not-found", detail="Sector bars are not published"
            )
        after_period_end = _bar_cursor_or_problem(
            cursor,
            scheme=sector_scheme,
            code=identifier.code,
            period=sector_period,
            start=start,
            end=end,
            publication=publication,
        )
        rows = repository.list_bars(
            sector_id=sector.sector_id, period=sector_period, start=start, end=end
        )
        visible_rows = tuple(
            row for row in rows if after_period_end is None or row[0].period_end > after_period_end
        )
        page_rows = visible_rows[:limit]
        next_cursor = (
            _encode_bar_cursor(
                scheme=sector_scheme,
                code=identifier.code,
                period=sector_period,
                start=start,
                end=end,
                publication=publication,
                period_end=page_rows[-1][0].period_end,
            )
            if len(visible_rows) > limit and page_rows
            else None
        )
        body = {
            "sector": _sector_resource(
                sector, _catalog_publication_or_problem(repository, sector_scheme)
            ),
            "period": sector_period.value,
            "dataVersion": str(publication.data_version),
            "publishedAt": _timestamp(publication.published_at),
            "items": [_bar_resource(row[0], is_final=row[2]) for row in page_rows],
            "nextCursor": next_cursor,
        }
        return _conditional_json_response(
            request=request,
            if_none_match=if_none_match,
            etag=_resource_etag("bars", publication.data_version, identifier.qualified_key, period),
            body=body,
        )

    if container is not None:

        @app.on_event("shutdown")
        def close_container() -> None:
            """在 HTTP 进程退出时关闭组合根持有的数据库、Redis 与对象存储资源。"""
            container.close()

    return app


def _scheme_or_problem(value: str) -> SectorScheme:
    """解析封闭分类体系，避免任意字符串成为数据库查询条件。"""
    try:
        return SectorScheme(value)
    except ValueError as error:
        raise InternalProblem(
            status=400, code="validation-error", detail="scheme is invalid"
        ) from error


def _period_or_problem(value: str) -> SectorPeriod:
    """解析三个允许的物理周期，拒绝分钟或日线派生请求。"""
    try:
        return SectorPeriod(value)
    except ValueError as error:
        raise InternalProblem(
            status=400, code="validation-error", detail="period is invalid"
        ) from error


def _identifier_or_problem(scheme: SectorScheme, code: str) -> SectorIdentifier:
    """构造稳定板块身份，并将格式失败标准化为参数问题。"""
    try:
        return SectorIdentifier(scheme=scheme, code=code)
    except ValueError as error:
        raise InternalProblem(
            status=400, code="validation-error", detail="sectorCode is invalid"
        ) from error


def _normalized_query(value: str | None) -> str | None:
    """保留前缀查询的用户语义，同时拒绝全空白条件。"""
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        raise InternalProblem(status=400, code="validation-error", detail="query is invalid")
    return normalized


def _catalog_publication_or_problem(
    repository: SectorMarketDataRepository, scheme: SectorScheme
) -> DatasetPublication:
    """要求目录已经发布，避免把 PENDING 或无名称身份误当成可用市场数据。"""
    publication = repository.get_current_publication(
        dataset=_CATALOG_DATASET, partition_key=scheme.value
    )
    if publication is None:
        raise InternalProblem(
            status=503,
            code="dependency-unavailable",
            detail="Sector catalog is not published",
        )
    return publication


def _published_sector_or_problem(
    repository: SectorMarketDataRepository, identifier: SectorIdentifier
) -> StoredSector:
    """仅返回目录已确认的 ACTIVE 身份，防止行情占位记录逃逸到 API。"""
    sector = repository.get_sector_by_identifier(identifier)
    if sector is None or sector.status != "ACTIVE" or sector.name is None:
        raise InternalProblem(status=404, code="not-found", detail="Sector is not found")
    return sector


def _sector_resource(sector: StoredSector, publication: DatasetPublication) -> dict[str, str]:
    """投影公开板块身份，不泄漏数据库主键或供应商字段。"""
    assert sector.name is not None
    return {
        "sectorId": str(sector.sector_id),
        "scheme": sector.identifier.scheme.value,
        "code": sector.identifier.code,
        "name": sector.name,
        "dataVersion": str(publication.data_version),
        "publishedAt": _timestamp(publication.published_at),
    }


def _bar_resource(bar: SectorBar, *, is_final: bool) -> dict[str, str | bool | None]:
    """投影精确小数字符串和来源原生单位，不进行跨板块成交量换算。"""
    return {
        "periodEnd": bar.period_end.isoformat(),
        "open": str(bar.open_price),
        "high": str(bar.high_price),
        "low": str(bar.low_price),
        "close": str(bar.close_price),
        "volumeValue": str(bar.volume_value),
        "volumeUnit": bar.volume_unit,
        "amountCny": str(bar.amount_cny),
        "amplitudePercent": _decimal_text(bar.amplitude_percent),
        "changePercent": _decimal_text(bar.change_percent),
        "changeAmount": _decimal_text(bar.change_amount),
        "turnoverPercent": _decimal_text(bar.turnover_percent),
        "isFinal": is_final,
    }


def _decimal_text(value: Decimal | None) -> str | None:
    """将可空精确小数渲染为合同约定的 JSON 字符串。"""
    return None if value is None else str(value)


def _timestamp(value: datetime) -> str:
    """将带时区发布时间标准化为 RFC 3339 UTC 文本。"""
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _catalog_cursor_or_problem(
    cursor: str | None, *, scheme: SectorScheme, query: str | None
) -> tuple[str | None, UUID | None]:
    """解码并验证目录游标，使其不能跨分类体系或搜索条件复用。"""
    if cursor is None:
        return None, None
    decoded = _decode_cursor(cursor)
    if decoded.get("s") != scheme.value or decoded.get("q") != query:
        raise InternalProblem(status=400, code="validation-error", detail="cursor is invalid")
    code = decoded.get("c")
    sector_id = decoded.get("i")
    if not isinstance(code, str) or not isinstance(sector_id, str):
        raise InternalProblem(status=400, code="validation-error", detail="cursor is invalid")
    try:
        return code, UUID(sector_id)
    except ValueError as error:
        raise InternalProblem(
            status=400, code="validation-error", detail="cursor is invalid"
        ) from error


def _encode_catalog_cursor(*, scheme: SectorScheme, query: str | None, sector: StoredSector) -> str:
    """编码目录下一页起点，不包含任何数据库内部数值主键。"""
    return _encode_cursor(
        {"s": scheme.value, "q": query, "c": sector.identifier.code, "i": str(sector.sector_id)}
    )


def _bar_cursor_or_problem(
    cursor: str | None,
    *,
    scheme: SectorScheme,
    code: str,
    period: SectorPeriod,
    start: date,
    end: date,
    publication: DatasetPublication,
) -> date | None:
    """验证 K 线游标的所有查询维度和发布版本，拒绝跨快照续页。"""
    if cursor is None:
        return None
    decoded = _decode_cursor(cursor)
    expected = {
        "s": scheme.value,
        "c": code,
        "p": period.value,
        "a": start.isoformat(),
        "b": end.isoformat(),
    }
    if any(decoded.get(key) != value for key, value in expected.items()):
        raise InternalProblem(status=400, code="validation-error", detail="cursor is invalid")
    if decoded.get("v") != str(publication.data_version):
        raise InternalProblem(
            status=409, code="snapshot-expired", detail="Published snapshot changed"
        )
    period_end = decoded.get("d")
    if not isinstance(period_end, str):
        raise InternalProblem(status=400, code="validation-error", detail="cursor is invalid")
    try:
        return date.fromisoformat(period_end)
    except ValueError as error:
        raise InternalProblem(
            status=400, code="validation-error", detail="cursor is invalid"
        ) from error


def _encode_bar_cursor(
    *,
    scheme: SectorScheme,
    code: str,
    period: SectorPeriod,
    start: date,
    end: date,
    publication: DatasetPublication,
    period_end: date,
) -> str:
    """编码绑定数据版本的 K 线下一页起点，以保证分页读到同一发布快照。"""
    return _encode_cursor(
        {
            "s": scheme.value,
            "c": code,
            "p": period.value,
            "a": start.isoformat(),
            "b": end.isoformat(),
            "v": str(publication.data_version),
            "d": period_end.isoformat(),
        }
    )


def _encode_cursor(value: dict[str, str | None]) -> str:
    """将不透明游标渲染为 URL 安全 base64 JSON，避免客户端依赖内部格式。"""
    payload = json.dumps(value, separators=(",", ":"), sort_keys=True).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _decode_cursor(value: str) -> dict[str, Any]:
    """解码游标 JSON，并把格式错误转换为稳定参数问题。"""
    try:
        padded = value + "=" * (-len(value) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(padded.encode()))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise InternalProblem(
            status=400, code="validation-error", detail="cursor is invalid"
        ) from error
    if not isinstance(decoded, dict):
        raise InternalProblem(status=400, code="validation-error", detail="cursor is invalid")
    return decoded


def _conditional_json_response(
    *,
    request: Request,
    if_none_match: str | None,
    etag: str,
    body: dict[str, Any],
) -> Response:
    """在内容匹配时返回 304，否则返回可私有复验的 JSON 表示。"""
    request_id = _request_id(request)
    headers = {"ETag": etag, "Cache-Control": _PRIVATE_REVALIDATE, "X-Request-Id": request_id}
    if if_none_match is not None and if_none_match == etag:
        return Response(status_code=304, headers=headers)
    return JSONResponse(content=body, headers=headers)


def _catalog_etag(
    publication: DatasetPublication, query: str | None, cursor: str | None, limit: int
) -> str:
    """为目录页面的筛选和位置生成表示级 ETag，避免跨页错误复用。"""
    discriminator = hashlib.sha256(
        json.dumps({"q": query, "c": cursor, "l": limit}, sort_keys=True).encode()
    ).hexdigest()[:16]
    return _resource_etag("catalog", publication.data_version, discriminator)


def _resource_etag(kind: str, data_version: UUID, *parts: str) -> str:
    """构造不泄漏原始数据内容的强校验器，并绑定相应发布版本。"""
    suffix = hashlib.sha256("\u0000".join(parts).encode()).hexdigest()[:16]
    return f'"sector-{kind}-{data_version}-{suffix}"'


def _request_id(request: Request) -> str:
    """复用受限长度请求标识，或为内部问题和响应生成新的可关联标识。"""
    supplied = request.headers.get("X-Request-Id")
    return supplied if supplied is not None and 1 <= len(supplied) <= 128 else str(uuid4())


def _problem_payload(
    *, status: int, code: str, detail: str, request_id: str
) -> dict[str, str | int]:
    """生成合同约定的最小问题对象，不包含异常、配置或 SQL 信息。"""
    return {
        "type": f"https://quant-v2.local/problems/{code}",
        "title": "Request failed",
        "status": status,
        "code": code,
        "detail": detail,
        "requestId": request_id,
    }
