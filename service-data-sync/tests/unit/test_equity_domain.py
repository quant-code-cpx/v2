"""标准证券身份与日线不变量的单元测试。"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from service_data_sync.domain.equity import EquityDailyBar, EquityIdentifier, Exchange


def test_equity_identifier_parses_stable_qualified_symbol() -> None:
    """接受带交易所限定的六位代码，并保留标准渲染结果。"""
    identifier = EquityIdentifier.parse("sse.600519")

    assert identifier.exchange is Exchange.SSE
    assert identifier.symbol == "600519"
    assert identifier.qualified_symbol == "SSE.600519"


@pytest.mark.parametrize("value", ["600519", "SSE.12345", "US.600519", "SSE.60051A"])
def test_equity_identifier_rejects_ambiguous_or_invalid_symbols(value: str) -> None:
    """拒绝缺少交易所、非六位代码和不支持的市场。"""
    with pytest.raises(ValueError):
        EquityIdentifier.parse(value)


def test_daily_bar_rejects_ohlc_range_violation() -> None:
    """阻止结构上不可能的日线进入持久化代码。"""
    with pytest.raises(ValueError, match="low price"):
        EquityDailyBar(
            trade_date=date(2026, 6, 30),
            open_price=Decimal("10"),
            high_price=Decimal("11"),
            low_price=Decimal("10.5"),
            close_price=Decimal("10.2"),
            volume_shares=100,
            amount_cny=Decimal("1000"),
            turnover_rate=None,
        )
