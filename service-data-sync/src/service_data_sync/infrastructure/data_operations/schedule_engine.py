"""数据运维结构化计划的频率、交易日与目标日期纯计算。

本模块不访问数据库、不投递 Celery，也不创建 command；调用方必须先持有计划行锁，再把这里
返回的确定性时刻、目标日期和版本快照持久化为 schedule fire。这样双 scheduler、重启或延迟
tick 不会把本地系统时间猜测混入权威账本。
"""

from __future__ import annotations

from calendar import monthrange
from collections.abc import Mapping
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class ScheduleFrequencyError(ValueError):
    """结构化频率违反 0022 oneOf、IANA 时区或字段空值约束时抛出。"""


class ScheduleCalendarUnavailableError(RuntimeError):
    """权威交易日历缺失、未覆盖或无法确认开市时抛出，调用方必须安全跳过 fire。"""


class TradingCalendar(Protocol):
    """为已绑定 calendarCode 查询业务日是否开市的最小只读能力。"""

    def is_open(self, *, trade_date: date) -> bool | None:
        """返回开市、休市或未知；未知不能被调度器替换成周末推断。"""
        ...


_FREQUENCY_KEYS = {
    "kind",
    "timezone",
    "localTime",
    "dayOfWeek",
    "dayOfMonth",
    "intervalMinutes",
    "calendarCode",
}
_SCHEDULE_KINDS = {"TRADING_DAY", "DAILY", "WEEKLY", "MONTHLY", "INTERVAL"}
_DATE_RESOLUTIONS = {"SCHEDULED_LOCAL_DATE", "LATEST_COMPLETED_TRADING_DATE"}
_SSE_SZSE_REGULAR_CLOSE = (15, 0, 0, 0)


def validate_frequency(frequency: Mapping[str, Any]) -> dict[str, Any]:
    """严格校验并复制 0022 `ScheduleFrequency`，使无关字段必须显式为 null。"""
    if set(frequency) != _FREQUENCY_KEYS:
        raise ScheduleFrequencyError("schedule frequency fields are invalid")
    kind = frequency.get("kind")
    timezone_name = frequency.get("timezone")
    if (
        kind not in _SCHEDULE_KINDS
        or not isinstance(timezone_name, str)
        or not timezone_name
        or len(timezone_name) > 80
    ):
        raise ScheduleFrequencyError("schedule frequency kind or timezone is invalid")
    try:
        ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as error:
        raise ScheduleFrequencyError("schedule timezone is not an IANA zone") from error

    local_time = frequency.get("localTime")
    day_of_week = frequency.get("dayOfWeek")
    day_of_month = frequency.get("dayOfMonth")
    interval_minutes = frequency.get("intervalMinutes")
    calendar_code = frequency.get("calendarCode")
    if kind == "INTERVAL":
        if (
            local_time is not None
            or day_of_week is not None
            or day_of_month is not None
            or calendar_code is not None
            or not isinstance(interval_minutes, int)
            or isinstance(interval_minutes, bool)
            or not 5 <= interval_minutes <= 43200
        ):
            raise ScheduleFrequencyError("interval schedule fields are invalid")
    else:
        _parse_local_time(local_time)
        if interval_minutes is not None:
            raise ScheduleFrequencyError("non-interval schedule intervalMinutes must be null")
        if kind == "TRADING_DAY":
            if not isinstance(calendar_code, str) or not calendar_code or len(calendar_code) > 80:
                raise ScheduleFrequencyError("trading-day schedule needs calendarCode")
            if day_of_week is not None or day_of_month is not None:
                raise ScheduleFrequencyError("trading-day schedule has unrelated day fields")
        elif kind == "DAILY":
            if calendar_code is not None or day_of_week is not None or day_of_month is not None:
                raise ScheduleFrequencyError("daily schedule has unrelated fields")
        elif kind == "WEEKLY":
            if (
                calendar_code is not None
                or day_of_month is not None
                or not isinstance(day_of_week, int)
                or isinstance(day_of_week, bool)
                or not 1 <= day_of_week <= 7
            ):
                raise ScheduleFrequencyError("weekly schedule fields are invalid")
        elif kind == "MONTHLY":
            if (
                calendar_code is not None
                or day_of_week is not None
                or not isinstance(day_of_month, int)
                or isinstance(day_of_month, bool)
                or not 1 <= day_of_month <= 31
            ):
                raise ScheduleFrequencyError("monthly schedule fields are invalid")
    return dict(frequency)


