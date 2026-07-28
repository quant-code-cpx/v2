"""未接入权威日历前的安全默认实现。"""

from __future__ import annotations

from datetime import date


class UnavailableTradingCalendar:
    """始终返回未知，确保任何 EOD 实际执行在日历接入前安全停止。"""

    def is_open(self, *, trade_date: date) -> bool | None:
        """不按周末或中国节假日推断开市状态，避免错误发布到非交易日。"""
        del trade_date
        return None
