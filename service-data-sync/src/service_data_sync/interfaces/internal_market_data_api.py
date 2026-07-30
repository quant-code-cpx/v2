"""0028 市场数据内部 POST catalog/query 路由及 HMAC 游标边界。

该模块统一将查询投影绑定到数据版本、过滤范围和分页位置，保证同一游标不能跨数据集、
时间窗或调用语义重用；HTTP 层只负责契约映射，不直接接触上游来源或持久化细节。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from service_data_sync.application.etf.query_contract import (
    ETF_V2_DATASETS,
    assert_etf_v2_query_contract,
)
from service_data_sync.application.ports.market_data_access import (
    MarketDataAccessRepository,
    MarketDataAccessUnavailable,
    MarketDataDatasetDescriptor,
    MarketDataDatasetNotFound,
    MarketDataFilter,
    MarketDataQuery,
    MarketDataQueryPage,
    MarketDataRequestValidationError,
    MarketDataSourceDescriptor,
)
from service_data_sync.interfaces.internal_sector_api import InternalProblem

_CONTRACT_VERSION = "1.0.0"
_MAX_REQUEST_BYTES = 65_536
_MAX_RESPONSE_BYTES = 2_097_152
_MAX_PAGE_SIZE = 500
_DEFAULT_PAGE_SIZE = 100


def register_market_data_routes(
    app: FastAPI,
    *,
    repository: MarketDataAccessRepository,
    require_service_bearer: Callable[..., None],
    cursor_secret: bytes,
) -> None:
    """注册仅 POST 的 catalog/query 路由，并保证游标绑定请求和 immutable data version。"""
    if len(cursor_secret) < 16:
        raise ValueError("market data cursor secret must contain at least 16 bytes")

    @app.post(
        "/internal/v1/market-data/datasets/search",
        dependencies=[Depends(require_service_bearer)],
    )
    async def search_datasets(request: Request, body: dict[str, object]) -> JSONResponse:
        """按代码、域、优先级、可用性和可见性能力发现 dataset，不泄漏物理表。"""
        _check_request_size(request)
        if set(body) - {
            "codes",
            "domains",
            "priorities",
            "availability",
            "supportsVisibilityMode",
            "page",
        }:
            raise _validation_problem("market-data catalog search contains an unsupported field")
        codes = (
            _string_tuple(body.get("codes"), maximum=50, label="codes") if "codes" in body else ()
        )
        if any(not _dataset_code_is_valid(code) for code in codes):
            raise _validation_problem("market-data catalog code is invalid")
        domains = _string_set(
            body.get("domains"),
            allowed={
                "INDEX",
                "ETF",
                "MARGIN",
                "STOCK_CONNECT",
                "BUSINESS_COMPOSITION",
                "CORPORATE_EVENT",
                "TRADING_EVENT",
                "DERIVATIVE",
                "UNKNOWN",
            },
        )
        priorities = _string_set(body.get("priorities"), allowed={"P0", "P1", "P2"})
        availability = _string_set(
            body.get("availability"),
            allowed={"AVAILABLE", "DEGRADED", "DISABLED", "UNKNOWN"},
        )
        visibility_mode = (
            None
            if "supportsVisibilityMode" not in body
            else _enum_or_problem(
                body.get("supportsVisibilityMode"),
                {"CURRENT", "PUBLIC_PIT", "OPERATIONAL_REPLAY"},
                "supportsVisibilityMode is invalid",
            )
        )
        limit = _page_limit(body.get("page"))
        descriptors = tuple(
            descriptor
            for descriptor in repository.search_datasets(
                priorities=priorities, availability=availability, query=None
            )
            if (not codes or descriptor.code in codes)
            and (not domains or descriptor.domain in domains)
            and (visibility_mode is None or visibility_mode in descriptor.visibility_modes)
        )
        request_id = _request_id(request)
        payload = {
            "requestId": request_id,
            "contractVersion": _CONTRACT_VERSION,
            "datasets": [_descriptor(item) for item in descriptors[:limit]],
            "page": {"limit": limit, "hasMore": len(descriptors) > limit, "nextCursor": None},
        }
        return _json_response(request_id=request_id, payload=payload)

    @app.post(
        "/internal/v1/market-data/query",
        dependencies=[Depends(require_service_bearer)],
    )
    async def query_market_data(request: Request, body: dict[str, object]) -> JSONResponse:
        """查询一个已发布 typed dataset，拒绝跨 data version、字段白名单或游标范围。"""
        _check_request_size(request)
        normalized = _query_or_problem(body)
        cursor = _optional_text(_mapping(body.get("page")).get("cursor"), maximum=2_048)
        cursor_payload = _decode_cursor_or_problem(
            cursor, request_fingerprint=normalized.request_fingerprint, secret=cursor_secret
        )
        after = None if cursor_payload is None else _required_cursor_text(cursor_payload, "after")
        try:
            page = repository.query(request=normalized, after=after)
        except MarketDataDatasetNotFound as error:
            raise InternalProblem(
                status=404,
                code="dataset-not-found",
                detail="Market data dataset or schema version is not registered",
            ) from error
        except MarketDataRequestValidationError as error:
            raise InternalProblem(
                status=422,
                code="dataset-contract-violation",
                detail="Market data request violates the dataset contract",
            ) from error
        except MarketDataAccessUnavailable as error:
            # 已注册 dataset 尚无 publication 是个人研究环境的正常首态；消费者必须可显示空页。
            request_id = _request_id(request)
            return _json_response(
                request_id=request_id,
                payload=_unavailable_query_payload(
                    request_id=request_id,
                    request=normalized,
                    availability=error.availability,
                    reason_code=error.reason_code,
                    observed_at=error.observed_at,
                    warnings=error.warnings,
                ),
            )
        if cursor_payload is not None and cursor_payload["dataVersion"] != str(page.data_version):
            raise InternalProblem(
                status=409,
                code="cursor-mismatch",
                detail="Cursor does not belong to the selected data version",
            )
        request_id = _request_id(request)
        payload = _query_payload(
            request_id=request_id,
            request=normalized,
            page=page,
            next_cursor=(
                None
                if page.next_position is None
                else _encode_cursor(
                    {
                        "request": normalized.request_fingerprint,
                        "dataVersion": str(page.data_version),
                        "after": page.next_position,
                    },
                    cursor_secret,
                )
            ),
        )
        response = _json_response(request_id=request_id, payload=payload)
        response.headers["X-Data-Version"] = str(page.data_version)
        return response


def _query_or_problem(body: dict[str, object]) -> MarketDataQuery:
    """把不受信任 JSON 规范化为单 dataset、受限字段、排序和分页请求。"""
    allowed = {
        "dataset",
        "businessScope",
        "identity",
        "time",
        "visibility",
        "selection",
        "filters",
        "fields",
        "sort",
        "page",
    }
    if set(body) - allowed:
        raise _validation_problem("market-data query contains an unsupported field")
    dataset = _mapping(body.get("dataset"))
    code = _optional_text(dataset.get("code"), maximum=120)
    schema_version = dataset.get("schemaVersion")
    if (
        code is None
        or not _dataset_code_is_valid(code)
        or not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version <= 0
        or set(dataset) != {"code", "schemaVersion"}
    ):
        raise _validation_problem("dataset code and schemaVersion are required")
    fields = _string_tuple(body.get("fields"), maximum=64, label="fields")
    if not fields:
        raise _validation_problem("at least one field is required")
    business_scope = _enum_or_problem(
        body.get("businessScope"),
        {
            "MARKET",
            "SECURITY",
            "INDEX",
            "ETF",
            "FUND",
            "CHANNEL",
            "REPORT",
            "EVENT",
            "CONTRACT",
        },
        "businessScope is invalid",
    )
    identity = _identity_or_problem(body.get("identity"))
    time = _time_or_problem(body.get("time"))
    visibility = _visibility_or_problem(body.get("visibility"))
    selection = _selection_or_problem(body.get("selection"))
    page = _mapping(body.get("page"))
    limit = _page_limit(page)
    filters = _filters_or_problem(body.get("filters"))
    sort = _sort_or_problem(body.get("sort"))
    if (
        code in ETF_V2_DATASETS
        and schema_version == 2
        and ("page" not in body or "limit" not in page)
    ):
        raise _validation_problem("ETF v2 page.limit is required")
    request_body = {
        "dataset": {"code": code, "schemaVersion": schema_version},
        "businessScope": business_scope,
        "identity": identity,
        "time": time,
        "visibility": visibility,
        "selection": selection,
        "fields": fields,
        "filters": [
            {"field": item.field, "operator": item.operator, "values": item.values}
            for item in filters
        ],
        "sort": sort,
        "page": {"limit": limit},
    }
    fingerprint = hashlib.sha256(
        json.dumps(request_body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    query = MarketDataQuery(
        dataset_code=code,
        schema_version=schema_version,
        business_scope=business_scope,
        identity=identity,
        time=time,
        visibility=visibility,
        selection=selection,
        fields=fields,
        filters=filters,
        sort=sort,
        limit=limit,
        request_fingerprint=fingerprint,
    )
    try:
        assert_etf_v2_query_contract(query)
    except MarketDataRequestValidationError as error:
        raise _validation_problem(str(error)) from error
    return query


def _query_payload(
    *,
    request_id: str,
    request: MarketDataQuery,
    page: MarketDataQueryPage,
    next_cursor: str | None,
) -> dict[str, object]:
    """投影不含数据库键、来源私有 URL 或 raw 定位的 v1 查询响应。"""
    return {
        "meta": {
            "requestId": request_id,
            "contractVersion": _CONTRACT_VERSION,
            "dataset": {"code": request.dataset_code, "schemaVersion": request.schema_version},
            "availability": "AVAILABLE",
            "release": {
                "dataVersion": str(page.data_version),
                "publishedAt": _timestamp(page.published_at),
                "knowledgeCutoff": _timestamp(page.knowledge_cutoff),
                "publicUsableAt": _timestamp(page.public_usable_at),
                "effectiveFrom": _timestamp(page.effective_from),
                "effectiveTo": _timestamp(page.effective_to),
                "methodology": dict(page.methodology),
                "sources": [_source(item) for item in page.sources],
                "quality": {"status": page.quality_status.upper(), "issueCodes": []},
                "completeness": page.completeness,
                "disclaimers": list(page.disclaimers),
            },
            "visibility": dict(request.visibility),
            "page": {
                "limit": request.limit,
                "hasMore": page.next_position is not None,
                "nextCursor": next_cursor,
            },
            "coverage": dict(page.coverage),
            "warnings": list(page.warnings),
            "disclaimers": list(page.disclaimers),
        },
        "records": list(page.items),
    }


def _unavailable_query_payload(
    *,
    request_id: str,
    request: MarketDataQuery,
    availability: str,
    reason_code: str,
    observed_at: datetime | None,
    warnings: tuple[str, ...],
) -> dict[str, object]:
    """构造无 publication、合法空集、来源不可用或暂不支持的成功空页。"""
    return {
        "meta": {
            "requestId": request_id,
            "contractVersion": _CONTRACT_VERSION,
            "dataset": {"code": request.dataset_code, "schemaVersion": request.schema_version},
            "availability": availability,
            "release": {
                "state": availability,
                "observedAt": _timestamp(observed_at),
                "reasonCode": reason_code,
            },
            "visibility": dict(request.visibility),
            "page": {"limit": request.limit, "hasMore": False, "nextCursor": None},
            "coverage": {
                "from": request.time.get("from"),
                "to": request.time.get("to"),
                "pitCoverage": "EMPTY" if availability == "EMPTY" else "UNKNOWN",
                "gaps": [],
            },
            "warnings": list(warnings),
            "disclaimers": [],
        },
        "records": [],
    }


def _dataset_code_is_valid(value: str) -> bool:
    """验证 dataset code 的冻结命名规则，阻止版本后缀、空段和任意 SQL 字符进入目录。"""
    import re

    return re.fullmatch(r"[a-z][a-z0-9_-]*(\.[a-z0-9][a-z0-9_-]*)+", value) is not None


def _enum_or_problem(value: object, allowed: set[str], detail: str) -> str:
    """读取一个封闭字符串枚举，拒绝空白、大小写漂移和未知值。"""
    if not isinstance(value, str) or value not in allowed:
        raise _validation_problem(detail)
    return value


def _identity_or_problem(value: object) -> dict[str, object] | None:
    """规范化可选身份选择器，保证最多 100 个对象且不把标识符字符串猜成 UUID。"""
    if value is None:
        return None
    identity = _mapping(value)
    if set(identity) - {"entityTypes", "entityRefs", "identifiers"} or not identity:
        raise _validation_problem("identity is invalid")
    normalized: dict[str, object] = {}
    if "entityTypes" in identity:
        normalized["entityTypes"] = _string_tuple(
            identity["entityTypes"], maximum=8, label="entityTypes"
        )
    if "entityRefs" in identity:
        references = _string_tuple(identity["entityRefs"], maximum=100, label="entityRefs")
        try:
            tuple(UUID(reference) for reference in references)
        except ValueError as error:
            raise _validation_problem("identity entityRefs must be UUID values") from error
        normalized["entityRefs"] = references
    if "identifiers" in identity:
        values = identity["identifiers"]
        if not isinstance(values, list) or not 1 <= len(values) <= 100:
            raise _validation_problem("identity identifiers are invalid")
        identifiers: list[dict[str, object]] = []
        for raw in values:
            item = _mapping(raw)
            if set(item) - {"scheme", "value", "venue", "authority"} or {
                "scheme",
                "value",
            } - set(item):
                raise _validation_problem("identity identifier is invalid")
            scheme = _enum_or_problem(
                item.get("scheme"),
                {
                    "exchange_symbol",
                    "administrator_code",
                    "fund_code",
                    "venue_contract_code",
                    "isin",
                },
                "identity identifier scheme is invalid",
            )
            identifier_value = _optional_text(item.get("value"), maximum=64)
            venue = _nullable_text_or_problem(
                item.get("venue"), maximum=24, detail="identifier venue is invalid"
            )
            authority = _nullable_text_or_problem(
                item.get("authority"), maximum=64, detail="identifier authority is invalid"
            )
            if identifier_value is None:
                raise _validation_problem("identity identifier value is invalid")
            identifiers.append(
                {
                    "scheme": scheme,
                    "value": identifier_value,
                    "venue": venue,
                    "authority": authority,
                }
            )
        normalized["identifiers"] = identifiers
    return normalized


def _time_or_problem(value: object) -> dict[str, object]:
    """校验事实时间维度和有界范围，不接受无时区的 datetime 或倒置窗口。"""
    raw = _mapping(value)
    if set(raw) - {"dimension", "from", "to", "timezone"} or {"dimension", "from", "to"} - set(raw):
        raise _validation_problem("time is invalid")
    dimension = _enum_or_problem(
        raw.get("dimension"),
        {
            "TRADE_DATE",
            "REPORT_PERIOD_END",
            "POSITION_DATE",
            "EVENT_DATE",
            "EFFECTIVE_AT",
            "VISIBLE_AT",
            "OBSERVED_AT",
            "EXPIRY_DATE",
        },
        "time dimension is invalid",
    )
    start = _temporal_or_problem(raw.get("from"), detail="time from is invalid")
    end = _temporal_or_problem(raw.get("to"), detail="time to is invalid")
    if start > end:
        raise _validation_problem("time from must not be after to")
    timezone = raw.get("timezone", "Asia/Shanghai")
    if timezone != "Asia/Shanghai":
        raise _validation_problem("time timezone is invalid")
    return {"dimension": dimension, "from": start, "to": end, "timezone": timezone}


def _visibility_or_problem(value: object) -> dict[str, object]:
    """校验当前、公开 PIT 与运维 replay 的互斥时间要求。"""
    raw = _mapping(value)
    if set(raw) - {"mode", "asOf", "knownAt"} or "mode" not in raw:
        raise _validation_problem("visibility is invalid")
    mode = _enum_or_problem(
        raw.get("mode"),
        {"CURRENT", "PUBLIC_PIT", "OPERATIONAL_REPLAY"},
        "visibility mode is invalid",
    )
    as_of = raw.get("asOf")
    known_at = raw.get("knownAt")
    if mode == "CURRENT":
        if as_of is not None or known_at is not None:
            raise _validation_problem("CURRENT visibility cannot include PIT times")
        return {"mode": mode}
    normalized_as_of = _utc_timestamp_or_problem(as_of, detail="visibility asOf is invalid")
    normalized_known_at = _utc_timestamp_or_problem(
        known_at, detail="visibility knownAt is invalid"
    )
    return {"mode": mode, "asOf": normalized_as_of, "knownAt": normalized_known_at}


def _selection_or_problem(value: object) -> dict[str, object]:
    """校验 publication、方法学和质量选择，避免未声明质量门禁的读取。"""
    raw = _mapping(value)
    if (
        set(raw) - {"dataVersion", "knownDataVersion", "methodology", "qualityStatuses"}
        or "qualityStatuses" not in raw
    ):
        raise _validation_problem("selection is invalid")
    statuses = _string_tuple(raw.get("qualityStatuses"), maximum=2, label="qualityStatuses")
    if not statuses or not set(statuses) <= {"PASSED", "WARNED"}:
        raise _validation_problem("selection qualityStatuses is invalid")
    normalized: dict[str, object] = {"qualityStatuses": statuses}
    for field in ("dataVersion", "knownDataVersion"):
        if field in raw:
            value = _optional_text(raw[field], maximum=36)
            try:
                UUID(value or "")
            except ValueError as error:
                raise _validation_problem(f"selection {field} is invalid") from error
            normalized[field] = value
    if "methodology" in raw:
        methodology = _mapping(raw["methodology"])
        if set(methodology) - {"code", "version", "kind"} or {"code", "version"} - set(methodology):
            raise _validation_problem("selection methodology is invalid")
        code = _optional_text(methodology.get("code"), maximum=100)
        version = _optional_text(methodology.get("version"), maximum=40)
        kind = methodology.get("kind", "UNKNOWN")
        if code is None or version is None or kind not in {"REPORTED", "DERIVED", "UNKNOWN"}:
            raise _validation_problem("selection methodology is invalid")
        normalized["methodology"] = {"code": code, "version": version, "kind": kind}
    return normalized


def _filters_or_problem(value: object) -> tuple[MarketDataFilter, ...]:
    """读取最多二十个有限 scalar 条件；字段白名单由具体 typed reader 再次裁决。"""
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > 20:
        raise _validation_problem("filters are invalid")
    normalized: list[MarketDataFilter] = []
    for raw in value:
        item = _mapping(raw)
        if set(item) != {"field", "operator", "values"}:
            raise _validation_problem("filter is invalid")
        field = _optional_text(item.get("field"), maximum=64)
        operator = _enum_or_problem(
            item.get("operator"),
            {"EQ", "IN", "GTE", "LTE", "RANGE", "PREFIX", "CONTAINS"},
            "filter operator is invalid",
        )
        values = item.get("values")
        if not isinstance(values, list) or not 1 <= len(values) <= 500:
            raise _validation_problem("filter values are invalid")
        scalar_values: list[str | int | bool] = []
        for scalar in values:
            if isinstance(scalar, bool):
                scalar_values.append(scalar)
            elif isinstance(scalar, str) and len(scalar) <= 120:
                scalar_values.append(scalar)
            elif isinstance(scalar, int) and not isinstance(scalar, bool):
                scalar_values.append(scalar)
            else:
                raise _validation_problem("filter value must be a bounded scalar")
        if field is None or not field.isascii() or not field[0].isalpha() or not field.isalnum():
            raise _validation_problem("filter field is invalid")
        if operator == "RANGE" and len(scalar_values) != 2:
            raise _validation_problem("RANGE filter requires exactly two values")
        normalized.append(
            MarketDataFilter(field=field, operator=operator, values=tuple(scalar_values))
        )
    if len({item.field for item in normalized}) != len(normalized):
        raise _validation_problem("filter fields must be unique")
    return tuple(normalized)


def _temporal_or_problem(value: object, *, detail: str) -> str:
    """接受 ISO date 或带明确偏移的 RFC3339 instant，并保留原字符串用于签名。"""
    if not isinstance(value, str):
        raise _validation_problem(detail)
    from datetime import date

    try:
        if "T" not in value:
            date.fromisoformat(value)
        elif datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo is None:
            raise ValueError
    except ValueError as error:
        raise _validation_problem(detail) from error
    return value


def _utc_timestamp_or_problem(value: object, *, detail: str) -> str:
    """读取必须以 Z 结尾的 UTC 时间，PIT 边界绝不依赖服务器本地时区。"""
    if not isinstance(value, str) or not value.endswith("Z"):
        raise _validation_problem(detail)
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise _validation_problem(detail) from error
    return value


def _nullable_text_or_problem(value: object, *, maximum: int, detail: str) -> str | None:
    """读取可选文本字段，`null` 合法但数字、空串和超长值一律拒绝。"""
    if value is None:
        return None
    normalized = _optional_text(value, maximum=maximum)
    if normalized is None:
        raise _validation_problem(detail)
    return normalized


def _descriptor(value: MarketDataDatasetDescriptor) -> dict[str, object]:
    """将端口数据集描述投影为契约白名单，省略所有内部持久化实现信息。"""
    return {
        "dataset": {"code": value.code, "schemaVersion": value.schema_version},
        "title": value.title,
        "domain": value.domain,
        "priority": value.priority,
        "availability": value.availability,
        "availabilityReason": value.availability_reason,
        "allowedTimeDimensions": list(value.allowed_time_dimensions),
        "visibilityModes": list(value.visibility_modes),
        "maxRangeDays": value.max_range_days,
        "maxIdentifiers": value.max_identifiers,
        "fields": [
            {
                "name": field.name,
                "logicalType": field.logical_type,
                "nullable": field.nullable,
                "selectable": field.selectable,
                "unit": field.unit,
                "filterOperators": list(field.filter_operators),
                "sortable": field.sortable,
            }
            for field in value.fields
        ],
        "filters": [
            {"field": item.field, "operators": list(item.operators), "maxValues": item.max_values}
            for item in value.filters
        ],
        "sortFields": list(value.allowed_sort_fields),
        "sources": [_source(item) for item in value.sources],
        "methodologies": [dict(item) for item in value.methodologies],
    }


def _source(value: object) -> dict[str, object]:
    """投影允许暴露的来源描述，Adapter 名、凭据和原始对象路径永不越过内部边界。"""
    if not isinstance(value, MarketDataSourceDescriptor):
        raise TypeError("market-data source descriptor is invalid")
    return {
        "sourceRef": value.source_ref,
        "publisher": value.publisher,
        "sourceDataset": value.source_dataset,
        "authoritative": value.authoritative,
        "redistribution": value.redistribution,
        "coverageNote": value.coverage_note,
    }


def _check_request_size(request: Request) -> None:
    """在解析大对象前按 Content-Length 拒绝超过 v1 有界请求的调用。"""
    value = request.headers.get("content-length")
    if value is None:
        return
    try:
        byte_size = int(value)
    except ValueError as error:
        raise _validation_problem("Content-Length is invalid") from error
    if byte_size < 0 or byte_size > _MAX_REQUEST_BYTES:
        raise InternalProblem(
            status=413,
            code="payload-too-large",
            detail="Request body exceeds the market-data limit",
        )


def _json_response(*, request_id: str, payload: dict[str, object]) -> JSONResponse:
    """序列化后执行响应大小门禁，避免服务端分页被无界字段投影绕过。"""
    compatible_payload = _json_compatible(payload)
    encoded = json.dumps(compatible_payload, ensure_ascii=False, separators=(",", ":")).encode()
    if len(encoded) > _MAX_RESPONSE_BYTES:
        raise InternalProblem(
            status=413,
            code="response-too-large",
            detail="Response exceeds the market-data limit",
        )
    return JSONResponse(
        content=compatible_payload,
        headers={"X-Request-Id": request_id, "Cache-Control": "no-store"},
    )


def _json_compatible(value: object) -> object:
    """将端口返回的十进制、UUID 与时区时间显式投影为合同允许的 JSON 标量。"""
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return _timestamp(value)
    if isinstance(value, dict):
        return {str(key): _json_compatible(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_compatible(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError("market-data typed reader returned a non-JSON value")


def _string_set(value: object, *, allowed: set[str]) -> frozenset[str]:
    """读取可选字符串集合；空集合表示不过滤而不是拒绝全部。"""
    if value is None:
        return frozenset()
    items = _string_tuple(value, maximum=len(allowed), label="filter")
    if not set(items) <= allowed:
        raise _validation_problem("market-data filter contains an unsupported value")
    return frozenset(items)


def _string_tuple(value: object, *, maximum: int, label: str) -> tuple[str, ...]:
    """验证有上限且不含空白项的字符串数组，保持请求 fingerprint 确定性。"""
    if not isinstance(value, list) or len(value) > maximum:
        raise _validation_problem(f"{label} must be an array within the allowed limit")
    items = tuple(_optional_text(item, maximum=120) for item in value)
    if any(item is None for item in items) or len(set(items)) != len(items):
        raise _validation_problem(f"{label} must contain unique non-empty strings")
    return tuple(item for item in items if item is not None)


def _sort_or_problem(value: object) -> tuple[tuple[str, str], ...]:
    """读取最多三个明确升降序排序键，拒绝隐式 SQL 表达式或未知方向。"""
    if not isinstance(value, list) or not 1 <= len(value) <= 3:
        raise _validation_problem("sort must contain one to three entries")
    normalized: list[tuple[str, str]] = []
    for entry in value:
        item = _mapping(entry)
        field = _optional_text(item.get("field"), maximum=120)
        direction = item.get("direction")
        if field is None or not isinstance(direction, str) or direction not in {"ASC", "DESC"}:
            raise _validation_problem("sort entries are invalid")
        normalized.append((field, direction))
    if len({field for field, _ in normalized}) != len(normalized):
        raise _validation_problem("sort fields must be unique")
    return tuple(normalized)


def _page_limit(value: object) -> int:
    """读取 v1 固定范围内页大小，空 page 使用默认值。"""
    page = _mapping(value)
    limit = page.get("limit", _DEFAULT_PAGE_SIZE)
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= _MAX_PAGE_SIZE:
        raise _validation_problem("page limit is invalid")
    return limit


def _mapping(value: object) -> dict[str, object]:
    """只接受 JSON object，避免 list 或标量绕过字段白名单验证。"""
    if value is None:
        return {}
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise _validation_problem("request object is invalid")
    return value


def _optional_text(value: object, *, maximum: int) -> str | None:
    """将 JSON 标量转换为受长度限制的非空文本，不做隐式类型猜测。"""
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        return None
    return normalized


def _encode_cursor(payload: dict[str, str], secret: bytes) -> str:
    """以独立 HMAC 签名请求、版本和继续位置，游标不承载认证凭据。"""
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    signature = hmac.new(secret, encoded, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(encoded + signature).decode().rstrip("=")


def _decode_cursor_or_problem(
    cursor: str | None, *, request_fingerprint: str, secret: bytes
) -> dict[str, str] | None:
    """校验游标完整性及请求绑定，任一漂移统一拒绝以防跨查询混页。"""
    if cursor is None:
        return None
    try:
        decoded = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        payload, signature = decoded[:-32], decoded[-32:]
        expected = hmac.new(secret, payload, hashlib.sha256).digest()
        value = json.loads(payload)
    except (ValueError, binascii.Error, json.JSONDecodeError) as error:
        raise _cursor_problem() from error
    if not hmac.compare_digest(signature, expected) or not isinstance(value, dict):
        raise _cursor_problem()
    normalized = {
        key: item for key, item in value.items() if isinstance(key, str) and isinstance(item, str)
    }
    if len(normalized) != len(value) or normalized.get("request") != request_fingerprint:
        raise _cursor_problem()
    if "dataVersion" not in normalized or "after" not in normalized:
        raise _cursor_problem()
    return normalized


def _required_cursor_text(payload: dict[str, str], key: str) -> str:
    """读取已验签游标的非空续页位置，阻止空值被解释为首页。"""
    value = payload.get(key)
    if value is None or not value:
        raise _cursor_problem()
    return value


def _request_id(request: Request) -> str:
    """优先复用调用方合法 request ID，缺失或非法时生成新的 UUID。"""
    value = request.headers.get("X-Request-Id")
    if value is not None and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}", value):
        return value
    from uuid import uuid4

    return str(uuid4())


def _timestamp(value: datetime | None) -> str | None:
    """将时区时间投影为 RFC 3339，未知时间保持空值。"""
    return None if value is None else value.isoformat().replace("+00:00", "Z")


def _validation_problem(detail: str) -> InternalProblem:
    """把输入结构错误映射为不会泄漏验证实现的稳定问题响应。"""
    return InternalProblem(status=400, code="validation-error", detail=detail)


def _cursor_problem() -> InternalProblem:
    """统一游标编码、签名、请求范围和版本不匹配失败。"""
    return InternalProblem(
        status=409,
        code="cursor-mismatch",
        detail="Cursor does not match the market-data request",
    )
