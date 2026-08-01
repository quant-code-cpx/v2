"""经由 `AKShare SDK` 直取东方财富个股周线与月线的适配器。

周线和月线是供应商原生周期，不从平台日线重新聚合。东财成交量原始单位为手，输出
前固定换算为股；换手率原始为百分数，输出前固定除以 100 成为小数比例。两种换算都
用成交额、`OHLC` 的 `VWAP` 对账，异常单位不能进入 `canonical` 数据。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import akshare as ak
import requests

from service_data_sync.application.ports.data_source import (
    ProviderBatch,
    ProviderError,
    ProviderErrorCode,
    SourceRequest,
)
from service_data_sync.domain.equity import EquityBarPeriod, EquityIdentifier, Exchange

_CAPABILITIES = frozenset({"equity.bar.1w.raw", "equity.bar.1mo.raw"})
_SCHEMA = "quant-v2.equity-period-bar.v1"
_AKSHARE_VERSION = "1.18.81"
_ADAPTER_VERSION = "akshare-1.18.81-stock_zh_a_hist-v4"
_UPSTREAM_SOURCE = "eastmoney-stock-kline"
_EMPTY_PROOF_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
_EMPTY_PROOF_FIELDS_1 = "f1,f2,f3,f4,f5,f6"
_EMPTY_PROOF_FIELDS_2 = "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f116"
_EMPTY_PROOF_TOKEN = "7eea3edcaed734bea9cbfc24409ed989"
_EMPTY_PROOF_MAX_BYTES = 1024 * 1024
_EXPECTED_COLUMNS = (
    "日期",
    "股票代码",
    "开盘",
    "收盘",
    "最高",
    "最低",
    "成交量",
    "成交额",
    "振幅",
    "涨跌幅",
    "涨跌额",
    "换手率",
)
_SCHEMA_FINGERPRINT = hashlib.sha256(
    json.dumps(
        {
            "sdk": "akshare",
            "sdkVersion": _AKSHARE_VERSION,
            "function": "stock_zh_a_hist",
            "columns": _EXPECTED_COLUMNS,
            "units": {
                "volume": "lot",
                "amount": "CNY",
                "turnover": "percent",
            },
            "emptyProof": {
                "url": _EMPTY_PROOF_URL,
                "fields1": _EMPTY_PROOF_FIELDS_1,
                "fields2": _EMPTY_PROOF_FIELDS_2,
                "successRc": 0,
                "identityFields": ("data.market", "data.code"),
                "emptyField": "data.klines=[]",
            },
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
).hexdigest()
_UPSTREAM_PERIOD = {
    EquityBarPeriod.WEEK_1: "weekly",
    EquityBarPeriod.MONTH_1: "monthly",
}
_EMPTY_PROOF_PERIOD = {
    EquityBarPeriod.WEEK_1: "102",
    EquityBarPeriod.MONTH_1: "103",
}
# 东财网关偶发主动断连；只重试明确的传输异常，不能把坏字段或身份响应伪装成网络抖动。
_PERIOD_FETCH_MAX_ATTEMPTS = 3
_PERIOD_FETCH_RETRY_BASE_SECONDS = 0.5
_TRANSPORT_ERRORS = (
    requests.exceptions.ChunkedEncodingError,
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
    BrokenPipeError,
    ConnectionAbortedError,
    ConnectionResetError,
)


class _EmptyProofSchemaError(ValueError):
    """表示东财空窗复核响应不满足冻结成功合同。"""


class AkshareEastmoneyEquityPeriodBarsAdapter:
    """分别调用 AKShare 周线、月线接口，不读取或聚合平台日线。

    它不声明日线能力，避免任务误把东财周月口径与腾讯日线来源混为同一数据集。
    """

    provider_id = "akshare-eastmoney-equity-period"
    # 当前冻结的 `AKShare`/东财探针只证明沪深两市周期线可用；北交所必须由独立、已批准
    # 的来源合同（例如未来单独注册的来源）声明支持，不能因同一 SDK 名称而静默兜底。
    supported_exchanges = frozenset({Exchange.SSE, Exchange.SZSE})

    def __init__(self, *, request_timeout_seconds: int) -> None:
        """保存阻塞式 AKShare 周/月请求的墙钟超时。"""
        self._request_timeout_seconds = request_timeout_seconds

    def capabilities(self) -> frozenset[str]:
        """声明两个独立上游周期能力。"""
        return _CAPABILITIES

    async def fetch(self, request: SourceRequest) -> ProviderBatch:
        """获取一个周线或月线包含端窗口，并输出中立标准载荷。"""
        if getattr(ak, "__version__", None) != _AKSHARE_VERSION:
            raise ProviderError(
                ProviderErrorCode.SCHEMA,
                "unsupported AKShare SDK version",
                retryable=False,
            )
        identifier, period, start, end = _request_values(request)
        empty_raw_payload: bytes | None = None
        deadline = asyncio.get_running_loop().time() + self._request_timeout_seconds
        try:
            # `period` 直接传给上游接口；本适配器没有任何日线读取或聚合依赖。
            frame = await _call_with_transport_retry(
                deadline=deadline,
                operation=lambda timeout_seconds: ak.stock_zh_a_hist(
                    symbol=identifier.symbol,
                    period=_UPSTREAM_PERIOD[period],
                    start_date=start.strftime("%Y%m%d"),
                    end_date=end.strftime("%Y%m%d"),
                    adjust="",
                    timeout=timeout_seconds,
                ),
            )
            # 固定 SDK 在 `klines` 为空时返回无列 DataFrame；仅以同请求原始成功响应
            # 中的精确空列表补证，`data=null`、错证券或异常响应都不得变成合法空窗。
            if bool(frame.empty) and not tuple(str(column) for column in frame.columns):
                empty_raw_payload = await _call_with_transport_retry(
                    deadline=deadline,
                    operation=lambda timeout_seconds: _fetch_explicit_empty_evidence(
                        identifier=identifier,
                        period=period,
                        start=start,
                        end=end,
                        timeout_seconds=timeout_seconds,
                    ),
                )
        except TimeoutError as error:
            raise ProviderError(
                ProviderErrorCode.UNAVAILABLE, "provider request timed out", retryable=True
            ) from error
        except ProviderError:
            # 已分类错误具有数据合同语义，绝不能因传输重试包装成可重试不可用。
            raise
        except _EmptyProofSchemaError as error:
            raise ProviderError(
                ProviderErrorCode.SCHEMA,
                "provider empty-window proof changed",
                retryable=False,
            ) from error
        except Exception as error:
            raise ProviderError(
                ProviderErrorCode.UNAVAILABLE, "provider request failed", retryable=True
            ) from error
        try:
            if empty_raw_payload is not None:
                # SDK 丢失的列集合已经由同请求原始响应中的成功码、身份与空列表补齐。
                raw_records = []
            else:
                # 非空或带列空表都必须逐列匹配固定 SDK 的最终投影顺序。
                if tuple(str(column) for column in frame.columns) != _EXPECTED_COLUMNS:
                    raise ValueError("provider columns do not match frozen schema")
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
        raw_payload = (
            empty_raw_payload
            if empty_raw_payload is not None
            else json.dumps(
                {
                    "instrument": identifier.qualified_symbol,
                    "period": period.value,
                    "records": raw_records,
                },
                ensure_ascii=False,
                default=_json_default,
                separators=(",", ":"),
            ).encode()
        )
        return ProviderBatch(
            provider_id=self.provider_id,
            capability=request.capability,
            payload=payload,
            observed_at=datetime.now(UTC),
            content_type="application/vnd.quant-v2.equity-period-bar+json",
            raw_payload=raw_payload,
            raw_content_type="application/json",
            upstream_source=_UPSTREAM_SOURCE,
            adapter_version=_ADAPTER_VERSION,
            schema_fingerprint=_SCHEMA_FINGERPRINT,
        )


async def _call_with_transport_retry(
    *,
    deadline: float,
    operation: Callable[[float], Any],
) -> Any:
    """在单一总墙钟预算内重试东财的短暂传输中断。

    每次调用使用同一证券、周期与日期窗；`ProviderError`、字段解析和身份证明失败均不会重试，
    以防确定性坏响应延迟失败或被错误写成空数据。
    """
    loop = asyncio.get_running_loop()
    for attempt in range(_PERIOD_FETCH_MAX_ATTEMPTS):
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise TimeoutError("period-bar provider total request budget exhausted")
        try:
            async with asyncio.timeout(remaining):
                return await asyncio.to_thread(operation, remaining)
        except Exception as error:
            if (
                isinstance(error, ProviderError)
                or not isinstance(error, _TRANSPORT_ERRORS)
                or attempt + 1 >= _PERIOD_FETCH_MAX_ATTEMPTS
            ):
                raise
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError("period-bar provider total request budget exhausted") from error
            # 退避时间同样从总预算扣除，不能让三次尝试突破调用方配置的墙钟上限。
            delay = min(_PERIOD_FETCH_RETRY_BASE_SECONDS * (2**attempt), remaining)
            if delay > 0:
                await asyncio.sleep(delay)
    raise AssertionError("period-bar transport retry loop must return or raise")


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
    if identifier.exchange not in AkshareEastmoneyEquityPeriodBarsAdapter.supported_exchanges:
        # 未经证明的交易所不能触发网络请求；调用方会据冻结来源支持矩阵记为
        # `SOURCE_EXCHANGE_UNAVAILABLE`，而不是把端点缺数写成合法零记录 coverage。
        raise ProviderError(
            ProviderErrorCode.CURRENTLY_UNSUPPORTED,
            "EastMoney period bars do not support this exchange in the frozen source contract",
            retryable=False,
        )
    if period is EquityBarPeriod.DAY_1 or period.capability != request.capability or start > end:
        raise ProviderError(
            ProviderErrorCode.INVALID_REQUEST,
            "invalid period-bar capability or date range",
            retryable=False,
        )
    return identifier, period, start, end


def _fetch_explicit_empty_evidence(
    *,
    identifier: EquityIdentifier,
    period: EquityBarPeriod,
    start: date,
    end: date,
    timeout_seconds: float,
) -> bytes:
    """复发固定 SDK 的同一东财请求，并只接受带身份回显的显式空 `klines`。"""
    market_code = 1 if identifier.symbol.startswith("6") else 0
    response = requests.get(
        _EMPTY_PROOF_URL,
        params={
            "fields1": _EMPTY_PROOF_FIELDS_1,
            "fields2": _EMPTY_PROOF_FIELDS_2,
            "ut": _EMPTY_PROOF_TOKEN,
            "klt": _EMPTY_PROOF_PERIOD[period],
            "fqt": "0",
            "secid": f"{market_code}.{identifier.symbol}",
            "beg": start.strftime("%Y%m%d"),
            "end": end.strftime("%Y%m%d"),
        },
        timeout=float(timeout_seconds),
    )
    response.raise_for_status()
    raw_payload = bytes(response.content)
    if not raw_payload or len(raw_payload) > _EMPTY_PROOF_MAX_BYTES:
        raise _EmptyProofSchemaError("empty-window proof payload size is invalid")
    body = response.json()
    if not isinstance(body, dict) or body.get("rc") != 0:
        raise _EmptyProofSchemaError("empty-window proof has no successful result code")
    data = body.get("data")
    if (
        not isinstance(data, dict)
        or str(data.get("code")) != identifier.symbol
        or data.get("market") != market_code
        or data.get("klines") != []
    ):
        raise _EmptyProofSchemaError("empty-window proof does not match identity and window")
    return raw_payload


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
