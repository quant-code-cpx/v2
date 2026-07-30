"""结构化自动计划时间计算与交易日安全边界测试。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, cast

import pytest

from service_data_sync.application.source_registry import SourceRegistry
from service_data_sync.infrastructure.data_operations.control_plane import (
    DataOperationsControlPlane,
    DatasetDefinition,
    OperationProblem,
)
from service_data_sync.infrastructure.data_operations.schedule_engine import (
    ScheduleCalendarUnavailableError,
    ScheduleFrequencyError,
    due_occurrences,
    next_occurrence,
    next_occurrences,
    resolve_observation_date,
    validate_frequency,
)
from service_data_sync.infrastructure.database.connection import DatabaseClient


class FakeTradingCalendar:
    """按测试给定字典返回开市事实，未登记日期明确表示权威日历未知。"""

    def __init__(self, days: dict[date, bool]) -> None:
        """冻结日期到开市结论的最小测试日历。"""
        self._days = days

    def is_open(self, *, trade_date: date) -> bool | None:
        """返回预置开市事实或未知，模拟日历覆盖边界。"""
        return self._days.get(trade_date)


@dataclass(frozen=True, slots=True)
class _Provider:
    """提供计划资格校验所需的稳定测试 provider 身份。"""

    provider_id: str


class FakeSourceRegistry:
    """让控制面确认市场完整包的必需 capability 均由同一来源提供。"""

    def provider_ids(self) -> frozenset[str]:
        """返回唯一测试来源，避免计划测试依赖真实 adapter。"""
        return frozenset({"tushare-pro"})

    def for_capability(self, _capability: str) -> tuple[_Provider, ...]:
        """为任意测试 capability 返回已批准的 Tushare 来源。"""
        return (_Provider("tushare-pro"),)


def _frequency(kind: str, **overrides: object) -> dict[str, object]:
    """构造包含全部 nullable 字段的合同频率，单测只覆盖一个差异点。"""
    value: dict[str, object] = {
        "kind": kind,
        "timezone": "Asia/Shanghai",
        "localTime": "18:00",
        "dayOfWeek": None,
        "dayOfMonth": None,
        "intervalMinutes": None,
        "calendarCode": None,
    }
    value.update(overrides)
    return value


def _market_schedule_validator() -> tuple[DataOperationsControlPlane, DatasetDefinition]:
    """构造只执行市场完整包计划校验、不会访问数据库的控制面。"""
    definition = DatasetDefinition(
        dataset_code="market.overview-and-sectors.bundle",
        display_name="市场概览与行业板块完整包",
        domain="market",
        description="计划边界测试",
        grain="沪深共同交易日",
        capability="market.source.preflight",
        modes=("INCREMENTAL",),
        schedule_modes=("INCREMENTAL",),
        source_capabilities=("market.source.preflight",),
        selector_kinds=("GLOBAL",),
        dispatcher_ready=True,
        config_enabled=True,
        provider_id="tushare-pro",
    )
    control_plane = DataOperationsControlPlane(
        database=cast(DatabaseClient, object()),
        catalog={definition.dataset_code: definition},
        source_registry=cast(SourceRegistry, FakeSourceRegistry()),
    )
    return control_plane, definition


def _market_frequency(**overrides: object) -> dict[str, Any]:
    """构造市场完整包唯一允许的 19:20 沪深共同交易日频率。"""
    value: dict[str, Any] = {
        "kind": "TRADING_DAY",
        "timezone": "Asia/Shanghai",
        "localTime": "19:20",
        "dayOfWeek": None,
        "dayOfMonth": None,
        "intervalMinutes": None,
        "calendarCode": "SSE-SZSE",
    }
    value.update(overrides)
    return value


def test_market_overview_schedule_is_fixed_after_late_money_flow_publication() -> None:
    """完整包只接受 19:20 共同交易日计划，避免 19:00 更新的资金流稳定缺席。"""
    control_plane, definition = _market_schedule_validator()
    policy = {"policyVersion": 1, "dateResolution": "NONE"}

    accepted = control_plane._validate_schedule(
        definition,
        "INCREMENTAL",
        policy,
        _market_frequency(),
    )

    assert accepted == _market_frequency()
    for frequency in (
        _market_frequency(localTime="17:20"),
        _market_frequency(timezone="UTC"),
        _market_frequency(calendarCode="CN_A_SHARE"),
        _market_frequency(
            kind="DAILY",
            calendarCode=None,
        ),
    ):
        with pytest.raises(OperationProblem) as captured:
            control_plane._validate_schedule(
                definition,
                "INCREMENTAL",
                policy,
                frequency,
            )
        assert captured.value.status == 400
        assert captured.value.code == "market-overview-schedule-invalid"

    with pytest.raises(OperationProblem, match="INCREMENTAL") as invalid_mode:
        control_plane._validate_schedule(
            definition,
            "FULL",
            policy,
            _market_frequency(),
        )
    assert invalid_mode.value.status == 400

    with pytest.raises(OperationProblem, match="policy") as invalid_policy:
        control_plane._validate_schedule(
            definition,
            "INCREMENTAL",
            {"policyVersion": 2, "dateResolution": "NONE"},
            _market_frequency(),
        )
    assert invalid_policy.value.status == 400


def test_frequency_enforces_one_of_nulls_and_iana_timezone() -> None:
    """非适用字段必须是 null，非法 IANA 名称不能被服务器环境的默认时区吞掉。"""
    valid = _frequency("WEEKLY", dayOfWeek=5)

    assert validate_frequency(valid) == valid
    with pytest.raises(ScheduleFrequencyError):
        validate_frequency(_frequency("DAILY", dayOfWeek=1))
    with pytest.raises(ScheduleFrequencyError):
        validate_frequency(_frequency("DAILY", timezone="Mars/Olympus"))
    with pytest.raises(ScheduleFrequencyError):
        validate_frequency(_frequency("INTERVAL", localTime=None, intervalMinutes=4))


def test_next_occurrence_honors_local_daily_weekly_and_monthly_wall_clock() -> None:
    """每日、每周和每月都按计划 IANA 时区计算，月 31 日不会被悄悄夹到二月末。"""
    after = datetime(2026, 2, 27, 11, 0, tzinfo=UTC)

    assert next_occurrence(_frequency("DAILY"), after) == datetime(2026, 2, 28, 10, 0, tzinfo=UTC)
    assert next_occurrence(_frequency("WEEKLY", dayOfWeek=1), after) == datetime(
        2026, 3, 2, 10, 0, tzinfo=UTC
    )
    assert next_occurrence(_frequency("MONTHLY", dayOfMonth=31), after) == datetime(
        2026, 3, 31, 10, 0, tzinfo=UTC
    )


def test_trading_day_and_latest_completed_resolution_fail_closed_when_calendar_is_unknown() -> None:
    """交易日计划跳过已知休市日，但日历未知时不以自然日伪造 observationDate。"""
    calendar = FakeTradingCalendar(
        {
            date(2026, 7, 31): False,
            date(2026, 8, 1): False,
            date(2026, 8, 2): False,
            date(2026, 8, 3): True,
        }
    )
    frequency = _frequency("TRADING_DAY", calendarCode="CN_A_SHARE")
    after = datetime(2026, 7, 31, 11, 0, tzinfo=UTC)

    assert next_occurrence(frequency, after, calendar=calendar) == datetime(
        2026, 8, 3, 10, 0, tzinfo=UTC
    )
    assert resolve_observation_date(
        frequency,
        "LATEST_COMPLETED_TRADING_DATE",
        datetime(2026, 8, 3, 10, 0, tzinfo=UTC),
        calendar=calendar,
    ) == date(2026, 8, 3)
    with pytest.raises(ScheduleCalendarUnavailableError):
        resolve_observation_date(
            frequency,
            "LATEST_COMPLETED_TRADING_DATE",
            datetime(2026, 8, 4, 10, 0, tzinfo=UTC),
            calendar=calendar,
        )


def test_latest_completed_trading_date_does_not_select_an_open_session_before_close() -> None:
    """早于收市的触发只能绑定前一已完成交易日，不能把当日盘中快照当 EOD。"""
    calendar = FakeTradingCalendar(
        {
            date(2026, 7, 31): True,
            date(2026, 8, 1): False,
            date(2026, 8, 2): False,
            date(2026, 8, 3): True,
        }
    )
    frequency = _frequency("TRADING_DAY", calendarCode="CN_A_SHARE")

    assert resolve_observation_date(
        frequency,
        "LATEST_COMPLETED_TRADING_DATE",
        datetime(2026, 8, 3, 2, 0, tzinfo=UTC),
        calendar=calendar,
    ) == date(2026, 7, 31)
    assert resolve_observation_date(
        frequency,
        "LATEST_COMPLETED_TRADING_DATE",
        datetime(2026, 8, 3, 8, 0, tzinfo=UTC),
        calendar=calendar,
    ) == date(2026, 8, 3)


def test_due_occurrences_is_bounded_and_future_preview_uses_the_same_calculation() -> None:
    """延迟 tick 的 due 集合有上界，计划详情预览不会采用另一套近似算法。"""
    frequency = _frequency("INTERVAL", localTime=None, intervalMinutes=60)
    first_due = datetime(2026, 7, 29, 0, 0, tzinfo=UTC)
    now = datetime(2026, 7, 29, 2, 30, tzinfo=UTC)

    due, next_due = due_occurrences(frequency, first_due, now)

    assert due == [
        datetime(2026, 7, 29, 0, 0, tzinfo=UTC),
        datetime(2026, 7, 29, 1, 0, tzinfo=UTC),
        datetime(2026, 7, 29, 2, 0, tzinfo=UTC),
    ]
    assert next_due == datetime(2026, 7, 29, 3, 0, tzinfo=UTC)
    assert next_occurrences(frequency, now, count=2) == [
        datetime(2026, 7, 29, 3, 30, tzinfo=UTC),
        datetime(2026, 7, 29, 4, 30, tzinfo=UTC),
    ]
