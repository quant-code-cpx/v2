"""经由东财全市场快照获取 A 股证券目录的适配器。

东财接口给出的是当前市场截面，不是任意历史时点的正式目录；模块仅按请求交易所
筛出允许的六位 A 股代码，并将上游表头作 ``schema`` 指纹。历史目录请求会被明确拒绝，
不能把今天的名单回填到过去并掩盖代码复用或退市事实。

目录端点不能经阻塞 ``AKShare`` 分页调用：TLS 握手若在同步线程中悬挂，协程超时后仍会
等待该线程，进而占住受控 dispatcher。这里直接使用已有 ``httpx`` 异步传输；总预算取消
会关闭连接并使调用回到控制面，而 URL、查询字段和供应商字段均不离开本 adapter。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from service_data_sync.application.ports.data_source import (
    ProviderBatch,
    ProviderError,
    ProviderErrorCode,
    SourceRequest,
)
from service_data_sync.domain.equity import Exchange

_CAPABILITY = "equity.master.catalog"
_SCHEMA = "quant-v2.equity-master-catalog.v1"
_ADAPTER_VERSION = "eastmoney-http-catalog-v2"
_SHANGHAI = ZoneInfo("Asia/Shanghai")

# 以下地址、参数和字段属于东财私有传输合同，只能由本 adapter 使用。
_CATALOG_URL = "https://82.push2delay.eastmoney.com/api/qt/clist/get"
_CATALOG_HEADERS = {
    "Referer": "https://quote.eastmoney.com/",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/138.0.0.0 Safari/537.36"
    ),
}
_CATALOG_QUERY = {
    "pz": "100",
    "po": "1",
    "np": "1",
    "ut": "bd1d9ddb04089700cf9c27f6f7426281",
    "fltt": "2",
    "invt": "2",
    "fid": "f12",
    "fs": "m:0 t:6,m:0 t:80,m:1 t:2,m:1 t:23,m:0 t:81 s:2048",
    "fields": "f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f12,f13,f14,f15,f16,f17,f18,"
    "f20,f21,f23,f24,f25,f22,f11,f62,f128,f136,f115,f152",
}
_CATALOG_PAGE_SIZE = 100
_CATALOG_MAX_PAGES = 100
_CATALOG_INTER_PAGE_DELAY_SECONDS = 0.5


class _CatalogResponseSchemaError(ValueError):
    """表示东财目录响应偏离冻结的分页或字段形状。"""


class AkshareEastmoneyEquityCatalogAdapter:
    """获取东财 A 股完整快照，并按交易所输出标准证券目录。

    adapter 名称为兼容既有 provider 注册而保留；实际目录 HTTP 由 ``httpx`` 异步客户端执行，
    使连接、读取和总墙钟预算均可取消。上市状态和双时间身份仍由主数据发布层根据独立证据决定。
    """

    provider_id = "akshare-eastmoney-equity-catalog"

    def __init__(
        self,
        *,
        request_timeout_seconds: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """保存目录总预算和可替换异步传输，后者仅供确定性网络失败测试。"""
        if request_timeout_seconds <= 0:
            raise ValueError("request timeout must be positive")
        self._request_timeout_seconds = request_timeout_seconds
        self._transport = transport

    def capabilities(self) -> frozenset[str]:
        """仅声明完整 A 股证券目录能力。"""
        return frozenset({_CAPABILITY})

    async def fetch(self, request: SourceRequest) -> ProviderBatch:
        """请求一次全市场现货快照，并隔离为指定交易所目录。

        请求日期按上海时区验证，避免北京时间零点附近把同一响应误归入两个业务日。连接或读取
        超时必须归一为可重试的来源不可用，字段、分页或身份形状漂移则隔离为不可重试 ``schema``。
        """
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
            raw_records, raw_payload = await _fetch_catalog(
                request_timeout_seconds=self._request_timeout_seconds,
                transport=self._transport,
            )
        except (TimeoutError, httpx.TimeoutException) as error:
            raise ProviderError(
                ProviderErrorCode.UNAVAILABLE, "provider request timed out", retryable=True
            ) from error
        except _CatalogResponseSchemaError as error:
            raise ProviderError(
                ProviderErrorCode.SCHEMA,
                "provider equity catalog schema changed",
                retryable=False,
            ) from error
        except httpx.HTTPError as error:
            raise ProviderError(
                ProviderErrorCode.UNAVAILABLE, "provider request failed", retryable=True
            ) from error
        if not raw_records:
            raise ProviderError(
                ProviderErrorCode.SCHEMA,
                "provider returned an empty equity catalog",
                retryable=False,
            )
        try:
            # 交易所由代码段显式识别；名称不能作为证券身份或跨市归属的依据。
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


async def _fetch_catalog(
    *,
    request_timeout_seconds: float,
    transport: httpx.AsyncBaseTransport | None,
) -> tuple[list[dict[str, Any]], bytes]:
    """在可取消异步传输中读取完整目录，并将总预算传给每次连接和读取。

    ``httpx`` 请求每页都以剩余墙钟时间设置 connect/read/write/pool timeout；外层 ``asyncio``
    timeout 负责跨分页总预算。两层取消均在 event loop 内关闭 transport，不能留下阻塞 TLS 线程。
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + request_timeout_seconds
    records: list[dict[str, Any]] = []
    raw_pages: list[bytes] = []
    expected_total: int | None = None
    expected_page_count: int | None = None
    async with httpx.AsyncClient(
        headers=_CATALOG_HEADERS,
        follow_redirects=False,
        transport=transport,
    ) as client:
        async with asyncio.timeout(request_timeout_seconds):
            for page_number in range(1, _CATALOG_MAX_PAGES + 1):
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise TimeoutError("equity catalog request budget exhausted")
                # ``Timeout(remaining)`` 同时收紧 connect/read/write/pool；最后一页不会继承首
                # 页的长预算，从而保证分页、睡眠和 TLS 握手合计不突破 caller 的总上限。
                response = await client.get(
                    _CATALOG_URL,
                    params={**_CATALOG_QUERY, "pn": str(page_number)},
                    timeout=httpx.Timeout(remaining),
                )
                response.raise_for_status()
                raw_pages.append(response.content)
                page_records, page_total = _catalog_page_records(response)
                if expected_total is None:
                    expected_total = page_total
                    expected_page_count = math.ceil(page_total / _CATALOG_PAGE_SIZE)
                    if expected_page_count > _CATALOG_MAX_PAGES:
                        raise _CatalogResponseSchemaError("catalog page count exceeds frozen limit")
                elif page_total != expected_total:
                    raise _CatalogResponseSchemaError("catalog total changed during pagination")
                records.extend(page_records)
                if expected_page_count == 0:
                    if page_records:
                        raise _CatalogResponseSchemaError("empty catalog total has records")
                    return records, _raw_catalog_pages(raw_pages)
                if page_number == expected_page_count:
                    if len(records) != expected_total:
                        raise _CatalogResponseSchemaError("catalog page records do not match total")
                    return records, _raw_catalog_pages(raw_pages)
                # 保持有界顺序抓取，避免在同一目录快照中并发翻页而触发上游限流或混入不同截面。
                await asyncio.sleep(_CATALOG_INTER_PAGE_DELAY_SECONDS)
    raise _CatalogResponseSchemaError("catalog pagination did not terminate")


