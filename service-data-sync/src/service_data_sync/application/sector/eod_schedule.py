"""板块 EOD 的冻结 shadow 时序策略，不把它误写成交易所官方终态。"""

from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

_SHANGHAI = ZoneInfo("Asia/Shanghai")


def sector_eod_source_cutoff_at(trade_date: date) -> datetime:
    """为明确交易日生成 16:15 上海时区观察截点，日历开市校验由独立端口负责。"""
    return datetime.combine(trade_date, time(16, 15), tzinfo=_SHANGHAI)


def sector_eod_scheduler_target_date(now: datetime) -> date:
    """将调度器时钟转换到上海日期；调用方仍必须经权威日历确认开市。"""
    if now.tzinfo is None:
        raise ValueError("sector eod scheduler clock must include a timezone")
    return now.astimezone(_SHANGHAI).date()
