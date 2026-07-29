"""已发布交易日历的中立读取端口。

板块 `EOD` 必须依据明确市场、时区与交易日历判断是否可运行。
普通工作日、系统时钟或供应商默认日期都不能代替该事实。
"""

from __future__ import annotations

from datetime import date
from typing import Protocol


class TradingCalendarUnavailableError(RuntimeError):
    """尚无权威日历、日期未知或日历版本不允许用于 EOD 时抛出。"""

    pass


class TradingCalendarPort(Protocol):
    """读取已发布的中国 A 股交易日历，不承担抓取或推断日历来源的职责。"""

    def is_open(self, *, trade_date: date) -> bool | None:
        """返回开市、休市或未知；未知必须阻止 EOD 任务而不能按工作日猜测。"""
        ...


def require_open_trading_day(calendar: TradingCalendarPort, *, trade_date: date) -> None:
    """要求目标日被权威日历明确标记为开市日，拒绝未知和休市日期。"""
    is_open = calendar.is_open(trade_date=trade_date)
    if is_open is None:
        raise TradingCalendarUnavailableError("authoritative trading calendar is unavailable")
    if not is_open:
        raise TradingCalendarUnavailableError("target date is not an open trading day")
