"""沪深港通 `P0` 官方通道日终统计同步。

每个通道和方向的统计独立发布，金额缺失会连同披露可用性状态保留，不能从持仓、排行或另一方向推算。
成功与失败路径共用同一留存授权门禁；默认只保存标准事实、来源摘要和失败证据清单。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from uuid import UUID

from service_data_sync.application.ports.data_source import (
    DataSourcePort,
    ProviderBatch,
    ProviderError,
    ProviderErrorCode,
    SourceRequest,
)
from service_data_sync.application.ports.market_data import RawPayloadStore
from service_data_sync.application.ports.stock_connect import (
    StockConnectMarketDailyRepository,
    StockConnectSourceObservation,
)
from service_data_sync.domain.stock_connect import StockConnectChannel, StockConnectMarketDaily

_CAPABILITY = "market.stock_connect.market_stat.reported"
_SCHEMA = "quant-v2.stock-connect-market-daily.v1"


@dataclass(frozen=True, slots=True)
class StockConnectMarketDailySyncResult:
    """返回一个官方通道方向发布版本；制度性无记录时返回成功空结果。"""

    channel: StockConnectChannel
    data_version: UUID | None
    inserted_count: int
    unchanged_count: int
    availability: str = "available"


class StockConnectMarketDailySyncService:
    """同步官方通道统计，制度性未披露不触发估算值或其他来源回填。"""

    def __init__(
        self,
        *,
        source: DataSourcePort,
        repository: StockConnectMarketDailyRepository,
        raw_payload_store: RawPayloadStore,
    ) -> None:
        """接收来源无关 adapter、通道统计仓储及统一授权证据端口。"""
        self._source = source
        self._repository = repository
        self._raw_payload_store = raw_payload_store

    async def sync(
        self, *, channel: StockConnectChannel, start: date, end: date
    ) -> StockConnectMarketDailySyncResult:
        """抓取有界通道方向窗口；能力缺失、倒置日期或 schema 漂移一律 fail-closed。"""
        if start > end:
            raise ValueError("start must not be after end")
        if _CAPABILITY not in self._source.capabilities():
            raise ProviderError(
                ProviderErrorCode.INVALID_REQUEST,
                "unsupported stock-connect market capability",
                retryable=False,
            )
        batch = await self._source.fetch(
            SourceRequest(
                capability=_CAPABILITY,
                parameters=(
                    ("channel", channel.channel),
                    ("direction", channel.direction),
                    ("start", start.isoformat()),
                    ("end", end.isoformat()),
                ),
            )
        )
        records = decode_stock_connect_market_daily_batch(batch.payload, channel=channel)
        if not records:
            return StockConnectMarketDailySyncResult(
                channel=channel,
                data_version=None,
                inserted_count=0,
                unchanged_count=0,
                availability="empty",
            )
        published = self._repository.publish_market_daily(
            channel=channel,
            records=records,
            source=_archive_batch(batch=batch, payload_store=self._raw_payload_store),
        )
        return StockConnectMarketDailySyncResult(
            channel=published.channel,
            data_version=published.data_version,
            inserted_count=published.inserted_count,
            unchanged_count=published.unchanged_count,
        )


def decode_stock_connect_market_daily_batch(
    payload: bytes, *, channel: StockConnectChannel
) -> tuple[StockConnectMarketDaily, ...]:
    """解析单通道方向官方统计，拒绝把历史制度断点或估算字段混入标准对象。"""
    try:
        decoded = json.loads(payload)
    except json.JSONDecodeError as error:
        raise _schema_error("stock-connect market payload is not JSON") from error
    if not isinstance(decoded, dict) or decoded.get("schema") != _SCHEMA:
        raise _schema_error("unexpected stock-connect market schema")
    if decoded.get("channel") != channel.channel or decoded.get("direction") != channel.direction:
        raise _schema_error("stock-connect channel identity mismatch")
    if decoded.get("valueKind", "REPORTED") != "REPORTED":
        raise _schema_error("stock-connect P0 accepts only reported values")
    rows = decoded.get("records")
    if not isinstance(rows, list):
        raise _schema_error("stock-connect market payload has no records")
    try:
        records = tuple(_record(row) for row in rows)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise _schema_error("stock-connect market value is invalid") from error
    if len({record.trade_date for record in records}) != len(records):
        raise _schema_error("stock-connect market payload has duplicate trade dates")
    return tuple(sorted(records, key=lambda record: record.trade_date))


def _archive_batch(
    *, batch: ProviderBatch, payload_store: RawPayloadStore
) -> StockConnectSourceObservation:
    """构造不可回放来源摘要；外层统一门禁决定失败时是否允许保留来源字节。"""
    del payload_store
    raw_payload = batch.raw_payload if batch.raw_payload is not None else batch.payload
    raw_digest = hashlib.sha256(raw_payload).hexdigest()
    normalized_digest = hashlib.sha256(batch.payload).hexdigest()
    return StockConnectSourceObservation(
        provider_id=batch.provider_id,
        capability=batch.capability,
        raw_payload_sha256=raw_digest,
        raw_uri=f"digest-only://sha256/{raw_digest}",
        raw_content_type=batch.raw_content_type or batch.content_type,
        raw_byte_size=len(raw_payload),
        normalized_payload_sha256=normalized_digest,
        normalized_uri=f"digest-only://sha256/{normalized_digest}",
        normalized_content_type=batch.content_type,
        normalized_byte_size=len(batch.payload),
        observed_at=batch.observed_at,
        upstream_source=batch.upstream_source or batch.provider_id,
        adapter_version=batch.adapter_version,
        schema_fingerprint=batch.schema_fingerprint
        or hashlib.sha256(batch.capability.encode()).hexdigest(),
    )


def _record(value: object) -> StockConnectMarketDaily:
    """转换单日官方通道统计，未披露字段由 `None` 与 availability 状态共同表达。"""
    if not isinstance(value, dict):
        raise ValueError("stock-connect market row is not an object")
    return StockConnectMarketDaily(
        trade_date=date.fromisoformat(_required(value, "tradeDate")),
        buy_amount=_optional_decimal(value.get("buyAmount")),
        sell_amount=_optional_decimal(value.get("sellAmount")),
        turnover_amount=_optional_decimal(value.get("turnoverAmount")),
        net_buy_amount=_optional_decimal(value.get("netBuyAmount")),
        quota_balance=_optional_decimal(value.get("quotaBalance")),
        currency=_required(value, "currency"),
        availability_status=_required(value, "availabilityStatus"),
        trade_count=_optional_int(value.get("tradeCount")),
        etf_turnover_amount=_optional_decimal(value.get("etfTurnoverAmount")),
        field_availability=_field_availability(value.get("fieldAvailability")),
    )


def _required(value: dict[str, object], key: str) -> str:
    """读取必须出现的字段，缺失日期、币种或披露状态时不可发布该日记录。"""
    normalized = _optional_text(value.get(key))
    if normalized is None:
        raise ValueError(f"{key} is required")
    return normalized


def _optional_decimal(value: object) -> Decimal | None:
    """解析可选精确金额，真实零值与制度性未披露空值绝不互相替换。"""
    normalized = _optional_text(value)
    return None if normalized is None else Decimal(normalized)


def _optional_int(value: object) -> int | None:
    """解析可选非负整数，禁止把来源缺失笔数解释为零。"""
    normalized = _optional_text(value)
    return None if normalized is None else int(normalized)


def _field_availability(value: object) -> tuple[tuple[str, str], ...]:
    """冻结每个字段的可用性状态，避免仅靠空值猜测制度或来源缺失。"""
    if not isinstance(value, dict):
        raise ValueError("fieldAvailability is required")
    return tuple(sorted((str(key), str(status)) for key, status in value.items()))


def _optional_text(value: object) -> str | None:
    """处理 JSON 空值、空白与 pandas 缺失字面量，避免它们进入 canonical 事实。"""
    if value is None:
        return None
    normalized = str(value).strip()
    return None if not normalized or normalized.lower() in {"nan", "none", "nat"} else normalized


def _schema_error(message: str) -> ProviderError:
    """构造不可重试 schema 错误；错误证据仍受同一许可与加密门禁约束。"""
    return ProviderError(ProviderErrorCode.SCHEMA, message, retryable=False)
