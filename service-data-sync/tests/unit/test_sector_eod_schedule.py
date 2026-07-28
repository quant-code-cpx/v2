"""板块 EOD 调度时区与权威日历安全默认值测试。"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from service_data_sync.application.sector.eod_schedule import (
    sector_eod_scheduler_target_date,
    sector_eod_source_cutoff_at,
)
from service_data_sync.bootstrap.container import _trading_calendar_for_settings
from service_data_sync.bootstrap.settings import load_settings
from service_data_sync.infrastructure.calendar.sse_szse_a_share_2026 import (
    SseSzseAshare2026TradingCalendar,
)
from service_data_sync.infrastructure.calendar.unavailable_trading_calendar import (
    UnavailableTradingCalendar,
)


def test_source_cutoff_is_a_shanghai_1615_instant() -> None:
    """来源观察截点固定为指定交易日上海时区 16:15，而非 worker 所在时区。"""
    cutoff = sector_eod_source_cutoff_at(date(2026, 7, 27))

    assert cutoff.isoformat() == "2026-07-27T16:15:00+08:00"


def test_scheduler_target_date_uses_shanghai_calendar_day() -> None:
    """UTC 调度时钟跨越中国日期边界时，目标日期应按上海自然日计算。"""
    target_date = sector_eod_scheduler_target_date(datetime(2026, 7, 26, 18, tzinfo=UTC))

    assert target_date == date(2026, 7, 27)


def test_scheduler_rejects_naive_clock_and_calendar_default_is_unknown() -> None:
    """无时区时钟和未经接入的交易日历都不能被误解为可安全执行。"""
    with pytest.raises(ValueError, match="timezone"):
        sector_eod_scheduler_target_date(datetime(2026, 7, 27, 16, 20))

    assert UnavailableTradingCalendar().is_open(trade_date=date(2026, 7, 27)) is None


def test_published_2026_calendar_covers_open_closed_and_unknown_years() -> None:
    """已核验年度仅为开市、休市给出确定值，下一年度发布前必须保持未知。"""
    calendar = SseSzseAshare2026TradingCalendar()

    assert calendar.is_open(trade_date=date(2026, 7, 27)) is True
    assert calendar.is_open(trade_date=date(2026, 6, 19)) is False
    assert calendar.is_open(trade_date=date(2026, 7, 25)) is False
    assert calendar.is_open(trade_date=date(2027, 1, 4)) is None


def test_calendar_factory_requires_explicit_enablement(configured_environment: None) -> None:
    """默认容器仍使用未知日历，只有显式审批开关才装配已核验年度。"""
    settings = load_settings()
    disabled = _trading_calendar_for_settings(
        settings.model_copy(update={"trading_calendar_enabled": False})
    )
    enabled = _trading_calendar_for_settings(
        settings.model_copy(update={"trading_calendar_enabled": True})
    )

    assert disabled.is_open(trade_date=date(2026, 7, 27)) is None
    assert enabled.is_open(trade_date=date(2026, 7, 27)) is True
