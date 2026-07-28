"""基于 AKShare 固定版本交易所接口的显式上市生命周期适配器。"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import akshare as ak

from service_data_sync.application.ports.data_source import (
    ProviderBatch,
    ProviderError,
    ProviderErrorCode,
    SourceRequest,
)
from service_data_sync.domain.equity import Exchange

_CAPABILITY = "equity.lifecycle.explicit"
_SCHEMA = "quant-v2.equity-lifecycle-explicit.v1"
_ADAPTER_VERSION = "akshare-1.18.78-official-exchange-lifecycle-v1"
_SHANGHAI = ZoneInfo("Asia/Shanghai")


class AkshareExchangeEquityLifecycleAdapter:
    """从沪深终止上市历史和北交所在册上市日输出中立生命周期事实。"""

    provider_id = "akshare-official-exchange-equity-lifecycle"

    def __init__(self, *, request_timeout_seconds: int, client: Any = ak) -> None:
        """保存固定 AKShare client 与单次阻塞请求墙钟预算，便于 fixture 注入。"""
        self._request_timeout_seconds = request_timeout_seconds
        self._client = client

    def capabilities(self) -> frozenset[str]:
        """声明唯一显式生命周期能力。"""
        return frozenset({_CAPABILITY})

    async def fetch(self, request: SourceRequest) -> ProviderBatch:
        """调用交易所对应接口，归档原始行并输出可重放的标准生命周期 JSON。"""
        if request.capability != _CAPABILITY:
            raise ProviderError(
                ProviderErrorCode.INVALID_REQUEST,
                "unsupported capability",
                retryable=False,
            )
        exchange, target_date = _request_values(request)
        if target_date != datetime.now(_SHANGHAI).date():
            raise ProviderError(
                ProviderErrorCode.INVALID_REQUEST,
                "equity lifecycle source supports only the current Shanghai date",
                retryable=False,
            )
        try:
            async with asyncio.timeout(self._request_timeout_seconds):
                frame = await asyncio.to_thread(self._fetch_frame, exchange)
        except TimeoutError as error:
            raise ProviderError(
                ProviderErrorCode.UNAVAILABLE,
                "provider request timed out",
                retryable=True,
            ) from error
        except ProviderError:
            raise
        except Exception as error:
            raise ProviderError(
                ProviderErrorCode.UNAVAILABLE,
                "provider request failed",
                retryable=True,
            ) from error
        if frame.empty:
            raise ProviderError(
                ProviderErrorCode.SCHEMA,
                "provider returned an empty lifecycle dataset",
                retryable=False,
            )
        try:
            raw_records = frame.to_dict(orient="records")
            entries = _normalize_entries(raw_records, exchange=exchange)
        except (KeyError, TypeError, ValueError) as error:
            raise ProviderError(
                ProviderErrorCode.SCHEMA,
                "provider lifecycle schema changed",
                retryable=False,
            ) from error
        if not entries:
            raise ProviderError(
                ProviderErrorCode.SCHEMA,
                "provider returned no A-share lifecycle entries",
                retryable=False,
            )
        payload = json.dumps(
            {
                "schema": _SCHEMA,
                "exchange": exchange.value,
                "entries": entries,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        raw_payload = json.dumps(
            {"records": raw_records},
            ensure_ascii=False,
            default=_json_default,
            separators=(",", ":"),
        ).encode()
        return ProviderBatch(
            provider_id=self.provider_id,
            capability=_CAPABILITY,
            payload=payload,
            observed_at=datetime.now(UTC),
            content_type="application/vnd.quant-v2.equity-lifecycle-explicit+json",
            raw_payload=raw_payload,
            raw_content_type="application/json",
            upstream_source=_upstream_source(exchange),
            adapter_version=_ADAPTER_VERSION,
            schema_fingerprint=_schema_fingerprint(raw_records, exchange=exchange),
        )

    def _fetch_frame(self, exchange: Exchange) -> Any:
        """在 adapter 内选择固定版本真实函数及参数，禁止调用方感知 SDK 名称。"""
        if exchange is Exchange.SSE:
            # 1.18.78 源码查询 `COMPANY_STATUS=3` 的终止上市集合，但误把
            # `DELIST_DATE` 重命名为“暂停上市日期”；适配器按原字段语义映射退市。
            return self._client.stock_info_sh_delist("全部")
        if exchange is Exchange.SZSE:
            return self._client.stock_info_sz_delist("终止上市公司")
        return self._client.stock_info_bj_name_code()


def _request_values(request: SourceRequest) -> tuple[Exchange, date]:
    """解析中立请求，拒绝缺失交易所、非法目标日和额外来源参数。"""
    parameters = dict(request.parameters)
    if len(request.parameters) != 2 or set(parameters) != {"exchange", "targetDate"}:
        raise ProviderError(
            ProviderErrorCode.INVALID_REQUEST,
            "invalid equity lifecycle request",
            retryable=False,
        )
    try:
        return Exchange(parameters["exchange"]), date.fromisoformat(parameters["targetDate"])
    except (KeyError, ValueError) as error:
        raise ProviderError(
            ProviderErrorCode.INVALID_REQUEST,
            "invalid equity lifecycle request",
            retryable=False,
        ) from error


def _normalize_entries(
    records: list[dict[str, Any]], *, exchange: Exchange
) -> list[dict[str, str | None]]:
    """按交易所真实表头映射显式事实，过滤 B 股且不从名称或目录缺席推断状态。"""
    if exchange is Exchange.BSE:
        entries = [_bse_listing_entry(record) for record in records]
    elif exchange is Exchange.SSE:
        entries = [
            _delisting_entry(
                record,
                symbol_key="公司代码",
                listed_on_key="上市日期",
                delisted_on_key="暂停上市日期",
            )
            for record in records
        ]
    else:
        entries = [
            _delisting_entry(
                record,
                symbol_key="证券代码",
                listed_on_key="上市日期",
                delisted_on_key="终止上市日期",
            )
            for record in records
        ]
    filtered = [entry for entry in entries if _is_a_share_symbol(str(entry["symbol"]), exchange)]
    deduplicated = {
        (str(entry["symbol"]), str(entry["effectiveOn"]), str(entry["status"])): entry
        for entry in filtered
    }
    if len(deduplicated) != len(filtered):
        raise ValueError("provider lifecycle rows contain duplicate facts")
    return sorted(
        deduplicated.values(),
        key=lambda entry: (str(entry["effectiveOn"]), str(entry["symbol"])),
    )


def _delisting_entry(
    record: dict[str, Any],
    *,
    symbol_key: str,
    listed_on_key: str,
    delisted_on_key: str,
) -> dict[str, str | None]:
    """把沪深交易所终止上市行映射为带官方生效日的 DELISTED 事实。"""
    symbol = _symbol(record[symbol_key])
    listed_on = _optional_date(record.get(listed_on_key))
    delisted_on = _required_date(record[delisted_on_key])
    return {
        "symbol": symbol,
        "status": "DELISTED",
        "effectiveOn": delisted_on.isoformat(),
        "evidenceKind": "EXPLICIT_DELISTING",
        "listedOn": None if listed_on is None else listed_on.isoformat(),
        "delistedOn": delisted_on.isoformat(),
        "correctionApprovalReference": None,
    }


def _bse_listing_entry(record: dict[str, Any]) -> dict[str, str | None]:
    """把北交所在册列表中的官方上市日映射为 EXPLICIT_LISTING，不推断缺席证券。"""
    listed_on = _required_date(record["上市日期"])
    return {
        "symbol": _symbol(record["证券代码"]),
        "status": "LISTED",
        "effectiveOn": listed_on.isoformat(),
        "evidenceKind": "EXPLICIT_LISTING",
        "listedOn": listed_on.isoformat(),
        "delistedOn": None,
        "correctionApprovalReference": None,
    }


def _symbol(value: object) -> str:
    """将可能被 pandas 表示为数值的证券代码无损恢复为六位字符串。"""
    text = str(value).strip()
    if text.endswith(".0"):
        text = text[:-2]
    symbol = text.zfill(6)
    if len(symbol) != 6 or not symbol.isdigit():
        raise ValueError("invalid equity symbol")
    return symbol


def _required_date(value: object) -> date:
    """解析必需官方日期，NaT、空值和非 ISO 值均触发 schema 隔离。"""
    parsed = _optional_date(value)
    if parsed is None:
        raise ValueError("required lifecycle date is missing")
    return parsed


def _optional_date(value: object | None) -> date | None:
    """解析可空官方日期，不把缺失值替换为观测日。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text or text.lower() in {"nan", "nat", "none"}:
        return None
    return date.fromisoformat(text[:10])


