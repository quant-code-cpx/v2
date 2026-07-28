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

    def stock_info_sh_delist(self, symbol: str) -> pd.DataFrame:
        """模拟上交所终止上市函数及其误名的 DELIST_DATE 输出列。"""
        assert symbol == "全部"
        return pd.DataFrame(
            [
                {
                    "公司代码": "600001",
                    "公司简称": "邯郸钢铁",
                    "上市日期": "1998-01-22",
                    "暂停上市日期": "2009-12-29",
                },
                {
                    "公司代码": "900901",
                    "公司简称": "B股样本",
                    "上市日期": "1992-02-21",
                    "暂停上市日期": "2024-01-01",
                },
            ]
        )

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


def _request(exchange: str) -> SourceRequest:
    """构造当前上海日的 provider-neutral 生命周期请求。"""
    return SourceRequest(
        capability="equity.lifecycle.explicit",
        parameters=(
            ("exchange", exchange),
            ("targetDate", datetime.now(_SHANGHAI).date().isoformat()),
        ),
    )


def test_adapter_maps_sse_delist_date_and_preserves_raw_evidence() -> None:
    """SSE 的误名日期按固定源码 DELIST_DATE 语义映射，并过滤 B 股。"""
    adapter = AkshareExchangeEquityLifecycleAdapter(
        request_timeout_seconds=5,
        client=FakeAkshareLifecycleClient(),
    )
    batch = asyncio.run(adapter.fetch(_request("SSE")))
    payload = json.loads(batch.payload)
    raw = json.loads(batch.raw_payload or b"{}")

    assert payload["entries"] == [
        {
            "symbol": "600001",
            "status": "DELISTED",
            "effectiveOn": "2009-12-29",
            "evidenceKind": "EXPLICIT_DELISTING",
            "listedOn": "1998-01-22",
            "delistedOn": "2009-12-29",
            "correctionApprovalReference": None,
        }
    ]
    assert raw["records"][0]["暂停上市日期"] == "2009-12-29"
    assert batch.upstream_source == "sse.terminated-listing"
    assert batch.adapter_version == "akshare-1.18.78-official-exchange-lifecycle-v1"
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
