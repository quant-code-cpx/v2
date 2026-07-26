"""可选的 AKShare 实时冒烟测试，仅请求近期一个月交易窗口。"""

from __future__ import annotations

import asyncio
import os
from datetime import date, timedelta

import pytest

from service_data_sync.application.equity.daily_bar_sync import decode_daily_bar_batch
from service_data_sync.application.ports.data_source import SourceRequest
from service_data_sync.domain.equity import EquityIdentifier
from service_data_sync.infrastructure.providers.akshare.tencent_daily_bars import (
    AkshareTencentDailyBarsAdapter,
)


@pytest.mark.integration
def test_tencent_adapter_fetches_only_recent_month_when_live_smoke_is_enabled() -> None:
    """运维人员显式开启时，仅获取一只流动性股票至多 32 天的数据。"""
    if os.environ.get("DATA_SYNC_RUN_AKSHARE_SMOKE") != "1":
        pytest.skip("set DATA_SYNC_RUN_AKSHARE_SMOKE=1 to call the external provider")
    identifier = EquityIdentifier.parse("SSE.600519")
    end = date.today()
    start = end - timedelta(days=31)

    batch = asyncio.run(
        AkshareTencentDailyBarsAdapter(request_timeout_seconds=60).fetch(
            SourceRequest(
                capability="equity.bar.1d.raw",
                parameters=(
                    ("instrument", identifier.qualified_symbol),
                    ("start", start.isoformat()),
                    ("end", end.isoformat()),
                ),
            )
        )
    )
    bars = decode_daily_bar_batch(batch.payload, identifier)

    assert bars
    assert all(start <= bar.trade_date <= end for bar in bars)
    assert max(bar.trade_date for bar in bars) - min(bar.trade_date for bar in bars) <= timedelta(
        days=31
    )
