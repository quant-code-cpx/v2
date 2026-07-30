"""龙虎榜和大宗交易 `P0` 同步。

同步请求必须是明确的包含端日期窗口。
它会严格按各自 `schema` 检查事件身份、金额恒等、席位或成交序号与可见时间。
无披露是合法空结果；成功仅保存 `canonical` 事实与摘要，解析或发布失败才把来源字节留作私有排障证据。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Protocol
from uuid import UUID

from service_data_sync.application.ports.data_source import (
    DataSourcePort,
    ProviderBatch,
    ProviderError,
    ProviderErrorCode,
    SourceRequest,
)
from service_data_sync.application.ports.market_data import RawPayload, RawPayloadStore
from service_data_sync.application.ports.trading_events import (
    BlockTradeRepository,
    DragonTigerRepository,
    PublishedTradingEvents,
    TradingEventsSourceObservation,
)
from service_data_sync.domain.equity import EquityIdentifier
from service_data_sync.domain.trading_events import BlockTrade, DragonTigerEvent, DragonTigerSeat

_DRAGON_TIGER_CAPABILITY = "market.dragon_tiger.disclosure.1d"
_DRAGON_TIGER_SCHEMA = "quant-v2.dragon-tiger-disclosure.v1"
_BLOCK_TRADE_CAPABILITY = "market.block_trade.execution.1d"
_BLOCK_TRADE_SCHEMA = "quant-v2.block-trade-execution.v1"
_MAX_WINDOW_DAYS = 31


class _TradingPublisher(Protocol):
    """抽象两个交易事实仓储的共同发布返回形状，避免服务层依赖具体持久化实现。"""

    def __call__(
        self, *, records: Sequence[object], source: TradingEventsSourceObservation
    ) -> PublishedTradingEvents:
        """发布一个已通过严格解码的交易事实窗口。"""
        ...


@dataclass(frozen=True, slots=True)
class TradingEventsSyncResult:
    """返回交易披露覆盖版本；无匹配披露时仍返回零记录 publication 的版本。"""

    data_version: UUID
    inserted_count: int
    unchanged_count: int
    excluded_count: int = 0
    availability: str = "available"


class DragonTigerSyncService:
    """同步龙虎榜事件和席位，来源字段白名单在标准 JSON 解码时严格执行。"""

    def __init__(
        self,
        *,
        source: DataSourcePort,
        repository: DragonTigerRepository,
        raw_payload_store: RawPayloadStore,
    ) -> None:
        """接收中立数据源、龙虎榜仓储和失败排障载荷端口。"""
        self._source = source
        self._repository = repository
        self._raw_payload_store = raw_payload_store

    async def sync(
        self,
        *,
        start: date,
        end: date,
        identifier: EquityIdentifier | None = None,
        before_final_publication: Callable[[], None] | None = None,
    ) -> TradingEventsSyncResult:
        """同步最多 31 天龙虎榜窗口，并可按已验证交易所证券身份过滤。"""
        batch = await _fetch_window(
            source=self._source,
            capability=_DRAGON_TIGER_CAPABILITY,
            start=start,
            end=end,
            identifier=identifier,
        )
        events = decode_dragon_tiger_batch(batch.payload)
        if identifier is not None and any(
            event.source_security_code != identifier.symbol for event in events
        ):
            raise _schema_error("dragon tiger payload escaped the requested instrument")
        if any(not start <= event.trade_date <= end for event in events):
            raise _schema_error("dragon tiger payload escaped the requested date window")
        source = _archive_batch(batch=batch, payload_store=self._raw_payload_store)
        if before_final_publication is not None:
            before_final_publication()
        published = self._repository.publish_dragon_tiger(
            events=events,
            source=source,
            start=start,
            end=end,
            identifier=identifier,
        )
        return _result(published, empty=not events)


class BlockTradeSyncService:
    """同步大宗交易逐笔记录，显式 occurrence 保留相同经济字段的合法多笔成交。"""

    def __init__(
        self,
        *,
        source: DataSourcePort,
        repository: BlockTradeRepository,
        raw_payload_store: RawPayloadStore,
    ) -> None:
        """接收中立数据源、大宗交易仓储和失败排障载荷端口。"""
        self._source = source
        self._repository = repository
        self._raw_payload_store = raw_payload_store

    async def sync(
        self,
        *,
        start: date,
        end: date,
        identifier: EquityIdentifier | None = None,
        before_final_publication: Callable[[], None] | None = None,
    ) -> TradingEventsSyncResult:
        """同步最多 31 天大宗逐笔窗口，并可按已验证交易所证券身份过滤。"""
        batch = await _fetch_window(
            source=self._source,
            capability=_BLOCK_TRADE_CAPABILITY,
            start=start,
            end=end,
            identifier=identifier,
        )
        trades = decode_block_trade_batch(batch.payload)
        if identifier is not None and any(
            trade.source_security_code != identifier.symbol for trade in trades
        ):
            raise _schema_error("block trade payload escaped the requested instrument")
        if any(not start <= trade.trade_date <= end for trade in trades):
            raise _schema_error("block trade payload escaped the requested date window")
        source = _archive_batch(batch=batch, payload_store=self._raw_payload_store)
        if before_final_publication is not None:
            before_final_publication()
        published = self._repository.publish_block_trades(
            trades=trades,
            source=source,
            start=start,
            end=end,
            identifier=identifier,
        )
        return _result(published, empty=not trades)


async def _fetch_window(
    *,
    source: DataSourcePort,
    capability: str,
    start: date,
    end: date,
    identifier: EquityIdentifier | None,
) -> ProviderBatch:
    """读取受控窗口与可选证券，保持两个 P0 dataset 在 Provider 层完全隔离。"""
    if start > end:
        raise ValueError("start must not be after end")
    if (end - start).days + 1 > _MAX_WINDOW_DAYS:
        raise ValueError("trading events window must not exceed 31 days")
    if capability not in source.capabilities():
        raise ProviderError(
            ProviderErrorCode.INVALID_REQUEST,
            f"unsupported trading disclosure capability: {capability}",
            retryable=False,
        )
    parameters = [("start", start.isoformat()), ("end", end.isoformat())]
    if identifier is not None:
        parameters.append(("instrument", identifier.qualified_symbol))
    return await source.fetch(
        SourceRequest(
            capability=capability,
            parameters=tuple(parameters),
        )
    )


def decode_dragon_tiger_batch(payload: bytes) -> tuple[DragonTigerEvent, ...]:
    """解码龙虎榜标准载荷，拒绝未知字段、重复事件以及无法对账的金额恒等。"""
    decoded = _payload(payload, schema=_DRAGON_TIGER_SCHEMA)
    _reject_unknown(decoded, {"schema", "events"}, "root")
    values = decoded.get("events")
    if not isinstance(values, list):
        raise _schema_error("dragon tiger payload has no events")
    try:
        events = tuple(_dragon_tiger_event(value) for value in values)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise _schema_error("dragon tiger value is invalid") from error
    if len({event.source_event_key for event in events}) != len(events):
        raise _schema_error("dragon tiger payload has duplicate source event keys")
    return tuple(sorted(events, key=lambda item: (item.trade_date, item.source_event_key)))


def decode_block_trade_batch(payload: bytes) -> tuple[BlockTrade, ...]:
    """解码大宗交易标准载荷，保留 occurrence 不同的重复经济成交并拒绝未知列。"""
    decoded = _payload(payload, schema=_BLOCK_TRADE_SCHEMA)
    _reject_unknown(decoded, {"schema", "trades"}, "root")
    values = decoded.get("trades")
    if not isinstance(values, list):
        raise _schema_error("block trade payload has no trades")
    try:
        trades = tuple(_block_trade(value) for value in values)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise _schema_error("block trade value is invalid") from error
    if len({(trade.source_trade_key, trade.occurrence_no) for trade in trades}) != len(trades):
        raise _schema_error("block trade payload has duplicate source keys and occurrences")
    return tuple(
        sorted(
            trades, key=lambda item: (item.trade_date, item.source_trade_key, item.occurrence_no)
        )
    )


def _dragon_tiger_event(value: object) -> DragonTigerEvent:
    """转换一个龙虎榜事件及其席位，来源每个字段都须落在 P0 白名单。"""
    if not isinstance(value, dict):
        raise ValueError("dragon tiger event is not an object")
    _reject_unknown(
        value,
        {
            "sourceEventKey",
            "securityCode",
            "tradeDate",
            "reasonCode",
            "reasonText",
            "closePrice",
            "buyAmount",
            "sellAmount",
            "netAmount",
            "dealAmount",
            "marketTurnoverAmount",
            "dealRatio",
            "netRatio",
            "turnoverRatio",
            "sourcePublishedAt",
            "visibleTimePrecision",
            "visibleAt",
            "seats",
        },
        "dragon tiger event",
    )
    raw_seats = value.get("seats")
    if not isinstance(raw_seats, list):
        raise ValueError("dragon tiger seats must be an array")
    return DragonTigerEvent(
        source_event_key=_required(value, "sourceEventKey"),
        source_security_code=_required(value, "securityCode"),
        trade_date=date.fromisoformat(_required(value, "tradeDate")),
        reason_code=_required(value, "reasonCode"),
        reason_text=_required(value, "reasonText"),
        close_price=_optional_decimal(value.get("closePrice")),
        buy_amount=Decimal(_required(value, "buyAmount")),
        sell_amount=Decimal(_required(value, "sellAmount")),
        net_amount=Decimal(_required(value, "netAmount")),
        deal_amount=Decimal(_required(value, "dealAmount")),
        market_turnover_amount=_optional_decimal(value.get("marketTurnoverAmount")),
        deal_ratio=_optional_decimal(value.get("dealRatio")),
        net_ratio=_optional_decimal(value.get("netRatio")),
        turnover_ratio=_optional_decimal(value.get("turnoverRatio")),
        source_published_at=_optional_datetime(value.get("sourcePublishedAt")),
        visible_time_precision=_required(value, "visibleTimePrecision"),
        visible_at=_datetime(_required(value, "visibleAt")),
        seats=tuple(_seat(item) for item in raw_seats),
    )


def _seat(value: object) -> DragonTigerSeat:
    """转换一行买卖席位，排名只在同一事件同一侧内唯一。"""
    if not isinstance(value, dict):
        raise ValueError("dragon tiger seat is not an object")
    _reject_unknown(
        value,
        {
            "listSide",
            "rank",
            "seatCode",
            "seatName",
            "buyAmount",
            "sellAmount",
            "netAmount",
            "buyRatio",
            "sellRatio",
        },
        "dragon tiger seat",
    )
    return DragonTigerSeat(
        list_side=_required(value, "listSide"),
        rank=int(_required(value, "rank")),
        seat_code=_optional_text(value.get("seatCode")),
        seat_name=_required(value, "seatName"),
        buy_amount=Decimal(_required(value, "buyAmount")),
        sell_amount=Decimal(_required(value, "sellAmount")),
        net_amount=Decimal(_required(value, "netAmount")),
        buy_ratio=_optional_decimal(value.get("buyRatio")),
        sell_ratio=_optional_decimal(value.get("sellRatio")),
    )


def _block_trade(value: object) -> BlockTrade:
    """转换一笔大宗成交，保持来源席位原文且不从其他 dataset 推导折溢价。"""
    if not isinstance(value, dict):
        raise ValueError("block trade is not an object")
    _reject_unknown(
        value,
        {
            "sourceTradeKey",
            "securityCode",
            "tradeDate",
            "occurrenceNo",
            "executionPrice",
            "quantityShares",
            "notionalCny",
            "buyerSeatCode",
            "buyerSeatName",
            "sellerSeatCode",
            "sellerSeatName",
            "referenceClosePrice",
            "premiumDiscountRatio",
            "sourceDailyRank",
            "sourcePublishedAt",
            "visibleTimePrecision",
            "visibleAt",
        },
        "block trade",
    )
    return BlockTrade(
        source_trade_key=_required(value, "sourceTradeKey"),
        source_security_code=_required(value, "securityCode"),
        trade_date=date.fromisoformat(_required(value, "tradeDate")),
        occurrence_no=int(_required(value, "occurrenceNo")),
        execution_price=Decimal(_required(value, "executionPrice")),
        quantity_shares=int(_required(value, "quantityShares")),
        notional_cny=Decimal(_required(value, "notionalCny")),
        buyer_seat_code=_optional_text(value.get("buyerSeatCode")),
        buyer_seat_name=_required(value, "buyerSeatName"),
        seller_seat_code=_optional_text(value.get("sellerSeatCode")),
        seller_seat_name=_required(value, "sellerSeatName"),
        reference_close_price=_optional_decimal(value.get("referenceClosePrice")),
        premium_discount_ratio=_optional_decimal(value.get("premiumDiscountRatio")),
        source_daily_rank=_optional_int(value.get("sourceDailyRank")),
        source_published_at=_optional_datetime(value.get("sourcePublishedAt")),
        visible_time_precision=_required(value, "visibleTimePrecision"),
        visible_at=_datetime(_required(value, "visibleAt")),
    )


def _payload(payload: bytes, *, schema: str) -> dict[str, object]:
    """解析一个指定 schema 的 JSON 对象，防止错误 capability 的载荷混入当前同步链路。"""
    try:
        decoded = json.loads(payload)
    except json.JSONDecodeError as error:
        raise _schema_error("trading events payload is not JSON") from error
    if not isinstance(decoded, dict) or decoded.get("schema") != schema:
        raise _schema_error("unexpected trading events schema")
    return decoded


def _archive_batch(
    *, batch: ProviderBatch, payload_store: RawPayloadStore
) -> TradingEventsSourceObservation:
    """构造交易来源摘要；成功只保留 canonical 事实，失败才归档来源字节。"""
    raw_payload = batch.raw_payload if batch.raw_payload is not None else batch.payload
    raw_digest = hashlib.sha256(raw_payload).hexdigest()
    normalized_digest = hashlib.sha256(batch.payload).hexdigest()
    prefix = f"trading/{batch.capability}/{batch.provider_id}/{batch.observed_at:%Y/%m/%d}"
    raw_uri = payload_store.put(
        RawPayload(
            object_key=f"raw/{prefix}/{raw_digest}.json",
            content_sha256=raw_digest,
            content_type=batch.raw_content_type or batch.content_type,
            payload=raw_payload,
        )
    )
    normalized_uri = payload_store.put(
        RawPayload(
            object_key=f"normalized/{prefix}/{normalized_digest}.json",
            content_sha256=normalized_digest,
            content_type=batch.content_type,
            payload=batch.payload,
        )
    )
    return TradingEventsSourceObservation(
        provider_id=batch.provider_id,
        capability=batch.capability,
        raw_payload_sha256=raw_digest,
        raw_uri=raw_uri,
        raw_content_type=batch.raw_content_type or batch.content_type,
        raw_byte_size=len(raw_payload),
        normalized_payload_sha256=normalized_digest,
        normalized_uri=normalized_uri,
        normalized_content_type=batch.content_type,
        normalized_byte_size=len(batch.payload),
        observed_at=batch.observed_at,
        upstream_source=batch.upstream_source or batch.provider_id,
        adapter_version=batch.adapter_version,
        schema_fingerprint=batch.schema_fingerprint
        or hashlib.sha256(batch.capability.encode()).hexdigest(),
    )


def _result(published: PublishedTradingEvents, *, empty: bool = False) -> TradingEventsSyncResult:
    """将仓储发布结果收敛为服务返回 DTO，避免调用方依赖基础设施实现类型。"""
    del empty
    return TradingEventsSyncResult(
        data_version=published.data_version,
        inserted_count=published.inserted_count,
        unchanged_count=published.unchanged_count,
        excluded_count=published.excluded_count,
        availability=(
            "empty" if published.inserted_count + published.unchanged_count == 0 else "available"
        ),
    )


def _required(value: dict[str, object], key: str) -> str:
    """读取非空文本，拒绝 pandas 缺失字面量成为来源稳定键或席位名称。"""
    normalized = _optional_text(value.get(key))
    if normalized is None:
        raise ValueError(f"{key} is required")
    return normalized


def _optional_text(value: object) -> str | None:
    """标准化可选文本，保持真实空值以免把缺失码误判为可关联身份。"""
    if value is None:
        return None
    normalized = str(value).strip()
    return None if not normalized or normalized.lower() in {"nan", "none", "nat"} else normalized


def _optional_decimal(value: object) -> Decimal | None:
    """将可选数值保留为精确十进制，空值不转换为零或其他估算值。"""
    normalized = _optional_text(value)
    return None if normalized is None else Decimal(normalized)


def _optional_int(value: object) -> int | None:
    """读取可选来源定位排名，拒绝浮点或小数文本造成不稳定排序。"""
    normalized = _optional_text(value)
    return None if normalized is None else int(normalized)


def _datetime(value: str) -> datetime:
    """解析带时区 ISO 时间，禁止用服务器本地时区解释来源披露时刻。"""
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("datetime must include timezone")
    return result


def _optional_datetime(value: object) -> datetime | None:
    """解析可选发布时间；日期级与观测级披露以 `None` 配合精度枚举保存。"""
    normalized = _optional_text(value)
    return None if normalized is None else _datetime(normalized)


def _reject_unknown(value: dict[str, object], allowed: set[str], location: str) -> None:
    """拒绝未治理字段，让供应商新增的未来收益或排行列显式触发 schema 审查。"""
    unknown = set(value).difference(allowed)
    if unknown:
        raise ValueError(f"unexpected {location} fields: {', '.join(sorted(unknown))}")


def _schema_error(message: str) -> ProviderError:
    """统一返回不可重试 schema 错误，避免错误字段通过任务重试反复进入证据桶。"""
    return ProviderError(ProviderErrorCode.SCHEMA, message, retryable=False)