def next_occurrence(
    frequency: Mapping[str, Any],
    after: datetime,
    *,
    calendar: TradingCalendar | None = None,
) -> datetime:
    """返回严格晚于 `after` 的下一次 UTC fire；交易日频率只使用权威日历。"""
    normalized = validate_frequency(frequency)
    instant = _require_aware(after)
    if normalized["kind"] == "INTERVAL":
        return instant + timedelta(minutes=normalized["intervalMinutes"])
    zone = ZoneInfo(normalized["timezone"])
    local_after = instant.astimezone(zone)
    local_time = _parse_local_time(normalized["localTime"])
    if normalized["kind"] == "DAILY":
        candidate = _local_datetime(local_after.date(), local_time, zone)
        if candidate <= local_after:
            candidate = _local_datetime(local_after.date() + timedelta(days=1), local_time, zone)
        return candidate.astimezone(UTC)
    if normalized["kind"] == "WEEKLY":
        return _next_weekly(local_after, local_time, normalized["dayOfWeek"], zone).astimezone(UTC)
    if normalized["kind"] == "MONTHLY":
        return _next_monthly(local_after, local_time, normalized["dayOfMonth"], zone).astimezone(
            UTC
        )
    return _next_trading_day(local_after, local_time, zone, calendar).astimezone(UTC)


def next_occurrences(
    frequency: Mapping[str, Any],
    after: datetime,
    *,
    calendar: TradingCalendar | None = None,
    count: int = 5,
) -> list[datetime]:
    """计算至多五个未来 fire，供计划详情展示，不把频率解释委托给 Web。"""
    if not 1 <= count <= 5:
        raise ValueError("schedule occurrence count must be between 1 and 5")
    values: list[datetime] = []
    cursor = _require_aware(after)
    for _ in range(count):
        cursor = next_occurrence(frequency, cursor, calendar=calendar)
        values.append(cursor)
    return values


def due_occurrences(
    frequency: Mapping[str, Any],
    first_due: datetime,
    now: datetime,
    *,
    calendar: TradingCalendar | None = None,
    maximum: int = 512,
) -> tuple[list[datetime], datetime]:
    """返回不晚于 `now` 的受限 due fire 与下一次未到期时刻，防止恢复风暴。"""
    if maximum < 1:
        raise ValueError("schedule due occurrence maximum must be positive")
    current = _require_aware(first_due)
    upper_bound = _require_aware(now)
    values: list[datetime] = []
    while current <= upper_bound and len(values) < maximum:
        values.append(current)
        current = next_occurrence(frequency, current, calendar=calendar)
    if current <= upper_bound:
        raise ScheduleFrequencyError("schedule missed occurrence limit exceeded")
    return values, current


def resolve_observation_date(
    frequency: Mapping[str, Any],
    date_resolution: str,
    scheduled_for: datetime,
    *,
    calendar: TradingCalendar | None = None,
) -> date:
    """按冻结 scheduledFor 和版本化策略解析 OBSERVATION_DATE，绝不以 tick 的 now 猜测。"""
    normalized = validate_frequency(frequency)
    if date_resolution not in _DATE_RESOLUTIONS:
        raise ScheduleFrequencyError("schedule date resolution is invalid")
    local_scheduled_for = _require_aware(scheduled_for).astimezone(ZoneInfo(normalized["timezone"]))
    local_date = local_scheduled_for.date()
    if date_resolution == "SCHEDULED_LOCAL_DATE":
        return local_date
    if calendar is None:
        raise ScheduleCalendarUnavailableError("trading calendar is unavailable")
    # v1 的注册表仅提供沪深 A 股常规交易日历。计划时刻早于常规收市不能把尚在
    # 交易中的当天当成“已完成”，否则 EOD 快照会在事实尚未稳定时发布。
    local_time_tuple = (
        local_scheduled_for.hour,
        local_scheduled_for.minute,
        local_scheduled_for.second,
        local_scheduled_for.microsecond,
    )
    if local_time_tuple < _SSE_SZSE_REGULAR_CLOSE:
        local_date -= timedelta(days=1)
    for offset in range(0, 366 * 3):
        candidate = local_date - timedelta(days=offset)
        is_open = calendar.is_open(trade_date=candidate)
        if is_open is None:
            raise ScheduleCalendarUnavailableError("trading calendar coverage is unavailable")
        if is_open:
            return candidate
    raise ScheduleCalendarUnavailableError("completed trading day is unavailable")


