"""依据沪深交易所已发布公告固化的 2026 年 A 股交易日历。"""

from __future__ import annotations

from datetime import date

_CALENDAR_YEAR = 2026
# 此集合只保留工作日休市日；周末由两所交易所的常规休市规则覆盖。
# 依据上交所、深交所 2025-12-22 发布的 2026 年部分节假日休市安排逐项录入。
_OFFICIAL_CLOSED_WEEKDAYS = frozenset(
    {
        date(2026, 1, 1),
        date(2026, 1, 2),
        date(2026, 2, 16),
        date(2026, 2, 17),
        date(2026, 2, 18),
        date(2026, 2, 19),
        date(2026, 2, 20),
        date(2026, 2, 23),
        date(2026, 4, 6),
        date(2026, 5, 1),
        date(2026, 5, 4),
        date(2026, 5, 5),
        date(2026, 6, 19),
        date(2026, 9, 25),
        date(2026, 10, 1),
        date(2026, 10, 2),
        date(2026, 10, 5),
        date(2026, 10, 6),
        date(2026, 10, 7),
    }
)


class SseSzseAshare2026TradingCalendar:
    """仅为 2026 年已双交易所核验的 A 股日期返回确定结论，其他年份返回未知。"""

    def is_open(self, *, trade_date: date) -> bool | None:
        """按已发布年度日历判断开市；不以推测方式扩展到未发布年份。"""
        if trade_date.year != _CALENDAR_YEAR:
            return None
        if trade_date.weekday() >= 5:
            return False
        return trade_date not in _OFFICIAL_CLOSED_WEEKDAYS
