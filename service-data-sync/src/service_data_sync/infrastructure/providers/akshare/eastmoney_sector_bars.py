"""经由 `AKShare` 获取东财行业、概念板块目录与原生历史行情的适配器。

目录、日线、周线、月线是四项独立能力。行业与概念上游的周期字面量不同，模块在此
显式映射，不能在应用层假设两者相同或从日线聚合。成交额以 `CNY` 输出，但成交量单位
没有可靠的横向可比承诺，故保留为来源原生值并标记为 `provider_native`。
"""

from __future__ import annotations

import asyncio
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
from service_data_sync.domain.sector import SectorIdentifier, SectorPeriod, SectorScheme

_CAPABILITIES = frozenset(
    {
        "sector.catalog.raw",
        "sector.bar.1d.raw",
        "sector.bar.1w.raw",
        "sector.bar.1mo.raw",
    }
)
_SCHEMA = "quant-v2.sector-bar.v1"
_CATALOG_SCHEMA = "quant-v2.sector-catalog.v1"
_INDUSTRY_PERIODS = {
    SectorPeriod.DAY_1: "日k",
    SectorPeriod.WEEK_1: "周k",
    SectorPeriod.MONTH_1: "月k",
}
_CONCEPT_PERIODS = {
    SectorPeriod.DAY_1: "daily",
    SectorPeriod.WEEK_1: "weekly",
    SectorPeriod.MONTH_1: "monthly",
}


