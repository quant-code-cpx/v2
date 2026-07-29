"""将来源无关的真实衍生品合约日线批次转换为可发布候选。"""

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
from service_data_sync.application.ports.derivative_market import (
    DerivativeDailyBarRepository,
    DerivativeSourceObservation,
    PublishedDerivativeDailyBars,
)
from service_data_sync.application.ports.market_data import RawPayload, RawPayloadStore
from service_data_sync.domain.derivative import DerivativeContractIdentifier, DerivativeDailyBar

_CAPABILITY = "derivative.bar.1d.reported"
_SCHEMA = "quant-v2.derivative-daily-bar.v1"


@dataclass(frozen=True, slots=True)
class DerivativeDailyBarSyncResult:
    """返回真实合约日行情发布结果；合约窗口无数据时返回成功空结果。"""

    contract: DerivativeContractIdentifier
    data_version: UUID | None
    inserted_count: int
    unchanged_count: int
    availability: str = "available"


class DerivativeDailyBarSyncService:
    """同步一段真实合约日线，连续代码或无 identity 证据的载荷一律拒绝。"""

    def __init__(
        self,
        *,
        source: DataSourcePort,
        repository: DerivativeDailyBarRepository,
        raw_payload_store: RawPayloadStore,
    ) -> None:
        """接收来源、发布仓储及失败时才持久化的排障载荷端口。"""
        self._source = source
        self._repository = repository
        self._raw_payload_store = raw_payload_store

    async def sync(
        self,
        *,
        contract: DerivativeContractIdentifier,
        start: date,
        end: date,
    ) -> DerivativeDailyBarSyncResult:
        """同步包含端窗口的真实合约日行情；成功只发布 canonical 事实。"""
        if start > end:
            raise ValueError("start must not be after end")
        if _CAPABILITY not in self._source.capabilities():
            raise ProviderError(
                ProviderErrorCode.INVALID_REQUEST, "unsupported capability", retryable=False
            )
        batch = await self._source.fetch(
            SourceRequest(
                capability=_CAPABILITY,
                parameters=(
                    ("contract", contract.qualified_key),
                    ("start", start.isoformat()),
                    ("end", end.isoformat()),
                ),
            )
        )
        bars = decode_derivative_daily_bar_batch(batch.payload, contract=contract)
        if not bars:
            # 合约尚未上市或窗口无成交均属正常空集，不能写空 release 或留成功原文。
            return DerivativeDailyBarSyncResult(
                contract=contract,
                data_version=None,
                inserted_count=0,
                unchanged_count=0,
                availability="empty",
            )
        source = _archive_batch(batch=batch, payload_store=self._raw_payload_store)
        publication = self._repository.publish_daily_bars(
            contract=contract,
            bars=bars,
            source=source,
        )
        return _result(publication)


def decode_derivative_daily_bar_batch(
    payload: bytes, *, contract: DerivativeContractIdentifier
) -> tuple[DerivativeDailyBar, ...]:
    """解析 adapter 标准 JSON，拒绝连续序列、身份漂移和重复交易日。"""
    try:
        decoded = json.loads(payload)
    except json.JSONDecodeError as error:
        raise _schema_error("derivative daily-bar payload is not JSON") from error
    if not isinstance(decoded, dict) or decoded.get("schema") != _SCHEMA:
        raise _schema_error("unexpected derivative daily-bar schema")
    if decoded.get("contract") != contract.qualified_key:
        raise _schema_error("derivative daily-bar contract mismatch")
    if decoded.get("contractKind") != "REAL":
        raise _schema_error("derivative daily-bar must reference a real contract")
    rows = decoded.get("bars")
    if not isinstance(rows, list):
        raise _schema_error("derivative daily-bar payload has no bars")
    try:
        bars = tuple(_decode_bar(row) for row in rows)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise _schema_error("invalid derivative daily-bar value") from error
    if len({bar.trade_date for bar in bars}) != len(bars):
        raise _schema_error("derivative daily-bar payload has duplicate trade dates")
    return tuple(sorted(bars, key=lambda bar: bar.trade_date))


def _archive_batch(
    *, batch: ProviderBatch, payload_store: RawPayloadStore
) -> DerivativeSourceObservation:
    """分别固化上游字节和标准 JSON，确保 schema 漂移也能独立重放与审计。"""
    raw_payload = batch.raw_payload if batch.raw_payload is not None else batch.payload
    raw_digest = hashlib.sha256(raw_payload).hexdigest()
    normalized_digest = hashlib.sha256(batch.payload).hexdigest()
    prefix = f"derivative/{batch.capability}/{batch.provider_id}/{batch.observed_at:%Y/%m/%d}"
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
    return DerivativeSourceObservation(
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


def _decode_bar(row: object) -> DerivativeDailyBar:
    """把一条标准 JSON 记录转换为保留报价、结算与仓位边界的领域值。"""
    if not isinstance(row, dict):
        raise ValueError("derivative daily-bar row is not an object")
    turnover = _optional_decimal(row.get("turnover"))
    turnover_currency = _optional_text(row.get("turnoverCurrency"))
    return DerivativeDailyBar(
        trade_date=date.fromisoformat(_required(row, "tradeDate")),
        open_price=_decimal(row, "open"),
        high_price=_decimal(row, "high"),
        low_price=_decimal(row, "low"),
        close_price=_decimal(row, "close"),
        pre_close_price=_optional_decimal(row.get("preClose")),
        settlement_price=_optional_decimal(row.get("settlement")),
        pre_settlement_price=_optional_decimal(row.get("preSettlement")),
        volume_value=_decimal(row, "volume"),
        open_interest_value=_decimal(row, "openInterest"),
        turnover_value=turnover,
        turnover_currency=turnover_currency,
        turnover_unit=_optional_text(row.get("turnoverUnit")),
        trade_status=_optional_text(row.get("tradeStatus")),
    )


def _required(row: dict[str, object], key: str) -> str:
    """读取不可为空的 JSON 标量字段。"""
    value = row.get(key)
    if value is None or not str(value).strip():
        raise ValueError(f"{key} is required")
    return str(value)


def _decimal(row: dict[str, object], key: str) -> Decimal:
    """读取必填精确小数字段，避免浮点数进入 canonical。"""
    return Decimal(_required(row, key))


def _optional_decimal(value: object) -> Decimal | None:
    """解析真实空值为 `None`，不将其补成零。"""
    text = _optional_text(value)
    return None if text is None else Decimal(text)


def _optional_text(value: object) -> str | None:
    """将缺失、空白和 pandas 空值保持为真实空值。"""
    if value is None:
        return None
    text = str(value).strip()
    return None if not text or text.lower() == "nan" else text


def _schema_error(message: str) -> ProviderError:
    """构造不可重试的标准化载荷漂移错误。"""
    return ProviderError(ProviderErrorCode.SCHEMA, message, retryable=False)


def _result(publication: PublishedDerivativeDailyBars) -> DerivativeDailyBarSyncResult:
    """将仓储发布结果转换为任务、CLI 和监控可使用的稳定形状。"""
    return DerivativeDailyBarSyncResult(
        contract=publication.contract,
        data_version=publication.data_version,
        inserted_count=publication.inserted_count,
        unchanged_count=publication.unchanged_count,
    )
