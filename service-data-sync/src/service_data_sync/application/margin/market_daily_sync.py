"""融资融券 `P0` 场所日汇总同步。

一次同步对应一个交易场所和一个交易日窗口，金额、数量单位和币种按交易所直报语义保存，不能从证券明细反推。
空字段代表未披露而不是零值；成功路径只保留摘要，失败才留下可排障的来源证据。
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
from service_data_sync.application.ports.margin_market import (
    MarginMarketDailyRepository,
    MarginSourceObservation,
)
from service_data_sync.application.ports.market_data import RawPayload, RawPayloadStore
from service_data_sync.domain.margin import MarginMarketDaily, MarginVenue

_CAPABILITY = "market.margin.market.1d.reported"
_SCHEMA = "quant-v2.margin-market-daily.v1"


@dataclass(frozen=True, slots=True)
class MarginMarketDailySyncResult:
    """返回一个场所直报汇总发布版本或成功空结果，未暴露 Provider 专有列或对象 URI。"""

    venue: MarginVenue
    data_version: UUID | None
    inserted_count: int
    unchanged_count: int
    availability: str = "available"


class MarginMarketDailySyncService:
    """同步沪深两融场所汇总，来源空值不触发证券明细查询或公式补齐。"""

    def __init__(
        self,
        *,
        source: DataSourcePort,
        repository: MarginMarketDailyRepository,
        raw_payload_store: RawPayloadStore,
    ) -> None:
        """接收中立 adapter、领域仓储和对象存储端口。"""
        self._source = source
        self._repository = repository
        self._raw_payload_store = raw_payload_store

    async def sync(
        self, *, venue: MarginVenue, start: date, end: date
    ) -> MarginMarketDailySyncResult:
        """抓取一个场所的有界日频窗口，时间倒置和未声明 capability 立即失败。"""
        if start > end:
            raise ValueError("start must not be after end")
        if _CAPABILITY not in self._source.capabilities():
            raise ProviderError(
                ProviderErrorCode.INVALID_REQUEST,
                "unsupported margin market daily capability",
                retryable=False,
            )
        batch = await self._source.fetch(
            SourceRequest(
                capability=_CAPABILITY,
                parameters=(
                    ("venue", venue.code),
                    ("start", start.isoformat()),
                    ("end", end.isoformat()),
                ),
            )
        )
        records = decode_margin_market_daily_batch(batch.payload, venue=venue)
        if not records:
            # 空数组表示该窗口无匹配事实；不生成发布，也不暂存成功来源字节。
            return MarginMarketDailySyncResult(
                venue=venue,
                data_version=None,
                inserted_count=0,
                unchanged_count=0,
                availability="empty",
            )
        published = self._repository.publish_market_daily(
            venue=venue,
            records=records,
            source=_archive_batch(batch=batch, payload_store=self._raw_payload_store),
        )
        return MarginMarketDailySyncResult(
            venue=published.venue,
            data_version=published.data_version,
            inserted_count=published.inserted_count,
            unchanged_count=published.unchanged_count,
        )


def decode_margin_market_daily_batch(
    payload: bytes, *, venue: MarginVenue
) -> tuple[MarginMarketDaily, ...]:
    """解码交易所直报汇总，拒绝 venue 漂移、重复日期和任何派生字段声明。"""
    try:
        decoded = json.loads(payload)
    except json.JSONDecodeError as error:
        raise _schema_error("margin market payload is not JSON") from error
    if not isinstance(decoded, dict) or decoded.get("schema") != _SCHEMA:
        raise _schema_error("unexpected margin market schema")
    if decoded.get("venue") != venue.code:
        raise _schema_error("margin market venue mismatch")
    if decoded.get("valueKind", "REPORTED") != "REPORTED":
        raise _schema_error("margin P0 accepts only reported values")
    rows = decoded.get("records")
    if not isinstance(rows, list):
        raise _schema_error("margin market payload has no records")
    try:
        records = tuple(_record(row) for row in rows)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise _schema_error("margin market value is invalid") from error
    if len({record.trade_date for record in records}) != len(records):
        raise _schema_error("margin market payload has duplicate trade dates")
    return tuple(sorted(records, key=lambda record: record.trade_date))


def _archive_batch(
    *, batch: ProviderBatch, payload_store: RawPayloadStore
) -> MarginSourceObservation:
    """构造来源摘要；载荷仅暂存，只有失败包装器才会将其归档为排障证据。"""
    raw_payload = batch.raw_payload if batch.raw_payload is not None else batch.payload
    raw_digest = hashlib.sha256(raw_payload).hexdigest()
    normalized_digest = hashlib.sha256(batch.payload).hexdigest()
    prefix = f"margin/{batch.capability}/{batch.provider_id}/{batch.observed_at:%Y/%m/%d}"
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
    return MarginSourceObservation(
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


def _record(value: object) -> MarginMarketDaily:
    """将一行标准 JSON 转为直报市场汇总，所有可选数值字段保留 `None` 语义。"""
    if not isinstance(value, dict):
        raise ValueError("margin market row is not an object")
    return MarginMarketDaily(
        trade_date=date.fromisoformat(_required(value, "tradeDate")),
        financing_balance=_optional_decimal(value.get("financingBalance")),
        financing_buy_amount=_optional_decimal(value.get("financingBuyAmount")),
        financing_repayment_amount=_optional_decimal(value.get("financingRepaymentAmount")),
        lending_balance_amount=_optional_decimal(value.get("lendingBalanceAmount")),
        lending_balance_qty=_optional_decimal(value.get("lendingBalanceQty")),
        lending_sell_qty=_optional_decimal(value.get("lendingSellQty")),
        lending_repayment_qty=_optional_decimal(value.get("lendingRepaymentQty")),
        total_balance=_optional_decimal(value.get("totalBalance")),
        currency=_required(value, "currency"),
        quantity_unit=_optional_text(value.get("quantityUnit")),
    )


def _required(value: dict[str, object], key: str) -> str:
    """读取非空文本字段，避免缺少日期或币种时继续发布不可解释的数值。"""
    normalized = _optional_text(value.get(key))
    if normalized is None:
        raise ValueError(f"{key} is required")
    return normalized


def _optional_decimal(value: object) -> Decimal | None:
    """保持来源空值为 `None`，真实零值仍以精确十进制零写入。"""
    normalized = _optional_text(value)
    return None if normalized is None else Decimal(normalized)


def _optional_text(value: object) -> str | None:
    """统一空白和 pandas 缺失字面量，防止它们污染直报字段。"""
    if value is None:
        return None
    normalized = str(value).strip()
    return None if not normalized or normalized.lower() in {"nan", "none", "nat"} else normalized


def _schema_error(message: str) -> ProviderError:
    """将载荷漂移固定为不可重试错误，防止任务层把错误源数据误当临时网络问题。"""
    return ProviderError(ProviderErrorCode.SCHEMA, message, retryable=False)
