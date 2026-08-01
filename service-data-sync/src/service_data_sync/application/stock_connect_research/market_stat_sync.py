"""AKShare 港通市场统计的 research-only 获取、解码和持久化编排。

标准记录来自 provider-neutral `market.stock_connect.market_stat.reported` capability。该服务只
把 AKShare/Eastmoney 报告值写入独立 research 链路，不调用官方 `StockConnectMarketDailySyncService`，
不创建正式 bundle、release、publication 或 PIT 事实。
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from service_data_sync.application.ports.data_source import (
    DataSourcePort,
    ProviderBatch,
    ProviderError,
    ProviderErrorCode,
    SourceRequest,
)
from service_data_sync.application.ports.stock_connect_market_stat_research import (
    StockConnectMarketStatFailureEvidenceStore,
    StockConnectMarketStatResearchRecord,
    StockConnectMarketStatResearchRepository,
    StockConnectMarketStatResearchSourceObservation,
    StoredStockConnectMarketStatResearchBatch,
)
from service_data_sync.domain.stock_connect import StockConnectChannel

_LOGGER = logging.getLogger(__name__)
_CAPABILITY = "market.stock_connect.market_stat.reported"
_SCHEMA = "quant-v2.stock-connect-market-daily.v1"
_TOP_LEVEL_FIELDS = frozenset({"schema", "channel", "direction", "valueKind", "records"})
_RECORD_FIELDS = frozenset(
    {
        "tradeDate",
        "buyAmount",
        "sellAmount",
        "turnoverAmount",
        "netBuyAmount",
        "quotaBalance",
        "currency",
        "availabilityStatus",
        "fieldAvailability",
    }
)
_FIELD_AVAILABILITY_FIELDS = frozenset(
    {
        "buyAmount",
        "sellAmount",
        "turnoverAmount",
        "netBuyAmount",
        "quotaBalance",
        "currency",
        "availabilityStatus",
    }
)


@dataclass(frozen=True, slots=True)
class StockConnectMarketStatResearchSyncResult:
    """返回成功 research 写入摘要；故意不提供 publication 或 dataVersion。"""

    capability: str
    batch: StoredStockConnectMarketStatResearchBatch


class StockConnectMarketStatResearchSyncService:
    """同步一条 AKShare 港通统计观察，并在失败时留私有证据。

    成功路径只把摘要型 URI 传入仓储，`stage_batch` 的内存暂存最终会被丢弃，因此不会向 S3
    写入任何 raw 或 normalized 字节。解码、质量或持久化失败时才触发证据 manifest。
    """

    def __init__(
        self,
        *,
        source: DataSourcePort,
        repository: StockConnectMarketStatResearchRepository,
        failure_evidence_store: StockConnectMarketStatFailureEvidenceStore,
    ) -> None:
        """接收中立 adapter、research 仓储与失败证据端口，不依赖官方港通服务。"""
        self._source = source
        self._repository = repository
        self._failure_evidence_store = failure_evidence_store

    async def sync(
        self,
        *,
        channel: StockConnectChannel,
        start: date,
        end: date,
    ) -> StockConnectMarketStatResearchSyncResult:
        """抓取并记录有界通道方向窗口，返回零记录研究批次而不是伪造空行情。"""
        if start > end:
            raise ValueError("stock-connect research start must not be after end")
        try:
            batch = await self._fetch(channel=channel, start=start, end=end)
            self._failure_evidence_store.stage_batch(batch)
            records = decode_stock_connect_market_stat_research_batch(
                batch.payload,
                channel=channel,
            )
            if any(record.trade_date < start or record.trade_date > end for record in records):
                raise _schema_error("stock-connect research record is outside requested date range")
            stored = self._repository.record_market_statistics(
                channel=channel,
                records=records,
                source=_source_observation(batch),
            )
            return StockConnectMarketStatResearchSyncResult(
                capability=_CAPABILITY,
                batch=stored,
            )
        except Exception as error:
            self._retain_failure_evidence(error, channel=channel, start=start, end=end)
            raise
        finally:
            self._failure_evidence_store.discard()

    async def _fetch(
        self,
        *,
        channel: StockConnectChannel,
        start: date,
        end: date,
    ) -> ProviderBatch:
        """确认 capability 已声明后按中立参数请求，禁止另一来源或方向静默补齐。"""
        if _CAPABILITY not in self._source.capabilities():
            raise ProviderError(
                ProviderErrorCode.INVALID_REQUEST,
                "stock-connect market-stat research capability is unsupported",
                retryable=False,
            )
        return await self._source.fetch(
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

    def _retain_failure_evidence(
        self,
        error: Exception,
        *,
        channel: StockConnectChannel,
        start: date,
        end: date,
    ) -> None:
        """以不泄露供应商正文的摘要固化失败证据，且绝不掩盖原始同步异常。"""
        try:
            if isinstance(error, ProviderError) and error.failure_evidence is not None:
                self._failure_evidence_store.stage_failure_summary(
                    error.failure_evidence,
                    error.failure_evidence_content_type or "application/json",
                    capability=_CAPABILITY,
                )
            self._failure_evidence_store.stage_failure_summary(
                _failure_summary(
                    error,
                    channel=channel,
                    start=start,
                    end=end,
                ),
                "application/json",
                capability=_CAPABILITY,
            )
            manifest_uri = self._failure_evidence_store.persist_failure(error)
            if manifest_uri is not None:
                _LOGGER.error(
                    "港通市场统计 research 同步失败证据已归档",
                    extra={"failure_evidence_manifest_uri": manifest_uri},
                )
        except Exception:
            # 证据固化失败不能改变原始 provider、schema 或数据库异常的重试分类。
            _LOGGER.exception("港通市场统计 research 失败证据归档失败")


def decode_stock_connect_market_stat_research_batch(
    payload: bytes,
    *,
    channel: StockConnectChannel,
) -> tuple[StockConnectMarketStatResearchRecord, ...]:
    """解码 AKShare 标准批次，允许真实可选列但拒绝未冻结的 tradeCount/ETF 等字段。"""
    try:
        decoded = json.loads(payload)
    except json.JSONDecodeError as error:
        raise _schema_error("stock-connect research payload is not JSON") from error
    if not isinstance(decoded, dict) or set(decoded) != _TOP_LEVEL_FIELDS:
        raise _schema_error("stock-connect research payload fields changed")
    if decoded.get("schema") != _SCHEMA:
        raise _schema_error("stock-connect research schema is invalid")
    if decoded.get("channel") != channel.channel or decoded.get("direction") != channel.direction:
        raise _schema_error("stock-connect research channel identity mismatch")
    if decoded.get("valueKind") != "REPORTED":
        raise _schema_error("stock-connect research accepts reported values only")
    raw_records = decoded.get("records")
    if not isinstance(raw_records, list):
        raise _schema_error("stock-connect research records is invalid")
    try:
        records = tuple(_record(value) for value in raw_records)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise _schema_error("stock-connect research record is invalid") from error
    if len({record.trade_date for record in records}) != len(records):
        raise _schema_error("stock-connect research records contain duplicate trade dates")
    return tuple(sorted(records, key=lambda record: record.trade_date))


def _record(value: object) -> StockConnectMarketStatResearchRecord:
    """把一条标准记录转换为可选字段 research 值对象，不构造官方缺失字段。"""
    if not isinstance(value, dict) or not set(value) <= _RECORD_FIELDS:
        raise ValueError("stock-connect research record fields changed")
    return StockConnectMarketStatResearchRecord(
        trade_date=date.fromisoformat(_required_text(value, "tradeDate")),
        buy_amount=_optional_decimal(value.get("buyAmount")),
        sell_amount=_optional_decimal(value.get("sellAmount")),
        turnover_amount=_optional_decimal(value.get("turnoverAmount")),
        net_buy_amount=_optional_decimal(value.get("netBuyAmount")),
        quota_balance=_optional_decimal(value.get("quotaBalance")),
        currency=_optional_currency(value.get("currency")),
        availability_status=_optional_text(value.get("availabilityStatus")),
        field_availability=_field_availability(value.get("fieldAvailability")),
    )


def _source_observation(batch: ProviderBatch) -> StockConnectMarketStatResearchSourceObservation:
    """将成功批次投影为 digest-only 来源血缘，永不调用对象存储写入接口。"""
    raw_payload = batch.raw_payload if batch.raw_payload is not None else batch.payload
    raw_digest = hashlib.sha256(raw_payload).hexdigest()
    normalized_digest = hashlib.sha256(batch.payload).hexdigest()
    return StockConnectMarketStatResearchSourceObservation(
        provider_id=batch.provider_id,
        capability=batch.capability,
        raw_payload_sha256=raw_digest,
        raw_uri=f"unretained://sha256/{raw_digest}",
        raw_content_type=batch.raw_content_type or batch.content_type,
        raw_byte_size=len(raw_payload),
        normalized_payload_sha256=normalized_digest,
        normalized_uri=f"unretained://sha256/{normalized_digest}",
        normalized_content_type=batch.content_type,
        normalized_byte_size=len(batch.payload),
        observed_at=batch.observed_at,
        upstream_source=batch.upstream_source or batch.provider_id,
        adapter_version=batch.adapter_version,
        schema_fingerprint=(
            batch.schema_fingerprint or hashlib.sha256(batch.capability.encode()).hexdigest()
        ),
    )


def _failure_summary(
    error: Exception,
    *,
    channel: StockConnectChannel,
    start: date,
    end: date,
) -> bytes:
    """生成仅含稳定类别和请求边界的失败摘要，不写入供应商正文或异常文本。"""
    provider_code = error.code if isinstance(error, ProviderError) else None
    return json.dumps(
        {
            "capability": _CAPABILITY,
            "channel": channel.channel,
            "direction": channel.direction,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "errorType": type(error).__name__,
            "providerErrorCode": None if provider_code is None else str(provider_code),
        },
        separators=(",", ":"),
    ).encode()


def _required_text(value: dict[str, object], key: str) -> str:
    """读取唯一必填交易日文本，缺失时不能生成未知日期 research 观察。"""
    resolved = _optional_text(value.get(key))
    if resolved is None:
        raise ValueError(f"{key} is required")
    return resolved


def _optional_text(value: object) -> str | None:
    """将 JSON 空值、空白和 pandas 缺失文本归一为真实空值。"""
    if value is None:
        return None
    resolved = str(value).strip()
    return None if not resolved or resolved.lower() in {"nan", "none", "nat"} else resolved


def _optional_currency(value: object) -> str | None:
    """读取来源明确报告的 ISO 大写币种；缺失保持空值而不默认 CNY。"""
    currency = _optional_text(value)
    if currency is None:
        return None
    if len(currency) != 3 or currency != currency.upper() or not currency.isascii():
        raise ValueError("currency is invalid")
    return currency


def _optional_decimal(value: object) -> Decimal | None:
    """读取可空精确十进制金额，禁止二进制浮点和非数值文本进入 research。"""
    resolved = _optional_text(value)
    return None if resolved is None else Decimal(resolved)


def _field_availability(value: object) -> tuple[tuple[str, str], ...] | None:
    """保留来源显式逐字段状态；缺失整体元数据时返回空值而非补造状态。"""
    if value is None:
        return None
    if not isinstance(value, dict) or not set(value) <= _FIELD_AVAILABILITY_FIELDS:
        raise ValueError("fieldAvailability fields changed")
    normalized: list[tuple[str, str]] = []
    for field, status in value.items():
        if not isinstance(field, str) or not isinstance(status, str):
            raise ValueError("fieldAvailability entry is invalid")
        normalized_status = _optional_text(status)
        if normalized_status is None:
            raise ValueError("fieldAvailability entry is invalid")
        normalized.append((field, normalized_status))
    return tuple(sorted(normalized))


def _schema_error(message: str) -> ProviderError:
    """构造不可重试 schema 错误，阻止漂移载荷被当作 research 事实。"""
    return ProviderError(ProviderErrorCode.SCHEMA, message, retryable=False)


__all__ = [
    "StockConnectMarketStatResearchSyncResult",
    "StockConnectMarketStatResearchSyncService",
    "decode_stock_connect_market_stat_research_batch",
]
