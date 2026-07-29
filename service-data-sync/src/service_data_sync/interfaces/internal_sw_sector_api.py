"""申万 taxonomy、父级闭包与估值的版本化内部只读 API。

接口将一至三级行业、父级路径、方法学血缘和指定日期估值绑定到一个生产 release；游标
和 ETag 随版本变化，因而调用方不会把不同 taxonomy 版本或研究态观察混入同一页面。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends, FastAPI, Header, Path, Query, Request
from fastapi.responses import JSONResponse, Response

from service_data_sync.application.ports.sw_sector import (
    SwCapability,
    SwPublication,
    SwSectorRepository,
    SwStoredNode,
    SwStoredValuation,
)

_TAXONOMY_CAPABILITY: SwCapability = "sector.sw.taxonomy"
_VALUATION_CAPABILITY: SwCapability = "sector.sw.valuation"
_CODE_PATTERN = r"^[0-9]{6}\.SI$"


class SwSectorApiProblem(RuntimeError):
    """表示可安全返回给内部调用方的申万读取失败。"""

    def __init__(self, *, status: int, code: str, detail: str) -> None:
        """保存稳定状态、错误码和不泄漏持久化细节的说明。"""
        super().__init__(detail)
        self.status = status
        self.code = code
        self.detail = detail


def register_sw_sector_routes(
    app: FastAPI,
    *,
    repository: SwSectorRepository,
    require_service_bearer: Callable[..., None],
    cursor_secret: bytes,
) -> None:
    """向共享 FastAPI 应用注册申万目录、详情、闭包和估值路由。"""
    if len(cursor_secret) < 16:
        raise ValueError("SW cursor secret must contain at least 16 bytes")

    @app.exception_handler(SwSectorApiProblem)
    async def render_sw_problem(request: Request, error: SwSectorApiProblem) -> JSONResponse:
        """把申万预期失败投影为带 requestId 的 Problem Details。"""
        request_id = _request_id(request)
        headers = {"X-Request-Id": request_id, "Cache-Control": "no-store"}
        if error.status == 503:
            headers["Retry-After"] = "5"
        return JSONResponse(
            status_code=error.status,
            content={
                "type": f"https://quant-v2.invalid/problems/{error.code}",
                "title": error.code,
                "status": error.status,
                "detail": error.detail,
                "code": error.code,
                "requestId": request_id,
            },
            media_type="application/problem+json",
            headers=headers,
        )

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
        """分页读取精确日期或最新日期的申万估值观察。"""
        publication = _publication_or_problem(
            repository, capability=_VALUATION_CAPABILITY, snapshot_date=snapshot_date
        )
        after_code = _valuation_cursor_or_problem(
            cursor,
            publication=publication,
            level=level,
            secret=cursor_secret,
        )
        rows = repository.list_valuations(
            snapshot_date=publication.snapshot_date,
            level=level,
            after_code=after_code,
            limit=limit + 1,
        )
        visible_rows = tuple(rows[:limit])
        next_cursor = (
            _encode_cursor(
                {
                    "kind": "sw-valuation",
                    "dataVersion": str(publication.data_version),
                    "snapshotDate": publication.snapshot_date.isoformat(),
                    "level": level,
                    "afterCode": visible_rows[-1].node.code,
                },
                cursor_secret,
            )
            if len(rows) > limit and visible_rows
            else None
        )
        body = {
            "scheme": "sw.industry",
            "release": _release(publication),
            "items": [_valuation_resource(row) for row in visible_rows],
            "nextCursor": next_cursor,
        }
        return _conditional_response(
            request=request,
            if_none_match=if_none_match,
            data_version=publication.data_version,
            representation=("sw-valuations", level, cursor, limit),
            body=body,
        )

    @app.get(
        "/internal/v1/sw-industries",
        dependencies=[Depends(require_service_bearer)],
    )
    def list_nodes(
        request: Request,
        snapshot_date: Annotated[date | None, Query(alias="snapshotDate")] = None,
        level: Annotated[int | None, Query(ge=1, le=3)] = None,
        parent_code: Annotated[str | None, Query(alias="parentCode", pattern=_CODE_PATTERN)] = None,
        cursor: Annotated[str | None, Query(max_length=1024)] = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
        if_none_match: Annotated[str | None, Header()] = None,
    ) -> Response:
        """分页读取完整 taxonomy，并让游标绑定版本与筛选范围。"""
        publication = _publication_or_problem(
            repository, capability=_TAXONOMY_CAPABILITY, snapshot_date=snapshot_date
        )
        after_level, after_code = _node_cursor_or_problem(
            cursor,
            publication=publication,
            level=level,
            parent_code=parent_code,
            secret=cursor_secret,
        )
        rows = repository.list_nodes(
            snapshot_date=publication.snapshot_date,
            level=level,
            parent_code=parent_code,
            after_level=after_level,
            after_code=after_code,
            limit=limit + 1,
        )
        visible_rows = tuple(rows[:limit])
        next_cursor = (
            _encode_cursor(
                {
                    "kind": "sw-node",
                    "dataVersion": str(publication.data_version),
                    "snapshotDate": publication.snapshot_date.isoformat(),
                    "level": level,
                    "parentCode": parent_code,
                    "afterLevel": visible_rows[-1].node.level.value,
                    "afterCode": visible_rows[-1].node.code,
                },
                cursor_secret,
            )
            if len(rows) > limit and visible_rows
            else None
        )
        body = {
            "scheme": "sw.industry",
            "release": _release(publication),
            "items": [_node_resource(row) for row in visible_rows],
            "nextCursor": next_cursor,
        }
        return _conditional_response(
            request=request,
            if_none_match=if_none_match,
            data_version=publication.data_version,
            representation=("sw-nodes", level, parent_code, cursor, limit),
            body=body,
        )

    @app.get(
        "/internal/v1/sw-industries/{sector_code}",
        dependencies=[Depends(require_service_bearer)],
    )
    def get_node(
        request: Request,
        sector_code: Annotated[str, Path(pattern=_CODE_PATTERN)],
        snapshot_date: Annotated[date | None, Query(alias="snapshotDate")] = None,
        if_none_match: Annotated[str | None, Header()] = None,
    ) -> Response:
        """读取一个节点及冻结 taxonomy dataVersion 中的根到父级闭包。"""
        publication = _publication_or_problem(
            repository, capability=_TAXONOMY_CAPABILITY, snapshot_date=snapshot_date
        )
        node = repository.get_node(
            snapshot_date=publication.snapshot_date,
            code=sector_code,
        )
        if node is None:
            raise SwSectorApiProblem(
                status=404,
                code="sw-industry-not-found",
                detail="SW industry is not present in the published taxonomy",
            )
        ancestors = repository.list_ancestors(
            data_version=publication.data_version,
            snapshot_date=publication.snapshot_date,
            descendant_code=sector_code,
        )
        body = {
            "scheme": "sw.industry",
            "release": _release(publication),
            "industry": _node_resource(node),
            "ancestors": [_node_resource(ancestor) for ancestor in ancestors],
        }
        return _conditional_response(
            request=request,
            if_none_match=if_none_match,
            data_version=publication.data_version,
            representation=("sw-node-detail", sector_code),
            body=body,
        )


def _publication_or_problem(
    repository: SwSectorRepository,
    *,
    capability: SwCapability,
    snapshot_date: date | None,
) -> SwPublication:
    """只返回精确已发布快照，不读取未发布、隔离或半成品 revision。"""
    publication = repository.get_publication(
        capability=capability,
        snapshot_date=snapshot_date,
    )
    if publication is None:
        raise SwSectorApiProblem(
            status=503,
            code="sw-publication-unavailable",
            detail="SW industry publication is not available",
        )
    return publication


def _release(publication: SwPublication) -> dict[str, object]:
    """投影消费者版本、日期、质量和方法学血缘。"""
    return {
        "snapshotDate": publication.snapshot_date.isoformat(),
        "dataVersion": str(publication.data_version),
        "publishedAt": _timestamp(publication.published_at),
        "qualityStatus": publication.quality_status,
        "rowCount": publication.row_count,
        "methodology": {
            "code": publication.methodology.code,
            "version": publication.methodology.version,
            "status": publication.methodology.status,
            "upstreamSource": publication.methodology.upstream_source,
            "semanticSpecSha256": publication.methodology.semantic_spec_sha256,
        },
    }


def _node_resource(value: SwStoredNode) -> dict[str, object]:
    """把节点修订投影为不含数据库键的中立 API 资源。"""
    return {
        "code": value.node.code,
        "name": value.node.name,
        "level": value.node.level.value,
        "parentCode": value.node.parent_code,
        "componentCount": value.node.component_count,
        "revision": value.revision,
    }


def _valuation_resource(value: SwStoredValuation) -> dict[str, object]:
    """把估值观察投影为十进制字符串，并明确一比一股息率单位。"""
    return {
        "code": value.node.code,
        "name": value.node.name,
        "level": value.node.level.value,
        "parentCode": value.node.parent_code,
        "componentCount": value.node.component_count,
        "snapshotDate": value.valuation.snapshot_date.isoformat(),
        "staticPe": _decimal_text(value.valuation.static_pe),
        "ttmPe": _decimal_text(value.valuation.ttm_pe),
        "pb": _decimal_text(value.valuation.pb),
        "dividendYieldRatio": _decimal_text(value.valuation.dividend_yield_ratio),
        "finality": "PROVIDER_OBSERVATION",
        "valuationRevision": value.revision,
    }


def _node_cursor_or_problem(
    cursor: str | None,
    *,
    publication: SwPublication,
    level: int | None,
    parent_code: str | None,
    secret: bytes,
) -> tuple[int | None, str | None]:
    """验证 taxonomy 游标的版本、日期与筛选范围并返回最后排序键。"""
    if cursor is None:
        return None, None
    payload = _decode_cursor(cursor, secret)
    expected = {
        "kind": "sw-node",
        "dataVersion": str(publication.data_version),
        "snapshotDate": publication.snapshot_date.isoformat(),
        "level": level,
        "parentCode": parent_code,
    }
    if any(payload.get(key) != value for key, value in expected.items()):
        raise _cursor_problem()
    after_level = payload.get("afterLevel")
    after_code = payload.get("afterCode")
    if (
        not isinstance(after_level, int)
        or after_level not in {1, 2, 3}
        or not isinstance(after_code, str)
    ):
        raise _cursor_problem()
    return after_level, after_code


def _valuation_cursor_or_problem(
    cursor: str | None,
    *,
    publication: SwPublication,
    level: int | None,
    secret: bytes,
) -> str | None:
    """验证估值游标的发布版本和层级筛选并返回最后代码。"""
    if cursor is None:
        return None
    payload = _decode_cursor(cursor, secret)
    expected = {
        "kind": "sw-valuation",
        "dataVersion": str(publication.data_version),
        "snapshotDate": publication.snapshot_date.isoformat(),
        "level": level,
    }
    if any(payload.get(key) != value for key, value in expected.items()):
        raise _cursor_problem()
    after_code = payload.get("afterCode")
    if not isinstance(after_code, str):
        raise _cursor_problem()
    return after_code


def _encode_cursor(payload: dict[str, object], secret: bytes) -> str:
    """生成经 HMAC-SHA256 签名且不包含秘密的不透明游标。"""
    encoded_payload = base64.urlsafe_b64encode(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).rstrip(b"=")
    signature = base64.urlsafe_b64encode(
        hmac.new(secret, encoded_payload, hashlib.sha256).digest()
    ).rstrip(b"=")
    return f"{encoded_payload.decode()}.{signature.decode()}"


def _decode_cursor(cursor: str, secret: bytes) -> dict[str, Any]:
    """验签并解码游标，篡改、超长或非对象载荷统一拒绝。"""
    try:
        payload_part, signature_part = cursor.split(".", maxsplit=1)
        encoded_payload = payload_part.encode()
        expected = base64.urlsafe_b64encode(
            hmac.new(secret, encoded_payload, hashlib.sha256).digest()
        ).rstrip(b"=")
        if not hmac.compare_digest(expected, signature_part.encode()):
            raise ValueError("signature mismatch")
        padding = b"=" * (-len(encoded_payload) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(encoded_payload + padding))
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as error:
        raise _cursor_problem() from error
    if not isinstance(decoded, dict):
        raise _cursor_problem()
    return decoded


def _cursor_problem() -> SwSectorApiProblem:
    """构造游标与请求或当前发布不一致的稳定冲突响应。"""
    return SwSectorApiProblem(
        status=409,
        code="cursor-mismatch",
        detail="SW cursor does not match the request or current publication",
    )


def _conditional_response(
    *,
    request: Request,
    if_none_match: str | None,
    data_version: UUID,
    representation: tuple[object, ...],
    body: dict[str, object],
) -> Response:
    """返回带 ETag 的 JSON；内部 GET 命中条件请求时使用标准 304。"""
    etag = (
        '"'
        + hashlib.sha256(
            json.dumps(
                [str(data_version), *representation],
                ensure_ascii=False,
                default=str,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        + '"'
    )
    request_id = _request_id(request)
    headers = {
        "ETag": etag,
        "Cache-Control": "private, max-age=0, must-revalidate",
        "X-Data-Version": str(data_version),
        "X-Request-Id": request_id,
    }
    if if_none_match is not None and hmac.compare_digest(if_none_match, etag):
        return Response(status_code=304, headers=headers)
    return JSONResponse(content=body, headers=headers)


def _decimal_text(value: Decimal | None) -> str | None:
    """把可空精确小数序列化为不丢精度的 JSON 字符串。"""
    return None if value is None else str(value)


def _request_id(request: Request) -> str:
    """复用有界调用链标识，缺失或超长时生成不含业务信息的新值。"""
    candidate = request.headers.get("x-request-id")
    return (
        candidate if candidate is not None and 0 < len(candidate) <= 128 else secrets.token_hex(16)
    )


def _timestamp(value: datetime) -> str:
    """把带时区时间标准化为 UTC RFC 3339 文本。"""
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
