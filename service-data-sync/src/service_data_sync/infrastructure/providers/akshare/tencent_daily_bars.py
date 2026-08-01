"""经由 `AKShare SDK` 获取腾讯 A 股未复权日线的适配器。

上游字段只在本模块出现：标准载荷使用交易日、元计价 `OHLC`、`CNY` 成交额和股数成交量。
腾讯偶发把成交量按“手”返回，适配器仅在成交额反推的 `VWAP` 能与当日高低价对账时才
换算为股；不能对账的响应以 `schema` 错误隔离，绝不靠猜测写入 `canonical` 日线。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import akshare as ak

from service_data_sync.application.ports.data_source import (
    ProviderBatch,
    ProviderError,
    ProviderErrorCode,
    SourceRequest,
)
from service_data_sync.domain.equity import EquityIdentifier, Exchange

_CAPABILITY = "equity.bar.1d.raw"
_SCHEMA = "quant-v2.equity-daily-bar.v1"
_AKSHARE_VERSION = "1.18.81"
_ADAPTER_VERSION = "akshare-1.18.81-stock_zh_a_hist_tx-v2"
_UPSTREAM_SOURCE = "tencent-stock-kline"
_EXPECTED_COLUMNS = (
    "date",
    "open",
    "close",
    "high",
    "low",
    "volume",
    "turnover",
    "amount",
)
_SCHEMA_FINGERPRINT = hashlib.sha256(
    json.dumps(
        {
            "sdk": "akshare",
            "sdkVersion": _AKSHARE_VERSION,
            "function": "stock_zh_a_hist_tx",
            "columns": _EXPECTED_COLUMNS,
            "units": {
                "volume": "share",
                "amount": "CNY",
                "turnover": "ratio",
            },
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
).hexdigest()


class AkshareTencentDailyBarsAdapter:
    """获取腾讯 A 股未复权日线，并产出标准化的数据源无关批次。

    该适配器不提供复权、周线或月线能力，防止不同供应商及聚合口径被错误混用。
    """

    provider_id = "akshare-tencent"
    supported_exchanges = frozenset({Exchange.SSE, Exchange.SZSE})

    def __init__(self, *, request_timeout_seconds: int) -> None:
        """为阻塞式 AKShare 请求设置受限的墙钟超时。"""
        self._request_timeout_seconds = request_timeout_seconds

    def capabilities(self) -> frozenset[str]:
        """仅声明本适配器支持的未复权个股日线能力。"""
        return frozenset({_CAPABILITY})

    async def fetch(self, request: SourceRequest) -> ProviderBatch:
        """获取一个包含端日线窗口，并将上游失败隔离为中立错误。

        原始行与标准载荷一并交给应用层归档；只有两份对象都成功固化后才能发布窗口覆盖。
        """
        if request.capability != _CAPABILITY:
            raise ProviderError(
                ProviderErrorCode.INVALID_REQUEST, "unsupported capability", retryable=False
            )
        if getattr(ak, "__version__", None) != _AKSHARE_VERSION:
            raise ProviderError(
                ProviderErrorCode.SCHEMA,
                "unsupported AKShare SDK version",
                retryable=False,
            )
        identifier, start, end = _request_values(request)
        try:
            async with asyncio.timeout(self._request_timeout_seconds):
                # AKShare 调用会阻塞；移出事件循环，同时保留任务级超时。
                frame = await asyncio.to_thread(
                    ak.stock_zh_a_hist_tx,
                    symbol=_tencent_symbol(identifier),
                    start_date=start.strftime("%Y%m%d"),
                    end_date=end.strftime("%Y%m%d"),
                    adjust="",
                    timeout=float(self._request_timeout_seconds),
                )
        except TimeoutError as error:
            raise ProviderError(
                ProviderErrorCode.UNAVAILABLE, "provider request timed out", retryable=True
            ) from error
        except Exception as error:
            raise ProviderError(
                ProviderErrorCode.UNAVAILABLE, "provider request failed", retryable=True
            ) from error
        try:
            # 空窗口只有在 SDK 仍返回冻结列集合时才是可证明的业务空集；无列响应属于 schema 漂移。
            if tuple(str(column) for column in frame.columns) != _EXPECTED_COLUMNS:
                raise ValueError("provider columns do not match frozen schema")
            raw_records = frame.to_dict(orient="records")
            bars = [_normalize_record(record) for record in raw_records]
        except (KeyError, TypeError, ValueError, InvalidOperation) as error:
            raise ProviderError(
                ProviderErrorCode.SCHEMA, "provider daily-bar schema changed", retryable=False
            ) from error
        # 标准载荷不得携带供应商专有结构。
        # 原始字段结构单独保存为可审计证据。
        payload = json.dumps(
            {
                "schema": _SCHEMA,
                "instrument": identifier.qualified_symbol,
                "bars": bars,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        raw_payload = json.dumps(
            {"instrument": identifier.qualified_symbol, "records": raw_records},
            ensure_ascii=False,
            default=_json_default,
            separators=(",", ":"),
        ).encode()
        return ProviderBatch(
            provider_id=self.provider_id,
            capability=request.capability,
            payload=payload,
            observed_at=datetime.now(UTC),
            content_type="application/vnd.quant-v2.equity-daily-bar+json",
            raw_payload=raw_payload,
            raw_content_type="application/json",
            upstream_source=_UPSTREAM_SOURCE,
            adapter_version=_ADAPTER_VERSION,
            schema_fingerprint=_SCHEMA_FINGERPRINT,
        )


def _request_values(request: SourceRequest) -> tuple[EquityIdentifier, date, date]:
    """解析中立请求参数，不向上层暴露供应商参数名。"""
    parameters = dict(request.parameters)
    try:
        identifier = EquityIdentifier.parse(parameters["instrument"])
        start = date.fromisoformat(parameters["start"])
        end = date.fromisoformat(parameters["end"])
    except (KeyError, ValueError) as error:
        raise ProviderError(
            ProviderErrorCode.INVALID_REQUEST, "invalid daily-bar request", retryable=False
        ) from error
    if identifier.exchange not in AkshareTencentDailyBarsAdapter.supported_exchanges:
        raise ProviderError(
            ProviderErrorCode.CURRENTLY_UNSUPPORTED,
            "Tencent daily bars do not support this exchange",
            retryable=False,
        )
    if start > end:
        raise ProviderError(
            ProviderErrorCode.INVALID_REQUEST, "invalid date range", retryable=False
        )
    return identifier, start, end


def _tencent_symbol(identifier: EquityIdentifier) -> str:
    """将标准交易所身份映射为仅限本适配器使用的腾讯代码。"""
    prefix = {
        Exchange.SSE: "sh",
        Exchange.SZSE: "sz",
    }[identifier.exchange]
    return f"{prefix}{identifier.symbol}"


def _normalize_record(record: dict[str, Any]) -> dict[str, str | None]:
    """将腾讯/AKShare 字段转换为标准值，并修复已识别的“手”单位。

    价格和金额均以精确十进制文本传给应用层，不能使用二进制浮点改变哈希或发布判断。
    """
    open_price = _decimal(record["open"])
    high_price = _decimal(record["high"])
    low_price = _decimal(record["low"])
    close_price = _decimal(record["close"])
    volume_shares = int(_decimal(record["volume"]))
    amount_cny = _decimal(record["amount"])
    # 先保留来源整数，再以可审计的价格—金额恒等式判断其究竟是股还是手。
    normalized_volume = _normalize_volume_shares(
        volume_shares=volume_shares,
        amount_cny=amount_cny,
        low_price=low_price,
        high_price=high_price,
    )
    return {
        "tradeDate": _iso_date(record["date"]),
        "open": str(open_price),
        "high": str(high_price),
        "low": str(low_price),
        "close": str(close_price),
        "volumeShares": str(normalized_volume),
        "amountCny": str(amount_cny),
        "turnoverRate": str(_decimal(record["turnover"])),
    }


def _normalize_volume_shares(
    *,
    volume_shares: int,
    amount_cny: Decimal,
    low_price: Decimal,
    high_price: Decimal,
) -> int:
    """仅当 VWAP 证明“股”口径不一致时，修复按“手”返回的成交量。"""
    if volume_shares == 0 or amount_cny == 0:
        return volume_shares
    # 原始成交量推导的 VWAP 与当日价格区间一致时，必须保留“股”口径。
    if _vwap_in_range(amount_cny / Decimal(volume_shares), low_price, high_price):
        return volume_shares
    lot_normalized_volume = volume_shares * 100
    # 腾讯偶发按“手”返回；仅在换算后 VWAP 能对账时才乘以 100。
    if _vwap_in_range(amount_cny / Decimal(lot_normalized_volume), low_price, high_price):
        return lot_normalized_volume
    raise ValueError("volume unit does not reconcile with OHLC and amount")


def _vwap_in_range(vwap: Decimal, low_price: Decimal, high_price: Decimal) -> bool:
    """接受落在当日高低价区间上下各 1% 容差内的 VWAP。"""
    return low_price * Decimal("0.99") <= vwap <= high_price * Decimal("1.01")


def _decimal(value: object) -> Decimal:
    """经由文本转换供应商数值，避免二进制浮点写入漂移。"""
    return Decimal(str(value))


def _iso_date(value: object) -> str:
    """将 pandas、`datetime` 与字符串日期单元规范为 ISO 日历文本。"""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return date.fromisoformat(str(value)).isoformat()


def _json_default(value: object) -> str:
    """序列化归档中的 pandas 日期类值，保留其展示值。"""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)
