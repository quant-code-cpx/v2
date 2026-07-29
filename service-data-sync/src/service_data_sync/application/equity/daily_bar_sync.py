"""将数据源无关日线批次转换为已发布数据的应用用例。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from uuid import UUID

from service_data_sync.application.ports.data_source import (
    DataSourcePort,
    ProviderError,
    ProviderErrorCode,
    SourceRequest,
)
from service_data_sync.application.ports.market_data import (
    EquityAvailabilityObservation,
    EquityDailyBarRepository,
    PublishedDailyBars,
    RawPayload,
    RawPayloadStore,
)
from service_data_sync.domain.equity import EquityDailyBar, EquityIdentifier

_DAILY_BAR_SCHEMA = "quant-v2.equity-daily-bar.v1"


@dataclass(frozen=True, slots=True)
class DailyBarSyncResult:
    """返回不含数据源专有字段的发布身份与写入计数。"""

    instrument: EquityIdentifier
    data_version: UUID | None
    inserted_count: int
    unchanged_count: int
    availability: str = "available"


class EquityDailyBarSyncService:
    """同步一个有界日线窗口；成功为空时写观测，失败时才归档原始证据。"""

    def __init__(
        self,
        *,
        source: DataSourcePort,
        repository: EquityDailyBarRepository,
        raw_payload_store: RawPayloadStore,
    ) -> None:
        """从组合根接收数据源无关的来源与存储端口。"""
        self._source = source
        self._repository = repository
        self._raw_payload_store = raw_payload_store

    async def sync(
        self,
        *,
        identifier: EquityIdentifier,
        start: date,
        end: date,
    ) -> DailyBarSyncResult:
        """同步一只证券在包含起止日期窗口内的日线。"""
        if start > end:
            raise ValueError("start must not be after end")
        capability = "equity.bar.1d.raw"
        if capability not in self._source.capabilities():
            return _availability_result(
                identifier,
                self._repository.record_daily_bar_availability(
                    identifier=identifier,
                    start=start,
                    end=end,
                    availability="source_unavailable",
                    reason_code="capability_not_configured",
                    provider_id=self._source.provider_id,
                    observed_at=datetime.now(UTC),
                ),
            )
        # 应用层只传递标准证券标识与日期范围。
        # 供应商参数名必须封装在适配器内。
        try:
            batch = await self._source.fetch(
                SourceRequest(
                    capability=capability,
                    parameters=(
                        ("instrument", identifier.qualified_symbol),
                        ("start", start.isoformat()),
                        ("end", end.isoformat()),
                    ),
                )
            )
        except ProviderError as error:
            if error.code not in {
                ProviderErrorCode.UNAVAILABLE,
                ProviderErrorCode.RATE_LIMITED,
                ProviderErrorCode.AUTHENTICATION,
                ProviderErrorCode.INVALID_REQUEST,
            }:
                raise
            return _availability_result(
                identifier,
                self._repository.record_daily_bar_availability(
                    identifier=identifier,
                    start=start,
                    end=end,
                    availability="source_unavailable",
                    reason_code=error.code.value,
                    provider_id=self._source.provider_id,
                    observed_at=datetime.now(UTC),
                ),
            )
        # 原始证据或标准状态落库前，先拒绝结构和证券标识漂移。
        bars = decode_daily_bar_batch(batch.payload, identifier)
        if not bars:
            return _availability_result(
                identifier,
                self._repository.record_daily_bar_availability(
                    identifier=identifier,
                    start=start,
                    end=end,
                    availability="empty",
                    reason_code="no_matching_facts",
                    provider_id=batch.provider_id,
                    observed_at=batch.observed_at,
                ),
            )
        raw_payload = batch.raw_payload if batch.raw_payload is not None else batch.payload
        raw_content_type = batch.raw_content_type or batch.content_type
        raw_digest = hashlib.sha256(raw_payload).hexdigest()
        # 成功路径只记录摘要标记；若后续发布失败，入口会固化已暂存来源字节。
        raw_uri = self._raw_payload_store.put(
            RawPayload(
                object_key=(
                    f"raw/{capability}/{batch.provider_id}/{batch.observed_at:%Y/%m/%d}/"
                    f"{raw_digest}.json"
                ),
                content_sha256=raw_digest,
                content_type=raw_content_type,
                payload=raw_payload,
            )
        )
        # 仓储事务将原始血缘与带版本的业务值一并记录。
        publication = self._repository.publish_daily_bars(
            identifier=identifier,
            bars=bars,
            provider_id=batch.provider_id,
            source_payload_sha256=raw_digest,
            raw_uri=raw_uri,
            observed_at=batch.observed_at,
        )
        # 成功发布的业务事实必须覆盖同窗口旧空状态，避免读取端继续误报空集。
        self._repository.clear_daily_bar_availability(
            identifier=identifier,
            start=start,
            end=end,
            cleared_at=datetime.now(UTC),
        )
        return _result(identifier, publication)


def decode_daily_bar_batch(
    payload: bytes, identifier: EquityIdentifier
) -> tuple[EquityDailyBar, ...]:
    """解析适配器产出的标准 JSON，并拒绝结构或证券标识漂移。"""
    try:
        decoded = json.loads(payload)
    except json.JSONDecodeError as error:
        raise ProviderError(
            ProviderErrorCode.SCHEMA, "daily-bar payload is not JSON", retryable=False
        ) from error
    if not isinstance(decoded, dict) or decoded.get("schema") != _DAILY_BAR_SCHEMA:
        raise ProviderError(
            ProviderErrorCode.SCHEMA, "unexpected daily-bar schema", retryable=False
        )
    if decoded.get("instrument") != identifier.qualified_symbol:
        raise ProviderError(
            ProviderErrorCode.SCHEMA, "daily-bar identity mismatch", retryable=False
        )
    records = decoded.get("bars")
    if not isinstance(records, list):
        raise ProviderError(
            ProviderErrorCode.SCHEMA, "daily-bar payload has no bars", retryable=False
        )
    bars = tuple(_decode_bar(record) for record in records)
    if len({bar.trade_date for bar in bars}) != len(bars):
        raise ProviderError(
            ProviderErrorCode.SCHEMA, "daily-bar payload has duplicate dates", retryable=False
        )
    # 适配器返回顺序不构成契约；统一按交易日排序，保证发布与读取结果稳定。
    return tuple(sorted(bars, key=lambda bar: bar.trade_date))


def _decode_bar(record: object) -> EquityDailyBar:
    """将一条数据源无关 JSON 记录映射为精确的标准日线值。"""
    if not isinstance(record, dict):
        raise ProviderError(
            ProviderErrorCode.SCHEMA, "daily-bar record is not an object", retryable=False
        )
    try:
        turnover_value = record.get("turnoverRate")
        return EquityDailyBar(
            trade_date=date.fromisoformat(_required_string(record, "tradeDate")),
            open_price=_decimal(record, "open"),
            high_price=_decimal(record, "high"),
            low_price=_decimal(record, "low"),
            close_price=_decimal(record, "close"),
            volume_shares=int(_required_string(record, "volumeShares")),
            amount_cny=_decimal(record, "amountCny"),
            turnover_rate=None if turnover_value is None else Decimal(str(turnover_value)),
        )
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ProviderError(
            ProviderErrorCode.SCHEMA, "invalid daily-bar value", retryable=False
        ) from error


def _required_string(record: dict[str, object], key: str) -> str:
    """读取必填 JSON 标量字段，不接受缺失或 `null`。"""
    value = record.get(key)
    if value is None:
        raise ValueError(f"{key} is required")
    return str(value)


def _decimal(record: dict[str, object], key: str) -> Decimal:
    """读取一个精确小数 JSON 字段，并拒绝缺失值。"""
    return Decimal(_required_string(record, key))


def _result(identifier: EquityIdentifier, publication: PublishedDailyBars) -> DailyBarSyncResult:
    """将持久化结果投影为供任务或 CLI 调用方使用的应用结果。"""
    return DailyBarSyncResult(
        instrument=identifier,
        data_version=publication.data_version,
        inserted_count=publication.inserted_count,
        unchanged_count=publication.unchanged_count,
        availability="available",
    )


def _availability_result(
    identifier: EquityIdentifier, observation: EquityAvailabilityObservation
) -> DailyBarSyncResult:
    """将无事实同步观测映射为可成功结束的应用层结果。"""
    return DailyBarSyncResult(
        instrument=identifier,
        data_version=None,
        inserted_count=0,
        unchanged_count=0,
        availability=observation.availability,
    )
