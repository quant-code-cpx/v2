"""供受信任服务读取已发布证券主数据与上市状态历史的内部 HTTP 路由。

所有查询都绑定质量通过的交易所或全市场 publication，并以稳定证券身份而非可复用代码
组织结果；路由不暴露数据库主键、供应商原始字段或尚未发布的目录与生命周期候选记录。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, Header, Path, Query, Request
from fastapi.responses import JSONResponse, Response

from service_data_sync.application.ports.equity_master_read import (
    EquityMasterPublication,
    EquityMasterReadRepository,
    EquityMasterReadUnavailable,
    StoredEquityInstrument,
    StoredListingStatusPeriod,
)
from service_data_sync.domain.equity import EquityIdentifier, Exchange
from service_data_sync.interfaces.internal_sector_api import InternalProblem

_PRIVATE_REVALIDATE = "private, max-age=0, must-revalidate"
_LISTING_STATUSES = frozenset({"LISTED", "SUSPENDED", "DELISTED"})


def register_equity_routes(
    app: FastAPI,
    *,
    repository: EquityMasterReadRepository,
    require_service_bearer: Callable[..., None],
    cursor_secret: bytes,
) -> None:
    """把 0009 三条证券主数据路由挂载到共享内部应用。"""

    @app.get("/internal/v1/equities", dependencies=[Depends(require_service_bearer)])
    def list_equities(
        request: Request,
        exchange: Annotated[str | None, Query()] = None,
        status: Annotated[list[str] | None, Query()] = None,
        query: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
        as_of: Annotated[date | None, Query(alias="asOf")] = None,
        known_at: Annotated[datetime | None, Query(alias="knownAt")] = None,
        cursor: Annotated[str | None, Query(min_length=1, max_length=1024)] = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 100,
        if_none_match: Annotated[str | None, Header(max_length=256)] = None,
    ) -> Response:
        """分页返回单所或稳定三所聚合中的已确认证券双时间切片。"""
        selected_exchange = _exchange_or_problem(exchange) if exchange is not None else None
        selected_statuses = _statuses_or_problem(status)
        normalized_query = _query_or_problem(query)
        publication = _publication_or_problem(repository, selected_exchange)
        effective_as_of, knowledge_cutoff = _selection_or_problem(
            publication, as_of=as_of, known_at=known_at
        )
        decoded_cursor = _cursor_or_problem(
            cursor,
            secret=cursor_secret,
            kind="equity-list",
        )
        after_exchange, after_symbol, after_instrument_id = _list_position_or_problem(
            decoded_cursor,
            data_version=publication.data_version,
            exchange=selected_exchange,
            statuses=selected_statuses,
            query=normalized_query,
            selector_as_of=as_of,
            selector_known_at=known_at,
            effective_as_of=effective_as_of,
            knowledge_cutoff=knowledge_cutoff,
        )
        try:
            rows = repository.list_instruments(
                data_version=publication.data_version,
                exchange=selected_exchange,
                statuses=selected_statuses,
                query=normalized_query,
                as_of=effective_as_of,
                known_at=knowledge_cutoff,
                after_exchange=after_exchange,
                after_symbol=after_symbol,
                after_instrument_id=after_instrument_id,
                limit=limit + 1,
            )
        except EquityMasterReadUnavailable as error:
            raise _unavailable_problem() from error
        page_rows = tuple(rows[:limit])
        next_cursor = (
            _encode_cursor(
                {
                    "k": "equity-list",
                    "v": str(publication.data_version),
                    "x": None if selected_exchange is None else selected_exchange.value,
                    "s": list(selected_statuses),
                    "q": normalized_query,
                    "a": effective_as_of.isoformat(),
                    "n": _timestamp(knowledge_cutoff),
                    "ad": as_of is None,
                    "nd": known_at is None,
                    "e": page_rows[-1].identifier.exchange.value,
                    "c": page_rows[-1].identifier.symbol,
                    "i": str(page_rows[-1].instrument_id),
                },
                secret=cursor_secret,
            )
            if len(rows) > limit and page_rows
            else None
        )
        body = {
            "items": [_instrument_resource(row) for row in page_rows],
            "nextCursor": next_cursor,
            "dataVersion": str(publication.data_version),
            "publishedAt": _timestamp(publication.published_at),
            "effectiveAsOf": effective_as_of.isoformat(),
            "knowledgeCutoff": _timestamp(knowledge_cutoff),
            "publicationScope": publication.publication_scope,
        }
        return _conditional_response(
            request=request,
            if_none_match=if_none_match,
            publication=publication,
            etag=_representation_etag(
                "list",
                publication,
                {
                    "exchange": None if selected_exchange is None else selected_exchange.value,
                    "statuses": selected_statuses,
                    "query": normalized_query,
                    "asOf": effective_as_of.isoformat(),
                    "knownAt": _timestamp(knowledge_cutoff),
                    "cursor": cursor,
                    "limit": limit,
                },
            ),
            body=body,
        )

    @app.get(
        "/internal/v1/equities/{exchange}/{symbol}",
        dependencies=[Depends(require_service_bearer)],
    )
    def get_equity(
        request: Request,
        exchange: str,
        symbol: Annotated[str, Path(pattern=r"^[0-9]{6}$")],
        as_of: Annotated[date | None, Query(alias="asOf")] = None,
        known_at: Annotated[datetime | None, Query(alias="knownAt")] = None,
        if_none_match: Annotated[str | None, Header(max_length=256)] = None,
    ) -> Response:
        """按当前开放或显式历史日期解析唯一证券并返回双时间详情。"""
        identifier = _identifier_or_problem(exchange, symbol)
        publication = _publication_or_problem(repository, identifier.exchange)
        projection_as_of, knowledge_cutoff = _selection_or_problem(
            publication, as_of=as_of, known_at=known_at
        )
        instrument = _instrument_or_problem(
            repository,
            data_version=publication.data_version,
            identifier=identifier,
            identifier_as_of=as_of,
            projection_as_of=projection_as_of,
            known_at=knowledge_cutoff,
        )
        body = {
            **_instrument_resource(instrument),
            "dataVersion": str(publication.data_version),
            "publishedAt": _timestamp(publication.published_at),
            "effectiveAsOf": projection_as_of.isoformat(),
            "knowledgeCutoff": _timestamp(knowledge_cutoff),
        }
        return _conditional_response(
            request=request,
            if_none_match=if_none_match,
            publication=publication,
            etag=_representation_etag(
                "detail",
                publication,
                {
                    "exchange": identifier.exchange.value,
                    "symbol": identifier.symbol,
                    "asOf": None if as_of is None else as_of.isoformat(),
                    "knownAt": _timestamp(knowledge_cutoff),
                },
            ),
            body=body,
        )

    @app.get(
        "/internal/v1/equities/{exchange}/{symbol}/listing-status-history",
        dependencies=[Depends(require_service_bearer)],
    )
    def list_listing_status_history(
        request: Request,
        exchange: str,
        symbol: Annotated[str, Path(pattern=r"^[0-9]{6}$")],
        as_of: Annotated[date | None, Query(alias="asOf")] = None,
        effective_from: Annotated[date | None, Query(alias="effectiveFrom")] = None,
        effective_to: Annotated[date | None, Query(alias="effectiveTo")] = None,
        known_at: Annotated[datetime | None, Query(alias="knownAt")] = None,
        cursor: Annotated[str | None, Query(min_length=1, max_length=1024)] = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 100,
        if_none_match: Annotated[str | None, Header(max_length=256)] = None,
    ) -> Response:
        """解析路径所属证券，再分页返回知识截止时间前的生命周期修订。"""
        if effective_from is not None and effective_to is not None:
            if effective_from >= effective_to:
                raise InternalProblem(
                    status=400,
                    code="validation-error",
                    detail="effectiveFrom must be before effectiveTo",
                )
        identifier = _identifier_or_problem(exchange, symbol)
        publication = _publication_or_problem(repository, identifier.exchange)
        projection_as_of, knowledge_cutoff = _selection_or_problem(
            publication, as_of=as_of, known_at=known_at
        )
        decoded_cursor = _cursor_or_problem(
            cursor,
            secret=cursor_secret,
            kind="listing-history",
        )
        instrument = _instrument_or_problem(
            repository,
            data_version=publication.data_version,
            identifier=identifier,
            identifier_as_of=as_of,
            projection_as_of=projection_as_of,
            known_at=knowledge_cutoff,
        )
        after_effective_from, after_known_from, after_version_id = _history_position_or_problem(
            decoded_cursor,
            data_version=publication.data_version,
            identifier=identifier,
            instrument_id=instrument.instrument_id,
            selector_as_of=as_of,
            selector_known_at=known_at,
            effective_as_of=projection_as_of,
            knowledge_cutoff=knowledge_cutoff,
            effective_from=effective_from,
            effective_to=effective_to,
        )
        try:
            rows = repository.list_listing_status_history(
                data_version=publication.data_version,
                exchange=identifier.exchange,
                security_id=instrument.security_id,
                known_at=knowledge_cutoff,
                effective_from=effective_from,
                effective_to=effective_to,
                after_effective_from=after_effective_from,
                after_known_from=after_known_from,
                after_version_id=after_version_id,
                limit=limit + 1,
            )
        except EquityMasterReadUnavailable as error:
            raise _unavailable_problem() from error
        page_rows = tuple(rows[:limit])
        next_cursor = (
            _encode_cursor(
                {
                    "k": "listing-history",
                    "v": str(publication.data_version),
                    "x": identifier.exchange.value,
                    "c": identifier.symbol,
                    "i": str(instrument.instrument_id),
                    "a": projection_as_of.isoformat(),
                    "f": None if effective_from is None else effective_from.isoformat(),
                    "t": None if effective_to is None else effective_to.isoformat(),
                    "n": _timestamp(knowledge_cutoff),
                    "ad": as_of is None,
                    "nd": known_at is None,
                    "d": page_rows[-1].effective_from.isoformat(),
                    "r": _timestamp(page_rows[-1].known_from),
                    "z": str(page_rows[-1].version_id),
                },
                secret=cursor_secret,
            )
            if len(rows) > limit and page_rows
            else None
        )
        body = {
            "instrumentId": str(instrument.instrument_id),
            "exchange": identifier.exchange.value,
            "symbol": identifier.symbol,
            "items": [_listing_period_resource(row) for row in page_rows],
            "nextCursor": next_cursor,
            "dataVersion": str(publication.data_version),
            "publishedAt": _timestamp(publication.published_at),
            "knowledgeCutoff": _timestamp(knowledge_cutoff),
        }
        return _conditional_response(
            request=request,
            if_none_match=if_none_match,
            publication=publication,
            etag=_representation_etag(
                "listing-history",
                publication,
                {
                    "exchange": identifier.exchange.value,
                    "symbol": identifier.symbol,
                    "instrumentId": str(instrument.instrument_id),
                    "asOf": None if as_of is None else as_of.isoformat(),
                    "effectiveFrom": None if effective_from is None else effective_from.isoformat(),
                    "effectiveTo": None if effective_to is None else effective_to.isoformat(),
                    "knownAt": _timestamp(knowledge_cutoff),
                    "cursor": cursor,
                    "limit": limit,
                },
            ),
            body=body,
        )


def _publication_or_problem(
    repository: EquityMasterReadRepository,
    exchange: Exchange | None,
) -> EquityMasterPublication:
    """要求选定单所或聚合已有通过质量门的当前发布。"""
    try:
        publication = repository.get_current_publication(exchange=exchange)
    except EquityMasterReadUnavailable as error:
        raise _unavailable_problem() from error
    if publication is None:
        raise _unavailable_problem()
    return publication


def _selection_or_problem(
    publication: EquityMasterPublication,
    *,
    as_of: date | None,
    known_at: datetime | None,
) -> tuple[date, datetime]:
    """冻结请求的市场日期与知识截止时间，禁止越过发布版本。"""
    effective_as_of = publication.effective_as_of if as_of is None else as_of
    if effective_as_of > publication.effective_as_of:
        raise InternalProblem(
            status=400,
            code="validation-error",
            detail="asOf exceeds the selected publication",
        )
    knowledge_cutoff = publication.knowledge_cutoff if known_at is None else known_at
    if knowledge_cutoff.tzinfo is None:
        raise InternalProblem(
            status=400,
            code="validation-error",
            detail="knownAt must include a timezone",
        )
    normalized_knowledge = knowledge_cutoff.astimezone(UTC)
    if normalized_knowledge > publication.knowledge_cutoff.astimezone(UTC):
        raise InternalProblem(
            status=400,
            code="validation-error",
            detail="knownAt exceeds the selected publication",
        )
    if normalized_knowledge > datetime.now(UTC):
        raise InternalProblem(
            status=400,
            code="validation-error",
            detail="knownAt must not be in the future",
        )
    return effective_as_of, normalized_knowledge


def _instrument_or_problem(
    repository: EquityMasterReadRepository,
    *,
    data_version: UUID,
    identifier: EquityIdentifier,
    identifier_as_of: date | None,
    projection_as_of: date,
    known_at: datetime,
) -> StoredEquityInstrument:
    """解析唯一已确认身份；空值返回 404，多值返回 409。"""
    try:
        rows = repository.find_instruments(
            data_version=data_version,
            exchange=identifier.exchange,
            symbol=identifier.symbol,
            identifier_as_of=identifier_as_of,
            projection_as_of=projection_as_of,
            known_at=known_at,
            limit=2,
        )
    except EquityMasterReadUnavailable as error:
        raise _unavailable_problem() from error
    if not rows:
        raise InternalProblem(
            status=404,
            code="not-found",
            detail="Equity instrument is not found",
        )
    if len(rows) > 1:
        raise InternalProblem(
            status=409,
            code="identity-resolution-conflict",
            detail="Equity instrument identity is ambiguous",
        )
    return rows[0]


def _exchange_or_problem(value: str) -> Exchange:
    """解析合同封闭交易所枚举，不做大小写猜测。"""
    try:
        return Exchange(value)
    except ValueError as error:
        raise InternalProblem(
            status=400,
            code="validation-error",
            detail="exchange is invalid",
        ) from error


def _identifier_or_problem(exchange: str, symbol: str) -> EquityIdentifier:
    """构造交易所限定六位代码，并统一映射格式失败。"""
    try:
        return EquityIdentifier(exchange=_exchange_or_problem(exchange), symbol=symbol)
    except ValueError as error:
        raise InternalProblem(
            status=400,
            code="validation-error",
            detail="symbol is invalid",
        ) from error


def _statuses_or_problem(values: Sequence[str] | None) -> tuple[str, ...]:
    """校验、去重并规范化最多三个上市生命周期筛选值。"""
    if values is None:
        return ()
    if not 1 <= len(values) <= 3 or len(set(values)) != len(values):
        raise InternalProblem(
            status=400,
            code="validation-error",
            detail="status must contain up to three unique values",
        )
    if any(value not in _LISTING_STATUSES for value in values):
        raise InternalProblem(
            status=400,
            code="validation-error",
            detail="status is invalid",
        )
    return tuple(sorted(values))


def _query_or_problem(value: str | None) -> str | None:
    """规范前缀查询并拒绝只含空白的放大请求。"""
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        raise InternalProblem(
            status=400,
            code="validation-error",
            detail="query is invalid",
        )
    return normalized


def _cursor_or_problem(
    value: str | None,
    *,
    secret: bytes,
    kind: str,
) -> Mapping[str, Any] | None:
    """验签游标并校验路由类型，筛选与版本语义由具体分页器处理。"""
    if value is None:
        return None
    decoded = _decode_cursor(value, secret=secret)
    if decoded.get("k") != kind:
        raise _invalid_cursor_problem()
    return decoded


def _list_position_or_problem(
    cursor: Mapping[str, Any] | None,
    *,
    data_version: UUID,
    exchange: Exchange | None,
    statuses: tuple[str, ...],
    query: str | None,
    selector_as_of: date | None,
    selector_known_at: datetime | None,
    effective_as_of: date,
    knowledge_cutoff: datetime,
) -> tuple[Exchange | None, str | None, UUID | None]:
    """先验证目录游标筛选投影，再区分过期版本并解析稳定位置。"""
    if cursor is None:
        return None, None, None
    expected = {
        "x": None if exchange is None else exchange.value,
        "s": list(statuses),
        "q": query,
        "ad": selector_as_of is None,
        "nd": selector_known_at is None,
    }
    if any(cursor.get(key) != value for key, value in expected.items()):
        raise _invalid_cursor_problem()
    if selector_as_of is not None and cursor.get("a") != selector_as_of.isoformat():
        raise _invalid_cursor_problem()
    if selector_known_at is not None and cursor.get("n") != _timestamp(selector_known_at):
        raise _invalid_cursor_problem()
    _require_current_cursor_version(cursor, data_version=data_version)
    if cursor.get("a") != effective_as_of.isoformat() or cursor.get("n") != _timestamp(
        knowledge_cutoff
    ):
        raise _invalid_cursor_problem()
    cursor_exchange = cursor.get("e")
    symbol = cursor.get("c")
    instrument_id = cursor.get("i")
    if (
        not isinstance(cursor_exchange, str)
        or not isinstance(symbol, str)
        or not isinstance(instrument_id, str)
    ):
        raise _invalid_cursor_problem()
    try:
        return Exchange(cursor_exchange), symbol, UUID(instrument_id)
    except ValueError as error:
        raise _invalid_cursor_problem() from error


def _history_position_or_problem(
    cursor: Mapping[str, Any] | None,
    *,
    data_version: UUID,
    identifier: EquityIdentifier,
    instrument_id: UUID,
    selector_as_of: date | None,
    selector_known_at: datetime | None,
    effective_as_of: date,
    knowledge_cutoff: datetime,
    effective_from: date | None,
    effective_to: date | None,
) -> tuple[date | None, datetime | None, UUID | None]:
    """先验证历史游标路径与筛选，再区分过期版本并解析稳定位置。"""
    if cursor is None:
        return None, None, None
    expected = {
        "x": identifier.exchange.value,
        "c": identifier.symbol,
        "i": str(instrument_id),
        "f": None if effective_from is None else effective_from.isoformat(),
        "t": None if effective_to is None else effective_to.isoformat(),
        "ad": selector_as_of is None,
        "nd": selector_known_at is None,
    }
    if any(cursor.get(key) != value for key, value in expected.items()):
        raise _invalid_cursor_problem()
    if selector_as_of is not None and cursor.get("a") != selector_as_of.isoformat():
        raise _invalid_cursor_problem()
    if selector_known_at is not None and cursor.get("n") != _timestamp(selector_known_at):
        raise _invalid_cursor_problem()
    _require_current_cursor_version(cursor, data_version=data_version)
    if cursor.get("a") != effective_as_of.isoformat() or cursor.get("n") != _timestamp(
        knowledge_cutoff
    ):
        raise _invalid_cursor_problem()
    cursor_effective = cursor.get("d")
    cursor_known = cursor.get("r")
    cursor_version = cursor.get("z")
    if (
        not isinstance(cursor_effective, str)
        or not isinstance(cursor_known, str)
        or not isinstance(cursor_version, str)
    ):
        raise _invalid_cursor_problem()
    try:
        parsed_known = datetime.fromisoformat(cursor_known.replace("Z", "+00:00"))
        if parsed_known.tzinfo is None:
            raise ValueError("cursor known time has no timezone")
        return (
            date.fromisoformat(cursor_effective),
            parsed_known.astimezone(UTC),
            UUID(cursor_version),
        )
    except ValueError as error:
        raise _invalid_cursor_problem() from error


def _require_current_cursor_version(cursor: Mapping[str, Any], *, data_version: UUID) -> None:
    """仅在路由和筛选均匹配后把旧发布游标映射为可重试的 409。"""
    cursor_version = cursor.get("v")
    if not isinstance(cursor_version, str):
        raise _invalid_cursor_problem()
    try:
        parsed_version = UUID(cursor_version)
    except ValueError as error:
        raise _invalid_cursor_problem() from error
    if parsed_version != data_version:
        raise InternalProblem(
            status=409,
            code="snapshot-expired",
            detail="Published snapshot changed",
        )


def _encode_cursor(value: Mapping[str, Any], *, secret: bytes) -> str:
    """以稳定 JSON 与 HMAC-SHA256 生成不透明、防篡改游标。"""
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    signature = hmac.digest(secret, payload, "sha256")
    return f"{_base64url(payload)}.{_base64url(signature)}"


def _decode_cursor(value: str, *, secret: bytes) -> Mapping[str, Any]:
    """解码并常量时间验签游标，拒绝畸形或被篡改内容。"""
    payload_part, separator, signature_part = value.partition(".")
    if separator != "." or not payload_part or not signature_part:
        raise _invalid_cursor_problem()
    try:
        payload = _decode_base64url(payload_part)
        signature = _decode_base64url(signature_part)
        decoded = json.loads(payload)
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise _invalid_cursor_problem() from error
    if not hmac.compare_digest(signature, hmac.digest(secret, payload, "sha256")):
        raise _invalid_cursor_problem()
    if not isinstance(decoded, dict):
        raise _invalid_cursor_problem()
    return decoded


def _base64url(value: bytes) -> str:
    """把游标分段编码为无填充 URL 安全文本。"""
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _decode_base64url(value: str) -> bytes:
    """严格解码 URL 安全 base64，拒绝被忽略的非法字符。"""
    padded = value + "=" * (-len(value) % 4)
    return base64.b64decode(padded, altchars=b"-_", validate=True)


def _instrument_resource(instrument: StoredEquityInstrument) -> dict[str, Any]:
    """投影合同字段并隐藏 security_id、来源批次和质量细节。"""
    return {
        "instrumentId": str(instrument.instrument_id),
        "identifier": {
            "exchange": instrument.identifier.exchange.value,
            "symbol": instrument.identifier.symbol,
            "effectiveFrom": instrument.identifier.effective_from.isoformat(),
            "effectiveTo": _date_text(instrument.identifier.effective_to),
            "datePrecision": instrument.identifier.date_precision,
            "knownFrom": _timestamp(instrument.identifier.known_from),
            "observedAt": _timestamp(instrument.identifier.observed_at),
        },
        "name": {
            "value": instrument.name.value,
            "effectiveFrom": instrument.name.effective_from.isoformat(),
            "effectiveTo": _date_text(instrument.name.effective_to),
            "datePrecision": instrument.name.date_precision,
            "knownFrom": _timestamp(instrument.name.known_from),
            "observedAt": _timestamp(instrument.name.observed_at),
        },
        "listing": {
            "status": instrument.listing.status,
            "listedOn": _date_text(instrument.listing.listed_on),
            "delistedOn": _date_text(instrument.listing.delisted_on),
            "effectiveFrom": instrument.listing.effective_from.isoformat(),
            "effectiveTo": _date_text(instrument.listing.effective_to),
            "datePrecision": instrument.listing.date_precision,
            "knownFrom": _timestamp(instrument.listing.known_from),
            "observedAt": _timestamp(instrument.listing.observed_at),
        },
    }


def _listing_period_resource(period: StoredListingStatusPeriod) -> dict[str, Any]:
    """投影生命周期知识版本，不暴露内部 version_id 或证据字段。"""
    return {
        "status": period.status,
        "effectiveFrom": period.effective_from.isoformat(),
        "effectiveTo": _date_text(period.effective_to),
        "effectiveDatePrecision": period.effective_date_precision,
        "knownFrom": _timestamp(period.known_from),
        "knownTo": None if period.known_to is None else _timestamp(period.known_to),
        "observedAt": _timestamp(period.observed_at),
    }


def _conditional_response(
    *,
    request: Request,
    if_none_match: str | None,
    publication: EquityMasterPublication,
    etag: str,
    body: dict[str, Any],
) -> Response:
    """在表示未变化时返回 304，否则返回带发布版本头的私有可复验 JSON。"""
    headers = {
        "ETag": etag,
        "Cache-Control": _PRIVATE_REVALIDATE,
        "X-Request-Id": _request_id(request),
        "X-Data-Version": str(publication.data_version),
    }
    if if_none_match == etag:
        return Response(status_code=304, headers=headers)
    return JSONResponse(content=body, headers=headers)


def _representation_etag(
    kind: str,
    publication: EquityMasterPublication,
    projection: Mapping[str, Any],
) -> str:
    """把规范化请求投影和发布版本绑定为强 ETag。"""
    discriminator = hashlib.sha256(
        json.dumps(
            projection,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).hexdigest()[:24]
    return f'"equity-{kind}-{publication.data_version}-{discriminator}"'


def _request_id(request: Request) -> str:
    """复用受限请求标识；非法或缺失值由服务生成。"""
    supplied = request.headers.get("X-Request-Id")
    return supplied if supplied is not None and 1 <= len(supplied) <= 128 else str(uuid4())


def _timestamp(value: datetime) -> str:
    """把带时区时间标准化为 RFC 3339 UTC 文本。"""
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _date_text(value: date | None) -> str | None:
    """把可空业务日期转换为 ISO 文本。"""
    return None if value is None else value.isoformat()


def _invalid_cursor_problem() -> InternalProblem:
    """构造统一游标校验问题，避免泄漏签名或内部格式。"""
    return InternalProblem(
        status=400,
        code="validation-error",
        detail="cursor is invalid",
    )


def _unavailable_problem() -> InternalProblem:
    """构造 fail-closed 依赖问题，禁止拼接未发布或混合版本数据。"""
    return InternalProblem(
        status=503,
        code="dependency-unavailable",
        detail="Equity master publication is unavailable",
    )
