"""板块日、周、月行情的原始证据归档与标准发布编排。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from uuid import UUID

from service_data_sync.application.ports.data_source import (
    DataSourcePort,
    ProviderError,
    ProviderErrorCode,
    SourceRequest,
)
from service_data_sync.application.ports.market_data import RawPayload, RawPayloadStore
from service_data_sync.application.ports.sector_market_data import (
    PublishedSectorBars,
    SectorMarketDataRepository,
)
from service_data_sync.domain.sector import SectorBar, SectorIdentifier, SectorPeriod

_SECTOR_BAR_SCHEMA = "quant-v2.sector-bar.v1"


@dataclass(frozen=True, slots=True)
class SectorBarSyncResult:
    """向任务和 CLI 返回不含供应商专有字段的板块发布摘要。"""

    sector: SectorIdentifier
    period: SectorPeriod
    data_version: UUID
    inserted_count: int
    unchanged_count: int


class SectorBarSyncService:
    """同步一个有界板块周期窗口；三种周期均只接受上游直接结果。"""

    def __init__(
        self,
        *,
        source: DataSourcePort,
        repository: SectorMarketDataRepository,
        raw_payload_store: RawPayloadStore,
    ) -> None:
        """从组合根接收中立数据源、仓储和原始证据端口。"""
        self._source = source
        self._repository = repository
        self._raw_payload_store = raw_payload_store

    async def sync(
        self,
        *,
        identifier: SectorIdentifier,
        period: SectorPeriod,
        start: date,
        end: date,
    ) -> SectorBarSyncResult:
        """同步包含端日期窗口内一个板块的指定物理周期行情。"""
        if start > end:
            raise ValueError("start must not be after end")
        capability = period.capability
        if capability not in self._source.capabilities():
            raise ProviderError(
                ProviderErrorCode.INVALID_REQUEST, "unsupported capability", retryable=False
            )
        # 应用层只提交分类体系、板块代码、独立周期和日期范围。
        # 供应商函数、字段和周期参数映射只能存在于 adapter。
        batch = await self._source.fetch(
            SourceRequest(
                capability=capability,
                parameters=(
                    ("sectorScheme", identifier.scheme.value),
                    ("sector", identifier.code),
                    ("period", period.value),
                    ("start", start.isoformat()),
                    ("end", end.isoformat()),
                ),
            )
        )
        bars = decode_sector_bar_batch(batch.payload, identifier=identifier, period=period)
        raw_payload = batch.raw_payload if batch.raw_payload is not None else batch.payload
        raw_content_type = batch.raw_content_type or batch.content_type
        raw_digest = hashlib.sha256(raw_payload).hexdigest()
        # 所有 canonical 变更均必须能回链至不可变原始证据。
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
        publication = self._repository.publish_bars(
            identifier=identifier,
            period=period,
            bars=bars,
            provider_id=batch.provider_id,
            source_payload_sha256=raw_digest,
            raw_uri=raw_uri,
            observed_at=batch.observed_at,
        )
        return _result(identifier, period, publication)


def decode_sector_bar_batch(
    payload: bytes, *, identifier: SectorIdentifier, period: SectorPeriod
) -> tuple[SectorBar, ...]:
    """解析 adapter 标准 JSON，并拒绝身份、周期或结构漂移。"""
    try:
        decoded = json.loads(payload)
    except json.JSONDecodeError as error:
        raise ProviderError(
            ProviderErrorCode.SCHEMA, "sector-bar payload is not JSON", retryable=False
        ) from error
    if not isinstance(decoded, dict) or decoded.get("schema") != _SECTOR_BAR_SCHEMA:
        raise ProviderError(
            ProviderErrorCode.SCHEMA, "unexpected sector-bar schema", retryable=False
        )
    if (
        decoded.get("sectorScheme") != identifier.scheme.value
        or decoded.get("sector") != identifier.code
    ):
        raise ProviderError(
            ProviderErrorCode.SCHEMA, "sector-bar identity mismatch", retryable=False
        )
    if decoded.get("period") != period.value:
        raise ProviderError(ProviderErrorCode.SCHEMA, "sector-bar period mismatch", retryable=False)
    records = decoded.get("bars")
    if not isinstance(records, list) or not records:
        raise ProviderError(
            ProviderErrorCode.SCHEMA, "sector-bar payload has no bars", retryable=False
        )
    bars = tuple(_decode_bar(record) for record in records)
    if len({bar.period_end for bar in bars}) != len(bars):
        raise ProviderError(
            ProviderErrorCode.SCHEMA,
            "sector-bar payload has duplicate period ends",
            retryable=False,
        )
    # 供应商返回顺序不构成契约；排序确保发布和内部读取的行为稳定。
    return tuple(sorted(bars, key=lambda bar: bar.period_end))


def _decode_bar(record: object) -> SectorBar:
    """将一条中立 JSON 行映射为带清晰单位语义的标准板块行情。"""
    if not isinstance(record, dict):
        raise ProviderError(
            ProviderErrorCode.SCHEMA, "sector-bar record is not an object", retryable=False
        )
    try:
        return SectorBar(
            period_end=date.fromisoformat(_required_string(record, "periodEnd")),
            open_price=_decimal(record, "open"),
            high_price=_decimal(record, "high"),
            low_price=_decimal(record, "low"),
            close_price=_decimal(record, "close"),
            volume_value=_decimal(record, "volumeValue"),
            volume_unit=_required_string(record, "volumeUnit"),
            amount_cny=_decimal(record, "amountCny"),
            amplitude_percent=_optional_decimal(record, "amplitudePercent"),
            change_percent=_optional_decimal(record, "changePercent"),
            change_amount=_optional_decimal(record, "changeAmount"),
            turnover_percent=_optional_decimal(record, "turnoverPercent"),
        )
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ProviderError(
            ProviderErrorCode.SCHEMA, "invalid sector-bar value", retryable=False
        ) from error


def _required_string(record: dict[str, object], key: str) -> str:
    """读取必填 JSON 标量字段，不接受缺失或 `null`。"""
    value = record.get(key)
    if value is None:
        raise ValueError(f"{key} is required")
    return str(value)


def _decimal(record: dict[str, object], key: str) -> Decimal:
    """读取一个必填精确小数字段，避免二进制浮点进入领域对象。"""
    return Decimal(_required_string(record, key))


def _optional_decimal(record: dict[str, object], key: str) -> Decimal | None:
    """读取一个可空精确小数字段，保留供应商缺失语义。"""
    value = record.get(key)
    return None if value is None else Decimal(str(value))


def _result(
    identifier: SectorIdentifier,
    period: SectorPeriod,
    publication: PublishedSectorBars,
) -> SectorBarSyncResult:
    """投影持久化结果为任务调用方稳定的最小发布摘要。"""
    return SectorBarSyncResult(
        sector=identifier,
        period=period,
        data_version=publication.data_version,
        inserted_count=publication.inserted_count,
        unchanged_count=publication.unchanged_count,
    )