def _catalog_page_records(response: httpx.Response) -> tuple[list[dict[str, Any]], int]:
    """解析东财一页目录，并只向后续标准化暴露代码和名称。"""
    try:
        body = response.json()
        data = body["data"]
        total = data["total"]
        diff = data["diff"]
    except (json.JSONDecodeError, KeyError, TypeError) as error:
        raise _CatalogResponseSchemaError("catalog response has no data page") from error
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        raise _CatalogResponseSchemaError("catalog response total is invalid")
    if not isinstance(diff, list):
        raise _CatalogResponseSchemaError("catalog response diff is invalid")
    try:
        # ``f12``、``f14`` 是本 adapter 唯一承认的东财代码和名称字段，禁止它们进入标准载荷。
        return ([{"代码": item["f12"], "名称": item["f14"]} for item in diff], total)
    except (KeyError, TypeError) as error:
        raise _CatalogResponseSchemaError("catalog response item is invalid") from error


def _raw_catalog_pages(pages: list[bytes]) -> bytes:
    """把逐页原始 JSON 保留为一个 JSON 对象，供失败证据和来源摘要使用。"""
    return b'{"pages":[' + b",".join(pages) + b"]}"


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
    """将东财中文字段归一化，并只保留目标交易所允许的六位股票代码。

    ``listedOn`` 保持为空：当前现货目录不提供可审计的上市日，不能用抓取日伪造。
    """
    entries: list[dict[str, str | None]] = []
    for record in records:
        symbol = str(record["代码"]).zfill(6)
        # 仅恢复数值化丢失的前导零；非法代码会由 `_exchange_for_symbol` 拒绝。
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
    """记录 adapter 承认的东财字段集合哈希，以便身份字段漂移被审计和隔离。"""
    keys = sorted({str(key) for record in records for key in record})
    return hashlib.sha256(
        json.dumps(keys, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
