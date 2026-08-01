"""经由 `AKShare` 东财接口提供 A 股报表、指标与历史估值的隔离适配器。

三大报表、供应商指标和日频估值是三个独立 `capability`：它们的报告期、单位和修订
语义不同，不能在适配器中拼成同一张“财务表”。宽表中的来源列会被投影为带原始
单位和空值原因的事实，未知币种、审计状态或数值域保持未知，不根据列名之外的经验
猜测 `canonical` 口径。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from math import isnan
from typing import Any

import akshare as ak
import requests

from service_data_sync.application.ports.data_source import (
    ProviderBatch,
    ProviderError,
    ProviderErrorCode,
    SourceRequest,
)
from service_data_sync.domain.equity import Exchange

_STATEMENT_CAPABILITY = "financial.statement.raw"
_METRIC_CAPABILITY = "financial.metric.raw"
_VALUATION_CAPABILITY = "financial.valuation.raw"
_CAPABILITIES = frozenset({_STATEMENT_CAPABILITY, _METRIC_CAPABILITY, _VALUATION_CAPABILITY})
_SCHEMAS = {
    _STATEMENT_CAPABILITY: "quant-v2.financial-statement.v1",
    _METRIC_CAPABILITY: "quant-v2.financial-provider-metric.v1",
    _VALUATION_CAPABILITY: "quant-v2.financial-valuation.v1",
}
_ADAPTER_VERSION = "akshare-1.18.81-eastmoney-financial-v2"
_MAX_ATTEMPTS = 3
_BACKOFF_BASE_SECONDS = 1.0
_BACKOFF_JITTER_SECONDS = 0.5
_RECORD_METADATA = frozenset(
    {
        "SECUCODE",
        "SECURITY_CODE",
        "SECURITY_NAME_ABBR",
        "ORG_CODE",
        "SECURITY_INNER_CODE",
        "REPORT_DATE",
        "REPORTDATE",
        "REPORT_TYPE",
        "REPORT_DATE_NAME",
        "NOTICE_DATE",
        "UPDATE_DATE",
        "PUBLISH_DATE",
        "CURRENCY",
        "MONETARY_UNIT",
        "DATE_TYPE_CODE",
        "FISCAL_YEAR",
        "OPINION_TYPE",
        "LISTING_STATE",
        "ACCOUNTING_STANDARDS",
        "TRADE_MARKET_CODE",
        "SECURITY_TYPE_CODE",
    }
)

# 这些可替换边界让退避与总预算可以确定性测试，生产默认仍使用真实单调时钟。
Sleeper = Callable[[float], Awaitable[None]]
MonotonicClock = Callable[[], float]
RandomSource = Callable[[], float]


class AkshareEastmoneyFinancialAdapter:
    """将东财宽表隔离成三类来源中立财务能力，保留原始表头与行记录。

    标准载荷供应用层严格解码，完整原始行仅随批次携带供失败证据与 `schema` 漂移审计。
    """

    provider_id = "akshare-eastmoney-financial"

    def __init__(
        self,
        *,
        request_timeout_seconds: int,
        max_concurrency: int = 1,
        requests_per_minute: int | None = None,
        sleeper: Sleeper = asyncio.sleep,
        monotonic: MonotonicClock = time.monotonic,
        random_source: RandomSource = random.random,
    ) -> None:
        """保存总预算、进程内并发与速率边界，并注入可测试的时钟和退避依赖。"""
        if request_timeout_seconds < 1:
            raise ValueError("request_timeout_seconds must be positive")
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be positive")
        if requests_per_minute is not None and requests_per_minute < 1:
            raise ValueError("requests_per_minute must be positive")
        self._request_timeout_seconds = request_timeout_seconds
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._rate_lock = asyncio.Lock()
        self._rate_interval_seconds = (
            0.0 if requests_per_minute is None else 60.0 / requests_per_minute
        )
        self._next_request_at = 0.0
        self._sleeper = sleeper
        self._monotonic = monotonic
        self._random_source = random_source

    def capabilities(self) -> frozenset[str]:
        """声明三大报表、供应商财务指标和历史估值能力。"""
        return _CAPABILITIES

    async def fetch(self, request: SourceRequest) -> ProviderBatch:
        """按 `capability` 调用唯一东财接口组合，返回标准载荷与完整原始证据。

        网络故障属于可重试的来源不可用；不支持的请求和宽表形状变化则必须阻止自动重试。
        """
        exchange, symbol = _request_identity(request)
        provider_symbol = _provider_symbol(exchange, symbol)
        deadline = self._monotonic() + self._request_timeout_seconds
        try:
            async with asyncio.timeout(self._request_timeout_seconds):
                async with self._semaphore:
                    payload_object, raw_object = await self._fetch_with_retry(
                        request=request,
                        exchange=exchange,
                        symbol=symbol,
                        provider_symbol=provider_symbol,
                        deadline=deadline,
                    )
        except TimeoutError as error:
            raise ProviderError(
                ProviderErrorCode.UNAVAILABLE,
                "provider request budget exhausted",
                retryable=True,
            ) from error
        payload = json.dumps(
            payload_object,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode()
        raw_payload = json.dumps(
            raw_object,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode()
        return ProviderBatch(
            provider_id=self.provider_id,
            capability=request.capability,
            payload=payload,
            observed_at=datetime.now(UTC),
            content_type=f"application/vnd.{_SCHEMAS[request.capability]}+json",
            raw_payload=raw_payload,
            raw_content_type="application/json",
            upstream_source="eastmoney.financial",
            adapter_version=_ADAPTER_VERSION,
            schema_fingerprint=_schema_fingerprint(raw_object),
        )

    async def _fetch_with_retry(
        self,
        *,
        request: SourceRequest,
        exchange: Exchange,
        symbol: str,
        provider_symbol: str,
        deadline: float,
    ) -> tuple[dict[str, object], dict[str, object]]:
        """只对网络、超时和 HTTP 5xx 做至多三次幂等抓取，并共享同一总预算。"""
        for attempt in range(_MAX_ATTEMPTS):
            await self._wait_for_rate_slot(deadline)
            try:
                return await asyncio.to_thread(
                    _fetch_payload,
                    capability=request.capability,
                    exchange=exchange,
                    symbol=symbol,
                    provider_symbol=provider_symbol,
                )
            except Exception as error:
                classified = _classify_upstream_error(error)
            if not _should_retry(classified) or attempt == _MAX_ATTEMPTS - 1:
                raise classified
            await self._pause_before_retry(attempt=attempt, deadline=deadline)
        raise _budget_exhausted_error()

    async def _wait_for_rate_slot(self, deadline: float) -> None:
        """在总预算内串行预留下一次供应商调用，避免重试绕过每分钟速率上限。"""
        if self._rate_interval_seconds == 0:
            return
        async with self._rate_lock:
            now = self._monotonic()
            delay = max(0.0, self._next_request_at - now)
            remaining = deadline - now
            if remaining <= 0 or delay >= remaining:
                raise _budget_exhausted_error()
            if delay > 0:
                await self._sleeper(delay)
            request_started_at = self._monotonic()
            self._next_request_at = max(self._next_request_at, request_started_at)
            self._next_request_at += self._rate_interval_seconds

    async def _pause_before_retry(self, *, attempt: int, deadline: float) -> None:
        """按指数退避叠加抖动；等待时间不能吃穿当前逻辑请求的总预算。"""
        exponential = _BACKOFF_BASE_SECONDS * (2**attempt)
        jitter = _BACKOFF_JITTER_SECONDS * max(0.0, min(1.0, self._random_source()))
        delay = exponential + jitter
        remaining = deadline - self._monotonic()
        if remaining <= 0 or delay >= remaining:
            raise _budget_exhausted_error()
        await self._sleeper(delay)


def _classify_upstream_error(error: Exception) -> ProviderError:
    """把 SDK/HTTP 异常收敛为稳定类别，未知运行时错误保持不可自动重试。"""
    if isinstance(error, ProviderError):
        return error
    if isinstance(error, requests.exceptions.HTTPError):
        status = error.response.status_code if error.response is not None else None
        if status is not None and 500 <= status <= 599:
            return ProviderError(
                ProviderErrorCode.UNAVAILABLE,
                "provider returned a server error",
                retryable=True,
            )
        if status in {401, 403}:
            return ProviderError(
                ProviderErrorCode.AUTHENTICATION,
                "provider rejected authentication",
                retryable=False,
            )
        if status == 429:
            return ProviderError(
                ProviderErrorCode.RATE_LIMITED,
                "provider rate limit reached",
                retryable=False,
            )
        return ProviderError(
            ProviderErrorCode.INVALID_REQUEST,
            "provider rejected the request",
            retryable=False,
        )
    if isinstance(
        error,
        (
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
            requests.exceptions.ChunkedEncodingError,
            ConnectionError,
            TimeoutError,
        ),
    ):
        return ProviderError(
            ProviderErrorCode.UNAVAILABLE,
            "provider network request failed",
            retryable=True,
        )
    return ProviderError(
        ProviderErrorCode.UNAVAILABLE,
        "provider request failed",
        retryable=False,
    )


def _should_retry(error: ProviderError) -> bool:
    """仅允许明确标记为暂时不可用的网络、超时或 5xx 失败进入 adapter 内重试。"""
    return error.code is ProviderErrorCode.UNAVAILABLE and error.retryable


def _budget_exhausted_error() -> ProviderError:
    """构造不泄露供应商正文、但允许控制面稍后重跑的总预算耗尽错误。"""
    return ProviderError(
        ProviderErrorCode.UNAVAILABLE,
        "provider request budget exhausted",
        retryable=True,
    )


def _request_identity(request: SourceRequest) -> tuple[Exchange, str]:
    """解析标准证券身份，并拒绝未声明能力或非六位 A 股代码。"""
    if request.capability not in _CAPABILITIES:
        raise ProviderError(
            ProviderErrorCode.INVALID_REQUEST,
            "unsupported capability",
            retryable=False,
        )
    parameters = dict(request.parameters)
    try:
        exchange = Exchange(parameters["exchange"])
        symbol = parameters["symbol"]
    except (KeyError, ValueError) as error:
        raise ProviderError(
            ProviderErrorCode.INVALID_REQUEST,
            "financial request requires exchange and symbol",
            retryable=False,
        ) from error
    if len(symbol) != 6 or not symbol.isdigit():
        raise ProviderError(
            ProviderErrorCode.INVALID_REQUEST,
            "financial request symbol must be a six-digit A-share code",
            retryable=False,
        )
    return exchange, symbol


def _provider_symbol(exchange: Exchange, symbol: str) -> str:
    """将标准交易所身份映射为东财报表接口要求的后缀代码，不混用行情代码格式。"""
    prefix = {Exchange.SSE: "SH", Exchange.SZSE: "SZ", Exchange.BSE: "BJ"}[exchange]
    return f"{prefix}{symbol}"


def _fetch_payload(
    *, capability: str, exchange: Exchange, symbol: str, provider_symbol: str
) -> tuple[dict[str, object], dict[str, object]]:
    """执行一个能力的同步 SDK 调用；供应商函数名和宽表字段仅存在于本 adapter。"""
    if capability == _STATEMENT_CAPABILITY:
        return _statement_payload(exchange=exchange, symbol=symbol, provider_symbol=provider_symbol)
    if capability == _METRIC_CAPABILITY:
        return _metric_payload(exchange=exchange, symbol=symbol, provider_symbol=provider_symbol)
    return _valuation_payload(exchange=exchange, symbol=symbol)


def _statement_payload(
    *, exchange: Exchange, symbol: str, provider_symbol: str
) -> tuple[dict[str, object], dict[str, object]]:
    """获取东财三张按报告期宽表；不使用年度或单季接口二次拼接，避免混合口径。

    三张表必须来自本次相同的证券请求；任一表为空即停止整个报表能力，不能以局部成功
    伪装成完整财务披露。
    """
    calls: tuple[tuple[str, Callable[..., Any]], ...] = (
        ("BALANCE_SHEET", ak.stock_balance_sheet_by_report_em),
        ("INCOME_STATEMENT", ak.stock_profit_sheet_by_report_em),
        ("CASH_FLOW_STATEMENT", ak.stock_cash_flow_sheet_by_report_em),
    )
    statements: list[dict[str, object]] = []
    raw_statements: list[dict[str, object]] = []
    for statement_type, function in calls:
        frame = function(symbol=provider_symbol)
        if frame.empty:
            raise ProviderError(
                ProviderErrorCode.SCHEMA,
                f"provider returned an empty {statement_type} statement",
                retryable=False,
            )
        columns, records = _frame_records(frame)
        statements.append(
            {
                "statementType": statement_type,
                "reports": _normalize_statement_records(statement_type, records),
            }
        )
        raw_statements.append(
            {"statementType": statement_type, "columns": columns, "records": records}
        )
    identity = {"exchange": exchange.value, "symbol": symbol}
    return (
        {"schema": _SCHEMAS[_STATEMENT_CAPABILITY], **identity, "statements": statements},
        {"capability": _STATEMENT_CAPABILITY, **identity, "statements": raw_statements},
    )


def _metric_payload(
    *, exchange: Exchange, symbol: str, provider_symbol: str
) -> tuple[dict[str, object], dict[str, object]]:
    """获取东财按报告期主要指标宽表，不把其与三表披露事实混写。"""
    frame = ak.stock_financial_analysis_indicator_em(
        symbol=_eastmoney_security_code(exchange, provider_symbol), indicator="按报告期"
    )
    if frame.empty:
        raise ProviderError(
            ProviderErrorCode.SCHEMA,
            "provider returned empty financial indicators",
            retryable=False,
        )
    columns, records = _frame_records(frame)
    identity = {"exchange": exchange.value, "symbol": symbol}
    return (
        {
            "schema": _SCHEMAS[_METRIC_CAPABILITY],
            **identity,
            "metrics": _normalize_metric_records(records),
        },
        {
            "capability": _METRIC_CAPABILITY,
            **identity,
            "columns": columns,
            "records": records,
        },
    )


def _valuation_payload(
    *, exchange: Exchange, symbol: str
) -> tuple[dict[str, object], dict[str, object]]:
    """获取东财日频历史估值；一次响应同时覆盖市值、PE、PB 和市现率。"""
    frame = ak.stock_value_em(symbol=symbol)
    if frame.empty:
        raise ProviderError(
            ProviderErrorCode.SCHEMA,
            "provider returned empty valuation history",
            retryable=False,
        )
    columns, records = _frame_records(frame)
    identity = {"exchange": exchange.value, "symbol": symbol}
    return (
        {
            "schema": _SCHEMAS[_VALUATION_CAPABILITY],
            **identity,
            "valuations": _normalize_valuation_records(records),
        },
        {
            "capability": _VALUATION_CAPABILITY,
            **identity,
            "columns": columns,
            "records": records,
        },
    )


def _eastmoney_security_code(exchange: Exchange, provider_symbol: str) -> str:
    """生成主要指标接口要求的 `symbol.EXCHANGE` 代码，和三表接口的前缀格式分开处理。"""
    suffix = {Exchange.SSE: "SH", Exchange.SZSE: "SZ", Exchange.BSE: "BJ"}[exchange]
    return f"{provider_symbol[2:]}.{suffix}"


def _frame_records(frame: Any) -> tuple[list[str], list[dict[str, object | None]]]:
    """将 pandas 宽表安全转为 JSON 记录，保留列顺序供 raw evidence 与 schema 漂移审计。"""
    columns = [str(column) for column in frame.columns]
    records = [
        {str(key): _json_value(value) for key, value in record.items()}
        for record in frame.to_dict(orient="records")
    ]
    return columns, records


def _normalize_statement_records(
    statement_type: str, records: list[dict[str, object | None]]
) -> list[dict[str, object]]:
    """将东财宽表投影为报表头和事实数组；供应商表头不越过 adapter 边界。"""
    reports: list[dict[str, object]] = []
    for record in records:
        report_period = _required_date(record, "REPORT_DATE", "REPORTDATE")
        reports.append(
            {
                "reportPeriod": report_period.isoformat(),
                "periodBasis": "POINT_IN_TIME"
                if statement_type == "BALANCE_SHEET"
                else "YEAR_TO_DATE",
                "statementScope": _statement_scope(record),
                "currency": _currency(record.get("CURRENCY")),
                "currencyNullReason": "UNKNOWN_SOURCE"
                if _currency(record.get("CURRENCY")) is None
                else None,
                "reportType": _optional_text(record.get("REPORT_TYPE")) or "UNKNOWN",
                "announcementDate": _date_text(_optional_date(record.get("NOTICE_DATE"))),
                "providerUpdateAt": _optional_timestamp(record.get("UPDATE_DATE")),
                "auditStatus": _audit_status(record.get("OPINION_TYPE")),
                "facts": _normalize_facts(record, namespace=f"statement.{statement_type.lower()}"),
            }
        )
    return reports


def _normalize_metric_records(records: list[dict[str, object | None]]) -> list[dict[str, object]]:
    """将东财主要指标宽表投影为报告期指标数组，与三表事实保持独立能力。"""
    metrics: list[dict[str, object]] = []
    for record in records:
        report_period = _required_date(record, "REPORT_DATE", "REPORTDATE")
        for fact in _normalize_facts(record, namespace="provider_metric"):
            if fact["value"] is None:
                continue
            metrics.append(
                {
                    "reportPeriod": report_period.isoformat(),
                    "periodBasis": "YEAR_TO_DATE",
                    "statementScope": "UNKNOWN",
                    "currency": None,
                    "currencyNullReason": "UNKNOWN_SOURCE",
                    "metric": fact,
                }
            )
    return metrics


def _normalize_valuation_records(
    records: list[dict[str, object | None]],
) -> list[dict[str, object]]:
    """映射东财历史估值固定五项；不把未定义的 `PEG`、`PS` 等额外字段混入当前合同。

    市值以 `CNY` 计量；`PE`、`PB` 和市现率是无币种比例，不能当作可相加的货币金额。
    """
    fields = (
        ("总市值", "market_cap", "总市值", "monetary", "CNY", None),
        ("PE(TTM)", "pe_ttm", "市盈率（TTM）", "ratio", None, "NOT_APPLICABLE"),
        ("PE(静)", "pe_static", "市盈率（静态）", "ratio", None, "NOT_APPLICABLE"),
        ("市净率", "pb", "市净率", "ratio", None, "NOT_APPLICABLE"),
        ("市现率", "pcf", "市现率", "ratio", None, "NOT_APPLICABLE"),
    )
    valuations: list[dict[str, object]] = []
    for record in records:
        observation_date = _required_date(record, "数据日期")
        for source_field, code, label, domain, currency, currency_null_reason in fields:
            value = _decimal_text(record.get(source_field))
            if value is None:
                continue
            valuations.append(
                {
                    "observationDate": observation_date.isoformat(),
                    "code": code,
                    "label": label,
                    "value": value,
                    "valueDomain": domain,
                    "unit": "CNY" if domain == "monetary" else "ratio",
                    "currency": currency,
                    "currencyNullReason": currency_null_reason,
                }
            )
    return valuations


def _normalize_facts(
    record: dict[str, object | None], *, namespace: str
) -> list[dict[str, object | None]]:
    """把宽表中的数值列投影为中立事实；空数值保留 `UPSTREAM_NULL`，文本列不伪造成数值。

    每个事实带来源字段标签和保守的数值域，便于后续词典审核后再赋予精确单位或会计语义。
    """
    facts: list[dict[str, object | None]] = []
    for source_field, raw_value in record.items():
        if source_field in _RECORD_METADATA:
            continue
        value = _decimal_text(raw_value)
        # 非空但无法转换的展示文本不是零也不是空披露，不能静默进入数值事实。
        if value is None and raw_value is not None:
            continue
        code_suffix = _field_code(source_field, max_length=80 - len(namespace) - 1)
        facts.append(
            {
                "code": f"{namespace}.{code_suffix}",
                "label": source_field,
                "value": value,
                "nullReason": "UPSTREAM_NULL" if value is None else None,
                "valueDomain": _value_domain(source_field),
                "originalUnit": "source_unknown",
                "canonicalUnit": "source_unknown",
                "scaleFactor": "1",
                "signConvention": "provider_as_reported",
                "currency": None,
                "currencyNullReason": "UNKNOWN_SOURCE",
            }
        )
    return facts


def _required_date(record: dict[str, object | None], *keys: str) -> date:
    """读取东财报告或估值日期；缺失日期使整份载荷进入 schema 隔离而非猜测。"""
    for key in keys:
        value = _optional_date(record.get(key))
        if value is not None:
            return value
    raise ProviderError(ProviderErrorCode.SCHEMA, "provider record has no date", retryable=False)


def _optional_date(value: object | None) -> date | None:
    """解析供应商日期展示值，接受 ISO 日期和带时间的 ISO 时间，不接受模糊文本。"""
    text = _optional_text(value)
    if text is None:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError as error:
        raise ProviderError(
            ProviderErrorCode.SCHEMA, "provider date is invalid", retryable=False
        ) from error


def _optional_timestamp(value: object | None) -> str | None:
    """保留供应商显式更新时间的 ISO 文本；仅在可解析时下传为标准 UTC/带偏移时间。"""
    text = _optional_text(value)
    if text is None:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return None if parsed.tzinfo is None else parsed.isoformat()
    except ValueError as error:
        raise ProviderError(
            ProviderErrorCode.SCHEMA,
            "provider update timestamp is invalid",
            retryable=False,
        ) from error


def _date_text(value: date | None) -> str | None:
    """将可空来源日期投影为标准 JSON 字符串，避免中立载荷泄漏 Python 日期对象。"""
    return None if value is None else value.isoformat()


def _optional_text(value: object | None) -> str | None:
    """读取非空来源展示文本，并将 pandas 空标量统一为缺失。"""
    if value is None:
        return None
    text = str(value).strip()
    return None if text in {"", "<NA>", "NaT", "nan", "None", "--"} else text


def _decimal_text(value: object | None) -> str | None:
    """把来源数值转为十进制文本；非数值文本不进入事实或指标。"""
    text = _optional_text(value)
    if text is None:
        return None
    try:
        return format(Decimal(text.replace(",", "")), "f")
    except Exception:
        return None


def _field_code(source_field: str, *, max_length: int) -> str:
    """生成受长度上限约束的稳定 ASCII 字段代码，确保完整命名空间仍可写入字段字典。"""
    if max_length < 22:
        raise ValueError("financial metric code suffix limit is too small")
    normalized = "".join(
        character.lower() if character.isascii() and character.isalnum() else "-"
        for character in source_field
    ).strip("-")
    if normalized:
        return normalized[:max_length]
    return f"field-{hashlib.sha256(source_field.encode()).hexdigest()[:16]}"


def _value_domain(source_field: str) -> str:
    """仅按字段名称标记明显每股或比例；其余保守保留为来源未知数值域。"""
    uppercase = source_field.upper()
    if any(token in uppercase for token in ("EPS", "BPS", "MG", "PER_SHARE")):
        return "per_share"
    if any(token in uppercase for token in ("YOY", "QOQ", "RATE", "RATIO", "ROE", "ROA")):
        return "ratio"
    return "other"


def _currency(value: object | None) -> str | None:
    """仅把明确 CNY/RMB 表达映射为 ISO 代码；其他币种或空值一律保持未知。"""
    text = _optional_text(value)
    return "CNY" if text is not None and text.upper() in {"CNY", "RMB", "人民币"} else None


def _statement_scope(record: dict[str, object | None]) -> str:
    """从东财报告类型中的明确合并或母公司字样提取范围，缺失时保持 `UNKNOWN`。"""
    report_type = _optional_text(record.get("REPORT_TYPE")) or ""
    if "合并" in report_type:
        return "CONSOLIDATED"
    if "母公司" in report_type or "母公司报表" in report_type:
        return "PARENT"
    return "UNKNOWN"


def _audit_status(value: object | None) -> str:
    """仅在东财审计意见显式存在时标记审计，否则不从报告名称推断。"""
    return "AUDITED" if _optional_text(value) is not None else "UNKNOWN"


def _json_value(value: object) -> object | None:
    """将 pandas 空值、时间和精确数字转换为可重复 JSON，不允许 NaN 进入 evidence。"""
    item = getattr(value, "item", None)
    if callable(item):
        value = item()
    if value is None:
        return None
    if isinstance(value, float) and isnan(value):
        return None
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    text = str(value)
    return None if text in {"<NA>", "NaT", "nan", "None"} else value


def _schema_fingerprint(raw_object: dict[str, object]) -> str:
    """按能力与完整表头集合生成 SHA-256，列漂移可由后续 quarantine 精确归因。"""
    statements = raw_object.get("statements")
    layout = (
        [
            {
                "statementType": statement.get("statementType"),
                "columns": statement.get("columns"),
            }
            for statement in statements
            if isinstance(statement, dict)
        ]
        if isinstance(statements, list)
        else raw_object.get("columns")
    )
    return hashlib.sha256(
        json.dumps(
            {"capability": raw_object.get("capability"), "layout": layout},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
