"""沪深港通 `P0` 活跃证券集合同步。

排行榜每行先保留来源代码、通道、方向、交易日和金额，再在发布前通过冻结身份视图解析证券，避免跨市场代码误绑。
来源没有返回活跃证券是合法空结果；成功不存原始字节，失败才留存排障证据。
"""

from __future__ import annotations

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
from service_data_sync.application.ports.market_data import RawPayloadStore
from service_data_sync.application.ports.stock_connect import (
    StockConnectActiveSecurityRepository,
)
from service_data_sync.application.stock_connect.market_daily_sync import _archive_batch
from service_data_sync.domain.stock_connect import StockConnectActiveSecurity, StockConnectChannel

_CAPABILITY = "market.stock_connect.active_security.snapshot"
_SCHEMA = "quant-v2.stock-connect-active-security.v1"


@dataclass(frozen=True, slots=True)
class StockConnectActiveSecuritySyncResult:
    """返回独立活跃榜集合的发布版本；无榜单时返回成功空结果。"""

    channel: StockConnectChannel
    data_version: UUID | None
    inserted_count: int
    unchanged_count: int
    availability: str = "available"


class StockConnectActiveSecuritySyncService:
    """同步通道方向日终活跃证券；它与市场统计分 capability 和分 release 发布。"""

    def __init__(
        self,
        *,
        source: DataSourcePort,
        repository: StockConnectActiveSecurityRepository,
        raw_payload_store: RawPayloadStore,
    ) -> None:
        """接收中立数据源、活跃榜仓储和失败排障载荷端口。"""
        self._source = source
        self._repository = repository
        self._raw_payload_store = raw_payload_store

    async def sync(
        self, *, channel: StockConnectChannel, start: date, end: date
    ) -> StockConnectActiveSecuritySyncResult:
        """抓取一个通道方向日期窗，市场统计 publication 不会作为活跃榜替代输入。"""
        if start > end:
            raise ValueError("start must not be after end")
        if _CAPABILITY not in self._source.capabilities():
            raise ProviderError(
                ProviderErrorCode.INVALID_REQUEST,
                "unsupported stock-connect active-security capability",
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
        records = decode_stock_connect_active_security_batch(batch.payload, channel=channel)
        if not records:
            return StockConnectActiveSecuritySyncResult(
                channel=channel,
                data_version=None,
                inserted_count=0,
                unchanged_count=0,
                availability="empty",
            )
        published = self._repository.publish_active_securities(
            channel=channel,
            records=records,
            source=_archive_batch(batch=batch, payload_store=self._raw_payload_store),
        )
        return StockConnectActiveSecuritySyncResult(
            channel=published.channel,
            data_version=published.data_version,
            inserted_count=published.inserted_count,
            unchanged_count=published.unchanged_count,
        )


def decode_stock_connect_active_security_batch(
    payload: bytes, *, channel: StockConnectChannel
) -> tuple[StockConnectActiveSecurity, ...]:
    """解码活跃榜标准载荷，拒绝通道漂移、重复日期排名和未知排行/估算字段。"""
    try:
        decoded = json.loads(payload)
    except json.JSONDecodeError as error:
        raise _schema_error("stock-connect active-security payload is not JSON") from error
    if not isinstance(decoded, dict) or decoded.get("schema") != _SCHEMA:
        raise _schema_error("unexpected stock-connect active-security schema")
    if decoded.get("channel") != channel.channel or decoded.get("direction") != channel.direction:
        raise _schema_error("stock-connect active-security channel identity mismatch")
    if decoded.get("valueKind", "REPORTED") != "REPORTED":
        raise _schema_error("stock-connect P0 accepts only reported active securities")
    _reject_unknown(
        decoded,
        {"schema", "channel", "direction", "valueKind", "records"},
        "root",
    )
    values = decoded.get("records")
    if not isinstance(values, list):
        raise _schema_error("stock-connect active-security payload has no records")
    try:
        records = tuple(_record(value) for value in values)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise _schema_error("stock-connect active-security value is invalid") from error
    if len({(item.trade_date, item.rank_no) for item in records}) != len(records):
        raise _schema_error("stock-connect active-security payload has duplicate date ranks")
    return tuple(sorted(records, key=lambda item: (item.trade_date, item.rank_no)))


def _record(value: object) -> StockConnectActiveSecurity:
    """转换一行活跃榜原始事实，不把名次外的来源热度或预测字段纳入 P0。"""
    if not isinstance(value, dict):
        raise ValueError("stock-connect active-security record is not an object")
    _reject_unknown(
        value,
        {
            "instrumentCode",
            "tradeDate",
            "rankNo",
            "buyAmount",
            "sellAmount",
            "turnoverAmount",
            "currency",
        },
        "stock-connect active-security record",
    )
    return StockConnectActiveSecurity(
        source_instrument_code=_required(value, "instrumentCode"),
        trade_date=date.fromisoformat(_required(value, "tradeDate")),
        rank_no=int(_required(value, "rankNo")),
        buy_amount=_optional_decimal(value.get("buyAmount")),
        sell_amount=_optional_decimal(value.get("sellAmount")),
        turnover_amount=_optional_decimal(value.get("turnoverAmount")),
        currency=_required(value, "currency"),
    )


def _required(value: dict[str, object], key: str) -> str:
    """读取非空文本并拒绝常见缺失字面量，避免它们成为跨市场身份或排名。"""
    normalized = _optional_text(value.get(key))
    if normalized is None:
        raise ValueError(f"{key} is required")
    return normalized


def _optional_text(value: object) -> str | None:
    """保留真正空值，禁止把 pandas 缺失字面量写成来源字段内容。"""
    if value is None:
        return None
    normalized = str(value).strip()
    return None if not normalized or normalized.lower() in {"nan", "none", "nat"} else normalized


def _optional_decimal(value: object) -> Decimal | None:
    """使用精确十进制读取可选金额，零值仍是来源披露事实。"""
    normalized = _optional_text(value)
    return None if normalized is None else Decimal(normalized)


def _reject_unknown(value: dict[str, object], allowed: set[str], location: str) -> None:
    """拒绝未治理字段，阻止供应商估算流量或未来收益无审计地进入标准对象。"""
    unknown = set(value).difference(allowed)
    if unknown:
        raise ValueError(f"unexpected {location} fields: {', '.join(sorted(unknown))}")


def _schema_error(message: str) -> ProviderError:
    """构造不可重试 schema 错误，字段漂移必须先经过独立映射与质量评审。"""
    return ProviderError(ProviderErrorCode.SCHEMA, message, retryable=False)
