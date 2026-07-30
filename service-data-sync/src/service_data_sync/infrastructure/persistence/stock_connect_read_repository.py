"""互联互通中心内部合同只读投影。

所有查询只读取不可变 `StockConnectBundlePublication` 或已持久化的
`StockConnectOverviewPublication`；不会从暂存表、最新 revision 或第三方源临时补装响应。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.database.models.market import (
    StockConnectBundlePublication,
    StockConnectOverviewPublication,
)
from service_data_sync.infrastructure.persistence.stock_connect_readiness_repository import (
    SqlAlchemyStockConnectReadinessRepository,
)


class StockConnectPublicationNotReady(LookupError):
    """表示所选范围没有任何已持久化 bundle publication。"""


class StockConnectExactDateNotPublished(LookupError):
    """表示请求精确业务日尚未形成所选通道 publication。"""


class StockConnectCursorInvalid(ValueError):
    """表示游标签名、编码或结构无效。"""


class StockConnectCursorVersionMismatch(ValueError):
    """表示有效游标绑定了不同 publication 或筛选。"""


class StockConnectSecurityContextNotFound(LookupError):
    """表示已发布来源活跃榜中没有该稳定工具引用。"""


class StockConnectParentPublicationMismatch(ValueError):
    """表示活跃榜请求的父 publication 不存在或与日期、通道不一致。"""


@dataclass(frozen=True, slots=True)
class StockConnectReadResult:
    """返回响应正文和必须与 publication 完全一致的数据版本。"""

    body: dict[str, object]
    data_version: str


@dataclass(frozen=True, slots=True)
class _ResolvedActiveParent:
    """固定活跃榜组件和响应 publication，避免 latest 二次解析产生 TOCTOU。"""

    bundle: StockConnectBundlePublication
    publication: dict[str, object]
    data_version: str
    trade_date: date
    date_resolution: str


class SqlAlchemyStockConnectReadRepository:
    """实现四条只读查询及带 HMAC 的活跃榜游标。"""

    def __init__(self, database: DatabaseClient, *, cursor_secret: bytes) -> None:
        """保存数据库和独立游标签名密钥，拒绝低熵或空密钥。"""
        if len(cursor_secret) < 32:
            raise ValueError("stock-connect cursor secret must contain at least 32 bytes")
        self._database = database
        self._cursor_secret = cursor_secret
        self._readiness = SqlAlchemyStockConnectReadinessRepository(database)

    def readiness(
        self,
        *,
        mode: str,
        exact_date: date | None,
        channels: Sequence[str],
    ) -> StockConnectReadResult:
        """读取独立 readiness 表示，不把执行状态混入业务 bundle publication。"""
        result = self._readiness.query(
            mode=mode,
            exact_date=exact_date,
            selected_channels=channels,
        )
        return StockConnectReadResult(
            body=result.body,
            data_version=result.data_version,
        )

    def overview(
        self,
        *,
        mode: str,
        exact_date: date | None,
        channels: Sequence[str],
        trend_trading_days: int,
    ) -> StockConnectReadResult:
        """读取所选通道共同 publication 与逐点版本化趋势。"""
        channel_set = ",".join(sorted(channels))
        with self._database.session() as session:
            overview = _resolve_overview(
                session,
                mode=mode,
                exact_date=exact_date,
                channel_set=channel_set,
            )
            bundles = _overview_bundles(session, overview)
            trend = _overview_trend(
                session,
                channels=tuple(sorted(channels)),
                channel_set=channel_set,
                resolved_date=overview.trade_date,
                limit=trend_trading_days,
            )
            body = {
                "resolvedTradeDate": overview.trade_date.isoformat(),
                "dateResolution": "LATEST_COMMON" if mode == "LATEST" else "EXACT",
                "channels": [dict(bundles[channel].summary_json) for channel in sorted(channels)],
                "trend": trend,
                "publication": _overview_publication(overview),
            }
            return StockConnectReadResult(body=body, data_version=overview.data_version)

    def channel(
        self,
        *,
        mode: str,
        exact_date: date | None,
        channel: str,
        trend_trading_days: int,
    ) -> StockConnectReadResult:
        """读取单通道已发布摘要和带每点数据版本的历史趋势。"""
        with self._database.session() as session:
            bundle = _resolve_bundle(
                session,
                mode=mode,
                exact_date=exact_date,
                channel=channel,
            )
            body = {
                "resolvedTradeDate": bundle.trade_date.isoformat(),
                "dateResolution": "LATEST_CHANNEL" if mode == "LATEST" else "EXACT",
                "channel": dict(bundle.summary_json),
                "trend": _trend(
                    session,
                    channel=channel,
                    resolved_date=bundle.trade_date,
                    limit=trend_trading_days,
                ),
                "publication": _bundle_publication(bundle),
            }
            return StockConnectReadResult(body=body, data_version=bundle.data_version)

    def active_securities(
        self,
        *,
        mode: str,
        exact_date: date | None,
        channel: str,
        ranking: str,
        cursor: str | None,
        limit: int,
        parent_publication_data_version: str,
    ) -> StockConnectReadResult:
        """只按父 publication 固定组件读取榜单，禁止二次 latest 解析。"""
        with self._database.session() as session:
            parent = _resolve_active_parent(
                session,
                parent_data_version=parent_publication_data_version,
                mode=mode,
                exact_date=exact_date,
                channel=channel,
            )
            offset = (
                0
                if cursor is None
                else self._decode_cursor(
                    cursor,
                    data_version=parent.data_version,
                    channel=channel,
                    ranking=ranking,
                    limit=limit,
                )
            )
            items, availability = _ranked_items(
                parent.bundle.active_securities_json,
                ranking=ranking,
            )
            if availability != "DERIVED" and ranking != "SOURCE_ACTIVE":
                page: list[dict[str, object]] = []
                next_cursor = None
            else:
                page = items[offset : offset + limit]
                next_offset = offset + len(page)
                next_cursor = (
                    None
                    if next_offset >= len(items)
                    else self._encode_cursor(
                        data_version=parent.data_version,
                        channel=channel,
                        ranking=ranking,
                        limit=limit,
                        offset=next_offset,
                    )
                )
            body = {
                "resolvedTradeDate": parent.trade_date.isoformat(),
                "dateResolution": parent.date_resolution,
                "channel": channel,
                "ranking": ranking,
                "rankingAvailability": availability,
                "rankingScope": "SOURCE_ACTIVE_SECURITIES_ONLY",
                "items": page,
                "nextCursor": next_cursor,
                "publication": parent.publication,
            }
            return StockConnectReadResult(
                body=body,
                data_version=parent.data_version,
            )

    def security_context(
        self,
        *,
        instrument_entity_ref: str,
        mode: str,
        exact_date: date | None,
        channel: str | None,
        history_trading_days: int,
    ) -> StockConnectReadResult:
        """读取稳定工具在已发布来源活跃榜中的历史，不扩展为完整港股行情。"""
        with self._database.session() as session:
            if channel is None:
                overview = _resolve_overview(
                    session,
                    mode=mode,
                    exact_date=exact_date,
                    channel_set=("SH_NORTHBOUND,SH_SOUTHBOUND,SZ_NORTHBOUND,SZ_SOUTHBOUND"),
                )
                resolved_date = overview.trade_date
                publication = _overview_publication(overview)
                data_version = overview.data_version
                historical_rows = (
                    session.execute(
                        select(StockConnectOverviewPublication)
                        .where(
                            StockConnectOverviewPublication.channel_set == overview.channel_set,
                            StockConnectOverviewPublication.trade_date <= resolved_date,
                            StockConnectOverviewPublication.superseded_at.is_(None),
                        )
                        .order_by(StockConnectOverviewPublication.trade_date.desc())
                        .limit(history_trading_days)
                    )
                    .scalars()
                    .all()
                )
                historical_bundles = [
                    (component, historical.data_version)
                    for historical in historical_rows
                    for _channel, component in sorted(
                        _overview_bundles(session, historical).items()
                    )
                ]
            else:
                bundle = _resolve_bundle(
                    session,
                    mode=mode,
                    exact_date=exact_date,
                    channel=channel,
                )
                resolved_date = bundle.trade_date
                publication = _bundle_publication(bundle)
                data_version = bundle.data_version
                route, direction = _split_channel(channel)
                bundles = (
                    session.execute(
                        select(StockConnectBundlePublication)
                        .where(
                            StockConnectBundlePublication.channel == route,
                            StockConnectBundlePublication.direction == direction,
                            StockConnectBundlePublication.trade_date <= resolved_date,
                            StockConnectBundlePublication.superseded_at.is_(None),
                        )
                        .order_by(StockConnectBundlePublication.trade_date.desc())
                        .limit(history_trading_days)
                    )
                    .scalars()
                    .all()
                )
                historical_bundles = [
                    (historical, historical.data_version) for historical in bundles
                ]
            activities: list[dict[str, object]] = []
            identity: dict[str, object] | None = None
            for historical, historical_data_version in historical_bundles:
                for raw_item in historical.active_securities_json:
                    item = dict(raw_item)
                    raw_identity = item.get("identity")
                    if not isinstance(raw_identity, dict):
                        continue
                    if raw_identity.get("instrumentEntityRef") != instrument_entity_ref:
                        continue
                    if identity is None:
                        # 查询按日期倒序，标题身份固定为窗口内最新 publication 的名称版本。
                        identity = dict(raw_identity)
                    activities.append(
                        {
                            "dataVersion": historical_data_version,
                            "channel": _channel_code(historical),
                            "tradeDate": historical.trade_date.isoformat(),
                            "sourceRank": item["sourceRank"],
                            "turnoverAmount": item["turnoverAmount"],
                            "netBuyAmount": item["netBuyAmount"],
                        }
                    )
            if identity is None:
                raise StockConnectSecurityContextNotFound(
                    "stock-connect security context is not published"
                )
            body = {
                "resolvedTradeDate": resolved_date.isoformat(),
                "identity": identity,
                "activities": activities[:1000],
                "publication": publication,
            }
            return StockConnectReadResult(body=body, data_version=data_version)

    def _encode_cursor(
        self,
        *,
        data_version: str,
        channel: str,
        ranking: str,
        limit: int,
        offset: int,
    ) -> str:
        """签发绑定完整筛选和 publication 的 URL-safe HMAC 游标。"""
        payload = json.dumps(
            {
                "version": data_version,
                "channel": channel,
                "ranking": ranking,
                "limit": limit,
                "offset": offset,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        signature = hmac.new(self._cursor_secret, payload, hashlib.sha256).digest()
        return (
            base64.urlsafe_b64encode(payload).rstrip(b"=").decode()
            + "."
            + base64.urlsafe_b64encode(signature).rstrip(b"=").decode()
        )

    def _decode_cursor(
        self,
        cursor: str,
        *,
        data_version: str,
        channel: str,
        ranking: str,
        limit: int,
    ) -> int:
        """恒时验签并区分损坏游标与有效但绑定条件不匹配的游标。"""
        try:
            payload_part, signature_part = cursor.split(".", maxsplit=1)
            payload = _urlsafe_decode(payload_part)
            supplied_signature = _urlsafe_decode(signature_part)
            expected_signature = hmac.new(self._cursor_secret, payload, hashlib.sha256).digest()
            if not hmac.compare_digest(supplied_signature, expected_signature):
                raise StockConnectCursorInvalid("stock-connect cursor signature is invalid")
            decoded = json.loads(payload)
        except StockConnectCursorInvalid:
            raise
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as error:
            raise StockConnectCursorInvalid("stock-connect cursor is malformed") from error
        if not isinstance(decoded, dict) or set(decoded) != {
            "version",
            "channel",
            "ranking",
            "limit",
            "offset",
        }:
            raise StockConnectCursorInvalid("stock-connect cursor payload is invalid")
        if (
            decoded.get("version") != data_version
            or decoded.get("channel") != channel
            or decoded.get("ranking") != ranking
            or decoded.get("limit") != limit
        ):
            raise StockConnectCursorVersionMismatch(
                "stock-connect cursor does not match publication or filters"
            )
        offset = decoded.get("offset")
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            raise StockConnectCursorInvalid("stock-connect cursor offset is invalid")
        return offset


def _resolve_overview(
    session: Session,
    *,
    mode: str,
    exact_date: date | None,
    channel_set: str,
) -> StockConnectOverviewPublication:
    """解析所选通道集合已持久化的 latest common 或 exact 总览。"""
    statement = select(StockConnectOverviewPublication).where(
        StockConnectOverviewPublication.channel_set == channel_set,
        StockConnectOverviewPublication.superseded_at.is_(None),
    )
    if mode == "EXACT":
        statement = statement.where(StockConnectOverviewPublication.trade_date == exact_date)
    else:
        statement = statement.order_by(
            StockConnectOverviewPublication.trade_date.desc(),
            StockConnectOverviewPublication.published_at.desc(),
        ).limit(1)
    row = session.execute(statement).scalar_one_or_none()
    if row is None:
        if mode == "EXACT":
            raise StockConnectExactDateNotPublished(
                "requested stock-connect exact date is not published"
            )
        raise StockConnectPublicationNotReady("stock-connect overview is not published")
    return row


def _resolve_bundle(
    session: Session,
    *,
    mode: str,
    exact_date: date | None,
    channel: str,
) -> StockConnectBundlePublication:
    """解析单通道已持久化 latest 或 exact bundle。"""
    route, direction = _split_channel(channel)
    statement = select(StockConnectBundlePublication).where(
        StockConnectBundlePublication.channel == route,
        StockConnectBundlePublication.direction == direction,
        StockConnectBundlePublication.superseded_at.is_(None),
    )
    if mode == "EXACT":
        statement = statement.where(StockConnectBundlePublication.trade_date == exact_date)
    else:
        statement = statement.order_by(
            StockConnectBundlePublication.trade_date.desc(),
            StockConnectBundlePublication.published_at.desc(),
        ).limit(1)
    row = session.execute(statement).scalar_one_or_none()
    if row is None:
        if mode == "EXACT":
            raise StockConnectExactDateNotPublished(
                "requested stock-connect exact date is not published"
            )
        raise StockConnectPublicationNotReady("stock-connect channel is not published")
    return row


def _resolve_active_parent(
    session: Session,
    *,
    parent_data_version: str,
    mode: str,
    exact_date: date | None,
    channel: str,
) -> _ResolvedActiveParent:
    """按显式父版本解析固定组件；不存在、歧义或筛选不匹配统一返回 409 语义。"""
    overview = session.execute(
        select(StockConnectOverviewPublication).where(
            StockConnectOverviewPublication.data_version == parent_data_version
        )
    ).scalar_one_or_none()
    bundle = session.execute(
        select(StockConnectBundlePublication).where(
            StockConnectBundlePublication.data_version == parent_data_version
        )
    ).scalar_one_or_none()
    if (overview is None) == (bundle is None):
        raise StockConnectParentPublicationMismatch(
            "parent stock-connect publication is unavailable or ambiguous"
        )
    if mode not in {"LATEST", "EXACT"}:
        raise StockConnectParentPublicationMismatch(
            "parent stock-connect publication mode is invalid"
        )
    if overview is not None:
        if mode == "EXACT" and exact_date != overview.trade_date:
            raise StockConnectParentPublicationMismatch(
                "parent stock-connect publication does not match exact date"
            )
        try:
            selected = _overview_bundles(session, overview)[channel]
        except (KeyError, TypeError, ValueError) as error:
            raise StockConnectParentPublicationMismatch(
                "parent stock-connect overview does not contain requested channel"
            ) from error
        return _ResolvedActiveParent(
            bundle=selected,
            publication=_overview_publication(overview),
            data_version=overview.data_version,
            trade_date=overview.trade_date,
            date_resolution="LATEST_COMMON" if mode == "LATEST" else "EXACT",
        )
    assert bundle is not None
    if _channel_code(bundle) != channel or (mode == "EXACT" and exact_date != bundle.trade_date):
        raise StockConnectParentPublicationMismatch(
            "parent stock-connect channel publication does not match request"
        )
    return _ResolvedActiveParent(
        bundle=bundle,
        publication=_bundle_publication(bundle),
        data_version=bundle.data_version,
        trade_date=bundle.trade_date,
        date_resolution="LATEST_CHANNEL" if mode == "LATEST" else "EXACT",
    )


def _overview_bundles(
    session: Session, overview: StockConnectOverviewPublication
) -> dict[str, StockConnectBundlePublication]:
    """按 overview 固定的 bundle UUID 读取组件，缺一即判 publication 损坏。"""
    component_ids = {
        channel: UUID(value) for channel, value in overview.component_bundle_ids.items()
    }
    rows = (
        session.execute(
            select(StockConnectBundlePublication).where(
                StockConnectBundlePublication.bundle_release_id.in_(tuple(component_ids.values()))
            )
        )
        .scalars()
        .all()
    )
    by_id = {UUID(str(row.bundle_release_id)): row for row in rows}
    if set(by_id) != set(component_ids.values()):
        raise StockConnectPublicationNotReady("stock-connect overview component is unavailable")
    resolved = {channel: by_id[bundle_id] for channel, bundle_id in component_ids.items()}
    if any(
        _channel_code(bundle) != channel or bundle.trade_date != overview.trade_date
        for channel, bundle in resolved.items()
    ):
        raise StockConnectPublicationNotReady(
            "stock-connect overview component identity is inconsistent"
        )
    return resolved


def _overview_trend(
    session: Session,
    *,
    channels: tuple[str, ...],
    channel_set: str,
    resolved_date: date,
    limit: int,
) -> list[dict[str, object]]:
    """只取同一通道集合已形成的历史总览，每日所有点共用总览 dataVersion。"""
    overviews = (
        session.execute(
            select(StockConnectOverviewPublication)
            .where(
                StockConnectOverviewPublication.channel_set == channel_set,
                StockConnectOverviewPublication.trade_date <= resolved_date,
                StockConnectOverviewPublication.superseded_at.is_(None),
            )
            .order_by(StockConnectOverviewPublication.trade_date.desc())
            .limit(limit)
        )
        .scalars()
        .all()
    )
    points: list[dict[str, object]] = []
    for overview in reversed(overviews):
        bundles = _overview_bundles(session, overview)
        for channel in channels:
            bundle = bundles.get(channel)
            if bundle is None:
                raise StockConnectPublicationNotReady(
                    "stock-connect overview trend component is unavailable"
                )
            points.append(
                {
                    "dataVersion": overview.data_version,
                    "channel": channel,
                    "tradeDate": overview.trade_date.isoformat(),
                    "stats": dict(bundle.summary_json)["stats"],
                    "status": dict(bundle.summary_json)["status"],
                }
            )
    return points


def _trend(
    session: Session,
    *,
    channel: str,
    resolved_date: date,
    limit: int,
) -> list[dict[str, object]]:
    """读取通道逐日完整包并为每个趋势点携带其自身 dataVersion。"""
    route, direction = _split_channel(channel)
    rows = (
        session.execute(
            select(StockConnectBundlePublication)
            .where(
                StockConnectBundlePublication.channel == route,
                StockConnectBundlePublication.direction == direction,
                StockConnectBundlePublication.trade_date <= resolved_date,
                StockConnectBundlePublication.superseded_at.is_(None),
            )
            .order_by(StockConnectBundlePublication.trade_date.desc())
            .limit(limit)
        )
        .scalars()
        .all()
    )
    return [
        {
            "dataVersion": row.data_version,
            "channel": channel,
            "tradeDate": row.trade_date.isoformat(),
            "stats": dict(row.summary_json)["stats"],
            "status": dict(row.summary_json)["status"],
        }
        for row in reversed(rows)
    ]


def _ranked_items(
    raw_items: Sequence[Mapping[str, Any]], *, ranking: str
) -> tuple[list[dict[str, object]], str]:
    """按来源名次或可用派生净额排序，同额稳定回落来源名次和代码。"""
    items = [dict(item) for item in raw_items]
    if ranking == "SOURCE_ACTIVE":
        ordered = sorted(
            items,
            key=lambda item: (
                int(item["sourceRank"]),
                str(dict(item["identity"])["sourceSecurityCode"]),
            ),
        )
        return [_ranking_rank(item, rank) for rank, item in enumerate(ordered, 1)], "REPORTED"
    available = [
        item
        for item in items
        if dict(item["netBuyAmount"]).get("availability") == "DERIVED"
        and isinstance(dict(item["netBuyAmount"]).get("value"), dict)
    ]
    if not available:
        states = {str(dict(item["netBuyAmount"]).get("availability")) for item in items}
        unavailable = (
            "NOT_DISCLOSED_BY_REGIME"
            if "NOT_DISCLOSED_BY_REGIME" in states
            else "SOURCE_MISSING"
            if "SOURCE_MISSING" in states
            else "NOT_APPLICABLE"
        )
        return [], unavailable
    available = [
        item
        for item in available
        if (_net_amount(item) > 0 if ranking == "NET_BUY" else _net_amount(item) < 0)
    ]
    if not available:
        return [], "DERIVED"

    def sort_key(item: Mapping[str, object]) -> tuple[Decimal, int, str]:
        """按派生净额和来源稳定键生成确定性排序。"""
        net = _net_amount(item)
        primary = -net if ranking == "NET_BUY" else net
        source_rank = item.get("sourceRank")
        identity = item.get("identity")
        source_code = identity.get("sourceSecurityCode") if isinstance(identity, Mapping) else None
        if (
            isinstance(source_rank, bool)
            or not isinstance(source_rank, int)
            or not isinstance(source_code, str)
        ):
            raise ValueError("stock-connect ranking identity is invalid")
        return (
            primary,
            source_rank,
            source_code,
        )

    ordered = sorted(available, key=sort_key)
    return [_ranking_rank(item, rank) for rank, item in enumerate(ordered, 1)], "DERIVED"


def _net_amount(item: Mapping[str, object]) -> Decimal:
    """读取已经过 DERIVED 可用性筛选的净额基础单位字符串。"""
    fact = item.get("netBuyAmount")
    if not isinstance(fact, Mapping):
        raise ValueError("stock-connect net fact is invalid")
    value = fact.get("value")
    if not isinstance(value, Mapping):
        raise ValueError("stock-connect derived net value is invalid")
    amount = value.get("amount")
    if not isinstance(amount, str):
        raise ValueError("stock-connect derived net amount is invalid")
    return Decimal(amount)


def _ranking_rank(item: Mapping[str, object], rank: int) -> dict[str, object]:
    """写入当前筛选的确定性名次，同时保留 HKEX 原始 sourceRank。"""
    result = dict(item)
    result["rankingRank"] = rank
    return result


def _bundle_publication(bundle: StockConnectBundlePublication) -> dict[str, object]:
    """投影单通道不可变 publication。"""
    return {
        "bundleReleaseId": str(bundle.bundle_release_id),
        "dataVersion": bundle.data_version,
        "tradeDate": bundle.trade_date.isoformat(),
        "publishedAt": bundle.published_at.isoformat().replace("+00:00", "Z"),
        "qualityStatus": bundle.quality_status,
        "qualityIssues": bundle.quality_issues,
        "sourceRefs": bundle.source_refs,
    }


def _overview_publication(
    overview: StockConnectOverviewPublication,
) -> dict[str, object]:
    """投影所选通道集合的持久化 overview publication。"""
    return {
        "bundleReleaseId": str(overview.overview_release_id),
        "dataVersion": overview.data_version,
        "tradeDate": overview.trade_date.isoformat(),
        "publishedAt": overview.published_at.isoformat().replace("+00:00", "Z"),
        "qualityStatus": overview.quality_status,
        "qualityIssues": overview.quality_issues,
        "sourceRefs": overview.source_refs,
    }


def _split_channel(channel: str) -> tuple[str, str]:
    """拆分合同通道枚举，禁止由自由文本猜方向。"""
    mapping = {
        "SH_NORTHBOUND": ("SH", "NORTHBOUND"),
        "SZ_NORTHBOUND": ("SZ", "NORTHBOUND"),
        "SH_SOUTHBOUND": ("SH", "SOUTHBOUND"),
        "SZ_SOUTHBOUND": ("SZ", "SOUTHBOUND"),
    }
    try:
        return mapping[channel]
    except KeyError as error:
        raise ValueError("stock-connect channel is invalid") from error


def _channel_code(bundle: StockConnectBundlePublication) -> str:
    """将数据库通道和方向组合为合同枚举。"""
    return f"{bundle.channel}_{bundle.direction}"


def _urlsafe_decode(value: str) -> bytes:
    """解码无填充 URL-safe Base64，并拒绝空段。"""
    if not value:
        raise ValueError("cursor segment is empty")
    return base64.b64decode(
        value + "=" * (-len(value) % 4),
        altchars=b"-_",
        validate=True,
    )