def _parse_local_time(value: object) -> time:
    """解析合同 HH:MM 本地时间，拒绝秒、时区和宽松 ISO 格式。"""
    if not isinstance(value, str) or len(value) != 5 or value[2] != ":":
        raise ScheduleFrequencyError("schedule localTime is invalid")
    try:
        parsed = time.fromisoformat(value)
    except ValueError as error:
        raise ScheduleFrequencyError("schedule localTime is invalid") from error
    if parsed.second != 0 or parsed.microsecond != 0:
        raise ScheduleFrequencyError("schedule localTime is invalid")
    return parsed


def _require_aware(value: datetime) -> datetime:
    """规范化带时区时刻到 UTC；计划器不得接受无时区的机器本地时间。"""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ScheduleFrequencyError("schedule instant must include timezone")
    return value.astimezone(UTC)


def _local_datetime(value: date, local_time: time, zone: ZoneInfo) -> datetime:
    """构造计划时区中的当地 wall-clock 时刻，输出仍由调用方转换为 UTC。"""
    return datetime.combine(value, local_time, tzinfo=zone)


def _next_weekly(
    local_after: datetime, local_time: time, day_of_week: int, zone: ZoneInfo
) -> datetime:
    """计算 ISO 周序号的下一次周计划，不把 Sunday=7 错当 Python weekday=6。"""
    offset = (day_of_week - local_after.isoweekday()) % 7
    candidate = _local_datetime(local_after.date() + timedelta(days=offset), local_time, zone)
    if candidate <= local_after:
        candidate = _local_datetime(candidate.date() + timedelta(days=7), local_time, zone)
    return candidate


def _next_monthly(
    local_after: datetime, local_time: time, day_of_month: int, zone: ZoneInfo
) -> datetime:
    """选择实际存在的指定日；例如 31 日在二月跳过，而不擅自改成月末。"""
    year = local_after.year
    month = local_after.month
    for _ in range(48):
        if day_of_month <= monthrange(year, month)[1]:
            candidate = _local_datetime(date(year, month, day_of_month), local_time, zone)
            if candidate > local_after:
                return candidate
        year, month = _next_month(year, month)
    raise ScheduleFrequencyError("monthly schedule has no future occurrence")


def _next_trading_day(
    local_after: datetime,
    local_time: time,
    zone: ZoneInfo,
    calendar: TradingCalendar | None,
) -> datetime:
    """从本地候选日起寻找已确认开市日；未知日历范围立即 fail closed。"""
    if calendar is None:
        raise ScheduleCalendarUnavailableError("trading calendar is unavailable")
    candidate_date = local_after.date()
    candidate = _local_datetime(candidate_date, local_time, zone)
    if candidate <= local_after:
        candidate_date += timedelta(days=1)
    for _ in range(366 * 3):
        is_open = calendar.is_open(trade_date=candidate_date)
        if is_open is None:
            raise ScheduleCalendarUnavailableError("trading calendar coverage is unavailable")
        if is_open:
            return _local_datetime(candidate_date, local_time, zone)
        candidate_date += timedelta(days=1)
    raise ScheduleCalendarUnavailableError("next trading day is unavailable")


def _next_month(year: int, month: int) -> tuple[int, int]:
    """返回紧随给定年月的自然月，不依赖机器本地时区。"""
    return (year + 1, 1) if month == 12 else (year, month + 1)
