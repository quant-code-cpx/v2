"""市场完整包增量缺口恢复测试。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import cast

import pytest

from service_data_sync.application.ports.market_overview import MarketOverviewRepository
from service_data_sync.infrastructure.data_operations.canonical_executors import (
    _market_overview_pending_dates,
)


@dataclass(frozen=True, slots=True)
class FakeBundle:
    """只携带 current pointer 交易日。"""

    trade_date: date


class FakeActiveBundleRepository:
    """只记录具有 active complete bundle 的交易日。"""

    def __init__(self, active_dates: set[date]) -> None:
        """保存初始 active 日期集合。"""
        self.active_dates = active_dates
        self.requests: list[date] = []

    def get_bundle(self, *, trade_date: date | None) -> object | None:
        """按精确日期返回 active 占位对象，不实现任何日期回退。"""
        if trade_date is None:
            return FakeBundle(max(self.active_dates)) if self.active_dates else None
        self.requests.append(trade_date)
        return FakeBundle(trade_date) if trade_date in self.active_dates else None


def test_incremental_run_recovers_failed_day_before_processing_next_day() -> None:
    """T 日失败后，T+1 再跑必须按升序同时选择 T 与 T+1，不能永久跳过 T。"""
    first = date(2026, 6, 22)
    eligible_dates = [first + timedelta(days=offset) for offset in range(25)]
    failed_day = eligible_dates[-2]
    next_day = eligible_dates[-1]
    repository = FakeActiveBundleRepository(set(eligible_dates[:-2]))

    pending = _market_overview_pending_dates(
        repository=cast(MarketOverviewRepository, repository),
        eligible_dates=eligible_dates,
        incremental=True,
    )

    assert pending == [failed_day, next_day]
    assert repository.requests == eligible_dates


def test_incremental_run_checks_only_last_twenty_five_sessions_and_skips_active_days() -> None:
    """增量恢复窗口固定为最近 25 个共同交易日，已发布日期保持幂等跳过。"""
    first = date(2026, 5, 1)
    eligible_dates = [first + timedelta(days=offset) for offset in range(30)]
    missing_inside_window = eligible_dates[-1]
    missing_outside_window = eligible_dates[0]
    active_dates = set(eligible_dates)
    active_dates.remove(missing_inside_window)
    active_dates.remove(missing_outside_window)
    repository = FakeActiveBundleRepository(active_dates)

    pending = _market_overview_pending_dates(
        repository=cast(MarketOverviewRepository, repository),
        eligible_dates=eligible_dates,
        incremental=True,
    )

    assert pending == [missing_inside_window]
    assert repository.requests == eligible_dates[-25:]


def test_incremental_run_rejects_a_gap_behind_the_current_tip() -> None:
    """已有更晚 active bundle 时，历史缺口必须显式失败而非发布不一致候选后宣称成功。"""
    eligible_dates = [date(2026, 7, 27), date(2026, 7, 28), date(2026, 7, 29)]
    repository = FakeActiveBundleRepository({eligible_dates[0], eligible_dates[2]})

    with pytest.raises(ValueError, match="controlled chain replay"):
        _market_overview_pending_dates(
            repository=cast(MarketOverviewRepository, repository),
            eligible_dates=eligible_dates,
            incremental=True,
        )
