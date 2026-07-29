"""ETF P0 未复权日线同步；成功时不留存来源原始字节。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from uuid import UUID

from service_data_sync.application.ports.data_source import (
    DataSourcePort,
    ProviderBatch,
    ProviderError,
    ProviderErrorCode,
    SourceRequest,
)
from service_data_sync.application.ports.dataset_availability import DatasetAvailabilityRepository
from service_data_sync.application.ports.etf_market import (
    EtfDailyBarRepository,
    EtfSourceObservation,
)
from service_data_sync.application.ports.market_data import RawPayload, RawPayloadStore
from service_data_sync.domain.etf import EtfDailyBar, EtfIdentifier

_CAPABILITY = "fund.etf.bar.1d.raw"
_DATASET = "fund.etf.bar.1d.reported"
_SCHEMA = "quant-v2.etf-daily-bar.v1"


@dataclass(frozen=True, slots=True)
class EtfDailyBarSyncResult:
    """返回不含 Provider 内部字段的 ETF 发布标识和幂等计数。"""

    etf: EtfIdentifier
    data_version: UUID | None
    inserted_count: int
    unchanged_count: int
    availability: str = "available"


class EtfDailyBarSyncService:
    """同步一个 ETF 的未复权日线窗口，P0 不允许适配器请求或返回复权价格。"""

    def __init__(
        self,
        *,
        source: DataSourcePort,
        repository: EtfDailyBarRepository,
        raw_payload_store: RawPayloadStore,
        availability_repository: DatasetAvailabilityRepository | None = None,
    ) -> None:
        """接收来源无关 adapter、原子发布仓储和服务自有对象存储端口。"""
        self._source = source
        self._repository = repository
        self._raw_payload_store = raw_payload_store
        self._availability_repository = availability_repository

    async def sync(self, *, etf: EtfIdentifier, start: date, end: date) -> EtfDailyBarSyncResult:
        """抓取并发布包含边界的未复权窗口；来源不可用或空集可成功结束。"""
        if start > end:
            raise ValueError("start must not be after end")
        if _CAPABILITY not in self._source.capabilities():
            return self._availability_result(
                etf=etf,
                start=start,
                end=end,
                availability="source_unavailable",
                reason_code="capability_not_configured",
                provider_id=self._source.provider_id,
                observed_at=datetime.now(UTC),
            )
        try:
            batch = await self._source.fetch(
                SourceRequest(
                    capability=_CAPABILITY,
                    parameters=(
                        ("etf", etf.qualified_key),
                        ("start", start.isoformat()),
                        ("end", end.isoformat()),
                        ("priceBasis", "UNADJUSTED"),
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
            return self._availability_result(
                etf=etf,
                start=start,
                end=end,
                availability="source_unavailable",
                reason_code=error.code.value,
                provider_id=self._source.provider_id,
                observed_at=datetime.now(UTC),
            )
        bars = decode_etf_daily_bar_batch(batch.payload, etf=etf)
        if not bars:
            return self._availability_result(
                etf=etf,
                start=start,
                end=end,
                availability="empty",
                reason_code="no_matching_facts",
                provider_id=batch.provider_id,
                observed_at=batch.observed_at,
            )
        published = self._repository.publish_daily_bars(
            etf=etf,
            bars=bars,
            source=_archive_batch(batch=batch, payload_store=self._raw_payload_store),
        )
        if self._availability_repository is not None:
            self._availability_repository.clear(
                dataset=_DATASET,
                partition_key=_partition_key(etf=etf, start=start, end=end),
                cleared_at=datetime.now(UTC),
            )
        return EtfDailyBarSyncResult(
            etf=published.etf,
            data_version=published.data_version,
            inserted_count=published.inserted_count,
            unchanged_count=published.unchanged_count,
        )

    def _availability_result(
        self,
        *,
        etf: EtfIdentifier,
        start: date,
        end: date,
        availability: str,
        reason_code: str,
        provider_id: str | None,
        observed_at: datetime,
    ) -> EtfDailyBarSyncResult:
        """记录非事实结果并返回稳定空 DTO，供任务和消费者继续完成链路。"""
        if self._availability_repository is not None:
            self._availability_repository.record(
                dataset=_DATASET,
                partition_key=_partition_key(etf=etf, start=start, end=end),
                availability=availability,
                reason_code=reason_code,
                provider_id=provider_id,
                observed_at=observed_at,
            )
        return EtfDailyBarSyncResult(
            etf=etf,
            data_version=None,
            inserted_count=0,
            unchanged_count=0,
            availability=availability,
        )


def decode_etf_daily_bar_batch(payload: bytes, *, etf: EtfIdentifier) -> tuple[EtfDailyBar, ...]:
    """解码 adapter 标准 JSON，拒绝身份漂移、复权价格和重复交易日。"""
    try:
        decoded = json.loads(payload)
    except json.JSONDecodeError as error:
        raise _schema_error("ETF daily-bar payload is not JSON") from error
    if not isinstance(decoded, dict) or decoded.get("schema") != _SCHEMA:
        raise _schema_error("unexpected ETF daily-bar schema")
    if decoded.get("etf") != etf.qualified_key:
        raise _schema_error("ETF daily-bar identity mismatch")
    if decoded.get("priceBasis") != "UNADJUSTED":
        raise _schema_error("ETF P0 accepts only UNADJUSTED prices")
    records = decoded.get("bars")
    if not isinstance(records, list):
        raise _schema_error("ETF daily-bar payload has no bars")
    try:
        bars = tuple(_bar(record) for record in records)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise _schema_error("ETF daily-bar value is invalid") from error
    if len({bar.trade_date for bar in bars}) != len(bars):
        raise _schema_error("ETF daily-bar payload has duplicate trade dates")
    return tuple(sorted(bars, key=lambda item: item.trade_date))


def _partition_key(*, etf: EtfIdentifier, start: date, end: date) -> str:
    """用 ETF 上市代码和精确请求窗口构造不会跨窗口复用的空观测分区。"""
    return f"{etf.qualified_key}:{start.isoformat()}:{end.isoformat()}"


def _archive_batch(*, batch: ProviderBatch, payload_store: RawPayloadStore) -> EtfSourceObservation:
    """分别固化 raw 和标准 JSON，保证来源字段漂移后仍可回放和重做映射。"""
    raw_payload = batch.raw_payload if batch.raw_payload is not None else batch.payload
    raw_digest = hashlib.sha256(raw_payload).hexdigest()
    normalized_digest = hashlib.sha256(batch.payload).hexdigest()
    prefix = f"etf/{batch.capability}/{batch.provider_id}/{batch.observed_at:%Y/%m/%d}"
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
    return EtfSourceObservation(
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


def _bar(value: object) -> EtfDailyBar:
    """将单个中立 JSON 行转换为精确 ETF 日线，单位和状态不做来源外推。"""
    if not isinstance(value, dict):
        raise ValueError("ETF daily-bar row is not an object")
    return EtfDailyBar(
        trade_date=date.fromisoformat(_required(value, "tradeDate")),
        open_price=_decimal(value, "open"),
        high_price=_decimal(value, "high"),
        low_price=_decimal(value, "low"),
        close_price=_decimal(value, "close"),
        volume_value=_decimal(value, "volume"),
        volume_unit=_required(value, "volumeUnit"),
        amount_value=_decimal(value, "amount"),
        currency=_required(value, "currency"),
        trade_status=_optional_text(value.get("tradeStatus")),
    )


def _required(value: dict[str, object], key: str) -> str:
    """读取不可为空的 JSON 文本字段，拒绝 `null`、空白和 pandas 空值。"""
    normalized = _optional_text(value.get(key))
    if normalized is None:
        raise ValueError(f"{key} is required")
    return normalized


def _decimal(value: dict[str, object], key: str) -> Decimal:
    """读取精确十进制字段，拒绝浮点补零和缺失金额。"""
    return Decimal(_required(value, key))


def _optional_text(value: object) -> str | None:
    """统一空值、空白和 pandas 缺失字面量，保留真正未知状态。"""
    if value is None:
        return None
    normalized = str(value).strip()
    return None if not normalized or normalized.lower() in {"nan", "none", "nat"} else normalized


def _schema_error(message: str) -> ProviderError:
    """构造不可重试的标准 schema 漂移异常，避免任务层把格式失败当网络抖动重试。"""
    return ProviderError(ProviderErrorCode.SCHEMA, message, retryable=False)
