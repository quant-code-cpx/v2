"""供受信任服务读取已发布个股行情、因子、事件与公司概况的内部路由。

接口把日、周、月原生行情、累计复权因子、公司行动和资料分成独立资源；游标、ETag 和
`X-Data-Version` 始终绑定请求范围与 publication，防止跨证券、跨版本或跨复权口径复用。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Header, Path, Query, Request
from fastapi.responses import JSONResponse, Response

from service_data_sync.application.ports.market_data import (
    EquityAvailabilityObservation,
    EquityDatasetPublication,
    EquityIdentityReadConflictError,
    EquityMarketDataRepository,
    StoredAdjustmentFactor,
    StoredCorporateAction,
    StoredEquityBar,
    StoredEquityInstrument,
)
from service_data_sync.domain.equity import (
    EquityBarPeriod,
    EquityDailyBar,
    EquityIdentifier,
)
from service_data_sync.interfaces.internal_sector_api import InternalProblem

_PRIVATE_REVALIDATE = "private, max-age=0, must-revalidate"
_ADJUSTMENT_MODES = frozenset({"none", "qfq", "hfq"})
_FORMULA_VERSION = "cumulative-hfq-v1"
_SHANGHAI = ZoneInfo("Asia/Shanghai")


def register_equity_market_routes(
    app: FastAPI,
    *,
    repository: EquityMarketDataRepository,
    require_service_bearer: Callable[..., None],
    cursor_secret: bytes,
) -> None:
    """挂载方案 0011 的四类只读市场数据端点和防篡改分页。"""

    @app.get(
        "/internal/v1/equities/{exchange}/{symbol}/bars",
        dependencies=[Depends(require_service_bearer)],
    )
    def list_equity_bars(
        request: Request,
        exchange: str,
        symbol: Annotated[str, Path(pattern=r"^[0-9]{6}$")],
        start: Annotated[date, Query()],
        end: Annotated[date, Query()],
        period: Annotated[str, Query()] = "1d",
        adjust: Annotated[str, Query()] = "none",
        adjust_as_of: Annotated[date | None, Query(alias="adjustAsOf")] = None,
        cursor: Annotated[str | None, Query(min_length=1, max_length=1024)] = None,
        limit: Annotated[int, Query(ge=1, le=2000)] = 500,
        if_none_match: Annotated[str | None, Header(max_length=256)] = None,
    ) -> Response:
        """读取独立物理周期表，并按已发布累计因子计算可选复权价格。"""
        if start > end:
            raise _validation_problem("start must not be after end")
        selected_period = _period_or_problem(period)
        adjustment_mode = _adjustment_or_problem(adjust)
        anchor = adjust_as_of or end
        identity_start = min(start, anchor) if adjustment_mode != "none" else start
        identity_end = max(end, anchor) if adjustment_mode != "none" else end
        identifier = _identifier_or_problem(exchange, symbol)
        try:
            instrument = repository.get_instrument_by_identifier(
                identifier,
                fact_start=identity_start,
                fact_end=identity_end,
            )
        except EquityIdentityReadConflictError as error:
            raise InternalProblem(
                status=409,
                code="identity-boundary-conflict",
                detail="Equity identifier spans multiple canonical securities",
            ) from error
        availability = repository.get_daily_bar_availability(
            identifier=identifier,
            start=start,
            end=end,
        )
        if instrument is None:
            if availability is not None:
                return _empty_bar_response(
                    request=request,
                    identifier=identifier,
                    period=selected_period,
                    adjustment_mode=adjustment_mode,
                    anchor=anchor,
                    observation=availability,
                )
            raise InternalProblem(
                status=404,
                code="not-found",
                detail="Equity instrument is not found",
            )
        if availability is not None and availability.availability == "empty":
            return _empty_bar_response(
                request=request,
                identifier=identifier,
                period=selected_period,
                adjustment_mode=adjustment_mode,
                anchor=anchor,
                observation=availability,
            )
        bar_publication = repository.get_current_publication(
            dataset=selected_period.capability,
            instrument=instrument,
        )
        if bar_publication is None:
            if availability is not None:
                return _empty_bar_response(
                    request=request,
                    identifier=identifier,
                    period=selected_period,
                    adjustment_mode=adjustment_mode,
                    anchor=anchor,
                    observation=availability,
                )
            raise InternalProblem(
                status=503,
                code="dependency-unavailable",
                detail="Equity bars are not published",
            )
        bars = tuple(
            repository.list_bars(
                security_id=instrument.security_id,
                period=selected_period,
                start=start,
                end=end,
            )
        )
        factor_publication: EquityDatasetPublication | None = None
        factor_rows: Sequence[StoredAdjustmentFactor] = ()
        if adjustment_mode != "none":
            factor_publication = _publication_or_problem(
                repository,
                dataset="equity.adjustment_factor",
                instrument=instrument,
                detail="Adjustment factors are not published",
            )
            factor_rows = repository.list_adjustment_factors(
                security_id=instrument.security_id,
                end=max(anchor, end),
            )
            if not factor_rows or _factor_at(factor_rows, anchor) is None:
                raise InternalProblem(
                    status=409,
                    code="adjustment-unavailable",
                    detail="Adjustment factor anchor is unavailable",
                )
        cursor_scope = _cursor_scope(
            {
                "instrument": str(instrument.instrument_id),
                "period": selected_period.value,
                "start": start.isoformat(),
                "end": end.isoformat(),
                "adjust": adjustment_mode,
                "adjustAsOf": None if adjustment_mode == "none" else anchor.isoformat(),
            }
        )
        snapshot_version = ":".join(
            (
                str(bar_publication.data_version),
                "" if factor_publication is None else str(factor_publication.data_version),
            )
        )
        page_rows, next_cursor = _page(
            bars,
            cursor=cursor,
            limit=limit,
            cursor_secret=cursor_secret,
            cursor_scope=cursor_scope,
            snapshot_version=snapshot_version,
        )
        body = {
            "exchange": identifier.exchange.value,
            "symbol": identifier.symbol,
            "period": selected_period.value,
            "adjustmentMode": adjustment_mode,
            "adjustAsOf": None if adjustment_mode == "none" else anchor.isoformat(),
            "factorVersion": (
                None if factor_publication is None else str(factor_publication.data_version)
            ),
            "formulaVersion": None if adjustment_mode == "none" else _FORMULA_VERSION,
            "dataVersion": str(bar_publication.data_version),
            "publishedAt": _timestamp(bar_publication),
            "availability": (
                "SOURCE_UNAVAILABLE"
                if availability is not None and availability.availability == "source_unavailable"
                else "AVAILABLE"
            ),
            "observedAt": (
                None
                if availability is None or availability.availability != "source_unavailable"
                else availability.observed_at.isoformat().replace("+00:00", "Z")
            ),
            "reasonCode": (
                None
                if availability is None or availability.availability != "source_unavailable"
                else availability.reason_code
            ),
            "qualityStatus": "passed",
            "stale": availability is not None and availability.availability == "source_unavailable",
            "items": [
                _bar_resource(
                    row,
                    adjustment_mode=adjustment_mode,
                    factors=factor_rows,
                    anchor=anchor,
                )
                for row in page_rows
            ],
            "nextCursor": next_cursor,
        }
        return _conditional_response(
            request=request,
            if_none_match=if_none_match,
            publication=bar_publication,
            etag=_etag(
                "bars",
                bar_publication,
                {
                    "instrument": str(instrument.instrument_id),
                    "period": selected_period.value,
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "adjust": adjustment_mode,
                    "adjustAsOf": None if adjustment_mode == "none" else anchor.isoformat(),
                    "factorVersion": (
                        None if factor_publication is None else str(factor_publication.data_version)
                    ),
                    "cursor": cursor,
                    "limit": limit,
                },
            ),
            body=body,
        )

    @app.get(
        "/internal/v1/equities/{exchange}/{symbol}/adjustment-factors",
        dependencies=[Depends(require_service_bearer)],
    )
    def list_adjustment_factors(
        request: Request,
        exchange: str,
        symbol: Annotated[str, Path(pattern=r"^[0-9]{6}$")],
        end: Annotated[date, Query()],
        start: Annotated[date | None, Query()] = None,
        cursor: Annotated[str | None, Query(min_length=1, max_length=1024)] = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 500,
        if_none_match: Annotated[str | None, Header(max_length=256)] = None,
    ) -> Response:
        """读取当前稀疏累计后复权因子序列。"""
        if start is not None and start > end:
            raise _validation_problem("start must not be after end")
        identifier, instrument = _instrument_or_problem(
            repository,
            exchange,
            symbol,
            fact_start=start,
            fact_end=end,
        )
        publication = _publication_or_problem(
            repository,
            dataset="equity.adjustment_factor",
            instrument=instrument,
            detail="Adjustment factors are not published",
        )
        all_rows = tuple(
            row
            for row in repository.list_adjustment_factors(
                security_id=instrument.security_id,
                end=end,
            )
            if start is None or row.factor.effective_date >= start
        )
        rows, next_cursor = _page(
            all_rows,
            cursor=cursor,
            limit=limit,
            cursor_secret=cursor_secret,
            cursor_scope=_cursor_scope(
                {
                    "instrument": str(instrument.instrument_id),
                    "start": None if start is None else start.isoformat(),
                    "end": end.isoformat(),
                }
            ),
            snapshot_version=str(publication.data_version),
        )
        body = {
            "exchange": identifier.exchange.value,
            "symbol": identifier.symbol,
            "factorVersion": str(publication.data_version),
            "dataVersion": str(publication.data_version),
            "publishedAt": _timestamp(publication),
            "qualityStatus": "passed",
            "stale": False,
            "items": [
                {
                    "effectiveDate": row.factor.effective_date.isoformat(),
                    "cumulativeFactor": str(row.factor.cumulative_factor),
                    "revision": row.revision,
                }
                for row in rows
            ],
            "nextCursor": next_cursor,
        }
        return _conditional_response(
            request=request,
            if_none_match=if_none_match,
            publication=publication,
            etag=_etag(
                "factors",
                publication,
                {
                    "instrument": str(instrument.instrument_id),
                    "start": None if start is None else start.isoformat(),
                    "end": end.isoformat(),
                    "cursor": cursor,
                    "limit": limit,
                },
            ),
            body=body,
        )

    @app.get(
        "/internal/v1/equities/{exchange}/{symbol}/corporate-actions",
        dependencies=[Depends(require_service_bearer)],
    )
    def list_corporate_actions(
        request: Request,
        exchange: str,
        symbol: Annotated[str, Path(pattern=r"^[0-9]{6}$")],
        start: Annotated[date | None, Query()] = None,
        end: Annotated[date | None, Query()] = None,
        cursor: Annotated[str | None, Query(min_length=1, max_length=1024)] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 100,
        if_none_match: Annotated[str | None, Header(max_length=256)] = None,
    ) -> Response:
        """读取公司行动当前 revision，并保留事件稳定 UUID。"""
        if start is not None and end is not None and start > end:
            raise _validation_problem("start must not be after end")
        identifier, instrument = _instrument_or_problem(
            repository,
            exchange,
            symbol,
            fact_start=start,
            fact_end=end,
        )
        publication = _publication_or_problem(
            repository,
            dataset="equity.corporate_action",
            instrument=instrument,
            detail="Corporate actions are not published",
        )
        all_rows = repository.list_corporate_actions(
            security_id=instrument.security_id,
            start=start,
            end=end,
        )
        rows, next_cursor = _page(
            all_rows,
            cursor=cursor,
            limit=limit,
            cursor_secret=cursor_secret,
            cursor_scope=_cursor_scope(
                {
                    "instrument": str(instrument.instrument_id),
                    "start": None if start is None else start.isoformat(),
                    "end": None if end is None else end.isoformat(),
                }
            ),
            snapshot_version=str(publication.data_version),
        )
        body = {
            "exchange": identifier.exchange.value,
            "symbol": identifier.symbol,
            "dataVersion": str(publication.data_version),
            "publishedAt": _timestamp(publication),
            "qualityStatus": "passed",
            "stale": False,
            "items": [_action_resource(row) for row in rows],
            "nextCursor": next_cursor,
        }
        return _conditional_response(
            request=request,
            if_none_match=if_none_match,
            publication=publication,
            etag=_etag(
                "actions",
                publication,
                {
                    "instrument": str(instrument.instrument_id),
                    "start": None if start is None else start.isoformat(),
                    "end": None if end is None else end.isoformat(),
                    "cursor": cursor,
                    "limit": limit,
                },
            ),
            body=body,
        )

    @app.get(
        "/internal/v1/equities/{exchange}/{symbol}/company-profile",
        dependencies=[Depends(require_service_bearer)],
    )
    def get_company_profile(
        request: Request,
        exchange: str,
        symbol: Annotated[str, Path(pattern=r"^[0-9]{6}$")],
        if_none_match: Annotated[str | None, Header(max_length=256)] = None,
    ) -> Response:
        """读取当前已发布公司概况，不返回 provider 字段或数据库键。"""
        fact_date = datetime.now(_SHANGHAI).date()
        identifier, instrument = _instrument_or_problem(
            repository,
            exchange,
            symbol,
            fact_start=fact_date,
            fact_end=fact_date,
        )
        publication = _publication_or_problem(
            repository,
            dataset="equity.profile",
            instrument=instrument,
            detail="Company profile is not published",
        )
        stored = repository.get_company_profile(security_id=instrument.security_id)
        if stored is None:
            raise InternalProblem(
                status=404,
                code="not-found",
                detail="Company profile is not found",
            )
        profile = stored.profile
        body = {
            "exchange": identifier.exchange.value,
            "symbol": identifier.symbol,
            "dataVersion": str(publication.data_version),
            "publishedAt": _timestamp(publication),
            "qualityStatus": "passed",
            "stale": False,
            "revision": stored.revision,
            "profile": {
                "companyName": profile.company_name,
                "englishName": profile.english_name,
                "industry": profile.industry,
                "legalRepresentative": profile.legal_representative,
                "establishedOn": _date_text(profile.established_on),
                "website": profile.website,
                "email": profile.email,
                "phone": profile.phone,
                "registeredAddress": profile.registered_address,
                "officeAddress": profile.office_address,
                "mainBusiness": profile.main_business,
                "businessScope": profile.business_scope,
                "summary": profile.summary,
            },
        }
        return _conditional_response(
            request=request,
            if_none_match=if_none_match,
            publication=publication,
            etag=_etag(
                "profile",
                publication,
                {"instrument": str(instrument.instrument_id)},
            ),
            body=body,
        )


def _instrument_or_problem(
    repository: EquityMarketDataRepository,
    exchange: str,
    symbol: str,
    *,
    fact_start: date | None,
    fact_end: date | None,
) -> tuple[EquityIdentifier, StoredEquityInstrument]:
    """按事实窗口和当前知识解析唯一确认身份，并拒绝代码复用歧义。"""
    identifier = _identifier_or_problem(exchange, symbol)
    try:
        instrument = repository.get_instrument_by_identifier(
            identifier,
            fact_start=fact_start,
            fact_end=fact_end,
        )
    except EquityIdentityReadConflictError as error:
        raise InternalProblem(
            status=409,
            code="identity-boundary-conflict",
            detail="Equity identifier spans multiple canonical securities",
        ) from error
    if instrument is None:
        raise InternalProblem(
            status=404,
            code="not-found",
            detail="Equity instrument is not found",
        )
    return identifier, instrument


def _identifier_or_problem(exchange: str, symbol: str) -> EquityIdentifier:
    """解析交易所限定代码，避免空观测路径绕开身份输入校验。"""
    try:
        return EquityIdentifier.parse(f"{exchange}.{symbol}")
    except ValueError as error:
        raise _validation_problem("equity identifier is invalid") from error


def _page[PageRow](
    rows: Sequence[PageRow],
    *,
    cursor: str | None,
    limit: int,
    cursor_secret: bytes,
    cursor_scope: str,
    snapshot_version: str,
) -> tuple[Sequence[PageRow], str | None]:
    """在固定发布和查询范围内按稳定序号切页，并签发下一页游标。"""
    offset = 0
    if cursor is not None:
        decoded = _decode_cursor(cursor, secret=cursor_secret)
        if decoded.get("v") != snapshot_version:
            raise InternalProblem(
                status=409,
                code="snapshot-expired",
                detail="Published equity market snapshot changed",
            )
        if decoded.get("s") != cursor_scope:
            raise _validation_problem("cursor does not match query")
        decoded_offset = decoded.get("o")
        if (
            not isinstance(decoded_offset, int)
            or isinstance(decoded_offset, bool)
            or decoded_offset < 0
            or decoded_offset > len(rows)
        ):
            raise _validation_problem("cursor is invalid")
        offset = decoded_offset
    page_end = min(offset + limit, len(rows))
    next_cursor = (
        None
        if page_end >= len(rows)
        else _encode_cursor(
            {"o": page_end, "s": cursor_scope, "v": snapshot_version},
            secret=cursor_secret,
        )
    )
    return rows[offset:page_end], next_cursor


def _cursor_scope(projection: Mapping[str, Any]) -> str:
    """把不含页大小和游标的标准查询绑定为紧凑范围指纹。"""
    return hashlib.sha256(
        json.dumps(projection, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()[:24]


def _encode_cursor(value: Mapping[str, Any], *, secret: bytes) -> str:
    """以稳定 JSON 和 HMAC-SHA256 生成不透明、防篡改游标。"""
    payload = json.dumps(value, separators=(",", ":"), sort_keys=True).encode()
    signature = hmac.digest(secret, payload, "sha256")
    return f"{_base64url(payload)}.{_base64url(signature)}"


def _decode_cursor(value: str, *, secret: bytes) -> Mapping[str, Any]:
    """严格解码并常量时间验签游标。"""
    payload_part, separator, signature_part = value.partition(".")
    if separator != "." or not payload_part or not signature_part:
        raise _validation_problem("cursor is invalid")
    try:
        payload = _decode_base64url(payload_part)
        signature = _decode_base64url(signature_part)
        decoded = json.loads(payload)
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise _validation_problem("cursor is invalid") from error
    if not hmac.compare_digest(signature, hmac.digest(secret, payload, "sha256")):
        raise _validation_problem("cursor is invalid")
    if not isinstance(decoded, dict):
        raise _validation_problem("cursor is invalid")
    return decoded


def _base64url(value: bytes) -> str:
    """把游标分段编码为无填充 URL 安全文本。"""
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _decode_base64url(value: str) -> bytes:
    """严格解码 URL 安全 base64，拒绝被忽略的非法字符。"""
    padded = value + "=" * (-len(value) % 4)
    return base64.b64decode(padded, altchars=b"-_", validate=True)


def _period_or_problem(value: str) -> EquityBarPeriod:
    """解析三个独立物理周期。"""
    try:
        return EquityBarPeriod(value)
    except ValueError as error:
        raise _validation_problem("period is invalid") from error


def _adjustment_or_problem(value: str) -> str:
    """解析封闭复权模式。"""
    if value not in _ADJUSTMENT_MODES:
        raise _validation_problem("adjust is invalid")
    return value


def _publication_or_problem(
    repository: EquityMarketDataRepository,
    *,
    dataset: str,
    instrument: StoredEquityInstrument,
    detail: str,
) -> EquityDatasetPublication:
    """要求永久证券分区已有通过质量门的当前发布。"""
    publication = repository.get_current_publication(
        dataset=dataset,
        instrument=instrument,
    )
    if publication is None:
        raise InternalProblem(status=503, code="dependency-unavailable", detail=detail)
    return publication


def _bar_resource(
    row: StoredEquityBar,
    *,
    adjustment_mode: str,
    factors: Sequence[StoredAdjustmentFactor],
    anchor: date,
) -> dict[str, str | bool | int | None]:
    """投影行情并仅对价格应用累计因子，量、额与换手率保持原始口径。"""
    bar = row.bar
    period_end = bar.trade_date if isinstance(bar, EquityDailyBar) else bar.period_end
    multiplier = Decimal("1")
    if adjustment_mode != "none":
        factor = _factor_at(factors, period_end)
        anchor_factor = _factor_at(factors, anchor)
        if factor is None or anchor_factor is None:
            raise InternalProblem(
                status=409,
                code="adjustment-unavailable",
                detail="Adjustment factor does not cover requested bars",
            )
        multiplier = factor if adjustment_mode == "hfq" else factor / anchor_factor
    return {
        "periodEnd": period_end.isoformat(),
        "open": _price_text(bar.open_price * multiplier),
        "high": _price_text(bar.high_price * multiplier),
        "low": _price_text(bar.low_price * multiplier),
        "close": _price_text(bar.close_price * multiplier),
        "volumeShares": str(bar.volume_shares),
        "amountCny": str(bar.amount_cny),
        "turnoverRate": (None if bar.turnover_rate is None else str(bar.turnover_rate)),
        "isFinal": row.is_final,
        "revision": row.revision,
    }


def _empty_bar_response(
    *,
    request: Request,
    identifier: EquityIdentifier,
    period: EquityBarPeriod,
    adjustment_mode: str,
    anchor: date,
    observation: EquityAvailabilityObservation,
) -> Response:
    """返回不含伪造事实和 publication 版本的成功空页。"""
    body = {
        "exchange": identifier.exchange.value,
        "symbol": identifier.symbol,
        "period": period.value,
        "adjustmentMode": adjustment_mode,
        "adjustAsOf": None if adjustment_mode == "none" else anchor.isoformat(),
        "factorVersion": None,
        "formulaVersion": None,
        "dataVersion": None,
        "publishedAt": None,
        "availability": observation.availability.upper(),
        "observedAt": observation.observed_at.isoformat().replace("+00:00", "Z"),
        "reasonCode": observation.reason_code,
        "qualityStatus": None,
        "stale": False,
        "items": [],
        "nextCursor": None,
    }
    return JSONResponse(
        content=body,
        headers={
            "Cache-Control": "no-store",
            "X-Request-Id": _request_id(request),
        },
    )


def _factor_at(
    factors: Sequence[StoredAdjustmentFactor],
    target: date,
) -> Decimal | None:
    """按生效日向后选择目标日使用的最后累计因子。"""
    selected: Decimal | None = None
    for row in factors:
        if row.factor.effective_date > target:
            break
        selected = row.factor.cumulative_factor
    return selected


def _action_resource(row: StoredCorporateAction) -> dict[str, str | int | None]:
    """投影公司行动标准字段。"""
    action = row.action
    return {
        "actionId": str(row.action_id),
        "revision": row.revision,
        "reportPeriod": action.report_period.isoformat(),
        "status": action.status,
        "announcementDate": _date_text(action.announcement_date),
        "recordDate": _date_text(action.record_date),
        "exDate": _date_text(action.ex_date),
        "cashDividendPer10": _decimal_text(action.cash_dividend_per_10),
        "bonusSharesPer10": _decimal_text(action.bonus_shares_per_10),
        "transferSharesPer10": _decimal_text(action.transfer_shares_per_10),
    }


def _conditional_response(
    *,
    request: Request,
    if_none_match: str | None,
    publication: EquityDatasetPublication,
    etag: str,
    body: Mapping[str, Any],
) -> Response:
    """命中 ETag 时返回 304，否则返回带数据版本的私有可复验 JSON。"""
    headers = {
        "ETag": etag,
        "Cache-Control": _PRIVATE_REVALIDATE,
        "X-Request-Id": _request_id(request),
        "X-Data-Version": str(publication.data_version),
    }
    if if_none_match == etag:
        return Response(status_code=304, headers=headers)
    return JSONResponse(content=dict(body), headers=headers)


def _request_id(request: Request) -> str:
    """复用调用方的有界关联标识，缺失或畸形时生成新的 UUID。"""
    supplied_request_id = request.headers.get("X-Request-Id")
    if supplied_request_id is not None and 1 <= len(supplied_request_id) <= 128:
        return supplied_request_id
    return str(uuid4())


def _etag(
    kind: str,
    publication: EquityDatasetPublication,
    projection: Mapping[str, Any],
) -> str:
    """把标准查询投影和发布版本绑定为强 ETag。"""
    discriminator = hashlib.sha256(
        json.dumps(projection, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()[:24]
    return f'"equity-{kind}-{publication.data_version}-{discriminator}"'


def _price_text(value: Decimal) -> str:
    """把复权结果规范到数据库价格精度并移除无意义尾零。"""
    quantized = value.quantize(Decimal("0.000001"))
    return format(quantized, "f")


def _timestamp(publication: EquityDatasetPublication) -> str:
    """把发布时间标准化为 RFC 3339 UTC 文本。"""
    return publication.published_at.isoformat().replace("+00:00", "Z")


def _date_text(value: date | None) -> str | None:
    """把可空日期转换为 ISO 文本。"""
    return None if value is None else value.isoformat()


def _decimal_text(value: Decimal | None) -> str | None:
    """把可空十进制值转换为精确字符串。"""
    return None if value is None else str(value)


def _validation_problem(detail: str) -> InternalProblem:
    """构造统一参数问题。"""
    return InternalProblem(status=400, code="validation-error", detail=detail)
