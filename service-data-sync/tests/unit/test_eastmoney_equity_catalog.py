"""AKShare 东财证券目录适配器边界的单元测试。"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from service_data_sync.application.ports.data_source import ProviderError, SourceRequest
from service_data_sync.infrastructure.providers.akshare import eastmoney_equity_catalog
from service_data_sync.infrastructure.providers.akshare.eastmoney_equity_catalog import (
    AkshareEastmoneyEquityCatalogAdapter,
)

_SHANGHAI = ZoneInfo("Asia/Shanghai")


class FakeFrame:
    """提供适配器转换所需的最小 DataFrame 表面。"""

    def __init__(self, records: list[dict[str, object]]) -> None:
        """保存确定性的供应商记录列表。"""
        self._records = records

    @property
    def empty(self) -> bool:
        """报告供应商响应是否没有任何记录。"""
        return not self._records

    def to_dict(self, *, orient: str) -> list[dict[str, object]]:
        """仅支持 adapter 使用的 records 导出模式。"""
        assert orient == "records"
        return self._records


def test_adapter_partitions_full_market_snapshot_by_exchange(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """东财全市场快照必须只把目标交易所的标准代码交给目录发布。"""
    frame = FakeFrame(
        [
            {"代码": "600519", "名称": "贵州茅台", "最新价": "1500"},
            {"代码": "000001", "名称": "平安银行", "最新价": "10"},
            {"代码": "920002", "名称": "万达轴承", "最新价": "20"},
        ]
    )
    # 固定 SDK 响应，验证供应商中文字段不会离开 adapter。
    monkeypatch.setattr(eastmoney_equity_catalog.ak, "stock_zh_a_spot_em", lambda: frame)
    target_date = datetime.now(_SHANGHAI).date()

    batch = asyncio.run(
        AkshareEastmoneyEquityCatalogAdapter(request_timeout_seconds=5).fetch(
            SourceRequest(
                capability="equity.master.catalog",
                parameters=(("exchange", "SSE"), ("targetDate", target_date.isoformat())),
            )
        )
    )

    payload = json.loads(batch.payload)
    assert payload["exchange"] == "SSE"
    assert payload["entries"] == [{"symbol": "600519", "name": "贵州茅台", "listedOn": None}]
    assert batch.upstream_source == "eastmoney"
    assert batch.schema_fingerprint is not None


def test_adapter_rejects_backfill_request_for_current_only_spot_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """现货来源没有历史目录能力，不能把当前快照伪装为历史回补。"""
    calls = 0

    def fail_if_called() -> FakeFrame:
        """确保无效目标日在访问供应商前被拒绝。"""
        nonlocal calls
        calls += 1
        raise AssertionError("历史目录请求不应访问现货来源")

    monkeypatch.setattr(eastmoney_equity_catalog.ak, "stock_zh_a_spot_em", fail_if_called)

    with pytest.raises(ProviderError, match="current Shanghai date") as error:
        asyncio.run(
            AkshareEastmoneyEquityCatalogAdapter(request_timeout_seconds=5).fetch(
                SourceRequest(
                    capability="equity.master.catalog",
                    parameters=(("exchange", "SSE"), ("targetDate", "2026-01-01")),
                )
            )
        )

    assert error.value.retryable is False
    assert calls == 0