class AkshareEastmoneySectorBarsAdapter:
    """调用东财板块目录与历史接口，分别获取目录和原生三周期 K 线。

    它不根据目录缺席推断板块停用，也不对来源的百分数展示字段擅自换成小数比例。
    """

    provider_id = "akshare-eastmoney-sector"

    def __init__(self, *, request_timeout_seconds: int) -> None:
        """保存阻塞式 AKShare 调用可使用的受限墙钟超时。"""
        self._request_timeout_seconds = request_timeout_seconds

    def capabilities(self) -> frozenset[str]:
        """声明行业和概念板块的目录及三个独立历史周期。"""
        return _CAPABILITIES

    async def fetch(self, request: SourceRequest) -> ProviderBatch:
        """获取一个目录或板块周期窗口，并将 `SDK` 失败隔离为中立错误。

        标准 JSON 与完整原始行共同返回：前者供应用层严格解码，后者仅供失败留证审计。
        """
        if request.capability == "sector.catalog.raw":
            return await self._fetch_catalog(request)
        identifier, period, start, end = _request_values(request)
        try:
            async with asyncio.timeout(self._request_timeout_seconds):
                frame = await asyncio.to_thread(
                    _fetch_history,
                    identifier=identifier,
                    period=period,
                    start=start,
                    end=end,
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
                ProviderErrorCode.SCHEMA, "provider returned no sector bars", retryable=False
            )
        try:
            raw_records = frame.to_dict(orient="records")
            bars = [_normalize_record(record) for record in raw_records]
        except (KeyError, TypeError, ValueError, InvalidOperation) as error:
            raise ProviderError(
                ProviderErrorCode.SCHEMA, "provider sector-bar schema changed", retryable=False
            ) from error
        # 标准载荷不泄漏 SDK 行结构；完整供应商记录另存为可审计原始证据。
        payload = json.dumps(
            {
                "schema": _SCHEMA,
                "sectorScheme": identifier.scheme.value,
                "sector": identifier.code,
                "period": period.value,
                "bars": bars,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        raw_payload = json.dumps(
            {
                "sectorScheme": identifier.scheme.value,
                "sector": identifier.code,
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
            content_type="application/vnd.quant-v2.sector-bar+json",
            raw_payload=raw_payload,
            raw_content_type="application/json",
        )

    async def _fetch_catalog(self, request: SourceRequest) -> ProviderBatch:
        """读取一个分类体系的目录快照，并保留完整供应商记录作为原始证据。"""
        scheme = _catalog_scheme(request)
        try:
            async with asyncio.timeout(self._request_timeout_seconds):
                frame = await asyncio.to_thread(_fetch_catalog, scheme=scheme)
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
                "provider returned no sector catalog entries",
                retryable=False,
            )
        try:
            raw_records = frame.to_dict(orient="records")
            entries = [_normalize_catalog_record(record) for record in raw_records]
        except (KeyError, TypeError, ValueError) as error:
            raise ProviderError(
                ProviderErrorCode.SCHEMA, "provider sector-catalog schema changed", retryable=False
            ) from error
        payload = json.dumps(
            {
                "schema": _CATALOG_SCHEMA,
                "sectorScheme": scheme.value,
                "sectors": entries,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        raw_payload = json.dumps(
            {"sectorScheme": scheme.value, "records": raw_records},
            ensure_ascii=False,
            default=_json_default,
            separators=(",", ":"),
        ).encode()
        return ProviderBatch(
            provider_id=self.provider_id,
            capability=request.capability,
            payload=payload,
            observed_at=datetime.now(UTC),
            content_type="application/vnd.quant-v2.sector-catalog+json",
            raw_payload=raw_payload,
            raw_content_type="application/json",
        )


def _request_values(request: SourceRequest) -> tuple[SectorIdentifier, SectorPeriod, date, date]:
    """解析中立参数，并验证其与独立周期能力名称一致。"""
    if request.capability not in _CAPABILITIES:
        raise ProviderError(
            ProviderErrorCode.INVALID_REQUEST, "unsupported capability", retryable=False
        )
    parameters = dict(request.parameters)
    try:
        identifier = SectorIdentifier(
            scheme=SectorScheme(parameters["sectorScheme"]), code=parameters["sector"]
        )
        period = SectorPeriod(parameters["period"])
        start = date.fromisoformat(parameters["start"])
        end = date.fromisoformat(parameters["end"])
    except (KeyError, ValueError) as error:
        raise ProviderError(
            ProviderErrorCode.INVALID_REQUEST, "invalid sector-bar request", retryable=False
        ) from error
    if start > end or request.capability != period.capability:
        raise ProviderError(
            ProviderErrorCode.INVALID_REQUEST,
            "invalid sector-bar date range or period",
            retryable=False,
        )
    return identifier, period, start, end


def _catalog_scheme(request: SourceRequest) -> SectorScheme:
    """解析仅含分类体系的目录请求，并拒绝夹带行情参数的错误能力。"""
    if request.capability != "sector.catalog.raw":
        raise ProviderError(
            ProviderErrorCode.INVALID_REQUEST, "unsupported capability", retryable=False
        )
    parameters = dict(request.parameters)
    try:
        return SectorScheme(parameters["sectorScheme"])
    except (KeyError, ValueError) as error:
        raise ProviderError(
            ProviderErrorCode.INVALID_REQUEST, "invalid sector-catalog request", retryable=False
        ) from error


def _fetch_history(
    *, identifier: SectorIdentifier, period: SectorPeriod, start: date, end: date
) -> Any:
    """将中立身份和周期映射到仅限此 adapter 的东财 AKShare 调用。"""
    request_dates = {
        "start_date": start.strftime("%Y%m%d"),
        "end_date": end.strftime("%Y%m%d"),
        "adjust": "",
    }
    if identifier.scheme is SectorScheme.EASTMONEY_INDUSTRY:
        return ak.stock_board_industry_hist_em(
            symbol=identifier.code,
            period=_INDUSTRY_PERIODS[period],
            **request_dates,
        )
    # 概念板块接口的周期字面量与行业接口不同；不得在应用层统一或从日线聚合。
    return ak.stock_board_concept_hist_em(
        symbol=identifier.code,
        period=_CONCEPT_PERIODS[period],
        **request_dates,
    )


def _fetch_catalog(*, scheme: SectorScheme) -> Any:
    """将中立分类体系映射为仅限本 adapter 的东财目录 SDK 调用。"""
    if scheme is SectorScheme.EASTMONEY_INDUSTRY:
        return ak.stock_board_industry_name_em()
    return ak.stock_board_concept_name_em()


def _normalize_record(record: dict[str, Any]) -> dict[str, str | None]:
    """将东财中文列名映射为明确单位和百分比语义的标准字段。

    `amplitudePercent`、`changePercent`、`turnoverPercent` 保持百分数展示口径，与个股
    小数换手率字段不能混用。
    """
    return {
        "periodEnd": _iso_date(record["日期"]),
        "open": str(_decimal(record["开盘"])),
        "high": str(_decimal(record["最高"])),
        "low": str(_decimal(record["最低"])),
        "close": str(_decimal(record["收盘"])),
        # 文档未保证行业和概念横向可比较的成交量单位，故标明为上游原生值。
        "volumeValue": str(_decimal(record["成交量"])),
        "volumeUnit": "provider_native",
        "amountCny": str(_decimal(record["成交额"])),
        "amplitudePercent": _optional_decimal_text(record.get("振幅")),
        "changePercent": _optional_decimal_text(record.get("涨跌幅")),
        "changeAmount": _optional_decimal_text(record.get("涨跌额")),
        "turnoverPercent": _optional_decimal_text(record.get("换手率")),
    }


def _normalize_catalog_record(record: dict[str, Any]) -> dict[str, str]:
    """将东财目录中文列名映射为不携带供应商字段的标准代码和名称。"""
    code = record["板块代码"]
    name = record["板块名称"]
    if not isinstance(code, str) or not isinstance(name, str):
        raise ValueError("provider catalog identity must be text")
    return {"code": code.strip(), "name": name.strip()}


def _decimal(value: object) -> Decimal:
    """经文本转换供应商数值，并拒绝 NaN、无穷或无法比较的值。"""
    decimal_value = Decimal(str(value))
    if not decimal_value.is_finite():
        raise ValueError("provider numeric value must be finite")
    return decimal_value


def _optional_decimal_text(value: object) -> str | None:
    """保留可空供应商字段，并将存在值转换为稳定精确小数字符串。"""
    return None if value is None else str(_decimal(value))


def _iso_date(value: object) -> str:
    """将 pandas、`datetime` 与字符串日期单元规范为 ISO 日历文本。"""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return date.fromisoformat(str(value)).isoformat()


def _json_default(value: object) -> str:
    """序列化原始归档中的 pandas 日期类值，保留展示含义。"""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)
