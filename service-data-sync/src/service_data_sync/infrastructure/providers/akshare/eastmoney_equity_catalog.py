"""经由 AKShare 东财全市场快照实现 A 股证券目录适配器。"""

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

_CAPABILITY = "equity.master.catalog"
_SCHEMA = "quant-v2.equity-master-catalog.v1"
_ADAPTER_VERSION = "akshare-1.18.78-eastmoney-spot-v1"
_SHANGHAI = ZoneInfo("Asia/Shanghai")


class AkshareEastmoneyEquityCatalogAdapter:
    """获取东财 A 股完整快照，并按交易所输出标准证券目录。"""

    provider_id = "akshare-eastmoney-equity-catalog"

    def __init__(self, *, request_timeout_seconds: int) -> None:
        """保存阻塞式 AKShare 调用可占用的最大墙钟时间。"""
        self._request_timeout_seconds = request_timeout_seconds

    def capabilities(self) -> frozenset[str]:
        """仅声明完整 A 股证券目录能力。"""
        return frozenset({_CAPABILITY})

    async def fetch(self, request: SourceRequest) -> ProviderBatch:
        """请求一次全市场现货快照，并隔离为指定交易所目录。"""
        if request.capability != _CAPABILITY:
            raise ProviderError(
                ProviderErrorCode.INVALID_REQUEST, "unsupported capability", retryable=False
            )
        exchange, target_date = _request_values(request)
        # `targetDate` 不是供应商请求参数；先拒绝历史请求，避免无效回补也消耗上游配额。
        if target_date != datetime.now(_SHANGHAI).date():
            raise ProviderError(
                ProviderErrorCode.INVALID_REQUEST,
                "spot catalog supports only the current Shanghai date",
                retryable=False,
            )
        try:
            async with asyncio.timeout(self._request_timeout_seconds):
                # AKShare SDK 调用会阻塞；移出事件循环并保留可取消的任务超时边界。
                frame = await asyncio.to_thread(ak.stock_zh_a_spot_em)
        except TimeoutError as error:
            raise ProviderError(
                ProviderErrorCode.UNAVAILABLE, "provider request timed out", retryable=True
            ) from error
        except Exception as error:
            raise ProviderError(
                ProviderErrorCode.UNAVAILABLE, "provider request failed", retryable=True
            ) from error
        if frame.empty:
            raise ProviderError(
                ProviderErrorCode.SCHEMA,
                "provider returned an empty equity catalog",
                retryable=False,
            )
        try:
            raw_records = frame.to_dict(orient="records")
            entries = _normalize_entries(raw_records, exchange=exchange)
        except (KeyError, TypeError, ValueError) as error:
            raise ProviderError(
                ProviderErrorCode.SCHEMA, "provider equity catalog schema changed", retryable=False
            ) from error
        if not entries:
            raise ProviderError(
                ProviderErrorCode.SCHEMA,
                "provider returned no entries for exchange",
                retryable=False,
            )
        payload = json.dumps(
            {"schema": _SCHEMA, "exchange": exchange.value, "entries": entries},
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
            capability=request.capability,
            payload=payload,
            observed_at=datetime.now(UTC),
            content_type="application/vnd.quant-v2.equity-master-catalog+json",
            raw_payload=raw_payload,
            raw_content_type="application/json",
            upstream_source="eastmoney",
            adapter_version=_ADAPTER_VERSION,
            schema_fingerprint=_schema_fingerprint(raw_records),
        )


def _request_values(request: SourceRequest) -> tuple[Exchange, date]:
    """解析中立请求值，拒绝 adapter 不能诚实满足的历史目录请求。"""
    parameters = dict(request.parameters)
    try:
        return Exchange(parameters["exchange"]), date.fromisoformat(parameters["targetDate"])
    except (KeyError, ValueError) as error:
        raise ProviderError(
            ProviderErrorCode.INVALID_REQUEST, "invalid equity catalog request", retryable=False
        ) from error


def _normalize_entries(
    records: list[dict[str, Any]], *, exchange: Exchange
) -> list[dict[str, str | None]]:
    """将东财中文字段归一化，并只保留目标交易所允许的六位股票代码。"""
    entries: list[dict[str, str | None]] = []
    for record in records:
        symbol = str(record["代码"]).zfill(6)
        if _exchange_for_symbol(symbol) is not exchange:
            continue
        name = str(record["名称"]).strip()
        if not name:
            raise ValueError("blank equity name")
        entries.append({"symbol": symbol, "name": name, "listedOn": None})
    return sorted(entries, key=lambda entry: str(entry["symbol"]))


def _exchange_for_symbol(symbol: str) -> Exchange:
    """按 A 股代码段划分交易所；非 A 股代码必须触发 schema 隔离。"""
    if len(symbol) != 6 or not symbol.isdigit():
        raise ValueError("invalid equity symbol")
    if symbol.startswith(("60", "68")):
        return Exchange.SSE
    if symbol.startswith(("4", "8", "92")):
        return Exchange.BSE
    if symbol.startswith(("00", "30")):
        return Exchange.SZSE
    raise ValueError("unsupported A-share symbol prefix")


def _schema_fingerprint(records: list[dict[str, Any]]) -> str:
    """记录原始表头集合哈希，以便上游列漂移被审计和隔离。"""
    keys = sorted({str(key) for record in records for key in record})
    return hashlib.sha256(
        json.dumps(keys, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def _json_default(value: object) -> str:
    """序列化归档中可能出现的 pandas 日期和数值展示值。"""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)