def _is_a_share_symbol(symbol: str, exchange: Exchange) -> bool:
    """只接受三所 A 股代码段，避免交易所返回的 B 股混入同一身份域。"""
    if exchange is Exchange.SSE:
        return symbol.startswith(("60", "68"))
    if exchange is Exchange.SZSE:
        return symbol.startswith(("00", "30"))
    return symbol.startswith(("4", "8", "92"))


def _upstream_source(exchange: Exchange) -> str:
    """记录 adapter 背后的交易所数据集，不用 provider-neutral 名称抹除方法来源。"""
    return {
        Exchange.SSE: "sse.terminated-listing",
        Exchange.SZSE: "szse.terminated-listing",
        Exchange.BSE: "bse.listed-company",
    }[exchange]


def _schema_fingerprint(records: list[dict[str, Any]], *, exchange: Exchange) -> str:
    """对交易所、固定映射版本和原始表头做哈希，字段漂移时阻断发布。"""
    keys = sorted({str(key) for record in records for key in record})
    return hashlib.sha256(
        json.dumps(
            {
                "exchange": exchange.value,
                "mapping": _ADAPTER_VERSION,
                "keys": keys,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _json_default(value: object) -> str:
    """序列化 pandas 日期和数值展示值，raw evidence 不做业务字段改写。"""
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)
