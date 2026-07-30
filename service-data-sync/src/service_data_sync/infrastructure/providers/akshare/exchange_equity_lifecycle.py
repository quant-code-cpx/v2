"""基于固定版本 `AKShare` 交易所接口的显式上市生命周期适配器。

沪深终止上市表只生成有官方日期的 `DELISTED` 事实，北交所在册表只生成有官方上市日的
`LISTED` 事实。适配器不会从目录缺席、名称变化或今日观察时间推断任何状态；历史日期
必须重放归档批次，保证生命周期的双时间修订有可复核来源。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import akshare as ak
import requests

from service_data_sync.application.ports.data_source import (
    ProviderBatch,
    ProviderError,
    ProviderErrorCode,
    SourceRequest,
)
from service_data_sync.domain.equity import Exchange

_CAPABILITY = "equity.lifecycle.explicit"
_SCHEMA = "quant-v2.equity-lifecycle-explicit.v1"
_ADAPTER_VERSION = "akshare-1.18.78-official-exchange-lifecycle-v2"
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_SSE_URL = "https://query.sse.com.cn/commonQuery.do"
_SSE_HEADERS = {
    "Accept": "*/*",
    "Accept-Encoding": "gzip, deflate",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "Host": "query.sse.com.cn",
    "Pragma": "no-cache",
    "Referer": "https://www.sse.com.cn/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/97.0.4692.71 Safari/537.36"
    ),
}
_SSE_PARAMS = {
    "sqlId": "COMMON_SSE_CP_GPJCTPZ_GPLB_GP_L",
    "isPagination": "true",
    "STOCK_CODE": "",
    "CSRC_CODE": "",
    "REG_PROVINCE": "",
    "STOCK_TYPE": "1,8",
    "COMPANY_STATUS": "3",
    "type": "inParams",
    "pageHelp.cacheSize": "1",
    "pageHelp.beginPage": "1",
    "pageHelp.pageSize": "500",
    "pageHelp.pageNo": "1",
    "pageHelp.endPage": "1",
}
_SSE_REQUIRED_FIELDS = frozenset(
    {
        "A_STOCK_CODE",
        "B_STOCK_CODE",
        "STOCK_TYPE",
        "COMPANY_ABBR",
        "LIST_DATE",
        "DELIST_DATE",
    }
)


class AkshareExchangeEquityLifecycleAdapter:
    """从沪深终止上市历史和北交所在册上市日输出中立生命周期事实。

    交易所专有函数和表头仅留在此适配器内，应用层只收到统一的显式证据 `JSON`。
    """

    provider_id = "akshare-official-exchange-equity-lifecycle"

    def __init__(
        self,
        *,
        request_timeout_seconds: int,
        client: Any = ak,
        sse_http_client: Any = requests,
    ) -> None:
        """保存固定上游 client 与单次阻塞请求墙钟预算，便于 fixture 注入。"""
        self._request_timeout_seconds = request_timeout_seconds
        self._client = client
        self._sse_http_client = sse_http_client

    def capabilities(self) -> frozenset[str]:
        """声明唯一显式生命周期能力。"""
        return frozenset({_CAPABILITY})

    async def fetch(self, request: SourceRequest) -> ProviderBatch:
        """调用交易所对应接口，归档原始行并输出可重放的标准生命周期 `JSON`。

        目标日按上海时区验证，避免不同部署地区在午夜附近产生错误的观察分区。
        """
        if request.capability != _CAPABILITY:
            raise ProviderError(
                ProviderErrorCode.INVALID_REQUEST,
                "unsupported capability",
                retryable=False,
            )
        exchange, target_date = _request_values(request)
        if target_date != datetime.now(_SHANGHAI).date():
            # 这些上游接口只给当前集合；拒绝历史请求，不用当前行伪造过去生命周期。
            raise ProviderError(
                ProviderErrorCode.INVALID_REQUEST,
                "equity lifecycle source supports only the current Shanghai date",
                retryable=False,
            )
        try:
            async with asyncio.timeout(self._request_timeout_seconds):
                raw_records, raw_payload = await asyncio.to_thread(
                    self._fetch_records,
                    exchange,
                )
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
        try:
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

    def _fetch_records(self, exchange: Exchange) -> tuple[list[dict[str, Any]], bytes]:
        """在 adapter 内冻结真实端点，并把完整原始响应留给证据归档。"""
        if exchange is Exchange.SSE:
            return self._fetch_sse_records()
        if exchange is Exchange.SZSE:
            frame = self._client.stock_info_sz_delist("终止上市公司")
        else:
            frame = self._client.stock_info_bj_name_code()
        if frame.empty:
            raise ProviderError(
                ProviderErrorCode.SCHEMA,
                "provider returned an empty lifecycle dataset",
                retryable=False,
            )
        records = frame.to_dict(orient="records")
        raw_payload = json.dumps(
            {"records": records},
            ensure_ascii=False,
            default=_json_default,
            separators=(",", ":"),
        ).encode()
        return records, raw_payload

    def _fetch_sse_records(self) -> tuple[list[dict[str, Any]], bytes]:
        """直读上交所原始 JSON，保留 A/B 证券字段并只请求 A 股与科创板类型。

        AKShare 1.18.78 的包装函数会裁掉 `STOCK_TYPE`、`A_STOCK_CODE` 和
        `B_STOCK_CODE`，随后错误使用公司代码，无法区分同一公司的 A/B 股。
        因此这里冻结其官方底层端点和查询参数，语义字段缺失时直接隔离。
        """
        documents: list[Mapping[str, Any]] = []
        records: list[dict[str, Any]] = []
        expected_total: int | None = None
        page_count: int | None = None
        page_number = 1
        while page_count is None or page_number <= page_count:
            document, page_records, total, page_count = self._fetch_sse_page(page_number)
            if expected_total is None:
                expected_total = total
            elif expected_total != total:
                raise ProviderError(
                    ProviderErrorCode.SCHEMA,
                    "SSE lifecycle pagination total changed during fetch",
                    retryable=True,
                )
            documents.append(document)
            records.extend(page_records)
            page_number += 1
        if expected_total is None or len(records) != expected_total:
            raise ProviderError(
                ProviderErrorCode.SCHEMA,
                "SSE lifecycle pagination is incomplete",
                retryable=False,
            )
        raw_payload = json.dumps(
            {"pages": documents},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        return records, raw_payload

    def _fetch_sse_page(
        self,
        page_number: int,
    ) -> tuple[Mapping[str, Any], list[dict[str, Any]], int, int]:
        """读取并校验一页上交所 JSON，防止页数增长后静默截断历史。"""
        params = dict(_SSE_PARAMS)
        params["pageHelp.beginPage"] = str(page_number)
        params["pageHelp.endPage"] = str(page_number)
        params["pageHelp.pageNo"] = str(page_number)
        response = self._sse_http_client.get(
            _SSE_URL,
            params=params,
            headers=_SSE_HEADERS,
            timeout=self._request_timeout_seconds,
        )
        response.raise_for_status()
        if not bytes(response.content):
            raise ProviderError(
                ProviderErrorCode.SCHEMA,
                "SSE lifecycle raw response is empty",
                retryable=False,
            )
        document = response.json()
        if not isinstance(document, Mapping):
            raise ProviderError(
                ProviderErrorCode.SCHEMA,
                "SSE lifecycle response must be a JSON object",
                retryable=False,
            )
        result = document.get("result")
        if not isinstance(result, list) or not result:
            raise ProviderError(
                ProviderErrorCode.SCHEMA,
                "SSE lifecycle response has no result rows",
                retryable=False,
            )
        page_help = document.get("pageHelp")
        if not isinstance(page_help, Mapping):
            raise ProviderError(
                ProviderErrorCode.SCHEMA,
                "SSE lifecycle pagination metadata is unavailable",
                retryable=False,
            )
        try:
            total = int(page_help["total"])
            page_count = int(page_help["pageCount"])
            actual_page = int(page_help["pageNo"])
        except (KeyError, TypeError, ValueError) as error:
            raise ProviderError(
                ProviderErrorCode.SCHEMA,
                "SSE lifecycle pagination metadata changed",
                retryable=False,
            ) from error
        if total <= 0 or page_count <= 0 or page_count > 100 or actual_page != page_number:
            raise ProviderError(
                ProviderErrorCode.SCHEMA,
                "SSE lifecycle pagination metadata is inconsistent",
                retryable=False,
            )
        records: list[dict[str, Any]] = []
        for raw_record in result:
            if not isinstance(raw_record, Mapping):
                raise ProviderError(
                    ProviderErrorCode.SCHEMA,
                    "SSE lifecycle result row must be a JSON object",
                    retryable=False,
                )
            record = dict(raw_record)
            if not _SSE_REQUIRED_FIELDS.issubset(record):
                raise ProviderError(
                    ProviderErrorCode.SCHEMA,
                    "SSE lifecycle semantic fields are unavailable",
                    retryable=False,
                )
            if str(record["STOCK_TYPE"]).strip() not in {"1", "8"}:
                raise ProviderError(
                    ProviderErrorCode.SCHEMA,
                    "SSE lifecycle response escaped the A-share type filter",
                    retryable=False,
                )
            records.append(record)
        return document, records, total, page_count


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
    """按交易所真实表头映射显式事实，过滤 B 股且不从名称或目录缺席推断状态。

    去重键包含代码、生效日和状态；同一代码的不同官方事实必须保留给状态机处理。
    """
    if exchange is Exchange.BSE:
        entries = [_bse_listing_entry(record) for record in records]
    elif exchange is Exchange.SSE:
        entries = [
            _delisting_entry(
                record,
                symbol_key="A_STOCK_CODE",
                name_key="COMPANY_ABBR",
                listed_on_key="LIST_DATE",
                delisted_on_key="DELIST_DATE",
            )
            for record in records
        ]
    else:
        entries = [
            _delisting_entry(
                record,
                symbol_key="证券代码",
                name_key="证券简称",
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
    name_key: str,
    listed_on_key: str,
    delisted_on_key: str,
) -> dict[str, str | None]:
    """把沪深交易所终止上市行映射为带官方生效日的 DELISTED 事实。"""
    symbol = _symbol(record[symbol_key])
    listed_on = _optional_date(record.get(listed_on_key))
    delisted_on = _required_date(record[delisted_on_key])
    return {
        "symbol": symbol,
        "name": _required_name(record[name_key]),
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
        "name": _required_name(record["证券简称"]),
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


def _required_name(value: object) -> str:
    """读取交易所官方证券简称，历史身份禁止使用代码或空白占位。"""
    name = str(value).strip()
    if not name or name.lower() in {"nan", "nat", "none"}:
        raise ValueError("lifecycle identity name is missing")
    return name


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
    if len(text) == 8 and text.isdecimal():
        return date(int(text[:4]), int(text[4:6]), int(text[6:8]))
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
