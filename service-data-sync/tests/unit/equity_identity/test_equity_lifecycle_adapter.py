"""AKShare 固定版本上市生命周期适配器回归测试。"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from service_data_sync.application.ports.data_source import ProviderError, SourceRequest
from service_data_sync.infrastructure.providers.akshare.exchange_equity_lifecycle import (
    AkshareExchangeEquityLifecycleAdapter,
)

_SHANGHAI = ZoneInfo("Asia/Shanghai")


class FakeAkshareLifecycleClient:
    """提供与 AKShare 1.18.78 已验证表头一致的确定性 fixture。"""

    def stock_info_sz_delist(self, symbol: str) -> pd.DataFrame:
        """模拟深交所终止上市集合。"""
        assert symbol == "终止上市公司"
        return pd.DataFrame(
            [
                {
                    "证券代码": "000003",
                    "证券简称": "PT金田Ａ",
                    "上市日期": "1991-01-14",
                    "终止上市日期": "2002-06-14",
                }
            ]
        )

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
    assert batch.adapter_version == "akshare-1.18.78-official-exchange-lifecycle-v2"
    assert batch.schema_fingerprint is not None
    assert len(batch.schema_fingerprint) == 64


def test_adapter_maps_szse_explicit_delisting() -> None:
    """SZSE 终止上市集合输出明确退市事实。"""
    adapter = AkshareExchangeEquityLifecycleAdapter(
        request_timeout_seconds=5,
        client=FakeAkshareLifecycleClient(),
    )
    batch = asyncio.run(adapter.fetch(_request("SZSE")))
    entry = json.loads(batch.payload)["entries"][0]

    assert entry["symbol"] == "000003"
    assert entry["name"] == "PT金田Ａ"
    assert entry["status"] == "DELISTED"
    assert entry["effectiveOn"] == "2002-06-14"
    assert batch.upstream_source == "szse.terminated-listing"


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
