"""经由 `AKShare SDK` 直取东方财富个股周线与月线的适配器。

周线和月线是供应商原生周期，不从平台日线重新聚合。东财成交量原始单位为手，输出
前固定换算为股；换手率原始为百分数，输出前固定除以 100 成为小数比例。两种换算都
用成交额、`OHLC` 的 `VWAP` 对账，异常单位不能进入 `canonical` 数据。
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
from service_data_sync.domain.equity import EquityBarPeriod, EquityIdentifier

_CAPABILITIES = frozenset({"equity.bar.1w.raw", "equity.bar.1mo.raw"})
_SCHEMA = "quant-v2.equity-period-bar.v1"
_UPSTREAM_PERIOD = {
    EquityBarPeriod.WEEK_1: "weekly",
    EquityBarPeriod.MONTH_1: "monthly",
}


class AkshareEastmoneyEquityPeriodBarsAdapter:
    """分别调用 AKShare 周线、月线接口，不读取或聚合平台日线。

    它不声明日线能力，避免任务误把东财周月口径与腾讯日线来源混为同一数据集。
    """

    provider_id = "akshare-eastmoney-equity-period"

    def __init__(self, *, request_timeout_seconds: int) -> None:
        """保存阻塞式 AKShare 周/月请求的墙钟超时。"""
        self._request_timeout_seconds = request_timeout_seconds

    def capabilities(self) -> frozenset[str]:
        """声明两个独立上游周期能力。"""
        return _CAPABILITIES

    async def fetch(self, request: SourceRequest) -> ProviderBatch:
        """获取一个周线或月线包含端窗口，并输出中立标准载荷。"""
        identifier, period, start, end = _request_values(request)
        try:
            async with asyncio.timeout(self._request_timeout_seconds):
                # `period` 直接传给上游接口；本适配器没有任何日线读取或聚合依赖。
                frame = await asyncio.to_thread(
                    ak.stock_zh_a_hist,
                    symbol=identifier.symbol,
                    period=_UPSTREAM_PERIOD[period],
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
        if frame.empty:
            raise ProviderError(
                ProviderErrorCode.SCHEMA, "provider returned no period bars", retryable=False
            )
        try:
            raw_records = frame.to_dict(orient="records")
            bars = [_normalize_record(record) for record in raw_records]
        except (KeyError, TypeError, ValueError, InvalidOperation) as error:
            raise ProviderError(
                ProviderErrorCode.SCHEMA, "provider period-bar schema changed", retryable=False
            ) from error
        payload = json.dumps(
            {
                "schema": _SCHEMA,
                "instrument": identifier.qualified_symbol,
                "period": period.value,
                "bars": bars,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        raw_payload = json.dumps(
            {
                "instrument": identifier.qualified_symbol,
                "period": period.value,
                "records": raw_records,
            },
            ensure_ascii=False,
            default=_json_default,
            separators=(",", ":"),
        ).encode()
        return ProviderBatch(
            provider_id=self.provider_id,
            capability=request.capability,
            payload=payload,
            observed_at=datetime.now(UTC),
            content_type="application/vnd.quant-v2.equity-period-bar+json",
            raw_payload=raw_payload,
            raw_content_type="application/json",
            upstream_source="eastmoney-stock-kline",
            adapter_version="akshare-1.18.78-v1",
            schema_fingerprint=hashlib.sha256(
                json.dumps(sorted(raw_records[0]), ensure_ascii=False).encode()
            ).hexdigest(),
        )


def _request_values(
    request: SourceRequest,
) -> tuple[EquityIdentifier, EquityBarPeriod, date, date]:
    """解析中立身份、周期与日期，并要求能力名与周期严格一致。"""
    if request.capability not in _CAPABILITIES:
        raise ProviderError(
            ProviderErrorCode.INVALID_REQUEST, "unsupported capability", retryable=False
        )
    parameters = dict(request.parameters)
    try:
        identifier = EquityIdentifier.parse(parameters["instrument"])
        period = EquityBarPeriod(parameters["period"])
        start = date.fromisoformat(parameters["start"])
        end = date.fromisoformat(parameters["end"])
    except (KeyError, ValueError) as error:
        raise ProviderError(
            ProviderErrorCode.INVALID_REQUEST, "invalid period-bar request", retryable=False
        ) from error
    if period is EquityBarPeriod.DAY_1 or period.capability != request.capability or start > end:
        raise ProviderError(
            ProviderErrorCode.INVALID_REQUEST,
            "invalid period-bar capability or date range",
            retryable=False,
        )
    return identifier, period, start, end


def _normalize_record(record: dict[str, Any]) -> dict[str, str | None]:
    """将东财周期行情转换为股、元和小数换手率口径。

    ``volumeShares`` 是股、``amountCny`` 是人民币元、``turnoverRate`` 是 0 到 1 的比率；
    这些字段不能保留为供应商展示单位。
    """
    # 东财周期接口把成交量按手展示；先显式换算，再由 VWAP 守住单位一致性。
    volume_lots = int(_decimal(record["成交量"]))
    volume_shares = volume_lots * 100
    amount_cny = _decimal(record["成交额"])
    low_price = _decimal(record["最低"])
    high_price = _decimal(record["最高"])
    if volume_shares > 0 and amount_cny > 0:
        vwap = amount_cny / Decimal(volume_shares)
        if not low_price * Decimal("0.99") <= vwap <= high_price * Decimal("1.01"):
            raise ValueError("period-bar volume unit does not reconcile")
    turnover_percent = _optional_decimal(record.get("换手率"))
    return {
        "periodEnd": _iso_date(record["日期"]),
        "open": str(_decimal(record["开盘"])),
        "high": str(high_price),
        "low": str(low_price),
        "close": str(_decimal(record["收盘"])),
        "volumeShares": str(volume_shares),
        "amountCny": str(amount_cny),
        "turnoverRate": (
            None if turnover_percent is None else str(turnover_percent / Decimal("100"))
        ),
    }


def _decimal(value: object) -> Decimal:
    """经文本无损解析有限供应商数值。"""
    parsed = Decimal(str(value))
    if not parsed.is_finite():
        raise ValueError("provider numeric value must be finite")
    return parsed


def _optional_decimal(value: object) -> Decimal | None:
    """将 pandas 空值映射为真实空值，否则解析精确小数。"""
    if value is None or str(value).lower() in {"nan", "nat", "none", ""}:
        return None
    return _decimal(value)


def _iso_date(value: object) -> str:
    """将 pandas、日期时间或字符串规范为 ISO 日期。"""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return date.fromisoformat(str(value)).isoformat()


def _json_default(value: object) -> str:
    """序列化 raw evidence 中的日期和 pandas 标量展示值。"""
    if isinstance(value, datetime | date):
        return value.isoformat()
    return str(value)
