"""财务与估值内部读取路由；所有响应仅选择生产 `publication` 的双时态视图。

报表、指标、估值和平台派生结果各自按冻结版本读取，并使用带签名的续页游标与条件请求；
research、raw、quarantine、内部数据库键和半成品 revision 永远不进入服务间契约。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, Header, Path, Query, Request
from fastapi.responses import JSONResponse, Response

from service_data_sync.application.ports.financial_read import (
    FinancialCapability,
    FinancialPublicationSnapshot,
    FinancialReadRepository,
    FinancialReadUnavailable,
    PublishedFinancialMetric,
    PublishedFinancialReport,
    PublishedFinancialReportDetail,
    PublishedFinancialStatementFact,
    PublishedValuationObservation,
)
from service_data_sync.domain.equity import Exchange

# 限定当前财务路径可接受的交易所代码。
ExchangeCode = Literal["SSE", "SZSE", "BSE"]
# 限定报表读取可选择的三大财务报表类型。
StatementType = Literal["BALANCE_SHEET", "INCOME_STATEMENT", "CASH_FLOW_STATEMENT"]
# 限定报告期的业务口径，避免接口层接收未治理的字符串。
PeriodBasis = Literal["POINT_IN_TIME", "YEAR_TO_DATE", "SINGLE_QUARTER", "TTM"]
# 限定报表合并范围的受控值。
StatementScope = Literal["CONSOLIDATED", "PARENT", "UNKNOWN"]
# 限定供应商报告值与平台派生值的来源类别。
MetricOrigin = Literal["PROVIDER_REPORTED", "PLATFORM_DERIVED"]
# 限定当前估值契约可读取的指标代码。
ValuationMetric = Literal["market_cap", "pe_ttm", "pe_static", "pb", "pcf"]
_PRIVATE_REVALIDATE = "private, max-age=0, must-revalidate"


def register_financial_routes(
    app: FastAPI,
    *,
    require_service_bearer: Callable[..., None],
    unavailable_problem: Callable[[], Exception],
    not_found_problem: Callable[[], Exception],
    validation_problem: Callable[[str], Exception],
    cursor_problem: Callable[[], Exception],
    snapshot_problem: Callable[[], Exception],
    cursor_secret: bytes,
    repository: FinancialReadRepository | None,
) -> None:
    """注册 0013 的四条只读路径，并将每页绑定到同一个生产发布快照。"""

    @app.get(
        "/internal/v1/equities/{exchange}/{symbol}/financial-reports",
        dependencies=[Depends(require_service_bearer)],
    )
    def list_financial_reports(
        request: Request,
        exchange: Annotated[ExchangeCode, Path()],
        symbol: Annotated[str, Path(pattern=r"^[0-9]{6}$")],
        methodology_code: Annotated[
            str,
            Query(alias="methodologyCode", pattern=r"^[a-z][a-z0-9_.-]{2,79}$"),
        ],
        methodology_version: Annotated[int, Query(alias="methodologyVersion", ge=1)],
        data_version: Annotated[UUID, Query(alias="dataVersion")],
        statement_type: Annotated[
            list[StatementType] | None,
            Query(alias="statementType", min_length=1, max_length=3),
        ] = None,
        basis: Annotated[list[PeriodBasis] | None, Query(min_length=1, max_length=4)] = None,
        scope: Annotated[StatementScope | None, Query()] = None,
        report_period_from: Annotated[date | None, Query(alias="reportPeriodFrom")] = None,
        report_period_to: Annotated[date | None, Query(alias="reportPeriodTo")] = None,
        as_of: Annotated[date | None, Query(alias="asOf")] = None,
        known_at: Annotated[datetime | None, Query(alias="knownAt")] = None,
        cursor: Annotated[str | None, Query(min_length=1, max_length=1024)] = None,
        limit: Annotated[int, Query(ge=1, le=50)] = 20,
        if_none_match: Annotated[str | None, Header(alias="If-None-Match", max_length=256)] = None,
    ) -> Response:
        """读取一个方法学的已发布报表页，游标、双时态视图和缓存表示都绑定同一版本。"""
        _validate_period_range(
            report_period_from,
            report_period_to,
            validation_problem=validation_problem,
        )
        _validate_known_at(known_at, validation_problem=validation_problem)
        publication = _current_publication_or_unavailable(
            repository=repository,
            exchange=Exchange(exchange),
            symbol=symbol,
            capability="financial.report",
            methodology_code=methodology_code,
            methodology_version=methodology_version,
            expected_data_version=data_version,
            as_of=as_of,
            known_at=known_at,
            unavailable_problem=unavailable_problem,
            snapshot_problem=snapshot_problem,
        )
        # 上一步在仓储缺失时已经抛出统一问题；此处收窄类型以安全调用已发布读取端口。
        assert repository is not None
        view_as_of, view_known_at = _report_view_times_or_problem(
            publication=publication,
            as_of=as_of,
            known_at=known_at,
            validation_problem=validation_problem,
        )
        statement_types = tuple(statement_type or ())
        period_bases = tuple(basis or ())
        after_report_period, after_statement_type, after_report_ref = (
            _financial_report_cursor_or_problem(
                cursor=cursor,
                publication=publication,
                as_of=view_as_of,
                known_at=view_known_at,
                statement_types=statement_types,
                period_bases=period_bases,
                statement_scope=scope,
                report_period_from=report_period_from,
                report_period_to=report_period_to,
                cursor_secret=cursor_secret,
                validation_problem=validation_problem,
                cursor_problem=cursor_problem,
            )
        )
        try:
            rows = repository.list_reports(
                publication=publication,
                as_of=view_as_of,
                known_at=view_known_at,
                statement_types=statement_types,
                period_bases=period_bases,
                statement_scope=scope,
                report_period_from=report_period_from,
                report_period_to=report_period_to,
                after_report_period=after_report_period,
                after_statement_type=after_statement_type,
                after_report_ref=after_report_ref,
                limit=limit + 1,
            )
        except FinancialReadUnavailable as error:
            raise unavailable_problem() from error
        visible_rows = rows[:limit]
        next_cursor = (
            _encode_financial_report_cursor(
                publication=publication,
                as_of=view_as_of,
                known_at=view_known_at,
                statement_types=statement_types,
                period_bases=period_bases,
                statement_scope=scope,
                report_period_from=report_period_from,
                report_period_to=report_period_to,
                report=visible_rows[-1],
                cursor_secret=cursor_secret,
            )
            if len(rows) > limit and visible_rows
            else None
        )
        body = {
            "instrumentId": str(publication.instrument_id),
            "exchange": exchange,
            "symbol": symbol,
            "items": [
                _financial_report_header(
                    report=report,
                    publication=publication,
                    exchange=exchange,
                    symbol=symbol,
                )
                for report in visible_rows
            ],
            "nextCursor": next_cursor,
            "methodologyCode": publication.methodology_code,
            "methodologyVersion": publication.methodology_version,
            "dataVersion": str(publication.data_version),
            "publishedAt": _timestamp(publication.published_at),
            "effectiveAsOf": publication.effective_as_of.isoformat(),
            "knowledgeCutoff": _timestamp(publication.knowledge_cutoff),
        }
        return _financial_conditional_json_response(
            request=request,
            if_none_match=if_none_match,
            etag=_financial_report_etag(
                publication=publication,
                cursor=cursor,
                as_of=view_as_of,
                known_at=view_known_at,
                statement_types=statement_types,
                period_bases=period_bases,
                statement_scope=scope,
                report_period_from=report_period_from,
                report_period_to=report_period_to,
                limit=limit,
            ),
            data_version=publication.data_version,
            body=body,
        )

    @app.get(
        "/internal/v1/equities/{exchange}/{symbol}/financial-reports/{report_ref}",
        dependencies=[Depends(require_service_bearer)],
    )
    def get_financial_report(
        request: Request,
        exchange: Annotated[ExchangeCode, Path()],
        symbol: Annotated[str, Path(pattern=r"^[0-9]{6}$")],
        report_ref: UUID,
        data_version: Annotated[UUID, Query(alias="dataVersion")],
        metric: Annotated[list[str] | None, Query(min_length=1, max_length=100)] = None,
        as_of: Annotated[date | None, Query(alias="asOf")] = None,
        known_at: Annotated[datetime | None, Query(alias="knownAt")] = None,
        cursor: Annotated[str | None, Query(min_length=1, max_length=1024)] = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 100,
        if_none_match: Annotated[str | None, Header(alias="If-None-Match", max_length=256)] = None,
    ) -> Response:
        """读取一个已发布报表的治理行项目页，所有字段与游标绑定相同双时态 revision。"""
        _validate_known_at(known_at, validation_problem=validation_problem)
        publication = _current_report_publication_or_unavailable(
            repository=repository,
            exchange=Exchange(exchange),
            symbol=symbol,
            report_ref=report_ref,
            expected_data_version=data_version,
            as_of=as_of,
            known_at=known_at,
            unavailable_problem=unavailable_problem,
            snapshot_problem=snapshot_problem,
        )
        # 发布选择成功后仓储必然存在；断言仅向类型检查表达此 fail-closed 前置条件。
        assert repository is not None
        view_as_of, view_known_at = _report_view_times_or_problem(
            publication=publication,
            as_of=as_of,
            known_at=known_at,
            validation_problem=validation_problem,
        )
        metric_codes = _metric_codes_or_problem(metric, validation_problem=validation_problem)
        try:
            detail = repository.get_report_detail(
                publication=publication,
                report_ref=report_ref,
                as_of=view_as_of,
                known_at=view_known_at,
            )
        except FinancialReadUnavailable as error:
            raise unavailable_problem() from error
        if detail is None:
            raise not_found_problem()
        after_metric_code = _financial_statement_fact_cursor_or_problem(
            cursor=cursor,
            publication=publication,
            report_ref=report_ref,
            as_of=view_as_of,
            known_at=view_known_at,
            metric_codes=metric_codes,
            cursor_secret=cursor_secret,
            validation_problem=validation_problem,
            cursor_problem=cursor_problem,
        )
        try:
            facts = repository.list_report_facts(
                detail=detail,
                metric_codes=metric_codes,
                after_metric_code=after_metric_code,
                limit=limit + 1,
            )
        except FinancialReadUnavailable as error:
            raise unavailable_problem() from error
        visible_facts = facts[:limit]
        next_cursor = (
            _encode_financial_statement_fact_cursor(
                publication=publication,
                report_ref=report_ref,
                as_of=view_as_of,
                known_at=view_known_at,
                metric_codes=metric_codes,
                metric_code=visible_facts[-1].metric_code,
                cursor_secret=cursor_secret,
            )
            if len(facts) > limit and visible_facts
            else None
        )
        body = {
            "report": _financial_report_header(
                report=detail.report,
                publication=publication,
                exchange=exchange,
                symbol=symbol,
            ),
            "items": [_financial_statement_item(fact) for fact in visible_facts],
            "nextCursor": next_cursor,
            "dataVersion": str(publication.data_version),
            "publishedAt": _timestamp(publication.published_at),
            "effectiveAsOf": publication.effective_as_of.isoformat(),
            "knowledgeCutoff": _timestamp(publication.knowledge_cutoff),
        }
        return _financial_conditional_json_response(
            request=request,
            if_none_match=if_none_match,
            etag=_financial_report_detail_etag(
                publication=publication,
                report_ref=report_ref,
                cursor=cursor,
                as_of=view_as_of,
                known_at=view_known_at,
                metric_codes=metric_codes,
                detail=detail,
                limit=limit,
            ),
            data_version=publication.data_version,
            body=body,
        )

    @app.get(
        "/internal/v1/equities/{exchange}/{symbol}/financial-metrics",
        dependencies=[Depends(require_service_bearer)],
    )
    def list_financial_metrics(
        request: Request,
        exchange: Annotated[ExchangeCode, Path()],
        symbol: Annotated[str, Path(pattern=r"^[0-9]{6}$")],
        origin: Annotated[MetricOrigin, Query()],
        methodology_code: Annotated[
            str,
            Query(alias="methodologyCode", pattern=r"^[a-z][a-z0-9_.-]{2,79}$"),
        ],
        methodology_version: Annotated[int, Query(alias="methodologyVersion", ge=1)],
        data_version: Annotated[UUID, Query(alias="dataVersion")],
        metric: Annotated[list[str], Query(min_length=1, max_length=50)],
        basis: Annotated[list[PeriodBasis] | None, Query(min_length=1, max_length=4)] = None,
        report_period_from: Annotated[date | None, Query(alias="reportPeriodFrom")] = None,
        report_period_to: Annotated[date | None, Query(alias="reportPeriodTo")] = None,
        as_of: Annotated[date | None, Query(alias="asOf")] = None,
        known_at: Annotated[datetime | None, Query(alias="knownAt")] = None,
        cursor: Annotated[str | None, Query(min_length=1, max_length=1024)] = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 200,
        if_none_match: Annotated[str | None, Header(alias="If-None-Match", max_length=256)] = None,
    ) -> Response:
        """读取单一来源和方法学的生产指标页，绝不混合供应商值与派生值。"""
        _validate_period_range(
            report_period_from,
            report_period_to,
            validation_problem=validation_problem,
        )
        _validate_known_at(known_at, validation_problem=validation_problem)
        capability: FinancialCapability = (
            "financial.provider-metric"
            if origin == "PROVIDER_REPORTED"
            else "financial.derived-metric"
        )
        publication = _current_publication_or_unavailable(
            repository=repository,
            exchange=Exchange(exchange),
            symbol=symbol,
            capability=capability,
            methodology_code=methodology_code,
            methodology_version=methodology_version,
            expected_data_version=data_version,
            as_of=as_of,
            known_at=known_at,
            unavailable_problem=unavailable_problem,
            snapshot_problem=snapshot_problem,
        )
        assert repository is not None
        view_as_of, view_known_at = _report_view_times_or_problem(
            publication=publication,
            as_of=as_of,
            known_at=known_at,
            validation_problem=validation_problem,
        )
        metric_codes = _metric_codes_or_problem(metric, validation_problem=validation_problem)
        period_bases = tuple(basis or ())
        after_report_period, after_metric_code = _financial_metric_cursor_or_problem(
            cursor=cursor,
            publication=publication,
            as_of=view_as_of,
            known_at=view_known_at,
            origin=origin,
            metric_codes=metric_codes,
            period_bases=period_bases,
            report_period_from=report_period_from,
            report_period_to=report_period_to,
            cursor_secret=cursor_secret,
            validation_problem=validation_problem,
            cursor_problem=cursor_problem,
        )
        try:
            read_metrics = (
                repository.list_provider_metrics
                if origin == "PROVIDER_REPORTED"
                else repository.list_derived_metrics
            )
            rows = read_metrics(
                publication=publication,
                as_of=view_as_of,
                known_at=view_known_at,
                metric_codes=metric_codes,
                period_bases=period_bases,
                report_period_from=report_period_from,
                report_period_to=report_period_to,
                after_report_period=after_report_period,
                after_metric_code=after_metric_code,
                limit=limit + 1,
            )
        except FinancialReadUnavailable as error:
            raise unavailable_problem() from error
        visible_rows = rows[:limit]
        next_cursor = (
            _encode_financial_metric_cursor(
                publication=publication,
                as_of=view_as_of,
                known_at=view_known_at,
                origin=origin,
                metric_codes=metric_codes,
                period_bases=period_bases,
                report_period_from=report_period_from,
                report_period_to=report_period_to,
                metric_row=visible_rows[-1],
                cursor_secret=cursor_secret,
            )
            if len(rows) > limit and visible_rows
            else None
        )
        body = {
            "instrumentId": str(publication.instrument_id),
            "exchange": exchange,
            "symbol": symbol,
            "origin": origin,
            "methodologyCode": publication.methodology_code,
            "methodologyVersion": publication.methodology_version,
            "items": [
                _financial_metric_item(metric_row=metric_row, publication=publication)
                for metric_row in visible_rows
            ],
            "nextCursor": next_cursor,
            "dataVersion": str(publication.data_version),
            "publishedAt": _timestamp(publication.published_at),
            "effectiveAsOf": publication.effective_as_of.isoformat(),
            "knowledgeCutoff": _timestamp(publication.knowledge_cutoff),
        }
        return _financial_conditional_json_response(
            request=request,
            if_none_match=if_none_match,
            etag=_financial_metric_etag(
                publication=publication,
                cursor=cursor,
                as_of=view_as_of,
                known_at=view_known_at,
                origin=origin,
                metric_codes=metric_codes,
                period_bases=period_bases,
                report_period_from=report_period_from,
                report_period_to=report_period_to,
                limit=limit,
            ),
            data_version=publication.data_version,
            body=body,
        )

    @app.get(
        "/internal/v1/equities/{exchange}/{symbol}/valuations",
        dependencies=[Depends(require_service_bearer)],
    )
    def list_valuations(
        request: Request,
        exchange: Annotated[ExchangeCode, Path()],
        symbol: Annotated[str, Path(pattern=r"^[0-9]{6}$")],
        methodology_code: Annotated[
            str,
            Query(alias="methodologyCode", pattern=r"^[a-z][a-z0-9_.-]{2,79}$"),
        ],
        methodology_version: Annotated[int, Query(alias="methodologyVersion", ge=1)],
        data_version: Annotated[UUID, Query(alias="dataVersion")],
        metric: Annotated[list[ValuationMetric], Query(min_length=1, max_length=5)],
        start: date,
        end: date,
        as_of: Annotated[date | None, Query(alias="asOf")] = None,
        known_at: Annotated[datetime | None, Query(alias="knownAt")] = None,
        cursor: Annotated[str | None, Query(min_length=1, max_length=1024)] = None,
        limit: Annotated[int, Query(ge=1, le=1000)] = 500,
        if_none_match: Annotated[str | None, Header(alias="If-None-Match", max_length=256)] = None,
    ) -> Response:
        """读取单一估值方法学的生产观察页，日期是上游观察键而非交易所最终确认。"""
        _validate_valuation_range(start, end, validation_problem=validation_problem)
        _validate_known_at(known_at, validation_problem=validation_problem)
        publication = _current_publication_or_unavailable(
            repository=repository,
            exchange=Exchange(exchange),
            symbol=symbol,
            capability="financial.valuation",
            methodology_code=methodology_code,
            methodology_version=methodology_version,
            expected_data_version=data_version,
            as_of=as_of,
            known_at=known_at,
            unavailable_problem=unavailable_problem,
            snapshot_problem=snapshot_problem,
        )
        assert repository is not None
        view_as_of, view_known_at = _report_view_times_or_problem(
            publication=publication,
            as_of=as_of,
            known_at=known_at,
            validation_problem=validation_problem,
        )
        metric_codes = _metric_codes_or_problem(metric, validation_problem=validation_problem)
        after_observation_date, after_metric_code = _valuation_cursor_or_problem(
            cursor=cursor,
            publication=publication,
            as_of=view_as_of,
            known_at=view_known_at,
            metric_codes=metric_codes,
            start=start,
            end=end,
            cursor_secret=cursor_secret,
            validation_problem=validation_problem,
            cursor_problem=cursor_problem,
        )
        try:
            rows = repository.list_valuations(
                publication=publication,
                as_of=view_as_of,
                known_at=view_known_at,
                metric_codes=metric_codes,
                start=start,
                end=end,
                after_observation_date=after_observation_date,
                after_metric_code=after_metric_code,
                limit=limit + 1,
            )
        except FinancialReadUnavailable as error:
            raise unavailable_problem() from error
        visible_rows = rows[:limit]
        next_cursor = (
            _encode_valuation_cursor(
                publication=publication,
                as_of=view_as_of,
                known_at=view_known_at,
                metric_codes=metric_codes,
                start=start,
                end=end,
                valuation=visible_rows[-1],
                cursor_secret=cursor_secret,
            )
            if len(rows) > limit and visible_rows
            else None
        )
        body = {
            "instrumentId": str(publication.instrument_id),
            "exchange": exchange,
            "symbol": symbol,
            "methodologyCode": publication.methodology_code,
            "methodologyVersion": publication.methodology_version,
            "items": [
                _valuation_item(valuation=valuation, publication=publication)
                for valuation in visible_rows
            ],
            "nextCursor": next_cursor,
            "dataVersion": str(publication.data_version),
            "publishedAt": _timestamp(publication.published_at),
            "effectiveAsOf": publication.effective_as_of.isoformat(),
            "knowledgeCutoff": _timestamp(publication.knowledge_cutoff),
        }
        return _financial_conditional_json_response(
            request=request,
            if_none_match=if_none_match,
            etag=_valuation_etag(
                publication=publication,
                cursor=cursor,
                as_of=view_as_of,
                known_at=view_known_at,
                metric_codes=metric_codes,
                start=start,
                end=end,
                limit=limit,
            ),
            data_version=publication.data_version,
            body=body,
        )


def _current_publication_or_unavailable(
    *,
    repository: FinancialReadRepository | None,
    exchange: Exchange,
    symbol: str,
    capability: FinancialCapability,
    methodology_code: str,
    methodology_version: int,
    expected_data_version: UUID,
    as_of: date | None,
    known_at: datetime | None,
    unavailable_problem: Callable[[], Exception],
    snapshot_problem: Callable[[], Exception],
) -> FinancialPublicationSnapshot:
    """按双时态身份只接受状态门控的精确当前发布。"""
    if repository is None:
        raise unavailable_problem()
    try:
        publication = repository.get_current_publication(
            exchange=exchange,
            symbol=symbol,
            capability=capability,
            methodology_code=methodology_code,
            methodology_version=methodology_version,
            as_of=as_of,
            known_at=known_at,
        )
    except FinancialReadUnavailable as error:
        raise unavailable_problem() from error
    if publication is None:
        raise snapshot_problem()
    if publication.data_version != expected_data_version:
        raise snapshot_problem()
    return publication


def _current_report_publication_or_unavailable(
    *,
    repository: FinancialReadRepository | None,
    exchange: Exchange,
    symbol: str,
    report_ref: UUID,
    expected_data_version: UUID,
    as_of: date | None,
    known_at: datetime | None,
    unavailable_problem: Callable[[], Exception],
    snapshot_problem: Callable[[], Exception],
) -> FinancialPublicationSnapshot:
    """从公开报表引用反查并校验状态门控的精确当前发布。"""
    if repository is None:
        raise unavailable_problem()
    try:
        publication = repository.get_current_report_publication(
            exchange=exchange,
            symbol=symbol,
            report_ref=report_ref,
            as_of=as_of,
            known_at=known_at,
        )
    except FinancialReadUnavailable as error:
        raise unavailable_problem() from error
    if publication is None:
        raise snapshot_problem()
    if publication.data_version != expected_data_version:
        raise snapshot_problem()
    return publication


def _report_view_times_or_problem(
    *,
    publication: FinancialPublicationSnapshot,
    as_of: date | None,
    known_at: datetime | None,
    validation_problem: Callable[[str], Exception],
) -> tuple[date, datetime]:
    """将可选双时态输入解析为 publication 范围内的明确读视图，禁止静默截断未来请求。"""
    view_as_of = publication.effective_as_of if as_of is None else as_of
    view_known_at = publication.knowledge_cutoff if known_at is None else known_at
    if view_as_of > publication.effective_as_of:
        raise validation_problem("asOf exceeds publication effective cutoff")
    if view_known_at > publication.knowledge_cutoff:
        raise validation_problem("knownAt exceeds publication knowledge cutoff")
    return view_as_of, view_known_at


def _financial_report_cursor_or_problem(
    *,
    cursor: str | None,
    publication: FinancialPublicationSnapshot,
    as_of: date,
    known_at: datetime,
    statement_types: tuple[str, ...],
    period_bases: tuple[str, ...],
    statement_scope: str | None,
    report_period_from: date | None,
    report_period_to: date | None,
    cursor_secret: bytes,
    validation_problem: Callable[[str], Exception],
    cursor_problem: Callable[[], Exception],
) -> tuple[date | None, str | None, UUID | None]:
    """验证签名游标与当前完整查询视图完全相同，避免跨筛选或跨发布续页。"""
    if cursor is None:
        return None, None, None
    decoded = _decode_financial_cursor(
        cursor, cursor_secret=cursor_secret, validation_problem=validation_problem
    )
    expected = _financial_report_cursor_scope(
        publication=publication,
        as_of=as_of,
        known_at=known_at,
        statement_types=statement_types,
        period_bases=period_bases,
        statement_scope=statement_scope,
        report_period_from=report_period_from,
        report_period_to=report_period_to,
    )
    if any(decoded.get(key) != value for key, value in expected.items()):
        raise cursor_problem()
    report_period = decoded.get("p")
    statement_type = decoded.get("t")
    report_ref = decoded.get("r")
    if not isinstance(report_period, str) or not isinstance(statement_type, str):
        raise validation_problem("cursor is invalid")
    if not isinstance(report_ref, str):
        raise validation_problem("cursor is invalid")
    try:
        return date.fromisoformat(report_period), statement_type, UUID(report_ref)
    except ValueError as error:
        raise validation_problem("cursor is invalid") from error


def _encode_financial_report_cursor(
    *,
    publication: FinancialPublicationSnapshot,
    as_of: date,
    known_at: datetime,
    statement_types: tuple[str, ...],
    period_bases: tuple[str, ...],
    statement_scope: str | None,
    report_period_from: date | None,
    report_period_to: date | None,
    report: PublishedFinancialReport,
    cursor_secret: bytes,
) -> str:
    """签名编码报表下一页位置，使客户端不能伪造位置或混用读取视图。"""
    payload = _financial_report_cursor_scope(
        publication=publication,
        as_of=as_of,
        known_at=known_at,
        statement_types=statement_types,
        period_bases=period_bases,
        statement_scope=statement_scope,
        report_period_from=report_period_from,
        report_period_to=report_period_to,
    )
    payload.update(
        {
            "p": report.report_period.isoformat(),
            "t": report.statement_type,
            "r": str(report.report_ref),
        }
    )
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    signature = hmac.new(cursor_secret, encoded, hashlib.sha256).digest()
    return f"{_base64url(encoded)}.{_base64url(signature)}"


def _financial_report_cursor_scope(
    *,
    publication: FinancialPublicationSnapshot,
    as_of: date,
    known_at: datetime,
    statement_types: tuple[str, ...],
    period_bases: tuple[str, ...],
    statement_scope: str | None,
    report_period_from: date | None,
    report_period_to: date | None,
) -> dict[str, object]:
    """构造参与签名和匹配的报表查询语义，不将内部数据库键暴露给客户端。"""
    return {
        "v": str(publication.data_version),
        "a": as_of.isoformat(),
        "k": _timestamp(known_at),
        "y": list(statement_types),
        "b": list(period_bases),
        "s": statement_scope,
        "f": None if report_period_from is None else report_period_from.isoformat(),
        "u": None if report_period_to is None else report_period_to.isoformat(),
    }


def _financial_metric_cursor_or_problem(
    *,
    cursor: str | None,
    publication: FinancialPublicationSnapshot,
    as_of: date,
    known_at: datetime,
    origin: MetricOrigin,
    metric_codes: tuple[str, ...],
    period_bases: tuple[str, ...],
    report_period_from: date | None,
    report_period_to: date | None,
    cursor_secret: bytes,
    validation_problem: Callable[[str], Exception],
    cursor_problem: Callable[[], Exception],
) -> tuple[date | None, str | None]:
    """验证指标游标与发布、来源、双时态视图和所有筛选条件完全一致。"""
    if cursor is None:
        return None, None
    decoded = _decode_financial_cursor(
        cursor, cursor_secret=cursor_secret, validation_problem=validation_problem
    )
    expected = _financial_metric_cursor_scope(
        publication=publication,
        as_of=as_of,
        known_at=known_at,
        origin=origin,
        metric_codes=metric_codes,
        period_bases=period_bases,
        report_period_from=report_period_from,
        report_period_to=report_period_to,
    )
    if any(decoded.get(key) != value for key, value in expected.items()):
        raise cursor_problem()
    report_period = decoded.get("p")
    metric_code = decoded.get("c")
    if not isinstance(report_period, str) or not isinstance(metric_code, str) or not metric_code:
        raise validation_problem("cursor is invalid")
    try:
        return date.fromisoformat(report_period), metric_code
    except ValueError as error:
        raise validation_problem("cursor is invalid") from error


def _encode_financial_metric_cursor(
    *,
    publication: FinancialPublicationSnapshot,
    as_of: date,
    known_at: datetime,
    origin: MetricOrigin,
    metric_codes: tuple[str, ...],
    period_bases: tuple[str, ...],
    report_period_from: date | None,
    report_period_to: date | None,
    metric_row: PublishedFinancialMetric,
    cursor_secret: bytes,
) -> str:
    """签名指标页的复合排序位置，防止客户端跨来源、发布或筛选条件续页。"""
    payload = _financial_metric_cursor_scope(
        publication=publication,
        as_of=as_of,
        known_at=known_at,
        origin=origin,
        metric_codes=metric_codes,
        period_bases=period_bases,
        report_period_from=report_period_from,
        report_period_to=report_period_to,
    )
    payload.update({"p": metric_row.report_period.isoformat(), "c": metric_row.metric_code})
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    signature = hmac.new(cursor_secret, encoded, hashlib.sha256).digest()
    return f"{_base64url(encoded)}.{_base64url(signature)}"


def _financial_metric_cursor_scope(
    *,
    publication: FinancialPublicationSnapshot,
    as_of: date,
    known_at: datetime,
    origin: MetricOrigin,
    metric_codes: tuple[str, ...],
    period_bases: tuple[str, ...],
    report_period_from: date | None,
    report_period_to: date | None,
) -> dict[str, object]:
    """构造指标游标的完整业务范围，指标集合排序后避免参数顺序影响续页。"""
    return {
        "v": str(publication.data_version),
        "a": as_of.isoformat(),
        "k": _timestamp(known_at),
        "o": origin,
        "m": list(metric_codes),
        "b": list(period_bases),
        "f": None if report_period_from is None else report_period_from.isoformat(),
        "u": None if report_period_to is None else report_period_to.isoformat(),
    }


def _valuation_cursor_or_problem(
    *,
    cursor: str | None,
    publication: FinancialPublicationSnapshot,
    as_of: date,
    known_at: datetime,
    metric_codes: tuple[str, ...],
    start: date,
    end: date,
    cursor_secret: bytes,
    validation_problem: Callable[[str], Exception],
    cursor_problem: Callable[[], Exception],
) -> tuple[date | None, str | None]:
    """验证估值游标的发布、时间窗、双时态视图和稳定复合排序位置。"""
    if cursor is None:
        return None, None
    decoded = _decode_financial_cursor(
        cursor, cursor_secret=cursor_secret, validation_problem=validation_problem
    )
    expected = _valuation_cursor_scope(
        publication=publication,
        as_of=as_of,
        known_at=known_at,
        metric_codes=metric_codes,
        start=start,
        end=end,
    )
    if any(decoded.get(key) != value for key, value in expected.items()):
        raise cursor_problem()
    observation_date = decoded.get("d")
    metric_code = decoded.get("c")
    if not isinstance(observation_date, str) or not isinstance(metric_code, str) or not metric_code:
        raise validation_problem("cursor is invalid")
    try:
        return date.fromisoformat(observation_date), metric_code
    except ValueError as error:
        raise validation_problem("cursor is invalid") from error


def _encode_valuation_cursor(
    *,
    publication: FinancialPublicationSnapshot,
    as_of: date,
    known_at: datetime,
    metric_codes: tuple[str, ...],
    start: date,
    end: date,
    valuation: PublishedValuationObservation,
    cursor_secret: bytes,
) -> str:
    """签名估值页位置，避免客户端跨观察窗口、指标集或数据版本继续读取。"""
    payload = _valuation_cursor_scope(
        publication=publication,
        as_of=as_of,
        known_at=known_at,
        metric_codes=metric_codes,
        start=start,
        end=end,
    )
    payload.update({"d": valuation.observation_date.isoformat(), "c": valuation.metric_code})
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    signature = hmac.new(cursor_secret, encoded, hashlib.sha256).digest()
    return f"{_base64url(encoded)}.{_base64url(signature)}"


def _valuation_cursor_scope(
    *,
    publication: FinancialPublicationSnapshot,
    as_of: date,
    known_at: datetime,
    metric_codes: tuple[str, ...],
    start: date,
    end: date,
) -> dict[str, object]:
    """构造估值游标的完整范围，日期窗口边界和指标筛选都属于游标身份。"""
    return {
        "v": str(publication.data_version),
        "a": as_of.isoformat(),
        "k": _timestamp(known_at),
        "m": list(metric_codes),
        "s": start.isoformat(),
        "e": end.isoformat(),
    }


def _metric_codes_or_problem(
    metric: Sequence[str] | None,
    *,
    validation_problem: Callable[[str], Exception],
) -> tuple[str, ...]:
    """将字段筛选规约为稳定集合，并拒绝重复字段导致的歧义游标范围。"""
    metric_codes = tuple(metric or ())
    if len(set(metric_codes)) != len(metric_codes):
        raise validation_problem("metric values must be unique")
    return tuple(sorted(metric_codes))


def _financial_statement_fact_cursor_or_problem(
    *,
    cursor: str | None,
    publication: FinancialPublicationSnapshot,
    report_ref: UUID,
    as_of: date,
    known_at: datetime,
    metric_codes: tuple[str, ...],
    cursor_secret: bytes,
    validation_problem: Callable[[str], Exception],
    cursor_problem: Callable[[], Exception],
) -> str | None:
    """验证报表详情游标的版本、报表、视图与字段范围，并返回字段续页位置。"""
    if cursor is None:
        return None
    decoded = _decode_financial_cursor(
        cursor, cursor_secret=cursor_secret, validation_problem=validation_problem
    )
    expected = _financial_statement_fact_cursor_scope(
        publication=publication,
        report_ref=report_ref,
        as_of=as_of,
        known_at=known_at,
        metric_codes=metric_codes,
    )
    if any(decoded.get(key) != value for key, value in expected.items()):
        raise cursor_problem()
    metric_code = decoded.get("c")
    if not isinstance(metric_code, str) or not metric_code:
        raise validation_problem("cursor is invalid")
    return metric_code


def _encode_financial_statement_fact_cursor(
    *,
    publication: FinancialPublicationSnapshot,
    report_ref: UUID,
    as_of: date,
    known_at: datetime,
    metric_codes: tuple[str, ...],
    metric_code: str,
    cursor_secret: bytes,
) -> str:
    """签名报表行项目字段位置，阻止客户端跨报表、范围或版本继续读取。"""
    payload = _financial_statement_fact_cursor_scope(
        publication=publication,
        report_ref=report_ref,
        as_of=as_of,
        known_at=known_at,
        metric_codes=metric_codes,
    )
    payload["c"] = metric_code
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    signature = hmac.new(cursor_secret, encoded, hashlib.sha256).digest()
    return f"{_base64url(encoded)}.{_base64url(signature)}"


def _financial_statement_fact_cursor_scope(
    *,
    publication: FinancialPublicationSnapshot,
    report_ref: UUID,
    as_of: date,
    known_at: datetime,
    metric_codes: tuple[str, ...],
) -> dict[str, object]:
    """构造详情行项目游标的完整读取范围，且不泄漏 revision 数据库主键。"""
    return {
        "v": str(publication.data_version),
        "r": str(report_ref),
        "a": as_of.isoformat(),
        "k": _timestamp(known_at),
        "m": list(metric_codes),
    }


def _decode_financial_cursor(
    value: str,
    *,
    cursor_secret: bytes,
    validation_problem: Callable[[str], Exception],
) -> dict[str, object]:
    """验证 HMAC 签名并解析游标 JSON，编码或签名失败统一映射为参数错误。"""
    try:
        payload_part, signature_part = value.split(".", 1)
        payload = _base64url_decode(payload_part)
        signature = _base64url_decode(signature_part)
        expected = hmac.new(cursor_secret, payload, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError("cursor signature is invalid")
        decoded = json.loads(payload)
        if not isinstance(decoded, dict):
            raise ValueError("cursor payload is invalid")
        return decoded
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise validation_problem("cursor is invalid") from error


def _base64url(value: bytes) -> str:
    """编码 URL 安全且无填充的二进制文本，减少游标长度。"""
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _base64url_decode(value: str) -> bytes:
    """解码 URL 安全 base64 文本，并自动补齐协议允许省略的填充字符。"""
    return base64.urlsafe_b64decode((value + "=" * (-len(value) % 4)).encode())


def _financial_report_header(
    *,
    report: PublishedFinancialReport,
    publication: FinancialPublicationSnapshot,
    exchange: ExchangeCode,
    symbol: str,
) -> dict[str, object]:
    """投影内部契约允许的报表头，保留双时态和来源方法学但不泄漏数据库键。"""
    return {
        "instrumentId": str(publication.instrument_id),
        "reportRef": str(report.report_ref),
        "exchange": exchange,
        "symbol": symbol,
        "statementType": report.statement_type,
        "reportPeriod": report.report_period.isoformat(),
        "periodBasis": report.period_basis,
        "statementScope": report.statement_scope,
        "currency": report.currency,
        "currencyNullReason": report.currency_null_reason,
        "reportType": report.report_type,
        "auditStatus": report.audit_status,
        "announcementDate": _date_text(report.announcement_date),
        "providerUpdateDate": _timestamp_optional(report.provider_update_at),
        "effectiveFrom": report.effective_from.isoformat(),
        "effectiveTo": _date_text(report.effective_to),
        "knownFrom": _timestamp(report.known_from),
        "knownTo": _timestamp_optional(report.known_to),
        "knowledgeBasis": report.knowledge_basis,
        "knowledgeConfidence": report.knowledge_confidence,
        "observedAt": _timestamp(report.observed_at),
        "revision": report.revision,
        "methodologyCode": publication.methodology_code,
        "methodologyVersion": publication.methodology_version,
        "sourceCode": publication.source_code,
        "qualityStatus": report.quality_status.upper(),
    }


def _financial_statement_item(fact: PublishedFinancialStatementFact) -> dict[str, str | None]:
    """将精确行项目投影为合同十进制字符串，保留空值和币种的受控原因。"""
    return {
        "metricCode": fact.metric_code,
        "label": fact.label,
        "value": _decimal_text(fact.value),
        "nullReason": fact.null_reason,
        "currency": fact.currency,
        "currencyNullReason": fact.currency_null_reason,
        "originalUnit": fact.original_unit,
        "canonicalUnit": fact.canonical_unit,
        "scaleFactor": _decimal_text(fact.scale_factor),
        "signConvention": fact.sign_convention,
    }


def _financial_metric_item(
    *,
    metric_row: PublishedFinancialMetric,
    publication: FinancialPublicationSnapshot,
) -> dict[str, object]:
    """投影供应商或派生指标，保留方法学和公式版本以禁止同标签值混用。"""
    return {
        "metricCode": metric_row.metric_code,
        "label": metric_row.label,
        "origin": metric_row.origin,
        "reportPeriod": metric_row.report_period.isoformat(),
        "periodBasis": metric_row.period_basis,
        "statementScope": metric_row.statement_scope,
        "value": _decimal_text(metric_row.value),
        "unit": metric_row.unit,
        "currency": metric_row.currency,
        "currencyNullReason": metric_row.currency_null_reason,
        "methodologyCode": publication.methodology_code,
        "methodologyVersion": publication.methodology_version,
        "formulaVersion": metric_row.formula_version,
        "effectiveFrom": metric_row.effective_from.isoformat(),
        "knownFrom": _timestamp(metric_row.known_from),
        "knowledgeBasis": metric_row.knowledge_basis,
        "knowledgeConfidence": metric_row.knowledge_confidence,
        "observedAt": _timestamp(metric_row.observed_at),
        "revision": metric_row.revision,
    }


def _valuation_item(
    *,
    valuation: PublishedValuationObservation,
    publication: FinancialPublicationSnapshot,
) -> dict[str, object]:
    """投影上游估值观察，明确其 finality 不是交易所或供应商最终确认。"""
    return {
        "observationDate": valuation.observation_date.isoformat(),
        "metricCode": valuation.metric_code,
        "value": _decimal_text(valuation.value),
        "unit": valuation.unit,
        "currency": valuation.currency,
        "currencyNullReason": valuation.currency_null_reason,
        "methodologyCode": publication.methodology_code,
        "methodologyVersion": publication.methodology_version,
        "finality": valuation.finality,
        "effectiveFrom": valuation.effective_from.isoformat(),
        "knownFrom": _timestamp(valuation.known_from),
        "knowledgeBasis": valuation.knowledge_basis,
        "knowledgeConfidence": valuation.knowledge_confidence,
        "observedAt": _timestamp(valuation.observed_at),
        "revision": valuation.revision,
    }


def _financial_report_etag(
    *,
    publication: FinancialPublicationSnapshot,
    cursor: str | None,
    as_of: date,
    known_at: datetime,
    statement_types: tuple[str, ...],
    period_bases: tuple[str, ...],
    statement_scope: str | None,
    report_period_from: date | None,
    report_period_to: date | None,
    limit: int,
) -> str:
    """构造绑定发布内容摘要、读视图、筛选和页位置的强 ETag。"""
    discriminator = json.dumps(
        {
            "c": cursor,
            "a": as_of.isoformat(),
            "k": _timestamp(known_at),
            "y": statement_types,
            "b": period_bases,
            "s": statement_scope,
            "f": None if report_period_from is None else report_period_from.isoformat(),
            "u": None if report_period_to is None else report_period_to.isoformat(),
            "l": limit,
            "h": publication.content_sha256,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    suffix = hashlib.sha256(discriminator.encode()).hexdigest()[:16]
    return f'"financial-reports-{publication.data_version}-{suffix}"'


def _financial_report_detail_etag(
    *,
    publication: FinancialPublicationSnapshot,
    report_ref: UUID,
    cursor: str | None,
    as_of: date,
    known_at: datetime,
    metric_codes: tuple[str, ...],
    detail: PublishedFinancialReportDetail,
    limit: int,
) -> str:
    """构造绑定报表 revision、字段筛选与页位置的强 ETag，避免不同详情页错误复用。"""
    discriminator = json.dumps(
        {
            "r": str(report_ref),
            "i": str(detail.revision_id),
            "c": cursor,
            "a": as_of.isoformat(),
            "k": _timestamp(known_at),
            "m": metric_codes,
            "l": limit,
            "h": publication.content_sha256,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    suffix = hashlib.sha256(discriminator.encode()).hexdigest()[:16]
    return f'"financial-report-{publication.data_version}-{suffix}"'


def _financial_metric_etag(
    *,
    publication: FinancialPublicationSnapshot,
    cursor: str | None,
    as_of: date,
    known_at: datetime,
    origin: MetricOrigin,
    metric_codes: tuple[str, ...],
    period_bases: tuple[str, ...],
    report_period_from: date | None,
    report_period_to: date | None,
    limit: int,
) -> str:
    """构造绑定指标来源、筛选、双时态视图和页位置的强 ETag。"""
    discriminator = json.dumps(
        {
            "c": cursor,
            "a": as_of.isoformat(),
            "k": _timestamp(known_at),
            "o": origin,
            "m": metric_codes,
            "b": period_bases,
            "f": None if report_period_from is None else report_period_from.isoformat(),
            "u": None if report_period_to is None else report_period_to.isoformat(),
            "l": limit,
            "h": publication.content_sha256,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    suffix = hashlib.sha256(discriminator.encode()).hexdigest()[:16]
    return f'"financial-metrics-{publication.data_version}-{suffix}"'


def _valuation_etag(
    *,
    publication: FinancialPublicationSnapshot,
    cursor: str | None,
    as_of: date,
    known_at: datetime,
    metric_codes: tuple[str, ...],
    start: date,
    end: date,
    limit: int,
) -> str:
    """构造绑定估值窗口、指标、双时态视图和页位置的强 ETag。"""
    discriminator = json.dumps(
        {
            "c": cursor,
            "a": as_of.isoformat(),
            "k": _timestamp(known_at),
            "m": metric_codes,
            "s": start.isoformat(),
            "e": end.isoformat(),
            "l": limit,
            "h": publication.content_sha256,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    suffix = hashlib.sha256(discriminator.encode()).hexdigest()[:16]
    return f'"valuations-{publication.data_version}-{suffix}"'


def _financial_conditional_json_response(
    *,
    request: Request,
    if_none_match: str | None,
    etag: str,
    data_version: UUID,
    body: dict[str, object],
) -> Response:
    """返回可私有复验的财务表示，命中 ETag 时保留必要版本头并省略响应体。"""
    headers = {
        "ETag": etag,
        "Cache-Control": _PRIVATE_REVALIDATE,
        "X-Data-Version": str(data_version),
        "X-Request-Id": _request_id(request),
    }
    if if_none_match is not None and hmac.compare_digest(if_none_match, etag):
        return Response(status_code=304, headers=headers)
    return JSONResponse(content=body, headers=headers)


def _timestamp(value: datetime) -> str:
    """将带时区时间转换为 RFC 3339 UTC 文本，避免响应时区表达漂移。"""
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _timestamp_optional(value: datetime | None) -> str | None:
    """转换可空时间字段，未知时间保持空值而不伪造来源日期。"""
    return None if value is None else _timestamp(value)


def _date_text(value: date | None) -> str | None:
    """转换可空日期字段，保留合同要求的 JSON 空值语义。"""
    return None if value is None else value.isoformat()


def _decimal_text(value: Decimal | None) -> str | None:
    """将精确小数序列化为非科学计数法字符串，防止 JSON 浮点或指数形式破坏合同。"""
    return None if value is None else format(value, "f")


def _request_id(request: Request) -> str:
    """复用受限长度请求标识；缺失或超长时生成新的可关联 UUID。"""
    supplied = request.headers.get("X-Request-Id")
    return supplied if supplied is not None and 1 <= len(supplied) <= 128 else str(uuid4())


def _validate_period_range(
    start: date | None,
    end: date | None,
    *,
    validation_problem: Callable[[str], Exception],
) -> None:
    """拒绝反向报告期范围，避免未来仓储实现接收语义不确定的筛选条件。"""
    if start is not None and end is not None and start > end:
        raise validation_problem("reportPeriodFrom must not be after reportPeriodTo")


def _validate_valuation_range(
    start: date,
    end: date,
    *,
    validation_problem: Callable[[str], Exception],
) -> None:
    """限制估值窗口，避免单请求在将来跨越未批准的大范围历史读取。"""
    if start > end:
        raise validation_problem("start must not be after end")
    if (end - start).days > 3660:
        raise validation_problem("valuation date span exceeds 3660 days")


def _validate_known_at(
    known_at: datetime | None,
    *,
    validation_problem: Callable[[str], Exception],
) -> None:
    """拒绝无时区或未来知识时间，防止暗发布响应形成前视数据承诺。"""
    if known_at is None:
        return
    if known_at.tzinfo is None:
        raise validation_problem("knownAt must include a timezone")
    if known_at > datetime.now(UTC):
        raise validation_problem("knownAt must not be in the future")
