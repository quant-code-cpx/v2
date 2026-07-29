"""未接入权威交易日历时的安全默认实现。

这个实现刻意不根据周末、法定节假日或历史经验猜测开市状态。调用方收到
``None`` 必须停止 `EOD` 发布；这样宁可暂缓一批数据，也不会把休市日当作正常
交易日并制造无法撤回的空集或行情版本。
"""

from __future__ import annotations

from datetime import date


class UnavailableTradingCalendar:
    """始终返回未知，确保 EOD 执行必须等待权威日历接入。

    它不是“周末规则”的简化实现，而是一个 `fail-closed`（无法确认即停止）边界。
    """

    def is_open(self, *, trade_date: date) -> bool | None:
        """返回未知而非推断结果，避免错误发布到非交易日。

        ``trade_date`` 仍保留在接口中，使真实日历实现可无缝替换本安全默认值。
        """
        del trade_date
        return None
