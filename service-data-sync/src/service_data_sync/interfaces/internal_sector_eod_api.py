"""板块 EOD 快照与确定性排行的内部 HTTP 路由。

读取只面向已经发布的完整横截面，并将快照版本、交易日和稳定排序一并固定；candidate、
quarantine 和影子观测不会被意外返回，确保消费者不会在同一请求范围看到半批数据。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Annotated

from fastapi import Depends, FastAPI, Header, Path, Query, Request
from fastapi.responses import Response

from service_data_sync.application.ports.sector_eod import (
    RankedSectorEodQuote,
    SectorEodRepository,
)
from service_data_sync.domain.sector import (
    SectorEodSnapshot,
    SectorEodSort,
    SectorIdentifier,
    SectorScheme,
    SortOrder,
)
from service_data_sync.interfaces.internal_sector_api import InternalProblem

_PRIVATE_REVALIDATE = "private, max-age=0, must-revalidate"


def register_sector_eod_routes(
    app: FastAPI,
    *,
    repository: SectorEodRepository,
    require_service_bearer: Callable[[], None],
    cursor_secret: bytes,
) -> None:
    """注册静态 EOD 路由，必须早于通用 `/{scheme}/{sectorCode}` 板块路由。"""

    @app.get("/internal/v1/sectors/eod-snapshots", dependencies=[Depends(require_service_bearer)])
    def list_eod_snapshots(
        request: Request,
        scheme: Annotated[str, Query(min_length=1)],
        as_of: Annotated[date | None, Query(alias="asOf")] = None,
        sort: Annotated[str, Query()] = SectorEodSort.CHANGE_PERCENT.value,
        order: Annotated[str, Query()] = SortOrder.DESC.value,
        cursor: Annotated[str | None, Query(max_length=1024)] = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
        if_none_match: Annotated[str | None, Header()] = None,
    ) -> Response:
        """在一个 immutable 快照内分页读取动态排行，拒绝跨版本游标续读。"""
        sector_scheme = _scheme_or_problem(scheme)
        eod_sort = _sort_or_problem(sort)
        sort_order = _order_or_problem(order)
        snapshot = _snapshot_or_problem(repository, scheme=sector_scheme, as_of=as_of)
        after_position = _cursor_or_problem(
            cursor,
            secret=cursor_secret,
            snapshot=snapshot,
            sort=eod_sort,
            order=sort_order,
        )
        rows = repository.list_ranked_quotes(
            snapshot_id=snapshot.snapshot_id,
            sort=eod_sort,
            order=sort_order,
            after_position=after_position,
            limit=limit + 1,
        )
        visible_rows = tuple(rows[:limit])
        next_cursor = (
            _encode_cursor(
                secret=cursor_secret,
                snapshot=snapshot,
                sort=eod_sort,
                order=sort_order,
                position=visible_rows[-1].position,
            )
            if len(rows) > limit and visible_rows
            else None
        )
        body = {
            **_snapshot_metadata(snapshot),
            "sort": eod_sort.value,
            "order": sort_order.value,
            "items": [_ranked_resource(row) for row in visible_rows],
            "nextCursor": next_cursor,
        }
        return _conditional_json_response(
            request=request,
            if_none_match=if_none_match,
            etag=_etag(
                "sector-eod-page",
                snapshot.data_version,
                eod_sort.value,
                sort_order.value,
                cursor,
                limit,
            ),
            body=body,
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
        """读取最新或精确交易日的单板块发布报价，不把候选或旧 revision 暴露出去。"""
        sector_scheme = _scheme_or_problem(scheme)
        identifier = _identifier_or_problem(sector_scheme, sector_code)
        snapshot = _snapshot_or_problem(repository, scheme=sector_scheme, as_of=as_of)
        quote = repository.get_snapshot_quote(
            snapshot_id=snapshot.snapshot_id, identifier=identifier
        )
        if quote is None:
            raise InternalProblem(
                status=404,
                code="not-found",
                detail="Sector observation is not published",
            )
        body = {**_snapshot_metadata(snapshot), **_quote_resource(quote, include_sector_id=True)}
        return _conditional_json_response(
            request=request,
            if_none_match=if_none_match,
            etag=_etag("sector-eod-resource", snapshot.data_version, identifier.qualified_key),
            body=body,
        )


def _scheme_or_problem(value: str) -> SectorScheme:
    """解析受限分类体系，避免任意字符串参与 SQL 和 cursor 比较。"""
    try:
        return SectorScheme(value)
    except ValueError as error:
        raise InternalProblem(
            status=400, code="validation-error", detail="scheme is invalid"
        ) from error


def _sort_or_problem(value: str) -> SectorEodSort:
    """解析冻结的 EOD 排行字段，杜绝客户端输入 SQL 标识符。"""
    try:
        return SectorEodSort(value)
    except ValueError as error:
        raise InternalProblem(
            status=400, code="validation-error", detail="sort is invalid"
        ) from error


def _order_or_problem(value: str) -> SortOrder:
    """解析升降序枚举，使仓储只拼接内部受控 SQL 常量。"""
    try:
        return SortOrder(value)
    except ValueError as error:
        raise InternalProblem(
            status=400, code="validation-error", detail="order is invalid"
        ) from error


def _identifier_or_problem(scheme: SectorScheme, code: str) -> SectorIdentifier:
    """构造 scheme 内稳定板块身份，并把格式错误映射为公开参数问题。"""
    try:
        return SectorIdentifier(scheme=scheme, code=code)
    except ValueError as error:
        raise InternalProblem(
            status=400, code="validation-error", detail="sectorCode is invalid"
        ) from error


def _snapshot_or_problem(
    repository: SectorEodRepository, *, scheme: SectorScheme, as_of: date | None
) -> SectorEodSnapshot:
    """读取 latest 或精确已发布版本；latest 缺失是依赖不可用，精确缺失是 404。"""
    snapshot = repository.get_published_snapshot(scheme=scheme, trade_date=as_of)
    if snapshot is not None:
        return snapshot
    if as_of is None:
        raise InternalProblem(
            status=503,
            code="dependency-unavailable",
            detail="No published sector EOD snapshot is available",
        )
    raise InternalProblem(
        status=404,
        code="not-found",
        detail="Requested trade date is not published",
    )


def _snapshot_metadata(snapshot: SectorEodSnapshot) -> dict[str, str]:
    """投影所有 EOD 元数据，明确 `post_close_observation` 不是官方 final。"""
    if snapshot.published_at is None:
        # internal HTTP 只从 published 查询投影；candidate 到此处说明仓储边界被破坏。
        raise RuntimeError("internal sector eod API requires a published snapshot")
    return {
        "scheme": snapshot.scheme.value,
        "tradeDate": snapshot.trade_date.isoformat(),
        "sourceCutoffAt": _timestamp(snapshot.source_cutoff_at),
        "observedAt": _timestamp(snapshot.observed_at),
        "finality": snapshot.finality.value,
        "qualityStatus": snapshot.quality_status,
        "dataVersion": str(snapshot.data_version),
        "publishedAt": _timestamp(snapshot.published_at),
    }


def _ranked_resource(row: RankedSectorEodQuote) -> dict[str, str | int | None]:
    """投影排行项并保留 null-last 输出与 stable position，不公开供应商排名。"""
    return {
        **_quote_resource(row, include_sector_id=True),
        "rank": row.rank,
        "position": row.position,
    }


def _quote_resource(
    row: RankedSectorEodQuote, *, include_sector_id: bool
) -> dict[str, str | int | None]:
    """投影来源原生数值和显示名称，禁止把领涨名称解析或猜测为证券身份。"""
    quote = row.quote
    body: dict[str, str | int | None] = {
        "scheme": quote.identifier.scheme.value,
        "code": quote.identifier.code,
        "name": quote.name,
        "latestValue": _decimal_text(quote.latest_value),
        "latestValueUnit": "provider_native",
        "changeValue": _decimal_text(quote.change_value),
        "changePercent": _decimal_text(quote.change_percent),
        "marketValue": _decimal_text(quote.market_value),
        "marketValueUnit": "provider_native",
        "turnoverPercent": _decimal_text(quote.turnover_percent),
        "advancers": quote.advancers,
        "decliners": quote.decliners,
        "leaderName": quote.leader_name,
        "leaderChangePercent": _decimal_text(quote.leader_change_percent),
    }
    if include_sector_id:
        return {"sectorId": str(row.sector_id), **body}
    return body


def _decimal_text(value: Decimal | None) -> str | None:
    """将精确可空数值转换为合同规定的 JSON 字符串而非二进制浮点。"""
    return None if value is None else str(value)


def _timestamp(value: datetime) -> str:
    """将有时区时间统一渲染为 RFC 3339 UTC 文本，避免跨服务时区歧义。"""
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _cursor_or_problem(
    cursor: str | None,
    *,
    secret: bytes,
    snapshot: SectorEodSnapshot,
    sort: SectorEodSort,
    order: SortOrder,
) -> int | None:
    """验证 cursor 所有查询维度；仅 dataVersion 变化返回 409 促使客户端重启首页。"""
    if cursor is None:
        return None
    decoded = _decode_cursor(cursor, secret=secret)
    expected = {
        "s": snapshot.scheme.value,
        "d": snapshot.trade_date.isoformat(),
        "x": sort.value,
        "o": order.value,
    }
    if any(decoded.get(key) != value for key, value in expected.items()):
        raise InternalProblem(status=400, code="validation-error", detail="cursor is invalid")
    if decoded.get("v") != str(snapshot.data_version):
        raise InternalProblem(
            status=409,
            code="snapshot-expired",
            detail="Published snapshot changed",
        )
    position = decoded.get("p")
    if isinstance(position, bool) or not isinstance(position, int) or position < 1:
        raise InternalProblem(status=400, code="validation-error", detail="cursor is invalid")
    return position


def _encode_cursor(
    *,
    secret: bytes,
    snapshot: SectorEodSnapshot,
    sort: SectorEodSort,
    order: SortOrder,
    position: int,
) -> str:
    """签名编码不透明续页状态，只保存 API 契约允许的版本与位置。"""
    payload = json.dumps(
        {
            "s": snapshot.scheme.value,
            "d": snapshot.trade_date.isoformat(),
            "x": sort.value,
            "o": order.value,
            "v": str(snapshot.data_version),
            "p": position,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    signature = hmac.new(secret, payload, hashlib.sha256).digest()
    payload_part = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    signature_part = base64.urlsafe_b64encode(signature).decode().rstrip("=")
    return f"{payload_part}.{signature_part}"


def _decode_cursor(value: str, *, secret: bytes) -> dict[str, object]:
    """验证 HMAC 后解析不透明 cursor，任何编码或签名错误统一为 400。"""
    try:
        payload_part, signature_part = value.split(".", 1)
        payload = _decode_base64url(payload_part)
        signature = _decode_base64url(signature_part)
        expected = hmac.new(secret, payload, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError("cursor signature is invalid")
        parsed = json.loads(payload)
        if not isinstance(parsed, dict):
            raise ValueError("cursor payload is invalid")
        return parsed
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise InternalProblem(
            status=400, code="validation-error", detail="cursor is invalid"
        ) from error


def _decode_base64url(value: str) -> bytes:
    """解码无填充 Base64URL 分段，避免 HMAC 原始字节与分隔符发生歧义。"""
    padded = value + "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(padded.encode())


def _etag(*parts: object) -> str:
    """从 dataVersion 与请求投影生成强 ETag，避免不同排序页错误复用缓存。"""
    digest = hashlib.sha256(
        json.dumps(parts, default=str, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()
    return f'"{digest}"'


def _conditional_json_response(
    *,
    request: Request,
    if_none_match: str | None,
    etag: str,
    body: dict[str, object],
) -> Response:
    """处理条件 GET；命中时返回无 body 的 304，未命中时附带私有复验缓存策略。"""
    headers = {"ETag": etag, "Cache-Control": _PRIVATE_REVALIDATE}
    if if_none_match is not None and hmac.compare_digest(if_none_match, etag):
        return Response(status_code=304, headers=headers)
    return Response(
        content=json.dumps(body, ensure_ascii=False, separators=(",", ":")),
        media_type="application/json",
        headers=headers,
    )
