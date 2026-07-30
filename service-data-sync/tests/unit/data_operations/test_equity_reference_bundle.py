"""股票中心引用 bundle 日期边界与七步目标的纯逻辑回归。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import cast
from zoneinfo import ZoneInfo

import pytest

from service_data_sync.application.ports.trading_calendar import TradingCalendarPort
from service_data_sync.infrastructure.data_operations.control_plane import (
    DataOperationsControlPlane,
)
from service_data_sync.infrastructure.data_operations.equity_reference_bundle import (
    EquityReferenceBundleOrchestrator,
    EquityReferenceGenerationError,
    _step_specs,
)
from service_data_sync.infrastructure.database.connection import DatabaseClient

_SHANGHAI = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True, slots=True)
class _Calendar:
    """按显式映射返回开市、休市或未知，未列日期一律视为休市。"""

    states: dict[date, bool | None]

    def is_open(self, *, trade_date: date) -> bool | None:
        """返回测试登记的权威日历结论。"""
        return self.states.get(trade_date, False)


def _orchestrator(calendar: _Calendar) -> EquityReferenceBundleOrchestrator:
    """构造只调用日期纯逻辑的编排器，数据库与控制面不会被访问。"""
    return EquityReferenceBundleOrchestrator(
        database=cast(DatabaseClient, object()),
        control_plane=cast(DataOperationsControlPlane, object()),
        trading_calendar=cast(TradingCalendarPort, calendar),
        poll_interval_seconds=0,
    )


def test_market_boundary_uses_previous_open_day_before_cutoff() -> None:
    """周五 16:15 前不得把当日伪装成已完整 EOD。"""
    thursday = date(2024, 3, 14)
    friday = date(2024, 3, 15)
    orchestrator = _orchestrator(_Calendar({thursday: True, friday: True}))

    assert orchestrator._boundaries(
        datetime(2024, 3, 15, 16, 14, tzinfo=_SHANGHAI)
    ) == (friday, thursday)


def test_market_boundary_accepts_open_day_at_cutoff_and_weekend_keeps_friday() -> None:
    """截点到达后选择当日，周末则保持最近已完整周五。"""
    friday = date(2024, 3, 15)
    saturday = date(2024, 3, 16)
    sunday = date(2024, 3, 17)
    orchestrator = _orchestrator(
        _Calendar({friday: True, saturday: False, sunday: False})
    )

    assert orchestrator._boundaries(
        datetime(2024, 3, 15, 16, 15, tzinfo=_SHANGHAI)
    ) == (friday, friday)
    assert orchestrator._boundaries(
        datetime(2024, 3, 17, 23, 59, tzinfo=_SHANGHAI)
    ) == (sunday, friday)


def test_market_boundary_fails_closed_when_calendar_is_unknown() -> None:
    """候选日期日历未知时立即失败，不能继续按工作日猜测。"""
    friday = date(2024, 3, 15)
    orchestrator = _orchestrator(_Calendar({friday: None}))

    with pytest.raises(
        EquityReferenceGenerationError,
        match="authoritative trading calendar is unavailable",
    ):
        orchestrator._boundaries(
            datetime(2024, 3, 15, 18, 0, tzinfo=_SHANGHAI)
        )


def test_step_specs_freeze_all_reference_targets_without_mixed_dates() -> None:
    """七步目标顺序、数据集和观察日必须完整且可直接提交控制面。"""
    snapshot = date(2024, 3, 17)
    market = date(2024, 3, 15)

    steps = _step_specs(
        snapshot_observed_on=snapshot,
        market_as_of=market,
    )

    assert [item[0] for item in steps] == list(range(1, 8))
    assert [item[2]["datasetCode"] for item in steps] == [
        "equity.master.cn-a",
        "equity.lifecycle.explicit",
        "sector.catalog.raw",
        "sector.membership.release",
        "sector.sw.taxonomy",
        "sector.sw2021.membership.snapshot",
        "equity.trading_status.1d",
    ]
    assert [item[2]["observationDate"] for item in steps] == [
        None,
        None,
        snapshot.isoformat(),
        snapshot.isoformat(),
        snapshot.isoformat(),
        snapshot.isoformat(),
        market.isoformat(),
    ]
    assert all(item[2]["selector"] == {"kind": "GLOBAL"} for item in steps)
