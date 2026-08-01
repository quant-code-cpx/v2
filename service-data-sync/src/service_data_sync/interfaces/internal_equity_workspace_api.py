"""股票中心冻结发现、统一事件与独立数据状态的内部 `POST` reader。"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from fastapi import Depends, FastAPI, Header, Path, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import and_, exists, false, func, or_, select
from sqlalchemy.orm import load_only

from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.database.models.canonical import (
    DatasetRelease,
    MethodologyVersion,
)
from service_data_sync.infrastructure.database.models.equity.identity.equity_identifier_version import (  # noqa: E501
    EquityIdentifierVersion,
)
from service_data_sync.infrastructure.database.models.equity.identity.equity_instrument import (
    EquityInstrument,
)
from service_data_sync.infrastructure.database.models.equity.identity.equity_name_version import (
    EquityNameVersion,
)
from service_data_sync.infrastructure.database.models.equity.market_data.equity_corporate_action_version import (  # noqa: E501
    EquityCorporateActionVersion,
)
from service_data_sync.infrastructure.database.models.equity.workspace import (
    EquityDiscoveryAvailability,
    EquityDiscoveryMembership,
    EquityDiscoverySnapshot,
    SwMembershipItem,
    SwMembershipRelease,
)
from service_data_sync.infrastructure.database.models.financial.financial_methodology import (
    FinancialMethodology,
)
from service_data_sync.infrastructure.database.models.financial.financial_publication import (
    FinancialPublication,
)
from service_data_sync.infrastructure.database.models.market import (
    BlockTradeExecutionRevision,
    CorporateEarningsValue,
    CorporateEvent,
    CorporateEventRevision,
    DisclosureDocument,
    DragonTigerEventRevision,
)
from service_data_sync.infrastructure.database.models.money_flow.money_flow_methodology import (
    MoneyFlowMethodology,
)
from service_data_sync.infrastructure.database.models.money_flow.money_flow_methodology_version import (  # noqa: E501
    MoneyFlowMethodologyVersion,
)
from service_data_sync.infrastructure.database.models.money_flow.money_flow_series import (
    MoneyFlowSeries,
)
from service_data_sync.infrastructure.database.models.provenance.source_batch import SourceBatch
from service_data_sync.infrastructure.database.models.publication.dataset_publication import (
    DatasetPublication,
)
from service_data_sync.infrastructure.database.models.publication.equity_event_window_coverage import (  # noqa: E501
    EquityEventWindowCoverage,
)
from service_data_sync.infrastructure.database.models.sector.membership.sector_membership_item import (  # noqa: E501
    SectorMembershipItem,
)
from service_data_sync.infrastructure.database.models.sector.membership.sector_membership_release import (  # noqa: E501
    SectorMembershipRelease,
)
from service_data_sync.infrastructure.database.models.sector.membership.sector_membership_release_sector import (  # noqa: E501
    SectorMembershipReleaseSector,
)
from service_data_sync.interfaces.internal_sector_api import InternalProblem

_DISCOVERY_DATASET = "equity.discovery.eod"
_DISCOVERY_PARTITION = "CN_A"
_PRIVATE_REVALIDATE = "private, max-age=0, must-revalidate"
_EXCHANGES = frozenset({"SSE", "SZSE", "BSE"})
_LIFECYCLE = frozenset({"LISTED", "SUSPENDED", "DELISTED"})
_TRADING = frozenset(
    {
        "TRADED",
        "TRADE_SUSPENDED",
        "UNKNOWN",
        "NO_SESSION",
        "NOT_APPLICABLE",
    }
)
_COLUMNS = (
    "symbol",
    "name",
    "exchange",
    "listingStatus",
    "tradingStatus",
    "tradeDate",
    "close",
    "previousClose",
    "changeAmount",
    "changePercent",
    "volumeShares",
    "amountCny",
    "turnoverRate",
    "totalShares",
    "listedTradableAShares",
    "totalMarketCapCny",
    "floatMarketCapCny",
    "peTtm",
    "pb",
    "psTtm",
    "memberships",
)
_SORT_FIELDS = (
    "symbol",
    "name",
    "close",
    "changePercent",
    "amountCny",
    "turnoverRate",
    "totalMarketCap",
    "floatMarketCap",
    "peTtm",
    "pb",
)
_SORT_ATTRIBUTES = {
    "symbol": "symbol",
    "name": "name",
    "close": "close_price",
    "changePercent": "change_percent",
    "amountCny": "amount_cny",
    "turnoverRate": "turnover_rate",
    "totalMarketCap": "total_market_cap_cny",
    "floatMarketCap": "float_market_cap_cny",
    "peTtm": "pe_ttm",
    "pb": "pb",
}
_COLUMN_ATTRIBUTES = {
    "tradeDate": "trade_date",
    "close": "close_price",
    "previousClose": "previous_close_price",
    "changeAmount": "change_amount",
    "changePercent": "change_percent",
    "volumeShares": "volume_shares",
    "amountCny": "amount_cny",
    "turnoverRate": "turnover_rate",
    "totalShares": "total_shares",
    "listedTradableAShares": "listed_tradable_a_shares",
    "totalMarketCapCny": "total_market_cap_cny",
    "floatMarketCapCny": "float_market_cap_cny",
    "peTtm": "pe_ttm",
    "pb": "pb",
    "psTtm": "ps_ttm",
    "moneyFlowNetAmount": "money_flow_net_amount_cny",
    "moneyFlowNetRatio": "money_flow_net_ratio",
}
_SCHEME_FROM_API = {
    "EASTMONEY_INDUSTRY": "eastmoney.industry",
    "EASTMONEY_CONCEPT": "eastmoney.concept",
    "SW2021_L1": "sw.industry",
    "SW2021_L2": "sw.industry",
    "SW2021_L3": "sw.industry",
}
_SCHEME_TO_API = {
    "eastmoney.industry": "EASTMONEY_INDUSTRY",
    "eastmoney.concept": "EASTMONEY_CONCEPT",
}
_EVENT_FAMILIES = frozenset(
    {
        "CORPORATE_ACTION",
        "EARNINGS_FORECAST",
        "EARNINGS_EXPRESS",
        "DRAGON_TIGER",
        "BLOCK_TRADE",
    }
)
_EVENT_DATASETS = {
    "CORPORATE_ACTION": "equity.corporate_action",
    "EARNINGS_FORECAST": "equity.corporate_event.earnings.reported",
    "EARNINGS_EXPRESS": "equity.corporate_event.earnings.reported",
    "DRAGON_TIGER": "equity.dragon_tiger.disclosure.reported",
    "BLOCK_TRADE": "equity.block_trade.execution.reported",
}
_STATUS_DATASETS = {
    "IDENTITY": "equity.master.catalog",
    "COMPANY_PROFILE": "equity.profile",
    "BARS_1D": "equity.bar.1d.raw",
    "BARS_1W": "equity.bar.1w.raw",
    "BARS_1MO": "equity.bar.1mo.raw",
    "ADJUSTMENT_FACTOR": "equity.adjustment_factor",
    "CORPORATE_ACTION": "equity.corporate_action",
    "FINANCIAL_REPORT": "financial.report",
    "FINANCIAL_INDICATOR": "financial.metric",
    "VALUATION": "financial.valuation",
    "MONEY_FLOW": "money_flow.daily",
    "INDUSTRY_MEMBERSHIP": "sector.membership.release",
    "CONCEPT_MEMBERSHIP": "sector.membership.release",
    "SW_INDUSTRY_MEMBERSHIP": "sector.sw2021.membership.snapshot",
    "EARNINGS_FORECAST": "equity.corporate_event.earnings.reported",
    "EARNINGS_EXPRESS": "equity.corporate_event.earnings.reported",
    "DRAGON_TIGER": "equity.dragon_tiger.disclosure.reported",
    "BLOCK_TRADE": "equity.block_trade.execution.reported",
}


@dataclass(frozen=True, slots=True)
class _EventCoverageCandidate:
    """组合一条窗口证据及其冻结 publication、方法学与真实来源身份。"""

    coverage: EquityEventWindowCoverage
    publication: DatasetPublication
    methodology: MethodologyVersion
    source_batch: SourceBatch


@dataclass(frozen=True, slots=True)
class _EventCoverageSlice:
    """把请求窗口切成由同一覆盖观察证明、采用同一知识截止点的非重叠日期片。"""

    coverage_from: date
    coverage_to: date
    evidence: _EventCoverageCandidate


@dataclass(frozen=True, slots=True)
class _EventCoverageSelection:
    """表示一个事件族可连续证明的窗口和唯一安全累计知识视图。"""

    family: str
    dataset: str
    security_id: int
    identifier_version_id: UUID
    coverage_from: date
    coverage_to: date
    segments: tuple[_EventCoverageSlice, ...]
    view_cutoff: datetime
    data_version: UUID
    source_label: str
    methodology_code: str
    methodology_version: int


_PER_SECURITY_DATASETS = frozenset(
    {
        "equity.profile",
        "equity.bar.1d.raw",
        "equity.bar.1w.raw",
        "equity.bar.1mo.raw",
        "equity.adjustment_factor",
        "equity.corporate_action",
    }
)
_MARKET_COLUMNS = frozenset(
    {
        "tradeDate",
        "close",
        "previousClose",
        "changeAmount",
        "changePercent",
        "volumeShares",
        "amountCny",
        "turnoverRate",
    }
)
_CAPITALIZATION_COLUMNS = frozenset(
    {
        "totalShares",
        "listedTradableAShares",
        "totalMarketCapCny",
        "floatMarketCapCny",
    }
)
_VALUATION_COLUMNS = frozenset({"peTtm", "pb", "psTtm"})
_MONEY_FLOW_COLUMNS = frozenset({"moneyFlowNetAmount", "moneyFlowNetRatio"})


def register_equity_workspace_routes(
    app: FastAPI,
    *,
    database: DatabaseClient,
    require_service_bearer: Callable[..., None],
    cursor_secret: bytes,
) -> None:
    """挂载三条只读 `POST` 路由，所有事实均绑定 immutable publication。"""

    @app.post(
        "/internal/v1/equity-discovery/query",
        dependencies=[Depends(require_service_bearer)],
    )
    def query_equity_discovery(
        request: Request,
        body: dict[str, Any],
        if_none_match: str | None = Header(default=None, max_length=256),
    ) -> Response:
        """在固定 discovery release 内执行搜索、筛选、排序与 keyset 分页。"""
        normalized = _validate_search(body)
        with database.session() as session:
            publication = _discovery_publication(
                session,
                data_version=normalized.get("dataVersion"),
            )
            if publication.effective_as_of is None:
                raise _publication_unavailable(
                    "Equity discovery publication has no effective business date"
                )
            statement = select(EquityDiscoverySnapshot).where(
                EquityDiscoverySnapshot.release_id == publication.release_id
            )
            statement = _apply_discovery_filters(statement, normalized)
            sort_spec = normalized["sort"]
            scope = _scope({key: value for key, value in normalized.items() if key != "cursor"})
            statement = _apply_discovery_cursor(
                session,
                statement=statement,
                release_id=UUID(str(publication.release_id)),
                cursor=normalized.get("cursor"),
                sort_spec=sort_spec,
                secret=cursor_secret,
                scope=scope,
                data_version=UUID(str(publication.data_version)),
            )
            limit = int(normalized["limit"])
            selected_columns = frozenset(normalized["columns"])
            statement = statement.options(
                load_only(*_discovery_load_attributes(selected_columns, sort_spec))
            ).order_by(*_discovery_order_by(sort_spec))
            fetched_rows = list(session.scalars(statement.limit(limit + 1)).all())
            page_rows = fetched_rows[:limit]
            next_cursor = _next_cursor(
                has_more=len(fetched_rows) > limit,
                page_rows=page_rows,
                secret=cursor_secret,
                scope=scope,
                data_version=UUID(str(publication.data_version)),
            )
            security_ids = tuple(row.security_id for row in page_rows)
            memberships = (
                _memberships(
                    session,
                    release_id=UUID(str(publication.release_id)),
                    security_ids=security_ids,
                )
                if "memberships" in selected_columns
                else {}
            )
            component_families = _component_families(selected_columns)
            availability = _availability_by_security(
                session,
                release_id=UUID(str(publication.release_id)),
                security_ids=security_ids,
                families=component_families,
            )
            components = _components(
                session,
                release_id=UUID(str(publication.release_id)),
                families=component_families,
            )
            completeness = "FULL" if publication.quality_status == "passed" else "PARTIAL"
            response_body = {
                "availability": "AVAILABLE",
                "reasonCode": None,
                "release": _release(publication, completeness=completeness),
                "components": components,
                "capabilities": {
                    "sortFields": list(_SORT_FIELDS),
                    "columns": list(normalized["columns"]),
                    "maxLimit": 100,
                },
                "records": [
                    _discovery_record(
                        row,
                        identity_as_of=_identity_as_of(
                            row,
                            snapshot_as_of=publication.effective_as_of,
                        ),
                        columns=selected_columns,
                        memberships=memberships.get(row.security_id, ()),
                        availability=availability.get(row.security_id, {}),
                    )
                    for row in page_rows
                ],
                "page": {"nextCursor": next_cursor, "limit": limit},
            }
        return _conditional_response(
            request=request,
            if_none_match=if_none_match,
            data_version=UUID(str(publication.data_version)),
            etag=_etag("equity-discovery", publication.data_version, normalized),
            body=response_body,
        )

    @app.post(
        "/internal/v1/equities/{exchange}/{symbol}/events/query",
        dependencies=[Depends(require_service_bearer)],
    )
    def query_equity_events(
        request: Request,
        body: dict[str, Any],
        exchange: str = Path(pattern=r"^(SSE|SZSE|BSE)$"),
        symbol: str = Path(pattern=r"^[0-9]{6}$"),
        if_none_match: str | None = Header(default=None, max_length=256),
    ) -> Response:
        """统一读取公司行动、业绩、龙虎榜和大宗交易当前知识版本。"""
        normalized = _validate_events(body)
        with database.session() as session:
            identity, identifier = _identity_for_event_window(
                session,
                exchange=exchange,
                symbol=symbol,
                request=normalized,
            )
            coverages = _event_coverages(
                session,
                security_id=identity.security_id,
                identifier_version_id=identifier.version_id,
                request=normalized,
            )
            events = _event_rows(
                session,
                security_id=identity.security_id,
                request=normalized,
                coverages=coverages,
            )
            requested = set(normalized["families"])
            available = set(coverages)
            if not available:
                raise _publication_unavailable(
                    "Requested equity event families have no complete coverage"
                )
            events.sort(
                key=_event_sort_key,
                reverse=True,
            )
            event_release = _event_release(
                coverages,
                requested=requested,
                requested_end=normalized["end"],
            )
            event_data_version = UUID(str(event_release["dataVersion"]))
            scope = _scope(
                {
                    "request": {key: value for key, value in normalized.items() if key != "cursor"},
                    "exchange": exchange,
                    "symbol": symbol,
                    "identityVersion": str(identifier.version_id),
                    "securityAnchor": identity.security_id,
                }
            )
            start = _event_cursor_start(
                events,
                normalized.get("cursor"),
                secret=cursor_secret,
                scope=scope,
                data_version=event_data_version,
            )
            limit = int(normalized["limit"])
            page = events[start : start + limit]
            next_cursor = _event_next_cursor(
                events,
                start=start,
                page=page,
                limit=limit,
                secret=cursor_secret,
                scope=scope,
                data_version=event_data_version,
            )
            response_body = {
                "availability": "AVAILABLE",
                "reasonCode": (
                    "EVENT_FAMILY_PARTIAL"
                    if not requested.issubset(available)
                    else "NO_EVENTS"
                    if not events
                    else None
                ),
                "release": event_release,
                "events": page,
                "page": {"nextCursor": next_cursor, "limit": limit},
            }
        return _conditional_response(
            request=request,
            if_none_match=if_none_match,
            data_version=event_data_version,
            etag=_etag(
                "equity-events",
                event_data_version,
                {
                    "request": normalized,
                    "exchange": exchange,
                    "symbol": symbol,
                    "identityVersion": str(identifier.version_id),
                },
            ),
            body=response_body,
        )

    @app.post(
        "/internal/v1/equities/{exchange}/{symbol}/data-status/query",
        dependencies=[Depends(require_service_bearer)],
    )
    def query_equity_data_status(
        request: Request,
        body: dict[str, Any],
        exchange: str = Path(pattern=r"^(SSE|SZSE|BSE)$"),
        symbol: str = Path(pattern=r"^[0-9]{6}$"),
        if_none_match: str | None = Header(default=None, max_length=256),
    ) -> Response:
        """返回详情数据集相互独立的可用性、陈旧度、来源与重试语义。"""
        normalized = _validate_status(body)
        with database.session() as session:
            instrument, identifier = _identity(
                session,
                exchange=exchange,
                symbol=symbol,
                as_of=normalized.get("asOf"),
                known_at=normalized.get("knownAt"),
            )
            discovery_availability = _discovery_availability(
                session,
                security_id=instrument.security_id,
                as_of=normalized.get("asOf"),
                known_at=normalized.get("knownAt"),
            )
            identity_as_of = _status_identity_as_of(
                session,
                requested_as_of=normalized.get("asOf"),
                known_at=normalized.get("knownAt"),
                availability_row=discovery_availability.get("identity"),
            )
            name_version = _identity_name(
                session,
                security_id=instrument.security_id,
                as_of=identity_as_of,
                known_at=normalized.get("knownAt"),
            )
            datasets = [
                _dataset_status(
                    session,
                    family=family,
                    security_id=instrument.security_id,
                    identifier_version_id=identifier.version_id,
                    as_of=normalized.get("asOf"),
                    known_at=normalized.get("knownAt"),
                    discovery=discovery_availability,
                )
                for family in normalized["families"]
            ]
            response_body = {
                "identity": {
                    "exchange": identifier.exchange,
                    "symbol": identifier.symbol,
                    "name": name_version.name,
                    "identityAsOf": identity_as_of.isoformat(),
                },
                "datasets": datasets,
            }
            status_version = _composite_version(
                "equity-data-status",
                {
                    "securityId": instrument.security_id,
                    "identityVersion": str(identifier.version_id),
                    "identityNameVersion": str(name_version.version_id),
                    "identityName": name_version.name,
                    "identityAsOf": identity_as_of.isoformat(),
                    "datasets": datasets,
                },
            )
        return _conditional_response(
            request=request,
            if_none_match=if_none_match,
            data_version=status_version,
            etag=_etag("equity-data-status", status_version, normalized),
            body=response_body,
        )


def _validate_search(body: Mapping[str, Any]) -> dict[str, Any]:
    """校验发现请求的封闭字段、范围和枚举。"""
    allowed = {
        "q",
        "exchanges",
        "lifecycleStatuses",
        "tradingStatuses",
        "memberships",
        "valuation",
        "moneyFlow",
        "columns",
        "sort",
        "cursor",
        "limit",
        "dataVersion",
    }
    _reject_unknown(body, allowed)
    _reject_unavailable_discovery_money_flow(body)
    limit = _integer(body.get("limit"), "limit", minimum=1, maximum=100)
    result = dict(body)
    result["limit"] = limit
    result["sort"] = _sort(body.get("sort"))
    columns = body.get("columns")
    if columns is None:
        result["columns"] = list(_COLUMNS)
    else:
        _string_list(
            columns,
            "columns",
            allowed=frozenset(_COLUMNS),
            maximum=len(_COLUMNS),
        )
        result["columns"] = list(columns)
    if exchanges := body.get("exchanges"):
        _string_list(exchanges, "exchanges", allowed=_EXCHANGES, maximum=3)
    if lifecycle := body.get("lifecycleStatuses"):
        _string_list(lifecycle, "lifecycleStatuses", allowed=_LIFECYCLE, maximum=3)
        if "SUSPENDED" in lifecycle:
            raise _validation("suspended listing lifecycle capability is unavailable")
    if trading := body.get("tradingStatuses"):
        _string_list(
            trading,
            "tradingStatuses",
            allowed=_TRADING,
            maximum=len(_TRADING),
        )
    if q := body.get("q"):
        if not isinstance(q, str) or not 1 <= len(q) <= 64:
            raise _validation("q is invalid")
    if version := body.get("dataVersion"):
        _uuid(version, "dataVersion")
    if cursor := body.get("cursor"):
        if not isinstance(cursor, str) or len(cursor) > 1024:
            raise _validation("cursor is invalid")
    memberships = body.get("memberships")
    if memberships is not None:
        if not isinstance(memberships, list) or not 1 <= len(memberships) <= 20:
            raise _validation("memberships is invalid")
        for item in memberships:
            if not isinstance(item, dict) or set(item) != {"scheme", "code"}:
                raise _validation("membership filter is invalid")
            if item["scheme"] not in _SCHEME_FROM_API:
                raise _validation("membership scheme is invalid")
            if not isinstance(item["code"], str) or not 1 <= len(item["code"]) <= 80:
                raise _validation("membership code is invalid")
    _validate_metric_filter(body.get("valuation"), money_flow=False)
    _validate_metric_filter(body.get("moneyFlow"), money_flow=True)
    return result


def _reject_unavailable_discovery_money_flow(body: Mapping[str, Any]) -> None:
    """稳定拒绝尚未获准发布的发现页资金流筛选、排序与列，避免全空结果伪装成可用。"""
    columns = body.get("columns")
    sort_spec = body.get("sort")
    requests_money_flow = (
        "moneyFlow" in body
        or isinstance(columns, list)
        and any(item in _MONEY_FLOW_COLUMNS for item in columns)
        or isinstance(sort_spec, list)
        and any(
            isinstance(item, Mapping) and item.get("field") == "moneyFlowNetAmount"
            for item in sort_spec
        )
    )
    if requests_money_flow:
        raise _validation("money flow discovery capability is unavailable")


def _validate_events(body: Mapping[str, Any]) -> dict[str, Any]:
    """校验统一事件请求。"""
    _reject_unknown(
        body,
        {"families", "asOf", "start", "end", "knownAt", "cursor", "limit"},
    )
    families = body.get("families") or sorted(_EVENT_FAMILIES)
    _string_list(families, "families", allowed=_EVENT_FAMILIES, maximum=5)
    as_of = _optional_date(body.get("asOf"), "asOf")
    start = _optional_date(body.get("start"), "start")
    end = _optional_date(body.get("end"), "end")
    if start is None or end is None:
        raise _validation("event coverage start and end are required")
    if start > end:
        raise _validation("start must not be after end")
    known_at = _optional_datetime(body.get("knownAt"), "knownAt")
    cursor = body.get("cursor")
    if cursor is not None and (not isinstance(cursor, str) or len(cursor) > 1024):
        raise _validation("cursor is invalid")
    return {
        **body,
        "families": list(families),
        "asOf": as_of,
        "start": start,
        "end": end,
        "knownAt": known_at,
        "limit": _integer(body.get("limit"), "limit", minimum=1, maximum=100),
    }


def _validate_status(body: Mapping[str, Any]) -> dict[str, Any]:
    """校验详情数据状态请求。"""
    _reject_unknown(body, {"families", "asOf", "knownAt"})
    families = body.get("families") or list(_STATUS_DATASETS)
    _string_list(
        families,
        "families",
        allowed=frozenset(_STATUS_DATASETS),
        maximum=len(_STATUS_DATASETS),
    )
    return {
        "families": list(families),
        "asOf": _optional_date(body.get("asOf"), "asOf"),
        "knownAt": _optional_datetime(body.get("knownAt"), "knownAt"),
    }


def _validate_metric_filter(value: object, *, money_flow: bool) -> None:
    """校验估值或资金流方法学与范围，不接受自由 SQL 字段。"""
    if value is None:
        return
    expected = (
        {"methodology", "range", "bucket"}
        if money_flow
        else {
            "metric",
            "methodology",
            "range",
        }
    )
    if not isinstance(value, dict) or set(value) != expected:
        raise _validation("metric filter is invalid")
    if money_flow and value["bucket"] != "MAIN":
        raise _validation("money flow bucket is invalid")
    if not money_flow and value["metric"] not in {"PE_TTM", "PB", "PS_TTM"}:
        raise _validation("valuation metric is invalid")
    methodology = value["methodology"]
    if (
        not isinstance(methodology, dict)
        or not set(methodology).issubset({"code", "version"})
        or "code" not in methodology
        or not isinstance(methodology["code"], str)
    ):
        raise _validation("methodology is invalid")
    range_value = value["range"]
    if not isinstance(range_value, dict) or not set(range_value).issubset({"min", "max"}):
        raise _validation("metric range is invalid")
    for bound in range_value.values():
        try:
            Decimal(str(bound))
        except Exception as error:
            raise _validation("metric range is invalid") from error


def _sort(value: object) -> list[dict[str, str]]:
    """校验最多三项稳定排序，默认按证券代码升序。"""
    if value is None:
        return [{"field": "symbol", "direction": "ASC"}]
    if not isinstance(value, list) or not 1 <= len(value) <= 3:
        raise _validation("sort is invalid")
    result: list[dict[str, str]] = []
    for item in value:
        if (
            not isinstance(item, dict)
            or set(item) != {"field", "direction"}
            or item["field"] not in _SORT_ATTRIBUTES
            or item["direction"] not in {"ASC", "DESC"}
        ):
            raise _validation("sort is invalid")
        result.append({"field": str(item["field"]), "direction": str(item["direction"])})
    if len({item["field"] for item in result}) != len(result):
        raise _validation("sort fields must be unique")
    return result


def _apply_discovery_filters(statement: Any, request: Mapping[str, Any]) -> Any:
    """把已校验筛选映射到冻结横截面 SQL。"""
    if q := request.get("q"):
        pattern = f"%{q}%"
        statement = statement.where(
            or_(
                EquityDiscoverySnapshot.symbol.ilike(pattern),
                EquityDiscoverySnapshot.name.ilike(pattern),
                EquityDiscoverySnapshot.exchange.ilike(pattern),
            )
        )
    if exchanges := request.get("exchanges"):
        statement = statement.where(EquityDiscoverySnapshot.exchange.in_(exchanges))
    if lifecycle := request.get("lifecycleStatuses"):
        statement = statement.where(EquityDiscoverySnapshot.lifecycle_status.in_(lifecycle))
    if trading := request.get("tradingStatuses"):
        mapped: set[str] = set()
        for value in trading:
            if value == "TRADE_SUSPENDED":
                mapped.add("SUSPENDED")
            elif value == "TRADED":
                mapped.add("TRADED")
            elif value == "UNKNOWN":
                # 存储层旧版 `RESUMED` 不在冻结公开枚举内，只能按未知暴露，不能伪装成交。
                mapped.update({"UNKNOWN", "RESUMED"})
            elif value in {"NO_SESSION", "NOT_APPLICABLE"}:
                mapped.add(value)
        if not mapped:
            statement = statement.where(false())
        else:
            statement = statement.where(EquityDiscoverySnapshot.trading_status.in_(mapped))
    grouped_memberships: dict[tuple[str, str | None], set[str]] = defaultdict(set)
    for item in request.get("memberships") or ():
        level = (
            item["scheme"].removeprefix("SW2021_L")
            if item["scheme"].startswith("SW2021_L")
            else None
        )
        grouped_memberships[(_SCHEME_FROM_API[item["scheme"]], level)].add(item["code"])
    for (internal_scheme, level), codes in grouped_memberships.items():
        predicate = [
            EquityDiscoveryMembership.release_id == EquityDiscoverySnapshot.release_id,
            EquityDiscoveryMembership.security_id == EquityDiscoverySnapshot.security_id,
            EquityDiscoveryMembership.scheme == internal_scheme,
            EquityDiscoveryMembership.code.in_(sorted(codes)),
        ]
        if level is not None:
            predicate.append(EquityDiscoveryMembership.level == level)
        statement = statement.where(exists(select(1).where(and_(*predicate))))
    valuation = request.get("valuation")
    if valuation:
        attribute = {
            "PE_TTM": EquityDiscoverySnapshot.pe_ttm,
            "PB": EquityDiscoverySnapshot.pb,
            "PS_TTM": EquityDiscoverySnapshot.ps_ttm,
        }[valuation["metric"]]
        statement = statement.where(
            EquityDiscoverySnapshot.valuation_methodology_code == valuation["methodology"]["code"]
        )
        if version := valuation["methodology"].get("version"):
            statement = statement.where(
                EquityDiscoverySnapshot.valuation_methodology_version == version
            )
        statement = _range(statement, attribute, valuation["range"])
    money_flow = request.get("moneyFlow")
    if money_flow:
        statement = statement.where(
            EquityDiscoverySnapshot.money_flow_methodology_code == money_flow["methodology"]["code"]
        )
        if version := money_flow["methodology"].get("version"):
            statement = statement.where(
                EquityDiscoverySnapshot.money_flow_methodology_version == version
            )
        statement = _range(
            statement,
            EquityDiscoverySnapshot.money_flow_net_amount_cny,
            money_flow["range"],
        )
    return statement


def _range(statement: Any, attribute: Any, value: Mapping[str, Any]) -> Any:
    """应用十进制闭区间筛选。"""
    if "min" in value:
        statement = statement.where(attribute >= Decimal(str(value["min"])))
    if "max" in value:
        statement = statement.where(attribute <= Decimal(str(value["max"])))
    return statement


def _discovery_sort_keys(
    sort_spec: Sequence[Mapping[str, str]],
) -> tuple[tuple[Any, str], ...]:
    """返回含内部唯一锚点的稳定排序键，公开代码复用不会造成并列。"""
    keys: list[tuple[Any, str]] = [
        (
            getattr(EquityDiscoverySnapshot, _SORT_ATTRIBUTES[item["field"]]),
            item["direction"],
        )
        for item in sort_spec
    ]
    used = {_SORT_ATTRIBUTES[item["field"]] for item in sort_spec}
    for name in ("exchange", "symbol", "security_id"):
        if name not in used:
            keys.append((getattr(EquityDiscoverySnapshot, name), "ASC"))
    return tuple(keys)


def _discovery_order_by(sort_spec: Sequence[Mapping[str, str]]) -> tuple[Any, ...]:
    """把冻结排序映射到数据库 `NULLS LAST` 表达式。"""
    return tuple(
        (attribute.desc().nulls_last() if direction == "DESC" else attribute.asc().nulls_last())
        for attribute, direction in _discovery_sort_keys(sort_spec)
    )


def _discovery_load_attributes(
    columns: frozenset[str],
    sort_spec: Sequence[Mapping[str, str]],
) -> tuple[Any, ...]:
    """只装载响应列、稳定排序键和基础身份，避免宽表整行物化。"""
    names = {
        "release_id",
        "security_id",
        "exchange",
        "symbol",
        "name",
        "lifecycle_status",
        "trading_status",
        "trading_status_reason",
        "listed_on",
        "delisted_on",
    }
    names.update(_SORT_ATTRIBUTES[item["field"]] for item in sort_spec)
    names.update(_COLUMN_ATTRIBUTES[column] for column in columns if column in _COLUMN_ATTRIBUTES)
    if columns & _CAPITALIZATION_COLUMNS:
        names.add("capital_effective_on")
    if columns & _VALUATION_COLUMNS:
        names.update(
            {
                "valuation_date",
                "valuation_source_label",
                "valuation_methodology_code",
                "valuation_methodology_version",
            }
        )
    if columns & _MONEY_FLOW_COLUMNS:
        names.update(
            {
                "money_flow_date",
                "money_flow_source_label",
                "money_flow_methodology_code",
                "money_flow_methodology_version",
            }
        )
    return tuple(getattr(EquityDiscoverySnapshot, name) for name in sorted(names))


def _apply_discovery_cursor(
    session: Any,
    *,
    statement: Any,
    release_id: UUID,
    cursor: object,
    sort_spec: Sequence[Mapping[str, str]],
    secret: bytes,
    scope: str,
    data_version: UUID,
) -> Any:
    """解析不泄露内部主键的锚点，并追加数据库 keyset 条件。"""
    if cursor is None:
        return statement
    value = _decode_cursor(str(cursor), secret=secret)
    if value.get("v") != str(data_version) or value.get("s") != scope:
        raise _snapshot_expired()
    exchange = value.get("e")
    symbol = value.get("y")
    opaque_anchor = value.get("a")
    if (
        not isinstance(exchange, str)
        or not isinstance(symbol, str)
        or not isinstance(opaque_anchor, str)
    ):
        raise _validation("cursor is invalid")
    candidates = session.scalars(
        select(EquityDiscoverySnapshot).where(
            EquityDiscoverySnapshot.release_id == release_id,
            EquityDiscoverySnapshot.exchange == exchange,
            EquityDiscoverySnapshot.symbol == symbol,
        )
    ).all()
    anchors = [
        row
        for row in candidates
        if hmac.compare_digest(
            _row_anchor(
                secret=secret,
                data_version=data_version,
                security_id=row.security_id,
            ),
            opaque_anchor,
        )
    ]
    if len(anchors) != 1:
        raise _snapshot_expired()
    return statement.where(_after_anchor(anchors[0], sort_spec))


def _after_anchor(
    anchor: EquityDiscoverySnapshot,
    sort_spec: Sequence[Mapping[str, str]],
) -> Any:
    """构造支持升降序和空值最后的词典序下一页条件。"""
    greater_terms: list[Any] = []
    equal_prefix: list[Any] = []
    for attribute, direction in _discovery_sort_keys(sort_spec):
        value = getattr(anchor, attribute.key)
        equality = attribute.is_(None) if value is None else attribute == value
        if value is not None:
            ordered_after = attribute < value if direction == "DESC" else attribute > value
            greater_terms.append(and_(*equal_prefix, or_(ordered_after, attribute.is_(None))))
        equal_prefix.append(equality)
    if not greater_terms:
        return false()
    return or_(*greater_terms)


def _row_anchor(*, secret: bytes, data_version: UUID, security_id: int) -> str:
    """把内部证券键转换成单向游标锚点，Base64 解码也无法恢复主键。"""
    payload = f"equity-discovery:{data_version}:{security_id}".encode()
    return _base64(hmac.digest(secret, payload, "sha256"))


def _next_cursor(
    *,
    has_more: bool,
    page_rows: Sequence[EquityDiscoverySnapshot],
    secret: bytes,
    scope: str,
    data_version: UUID,
) -> str | None:
    """以本页最后唯一行签发范围和版本绑定的下一页游标。"""
    if not page_rows or not has_more:
        return None
    last = page_rows[-1]
    return _encode_cursor(
        {
            "a": _row_anchor(
                secret=secret,
                data_version=data_version,
                security_id=last.security_id,
            ),
            "e": last.exchange,
            "y": last.symbol,
            "s": scope,
            "v": str(data_version),
        },
        secret=secret,
    )


def _memberships(
    session: Any, *, release_id: UUID, security_ids: Sequence[int]
) -> dict[int, tuple[EquityDiscoveryMembership, ...]]:
    """批量读取当前页全部分类，避免逐证券查询。"""
    if not security_ids:
        return {}
    rows = session.scalars(
        select(EquityDiscoveryMembership)
        .where(
            EquityDiscoveryMembership.release_id == release_id,
            EquityDiscoveryMembership.security_id.in_(security_ids),
        )
        .order_by(
            EquityDiscoveryMembership.security_id,
            EquityDiscoveryMembership.scheme,
            EquityDiscoveryMembership.code,
        )
    ).all()
    grouped: dict[int, list[EquityDiscoveryMembership]] = defaultdict(list)
    for row in rows:
        grouped[row.security_id].append(row)
    return {key: tuple(value) for key, value in grouped.items()}


def _availability_by_security(
    session: Any,
    *,
    release_id: UUID,
    security_ids: Sequence[int],
    families: frozenset[str],
) -> dict[int, dict[str, EquityDiscoveryAvailability]]:
    """批量读取当前页原因化可用性。"""
    if not security_ids or not families:
        return {}
    rows = session.scalars(
        select(EquityDiscoveryAvailability).where(
            EquityDiscoveryAvailability.release_id == release_id,
            EquityDiscoveryAvailability.security_id.in_(security_ids),
            EquityDiscoveryAvailability.family.in_(families),
        )
    ).all()
    result: dict[int, dict[str, EquityDiscoveryAvailability]] = defaultdict(dict)
    for row in rows:
        result[row.security_id][row.family] = row
    return dict(result)


def _components(
    session: Any,
    *,
    release_id: UUID,
    families: frozenset[str],
) -> list[dict[str, Any]]:
    """只读取组件状态去重集合，避免加载全市场逐证券 availability。"""
    if not families:
        return []
    rows = session.execute(
        select(
            EquityDiscoveryAvailability.family,
            EquityDiscoveryAvailability.availability,
            EquityDiscoveryAvailability.component_data_version,
            EquityDiscoveryAvailability.source_label,
            EquityDiscoveryAvailability.methodology,
        )
        .where(
            EquityDiscoveryAvailability.release_id == release_id,
            EquityDiscoveryAvailability.family.in_(families),
        )
        .distinct()
    ).all()
    grouped: dict[str, list[Any]] = defaultdict(list)
    for row in rows:
        grouped[str(row.family)].append(row)
    result: list[dict[str, Any]] = []
    for family in sorted(families):
        values = grouped.get(family, [])
        states = {item.availability for item in values}
        if not values:
            availability = "SOURCE_UNAVAILABLE"
        elif states <= {"DATA"}:
            availability = "AVAILABLE"
        elif states <= {"LEGITIMATE_EMPTY", "NOT_APPLICABLE"}:
            availability = "EMPTY"
        elif states & {"DATA", "LEGITIMATE_EMPTY", "NOT_APPLICABLE"}:
            availability = "PARTIAL"
        else:
            availability = "SOURCE_UNAVAILABLE"
        versions = {
            UUID(str(item.component_data_version))
            for item in values
            if item.component_data_version is not None
        }
        labels = {item.source_label for item in values if item.source_label}
        methods = {
            json.dumps(item.methodology, sort_keys=True) for item in values if item.methodology
        }
        result.append(
            {
                "family": family,
                "dataVersion": str(next(iter(versions))) if len(versions) == 1 else None,
                "availability": availability,
                "sourceLabel": next(iter(labels)) if len(labels) == 1 else None,
                "methodology": (
                    _methodology(json.loads(next(iter(methods)))) if len(methods) == 1 else None
                ),
            }
        )
    return result


def _component_families(columns: frozenset[str]) -> frozenset[str]:
    """把列投影转换为最小组件集合，未请求的宽组件不进入查询。"""
    families = {"identity"}
    if "tradingStatus" in columns:
        families.add("trading_status")
    if columns & _MARKET_COLUMNS:
        families.add("market")
    if columns & _CAPITALIZATION_COLUMNS:
        families.add("capitalization")
    if columns & _VALUATION_COLUMNS:
        families.add("valuation")
    if columns & _MONEY_FLOW_COLUMNS:
        families.add("money_flow")
    if "memberships" in columns:
        families.update({"industry", "concept", "sw"})
    return frozenset(families)


def _discovery_record(
    row: EquityDiscoverySnapshot,
    *,
    identity_as_of: date,
    columns: frozenset[str],
    memberships: Sequence[EquityDiscoveryMembership],
    availability: Mapping[str, EquityDiscoveryAvailability],
) -> dict[str, Any]:
    """把冻结 ORM 行投影为 API 防腐层严格合同。"""
    market_selected = bool(columns & _MARKET_COLUMNS)
    capitalization_selected = bool(columns & _CAPITALIZATION_COLUMNS)
    valuation_selected = bool(columns & _VALUATION_COLUMNS)
    money_flow_selected = bool(columns & _MONEY_FLOW_COLUMNS)
    return {
        "identity": {
            "exchange": row.exchange,
            "symbol": row.symbol,
            "name": row.name,
            "identityAsOf": identity_as_of.isoformat(),
        },
        "statuses": {
            "lifecycleStatus": row.lifecycle_status,
            "tradingStatus": _trading_status(row.trading_status),
            "tradingStatusReason": (
                row.trading_status_reason
                or ("SOURCE_STATUS_RESUMED_NOT_FROZEN" if row.trading_status == "RESUMED" else None)
            ),
            "listedOn": _date(row.listed_on),
            "delistedOn": _date(row.delisted_on),
        },
        "market": {
            "tradeDate": _date(row.trade_date) if "tradeDate" in columns else None,
            "close": _decimal(row.close_price) if "close" in columns else None,
            "previousClose": (
                _decimal(row.previous_close_price) if "previousClose" in columns else None
            ),
            "changeAmount": (_decimal(row.change_amount) if "changeAmount" in columns else None),
            "changePercent": (_decimal(row.change_percent) if "changePercent" in columns else None),
            "volumeShares": (_decimal(row.volume_shares) if "volumeShares" in columns else None),
            "amountCny": _decimal(row.amount_cny) if "amountCny" in columns else None,
            "turnoverRate": (_decimal(row.turnover_rate) if "turnoverRate" in columns else None),
            "currency": "CNY",
            "nullReason": (
                _null_reason(availability.get("market"))
                if market_selected
                else "COLUMN_NOT_REQUESTED"
            ),
        },
        "capitalization": {
            "effectiveOn": (_date(row.capital_effective_on) if capitalization_selected else None),
            "totalShares": (_decimal(row.total_shares) if "totalShares" in columns else None),
            "listedTradableAShares": (
                _decimal(row.listed_tradable_a_shares)
                if "listedTradableAShares" in columns
                else None
            ),
            "totalMarketCapCny": (
                _decimal(row.total_market_cap_cny) if "totalMarketCapCny" in columns else None
            ),
            "floatMarketCapCny": (
                _decimal(row.float_market_cap_cny) if "floatMarketCapCny" in columns else None
            ),
            "currency": "CNY",
            "methodology": (
                _row_methodology(availability.get("capitalization"))
                if capitalization_selected
                else None
            ),
            "nullReason": (
                _null_reason(availability.get("capitalization"))
                if capitalization_selected
                else "COLUMN_NOT_REQUESTED"
            ),
        },
        "valuation": {
            "tradeDate": _date(row.valuation_date) if valuation_selected else None,
            "peTtm": _decimal(row.pe_ttm) if "peTtm" in columns else None,
            "pb": _decimal(row.pb) if "pb" in columns else None,
            "psTtm": _decimal(row.ps_ttm) if "psTtm" in columns else None,
            "sourceLabel": (row.valuation_source_label if valuation_selected else None),
            "methodology": (
                _pair_methodology(
                    row.valuation_methodology_code,
                    row.valuation_methodology_version,
                )
                if valuation_selected
                else None
            ),
            "nullReason": (
                _null_reason(availability.get("valuation"))
                if valuation_selected
                else "COLUMN_NOT_REQUESTED"
            ),
        },
        "moneyFlow": {
            "tradeDate": _date(row.money_flow_date) if money_flow_selected else None,
            "netAmountCny": (
                _decimal(row.money_flow_net_amount_cny) if "moneyFlowNetAmount" in columns else None
            ),
            "netRatio": (
                _decimal(row.money_flow_net_ratio) if "moneyFlowNetRatio" in columns else None
            ),
            "sourceLabel": (row.money_flow_source_label if money_flow_selected else None),
            "methodology": (
                _pair_methodology(
                    row.money_flow_methodology_code,
                    row.money_flow_methodology_version,
                )
                if money_flow_selected
                else None
            ),
            "nullReason": (
                _null_reason(availability.get("money_flow"))
                if money_flow_selected
                else "COLUMN_NOT_REQUESTED"
            ),
        },
        "memberships": [
            {
                "scheme": _membership_scheme(item),
                "code": item.code,
                "name": item.name,
                "level": int(item.level) if item.level and item.level.isdigit() else None,
                "observedOn": item.observed_on.isoformat(),
            }
            for item in memberships
            if _membership_scheme(item) is not None
        ],
    }


def _identity_as_of(row: EquityDiscoverySnapshot, *, snapshot_as_of: date) -> date:
    """为详情路由提供双时态锚点；闭合退市身份使用最后有效业务日。"""
    if row.lifecycle_status == "DELISTED" and row.delisted_on is not None:
        return row.delisted_on
    return snapshot_as_of


def _membership_scheme(item: EquityDiscoveryMembership) -> str | None:
    """按冻结层级准确投影行业体系，禁止把所有申万节点伪装成三级。"""
    if item.scheme in _SCHEME_TO_API:
        return _SCHEME_TO_API[item.scheme]
    if item.scheme == "sw.industry" and item.level in {"1", "2", "3"}:
        return f"SW2021_L{item.level}"
    return None


def _event_rows(
    session: Any,
    *,
    security_id: int,
    request: Mapping[str, Any],
    coverages: Mapping[str, _EventCoverageSelection],
) -> list[dict[str, Any]]:
    """按每族唯一安全累计视图读取事实，避免跨 publication 连接制造重复行。"""
    requested = set(request["families"])
    events: list[dict[str, Any]] = []
    if action_coverage := coverages.get("CORPORATE_ACTION"):
        event_date = func.coalesce(
            EquityCorporateActionVersion.ex_date,
            EquityCorporateActionVersion.record_date,
            EquityCorporateActionVersion.announcement_date,
            EquityCorporateActionVersion.report_period,
        )
        statement = (
            select(EquityCorporateActionVersion)
            .join(
                SourceBatch,
                SourceBatch.source_batch_id == EquityCorporateActionVersion.source_batch_id,
            )
            .where(
                EquityCorporateActionVersion.security_id == security_id,
                SourceBatch.provider_id
                == action_coverage.segments[0].evidence.source_batch.provider_id,
                _event_slice_predicate(
                    action_coverage,
                    event_date=event_date,
                    known_from=EquityCorporateActionVersion.valid_from,
                    known_to=EquityCorporateActionVersion.valid_to,
                ),
            )
            .order_by(
                event_date,
                EquityCorporateActionVersion.action_id,
                EquityCorporateActionVersion.revision,
            )
        )
        for row in session.scalars(statement).all():
            occurred = row.ex_date or row.record_date
            events.append(
                {
                    "eventId": f"corporate-action:{row.action_id}:{row.revision}",
                    "family": "CORPORATE_ACTION",
                    "kind": "DIVIDEND_OR_SHARE_DISTRIBUTION",
                    "stage": None,
                    "status": row.status,
                    "occurredOn": _date(occurred),
                    "announcedOn": _date(row.announcement_date),
                    "reportPeriod": row.report_period.isoformat(),
                    "title": None,
                    "sourceLabel": action_coverage.source_label,
                    "dataVersion": str(action_coverage.data_version),
                    "facts": [
                        _fact("CASH_DIVIDEND_PER_10", row.cash_dividend_per_10, "CNY"),
                        _fact("BONUS_SHARES_PER_10", row.bonus_shares_per_10, "SHARE"),
                        _fact("TRANSFER_SHARES_PER_10", row.transfer_shares_per_10, "SHARE"),
                    ],
                }
            )
    earnings_requested = requested & {"EARNINGS_FORECAST", "EARNINGS_EXPRESS"}
    for family in sorted(earnings_requested):
        selection = coverages.get(family)
        if selection is None:
            continue
        _append_earnings_events(
            session,
            events=events,
            security_id=security_id,
            family=family,
            coverage=selection,
        )
    if dragon_coverage := coverages.get("DRAGON_TIGER"):
        _append_dragon_events(
            session,
            events,
            security_id,
            coverage=dragon_coverage,
        )
    if block_coverage := coverages.get("BLOCK_TRADE"):
        _append_block_events(
            session,
            events,
            security_id,
            coverage=block_coverage,
        )
    return _deduplicate_events(events)


def _event_coverages(
    session: Any,
    *,
    security_id: int,
    identifier_version_id: UUID,
    request: Mapping[str, Any],
) -> dict[str, _EventCoverageSelection]:
    """逐族选择覆盖完整请求闭区间的唯一来源与方法学证据集合。"""
    selected: dict[str, _EventCoverageSelection] = {}
    for family in request["families"]:
        selection = _event_coverage_selection(
            session,
            family=str(family),
            security_id=security_id,
            identifier_version_id=identifier_version_id,
            start=request["start"],
            end=request["end"],
            known_at=request.get("knownAt"),
        )
        if selection is not None:
            selected[str(family)] = selection
    return selected


def _event_coverage_selection(
    session: Any,
    *,
    family: str,
    security_id: int,
    identifier_version_id: UUID,
    start: date,
    end: date,
    known_at: datetime | None,
) -> _EventCoverageSelection | None:
    """按双时态可见性读取候选，并拒绝来源或方法学歧义及任一日覆盖缺口。"""
    dataset = _EVENT_DATASETS[family]
    candidates = _event_coverage_candidates(
        session,
        family=family,
        dataset=dataset,
        security_id=security_id,
        identifier_version_id=identifier_version_id,
        start=start,
        end=end,
        known_at=known_at,
    )
    return _select_event_coverage(
        candidates,
        family=family,
        dataset=dataset,
        security_id=security_id,
        identifier_version_id=identifier_version_id,
        start=start,
        end=end,
    )


def _event_coverage_candidates(
    session: Any,
    *,
    family: str,
    dataset: str,
    security_id: int,
    identifier_version_id: UUID,
    start: date | None,
    end: date | None,
    known_at: datetime | None,
) -> tuple[_EventCoverageCandidate, ...]:
    """读取一个身份版本在请求窗口相交且于知识时点可见的真实覆盖证据。"""
    statement = (
        select(
            EquityEventWindowCoverage,
            DatasetPublication,
            MethodologyVersion,
            SourceBatch,
        )
        .join(
            DatasetPublication,
            DatasetPublication.publication_id == EquityEventWindowCoverage.publication_id,
        )
        .join(
            DatasetRelease,
            DatasetRelease.release_id == DatasetPublication.release_id,
        )
        .join(
            MethodologyVersion,
            MethodologyVersion.methodology_version_id == DatasetRelease.methodology_version_id,
        )
        .join(
            SourceBatch,
            SourceBatch.source_batch_id == EquityEventWindowCoverage.source_batch_id,
        )
        .where(
            EquityEventWindowCoverage.dataset == dataset,
            EquityEventWindowCoverage.event_family == family,
            EquityEventWindowCoverage.security_id == security_id,
            EquityEventWindowCoverage.identifier_version_id == identifier_version_id,
        )
    )
    if end is not None:
        statement = statement.where(EquityEventWindowCoverage.coverage_from <= end)
    if start is not None:
        statement = statement.where(EquityEventWindowCoverage.coverage_to >= start)
    if known_at is None:
        statement = statement.where(EquityEventWindowCoverage.superseded_at.is_(None))
    else:
        statement = statement.where(
            EquityEventWindowCoverage.created_at <= known_at,
            or_(
                EquityEventWindowCoverage.superseded_at.is_(None),
                EquityEventWindowCoverage.superseded_at > known_at,
            ),
            DatasetPublication.published_at <= known_at,
            or_(
                DatasetPublication.knowledge_cutoff.is_(None),
                DatasetPublication.knowledge_cutoff <= known_at,
            ),
        )
    return tuple(
        _EventCoverageCandidate(
            coverage=coverage,
            publication=publication,
            methodology=methodology,
            source_batch=source_batch,
        )
        for coverage, publication, methodology, source_batch in session.execute(statement).all()
    )


def _select_event_coverage(
    candidates: Sequence[_EventCoverageCandidate],
    *,
    family: str,
    dataset: str,
    security_id: int,
    identifier_version_id: UUID,
    start: date,
    end: date,
) -> _EventCoverageSelection | None:
    """只接受一个可完整覆盖窗口的来源方法学组，并冻结最新安全累计截止点。"""
    grouped: dict[tuple[UUID, str, str], list[_EventCoverageCandidate]] = defaultdict(list)
    for candidate in candidates:
        grouped[
            (
                candidate.methodology.methodology_version_id,
                candidate.source_batch.provider_id,
                candidate.source_batch.upstream_source,
            )
        ].append(candidate)
    complete: list[_EventCoverageSelection] = []
    for group in grouped.values():
        selection = _complete_event_coverage(
            group,
            family=family,
            dataset=dataset,
            security_id=security_id,
            identifier_version_id=identifier_version_id,
            start=start,
            end=end,
        )
        if selection is not None:
            complete.append(selection)
    if len(complete) != 1:
        return None
    return complete[0]


def _complete_event_coverage(
    candidates: Sequence[_EventCoverageCandidate],
    *,
    family: str,
    dataset: str,
    security_id: int,
    identifier_version_id: UUID,
    start: date,
    end: date,
) -> _EventCoverageSelection | None:
    """先切分闭区间并逐片选最新证据，旧宽窗只补新窄窗未覆盖的日期。"""
    segments = _cover_event_interval(candidates, start=start, end=end)
    if not segments:
        return None
    methodology = segments[0].evidence.methodology
    source = segments[0].evidence.source_batch
    view_cutoff = max(item.evidence.coverage.created_at for item in segments)
    data_version = _composite_version(
        f"equity-event-coverage:{family}",
        {
            "dataset": dataset,
            "securityId": security_id,
            "identifierVersion": str(identifier_version_id),
            "coverageFrom": start.isoformat(),
            "coverageTo": end.isoformat(),
            "viewCutoff": _timestamp(view_cutoff),
            "segments": [
                {
                    "coverageVersion": str(item.evidence.coverage.coverage_version),
                    "publicationDataVersion": str(item.evidence.publication.data_version),
                    "sliceFrom": item.coverage_from.isoformat(),
                    "sliceTo": item.coverage_to.isoformat(),
                    "createdAt": _timestamp(item.evidence.coverage.created_at),
                }
                for item in segments
            ],
        },
    )
    return _EventCoverageSelection(
        family=family,
        dataset=dataset,
        security_id=security_id,
        identifier_version_id=identifier_version_id,
        coverage_from=start,
        coverage_to=end,
        segments=segments,
        view_cutoff=view_cutoff,
        data_version=data_version,
        source_label=source.upstream_source,
        methodology_code=methodology.code,
        methodology_version=methodology.version,
    )


def _cover_event_interval(
    candidates: Sequence[_EventCoverageCandidate],
    *,
    start: date,
    end: date,
) -> tuple[_EventCoverageSlice, ...]:
    """按全部覆盖边界切片并逐片选最新观察；任何一个自然日缺口都返回空。"""
    boundaries = {start, end + timedelta(days=1)}
    for candidate in candidates:
        clipped_from = max(start, candidate.coverage.coverage_from)
        clipped_to = min(end, candidate.coverage.coverage_to)
        if clipped_from > clipped_to:
            continue
        boundaries.add(clipped_from)
        boundaries.add(clipped_to + timedelta(days=1))
    ordered = sorted(boundaries)
    selected: list[_EventCoverageSlice] = []
    for index in range(len(ordered) - 1):
        slice_from = ordered[index]
        slice_to = ordered[index + 1] - timedelta(days=1)
        if slice_from > end or slice_to < start:
            continue
        reachable = [
            item
            for item in candidates
            if item.coverage.coverage_from <= slice_from and item.coverage.coverage_to >= slice_to
        ]
        if not reachable:
            return ()
        chosen = max(
            reachable,
            key=lambda item: (
                item.coverage.created_at,
                -(item.coverage.coverage_to - item.coverage.coverage_from).days,
                str(item.coverage.coverage_version),
            ),
        )
        if (
            selected
            and selected[-1].evidence.coverage.coverage_version == chosen.coverage.coverage_version
            and selected[-1].coverage_to + timedelta(days=1) == slice_from
        ):
            previous = selected[-1]
            selected[-1] = _EventCoverageSlice(
                coverage_from=previous.coverage_from,
                coverage_to=slice_to,
                evidence=chosen,
            )
            continue
        selected.append(
            _EventCoverageSlice(
                coverage_from=slice_from,
                coverage_to=slice_to,
                evidence=chosen,
            )
        )
    return tuple(selected)


def _event_release(
    coverages: Mapping[str, _EventCoverageSelection],
    *,
    requested: set[str],
    requested_end: date,
) -> dict[str, Any]:
    """把实际覆盖组件组合成独立 release，版本包含每条窗口观察身份。"""
    ordered = [coverages[family] for family in sorted(coverages)]
    data_version = _composite_version(
        "equity-events",
        {
            "families": sorted(coverages),
            "components": [
                {
                    "family": item.family,
                    "dataset": item.dataset,
                    "dataVersion": str(item.data_version),
                }
                for item in ordered
            ],
        },
    )
    publications = [segment.evidence.publication for item in ordered for segment in item.segments]
    partial = set(coverages) != requested
    warning = partial or any(item.quality_status != "passed" for item in publications)
    return {
        "dataset": "equity.events.composite",
        "dataVersion": str(data_version),
        "publishedAt": _timestamp(
            max(
                segment.evidence.coverage.created_at
                for item in ordered
                for segment in item.segments
            )
        ),
        "effectiveAsOf": requested_end.isoformat(),
        "knowledgeCutoff": _timestamp(max(item.view_cutoff for item in ordered)),
        "qualityStatus": "warning" if warning else "passed",
    }


def _event_sort_key(item: Mapping[str, Any]) -> tuple[str, str]:
    """按业务日期再按内部事件键稳定排序，确保同一发布的分页顺序不漂移。"""
    return (
        item["occurredOn"] or item["announcedOn"] or item["reportPeriod"] or "",
        item["eventId"],
    )


def _publication_id_sort_key(item: DatasetPublication) -> str:
    """按 publication UUID 稳定排列复合组件，避免数据库返回顺序改变版本。"""
    return str(item.publication_id)


def _event_slice_predicate(
    coverage: _EventCoverageSelection,
    *,
    event_date: Any,
    known_from: Any,
    known_to: Any,
) -> Any:
    """把非重叠业务日期片与各自 coverage 提交时点组合成同一条 PIT 条件。"""
    return or_(
        *(
            and_(
                event_date >= segment.coverage_from,
                event_date <= segment.coverage_to,
                known_from <= segment.evidence.coverage.created_at,
                or_(
                    known_to.is_(None),
                    known_to > segment.evidence.coverage.created_at,
                ),
            )
            for segment in coverage.segments
        )
    )


def _append_earnings_events(
    session: Any,
    *,
    events: list[dict[str, Any]],
    security_id: int,
    family: str,
    coverage: _EventCoverageSelection,
) -> None:
    """按公告日与唯一累计截止点追加业绩事件，报告期不充当事件发生日。"""
    value_kind = "GUIDANCE" if family == "EARNINGS_FORECAST" else "EXPRESS"
    statement = (
        select(
            CorporateEventRevision,
            CorporateEvent,
            DisclosureDocument,
            CorporateEarningsValue,
        )
        .select_from(CorporateEventRevision)
        .join(CorporateEvent, CorporateEvent.event_id == CorporateEventRevision.event_id)
        .join(
            DisclosureDocument,
            DisclosureDocument.document_id == CorporateEventRevision.primary_document_id,
        )
        .join(
            CorporateEarningsValue,
            CorporateEarningsValue.event_revision_id == CorporateEventRevision.event_revision_id,
        )
        .join(
            SourceBatch,
            SourceBatch.source_batch_id == CorporateEventRevision.source_batch_id,
        )
        .where(
            CorporateEvent.security_id == security_id,
            CorporateEventRevision.methodology_version_id
            == coverage.segments[0].evidence.methodology.methodology_version_id,
            SourceBatch.provider_id == coverage.segments[0].evidence.source_batch.provider_id,
            CorporateEarningsValue.value_kind == value_kind,
            _event_slice_predicate(
                coverage,
                event_date=DisclosureDocument.announced_on,
                known_from=CorporateEventRevision.known_from,
                known_to=CorporateEventRevision.known_to,
            ),
        )
        .order_by(
            DisclosureDocument.announced_on,
            CorporateEventRevision.event_revision_id,
            CorporateEarningsValue.metric_code,
        )
    )
    grouped: dict[
        UUID,
        tuple[
            CorporateEventRevision,
            CorporateEvent,
            DisclosureDocument,
            list[CorporateEarningsValue],
        ],
    ] = {}
    for revision, event, document, metric in session.execute(statement).all():
        group = grouped.setdefault(
            revision.event_revision_id,
            (revision, event, document, []),
        )
        group[3].append(metric)
    for revision, event, document, metrics in grouped.values():
        facts = [
            {
                "code": item.metric_code,
                "value": _decimal(item.value_single),
                "valueLow": _decimal(item.value_low),
                "valueHigh": _decimal(item.value_high),
                "unit": item.metric_unit,
                "currency": item.currency,
                "text": None,
            }
            for item in metrics
        ]
        events.append(
            {
                "eventId": (f"corporate-event:{family}:{event.event_id}:{revision.revision_no}"),
                "family": family,
                "kind": family,
                "stage": revision.stage,
                "status": revision.status,
                "occurredOn": _date(revision.event_date),
                "announcedOn": document.announced_on.isoformat(),
                "reportPeriod": _date(revision.report_period),
                "title": document.title[:500] or None,
                "sourceLabel": coverage.source_label,
                "dataVersion": str(coverage.data_version),
                "facts": facts,
            }
        )


def _append_dragon_events(
    session: Any,
    events: list[dict[str, Any]],
    security_id: int,
    *,
    coverage: _EventCoverageSelection,
) -> None:
    """按唯一累计截止点追加龙虎榜 revision，不把席位解释为投资者身份。"""
    statement = (
        select(DragonTigerEventRevision)
        .select_from(DragonTigerEventRevision)
        .join(
            SourceBatch,
            SourceBatch.source_batch_id == DragonTigerEventRevision.source_batch_id,
        )
        .where(
            DragonTigerEventRevision.security_id == security_id,
            DragonTigerEventRevision.methodology_version_id
            == coverage.segments[0].evidence.methodology.methodology_version_id,
            SourceBatch.provider_id == coverage.segments[0].evidence.source_batch.provider_id,
            _event_slice_predicate(
                coverage,
                event_date=DragonTigerEventRevision.trade_date,
                known_from=DragonTigerEventRevision.known_from,
                known_to=DragonTigerEventRevision.known_to,
            ),
        )
        .order_by(
            DragonTigerEventRevision.trade_date,
            DragonTigerEventRevision.event_revision_id,
        )
    )
    for row in session.scalars(statement).all():
        events.append(
            {
                "eventId": f"dragon-tiger:{row.event_revision_id}",
                "family": "DRAGON_TIGER",
                "kind": row.reason_family,
                "stage": None,
                "status": None,
                "occurredOn": row.trade_date.isoformat(),
                "announcedOn": (
                    _date(row.source_published_at.date()) if row.source_published_at else None
                ),
                "reportPeriod": None,
                "title": row.reason_raw[:500],
                "sourceLabel": coverage.source_label,
                "dataVersion": str(coverage.data_version),
                "facts": [
                    _fact("BUY_AMOUNT", row.buy_amount, row.currency),
                    _fact("SELL_AMOUNT", row.sell_amount, row.currency),
                    _fact("NET_AMOUNT", row.net_amount, row.currency),
                    _fact("TURNOVER_AMOUNT", row.turnover_amount, row.currency),
                ],
            }
        )


def _append_block_events(
    session: Any,
    events: list[dict[str, Any]],
    security_id: int,
    *,
    coverage: _EventCoverageSelection,
) -> None:
    """按唯一累计截止点追加大宗逐笔 revision，经济重复行保持独立。"""
    statement = (
        select(BlockTradeExecutionRevision)
        .select_from(BlockTradeExecutionRevision)
        .join(
            SourceBatch,
            SourceBatch.source_batch_id == BlockTradeExecutionRevision.source_batch_id,
        )
        .where(
            BlockTradeExecutionRevision.security_id == security_id,
            BlockTradeExecutionRevision.methodology_version_id
            == coverage.segments[0].evidence.methodology.methodology_version_id,
            SourceBatch.provider_id == coverage.segments[0].evidence.source_batch.provider_id,
            _event_slice_predicate(
                coverage,
                event_date=BlockTradeExecutionRevision.trade_date,
                known_from=BlockTradeExecutionRevision.known_from,
                known_to=BlockTradeExecutionRevision.known_to,
            ),
        )
        .order_by(
            BlockTradeExecutionRevision.trade_date,
            BlockTradeExecutionRevision.execution_revision_id,
        )
    )
    for row in session.scalars(statement).all():
        events.append(
            {
                "eventId": f"block-trade:{row.execution_revision_id}",
                "family": "BLOCK_TRADE",
                "kind": "EXECUTION",
                "stage": None,
                "status": None,
                "occurredOn": row.trade_date.isoformat(),
                "announcedOn": (
                    _date(row.source_published_at.date()) if row.source_published_at else None
                ),
                "reportPeriod": None,
                "title": None,
                "sourceLabel": coverage.source_label,
                "dataVersion": str(coverage.data_version),
                "facts": [
                    _fact("PRICE", row.price, row.currency),
                    _fact("QUANTITY", row.quantity, row.quantity_unit),
                    _fact("AMOUNT", row.amount, row.currency),
                    _fact("PREMIUM_RATIO", row.premium_ratio, "RATIO"),
                ],
            }
        )


def _deduplicate_events(events: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """按稳定事件键去重；同键内容冲突时失败关闭，避免静默选择任意事实。"""
    unique: dict[str, dict[str, Any]] = {}
    for event in events:
        event_id = str(event["eventId"])
        previous = unique.get(event_id)
        if previous is not None and previous != event:
            raise InternalProblem(
                status=409,
                code="event-ambiguous",
                detail="Published equity event identity is ambiguous",
            )
        unique[event_id] = event
    return list(unique.values())


def _fact(code: str, value: object, unit: str | None) -> dict[str, Any]:
    """构造单值事件事实，并只在 ISO 币种时填写 currency。"""
    currency = unit if unit and len(unit) == 3 and unit.isalpha() else None
    return {
        "code": code,
        "value": _decimal(value),
        "valueLow": None,
        "valueHigh": None,
        "unit": unit,
        "currency": currency,
        "text": None,
    }


def _known_action(statement: Any, known_at: datetime) -> Any:
    """按所选 publication 截止点读取公司行动修订，避免混入后续知识。"""
    return statement.where(
        EquityCorporateActionVersion.valid_from <= known_at,
        or_(
            EquityCorporateActionVersion.valid_to.is_(None),
            EquityCorporateActionVersion.valid_to > known_at,
        ),
    )


def _outside(value: date | None, request: Mapping[str, Any]) -> bool:
    """判断事件业务日期是否落在请求闭区间外。"""
    if value is None:
        return False
    start = request.get("start")
    end = request.get("end")
    return bool((start is not None and value < start) or (end is not None and value > end))


def _event_cursor_start(
    events: Sequence[Mapping[str, Any]],
    cursor: object,
    *,
    secret: bytes,
    scope: str,
    data_version: UUID,
) -> int:
    """解析统一事件 keyset 游标。"""
    if cursor is None:
        return 0
    value = _decode_cursor(str(cursor), secret=secret)
    if value.get("v") != str(data_version) or value.get("s") != scope:
        raise _snapshot_expired()
    opaque_anchor = value.get("a")
    if not isinstance(opaque_anchor, str):
        raise _validation("cursor is invalid")
    matches = [
        index
        for index, event in enumerate(events)
        if hmac.compare_digest(
            _event_anchor(
                secret=secret,
                data_version=data_version,
                event_id=str(event["eventId"]),
            ),
            opaque_anchor,
        )
    ]
    if len(matches) == 1:
        return matches[0] + 1
    raise _snapshot_expired()


def _event_next_cursor(
    events: Sequence[Mapping[str, Any]],
    *,
    start: int,
    page: Sequence[Mapping[str, Any]],
    limit: int,
    secret: bytes,
    scope: str,
    data_version: UUID,
) -> str | None:
    """签发统一事件下一页游标。"""
    if not page or start + limit >= len(events):
        return None
    return _encode_cursor(
        {
            "a": _event_anchor(
                secret=secret,
                data_version=data_version,
                event_id=str(page[-1]["eventId"]),
            ),
            "s": scope,
            "v": str(data_version),
        },
        secret=secret,
    )


def _event_anchor(*, secret: bytes, data_version: UUID, event_id: str) -> str:
    """把内部事件键转换为不可逆游标锚点，避免游标旁路泄露内部 UUID。"""
    payload = f"equity-event:{data_version}:{event_id}".encode()
    return _base64(hmac.digest(secret, payload, "sha256"))


def _dataset_status(
    session: Any,
    *,
    family: str,
    security_id: int,
    identifier_version_id: UUID,
    as_of: date | None,
    known_at: datetime | None,
    discovery: Mapping[str, EquityDiscoveryAvailability],
) -> dict[str, Any]:
    """把一个详情数据集投影为独立 availability，不借其他模型伪造事实。"""
    dataset = _STATUS_DATASETS[family]
    availability_row = discovery.get(_discovery_family(family))
    if family == "IDENTITY":
        return _identity_dataset_status(
            session,
            family=family,
            as_of=as_of,
            known_at=known_at,
            availability_row=availability_row,
        )
    if family in {"FINANCIAL_REPORT", "FINANCIAL_INDICATOR", "VALUATION"}:
        return _financial_dataset_status(
            session,
            family=family,
            security_id=security_id,
            as_of=as_of,
            known_at=known_at,
        )
    if family == "MONEY_FLOW":
        return _money_flow_dataset_status(
            session,
            family=family,
            security_id=security_id,
            as_of=as_of,
            known_at=known_at,
        )
    if family in {"INDUSTRY_MEMBERSHIP", "CONCEPT_MEMBERSHIP"}:
        return _sector_membership_dataset_status(
            session,
            family=family,
            security_id=security_id,
            as_of=as_of,
            known_at=known_at,
            availability_row=availability_row,
        )
    if family == "SW_INDUSTRY_MEMBERSHIP":
        return _sw_membership_dataset_status(
            session,
            family=family,
            security_id=security_id,
            as_of=as_of,
            known_at=known_at,
            availability_row=availability_row,
        )
    if family in _EVENT_FAMILIES:
        return _event_dataset_status(
            session,
            family=family,
            security_id=security_id,
            identifier_version_id=identifier_version_id,
            as_of=as_of,
            known_at=known_at,
        )
    partition = f"security:{security_id}" if dataset in _PER_SECURITY_DATASETS else None
    publication = (
        _maybe_publication(
            session,
            dataset=dataset,
            partition_key=partition,
            known_at=known_at,
        )
        if partition is not None
        else None
    )
    if publication is None:
        return _unavailable_status(family=family, dataset=dataset)
    source_label, methodology = _matching_discovery_metadata(
        availability_row,
        data_version=publication.data_version,
    )
    return _publication_status(
        family=family,
        dataset=dataset,
        publication=publication,
        as_of=as_of,
        source_label=source_label,
        methodology=methodology,
    )


def _identity_dataset_status(
    session: Any,
    *,
    family: str,
    as_of: date | None,
    known_at: datetime | None,
    availability_row: EquityDiscoveryAvailability | None,
) -> dict[str, Any]:
    """只在 discovery 行精确绑定 identity 组件版本时声明身份数据可用。"""
    if availability_row is None or availability_row.component_data_version is None:
        return _unavailable_status(
            family=family,
            dataset=_STATUS_DATASETS[family],
        )
    statement = select(DatasetPublication).where(
        DatasetPublication.dataset == _STATUS_DATASETS[family],
        DatasetPublication.data_version == availability_row.component_data_version,
    )
    if known_at is not None:
        statement = statement.where(
            DatasetPublication.published_at <= known_at,
            or_(
                DatasetPublication.knowledge_cutoff.is_(None),
                DatasetPublication.knowledge_cutoff <= known_at,
            ),
        )
    publication = session.scalar(statement)
    if publication is None:
        return _unavailable_status(
            family=family,
            dataset=_STATUS_DATASETS[family],
        )
    return _publication_status(
        family=family,
        dataset=publication.dataset,
        publication=publication,
        as_of=as_of,
        source_label=availability_row.source_label,
        methodology=_row_methodology(availability_row),
    )


def _status_identity_as_of(
    session: Any,
    *,
    requested_as_of: date | None,
    known_at: datetime | None,
    availability_row: EquityDiscoveryAvailability | None,
) -> date:
    """确定数据状态身份锚点；默认值只能来自 discovery 精确绑定的身份发布。"""
    if requested_as_of is not None:
        return requested_as_of
    if availability_row is None or availability_row.component_data_version is None:
        raise _publication_unavailable(
            "Equity identity publication is unavailable for default data-status query"
        )
    statement = select(DatasetPublication).where(
        DatasetPublication.dataset == _STATUS_DATASETS["IDENTITY"],
        DatasetPublication.data_version == availability_row.component_data_version,
    )
    if known_at is not None:
        statement = statement.where(
            DatasetPublication.published_at <= known_at,
            or_(
                DatasetPublication.knowledge_cutoff.is_(None),
                DatasetPublication.knowledge_cutoff <= known_at,
            ),
        )
    publication = session.scalar(statement)
    if publication is None or publication.effective_as_of is None:
        raise _publication_unavailable("Equity identity publication effectiveAsOf is unavailable")
    return publication.effective_as_of


def _financial_dataset_status(
    session: Any,
    *,
    family: str,
    security_id: int,
    as_of: date | None,
    known_at: datetime | None,
) -> dict[str, Any]:
    """按证券和财务方法学明细选择 publication，不猜 `{security_id}` 通用分区。"""
    capabilities = (
        ("financial.provider-metric", "financial.derived-metric")
        if family == "FINANCIAL_INDICATOR"
        else (_STATUS_DATASETS[family],)
    )
    statement = (
        select(
            DatasetPublication,
            FinancialPublication.capability,
            FinancialMethodology,
        )
        .join(
            FinancialPublication,
            FinancialPublication.data_version == DatasetPublication.data_version,
        )
        .join(
            FinancialMethodology,
            FinancialMethodology.methodology_id == FinancialPublication.methodology_id,
        )
        .where(
            FinancialPublication.capability.in_(capabilities),
            FinancialPublication.security_id == security_id,
            FinancialMethodology.status == "validated",
        )
    )
    statement = _publication_visibility(statement, known_at=known_at)
    rows = session.execute(statement).all()
    if family == "FINANCIAL_INDICATOR":
        return _financial_indicator_status(
            rows=rows,
            family=family,
            as_of=as_of,
        )
    candidates = [
        (
            publication,
            methodology.source_code,
            {"code": methodology.code, "version": str(methodology.version)},
        )
        for publication, _capability, methodology in rows
    ]
    return _candidate_status(
        family=family,
        dataset=_STATUS_DATASETS[family],
        candidates=candidates,
        as_of=as_of,
    )


def _financial_indicator_status(
    *,
    rows: Sequence[tuple[DatasetPublication, str, FinancialMethodology]],
    family: str,
    as_of: date | None,
) -> dict[str, Any]:
    """组合指标质量，并把可消费版本精确锚定到平台派生 leaf。"""
    grouped: dict[
        str,
        dict[UUID, tuple[DatasetPublication, str | None, dict[str, str] | None]],
    ] = defaultdict(dict)
    for publication, capability, methodology in rows:
        grouped[capability][publication.publication_id] = (
            publication,
            methodology.source_code,
            {"code": methodology.code, "version": str(methodology.version)},
        )
    required = {"financial.provider-metric", "financial.derived-metric"}
    if not grouped:
        return _unavailable_status(
            family=family,
            dataset=_STATUS_DATASETS[family],
        )
    if any(len(values) != 1 for values in grouped.values()):
        return _candidate_status(
            family=family,
            dataset=_STATUS_DATASETS[family],
            candidates=[candidate for values in grouped.values() for candidate in values.values()],
            as_of=as_of,
        )
    candidates = [next(iter(values.values())) for values in grouped.values()]
    publications = [candidate[0] for candidate in candidates]
    effective_dates = [
        publication.effective_as_of
        for publication in publications
        if publication.effective_as_of is not None
    ]
    knowledge_cutoffs = [
        publication.knowledge_cutoff
        for publication in publications
        if publication.knowledge_cutoff is not None
    ]
    complete = set(grouped) == required
    derived_values = grouped.get("financial.derived-metric", {})
    derived = next(iter(derived_values.values())) if len(derived_values) == 1 else None
    quality = [_publication_quality(item.quality_status) for item in publications]
    failed = next(
        (item for item in quality if item[0] == "SOURCE_UNAVAILABLE"),
        None,
    )
    warned = any(item[0] == "PARTIAL" for item in quality)
    if failed is not None:
        availability, reason_code, retryable = failed
    elif not complete:
        availability = "PARTIAL"
        reason_code = "FINANCIAL_COMPONENT_PARTIAL"
        retryable = True
    elif warned:
        availability = "PARTIAL"
        reason_code = "QUALITY_WARNING"
        retryable = False
    else:
        availability = "AVAILABLE"
        reason_code = None
        retryable = False
    return _status_payload(
        family=family,
        dataset=_STATUS_DATASETS[family],
        availability=availability,
        data_version=None if derived is None else derived[0].data_version,
        published_at=max(publication.published_at for publication in publications),
        effective_as_of=min(effective_dates) if effective_dates else None,
        knowledge_cutoff=min(knowledge_cutoffs) if knowledge_cutoffs else None,
        as_of=as_of,
        source_label=derived[1] if derived is not None else None,
        methodology=derived[2] if derived is not None else None,
        reason_code=reason_code,
        retryable=retryable,
    )


def _money_flow_dataset_status(
    session: Any,
    *,
    family: str,
    security_id: int,
    as_of: date | None,
    known_at: datetime | None,
) -> dict[str, Any]:
    """按证券强身份序列定位资金流 publication，研究方法学绝不提升为可用。"""
    methods = session.execute(
        select(
            MoneyFlowSeries,
            MoneyFlowMethodology,
            MoneyFlowMethodologyVersion,
        )
        .join(
            MoneyFlowMethodologyVersion,
            MoneyFlowMethodologyVersion.version_id == MoneyFlowSeries.methodology_version_id,
        )
        .join(
            MoneyFlowMethodology,
            MoneyFlowMethodology.methodology_id == MoneyFlowMethodologyVersion.methodology_id,
        )
        .where(
            MoneyFlowSeries.scope_type == "equity",
            MoneyFlowSeries.security_id == security_id,
            MoneyFlowSeries.window_type == "daily_source",
            MoneyFlowMethodologyVersion.production_enabled.is_(True),
            MoneyFlowMethodologyVersion.status == "validated",
        )
    ).all()
    candidates: list[tuple[DatasetPublication, str | None, dict[str, str] | None]] = []
    for series, methodology, version in methods:
        publication = _maybe_publication(
            session,
            dataset=_STATUS_DATASETS[family],
            partition_key=f"series:{series.series_id}",
            known_at=known_at,
        )
        if publication is not None:
            candidates.append(
                (
                    publication,
                    version.upstream_source,
                    {"code": methodology.public_key, "version": version.version},
                )
            )
    if not candidates:
        return _unavailable_status(
            family=family,
            dataset=_STATUS_DATASETS[family],
            reason_code="METHODOLOGY_NOT_FROZEN",
            retryable=False,
        )
    return _candidate_status(
        family=family,
        dataset=_STATUS_DATASETS[family],
        candidates=candidates,
        as_of=as_of,
    )


def _sector_membership_dataset_status(
    session: Any,
    *,
    family: str,
    security_id: int,
    as_of: date | None,
    known_at: datetime | None,
    availability_row: EquityDiscoveryAvailability | None,
) -> dict[str, Any]:
    """以完整 scheme release 证明单证券行业/概念归属或合法空集。"""
    scheme = "eastmoney.industry" if family == "INDUSTRY_MEMBERSHIP" else "eastmoney.concept"
    statement = select(SectorMembershipRelease).where(SectorMembershipRelease.scheme == scheme)
    statement = _release_visibility(
        statement,
        model=SectorMembershipRelease,
        known_at=known_at,
    ).order_by(SectorMembershipRelease.published_at.desc())
    release = session.scalar(statement.limit(1))
    if release is None:
        return _unavailable_status(
            family=family,
            dataset=_STATUS_DATASETS[family],
        )
    has_membership = bool(
        session.scalar(
            select(
                exists(
                    select(1)
                    .select_from(SectorMembershipReleaseSector)
                    .join(
                        SectorMembershipItem,
                        SectorMembershipItem.snapshot_id
                        == SectorMembershipReleaseSector.snapshot_id,
                    )
                    .where(
                        SectorMembershipReleaseSector.release_id == release.release_id,
                        SectorMembershipItem.security_id == security_id,
                    )
                )
            )
        )
    )
    source_label, methodology = _matching_discovery_metadata(
        availability_row,
        data_version=release.data_version,
    )
    quality_warned = release.quality_status.casefold() != "passed"
    return _status_payload(
        family=family,
        dataset=_STATUS_DATASETS[family],
        availability=(
            "PARTIAL" if quality_warned else ("AVAILABLE" if has_membership else "EMPTY")
        ),
        data_version=release.data_version,
        published_at=release.published_at,
        effective_as_of=release.release_as_of.date(),
        knowledge_cutoff=release.published_at,
        as_of=as_of,
        source_label=source_label,
        methodology=methodology,
        reason_code=(
            "QUALITY_WARNING" if quality_warned else (None if has_membership else "NO_MEMBERSHIP")
        ),
        retryable=False,
    )


def _sw_membership_dataset_status(
    session: Any,
    *,
    family: str,
    security_id: int,
    as_of: date | None,
    known_at: datetime | None,
    availability_row: EquityDiscoveryAvailability | None,
) -> dict[str, Any]:
    """以 discovery 实际展示版本校验申万归属，并显式标记 leaf 更新窗口。"""
    dataset = _STATUS_DATASETS[family]
    if availability_row is None or availability_row.component_data_version is None:
        return _unavailable_status(
            family=family,
            dataset=dataset,
            reason_code="DISCOVERY_COMPONENT_UNAVAILABLE",
        )
    bound_statement = (
        select(DatasetPublication)
        .join(
            SwMembershipRelease,
            SwMembershipRelease.release_id == DatasetPublication.release_id,
        )
        .join(
            SwMembershipItem,
            SwMembershipItem.release_id == SwMembershipRelease.release_id,
        )
        .where(
            DatasetPublication.dataset == dataset,
            DatasetPublication.data_version == availability_row.component_data_version,
            SwMembershipItem.security_id == security_id,
            SwMembershipItem.resolution_status == "RESOLVED",
        )
    )
    if known_at is not None:
        bound_statement = bound_statement.where(
            DatasetPublication.published_at <= known_at,
            or_(
                DatasetPublication.knowledge_cutoff.is_(None),
                DatasetPublication.knowledge_cutoff <= known_at,
            ),
        )
    bound = session.scalar(bound_statement.limit(1))
    if bound is None:
        return _unavailable_status(
            family=family,
            dataset=dataset,
            reason_code="DISCOVERY_COMPONENT_FACT_UNAVAILABLE",
        )
    latest_statement = (
        select(DatasetPublication)
        .join(
            SwMembershipRelease,
            SwMembershipRelease.release_id == DatasetPublication.release_id,
        )
        .where(DatasetPublication.dataset == dataset)
    )
    latest_statement = _publication_visibility(latest_statement, known_at=known_at)
    latest = session.scalar(
        latest_statement.order_by(
            DatasetPublication.effective_as_of.desc().nulls_last(),
            DatasetPublication.published_at.desc(),
            DatasetPublication.publication_id.desc(),
        ).limit(1)
    )
    if latest is None:
        return _unavailable_status(family=family, dataset=dataset)
    source_label, methodology = _matching_discovery_metadata(
        availability_row,
        data_version=bound.data_version,
    )
    status = _publication_status(
        family=family,
        dataset=dataset,
        publication=bound,
        as_of=as_of,
        source_label=source_label,
        methodology=methodology,
    )
    if UUID(str(latest.data_version)) == UUID(str(bound.data_version)):
        return status
    if status["availability"] == "SOURCE_UNAVAILABLE":
        return status
    # Web 展示的是 discovery 冻结归属；leaf 已前进时必须暴露 stale，等待 discovery 重建。
    status.update(
        availability="PARTIAL",
        freshness="STALE",
        reasonCode="DISCOVERY_COMPONENT_STALE",
        retryable=True,
    )
    return status


def _event_dataset_status(
    session: Any,
    *,
    family: str,
    security_id: int,
    identifier_version_id: UUID,
    as_of: date | None,
    known_at: datetime | None,
) -> dict[str, Any]:
    """以同一 coverage selector 证明事件族，并让合法零记录窗口得到 `EMPTY`。"""
    dataset = _STATUS_DATASETS[family]
    if as_of is None:
        selection = _latest_event_coverage_selection(
            session,
            family=family,
            security_id=security_id,
            identifier_version_id=identifier_version_id,
            known_at=known_at,
        )
    else:
        selection = _event_coverage_selection(
            session,
            family=family,
            security_id=security_id,
            identifier_version_id=identifier_version_id,
            start=as_of,
            end=as_of,
            known_at=known_at,
        )
    if selection is None:
        return _unavailable_status(
            family=family,
            dataset=dataset,
            reason_code="NO_COVERAGE",
        )
    fact_publication = (
        _event_fact_publication(
            session,
            dataset=dataset,
            security_id=security_id,
            selection=selection,
        )
        if family == "CORPORATE_ACTION"
        else None
    )
    if family == "CORPORATE_ACTION" and fact_publication is None:
        # coverage manifest 能证明零事件窗口，但公开公司行动 leaf 读取的是证券累积事实
        # publication。两者缺一不可，不能把 coverage 版本伪装成 leaf 可消费版本。
        return _unavailable_status(
            family=family,
            dataset=dataset,
            reason_code="FACT_PUBLICATION_UNAVAILABLE",
        )
    publications = [item.evidence.publication for item in selection.segments]
    if fact_publication is not None:
        publications.append(fact_publication)
    quality = next(
        (
            status
            for publication in publications
            if (status := _publication_quality(publication.quality_status))[0] != "AVAILABLE"
        ),
        ("AVAILABLE", None, False),
    )
    status_from = as_of or selection.coverage_from
    status_to = as_of or selection.coverage_to
    facts = _event_rows(
        session,
        security_id=security_id,
        request={
            "families": [family],
            "start": status_from,
            "end": status_to,
        },
        coverages={family: selection},
    )
    declared_empty = all(item.evidence.coverage.record_count == 0 for item in selection.segments)
    if declared_empty and facts:
        return _unavailable_status(
            family=family,
            dataset=dataset,
            reason_code="COVERAGE_FACT_MISMATCH",
            retryable=False,
        )
    empty = not facts
    return _status_payload(
        family=family,
        dataset=dataset,
        availability="EMPTY" if empty else quality[0],
        # 公司行动状态必须交付 leaf 可验证的事实 publication；coverage composite 仅用于
        # 状态证明，不能直接作为 `/corporate-actions` 的 dataVersion。
        data_version=(
            fact_publication.data_version
            if fact_publication is not None
            else selection.data_version
        ),
        published_at=(
            fact_publication.published_at
            if fact_publication is not None
            else max(item.evidence.coverage.created_at for item in selection.segments)
        ),
        effective_as_of=as_of or selection.coverage_to,
        knowledge_cutoff=(
            fact_publication.knowledge_cutoff
            if fact_publication is not None
            else selection.view_cutoff
        ),
        as_of=as_of,
        source_label=selection.source_label,
        methodology={
            "code": selection.methodology_code,
            "version": str(selection.methodology_version),
        },
        reason_code="NO_EVENTS" if empty else quality[1],
        retryable=False if empty else quality[2],
    )


def _event_fact_publication(
    session: Any,
    *,
    dataset: str,
    security_id: int,
    selection: _EventCoverageSelection,
) -> DatasetPublication | None:
    """按 coverage 已冻结的知识截止点解析公司行动 leaf 的证券事实版本。

    coverage manifest 与累积公司行动事实使用不同 publication；只用 coverage 的
    `view_cutoff` 查询 `security:{security_id}` 分区，可避免较晚的事实发布倒灌进已选择的
    零事件窗口，同时让 data-status 返回 leaf 能严格验证的真实 `dataVersion`。
    """
    return _maybe_publication(
        session,
        dataset=dataset,
        partition_key=f"security:{security_id}",
        known_at=selection.view_cutoff,
    )


def _latest_event_coverage_selection(
    session: Any,
    *,
    family: str,
    security_id: int,
    identifier_version_id: UUID,
    known_at: datetime | None,
) -> _EventCoverageSelection | None:
    """在未指定业务日时先锚定最新覆盖业务日，晚补旧窗不得让状态日期倒退。"""
    dataset = _EVENT_DATASETS[family]
    candidates = _event_coverage_candidates(
        session,
        family=family,
        dataset=dataset,
        security_id=security_id,
        identifier_version_id=identifier_version_id,
        start=None,
        end=None,
        known_at=known_at,
    )
    if not candidates:
        return None
    latest_business_date = max(item.coverage.coverage_to for item in candidates)
    return _select_event_coverage(
        candidates,
        family=family,
        dataset=dataset,
        security_id=security_id,
        identifier_version_id=identifier_version_id,
        start=latest_business_date,
        end=latest_business_date,
    )


def _candidate_status(
    *,
    family: str,
    dataset: str,
    candidates: Sequence[tuple[DatasetPublication, str | None, dict[str, str] | None]],
    as_of: date | None,
) -> dict[str, Any]:
    """把零、一或多个精确 publication 变为保守状态，不静默任选方法学。"""
    unique = {
        publication.publication_id: (publication, source_label, methodology)
        for publication, source_label, methodology in candidates
    }
    values = list(unique.values())
    if not values:
        return _unavailable_status(family=family, dataset=dataset)
    if len(values) == 1:
        publication, source_label, methodology = values[0]
        return _publication_status(
            family=family,
            dataset=dataset,
            publication=publication,
            as_of=as_of,
            source_label=source_label,
            methodology=methodology,
        )
    publications = [item[0] for item in values]
    effective_dates = [
        publication.effective_as_of
        for publication in publications
        if publication.effective_as_of is not None
    ]
    knowledge_cutoffs = [
        publication.knowledge_cutoff
        for publication in publications
        if publication.knowledge_cutoff is not None
    ]
    # 任一候选未过质量门禁时，禁止“多版本”部分状态掩盖不可消费事实。
    failed = next(
        (
            quality
            for publication in publications
            if (quality := _publication_quality(publication.quality_status))[0]
            == "SOURCE_UNAVAILABLE"
        ),
        None,
    )
    composite = _composite_version(
        f"equity-data-status:{family}",
        sorted(
            (str(publication.data_version), publication.quality_status.casefold())
            for publication in publications
        ),
    )
    return _status_payload(
        family=family,
        dataset=dataset,
        availability=failed[0] if failed is not None else "PARTIAL",
        data_version=composite,
        published_at=max(publication.published_at for publication in publications),
        effective_as_of=min(effective_dates) if effective_dates else None,
        knowledge_cutoff=min(knowledge_cutoffs) if knowledge_cutoffs else None,
        as_of=as_of,
        source_label=None,
        methodology=None,
        reason_code=failed[1] if failed is not None else "MULTIPLE_PUBLICATIONS",
        retryable=failed[2] if failed is not None else False,
    )


def _publication_status(
    *,
    family: str,
    dataset: str,
    publication: DatasetPublication,
    as_of: date | None,
    source_label: str | None,
    methodology: dict[str, str] | None,
) -> dict[str, Any]:
    """投影一个精确 publication 的状态元数据。"""
    availability, reason_code, retryable = _publication_quality(publication.quality_status)
    return _status_payload(
        family=family,
        dataset=dataset,
        availability=availability,
        data_version=publication.data_version,
        published_at=publication.published_at,
        effective_as_of=publication.effective_as_of,
        knowledge_cutoff=publication.knowledge_cutoff,
        as_of=as_of,
        source_label=source_label,
        methodology=methodology,
        reason_code=reason_code,
        retryable=retryable,
    )


def _publication_quality(quality_status: str) -> tuple[str, str | None, bool]:
    """把 publication 质量门禁统一投影为 availability、原因和重试语义。"""
    normalized = quality_status.casefold()
    if normalized == "passed":
        return "AVAILABLE", None, False
    if normalized in {"warning", "warned", "partial"}:
        return "PARTIAL", "QUALITY_WARNING", False
    if normalized == "failed":
        return "SOURCE_UNAVAILABLE", "QUALITY_FAILED", True
    return "SOURCE_UNAVAILABLE", "QUALITY_STATUS_UNKNOWN", False


def _status_payload(
    *,
    family: str,
    dataset: str,
    availability: str,
    data_version: UUID | None,
    published_at: datetime | None,
    effective_as_of: date | None,
    knowledge_cutoff: datetime | None,
    as_of: date | None,
    source_label: str | None,
    methodology: dict[str, str] | None,
    reason_code: str | None,
    retryable: bool,
) -> dict[str, Any]:
    """构造数据状态合同，并按请求业务日计算陈旧度。"""
    freshness = (
        "UNKNOWN"
        if effective_as_of is None
        else "STALE"
        if as_of is not None and effective_as_of < as_of
        else "FRESH"
    )
    return {
        "family": family,
        "dataset": dataset,
        "availability": availability,
        "freshness": freshness,
        "dataVersion": str(data_version) if data_version is not None else None,
        "publishedAt": _timestamp(published_at),
        "effectiveAsOf": _date(effective_as_of),
        "knowledgeCutoff": _timestamp(knowledge_cutoff),
        "sourceLabel": source_label,
        "methodology": methodology,
        "reasonCode": reason_code,
        "retryable": retryable,
    }


def _unavailable_status(
    *,
    family: str,
    dataset: str,
    reason_code: str = "NO_PUBLICATION",
    retryable: bool = True,
) -> dict[str, Any]:
    """构造无法证明 publication 的保守状态。"""
    return _status_payload(
        family=family,
        dataset=dataset,
        availability="SOURCE_UNAVAILABLE",
        data_version=None,
        published_at=None,
        effective_as_of=None,
        knowledge_cutoff=None,
        as_of=None,
        source_label=None,
        methodology=None,
        reason_code=reason_code,
        retryable=retryable,
    )


def _matching_discovery_metadata(
    availability: EquityDiscoveryAvailability | None,
    *,
    data_version: UUID,
) -> tuple[str | None, dict[str, str] | None]:
    """仅当 discovery 行引用同一组件版本时复用展示来源与方法学。"""
    if (
        availability is None
        or availability.component_data_version is None
        or UUID(str(availability.component_data_version)) != UUID(str(data_version))
    ):
        return None, None
    return availability.source_label, _row_methodology(availability)


def _discovery_family(family: str) -> str:
    """把详情 family 映射到 discovery 原因化语义族。"""
    return {
        "IDENTITY": "identity",
        "BARS_1D": "market",
        "VALUATION": "valuation",
        "MONEY_FLOW": "money_flow",
        "INDUSTRY_MEMBERSHIP": "industry",
        "CONCEPT_MEMBERSHIP": "concept",
        "SW_INDUSTRY_MEMBERSHIP": "sw",
    }.get(family, family.casefold())


def _discovery_availability(
    session: Any,
    *,
    security_id: int,
    as_of: date | None,
    known_at: datetime | None,
) -> dict[str, EquityDiscoveryAvailability]:
    """读取请求知识时点的同证券 discovery 元数据；组件版本仍需逐项精确匹配。"""
    publication = _maybe_publication(
        session,
        dataset=_DISCOVERY_DATASET,
        partition_key=_DISCOVERY_PARTITION,
        known_at=known_at,
    )
    if publication is None or publication.release_id is None:
        return {}
    # `as_of` 只用于身份与 freshness；来源标签必须再通过 component dataVersion 精确匹配，
    # 因而这里可复用同证券当前 discovery 的 identity 证明，而不会借用别日行情口径。
    _ = as_of
    rows = session.scalars(
        select(EquityDiscoveryAvailability).where(
            EquityDiscoveryAvailability.release_id == publication.release_id,
            EquityDiscoveryAvailability.security_id == security_id,
        )
    ).all()
    return {row.family: row for row in rows}


def _identity(
    session: Any,
    *,
    exchange: str,
    symbol: str,
    as_of: date | None,
    known_at: datetime | None,
) -> tuple[EquityInstrument, EquityIdentifierVersion]:
    """按业务日和知识时点解析唯一代码身份；默认只接受仍开放的当前身份。"""
    if exchange not in _EXCHANGES:
        raise _validation("exchange is invalid")
    statement = _identifier_statement(
        exchange=exchange,
        symbol=symbol,
        known_at=known_at,
    )
    if as_of is None:
        statement = statement.where(EquityIdentifierVersion.effective_to.is_(None))
    else:
        statement = statement.where(
            EquityIdentifierVersion.effective_from <= as_of,
            or_(
                EquityIdentifierVersion.effective_to.is_(None),
                EquityIdentifierVersion.effective_to > as_of,
            ),
        )
    rows = session.execute(statement).all()
    return _single_identity(rows)


def _identity_name(
    session: Any,
    *,
    security_id: int,
    as_of: date,
    known_at: datetime | None,
) -> EquityNameVersion:
    """按与 identifier 相同的业务日和知识时点解析唯一证券名称版本。"""
    statement = select(EquityNameVersion).where(
        EquityNameVersion.security_id == security_id,
        EquityNameVersion.effective_from <= as_of,
        or_(
            EquityNameVersion.effective_to.is_(None),
            EquityNameVersion.effective_to > as_of,
        ),
    )
    if known_at is None:
        statement = statement.where(EquityNameVersion.known_to.is_(None))
    else:
        statement = statement.where(
            EquityNameVersion.known_from <= known_at,
            or_(
                EquityNameVersion.known_to.is_(None),
                EquityNameVersion.known_to > known_at,
            ),
        )
    rows = session.scalars(statement).all()
    if len(rows) != 1:
        raise InternalProblem(
            status=409,
            code="identity-incomplete",
            detail="Equity name identity is unavailable or ambiguous",
        )
    return rows[0]


def _identity_for_event_window(
    session: Any,
    *,
    exchange: str,
    symbol: str,
    request: Mapping[str, Any],
) -> tuple[EquityInstrument, EquityIdentifierVersion]:
    """用独立身份锚点解析证券，并要求事件窗口不越过代码有效范围。"""
    if exchange not in _EXCHANGES:
        raise _validation("exchange is invalid")
    as_of = request.get("asOf")
    start = request.get("start")
    end = request.get("end")
    if as_of is not None:
        identity, identifier = _identity(
            session,
            exchange=exchange,
            symbol=symbol,
            as_of=as_of,
            known_at=request.get("knownAt"),
        )
        for boundary in (start, end):
            if boundary is None:
                continue
            if boundary < identifier.effective_from or (
                identifier.effective_to is not None and boundary >= identifier.effective_to
            ):
                raise _identity_conflict()
        return identity, identifier
    statement = _identifier_statement(
        exchange=exchange,
        symbol=symbol,
        known_at=request.get("knownAt"),
    )
    if start is None and end is None:
        statement = statement.where(EquityIdentifierVersion.effective_to.is_(None))
    else:
        if end is not None:
            statement = statement.where(EquityIdentifierVersion.effective_from <= end)
        if start is not None:
            statement = statement.where(
                or_(
                    EquityIdentifierVersion.effective_to.is_(None),
                    EquityIdentifierVersion.effective_to > start,
                )
            )
    rows = session.execute(statement).all()
    identity, identifier = _single_identity(rows, conflict_on_multiple=True)
    if (
        start is not None
        and identifier.effective_from > start
        or end is not None
        and identifier.effective_to is not None
        and identifier.effective_to <= end
    ):
        raise _identity_conflict()
    return identity, identifier


def _identifier_statement(
    *,
    exchange: str,
    symbol: str,
    known_at: datetime | None,
) -> Any:
    """构造已确认 identifier 与永久证券锚的双时态查询。"""
    statement = (
        select(EquityInstrument, EquityIdentifierVersion)
        .join(
            EquityIdentifierVersion,
            EquityIdentifierVersion.security_id == EquityInstrument.security_id,
        )
        .where(
            EquityIdentifierVersion.exchange == exchange,
            EquityIdentifierVersion.symbol == symbol,
            EquityIdentifierVersion.identity_state == "CONFIRMED",
            EquityInstrument.master_confirmed_at.is_not(None),
            EquityInstrument.listing_status != "PENDING",
        )
    )
    if known_at is None:
        return statement.where(EquityIdentifierVersion.known_to.is_(None))
    return statement.where(
        EquityIdentifierVersion.known_from <= known_at,
        or_(
            EquityIdentifierVersion.known_to.is_(None),
            EquityIdentifierVersion.known_to > known_at,
        ),
    )


def _single_identity(
    rows: Sequence[Any],
    *,
    conflict_on_multiple: bool = True,
) -> tuple[EquityInstrument, EquityIdentifierVersion]:
    """把双时态 identifier 候选收敛为一个永久身份，并区分不存在与代码复用冲突。"""
    if not rows:
        raise InternalProblem(status=404, code="not-found", detail="Equity is not found")
    if len(rows) != 1:
        if conflict_on_multiple:
            raise _identity_conflict()
        raise InternalProblem(status=404, code="not-found", detail="Equity is not found")
    return rows[0]


def _discovery_publication(session: Any, *, data_version: object) -> DatasetPublication:
    """读取请求版本或当前 discovery publication，无发布时稳定返回 503。"""
    statement = select(DatasetPublication).where(
        DatasetPublication.dataset == _DISCOVERY_DATASET,
        DatasetPublication.partition_key == _DISCOVERY_PARTITION,
        DatasetPublication.release_id.is_not(None),
    )
    if data_version is None:
        statement = statement.where(DatasetPublication.superseded_at.is_(None))
    else:
        statement = statement.where(
            DatasetPublication.data_version == _uuid(data_version, "dataVersion")
        )
    publication = session.scalar(statement)
    if publication is None:
        if data_version is not None:
            raise _snapshot_expired()
        raise _publication_unavailable("Equity discovery publication is unavailable")
    return publication


def _maybe_publication(
    session: Any,
    *,
    dataset: str,
    partition_key: str,
    known_at: datetime | None,
) -> DatasetPublication | None:
    """读取精确分区在请求知识时点可见的 publication。"""
    statement = (
        select(DatasetPublication)
        .where(
            DatasetPublication.dataset == dataset,
            DatasetPublication.partition_key == partition_key,
        )
        .order_by(DatasetPublication.published_at.desc())
    )
    statement = _publication_visibility(statement, known_at=known_at)
    return session.scalar(statement.limit(1))


def _publications_at(
    session: Any,
    *,
    dataset: str,
    known_at: datetime | None,
    require_release: bool,
) -> tuple[DatasetPublication, ...]:
    """读取多分区数据集在请求知识时点实际可见的 publication 集合。"""
    statement = select(DatasetPublication).where(DatasetPublication.dataset == dataset)
    if require_release:
        statement = statement.where(DatasetPublication.release_id.is_not(None))
    statement = _publication_visibility(statement, known_at=known_at)
    return tuple(session.scalars(statement.order_by(DatasetPublication.partition_key)).all())


def _publication_visibility(statement: Any, *, known_at: datetime | None) -> Any:
    """把通用 publication 指针限制到当前或历史知识切片。"""
    if known_at is None:
        return statement.where(DatasetPublication.superseded_at.is_(None))
    return statement.where(
        DatasetPublication.published_at <= known_at,
        or_(
            DatasetPublication.superseded_at.is_(None),
            DatasetPublication.superseded_at > known_at,
        ),
        or_(
            DatasetPublication.knowledge_cutoff.is_(None),
            DatasetPublication.knowledge_cutoff <= known_at,
        ),
    )


def _release_visibility(
    statement: Any,
    *,
    model: Any,
    known_at: datetime | None,
) -> Any:
    """把具有发布/替换列的专用 release 限制到请求知识切片。"""
    if known_at is None:
        return statement.where(model.superseded_at.is_(None))
    return statement.where(
        model.published_at <= known_at,
        or_(model.superseded_at.is_(None), model.superseded_at > known_at),
    )


def _release(publication: DatasetPublication, *, completeness: str | None) -> dict[str, Any]:
    """投影消费者可复验 release 元数据。"""
    result = {
        "dataset": publication.dataset,
        "dataVersion": str(publication.data_version),
        "publishedAt": _timestamp(publication.published_at),
        "effectiveAsOf": _date(publication.effective_as_of),
        "knowledgeCutoff": _timestamp(publication.knowledge_cutoff),
        "qualityStatus": (
            "warning"
            if publication.quality_status in {"warned", "partial"}
            else publication.quality_status
        ),
    }
    if completeness is not None:
        result["completeness"] = completeness
    return result


def _conditional_response(
    *,
    request: Request,
    if_none_match: str | None,
    data_version: UUID,
    etag: str,
    body: Mapping[str, Any],
) -> Response:
    """返回强 ETag、请求关联和数据版本；命中条件读取时不返回正文。"""
    headers = {
        "ETag": etag,
        "Cache-Control": _PRIVATE_REVALIDATE,
        "X-Request-Id": _request_id(request),
        "X-Data-Version": str(data_version),
    }
    if if_none_match == etag:
        return Response(status_code=304, headers=headers)
    return JSONResponse(content=dict(body), headers=headers)


def _etag(kind: str, data_version: object, request: Mapping[str, Any]) -> str:
    """把发布版本和标准请求绑定为强 ETag。"""
    digest = hashlib.sha256(
        json.dumps(request, default=_json_default, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:24]
    return f'"{kind}-{data_version}-{digest}"'


def _scope(value: Mapping[str, Any]) -> str:
    """计算游标请求范围摘要。"""
    return hashlib.sha256(
        json.dumps(value, default=_json_default, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:24]


def _composite_version(kind: str, value: object) -> UUID:
    """由实际组件集合生成稳定复合 UUID，组件变化必然使条件读取失效。"""
    canonical = json.dumps(
        value,
        default=_json_default,
        sort_keys=True,
        separators=(",", ":"),
    )
    return uuid5(NAMESPACE_URL, f"quant-v2:{kind}:{canonical}")


def _encode_cursor(value: Mapping[str, Any], *, secret: bytes) -> str:
    """签发 HMAC-SHA256 不透明游标。"""
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    signature = hmac.digest(secret, payload, "sha256")
    return f"{_base64(payload)}.{_base64(signature)}"


def _decode_cursor(value: str, *, secret: bytes) -> Mapping[str, Any]:
    """严格解码并验签游标。"""
    payload_text, separator, signature_text = value.partition(".")
    if separator != "." or not payload_text or not signature_text:
        raise _validation("cursor is invalid")
    try:
        payload = _unbase64(payload_text)
        signature = _unbase64(signature_text)
        decoded = json.loads(payload)
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise _validation("cursor is invalid") from error
    if not hmac.compare_digest(signature, hmac.digest(secret, payload, "sha256")):
        raise _validation("cursor is invalid")
    if not isinstance(decoded, dict):
        raise _validation("cursor is invalid")
    return decoded


def _base64(value: bytes) -> str:
    """编码无填充 URL 安全文本。"""
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _unbase64(value: str) -> bytes:
    """严格解码 URL 安全 base64。"""
    return base64.b64decode(
        value + "=" * (-len(value) % 4),
        altchars=b"-_",
        validate=True,
    )


def _trading_status(value: str) -> str:
    """把存储状态映射到内部合同，未知必须保持未知。"""
    if value == "SUSPENDED":
        return "TRADE_SUSPENDED"
    if value == "RESUMED":
        return "UNKNOWN"
    return value if value in _TRADING else "UNKNOWN"


def _row_methodology(
    value: EquityDiscoveryAvailability | None,
) -> dict[str, str] | None:
    """读取可用性行方法学。"""
    return None if value is None or value.methodology is None else _methodology(value.methodology)


def _methodology(value: Mapping[str, Any]) -> dict[str, str] | None:
    """规范化方法学代码与版本。"""
    code = value.get("code")
    version = value.get("version")
    if code is None or version is None:
        return None
    return {"code": str(code), "version": str(version)}


def _pair_methodology(code: object, version: object) -> dict[str, str] | None:
    """从两个可空列构造方法学。"""
    if code is None or version is None:
        return None
    return {"code": str(code), "version": str(version)}


def _null_reason(value: EquityDiscoveryAvailability | None) -> str | None:
    """读取稳定缺失原因。"""
    return None if value is None else value.null_reason


def _date(value: date | None) -> str | None:
    """序列化可空日期。"""
    return None if value is None else value.isoformat()


def _timestamp(value: datetime | None) -> str | None:
    """序列化可空 UTC 时间。"""
    if value is None:
        return None
    current = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return current.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _decimal(value: object) -> str | None:
    """精确序列化可空数值。"""
    return None if value is None else str(value)


def _request_id(request: Request) -> str:
    """复用有界请求标识，否则生成 UUID。"""
    value = request.headers.get("X-Request-Id")
    return value if value is not None and 1 <= len(value) <= 128 else str(uuid4())


def _json_default(value: object) -> str:
    """序列化请求摘要中的日期、时间和 UUID。"""
    if isinstance(value, (date, datetime, UUID, Decimal)):
        return str(value)
    raise TypeError("value is not JSON serializable")


def _reject_unknown(value: Mapping[str, Any], allowed: set[str]) -> None:
    """拒绝未知请求字段。"""
    if set(value) - allowed:
        raise _validation("request contains unknown fields")


def _string_list(
    value: object,
    name: str,
    *,
    allowed: frozenset[str],
    maximum: int,
) -> None:
    """校验非空、去重、受控枚举数组。"""
    if (
        not isinstance(value, list)
        or not 1 <= len(value) <= maximum
        or len(set(value)) != len(value)
        or any(not isinstance(item, str) or item not in allowed for item in value)
    ):
        raise _validation(f"{name} is invalid")


def _integer(value: object, name: str, *, minimum: int, maximum: int) -> int:
    """校验有界整数。"""
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise _validation(f"{name} is invalid")
    return value


def _optional_date(value: object, name: str) -> date | None:
    """解析可空 ISO 日期。"""
    if value is None:
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError as error:
        raise _validation(f"{name} is invalid") from error


def _optional_datetime(value: object, name: str) -> datetime | None:
    """解析必须带时区的可空 RFC 3339 时间。"""
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as error:
        raise _validation(f"{name} is invalid") from error
    if parsed.tzinfo is None:
        raise _validation(f"{name} is invalid")
    return parsed


def _uuid(value: object, name: str) -> UUID:
    """解析 UUID。"""
    try:
        return UUID(str(value))
    except (TypeError, ValueError, AttributeError) as error:
        raise _validation(f"{name} is invalid") from error


def _validation(detail: str) -> InternalProblem:
    """构造请求校验问题。"""
    return InternalProblem(status=400, code="validation-error", detail=detail)


def _publication_unavailable(detail: str) -> InternalProblem:
    """构造无 publication 问题。"""
    return InternalProblem(status=503, code="publication-unavailable", detail=detail)


def _snapshot_expired() -> InternalProblem:
    """构造游标发布已变化问题。"""
    return InternalProblem(
        status=409,
        code="snapshot-expired",
        detail="Published equity workspace snapshot changed",
    )


def _identity_conflict() -> InternalProblem:
    """构造代码复用或请求窗口跨越 identifier 范围的冲突。"""
    return InternalProblem(
        status=409,
        code="identity-resolution-conflict",
        detail="Equity identifier does not resolve to one security in the requested time range",
    )
