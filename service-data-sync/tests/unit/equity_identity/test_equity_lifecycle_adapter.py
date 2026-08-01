"""AKShare 固定版本上市生命周期适配器回归测试。"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from io import BytesIO
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
import requests

from service_data_sync.application.ports.data_source import (
    ProviderError,
    ProviderErrorCode,
    SourceRequest,
)
from service_data_sync.infrastructure.providers.akshare.exchange_equity_lifecycle import (
    AkshareExchangeEquityLifecycleAdapter,
)

_SHANGHAI = ZoneInfo("Asia/Shanghai")


class FakeAkshareLifecycleClient:
    """提供与 AKShare 1.18.81 已验证表头一致的确定性 fixture。"""

    def stock_info_bj_name_code(self) -> pd.DataFrame:
        """模拟北交所在册证券及其官方上市日期。"""
        return pd.DataFrame(
            [
                {
                    "证券代码": "430017",
                    "证券简称": "星昊医药",
                    "总股本": 122_577_200,
                    "流通股本": 91_000_000,
                    "上市日期": "2023-05-31",
                    "所属行业": "医药制造业",
                    "地区": "北京市",
                    "报告日期": "2026-07-28",
                }
            ]
        )


class FakeSseResponse:
    """模拟保留完整上交所官方字段的 HTTP JSON 响应。"""

    def __init__(
        self,
        records: list[dict[str, str]],
        *,
        total: int | None = None,
        page_count: int = 1,
        page_no: int = 1,
    ) -> None:
        """保存原始结果，并生成与真实归档形态一致的响应字节。"""
        self._document: dict[str, object] = {
            "result": records,
            "pageHelp": {
                "total": len(records) if total is None else total,
                "pageCount": page_count,
                "pageNo": page_no,
                "pageSize": 500,
            },
        }
        self.content = json.dumps(
            self._document,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()

    def raise_for_status(self) -> None:
        """成功 fixture 不产生 HTTP 状态错误。"""

    def json(self) -> dict[str, object]:
        """返回完整官方响应对象。"""
        return self._document


class FakeSzseResponse:
    """生成包含深交所终止上市字段的最小 XLSX HTTP 响应。"""

    def __init__(self) -> None:
        """写入固定官方列，供 adapter 验证直连 XLSX 路径。"""
        output = BytesIO()
        pd.DataFrame(
            [
                {
                    "证券代码": "000003",
                    "证券简称": "PT金田Ａ",
                    "上市日期": "1991-01-14",
                    "终止上市日期": "2002-06-14",
                }
            ]
        ).to_excel(output, index=False, engine="openpyxl")
        self.status_code = 200
        self.content = output.getvalue()


class FakeSzseHttpClient:
    """按脚本返回 TLS 失败或 XLSX 成功响应，并记录每次重试。"""

    def __init__(self, responses: list[FakeSzseResponse | BaseException] | None = None) -> None:
        """复制可消费的响应脚本；默认首次即成功。"""
        self._responses = list(responses or [FakeSzseResponse()])
        self.calls: list[tuple[dict[str, str], tuple[float, float]]] = []

    def get(
        self,
        _url: str,
        *,
        params: dict[str, str],
        headers: dict[str, str],
        timeout: tuple[float, float],
    ) -> FakeSzseResponse:
        """记录官方参数与总预算派生 timeout，并按顺序给出脚本值。"""
        del headers
        self.calls.append((dict(params), timeout))
        value = self._responses.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


class FakeSseHttpClient:
    """断言 adapter 冻结为上交所 A 股与科创板原始查询。"""

    def __init__(self, records: list[dict[str, str]] | None = None) -> None:
        """创建默认包含一条 A 股历史的官方字段 fixture。"""
        self._records = records or [
            {
                "COMPANY_CODE": "600001",
                "A_STOCK_CODE": "600001",
                "B_STOCK_CODE": "-",
                "STOCK_TYPE": "1",
                "COMPANY_ABBR": "邯郸钢铁",
                "LIST_DATE": "19980122",
                "DELIST_DATE": "20091229",
            }
        ]

    def get(
        self,
        url: str,
        *,
        params: dict[str, str],
        headers: dict[str, str],
        timeout: int,
    ) -> FakeSseResponse:
        """校验官方端点、证券类型白名单和显式墙钟超时。"""
        assert url == "https://query.sse.com.cn/commonQuery.do"
        assert params["sqlId"] == "COMMON_SSE_CP_GPJCTPZ_GPLB_GP_L"
        assert params["STOCK_TYPE"] == "1,8"
        assert params["COMPANY_STATUS"] == "3"
        assert params["pageHelp.pageNo"] == "1"
        assert headers["Referer"] == "https://www.sse.com.cn/"
        assert timeout == 5
        return FakeSseResponse(self._records)


class PagedFakeSseHttpClient:
    """按请求页号返回两页完整官方响应，用于防截断回归测试。"""

    def __init__(self, pages: list[list[dict[str, str]]]) -> None:
        """保存各页记录并记录 adapter 实际请求顺序。"""
        self._pages = pages
        self.requested_pages: list[int] = []

    def get(
        self,
        url: str,
        *,
        params: dict[str, str],
        headers: dict[str, str],
        timeout: int,
    ) -> FakeSseResponse:
        """按 pageNo 返回对应页并保留总数元数据。"""
        del url, headers, timeout
        page_no = int(params["pageHelp.pageNo"])
        self.requested_pages.append(page_no)
        return FakeSseResponse(
            self._pages[page_no - 1],
            total=sum(len(page) for page in self._pages),
            page_count=len(self._pages),
            page_no=page_no,
        )


def _request(exchange: str) -> SourceRequest:
    """构造当前上海日的 provider-neutral 生命周期请求。"""
    return SourceRequest(
        capability="equity.lifecycle.explicit",
        parameters=(
            ("exchange", exchange),
            ("targetDate", datetime.now(_SHANGHAI).date().isoformat()),
        ),
    )


def test_adapter_reads_sse_a_stock_code_and_preserves_full_raw_evidence() -> None:
    """SSE 直读原始证券字段，按 A_STOCK_CODE 映射且不丢失类型与 B 股代码。"""
    adapter = AkshareExchangeEquityLifecycleAdapter(
        request_timeout_seconds=5,
        client=FakeAkshareLifecycleClient(),
        sse_http_client=FakeSseHttpClient(),
    )
    batch = asyncio.run(adapter.fetch(_request("SSE")))
    payload = json.loads(batch.payload)
    raw = json.loads(batch.raw_payload or b"{}")

    assert payload["entries"] == [
        {
            "symbol": "600001",
            "name": "邯郸钢铁",
            "status": "DELISTED",
            "effectiveOn": "2009-12-29",
            "evidenceKind": "EXPLICIT_DELISTING",
            "listedOn": "1998-01-22",
            "delistedOn": "2009-12-29",
            "correctionApprovalReference": None,
        }
    ]
    assert raw["pages"][0]["result"][0]["A_STOCK_CODE"] == "600001"
    assert raw["pages"][0]["result"][0]["B_STOCK_CODE"] == "-"
    assert raw["pages"][0]["result"][0]["STOCK_TYPE"] == "1"
    assert batch.upstream_source == "sse.terminated-listing"
    assert batch.adapter_version == "akshare-1.18.81-official-exchange-lifecycle-v3"
    assert batch.schema_fingerprint is not None
    assert len(batch.schema_fingerprint) == 64


def test_adapter_maps_szse_explicit_delisting() -> None:
    """SZSE 终止上市集合输出明确退市事实。"""
    adapter = AkshareExchangeEquityLifecycleAdapter(
        request_timeout_seconds=5,
        client=FakeAkshareLifecycleClient(),
        szse_http_client=FakeSzseHttpClient(),
    )
    batch = asyncio.run(adapter.fetch(_request("SZSE")))
    entry = json.loads(batch.payload)["entries"][0]

    assert entry["symbol"] == "000003"
    assert entry["name"] == "PT金田Ａ"
    assert entry["status"] == "DELISTED"
    assert entry["effectiveOn"] == "2002-06-14"
    assert batch.upstream_source == "szse.terminated-listing"


def test_adapter_keeps_szse_transport_inside_short_total_budget() -> None:
    """剩余总预算不足一秒时，底层 HTTP timeout 不能被默认值放大。"""
    transport = FakeSzseHttpClient()
    adapter = AkshareExchangeEquityLifecycleAdapter(
        request_timeout_seconds=0.5,
        client=FakeAkshareLifecycleClient(),
        szse_http_client=transport,
    )

    asyncio.run(adapter.fetch(_request("SZSE")))

    _, (connect_timeout, read_timeout) = transport.calls[0]
    assert 0 < connect_timeout <= read_timeout <= 0.5


def test_adapter_retries_szse_tls_eof_within_total_request_budget() -> None:
    """SZSE TLS 短断后只重试同一官方请求，并仍保留标准退市事实。"""
    transport = FakeSzseHttpClient(
        [
            requests.exceptions.SSLError("UNEXPECTED_EOF_WHILE_READING"),
            FakeSzseResponse(),
        ]
    )
    adapter = AkshareExchangeEquityLifecycleAdapter(
        request_timeout_seconds=5,
        client=FakeAkshareLifecycleClient(),
        szse_http_client=transport,
    )

    batch = asyncio.run(adapter.fetch(_request("SZSE")))

    assert len(transport.calls) == 2
    assert all(
        connect_timeout <= read_timeout for _, (connect_timeout, read_timeout) in transport.calls
    )
    assert json.loads(batch.payload)["entries"][0]["symbol"] == "000003"


def test_adapter_reports_szse_tls_eof_after_bounded_retry() -> None:
    """SZSE 连续 TLS EOF 用可重试不可用失败退出，绝不伪造空或旧生命周期。"""
    transport = FakeSzseHttpClient(
        [requests.exceptions.SSLError("UNEXPECTED_EOF_WHILE_READING")] * 3
    )
    adapter = AkshareExchangeEquityLifecycleAdapter(
        request_timeout_seconds=5,
        client=FakeAkshareLifecycleClient(),
        szse_http_client=transport,
    )

    with pytest.raises(ProviderError) as captured:
        asyncio.run(adapter.fetch(_request("SZSE")))

    assert captured.value.code is ProviderErrorCode.UNAVAILABLE
    assert captured.value.retryable is True
    assert len(transport.calls) == 3


def test_adapter_rejects_szse_non_xlsx_without_transport_retry() -> None:
    """SZSE 文件合同漂移立即隔离，不能被 TLS 重试逻辑改写为成功或不可用。"""
    invalid_response = FakeSzseResponse()
    invalid_response.content = b'{"message":"schema drift"}'
    transport = FakeSzseHttpClient([invalid_response])
    adapter = AkshareExchangeEquityLifecycleAdapter(
        request_timeout_seconds=5,
        client=FakeAkshareLifecycleClient(),
        szse_http_client=transport,
    )

    with pytest.raises(ProviderError) as captured:
        asyncio.run(adapter.fetch(_request("SZSE")))

    assert captured.value.code is ProviderErrorCode.SCHEMA
    assert captured.value.retryable is False
    assert len(transport.calls) == 1


def test_adapter_maps_bse_listing_without_inferring_absence() -> None:
    """BSE 只从返回行的官方上市日产生 LISTED，不产生任何缺席或退市事实。"""
    adapter = AkshareExchangeEquityLifecycleAdapter(
        request_timeout_seconds=5,
        client=FakeAkshareLifecycleClient(),
    )
    batch = asyncio.run(adapter.fetch(_request("BSE")))
    entry = json.loads(batch.payload)["entries"][0]

    assert entry == {
        "symbol": "430017",
        "name": "星昊医药",
        "status": "LISTED",
        "effectiveOn": "2023-05-31",
        "evidenceKind": "EXPLICIT_LISTING",
        "listedOn": "2023-05-31",
        "delistedOn": None,
        "correctionApprovalReference": None,
    }
    assert batch.upstream_source == "bse.listed-company"


def test_adapter_rejects_provider_specific_or_duplicate_request_parameters() -> None:
    """adapter 只接受中立 exchange/targetDate，禁止调用方渗入 SDK 参数。"""
    adapter = AkshareExchangeEquityLifecycleAdapter(
        request_timeout_seconds=5,
        client=FakeAkshareLifecycleClient(),
    )
    request = SourceRequest(
        capability="equity.lifecycle.explicit",
        parameters=(
            ("exchange", "SSE"),
            ("targetDate", datetime.now(_SHANGHAI).date().isoformat()),
            ("symbol", "全部"),
        ),
    )

    with pytest.raises(ProviderError, match="invalid equity lifecycle request"):
        asyncio.run(adapter.fetch(request))


def test_adapter_rejects_sse_rows_outside_frozen_a_share_types() -> None:
    """官方响应若逸出 1/8 白名单必须隔离，不能再按公司代码或前缀猜测。"""
    client = FakeSseHttpClient(
        [
            {
                "COMPANY_CODE": "600190",
                "A_STOCK_CODE": "600190",
                "B_STOCK_CODE": "900952",
                "STOCK_TYPE": "2",
                "COMPANY_ABBR": "锦州港Ｂ",
                "LIST_DATE": "1998-05-19",
                "DELIST_DATE": "2025-07-08",
            }
        ]
    )
    adapter = AkshareExchangeEquityLifecycleAdapter(
        request_timeout_seconds=5,
        client=FakeAkshareLifecycleClient(),
        sse_http_client=client,
    )

    with pytest.raises(ProviderError, match="escaped the A-share type filter"):
        asyncio.run(adapter.fetch(_request("SSE")))


def test_adapter_fetches_every_sse_page_before_normalizing() -> None:
    """pageHelp 声明多页时必须全部拉取并归档，不能只接受第一页的截断历史。"""
    first = FakeSseHttpClient()._records[0]
    second = {
        **first,
        "COMPANY_CODE": "688555",
        "A_STOCK_CODE": "688555",
        "STOCK_TYPE": "8",
        "COMPANY_ABBR": "退市泽达",
        "LIST_DATE": "20200623",
        "DELIST_DATE": "20230707",
    }
    client = PagedFakeSseHttpClient([[first], [second]])
    adapter = AkshareExchangeEquityLifecycleAdapter(
        request_timeout_seconds=5,
        client=FakeAkshareLifecycleClient(),
        sse_http_client=client,
    )

    batch = asyncio.run(adapter.fetch(_request("SSE")))
    payload = json.loads(batch.payload)
    raw = json.loads(batch.raw_payload or b"{}")

    assert client.requested_pages == [1, 2]
    assert [entry["symbol"] for entry in payload["entries"]] == ["600001", "688555"]
    assert len(raw["pages"]) == 2
