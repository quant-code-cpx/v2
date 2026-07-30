"""市场概览原子 bundle、排行、日历、板块和申万资源式内部 GET API。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID, uuid5
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Header, Path, Query, Request
from fastapi.responses import JSONResponse, Response

from service_data_sync.application.ports.market_overview import (
    MarketOverviewRepository,
    StoredMarketBundle,
    StoredMarketComponent,
)
from service_data_sync.interfaces.internal_sector_api import InternalProblem

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_PRIVATE_REVALIDATE = "private, max-age=0, must-revalidate"
_MARKET_NAMESPACE = UUID("e491ce27-d419-4ed7-a17a-c51505e01331")
_INDEX_IDS = frozenset({"sse-composite", "szse-component", "csi-300", "chinext"})
_SECTOR_SCHEMES = frozenset({"eastmoney.industry", "eastmoney.concept"})
_PERIODS = frozenset({"1d", "1w", "1mo"})


@dataclass(frozen=True, slots=True)
class _Snapshot:
    """固定一个完整 bundle 及其 manifest 引用的精确组件版本。"""

    bundle: StoredMarketBundle
    components: dict[str, StoredMarketComponent]


def register_market_overview_routes(
    app: FastAPI,
    *,
    repository: MarketOverviewRepository,
    require_service_bearer: Callable[..., None],
    cursor_secret: bytes,
    now: Callable[[], datetime] | None = None,
) -> None:
    """注册只读资源路由；所有响应绑定强 ETag 与 `X-Data-Version`。"""
    clock = now or (lambda: datetime.now(_SHANGHAI))

    @app.get(
        "/internal/v1/market/overview-bundles/{snapshot}",
        dependencies=[Depends(require_service_bearer)],
    )
    def get_overview(
        request: Request,
        snapshot: Annotated[str, Path(min_length=6, max_length=10)],
        if_none_match: Annotated[str | None, Header()] = None,
    ) -> Response:
        """读取 latest 或精确交易日完整包，并在读时派生交易状态与新鲜度。"""
        snapshot_date = _snapshot_date(snapshot)
        selected = _snapshot(repository, snapshot_date)
        body = dict(selected.bundle.payload)
        body["status"] = _market_status(
            selected,
            clock(),
            historical=snapshot_date is not None,
        )
        return _conditional_response(
            request=request,
            body=body,
            data_version=selected.bundle.data_version,
            if_none_match=if_none_match,
            etag_scope={"resource": "overview", "status": body["status"]},
        )

    @app.get(
        "/internal/v1/market/indices/{index_id}/bars",
        dependencies=[Depends(require_service_bearer)],
    )
    def list_index_bars(
        request: Request,
        index_id: Annotated[str, Path(min_length=2, max_length=32)],
        period: Annotated[str, Query()] = "1d",
        start: Annotated[date, Query()] = date.min,
        end: Annotated[date, Query()] = date.max,
        cursor: Annotated[str | None, Query(max_length=2048)] = None,
        limit: Annotated[int, Query(ge=1, le=1000)] = 500,
        if_none_match: Annotated[str | None, Header()] = None,
    ) -> Response:
        """读取四个稳定指数身份的真实日线，不从成分观察推导行情。"""
        if index_id not in _INDEX_IDS or period != "1d" or start > end:
            raise _validation_problem("Index bar query is invalid")
        components = repository.list_components(
            dataset_code="index.bar.1d",
            start=None,
            end=end,
        )
        matching, contributing = _index_records(
            components,
            index_id=index_id,
            start=start,
            end=end,
        )
        query_identity = {
            "indexId": index_id,
            "period": period,
            "start": str(start),
            "end": str(end),
        }
        data_version = _composite_data_version(
            "index-bars",
            contributing,
            scope=query_identity,
        )
        input_versions = _active_component_versions(contributing)
        latest_component = max(contributing, key=lambda component: component.published_at)
        source = _common_market_source(contributing, selected=latest_component)
        page, next_cursor = _page(
            matching,
            cursor=cursor,
            limit=limit,
            data_version=data_version,
            fingerprint=query_identity,
            secret=cursor_secret,
        )
        first = matching[0]
        body = {
            "dataVersion": str(data_version),
            "publishedAt": _timestamp(max(component.published_at for component in contributing)),
            "index": {"indexId": index_id, "name": first["name"]},
            "period": "1d",
            "volumeUnit": "lot",
            "source": source,
            "inputDataVersions": input_versions,
            "items": [
                {
                    "tradeDate": row["tradeDate"],
                    "open": row["open"],
                    "high": row["high"],
                    "low": row["low"],
                    "close": row["close"],
                    "previousClose": row["previousClose"],
                    "change": row["change"],
                    "changePercent": row["changePercent"],
                    "volume": row["volume"],
                    "amountCny": row["amountCny"],
                    "finality": "final",
                }
                for row in page
            ],
            "nextCursor": next_cursor,
        }
        return _conditional_response(
            request=request,
            body=body,
            data_version=data_version,
            if_none_match=if_none_match,
            etag_scope={"resource": "index-bars", "query": _query_scope(body, cursor)},
        )

    @app.get(
        "/internal/v1/market/equity-rankings",
        dependencies=[Depends(require_service_bearer)],
    )
    def list_equity_rankings(
        request: Request,
        metric: Annotated[str, Query()],
        order: Annotated[str, Query()],
        as_of: Annotated[date | None, Query(alias="asOf")] = None,
        cursor: Annotated[str | None, Query(max_length=2048)] = None,
        limit: Annotated[int, Query(ge=1, le=50)] = 20,
        if_none_match: Annotated[str | None, Header()] = None,
    ) -> Response:
        """读取冻结全市场横截面排行，并让游标绑定 metric、order 和版本。"""
        if metric not in {"changePercent", "amountCny", "turnoverPercent"} or order not in {
            "asc",
            "desc",
        }:
            raise _validation_problem("Equity ranking query is invalid")
        selected = _snapshot(repository, as_of)
        component = _component(selected, "equity.market-ranking.eod")
        payload = component.payload
        source_rows = _equity_ranking_rows(payload, metric, order)
        rows = _rerank(source_rows)
        page, next_cursor = _page(
            rows,
            cursor=cursor,
            limit=limit,
            data_version=component.data_version,
            fingerprint={"metric": metric, "order": order, "asOf": str(as_of)},
            secret=cursor_secret,
        )
        body = {
            "dataVersion": str(component.data_version),
            "tradeDate": payload["tradeDate"],
            "publishedAt": _timestamp(component.published_at),
            "metric": metric,
            "order": order,
            "source": payload["source"],
            "universe": payload["universe"],
            "coverage": payload["coverage"],
            "finality": payload["finality"],
            "quality": _quality_with_checks(payload["quality"], component.quality),
            "items": page,
            "nextCursor": next_cursor,
        }
        return _conditional_response(
            request=request,
            body=body,
            data_version=component.data_version,
            if_none_match=if_none_match,
            etag_scope={"resource": "equity-rankings", "query": _query_scope(body, cursor)},
        )

    @app.get(
        "/internal/v1/market/money-flow/equity-rankings",
        dependencies=[Depends(require_service_bearer)],
    )
    def list_equity_money_flow_rankings(
        request: Request,
        direction: Annotated[str, Query()],
        as_of: Annotated[date | None, Query(alias="asOf")] = None,
        cursor: Annotated[str | None, Query(max_length=2048)] = None,
        limit: Annotated[int, Query(ge=1, le=50)] = 20,
        if_none_match: Annotated[str | None, Header()] = None,
    ) -> Response:
        """读取订单规模方法学的单侧股票资金流排行，不改写为统一市场事实。"""
        if direction not in {"inflow", "outflow"}:
            raise _validation_problem("Money-flow direction is invalid")
        selected = _snapshot(repository, as_of)
        component = _component(selected, "money-flow.equity-ranking.eod")
        payload = component.payload
        rows = _rerank(list(payload[direction]))
        page, next_cursor = _page(
            rows,
            cursor=cursor,
            limit=limit,
            data_version=component.data_version,
            fingerprint={"direction": direction, "asOf": str(as_of)},
            secret=cursor_secret,
        )
        body = {
            "dataVersion": str(component.data_version),
            "tradeDate": payload["tradeDate"],
            "publishedAt": _timestamp(component.published_at),
            "direction": direction,
            "source": payload["source"],
            "methodology": {
                "id": payload["methodologyId"],
                "version": payload["methodologyVersion"],
                "semanticFamily": "order_size_flow",
                "status": "source_reported",
            },
            "universe": payload["universe"],
            "coverage": payload["coverage"],
            "finality": payload["finality"],
            "quality": payload["quality"],
            "items": page,
            "nextCursor": next_cursor,
        }
        return _conditional_response(
            request=request,
            body=body,
            data_version=component.data_version,
            if_none_match=if_none_match,
            etag_scope={"resource": "equity-money-flow", "query": _query_scope(body, cursor)},
        )

    @app.get(
        "/internal/v1/market/calendar",
        dependencies=[Depends(require_service_bearer)],
    )
    def list_calendar(
        request: Request,
        venues: Annotated[str, Query(min_length=3, max_length=16)],
        start: Annotated[date, Query()],
        end: Annotated[date, Query()],
        if_none_match: Annotated[str | None, Header()] = None,
    ) -> Response:
        """读取来源日历和版本化会话，不按周末规则补造日期。"""
        requested_venues = tuple(part.strip() for part in venues.split(","))
        if (
            not requested_venues
            or len(set(requested_venues)) != len(requested_venues)
            or not set(requested_venues) <= {"SSE", "SZSE"}
            or start > end
            or (end - start).days > 730
        ):
            raise _validation_problem("Calendar query is invalid")
        selected = _snapshot(repository, None)
        component = _component(selected, "market.calendar")
        rows = sorted(
            (
                row
                for row in _records(component.payload)
                if row["venue"] in requested_venues
                and start <= date.fromisoformat(str(row["tradeDate"])) <= end
            ),
            key=lambda row: (str(row["tradeDate"]), str(row["venue"])),
        )
        expected_count = (end - start).days + 1
        if len(rows) != expected_count * len(requested_venues):
            raise _component_unavailable("Calendar publication does not cover the requested range")
        body = {
            "dataVersion": str(component.data_version),
            "publishedAt": _timestamp(component.published_at),
            "timezone": component.payload["timezone"],
            "sessionScheduleVersion": component.payload["sessionScheduleVersion"],
            "source": component.payload["source"],
            "quality": {
                "status": "passed",
                "checks": [
                    _check(
                        "calendar-range-coverage", len(rows), expected_count * len(requested_venues)
                    )
                ],
            },
            "items": rows,
        }
        return _conditional_response(
            request=request,
            body=body,
            data_version=component.data_version,
            if_none_match=if_none_match,
            etag_scope={
                "resource": "calendar",
                "venues": requested_venues,
                "start": str(start),
                "end": str(end),
            },
        )

    @app.get(
        "/internal/v1/market/sectors/strength",
        dependencies=[Depends(require_service_bearer)],
    )
    def list_sector_strength(
        request: Request,
        scheme: Annotated[str, Query()],
        window: Annotated[int, Query()],
        order: Annotated[str, Query()],
        as_of: Annotated[date | None, Query(alias="asOf")] = None,
        cursor: Annotated[str | None, Query(max_length=2048)] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        if_none_match: Annotated[str | None, Header()] = None,
    ) -> Response:
        """读取同步期已物化的板块强弱，不在 API 请求线程跨日派生。"""
        if (
            scheme not in _SECTOR_SCHEMES
            or window not in {1, 5, 20}
            or order
            not in {
                "asc",
                "desc",
            }
        ):
            raise _validation_problem("Sector strength query is invalid")
        selected = _snapshot(repository, as_of)
        component = _component(selected, "sector.strength.eod")
        rows = [
            dict(row)
            for row in _records(component.payload)
            if row["scheme"] == scheme
            and row["window"] == window
            and row.get("availability") == "available"
        ]
        if not rows:
            raise InternalProblem(
                status=503,
                code="dataset-unavailable",
                detail="Sector strength history is not complete for the requested window",
            )
        quality_payload = component.payload.get("quality")
        if not isinstance(quality_payload, dict):
            raise _component_unavailable("Sector strength quality evidence is invalid")
        count_by_scheme = quality_payload.get("validUniverseCountByScheme")
        if not isinstance(count_by_scheme, dict):
            raise _component_unavailable("Sector strength universe evidence is unavailable")
        scheme_counts = count_by_scheme.get(scheme)
        if not isinstance(scheme_counts, dict):
            raise _component_unavailable("Sector strength scheme evidence is unavailable")
        expected_count = scheme_counts.get(str(window))
        if (
            not isinstance(expected_count, int)
            or expected_count < 1
            or len(rows) != expected_count
            or any(
                row.get("validSamples") != window
                or Decimal(str(row.get("coverage"))) != Decimal("1")
                for row in rows
            )
        ):
            raise _component_unavailable(
                "Sector strength complete-window evidence does not reconcile"
            )
        rows.sort(
            key=lambda row: (
                Decimal(str(row["cumulativeReturn"])),
                str(row["sectorCode"]),
            ),
            reverse=order == "desc",
        )
        rows = _rerank(rows)
        page, next_cursor = _page(
            rows,
            cursor=cursor,
            limit=limit,
            data_version=component.data_version,
            fingerprint={"scheme": scheme, "window": window, "order": order, "asOf": str(as_of)},
            secret=cursor_secret,
        )
        input_versions_by_window = component.payload.get("inputDataVersionsByWindow")
        if not isinstance(input_versions_by_window, dict):
            raise _component_unavailable("Sector strength input lineage is unavailable")
        input_versions = input_versions_by_window.get(str(window))
        if (
            not isinstance(input_versions, list)
            or len(input_versions) != window
            or any(not isinstance(value, str) for value in input_versions)
        ):
            raise _component_unavailable(
                "Sector strength input lineage does not match the complete window"
            )
        body = {
            "dataVersion": str(component.data_version),
            "tradeDate": component.payload["tradeDate"],
            "publishedAt": _timestamp(component.published_at),
            "scheme": scheme,
            "window": window,
            "order": order,
            "methodologyVersion": component.payload["methodologyVersion"],
            "source": component.payload["source"],
            "inputDataVersions": input_versions,
            "quality": {
                "status": "passed",
                "validUniverseCount": expected_count,
                "checks": [
                    _check("strength-universe-coverage", len(rows), expected_count),
                    _check("strength-input-publications", len(input_versions), window),
                ],
            },
            "items": [_strength_item(row) for row in page],
            "nextCursor": next_cursor,
        }
        return _conditional_response(
            request=request,
            body=body,
            data_version=component.data_version,
            if_none_match=if_none_match,
            etag_scope={"resource": "sector-strength", "query": _query_scope(body, cursor)},
        )

    @app.get(
        "/internal/v1/market/sectors/money-flow-rankings",
        dependencies=[Depends(require_service_bearer)],
    )
    def list_sector_money_flow_rankings(
        request: Request,
        scheme: Annotated[str, Query()],
        order: Annotated[str, Query()],
        as_of: Annotated[date | None, Query(alias="asOf")] = None,
        cursor: Annotated[str | None, Query(max_length=2048)] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        if_none_match: Annotated[str | None, Header()] = None,
    ) -> Response:
        """读取 DC 板块资金流同 scheme 排行，不以价格强弱代替资金流。"""
        if scheme not in _SECTOR_SCHEMES or order not in {"asc", "desc"}:
            raise _validation_problem("Sector money-flow query is invalid")
        selected = _snapshot(repository, as_of)
        component = _component(selected, "sector.money-flow.dc.eod")
        rows = [dict(row) for row in _records(component.payload) if row["scheme"] == scheme]
        rows.sort(
            key=lambda row: (Decimal(str(row["netAmountCny"])), str(row["sectorCode"])),
            reverse=order == "desc",
        )
        ranked = _rerank(rows)
        page, next_cursor = _page(
            ranked,
            cursor=cursor,
            limit=limit,
            data_version=component.data_version,
            fingerprint={"scheme": scheme, "order": order, "asOf": str(as_of)},
            secret=cursor_secret,
        )
        body = {
            "dataVersion": str(component.data_version),
            "tradeDate": component.payload["tradeDate"],
            "publishedAt": _timestamp(component.published_at),
            "scheme": scheme,
            "order": order,
            "source": _market_source(component),
            "methodology": {
                "id": component.payload["methodologyId"],
                "version": component.payload["methodologyVersion"],
                "semanticFamily": component.payload["semanticFamily"],
                "status": component.payload["methodologyStatus"],
                "rankingBasis": "canonical_net_amount",
            },
            "coverage": "1",
            "finality": "final",
            "quality": {
                "status": "passed",
                "validUniverseCount": len(rows),
                "checks": [_check("sector-money-flow-coverage", len(rows), len(rows))],
            },
            "items": [
                {
                    "rank": row["rank"],
                    "sectorCode": row["sectorCode"],
                    "name": row["name"],
                    "close": row["close"],
                    "changePercent": row.get("changePercent"),
                    "netAmountCny": row["netAmountCny"],
                }
                for row in page
            ],
            "nextCursor": next_cursor,
        }
        return _conditional_response(
            request=request,
            body=body,
            data_version=component.data_version,
            if_none_match=if_none_match,
            etag_scope={"resource": "sector-money-flow", "query": _query_scope(body, cursor)},
        )

    _register_sw_market_routes(
        app,
        repository=repository,
        require_service_bearer=require_service_bearer,
        cursor_secret=cursor_secret,
    )
    _register_sector_compatibility_routes(
        app,
        repository=repository,
        require_service_bearer=require_service_bearer,
        cursor_secret=cursor_secret,
    )
    _register_sw_compatibility_routes(
        app,
        repository=repository,
        require_service_bearer=require_service_bearer,
        cursor_secret=cursor_secret,
    )


def _register_sw_market_routes(
    app: FastAPI,
    *,
    repository: MarketOverviewRepository,
    require_service_bearer: Callable[..., None],
    cursor_secret: bytes,
) -> None:
    """注册申万详情使用的新市场资源路由。"""

    @app.get(
        "/internal/v1/market/industries/sw/{code}/bars",
        dependencies=[Depends(require_service_bearer)],
    )
    def list_sw_bars(
        request: Request,
        code: Annotated[str, Path(pattern=r"^\d{6}\.SI$")],
        period: Annotated[str, Query()] = "1d",
        start: Annotated[date, Query()] = date.min,
        end: Annotated[date, Query()] = date.max,
        cursor: Annotated[str | None, Query(max_length=2048)] = None,
        limit: Annotated[int, Query(ge=1, le=1000)] = 500,
        if_none_match: Annotated[str | None, Header()] = None,
    ) -> Response:
        """读取同步期已发布的申万日周月 bar，reader 只选 revision 不做聚合。"""
        if period not in _PERIODS or start > end:
            raise _validation_problem("SW bar query is invalid")
        dataset = f"sw.bar.{period}"
        components = repository.list_components(dataset_code=dataset, start=None, end=end)
        bars, contributing = _period_records(
            components,
            code_field="code",
            code=code,
            start=start,
            end=end,
        )
        taxonomy = _taxonomy_for(repository, end)
        industry = _industry_or_problem(taxonomy, code)
        data_version = _composite_data_version(
            "sw-bars",
            (*contributing, taxonomy),
        )
        published_at = max(component.published_at for component in (*contributing, taxonomy))
        latest_component = max(
            contributing,
            key=lambda component: component.published_at,
        )
        input_versions = _composite_input_versions(contributing)
        page, next_cursor = _page(
            bars,
            cursor=cursor,
            limit=limit,
            data_version=data_version,
            fingerprint={"code": code, "period": period, "start": str(start), "end": str(end)},
            secret=cursor_secret,
        )
        body = {
            "dataVersion": str(data_version),
            "publishedAt": _timestamp(published_at),
            "industry": _industry_identity(industry, nullable_parent=True),
            "period": period,
            "volumeUnit": "provider_native",
            "source": latest_component.payload["source"],
            "methodology": _sw_bar_methodology(period),
            "inputDataVersions": input_versions,
            "finality": "final",
            "items": [_sw_bar_item(row) for row in page],
            "nextCursor": next_cursor,
        }
        return _conditional_response(
            request=request,
            body=body,
            data_version=data_version,
            if_none_match=if_none_match,
            etag_scope={"resource": "sw-bars", "query": _query_scope(body, cursor)},
        )

    @app.get(
        "/internal/v1/market/industries/sw/{code}/constituents",
        dependencies=[Depends(require_service_bearer)],
    )
    def list_sw_constituents(
        request: Request,
        code: Annotated[str, Path(pattern=r"^\d{6}\.SI$")],
        as_of: Annotated[date | None, Query(alias="asOf")] = None,
        cursor: Annotated[str | None, Query(max_length=2048)] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 100,
        if_none_match: Annotated[str | None, Header()] = None,
    ) -> Response:
        """读取申万正式有效区间在目标日生效的成分，支持任一 taxonomy 层级。"""
        selected = _snapshot(repository, as_of)
        membership = _component(selected, "sw.membership")
        taxonomy = _component(selected, "sw.taxonomy")
        industry = _industry_or_problem(taxonomy, code)
        contributing = (membership, taxonomy)
        data_version = _composite_data_version(
            "sw-constituents",
            contributing,
            scope={"code": code, "asOf": str(as_of)},
        )
        rows = _sw_constituents(
            _records(membership.payload),
            code,
            date.fromisoformat(str(membership.payload["snapshotDate"])),
        )
        page, next_cursor = _page(
            rows,
            cursor=cursor,
            limit=limit,
            data_version=data_version,
            fingerprint={"code": code, "asOf": str(as_of)},
            secret=cursor_secret,
        )
        body = {
            "dataVersion": str(data_version),
            "snapshotDate": membership.payload["snapshotDate"],
            "publishedAt": _timestamp(max(component.published_at for component in contributing)),
            "historyMode": membership.payload["historyMode"],
            "knowledgeCutoff": membership.payload["knowledgeCutoff"],
            "observedAt": membership.source["observedAt"],
            "industry": _industry_identity(industry, nullable_parent=True),
            "source": _market_source(membership),
            "inputDataVersions": _active_component_versions(contributing),
            "methodology": {
                "id": "quant-v2.sw-membership.v1",
                "version": "1",
                "status": "source_reported",
                "temporalSemantics": "latest_revision_effective_interval",
            },
            "items": page,
            "nextCursor": next_cursor,
        }
        return _conditional_response(
            request=request,
            body=body,
            data_version=data_version,
            if_none_match=if_none_match,
            etag_scope={"resource": "sw-constituents", "query": _query_scope(body, cursor)},
        )

    @app.get(
        "/internal/v1/market/industries/sw/{code}/valuation",
        dependencies=[Depends(require_service_bearer)],
    )
    def get_sw_valuation(
        request: Request,
        code: Annotated[str, Path(pattern=r"^\d{6}\.SI$")],
        as_of: Annotated[date | None, Query(alias="asOf")] = None,
        if_none_match: Annotated[str | None, Header()] = None,
    ) -> Response:
        """逐字段投影申万来源估值，缺失 PE_TTM 与股息率明确标记未报告。"""
        selected = _snapshot(repository, as_of)
        market = _component(selected, "sw.market-data")
        taxonomy = _component(selected, "sw.taxonomy")
        industry = _industry_or_problem(taxonomy, code)
        contributing = (market, taxonomy)
        data_version = _composite_data_version(
            "sw-valuation",
            contributing,
            scope={"code": code, "asOf": str(as_of)},
        )
        row = next((item for item in _records(market.payload) if item["code"] == code), None)
        if row is None:
            raise _resource_not_found("SW valuation is not found")
        body = {
            "dataVersion": str(data_version),
            "tradeDate": row["tradeDate"],
            "publishedAt": _timestamp(max(component.published_at for component in contributing)),
            "industry": _industry_identity(industry, nullable_parent=True),
            "source": _market_source(market),
            "inputDataVersions": _active_component_versions(contributing),
            "methodology": {
                "id": "sw-source-reported-valuation",
                "version": "1",
                "owner": "Shenwan",
                "status": "mixed_per_field",
            },
            "valuation": {
                "pe": _valuation_metric(row.get("pe"), "pe"),
                "peTtm": _valuation_metric(None, None),
                "pb": _valuation_metric(row.get("pb"), "pb"),
                "dividendYield": _valuation_metric(None, None),
            },
            "finality": "final",
        }
        return _conditional_response(
            request=request,
            body=body,
            data_version=data_version,
            if_none_match=if_none_match,
            etag_scope={"resource": "sw-valuation", "code": code},
        )


def _snapshot(
    repository: MarketOverviewRepository,
    trade_date: date | None,
) -> _Snapshot:
    """读取一个完整包和其固定组件；缺任一侧都按 publication 不可用失败。"""
    stored = repository.get_snapshot(trade_date=trade_date)
    if stored is None:
        if trade_date is None:
            raise InternalProblem(
                status=503,
                code="dataset-unavailable",
                detail="No complete market publication is currently available",
            )
        raise InternalProblem(
            status=404,
            code="publication-not-found",
            detail="Requested market publication is not found",
        )
    if not stored.components:
        raise _component_unavailable("Market bundle manifest is unavailable")
    return _Snapshot(
        bundle=stored.bundle,
        components={component.dataset_code: component for component in stored.components},
    )


def _component(snapshot: _Snapshot, dataset_code: str) -> StoredMarketComponent:
    """从固定 manifest 读取必需组件，绝不回退到数据集 latest。"""
    component = snapshot.components.get(dataset_code)
    if component is None:
        raise _component_unavailable(f"Required component is unavailable: {dataset_code}")
    return component


def _market_status(
    snapshot: _Snapshot,
    current: datetime,
    *,
    historical: bool = False,
) -> dict[str, Any]:
    """从版本化日历、会话和 EOD eligibility 规则派生状态与 trading-day lag。

    17:20 只表示当天可进入 EOD 候选范围；完整 bundle 的正常计划固定在 19:20，
    以等待 19:00 后更新的个股资金流，不能把 eligibility 解释为 publication 承诺。
    """
    if current.tzinfo is None:
        raise ValueError("market status clock must be timezone-aware")
    if historical:
        snapshot_as_of = datetime.combine(
            snapshot.bundle.trade_date,
            time(15, 0),
            tzinfo=_SHANGHAI,
        )
        return {
            "marketState": "closed",
            "marketStateAsOf": snapshot_as_of.isoformat(),
            "marketStateMethodology": "calendar_schedule_derived",
            "eodEligibilityScheduleVersion": "cn-a-eod-eligibility-2026-v1",
            "freshness": "stale",
            "latestEligibleTradeDate": snapshot.bundle.trade_date.isoformat(),
            "latestAttemptedTradeDate": None,
            "lagTradingDays": 0,
            "freshnessReason": "historical_snapshot",
            "quality": "passed",
        }
    # 分钟桶让同一分钟可 304，同时保证 60 秒轮询跨桶获得新的动态状态实体。
    local = current.astimezone(_SHANGHAI).replace(second=0, microsecond=0)
    calendar = _component(snapshot, "market.calendar")
    rows = _records(calendar.payload)
    common = _common_open_dates(rows)
    today = local.date()
    today_rows = [row for row in rows if row["tradeDate"] == today.isoformat()]
    if {row["venue"] for row in today_rows} != {"SSE", "SZSE"}:
        raise _component_unavailable("Calendar does not cover the current market date")
    is_trading_day = all(row["isTradingDay"] is True for row in today_rows)
    market_state = _session_state(local.timetz().replace(tzinfo=None), is_trading_day)
    eligible_candidates = sorted(value for value in common if value <= today)
    if not eligible_candidates:
        raise _component_unavailable("Calendar has no eligible trading date")
    if is_trading_day and local.timetz().replace(tzinfo=None) < time(17, 20):
        eligible_candidates = [value for value in eligible_candidates if value < today]
    if not eligible_candidates:
        raise _component_unavailable("Calendar has no prior eligible trading date")
    latest_eligible = eligible_candidates[-1]
    if snapshot.bundle.trade_date > latest_eligible:
        raise _component_unavailable("Active bundle is newer than the latest eligible market date")
    lag = len([value for value in common if snapshot.bundle.trade_date < value <= latest_eligible])
    reason = (
        "publication_rollback"
        if snapshot.bundle.active_action == "rollback"
        else ("latest_eligible_complete" if lag == 0 else "latest_eligible_bundle_unavailable")
    )
    freshness = "current" if lag == 0 and reason == "latest_eligible_complete" else "stale"
    return {
        "marketState": market_state,
        "marketStateAsOf": local.isoformat(),
        "marketStateMethodology": "calendar_schedule_derived",
        "eodEligibilityScheduleVersion": "cn-a-eod-eligibility-2026-v1",
        "freshness": freshness,
        "latestEligibleTradeDate": latest_eligible.isoformat(),
        "latestAttemptedTradeDate": None,
        "lagTradingDays": lag,
        "freshnessReason": reason,
        "quality": "passed",
    }


def _session_state(current: time, is_trading_day: bool) -> str:
    """按已发布 A 股现金市场会话边界返回当前交易状态。"""
    if not is_trading_day:
        return "non_trading_day"
    if current < time(9, 30):
        return "pre_open"
    if time(9, 30) <= current < time(11, 30):
        return "trading"
    if time(11, 30) <= current < time(13, 0):
        return "lunch_break"
    if time(13, 0) <= current < time(15, 0):
        return "trading"
    return "closed"


def _common_open_dates(rows: list[dict[str, Any]]) -> set[date]:
    """从日历记录取得 SSE/SZSE 共同开市日期集合。"""
    by_venue: dict[str, set[date]] = {"SSE": set(), "SZSE": set()}
    for row in rows:
        venue = str(row.get("venue"))
        if venue in by_venue and row.get("isTradingDay") is True:
            by_venue[venue].add(date.fromisoformat(str(row["tradeDate"])))
    return by_venue["SSE"] & by_venue["SZSE"]


def _snapshot_date(value: str) -> date | None:
    """解析 latest 或 ISO 日期路径，其他字面量映射为 400。"""
    if value == "latest":
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise _validation_problem("Market snapshot path is invalid") from error


def _as_of_date(value: datetime | None) -> date | None:
    """把带偏移 asOf 转为上海日期；拒绝依赖主机时区解释 naive 时间。"""
    if value is None:
        return None
    if value.tzinfo is None:
        raise _validation_problem("asOf must include an explicit UTC offset")
    return value.astimezone(_SHANGHAI).date()


def _equity_ranking_rows(
    payload: dict[str, Any],
    metric: str,
    order: str,
) -> list[dict[str, Any]]:
    """选择全量排行榜并按请求方向重新稳定排序。"""
    key = {
        ("changePercent", "desc"): "gainers",
        ("changePercent", "asc"): "losers",
        ("amountCny", "desc"): "amount",
        ("amountCny", "asc"): "amount",
        ("turnoverPercent", "desc"): "turnover",
        ("turnoverPercent", "asc"): "turnover",
    }[(metric, order)]
    rows = [dict(row) for row in payload[key]]
    rows.sort(
        key=lambda row: (Decimal(str(row[metric])), f"{row['exchange']}:{row['symbol']}"),
        reverse=order == "desc",
    )
    return rows


def _rerank(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """在最终排序后从一开始重写连续 rank，分页不会复用错误方向的旧名次。"""
    return [{**row, "rank": rank} for rank, row in enumerate(rows, 1)]


def _strength_item(row: dict[str, Any]) -> dict[str, Any]:
    """投影独立强弱页允许公开的字段，移除内部 window 和成分 leader。"""
    return {
        "rank": row["rank"],
        "sectorCode": row["sectorCode"],
        "name": row["name"],
        "changePercent": row["changePercent"],
        "turnoverPercent": row.get("turnoverPercent"),
        "amountCny": row.get("amountCny"),
        "cumulativeReturn": row["cumulativeReturn"],
        "upDays": row["upDays"],
        "medianRank": row.get("medianRank"),
        "validSamples": row["validSamples"],
        "coverage": row["coverage"],
    }


def _period_records(
    components: tuple[StoredMarketComponent, ...],
    *,
    code_field: str,
    code: str,
    start: date,
    end: date,
) -> tuple[list[dict[str, Any]], tuple[StoredMarketComponent, ...]]:
    """选择每个周期键最后一次同步期 revision，不执行 OHLC 聚合。"""
    latest: dict[str, tuple[StoredMarketComponent, dict[str, Any]]] = {}
    for component in components:
        for row in _records(component.payload):
            period_end = date.fromisoformat(str(row["periodEnd"]))
            if (
                row.get(code_field) != code
                or row.get("isFinal") is not True
                or not start <= period_end <= end
            ):
                continue
            latest[str(row["periodKey"])] = (component, row)
    if not latest:
        raise _resource_not_found("Bar publication is not found")
    ordered = sorted(latest.values(), key=lambda item: str(item[1]["periodEnd"]))
    contributing = tuple({component.data_version: component for component, _ in ordered}.values())
    return [dict(row) for _, row in ordered], contributing


def _index_records(
    components: tuple[StoredMarketComponent, ...],
    *,
    index_id: str,
    start: date,
    end: date,
) -> tuple[list[dict[str, Any]], tuple[StoredMarketComponent, ...]]:
    """按交易日选择最新 active 指数组件，历史读取不依赖当前 bundle 的有限回看窗口。"""
    latest: dict[str, tuple[StoredMarketComponent, dict[str, Any]]] = {}
    for component in components:
        for row in _records(component.payload):
            trade_date = date.fromisoformat(str(row["tradeDate"]))
            if row.get("indexId") != index_id or not start <= trade_date <= end:
                continue
            key = str(row["tradeDate"])
            previous = latest.get(key)
            candidate_order = (
                component.trade_date or date.min,
                component.published_at,
                str(component.data_version),
            )
            previous_order = (
                (
                    previous[0].trade_date or date.min,
                    previous[0].published_at,
                    str(previous[0].data_version),
                )
                if previous is not None
                else None
            )
            if previous_order is None or candidate_order > previous_order:
                latest[key] = (component, row)
    if not latest:
        raise _resource_not_found("Index bar publication is not found")
    ordered = sorted(latest.values(), key=lambda item: str(item[1]["tradeDate"]))
    contributing = tuple({component.data_version: component for component, _ in ordered}.values())
    return [dict(row) for _, row in ordered], contributing


def _composite_data_version(
    resource: str,
    components: tuple[StoredMarketComponent, ...],
    *,
    scope: dict[str, Any] | None = None,
) -> UUID:
    """由完整 active component 集合生成稳定 read-publication UUID，防止旧期修订冻结 304。"""
    if not components:
        raise _component_unavailable("Composite publication has no input component")
    identities = sorted(
        (component.dataset_code, str(component.data_version)) for component in components
    )
    return uuid5(
        _MARKET_NAMESPACE,
        json.dumps(
            {"resource": resource, "scope": scope or {}, "components": identities},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
    )


def _active_component_versions(
    components: tuple[StoredMarketComponent, ...],
) -> list[str]:
    """按 publication 日期和数据集稳定排序 active component UUID，并去除重复引用。"""
    versions: list[str] = []
    seen: set[UUID] = set()
    for component in sorted(
        components,
        key=lambda item: (
            item.trade_date or date.min,
            item.dataset_code,
            str(item.data_version),
        ),
    ):
        if component.data_version not in seen:
            seen.add(component.data_version)
            versions.append(str(component.data_version))
    if not versions:
        raise _component_unavailable("Composite publication lineage is empty")
    return versions


def _composite_input_versions(
    components: tuple[StoredMarketComponent, ...],
) -> list[str]:
    """合并每个写时 bar publication 的精确日线输入，保持首次出现顺序并去重。"""
    versions: list[str] = []
    seen: set[str] = set()
    for component in sorted(
        components,
        key=lambda item: (
            item.trade_date or date.min,
            item.dataset_code,
        ),
    ):
        values = component.payload.get("inputDataVersions")
        if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
            raise _component_unavailable("Bar input publication lineage is invalid")
        for value in values:
            if value not in seen:
                seen.add(value)
                versions.append(value)
    if not versions:
        raise _component_unavailable("Bar input publication lineage is empty")
    return versions


def _sw_bar_methodology(period: str) -> dict[str, Any]:
    """按周期返回写时方法学和昨收派生边界，禁止请求时猜测。"""
    daily = period == "1d"
    return {
        "id": ("source-reported-daily-bar" if daily else "calendar-bounded-ohlcv-aggregation"),
        "version": "1",
        "status": "source_reported" if daily else "platform_derived",
        "inputDataset": "sw.market-data",
        "previousClose": {
            "kind": "derived",
            "id": (
                "sw-previous-close-from-close-change"
                if daily
                else "period-opening-previous-close-from-daily"
            ),
            "version": "1",
            "inputs": ["close", "change"] if daily else ["daily.previousClose"],
        },
    }


def _taxonomy_for(
    repository: MarketOverviewRepository,
    end: date,
) -> StoredMarketComponent:
    """选择不晚于查询终点的最新申万 taxonomy publication。"""
    components = repository.list_components(dataset_code="sw.taxonomy", start=None, end=end)
    if not components:
        raise _resource_not_found("SW taxonomy publication is not found")
    return components[-1]


def _industry_or_problem(
    taxonomy: StoredMarketComponent,
    code: str,
) -> dict[str, Any]:
    """在冻结 taxonomy 中解析节点，未知代码返回资源缺失。"""
    row = next((item for item in _records(taxonomy.payload) if item["code"] == code), None)
    if row is None:
        raise _resource_not_found("SW industry is not found")
    return row


def _industry_identity(
    row: dict[str, Any],
    *,
    nullable_parent: bool = False,
) -> dict[str, Any]:
    """投影申万代码、名称、层级和父代码，按目标合同处理根节点空值。"""
    parent = row.get("parentCode")
    result = {
        "code": row["code"],
        "name": row["name"],
        "level": row["level"],
    }
    if parent is not None or nullable_parent:
        result["parentCode"] = parent
    return result


def _sw_constituents(
    rows: list[dict[str, Any]],
    code: str,
    as_of: date,
) -> list[dict[str, Any]]:
    """按 L1/L2/L3 任一代码选择并去重正式成分。"""
    unique: dict[str, dict[str, Any]] = {}
    for row in rows:
        if code not in {row["l1Code"], row["l2Code"], row["l3Code"]}:
            continue
        in_date = date.fromisoformat(str(row["inDate"]))
        out_date = None if row.get("outDate") is None else date.fromisoformat(str(row["outDate"]))
        if not (in_date <= as_of and (out_date is None or as_of < out_date)):
            continue
        exchange, symbol = _equity_identity(str(row["tsCode"]))
        unique[str(row["tsCode"])] = {
            "exchange": exchange,
            "symbol": symbol,
            "name": row["name"],
            "inDate": row.get("inDate"),
            "outDate": row.get("outDate"),
            "isActive": True,
        }
    return [unique[key] for key in sorted(unique)]


def _sw_bar_item(row: dict[str, Any]) -> dict[str, Any]:
    """投影申万 bar；周月数据同样来自同步期已物化 publication。"""
    return {
        "period": row["period"],
        "periodKey": row["periodKey"],
        "periodStart": row["periodStart"],
        "periodEnd": row["periodEnd"],
        "open": row["open"],
        "high": row["high"],
        "low": row["low"],
        "close": row["close"],
        "change": row["change"],
        "changePercent": row["changePercent"],
        "volume": row["volume"],
        "amountCny": row["amountCny"],
        "amplitudePercent": row["amplitudePercent"],
        "turnoverPercent": row["turnoverPercent"],
        "previousClose": row["previousClose"],
        "isFinal": True,
    }


def _valuation_metric(value: object, source_field: str | None) -> dict[str, Any]:
    """把申万逐字段来源可用性投影为自洽 discriminated union。"""
    if value is None or source_field is None:
        return {
            "value": None,
            "availability": "source_not_reported",
            "methodology": None,
        }
    return {
        "value": str(value),
        "availability": "available",
        "methodology": {"kind": "source_reported", "sourceField": source_field},
    }


def _page(
    rows: list[dict[str, Any]],
    *,
    cursor: str | None,
    limit: int,
    data_version: UUID,
    fingerprint: dict[str, Any],
    secret: bytes,
) -> tuple[list[dict[str, Any]], str | None]:
    """使用 HMAC 绑定版本、筛选和 offset，拒绝跨 publication 续页。"""
    request_fingerprint = _digest(fingerprint)
    offset = 0
    if cursor is not None:
        decoded = _decode_cursor(cursor, secret)
        if (
            decoded.get("dataVersion") != str(data_version)
            or decoded.get("request") != request_fingerprint
        ):
            raise _cursor_problem()
        try:
            offset = int(decoded["offset"])
        except (KeyError, ValueError) as error:
            raise _cursor_problem() from error
        if offset < 0 or offset > len(rows):
            raise _cursor_problem()
    values = rows[offset : offset + limit]
    next_offset = offset + len(values)
    next_cursor = (
        _encode_cursor(
            {
                "dataVersion": str(data_version),
                "request": request_fingerprint,
                "offset": str(next_offset),
            },
            secret,
        )
        if next_offset < len(rows)
        else None
    )
    return values, next_cursor


def _encode_cursor(payload: dict[str, str], secret: bytes) -> str:
    """编码并签名游标，正文只含不可变版本、请求摘要和继续位置。"""
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    signature = hmac.new(secret, raw, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(raw + signature).decode().rstrip("=")


def _decode_cursor(value: str, secret: bytes) -> dict[str, str]:
    """验签并限制游标为字符串对象，任何损坏统一映射 409。"""
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        raw, signature = decoded[:-32], decoded[-32:]
        expected = hmac.new(secret, raw, hashlib.sha256).digest()
        payload = json.loads(raw)
    except (ValueError, json.JSONDecodeError) as error:
        raise _cursor_problem() from error
    if (
        not hmac.compare_digest(signature, expected)
        or not isinstance(payload, dict)
        or any(
            not isinstance(key, str) or not isinstance(item, str) for key, item in payload.items()
        )
    ):
        raise _cursor_problem()
    return payload


def _conditional_response(
    *,
    request: Request,
    body: dict[str, Any],
    data_version: UUID,
    if_none_match: str | None,
    etag_scope: dict[str, Any],
) -> Response:
    """返回强 ETag、版本头和 no-stale 缓存语义；304 保留相同版本元数据。"""
    etag = f'"{_digest({"dataVersion": str(data_version), **etag_scope, "body": body})}"'
    headers = {
        "ETag": etag,
        "X-Data-Version": str(data_version),
        "Cache-Control": _PRIVATE_REVALIDATE,
        "X-Request-Id": _request_id(request),
    }
    if if_none_match is not None and hmac.compare_digest(if_none_match, etag):
        return Response(status_code=304, headers=headers)
    return JSONResponse(content=body, headers=headers)


def _query_scope(body: dict[str, Any], cursor: str | None) -> dict[str, Any]:
    """从响应元数据与当前游标形成条件表示范围，避免跨页复用 ETag。"""
    return {
        "dataVersion": body["dataVersion"],
        "nextCursor": body.get("nextCursor"),
        "cursor": cursor,
    }


def _quality_with_checks(
    payload_quality: dict[str, Any],
    component_quality: dict[str, Any],
) -> dict[str, Any]:
    """把组件质量中的字符串检查映射为跨服务可验证的结构化证据。"""
    checks = payload_quality.get("checks", component_quality.get("checks", []))
    structured = [
        item if isinstance(item, dict) else _check(str(item), "passed", "passed") for item in checks
    ]
    return {**payload_quality, "checks": structured}


def _check(code: str, actual: object, expected: object) -> dict[str, str]:
    """构造首页与独立资源共用的通过型质量证据。"""
    return {
        "code": code,
        "status": "passed",
        "actual": str(actual),
        "expected": str(expected),
    }


def _records(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """读取已发布组件记录并在持久化漂移时 fail-closed。"""
    rows = payload.get("records")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise _component_unavailable("Published component records are invalid")
    return rows


def _market_source(component: StoredMarketComponent) -> dict[str, Any]:
    """从长期来源证据中只投影跨服务 market source 六字段。"""
    return {
        key: component.source[key]
        for key in (
            "provider",
            "upstreamSource",
            "sourceDataset",
            "observedAt",
            "adapterVersion",
            "schemaFingerprint",
        )
    }


def _common_market_source(
    components: tuple[StoredMarketComponent, ...],
    *,
    selected: StoredMarketComponent,
) -> dict[str, Any]:
    """校验复合历史页来自同一来源语义，并以最新观测承载公共来源摘要。"""
    comparable_fields = (
        "provider",
        "upstreamSource",
        "sourceDataset",
        "adapterVersion",
        "schemaFingerprint",
    )
    expected = tuple(selected.source[field] for field in comparable_fields)
    if any(
        tuple(component.source[field] for field in comparable_fields) != expected
        for component in components
    ):
        raise _component_unavailable("Composite publication sources are not comparable")
    return _market_source(selected)


def _equity_identity(ts_code: str) -> tuple[str, str]:
    """把 canonical Tushare 代码后缀映射为跨服务交易所身份。"""
    symbol, separator, suffix = ts_code.partition(".")
    if separator != "." or len(symbol) != 6:
        raise _component_unavailable("Published equity identity is invalid")
    exchange = {"SH": "SSE", "SZ": "SZSE", "BJ": "BSE"}.get(suffix)
    if exchange is None:
        raise _component_unavailable("Published equity exchange is invalid")
    return exchange, symbol


def _stable_uuid(kind: str, *parts: str) -> str:
    """为仅内部兼容字段生成稳定 UUID，不替代真实市场标识。"""
    return str(uuid5(_MARKET_NAMESPACE, ":".join((kind, *parts))))


def _timestamp(value: datetime) -> str:
    """将带时区发布时间规范化为 RFC 3339 UTC 字符串。"""
    return value.isoformat().replace("+00:00", "Z")


def _digest(value: object) -> str:
    """计算稳定规范 JSON SHA-256，供请求指纹和强 ETag 共用。"""
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _request_id(request: Request) -> str:
    """复用合法调用方请求标识，缺失时使用不可预测 UUID。"""
    value = request.headers.get("X-Request-Id")
    if value is not None and re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}",
        value,
    ):
        return value
    from uuid import uuid4

    return str(uuid4())


def _validation_problem(detail: str) -> InternalProblem:
    """返回稳定 400 参数问题。"""
    return InternalProblem(status=400, code="validation-error", detail=detail)


def _resource_not_found(detail: str) -> InternalProblem:
    """返回稳定 404 资源问题。"""
    return InternalProblem(status=404, code="resource-not-found", detail=detail)


def _component_unavailable(detail: str) -> InternalProblem:
    """返回必需发布组件缺失的 424，禁止部分结果继续。"""
    return InternalProblem(status=424, code="required-component-unavailable", detail=detail)


def _cursor_problem() -> InternalProblem:
    """返回游标版本、筛选或签名不匹配的稳定 409。"""
    return InternalProblem(
        status=409,
        code="cursor-mismatch",
        detail="Cursor does not match the requested market publication",
    )


# 兼容路由在下方独立定义，确保新旧消费者都读取同一 Tushare publication。
def _register_sector_compatibility_routes(
    app: FastAPI,
    *,
    repository: MarketOverviewRepository,
    require_service_bearer: Callable[..., None],
    cursor_secret: bytes,
) -> None:
    """注册旧 IndustryModule 的东财板块路径，并统一读取 Tushare bundle。"""

    @app.get(
        "/internal/v1/sectors/eod-snapshots",
        dependencies=[Depends(require_service_bearer)],
    )
    def list_eod_snapshots(
        request: Request,
        scheme: Annotated[str, Query()],
        as_of: Annotated[date | None, Query(alias="asOf")] = None,
        sort: Annotated[str, Query()] = "changePercent",
        order: Annotated[str, Query()] = "desc",
        cursor: Annotated[str | None, Query(max_length=1024)] = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
        if_none_match: Annotated[str | None, Header()] = None,
    ) -> Response:
        """从 dc_daily 同日横截面读取一个 scheme 的 EOD 排行。"""
        if (
            scheme not in _SECTOR_SCHEMES
            or sort
            not in {
                "changePercent",
                "turnoverPercent",
                "marketValue",
                "latestValue",
                "advancers",
                "decliners",
                "leaderChangePercent",
                "code",
            }
            or order not in {"asc", "desc"}
        ):
            raise _validation_problem("Sector EOD query is invalid")
        selected = _snapshot(repository, as_of)
        quote = _component(selected, "sector.quote.eod.dc")
        strength = _component(selected, "sector.strength.eod")
        contributing = (quote, strength)
        data_version = _composite_data_version(
            "sector-eod-list",
            contributing,
            scope={
                "scheme": scheme,
                "asOf": str(as_of),
                "sort": sort,
                "order": order,
            },
        )
        published_at = max(component.published_at for component in contributing)
        leaders = _sector_leaders(strength, scheme)
        rows = [
            _sector_eod_item(row, quote, leaders.get(str(row["sectorCode"])))
            for row in _records(quote.payload)
            if row["scheme"] == scheme
        ]
        rows = _sort_sector_eod(rows, sort=sort, order=order)
        ranked = [
            {**row, "rank": position, "position": position} for position, row in enumerate(rows, 1)
        ]
        page, next_cursor = _page(
            ranked,
            cursor=cursor,
            limit=limit,
            data_version=data_version,
            fingerprint={"scheme": scheme, "asOf": str(as_of), "sort": sort, "order": order},
            secret=cursor_secret,
        )
        body = {
            **_sector_eod_metadata(
                quote,
                scheme,
                data_version=data_version,
                published_at=published_at,
                input_versions=_active_component_versions(contributing),
            ),
            "sort": sort,
            "order": order,
            "items": page,
            "nextCursor": next_cursor,
        }
        return _conditional_response(
            request=request,
            body=body,
            data_version=data_version,
            if_none_match=if_none_match,
            etag_scope={"resource": "legacy-sector-eod", "query": _query_scope(body, cursor)},
        )

    @app.get(
        "/internal/v1/sectors",
        dependencies=[Depends(require_service_bearer)],
    )
    def list_sectors(
        request: Request,
        scheme: Annotated[str, Query()],
        query: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
        cursor: Annotated[str | None, Query(max_length=1024)] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 100,
        if_none_match: Annotated[str | None, Header()] = None,
    ) -> Response:
        """从冻结 dc_index publication 分页读取行业或概念目录。"""
        if scheme not in _SECTOR_SCHEMES:
            raise _validation_problem("Sector scheme is invalid")
        selected = _snapshot(repository, None)
        catalog = _component(selected, "sector.catalog.dc")
        normalized_query = None if query is None else query.strip().casefold()
        rows = sorted(
            (
                row
                for row in _records(catalog.payload)
                if row["scheme"] == scheme
                and (
                    normalized_query is None
                    or normalized_query in str(row["name"]).casefold()
                    or normalized_query in str(row["sectorCode"]).casefold()
                )
            ),
            key=lambda row: str(row["sectorCode"]),
        )
        resources = [_sector_identity(row, catalog) for row in rows]
        page, next_cursor = _page(
            resources,
            cursor=cursor,
            limit=limit,
            data_version=catalog.data_version,
            fingerprint={"scheme": scheme, "query": normalized_query},
            secret=cursor_secret,
        )
        body = {
            "items": page,
            "nextCursor": next_cursor,
            "dataVersion": str(catalog.data_version),
            "publishedAt": _timestamp(catalog.published_at),
        }
        return _conditional_response(
            request=request,
            body=body,
            data_version=catalog.data_version,
            if_none_match=if_none_match,
            etag_scope={"resource": "legacy-sector-catalog", "query": _query_scope(body, cursor)},
        )

    @app.get(
        "/internal/v1/sectors/{scheme}/{sector_code}/eod-snapshot",
        dependencies=[Depends(require_service_bearer)],
    )
    def get_eod_snapshot(
        request: Request,
        scheme: str,
        sector_code: Annotated[str, Path(min_length=1, max_length=64)],
        as_of: Annotated[date | None, Query(alias="asOf")] = None,
        if_none_match: Annotated[str | None, Header()] = None,
    ) -> Response:
        """读取一个 dc_daily 板块 EOD 资源。"""
        if scheme not in _SECTOR_SCHEMES:
            raise _validation_problem("Sector scheme is invalid")
        selected = _snapshot(repository, as_of)
        quote = _component(selected, "sector.quote.eod.dc")
        strength = _component(selected, "sector.strength.eod")
        contributing = (quote, strength)
        data_version = _composite_data_version(
            "sector-eod-resource",
            contributing,
            scope={"scheme": scheme, "code": sector_code, "asOf": str(as_of)},
        )
        published_at = max(component.published_at for component in contributing)
        row = _sector_row_or_problem(quote, scheme, sector_code)
        leader = _sector_leaders(strength, scheme).get(sector_code)
        body = {
            **_sector_eod_metadata(
                quote,
                scheme,
                data_version=data_version,
                published_at=published_at,
                input_versions=_active_component_versions(contributing),
            ),
            **_sector_eod_item(row, quote, leader),
        }
        body.pop("rank", None)
        body.pop("position", None)
        return _conditional_response(
            request=request,
            body=body,
            data_version=data_version,
            if_none_match=if_none_match,
            etag_scope={
                "resource": "legacy-sector-eod-resource",
                "scheme": scheme,
                "code": sector_code,
            },
        )

    @app.get(
        "/internal/v1/sectors/{scheme}/{sector_code}/bars",
        dependencies=[Depends(require_service_bearer)],
    )
    def list_sector_bars(
        request: Request,
        scheme: str,
        sector_code: Annotated[str, Path(min_length=1, max_length=64)],
        period: Annotated[str, Query()],
        start: Annotated[date, Query()],
        end: Annotated[date, Query()],
        cursor: Annotated[str | None, Query(max_length=1024)] = None,
        limit: Annotated[int, Query(ge=1, le=1000)] = 1000,
        if_none_match: Annotated[str | None, Header()] = None,
    ) -> Response:
        """读取同步期已物化 dc_daily 日周月正式 bar。"""
        if scheme not in _SECTOR_SCHEMES or period not in _PERIODS or start > end:
            raise _validation_problem("Sector bar query is invalid")
        components = repository.list_components(
            dataset_code=f"sector.bar.{period}.dc",
            start=None,
            end=end,
        )
        bars, contributing = _sector_period_records(
            components,
            scheme=scheme,
            sector_code=sector_code,
            start=start,
            end=end,
        )
        selected = _snapshot(repository, None)
        catalog = _component(selected, "sector.catalog.dc")
        sector = _sector_identity(
            _sector_row_or_problem(catalog, scheme, sector_code),
            catalog,
        )
        data_version = _composite_data_version(
            "sector-bars",
            (*contributing, catalog),
        )
        published_at = max(component.published_at for component in (*contributing, catalog))
        page, next_cursor = _page(
            bars,
            cursor=cursor,
            limit=limit,
            data_version=data_version,
            fingerprint={
                "scheme": scheme,
                "code": sector_code,
                "period": period,
                "start": str(start),
                "end": str(end),
            },
            secret=cursor_secret,
        )
        body = {
            "sector": sector,
            "period": period,
            "dataVersion": str(data_version),
            "publishedAt": _timestamp(published_at),
            "items": [_legacy_sector_bar(row) for row in page],
            "nextCursor": next_cursor,
        }
        return _conditional_response(
            request=request,
            body=body,
            data_version=data_version,
            if_none_match=if_none_match,
            etag_scope={"resource": "legacy-sector-bars", "query": _query_scope(body, cursor)},
        )

    @app.get(
        "/internal/v1/sectors/{scheme}/{sector_code}/constituents",
        dependencies=[Depends(require_service_bearer)],
    )
    def list_sector_constituents(
        request: Request,
        scheme: str,
        sector_code: Annotated[str, Path(min_length=1, max_length=64)],
        as_of: Annotated[datetime | None, Query(alias="asOf")] = None,
        cursor: Annotated[str | None, Query(max_length=1024)] = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 200,
        if_none_match: Annotated[str | None, Header()] = None,
    ) -> Response:
        """读取 dc_member 指定交易日观察，绝不冒充正式调入调出区间。"""
        if scheme not in _SECTOR_SCHEMES:
            raise _validation_problem("Sector scheme is invalid")
        selected = _snapshot(repository, _as_of_date(as_of))
        membership = _component(selected, "sector.membership.dc")
        catalog = _component(selected, "sector.catalog.dc")
        equity_catalog = _component(selected, "equity.catalog")
        suspensions = _component(selected, "equity.suspension.eod")
        contributing = (membership, catalog, equity_catalog, suspensions)
        data_version = _composite_data_version(
            "sector-constituents",
            contributing,
            scope={"scheme": scheme, "code": sector_code, "asOf": str(as_of)},
        )
        published_at = max(component.published_at for component in contributing)
        sector_row = _sector_row_or_problem(catalog, scheme, sector_code)
        observed_at = str(membership.source["observedAt"])
        listing = {str(row["tsCode"]): row for row in _records(equity_catalog.payload)}
        suspended = {
            str(row["tsCode"])
            for row in _records(suspensions.payload)
            if row.get("suspendTiming") in {None, "全天", "全日"}
        }
        rows = []
        for row in _records(membership.payload):
            if row["scheme"] != scheme or row["sectorCode"] != sector_code:
                continue
            exchange, symbol = _equity_identity(str(row["tsCode"]))
            rows.append(
                {
                    "instrumentId": _stable_uuid("equity", exchange, symbol),
                    "exchange": exchange,
                    "symbol": symbol,
                    "name": row["name"],
                    "listingStatus": (
                        "SUSPENDED"
                        if row["tsCode"] in suspended
                        else _legacy_listing_status(listing.get(str(row["tsCode"])))
                    ),
                    "observedFrom": observed_at,
                    "observedTo": None,
                }
            )
        rows.sort(key=lambda row: (str(row["exchange"]), str(row["symbol"])))
        page, next_cursor = _page(
            rows,
            cursor=cursor,
            limit=limit,
            data_version=data_version,
            fingerprint={"scheme": scheme, "code": sector_code, "asOf": str(as_of)},
            secret=cursor_secret,
        )
        body = {
            "sector": _membership_sector(sector_row),
            "release": _membership_release(
                membership,
                requested_as_of=as_of,
                data_version=data_version,
                published_at=published_at,
            ),
            "snapshotObservedAt": observed_at,
            "carriedForward": False,
            "items": page,
            "nextCursor": next_cursor,
        }
        return _conditional_response(
            request=request,
            body=body,
            data_version=data_version,
            if_none_match=if_none_match,
            etag_scope={
                "resource": "legacy-sector-members",
                "query": _query_scope(
                    {"dataVersion": str(data_version), "nextCursor": next_cursor}, cursor
                ),
            },
        )

    @app.get(
        "/internal/v1/equities/{exchange}/{symbol}/sectors",
        dependencies=[Depends(require_service_bearer)],
    )
    def list_equity_sectors(
        request: Request,
        exchange: str,
        symbol: Annotated[str, Path(pattern=r"^\d{6}$")],
        scheme: Annotated[str, Query()],
        as_of: Annotated[datetime | None, Query(alias="asOf")] = None,
        cursor: Annotated[str | None, Query(max_length=1024)] = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 200,
        if_none_match: Annotated[str | None, Header()] = None,
    ) -> Response:
        """反向读取一只证券在同一 dc_member observation 中的板块归属。"""
        if exchange not in {"SSE", "SZSE", "BSE"} or scheme not in _SECTOR_SCHEMES:
            raise _validation_problem("Equity sector query is invalid")
        selected = _snapshot(repository, _as_of_date(as_of))
        membership = _component(selected, "sector.membership.dc")
        catalog = _component(selected, "sector.catalog.dc")
        equity_catalog = _component(selected, "equity.catalog")
        contributing = (membership, catalog, equity_catalog)
        data_version = _composite_data_version(
            "equity-sectors",
            contributing,
            scope={
                "exchange": exchange,
                "symbol": symbol,
                "scheme": scheme,
                "asOf": str(as_of),
            },
        )
        published_at = max(component.published_at for component in contributing)
        suffix = {"SSE": "SH", "SZSE": "SZ", "BSE": "BJ"}[exchange]
        ts_code = f"{symbol}.{suffix}"
        equity_row = next(
            (row for row in _records(equity_catalog.payload) if row["tsCode"] == ts_code),
            None,
        )
        if equity_row is None:
            raise _resource_not_found("Equity is not found")
        catalog_rows = {
            (str(row["scheme"]), str(row["sectorCode"])): row for row in _records(catalog.payload)
        }
        observed_at = str(membership.source["observedAt"])
        rows = []
        for row in _records(membership.payload):
            if row["scheme"] != scheme or row["tsCode"] != ts_code:
                continue
            sector_row = catalog_rows[(scheme, str(row["sectorCode"]))]
            rows.append(
                {
                    "sectorId": _stable_uuid("sector", scheme, str(row["sectorCode"])),
                    "scheme": scheme,
                    "code": row["sectorCode"],
                    "name": sector_row["name"],
                    "observedFrom": observed_at,
                    "observedTo": None,
                    "snapshotObservedAt": observed_at,
                    "carriedForward": False,
                }
            )
        rows.sort(key=lambda row: str(row["code"]))
        page, next_cursor = _page(
            rows,
            cursor=cursor,
            limit=limit,
            data_version=data_version,
            fingerprint={
                "exchange": exchange,
                "symbol": symbol,
                "scheme": scheme,
                "asOf": str(as_of),
            },
            secret=cursor_secret,
        )
        body = {
            "equity": {
                "instrumentId": _stable_uuid("equity", exchange, symbol),
                "exchange": exchange,
                "symbol": symbol,
                "name": equity_row["name"],
                "listingStatus": _legacy_listing_status(equity_row),
            },
            "scheme": scheme,
            "release": _membership_release(
                membership,
                requested_as_of=as_of,
                data_version=data_version,
                published_at=published_at,
            ),
            "items": page,
            "nextCursor": next_cursor,
        }
        return _conditional_response(
            request=request,
            body=body,
            data_version=data_version,
            if_none_match=if_none_match,
            etag_scope={
                "resource": "legacy-equity-sectors",
                "query": _query_scope(
                    {"dataVersion": str(data_version), "nextCursor": next_cursor}, cursor
                ),
            },
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
        """读取一个 dc_index 板块身份。"""
        if scheme not in _SECTOR_SCHEMES:
            raise _validation_problem("Sector scheme is invalid")
        selected = _snapshot(repository, None)
        catalog = _component(selected, "sector.catalog.dc")
        body = _sector_identity(
            _sector_row_or_problem(catalog, scheme, sector_code),
            catalog,
        )
        return _conditional_response(
            request=request,
            body=body,
            data_version=catalog.data_version,
            if_none_match=if_none_match,
            etag_scope={"resource": "legacy-sector", "scheme": scheme, "code": sector_code},
        )


def _sector_leaders(
    component: StoredMarketComponent,
    scheme: str,
) -> dict[str, dict[str, Any] | None]:
    """从同 bundle 强弱组件读取一日领先证券，避免 EOD reader 临时跨表排序。"""
    return {
        str(row["sectorCode"]): row.get("leadingEquity")
        for row in _records(component.payload)
        if row["scheme"] == scheme and row["window"] == 1
    }


def _sector_eod_item(
    row: dict[str, Any],
    component: StoredMarketComponent,
    leader: dict[str, Any] | None,
) -> dict[str, Any]:
    """投影既有 EOD 合同字段，所有数值仍来自同日 dc_index/dc_daily。"""
    return {
        "sectorId": _stable_uuid("sector", str(row["scheme"]), str(row["sectorCode"])),
        "scheme": row["scheme"],
        "code": row["sectorCode"],
        "name": row["name"],
        "latestValue": row["close"],
        "latestValueUnit": "provider_native",
        "changeValue": row["change"],
        "changePercent": row["changePercent"],
        "marketValue": row.get("totalMarketValueCny"),
        "marketValueUnit": "CNY",
        "turnoverPercent": row.get("turnoverPercent"),
        "advancers": row.get("advancing"),
        "decliners": row.get("declining"),
        "leaderName": None if leader is None else leader["name"],
        "leaderChangePercent": (None if leader is None else leader["changePercent"]),
        "rank": None,
        "position": 1,
    }


def _sort_sector_eod(
    rows: list[dict[str, Any]],
    *,
    sort: str,
    order: str,
) -> list[dict[str, Any]]:
    """按冻结排序白名单稳定排列 EOD 行，空值始终置后。"""
    field = {
        "changePercent": "changePercent",
        "turnoverPercent": "turnoverPercent",
        "marketValue": "marketValue",
        "latestValue": "latestValue",
        "advancers": "advancers",
        "decliners": "decliners",
        "leaderChangePercent": "leaderChangePercent",
        "code": "code",
    }[sort]

    def value(row: dict[str, Any]) -> tuple[int, Decimal | str, str]:
        """把一行转换为可比较排序键，并使空值稳定落后。"""
        raw = row.get(field)
        comparable: Decimal | str
        comparable = str(raw) if field == "code" else Decimal(str(raw or "0"))
        return (1 if raw is None else 0, comparable, str(row["code"]))

    present = [row for row in rows if row.get(field) is not None]
    missing = [row for row in rows if row.get(field) is None]
    present.sort(key=value, reverse=order == "desc")
    missing.sort(key=lambda row: str(row["code"]))
    return present + missing


def _sector_eod_metadata(
    component: StoredMarketComponent,
    scheme: str,
    *,
    data_version: UUID,
    published_at: datetime,
    input_versions: list[str],
) -> dict[str, Any]:
    """构造报价与领先证券强弱组件共同约束的 EOD publication 元数据。"""
    observed_at = str(component.source["observedAt"])
    return {
        "scheme": scheme,
        "tradeDate": component.payload["tradeDate"],
        "sourceCutoffAt": observed_at,
        "observedAt": observed_at,
        "finality": "post_close_observation",
        "qualityStatus": "passed",
        "dataVersion": str(data_version),
        "publishedAt": _timestamp(published_at),
        "inputDataVersions": input_versions,
    }


def _sector_identity(
    row: dict[str, Any],
    component: StoredMarketComponent,
) -> dict[str, Any]:
    """投影既有板块身份合同，并以 canonical scheme/code 生成内部稳定 UUID。"""
    return {
        "sectorId": _stable_uuid("sector", str(row["scheme"]), str(row["sectorCode"])),
        "scheme": row["scheme"],
        "code": row["sectorCode"],
        "name": row["name"],
        "dataVersion": str(component.data_version),
        "publishedAt": _timestamp(component.published_at),
    }


def _membership_sector(row: dict[str, Any]) -> dict[str, Any]:
    """投影成分合同中的板块身份。"""
    return {
        "sectorId": _stable_uuid("sector", str(row["scheme"]), str(row["sectorCode"])),
        "scheme": row["scheme"],
        "code": row["sectorCode"],
        "name": row["name"],
    }


def _sector_row_or_problem(
    component: StoredMarketComponent,
    scheme: str,
    sector_code: str,
) -> dict[str, Any]:
    """在一个冻结组件中解析板块，未知身份返回 404。"""
    row = next(
        (
            item
            for item in _records(component.payload)
            if item["scheme"] == scheme and item["sectorCode"] == sector_code
        ),
        None,
    )
    if row is None:
        raise _resource_not_found("Sector is not found")
    return row


def _sector_period_records(
    components: tuple[StoredMarketComponent, ...],
    *,
    scheme: str,
    sector_code: str,
    start: date,
    end: date,
) -> tuple[list[dict[str, Any]], tuple[StoredMarketComponent, ...]]:
    """选择板块每个周期键最后一条同步期 final revision，不做请求时聚合。"""
    latest: dict[str, tuple[StoredMarketComponent, dict[str, Any]]] = {}
    for component in components:
        for row in _records(component.payload):
            period_end = date.fromisoformat(str(row["periodEnd"]))
            if (
                row.get("scheme") != scheme
                or row.get("sectorCode") != sector_code
                or row.get("isFinal") is not True
                or not start <= period_end <= end
            ):
                continue
            latest[str(row["periodKey"])] = (component, row)
    if not latest:
        raise _resource_not_found("Sector bar publication is not found")
    ordered = sorted(latest.values(), key=lambda item: str(item[1]["periodEnd"]))
    contributing = tuple({component.data_version: component for component, _ in ordered}.values())
    return [dict(row) for _, row in ordered], contributing


def _legacy_sector_bar(row: dict[str, Any]) -> dict[str, Any]:
    """投影既有 K 线合同；来源空 volume/amount 保持 null，不补零。"""
    return {
        "periodEnd": row["periodEnd"],
        "open": row["open"],
        "high": row["high"],
        "low": row["low"],
        "close": row["close"],
        "volumeValue": row.get("volume"),
        "volumeUnit": "provider_native",
        "amountCny": row.get("amountCny"),
        "amplitudePercent": row.get("amplitudePercent"),
        "changePercent": row.get("changePercent"),
        "changeAmount": row.get("change"),
        "turnoverPercent": row.get("turnoverPercent"),
        "isFinal": True,
    }


def _legacy_listing_status(row: dict[str, Any] | None) -> str:
    """把 Tushare 上市状态映射为既有三态；未知身份不伪装为上市。"""
    if row is None:
        raise _component_unavailable("Sector membership equity identity is unresolved")
    status = row.get("listStatus")
    if status in {"L", "P"}:
        return "LISTED"
    if status == "D":
        return "DELISTED"
    raise _component_unavailable("Equity listing status is unsupported")


def _membership_release(
    component: StoredMarketComponent,
    *,
    requested_as_of: datetime | None,
    data_version: UUID,
    published_at: datetime,
) -> dict[str, Any]:
    """构造复合 observation release，明确其不是正式有效区间。"""
    observed_at = str(component.source["observedAt"])
    return {
        "requestedAsOf": (None if requested_as_of is None else requested_as_of.isoformat()),
        "resolvedAsOf": observed_at,
        "coverageStart": observed_at,
        "membershipSemantics": "observed",
        "qualityStatus": "passed",
        "identityCoveragePercent": "100",
        "excludedIdentityCount": 0,
        "carriedForwardSectorCount": 0,
        "dataVersion": str(data_version),
        "publishedAt": _timestamp(published_at),
    }


def _register_sw_compatibility_routes(
    app: FastAPI,
    *,
    repository: MarketOverviewRepository,
    require_service_bearer: Callable[..., None],
    cursor_secret: bytes,
) -> None:
    """注册旧 IndustryModule 的申万路径，并统一读取 Tushare publication。"""

    @app.get(
        "/internal/v1/sw-industries/valuations",
        dependencies=[Depends(require_service_bearer)],
    )
    def list_valuations(
        request: Request,
        snapshot_date: Annotated[date | None, Query(alias="snapshotDate")] = None,
        level: Annotated[int | None, Query(ge=1, le=3)] = None,
        cursor: Annotated[str | None, Query(max_length=1024)] = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
        if_none_match: Annotated[str | None, Header()] = None,
    ) -> Response:
        """分页读取 sw_daily source-reported PE/PB 观察。"""
        selected = _snapshot(repository, snapshot_date)
        taxonomy = _component(selected, "sw.taxonomy")
        market = _component(selected, "sw.market-data")
        membership = _component(selected, "sw.membership")
        contributing = (market, taxonomy, membership)
        data_version = _composite_data_version(
            "legacy-sw-valuations",
            contributing,
            scope={"snapshotDate": str(snapshot_date), "level": level},
        )
        published_at = max(component.published_at for component in contributing)
        taxonomy_by_code = {str(row["code"]): row for row in _records(taxonomy.payload)}
        counts = _sw_component_counts(
            _records(membership.payload),
            date.fromisoformat(str(membership.payload["snapshotDate"])),
        )
        rows = []
        for row in _records(market.payload):
            node = taxonomy_by_code.get(str(row["code"]))
            if node is None or (level is not None and node["level"] != level):
                continue
            rows.append(
                {
                    **_legacy_sw_node(node, counts),
                    "snapshotDate": row["tradeDate"],
                    "staticPe": row.get("pe"),
                    "ttmPe": None,
                    "pb": row.get("pb"),
                    "dividendYieldRatio": None,
                    "finality": "PROVIDER_OBSERVATION",
                    "valuationRevision": 1,
                }
            )
        rows.sort(key=lambda row: str(row["code"]))
        page, next_cursor = _page(
            rows,
            cursor=cursor,
            limit=limit,
            data_version=data_version,
            fingerprint={"snapshotDate": str(snapshot_date), "level": level},
            secret=cursor_secret,
        )
        body = {
            "scheme": "sw.industry",
            "release": _legacy_sw_release(
                market,
                snapshot_date=selected.bundle.trade_date,
                row_count=len(rows),
                methodology_code="sw-source-reported-valuation",
                data_version=data_version,
                published_at=published_at,
            ),
            "items": page,
            "nextCursor": next_cursor,
        }
        return _conditional_response(
            request=request,
            body=body,
            data_version=data_version,
            if_none_match=if_none_match,
            etag_scope={
                "resource": "legacy-sw-valuations",
                "query": _query_scope(
                    {"dataVersion": str(data_version), "nextCursor": next_cursor}, cursor
                ),
            },
        )

    @app.get(
        "/internal/v1/sw-industries",
        dependencies=[Depends(require_service_bearer)],
    )
    def list_nodes(
        request: Request,
        snapshot_date: Annotated[date | None, Query(alias="snapshotDate")] = None,
        level: Annotated[int | None, Query(ge=1, le=3)] = None,
        parent_code: Annotated[
            str | None, Query(alias="parentCode", pattern=r"^\d{6}\.SI$")
        ] = None,
        cursor: Annotated[str | None, Query(max_length=1024)] = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
        if_none_match: Annotated[str | None, Header()] = None,
    ) -> Response:
        """分页读取 index_classify SW2021 一至三级 taxonomy。"""
        selected = _snapshot(repository, snapshot_date)
        taxonomy = _component(selected, "sw.taxonomy")
        membership = _component(selected, "sw.membership")
        contributing = (taxonomy, membership)
        data_version = _composite_data_version(
            "legacy-sw-taxonomy",
            contributing,
            scope={
                "snapshotDate": str(snapshot_date),
                "level": level,
                "parentCode": parent_code,
            },
        )
        published_at = max(component.published_at for component in contributing)
        counts = _sw_component_counts(
            _records(membership.payload),
            date.fromisoformat(str(membership.payload["snapshotDate"])),
        )
        rows = [
            _legacy_sw_node(row, counts)
            for row in _records(taxonomy.payload)
            if (level is None or row["level"] == level)
            and (parent_code is None or row.get("parentCode") == parent_code)
        ]
        rows.sort(key=lambda row: (int(row["level"]), str(row["code"])))
        page, next_cursor = _page(
            rows,
            cursor=cursor,
            limit=limit,
            data_version=data_version,
            fingerprint={
                "snapshotDate": str(snapshot_date),
                "level": level,
                "parentCode": parent_code,
            },
            secret=cursor_secret,
        )
        body = {
            "scheme": "sw.industry",
            "release": _legacy_sw_release(
                taxonomy,
                snapshot_date=selected.bundle.trade_date,
                row_count=len(_records(taxonomy.payload)),
                methodology_code="sw2021-taxonomy",
                data_version=data_version,
                published_at=published_at,
            ),
            "items": page,
            "nextCursor": next_cursor,
        }
        return _conditional_response(
            request=request,
            body=body,
            data_version=data_version,
            if_none_match=if_none_match,
            etag_scope={
                "resource": "legacy-sw-taxonomy",
                "query": _query_scope(
                    {"dataVersion": str(data_version), "nextCursor": next_cursor}, cursor
                ),
            },
        )

    @app.get(
        "/internal/v1/sw-industries/{sector_code}",
        dependencies=[Depends(require_service_bearer)],
    )
    def get_node(
        request: Request,
        sector_code: Annotated[str, Path(pattern=r"^\d{6}\.SI$")],
        snapshot_date: Annotated[date | None, Query(alias="snapshotDate")] = None,
        if_none_match: Annotated[str | None, Header()] = None,
    ) -> Response:
        """读取一个 SW 节点及同一 taxonomy publication 中的祖先闭包。"""
        selected = _snapshot(repository, snapshot_date)
        taxonomy = _component(selected, "sw.taxonomy")
        membership = _component(selected, "sw.membership")
        contributing = (taxonomy, membership)
        data_version = _composite_data_version(
            "legacy-sw-node",
            contributing,
            scope={"snapshotDate": str(snapshot_date), "code": sector_code},
        )
        published_at = max(component.published_at for component in contributing)
        counts = _sw_component_counts(
            _records(membership.payload),
            date.fromisoformat(str(membership.payload["snapshotDate"])),
        )
        rows = {str(row["code"]): row for row in _records(taxonomy.payload)}
        node = rows.get(sector_code)
        if node is None:
            raise _resource_not_found("SW industry is not found")
        ancestors: list[dict[str, Any]] = []
        parent = node.get("parentCode")
        while parent is not None:
            ancestor = rows.get(str(parent))
            if ancestor is None:
                raise _component_unavailable("SW taxonomy parent closure is incomplete")
            ancestors.append(_legacy_sw_node(ancestor, counts))
            parent = ancestor.get("parentCode")
        ancestors.reverse()
        body = {
            "scheme": "sw.industry",
            "release": _legacy_sw_release(
                taxonomy,
                snapshot_date=selected.bundle.trade_date,
                row_count=len(rows),
                methodology_code="sw2021-taxonomy",
                data_version=data_version,
                published_at=published_at,
            ),
            "industry": _legacy_sw_node(node, counts),
            "ancestors": ancestors,
        }
        return _conditional_response(
            request=request,
            body=body,
            data_version=data_version,
            if_none_match=if_none_match,
            etag_scope={"resource": "legacy-sw-node", "code": sector_code},
        )


def _sw_component_counts(
    rows: list[dict[str, Any]],
    snapshot_date: date,
) -> dict[str, int]:
    """按正式有效区间计算各 L1/L2/L3 节点成分数。"""
    members: dict[str, set[str]] = {}
    for row in rows:
        start = date.fromisoformat(str(row["inDate"]))
        end = None if row.get("outDate") is None else date.fromisoformat(str(row["outDate"]))
        if not (start <= snapshot_date and (end is None or snapshot_date < end)):
            continue
        for field in ("l1Code", "l2Code", "l3Code"):
            members.setdefault(str(row[field]), set()).add(str(row["tsCode"]))
    return {code: len(values) for code, values in members.items()}


def _legacy_sw_node(
    row: dict[str, Any],
    component_counts: dict[str, int],
) -> dict[str, Any]:
    """投影既有 SW 节点合同。"""
    return {
        "code": row["code"],
        "name": row["name"],
        "level": row["level"],
        "parentCode": row.get("parentCode"),
        "componentCount": component_counts.get(str(row["code"]), 0),
        "revision": 1,
    }


def _legacy_sw_release(
    component: StoredMarketComponent,
    *,
    snapshot_date: date,
    row_count: int,
    methodology_code: str,
    data_version: UUID,
    published_at: datetime,
) -> dict[str, Any]:
    """构造复合 SW release，并保留主要事实组件的真实上游与 schema 指纹。"""
    if row_count < 1:
        raise _component_unavailable("SW publication has no visible rows")
    return {
        "snapshotDate": snapshot_date.isoformat(),
        "dataVersion": str(data_version),
        "publishedAt": _timestamp(published_at),
        "qualityStatus": "passed",
        "rowCount": row_count,
        "methodology": {
            "code": methodology_code,
            "version": 1,
            "status": "source_reported",
            "upstreamSource": component.source["upstreamSource"],
            "semanticSpecSha256": component.source["schemaFingerprint"],
        },
    }


__all__ = ["register_market_overview_routes"]
