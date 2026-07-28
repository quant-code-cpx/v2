"""通过 AKShare 1.18.78 隔离东财与同花顺资金流接口及供应商字段。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
from collections.abc import Callable
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

_EQUITY_DAILY = "money_flow.order_size.daily.equity.raw"
_SECTOR_DAILY = "money_flow.order_size.daily.sector.raw"
_MARKET_DAILY = "money_flow.order_size.daily.market.raw"
_EQUITY_RANKING = "money_flow.order_size.ranking.equity.raw"
_SECTOR_RANKING = "money_flow.order_size.ranking.sector.raw"
_EASTMONEY_CAPABILITIES = frozenset(
    {_EQUITY_DAILY, _SECTOR_DAILY, _MARKET_DAILY, _EQUITY_RANKING, _SECTOR_RANKING}
)
_TRADE_EQUITY_RANKING = "money_flow.trade_direction.ranking.equity.raw"
_TRADE_INDUSTRY_RANKING = "money_flow.trade_direction.ranking.industry.raw"
_TRADE_CONCEPT_RANKING = "money_flow.trade_direction.ranking.concept.raw"
_THS_CAPABILITIES = frozenset(
    {_TRADE_EQUITY_RANKING, _TRADE_INDUSTRY_RANKING, _TRADE_CONCEPT_RANKING}
)
_DAILY_SCHEMA = "quant-v2.money-flow-daily.v1"
_RANKING_SCHEMA = "quant-v2.money-flow-ranking.v1"
_ORDER_BUCKETS = (
    ("main", "主力"),
    ("super_large", "超大单"),
    ("large", "大单"),
    ("medium", "中单"),
    ("small", "小单"),
)
_EASTMONEY_ADAPTER_VERSION = "akshare-1.18.78-eastmoney-money-flow-v1"
_THS_ADAPTER_VERSION = "akshare-1.18.78-ths-money-flow-v1"

FrameFetcher = Callable[..., Any]


class AkshareEastmoneyMoneyFlowAdapter:
    """把东财订单规模日序列与 supplier ranking 归一为中立 batch。"""

    provider_id = "akshare-eastmoney-money-flow"

    def __init__(
        self,
        *,
        request_timeout_seconds: int,
        equity_daily_fetcher: FrameFetcher = ak.stock_individual_fund_flow,
        sector_daily_fetcher: FrameFetcher = ak.stock_sector_fund_flow_hist,
        market_daily_fetcher: FrameFetcher = ak.stock_market_fund_flow,
        equity_ranking_fetcher: FrameFetcher = ak.stock_individual_fund_flow_rank,
        sector_ranking_fetcher: FrameFetcher = ak.stock_sector_fund_flow_rank,
    ) -> None:
        """保存有界调用时间与可替换 SDK 函数，便于 fixture 和失败注入。"""
        self._request_timeout_seconds = request_timeout_seconds
        self._equity_daily_fetcher = equity_daily_fetcher
        self._sector_daily_fetcher = sector_daily_fetcher
        self._market_daily_fetcher = market_daily_fetcher
        self._equity_ranking_fetcher = equity_ranking_fetcher
        self._sector_ranking_fetcher = sector_ranking_fetcher

    def capabilities(self) -> frozenset[str]:
        """声明订单规模日序列和供应商排行能力。"""
        return _EASTMONEY_CAPABILITIES

    async def fetch(self, request: SourceRequest) -> ProviderBatch:
        """执行唯一匹配的 SDK 调用并同时返回标准 JSON 与完整 DataFrame raw。"""
        if request.capability not in _EASTMONEY_CAPABILITIES:
            raise _invalid_request("unsupported money-flow capability")
        parameters = dict(request.parameters)
        try:
            async with asyncio.timeout(self._request_timeout_seconds):
                frame = await asyncio.to_thread(self._fetch_frame, request.capability, parameters)
        except TimeoutError as error:
            raise ProviderError(
                ProviderErrorCode.UNAVAILABLE,
                "money-flow provider request timed out",
                retryable=True,
            ) from error
        except ProviderError:
            raise
        except Exception as error:
            raise ProviderError(
                ProviderErrorCode.UNAVAILABLE,
                "money-flow provider request failed",
                retryable=True,
            ) from error
        return _provider_batch(
            provider_id=self.provider_id,
            capability=request.capability,
            frame=frame,
            normalized=self._normalize(request.capability, parameters, frame),
            upstream_source="eastmoney.money-flow",
            adapter_version=_EASTMONEY_ADAPTER_VERSION,
        )

    def _fetch_frame(self, capability: str, parameters: dict[str, str]) -> Any:
        """把中立 capability 映射到固定版本的精确 AKShare 函数签名。"""
        if capability == _EQUITY_DAILY:
            exchange = _required(parameters, "exchange").upper()
            market = {"SSE": "sh", "SZSE": "sz", "BSE": "bj"}.get(exchange)
            if market is None:
                raise _invalid_request("unsupported equity exchange")
            return self._equity_daily_fetcher(stock=_six_digit_symbol(parameters), market=market)
        if capability == _SECTOR_DAILY:
            if _required(parameters, "scheme") != "eastmoney.industry":
                raise _invalid_request("sector daily source supports eastmoney.industry only")
            return self._sector_daily_fetcher(symbol=_required(parameters, "sectorName"))
        if capability == _MARKET_DAILY:
            if _required(parameters, "marketCode") != "cn-a":
                raise _invalid_request("market daily source supports cn-a only")
            return self._market_daily_fetcher()
        indicator = _required(parameters, "indicator")
        if capability == _EQUITY_RANKING:
            if indicator not in {"今日", "3日", "5日", "10日"}:
                raise _invalid_request("equity ranking indicator is invalid")
            return self._equity_ranking_fetcher(indicator=indicator)
        sector_type = _required(parameters, "sectorType")
        if indicator not in {"今日", "5日", "10日"} or sector_type not in {
            "行业资金流",
            "概念资金流",
            "地域资金流",
        }:
            raise _invalid_request("sector ranking parameters are invalid")
        return self._sector_ranking_fetcher(indicator=indicator, sector_type=sector_type)

    def _normalize(
        self, capability: str, parameters: dict[str, str], frame: Any
    ) -> dict[str, object]:
        """校验固定表头并转换为不含中文供应商字段的标准载荷。"""
        if getattr(frame, "empty", True):
            raise ProviderError(
                ProviderErrorCode.SCHEMA,
                "money-flow provider returned an empty frame",
                retryable=False,
            )
        records = frame.to_dict(orient="records")
        try:
            if capability in {_EQUITY_DAILY, _SECTOR_DAILY, _MARKET_DAILY}:
                return _normalize_order_size_daily(capability, parameters, records)
            return _normalize_order_size_ranking(capability, parameters, records)
        except (KeyError, TypeError, ValueError, InvalidOperation) as error:
            raise ProviderError(
                ProviderErrorCode.SCHEMA,
                "money-flow provider schema changed",
                retryable=False,
            ) from error


class AkshareThsMoneyFlowAdapter:
    """把同花顺交易方向即时或滚动排行隔离成独立方法学 batch。"""

    provider_id = "akshare-ths-money-flow"

    def __init__(
        self,
        *,
        request_timeout_seconds: int,
        equity_fetcher: FrameFetcher = ak.stock_fund_flow_individual,
        industry_fetcher: FrameFetcher = ak.stock_fund_flow_industry,
        concept_fetcher: FrameFetcher = ak.stock_fund_flow_concept,
    ) -> None:
        """保存调用预算与可替换的 HTTP/JS SDK 函数。"""
        self._request_timeout_seconds = request_timeout_seconds
        self._fetchers = {
            _TRADE_EQUITY_RANKING: equity_fetcher,
            _TRADE_INDUSTRY_RANKING: industry_fetcher,
            _TRADE_CONCEPT_RANKING: concept_fetcher,
        }

    def capabilities(self) -> frozenset[str]:
        """声明三个不能与东财订单规模序列混合的交易方向排行能力。"""
        return _THS_CAPABILITIES

    async def fetch(self, request: SourceRequest) -> ProviderBatch:
        """执行同花顺 SDK 调用，保留带万/亿后缀 raw 并按版本化规则换算。"""
        if request.capability not in _THS_CAPABILITIES:
            raise _invalid_request("unsupported money-flow capability")
        parameters = dict(request.parameters)
        indicator = _required(parameters, "indicator")
        if indicator not in {"即时", "3日排行", "5日排行", "10日排行", "20日排行"}:
            raise _invalid_request("ths money-flow indicator is invalid")
        try:
            async with asyncio.timeout(self._request_timeout_seconds):
                frame = await asyncio.to_thread(
                    self._fetchers[request.capability], symbol=indicator
                )
        except TimeoutError as error:
            raise ProviderError(
                ProviderErrorCode.UNAVAILABLE,
                "ths money-flow provider request timed out",
                retryable=True,
            ) from error
        except ProviderError:
            raise
        except Exception as error:
            raise ProviderError(
                ProviderErrorCode.UNAVAILABLE,
                "ths money-flow provider request failed",
                retryable=True,
            ) from error
        if getattr(frame, "empty", True):
            raise ProviderError(
                ProviderErrorCode.SCHEMA,
                "ths money-flow provider returned an empty frame",
                retryable=False,
            )
        try:
            normalized = _normalize_ths_ranking(
                request.capability, parameters, frame.to_dict(orient="records")
            )
        except (KeyError, TypeError, ValueError, InvalidOperation) as error:
            raise ProviderError(
                ProviderErrorCode.SCHEMA,
                "ths money-flow provider schema changed",
                retryable=False,
            ) from error
        return _provider_batch(
            provider_id=self.provider_id,
            capability=request.capability,
            frame=frame,
            normalized=normalized,
            upstream_source="10jqka.money-flow",
            adapter_version=_THS_ADAPTER_VERSION,
        )


def _normalize_order_size_daily(
    capability: str,
    parameters: dict[str, str],
    records: list[dict[str, object]],
) -> dict[str, object]:
    """把东财五个 bucket 的日历史转换为固定四度量列。"""
    observations: list[dict[str, object]] = []
    for record in records:
        trade_date = _date_text(record["日期"])
        for bucket_code, vendor_label in _ORDER_BUCKETS:
            observations.append(
                {
                    "tradeDate": trade_date,
                    "bucket": bucket_code,
                    "grossInflow": None,
                    "grossOutflow": None,
                    "netAmount": _decimal_text(record[f"{vendor_label}净流入-净额"]),
                    # 来源返回百分数；canonical 统一换算为十进制比率。
                    "netRatio": _ratio_text(record[f"{vendor_label}净流入-净占比"]),
                }
            )
    scope = _daily_scope(capability, parameters)
    return {
        "schema": _DAILY_SCHEMA,
        "methodologyKey": "eastmoney-order-size",
        "methodologyVersion": "1",
        "scope": scope,
        "universe": _daily_universe(capability),
        "observations": observations,
    }


def _normalize_order_size_ranking(
    capability: str,
    parameters: dict[str, str],
    records: list[dict[str, object]],
) -> dict[str, object]:
    """把东财合并 DataFrame 排行保留为未验证完整性的供应商快照。"""
    indicator = parameters["indicator"]
    prefix = indicator
    window_size = {"今日": 1, "3日": 3, "5日": 5, "10日": 10}[indicator]
    items: list[dict[str, object]] = []
    for fallback_position, record in enumerate(records, start=1):
        metrics = [
            {
                "bucket": bucket_code,
                "grossInflow": None,
                "grossOutflow": None,
                "netAmount": _decimal_text(record[f"{prefix}{vendor_label}净流入-净额"]),
                "netRatio": _ratio_text(record[f"{prefix}{vendor_label}净流入-净占比"]),
            }
            for bucket_code, vendor_label in _ORDER_BUCKETS
        ]
        if capability == _EQUITY_RANKING:
            scope = {
                "scopeType": "equity",
                "sourceSymbol": str(record["代码"]).zfill(6),
                "name": _text(record["名称"]),
            }
        else:
            scope = {
                "scopeType": "sector",
                "scheme": _sector_scheme(parameters["sectorType"]),
                "sourceName": _text(record["名称"]),
            }
        items.append(
            {
                "supplierPosition": _supplier_position(
                    record.get("序号"),
                    fallback=fallback_position,
                ),
                "scope": scope,
                "metrics": metrics,
            }
        )
    return {
        "schema": _RANKING_SCHEMA,
        "methodologyKey": "eastmoney-order-size",
        "methodologyVersion": "1",
        "targetTradeDate": date.fromisoformat(parameters["targetDate"]).isoformat(),
        "scopeType": "equity" if capability == _EQUITY_RANKING else "sector",
        "universe": parameters.get("universe", "cn-a"),
        "windowType": "supplier_day" if window_size == 1 else "supplier_rolling",
        "windowSize": window_size,
        "rankingBucket": "main",
        "rankingBasis": "supplier_reported_order",
        "completenessBasis": "sdk_returned",
        "isComplete": False,
        "items": items,
    }


def _normalize_ths_ranking(
    capability: str,
    parameters: dict[str, str],
    records: list[dict[str, object]],
) -> dict[str, object]:
    """解析同花顺带中文倍率金额，滚动排行仍不伪装为日历史。"""
    indicator = parameters["indicator"]
    window_size = 1 if indicator == "即时" else int(indicator.removesuffix("日排行"))
    items: list[dict[str, object]] = []
    for fallback_position, record in enumerate(records, start=1):
        if capability == _TRADE_EQUITY_RANKING:
            scope = {
                "scopeType": "equity",
                "sourceSymbol": str(record["股票代码"]).zfill(6),
                "name": _text(record["股票简称"]),
            }
        else:
            scope = {
                "scopeType": "sector",
                "scheme": (
                    "10jqka.industry" if capability == _TRADE_INDUSTRY_RANKING else "10jqka.concept"
                ),
                "sourceName": _text(record["行业"]),
            }
        if indicator == "即时":
            gross_inflow = _scaled_cny_text(record["流入资金"])
            gross_outflow = _scaled_cny_text(record["流出资金"])
            net_amount = _scaled_cny_text(record["净额"])
        else:
            gross_inflow = None
            gross_outflow = None
            net_amount = _scaled_cny_text(
                record["资金流入净额"] if capability == _TRADE_EQUITY_RANKING else record["净额"]
            )
        items.append(
            {
                "supplierPosition": _supplier_position(
                    record.get("序号"),
                    fallback=fallback_position,
                ),
                "scope": scope,
                "metrics": [
                    {
                        "bucket": "all",
                        "grossInflow": gross_inflow,
                        "grossOutflow": gross_outflow,
                        "netAmount": net_amount,
                        "netRatio": None,
                    }
                ],
            }
        )
    return {
        "schema": _RANKING_SCHEMA,
        "methodologyKey": "10jqka-trade-direction",
        "methodologyVersion": "1",
        "targetTradeDate": date.fromisoformat(parameters["targetDate"]).isoformat(),
        "scopeType": "equity" if capability == _TRADE_EQUITY_RANKING else "sector",
        "universe": parameters.get("universe", "provider-page"),
        "windowType": "supplier_day" if window_size == 1 else "supplier_rolling",
        "windowSize": window_size,
        "rankingBucket": "all",
        "rankingBasis": "supplier_reported_order",
        "completenessBasis": "sdk_returned",
        "isComplete": False,
        "items": items,
    }


def _provider_batch(
    *,
    provider_id: str,
    capability: str,
    frame: Any,
    normalized: dict[str, object],
    upstream_source: str,
    adapter_version: str,
) -> ProviderBatch:
    """序列化标准载荷与 raw 记录，并冻结完整表头 fingerprint。"""
    raw_object = {
        "columns": [str(column) for column in frame.columns],
        "records": [
            {str(key): _raw_json_value(value) for key, value in record.items()}
            for record in frame.to_dict(orient="records")
        ],
    }
    raw_payload = json.dumps(
        raw_object,
        ensure_ascii=False,
        allow_nan=False,
        default=_json_default,
        separators=(",", ":"),
    ).encode()
    payload = json.dumps(
        normalized,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode()
    fingerprint = hashlib.sha256(
        json.dumps(raw_object["columns"], ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    return ProviderBatch(
        provider_id=provider_id,
        capability=capability,
        payload=payload,
        observed_at=datetime.now(UTC),
        content_type="application/vnd.quant-v2.money-flow+json",
        raw_payload=raw_payload,
        raw_content_type="application/json",
        upstream_source=upstream_source,
        adapter_version=adapter_version,
        schema_fingerprint=fingerprint,
    )


def _daily_scope(capability: str, parameters: dict[str, str]) -> dict[str, str]:
    """构造不含供应商字段的日序列 scope 身份。"""
    if capability == _EQUITY_DAILY:
        return {
            "scopeType": "equity",
            "exchange": _required(parameters, "exchange").upper(),
            "symbol": _six_digit_symbol(parameters),
        }
    if capability == _SECTOR_DAILY:
        return {
            "scopeType": "sector",
            "scheme": _required(parameters, "scheme"),
            "sectorCode": _required(parameters, "sectorCode"),
            "name": _required(parameters, "sectorName"),
        }
    return {"scopeType": "market", "marketCode": _required(parameters, "marketCode")}


def _daily_universe(capability: str) -> str:
    """返回来源自身定义的 universe，不从下级 scope 聚合推断。"""
    return "cn-a" if capability != _SECTOR_DAILY else "eastmoney-industry"


def _sector_scheme(sector_type: str) -> str:
    """把东财排行类别映射为稳定 scheme，地域类别保持独立身份。"""
    return {
        "行业资金流": "eastmoney.industry",
        "概念资金流": "eastmoney.concept",
        "地域资金流": "eastmoney.region",
    }[sector_type]


def _six_digit_symbol(parameters: dict[str, str]) -> str:
    """读取并校验六位 A 股来源代码。"""
    symbol = _required(parameters, "symbol")
    if len(symbol) != 6 or not symbol.isdigit():
        raise _invalid_request("money-flow symbol must contain six digits")
    return symbol


def _required(parameters: dict[str, str], key: str) -> str:
    """读取非空请求参数，并转换为中立不可重试错误。"""
    value = parameters.get(key, "").strip()
    if not value:
        raise _invalid_request(f"money-flow request requires {key}")
    return value


def _invalid_request(message: str) -> ProviderError:
    """构造 provider-neutral 的不可重试请求错误。"""
    return ProviderError(ProviderErrorCode.INVALID_REQUEST, message, retryable=False)


def _decimal_text(value: object) -> str | None:
    """把来源数值转换为无科学计数法的十进制字符串。"""
    if value is None or _text(value) in {"", "-", "--", "nan", "None", "<NA>"}:
        return None
    decimal_value = Decimal(_text(value).replace(",", ""))
    return format(decimal_value, "f")


def _ratio_text(value: object) -> str | None:
    """把来源百分数转换为 canonical 十进制比率。"""
    decimal_text = _decimal_text(value)
    return None if decimal_text is None else format(Decimal(decimal_text) / Decimal(100), "f")


def _scaled_cny_text(value: object) -> str | None:
    """按同花顺原始万/亿后缀换算为 CNY，并拒绝未知倍率。"""
    text = _text(value).replace(",", "").replace(" ", "")
    if text in {"", "-", "--", "nan", "None", "<NA>"}:
        return None
    multipliers = {"万": Decimal(10_000), "亿": Decimal(100_000_000)}
    multiplier = Decimal(1)
    if text[-1:] in multipliers:
        multiplier = multipliers[text[-1]]
        text = text[:-1]
    elif text[-1:] and not (text[-1].isdigit() or text[-1] == "."):
        raise ValueError("unsupported money-flow amount suffix")
    return format(Decimal(text) * multiplier, "f")


def _date_text(value: object) -> str:
    """规范化 pandas 日期或 ISO 文本，并拒绝无法解析的日期。"""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return date.fromisoformat(_text(value)[:10]).isoformat()


def _supplier_position(value: object, *, fallback: int) -> int:
    """规范供应商排行位置；空序号使用稳定页序，非法或非正位置直接拒绝。"""
    normalized = _text(value)
    if normalized in {"", "nan", "None", "<NA>"}:
        return fallback
    position = int(normalized)
    if position <= 0:
        raise ValueError("money-flow supplier position must be positive")
    return position


def _text(value: object) -> str:
    """将 pandas 标量保守转换为去空白文本。"""
    return str(value).strip()


def _json_default(value: object) -> str:
    """归档 pandas 与 Decimal 等非原生 JSON 标量时保留文本证据。"""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def _raw_json_value(value: object) -> object:
    """把 raw 中的非有限浮点改为 JSON null，其余标量保留原值或文本证据。"""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    item = getattr(value, "item", None)
    if callable(item):
        scalar = item()
        if isinstance(scalar, float) and not math.isfinite(scalar):
            return None
        if isinstance(scalar, (str, int, float, bool)) or scalar is None:
            return scalar
    return str(value)


__all__ = [
    "AkshareEastmoneyMoneyFlowAdapter",
    "AkshareThsMoneyFlowAdapter",
]
