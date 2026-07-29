"""板块成分观测历史的 internal v1 路由，只读取固定 release manifest。

成分关系以已发布 release 为准，`observedFrom` 与 `observedTo` 表示来源快照观察区间而非
官方调仓日；双向查询和分页都绑定该 release，避免目录更新时历史关系发生漂移。
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, Path, Query, Request
from fastapi.responses import Response

from service_data_sync.application.ports.sector_membership import (
    SectorMembershipRepository,
    StoredMembershipConstituent,
    StoredSectorMembershipRelease,
)
from service_data_sync.domain.equity import Exchange
from service_data_sync.domain.sector import SectorIdentifier, SectorScheme
from service_data_sync.interfaces.internal_sector_api import (
    InternalProblem,
    _conditional_json_response,
    _timestamp,
)


def register_sector_membership_routes(
    app: FastAPI,
    *,
    repository: SectorMembershipRepository,
    require_service_bearer: Callable[..., None],
) -> None:
    """在共享 internal app 注册双向成员读取，避免新增独立 HTTP 进程与鉴权边界。"""

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
        """返回一板块在固定 release 快照中的 verified 成分，观测边界不等同真实调入调出。"""
        sector_scheme = _scheme_or_problem(scheme)
        identifier = _identifier_or_problem(sector_scheme, sector_code)
        requested_as_of = _as_of_or_problem(as_of)
        release = _release_or_problem(repository, scheme=sector_scheme, as_of=requested_as_of)
        release_sector = repository.get_release_sector(
            release_id=release.release_id,
            identifier=identifier,
        )
        if release_sector is None:
            raise InternalProblem(status=404, code="not-found", detail="Sector is not found")
        sector, snapshot_observed_at, carried_forward = release_sector
        after_exchange, after_symbol = _constituent_cursor_or_problem(
            cursor,
            release=release,
            identifier=identifier,
            requested_as_of=requested_as_of,
        )
        rows = repository.list_constituents(
            release_id=release.release_id,
            identifier=identifier,
            after_exchange=after_exchange,
            after_symbol=after_symbol,
            limit=limit + 1,
        )
        page_rows = tuple(rows[:limit])
        next_cursor = (
            _encode_constituent_cursor(
                release=release,
                identifier=identifier,
                requested_as_of=requested_as_of,
                row=page_rows[-1],
            )
            if len(rows) > limit and page_rows
            else None
        )
        body = {
            "sector": {
                "sectorId": str(sector.sector_id),
                "scheme": sector.identifier.scheme.value,
                "code": sector.identifier.code,
                "name": sector.name,
            },
            "release": _release_resource(release),
            "snapshotObservedAt": _timestamp(snapshot_observed_at),
            "carriedForward": carried_forward,
            "items": [_constituent_resource(row) for row in page_rows],
            "nextCursor": next_cursor,
        }
        return _conditional_json_response(
            request=request,
            if_none_match=if_none_match,
            etag=_etag(
                "constituents",
                release,
                identifier.qualified_key,
                requested_as_of.isoformat() if requested_as_of is not None else "current",
                cursor or "first",
                str(limit),
            ),
            body=body,
        )

    @app.get(
        "/internal/v1/equities/{exchange}/{symbol}/sectors",
        dependencies=[Depends(require_service_bearer)],
    )
    def list_equity_sectors(
        request: Request,
        exchange: str,
        symbol: Annotated[str, Path(pattern="^[0-9]{6}$")],
        scheme: Annotated[str, Query(min_length=1)],
        as_of: Annotated[datetime | None, Query(alias="asOf")] = None,
        cursor: Annotated[str | None, Query(max_length=1024)] = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 200,
        if_none_match: Annotated[str | None, Header()] = None,
    ) -> Response:
        """返回一只 confirmed 证券在单一 release 中的板块归属，已知无归属返回空页。"""
        sector_scheme = _scheme_or_problem(scheme)
        equity_exchange = _exchange_or_problem(exchange)
        requested_as_of = _as_of_or_problem(as_of)
        release = _release_or_problem(repository, scheme=sector_scheme, as_of=requested_as_of)
        equity = repository.get_release_equity(
            release_id=release.release_id,
            exchange=equity_exchange,
            symbol=symbol,
        )
        if equity is None:
            raise InternalProblem(status=404, code="not-found", detail="Equity is not found")
        after_sector_code = _equity_cursor_or_problem(
            cursor,
            release=release,
            scheme=sector_scheme,
            exchange=equity_exchange,
            symbol=symbol,
            requested_as_of=requested_as_of,
        )
        rows = repository.list_equity_memberships(
            release_id=release.release_id,
            instrument_id=equity.instrument_id,
            after_sector_code=after_sector_code,
            limit=limit + 1,
        )
        page_rows = tuple(rows[:limit])
        next_cursor = (
            _encode_equity_cursor(
                release=release,
                scheme=sector_scheme,
                exchange=equity_exchange,
                symbol=symbol,
                requested_as_of=requested_as_of,
                sector_code=page_rows[-1].sector.identifier.code,
            )
            if len(rows) > limit and page_rows
            else None
        )
        body = {
            "equity": {
                "instrumentId": str(equity.instrument_id),
                "exchange": equity.exchange.value,
                "symbol": equity.symbol,
                "name": equity.name,
                "listingStatus": equity.listing_status,
            },
            "scheme": sector_scheme.value,
            "release": _release_resource(release),
            "items": [
                {
                    "sectorId": str(row.sector.sector_id),
                    "scheme": row.sector.identifier.scheme.value,
                    "code": row.sector.identifier.code,
                    "name": row.sector.name,
                    "observedFrom": _timestamp(row.observed_from),
                    "observedTo": _timestamp_or_none(row.observed_to),
                    "snapshotObservedAt": _timestamp(row.snapshot_observed_at),
                    "carriedForward": row.carried_forward,
                }
                for row in page_rows
            ],
            "nextCursor": next_cursor,
        }
        return _conditional_json_response(
            request=request,
            if_none_match=if_none_match,
            etag=_etag(
                "equity-sectors",
                release,
                sector_scheme.value,
                equity_exchange.value,
                symbol,
                requested_as_of.isoformat() if requested_as_of is not None else "current",
                cursor or "first",
                str(limit),
            ),
            body=body,
        )


def _scheme_or_problem(value: str) -> SectorScheme:
    """解析板块分类体系，避免任意字符串进入 release 查询。"""
    try:
        return SectorScheme(value)
    except ValueError as error:
        raise InternalProblem(
            status=400, code="validation-error", detail="scheme is invalid"
        ) from error


def _exchange_or_problem(value: str) -> Exchange:
    """解析受限证券交易所，防止反向身份查询退化为自由文本匹配。"""
    try:
        return Exchange(value)
    except ValueError as error:
        raise InternalProblem(
            status=400, code="validation-error", detail="exchange is invalid"
        ) from error


def _identifier_or_problem(scheme: SectorScheme, code: str) -> SectorIdentifier:
    """构造稳定板块身份，并把格式错误统一投影为合同参数问题。"""
    try:
        return SectorIdentifier(scheme=scheme, code=code)
    except ValueError as error:
        raise InternalProblem(
            status=400,
            code="validation-error",
            detail="sectorCode is invalid",
        ) from error


def _as_of_or_problem(value: datetime | None) -> datetime | None:
    """要求历史选择时刻带偏移量，避免 API 进程时区改变 release 语义。"""
    if value is None:
        return None
    if value.tzinfo is None:
        raise InternalProblem(
            status=400, code="validation-error", detail="asOf must include an offset"
        )
    return value.astimezone(UTC)


def _release_or_problem(
    repository: SectorMembershipRepository,
    *,
    scheme: SectorScheme,
    as_of: datetime | None,
) -> StoredSectorMembershipRelease:
    """选择已发布清单；没有当前或历史覆盖时返回 404，绝不伪造空集合。"""
    release = repository.get_release(scheme=scheme, as_of=as_of)
    if release is None:
        raise InternalProblem(
            status=404, code="not-found", detail="Membership release is not found"
        )
    return release


def _release_resource(release: StoredSectorMembershipRelease) -> dict[str, str | int | None]:
    """投影 contract 0007 release 上下文，固定公开身份覆盖与观测语义。"""
    return {
        "requestedAsOf": _timestamp_or_none(release.requested_as_of),
        "resolvedAsOf": _timestamp(release.resolved_as_of),
        "coverageStart": _timestamp(release.coverage_start),
        "membershipSemantics": "observed",
        "qualityStatus": release.quality_status,
        "identityCoveragePercent": "100",
        "excludedIdentityCount": 0,
        "carriedForwardSectorCount": release.carried_forward_sector_count,
        "dataVersion": str(release.data_version),
        "publishedAt": _timestamp(release.published_at),
    }


def _constituent_resource(row: StoredMembershipConstituent) -> dict[str, str | None]:
    """投影 verified 成分，不泄漏内部 security_id、raw 或质量处置数据。"""
    return {
        "instrumentId": str(row.instrument_id),
        "exchange": row.exchange.value,
        "symbol": row.symbol,
        "name": row.name,
        "listingStatus": row.listing_status,
        "observedFrom": _timestamp(row.observed_from),
        "observedTo": _timestamp_or_none(row.observed_to),
    }


def _constituent_cursor_or_problem(
    cursor: str | None,
    *,
    release: StoredSectorMembershipRelease,
    identifier: SectorIdentifier,
    requested_as_of: datetime | None,
) -> tuple[Exchange | None, str | None]:
    """解码并验证板块成分页游标，版本切换时返回 409 而非跨清单续页。"""
    if cursor is None:
        return None, None
    decoded = _decode_cursor(cursor)
    _validate_cursor(
        decoded,
        release=release,
        expected={
            "r": "constituents",
            "s": identifier.scheme.value,
            "c": identifier.code,
            "a": _cursor_as_of(requested_as_of),
        },
    )
    exchange = decoded.get("x")
    symbol = decoded.get("y")
    if not isinstance(exchange, str) or not isinstance(symbol, str) or len(symbol) != 6:
        raise InternalProblem(status=400, code="validation-error", detail="cursor is invalid")
    return _exchange_or_problem(exchange), symbol


def _equity_cursor_or_problem(
    cursor: str | None,
    *,
    release: StoredSectorMembershipRelease,
    scheme: SectorScheme,
    exchange: Exchange,
    symbol: str,
    requested_as_of: datetime | None,
) -> str | None:
    """解码并验证证券反向分页游标，筛选变化或版本失效均不能继续分页。"""
    if cursor is None:
        return None
    decoded = _decode_cursor(cursor)
    _validate_cursor(
        decoded,
        release=release,
        expected={
            "r": "equity-sectors",
            "s": scheme.value,
            "x": exchange.value,
            "y": symbol,
            "a": _cursor_as_of(requested_as_of),
        },
    )
    sector_code = decoded.get("c")
    if not isinstance(sector_code, str):
        raise InternalProblem(status=400, code="validation-error", detail="cursor is invalid")
    return sector_code


def _encode_constituent_cursor(
    *,
    release: StoredSectorMembershipRelease,
    identifier: SectorIdentifier,
    requested_as_of: datetime | None,
    row: StoredMembershipConstituent,
) -> str:
    """编码绑定 release、筛选和排序键的下一页起点，不让客户端依赖数据库主键。"""
    return _encode_cursor(
        {
            "r": "constituents",
            "v": str(release.data_version),
            "s": identifier.scheme.value,
            "c": identifier.code,
            "a": _cursor_as_of(requested_as_of),
            "x": row.exchange.value,
            "y": row.symbol,
        }
    )


def _encode_equity_cursor(
    *,
    release: StoredSectorMembershipRelease,
    scheme: SectorScheme,
    exchange: Exchange,
    symbol: str,
    requested_as_of: datetime | None,
    sector_code: str,
) -> str:
    """编码证券反向页的 release 和代码边界，公开结果不会含 instrument UUID。"""
    return _encode_cursor(
        {
            "r": "equity-sectors",
            "v": str(release.data_version),
            "s": scheme.value,
            "x": exchange.value,
            "y": symbol,
            "a": _cursor_as_of(requested_as_of),
            "c": sector_code,
        }
    )


def _validate_cursor(
    decoded: dict[str, Any],
    *,
    release: StoredSectorMembershipRelease,
    expected: dict[str, str | None],
) -> None:
    """校验游标筛选和 dataVersion；仅版本改变是冲突，其余格式问题为 400。"""
    if decoded.get("v") != str(release.data_version):
        raise InternalProblem(
            status=409, code="snapshot-expired", detail="Published snapshot changed"
        )
    if any(decoded.get(key) != value for key, value in expected.items()):
        raise InternalProblem(status=400, code="validation-error", detail="cursor is invalid")


def _encode_cursor(value: dict[str, str | None]) -> str:
    """以 URL 安全 base64 JSON 保存不透明分页状态，避免泄漏内部数值主键。"""
    return (
        base64.urlsafe_b64encode(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())
        .decode()
        .rstrip("=")
    )


def _decode_cursor(value: str) -> dict[str, Any]:
    """解析游标并将编码或 JSON 错误归一为稳定 400 问题。"""
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


def _cursor_as_of(value: datetime | None) -> str | None:
    """把可空 API 选择时刻规范化为游标可比较 UTC 文本。"""
    return None if value is None else _timestamp(value)


def _etag(
    kind: str,
    release: StoredSectorMembershipRelease,
    *parts: str,
) -> str:
    """构造绑定固定 release 与查询维度的强 ETag，避免不同筛选错误复用表示。"""
    digest = hashlib.sha256("\u0000".join(parts).encode()).hexdigest()[:16]
    return f'"sector-membership-{kind}-{release.data_version}-{digest}"'


def _timestamp_or_none(value: datetime | None) -> str | None:
    """将可空 UTC 时间投影为合同 RFC 3339 字段。"""
    return None if value is None else _timestamp(value)
