"""龙虎榜和大宗交易 P0 的披露事实，排除所有事后收益与供应商解读。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class DragonTigerSeat:
    """表示一个龙虎榜事件的一侧席位，买卖榜单和排名必须物理区分。"""

    list_side: str
    rank: int
    seat_code: str | None
    seat_name: str
    buy_amount: Decimal
    sell_amount: Decimal
    net_amount: Decimal
    buy_ratio: Decimal | None
    sell_ratio: Decimal | None

    def __post_init__(self) -> None:
        """校验席位金额、排名和原始名称，净额只接受同一披露行的买卖差。"""
        if self.list_side not in {"BUY", "SELL"}:
            raise ValueError("dragon tiger seat side must be BUY or SELL")
        if self.rank < 1:
            raise ValueError("dragon tiger seat rank must be positive")
        if not self.seat_name.strip():
            raise ValueError("dragon tiger seat name must not be blank")
        _validate_nonnegative(self.buy_amount, self.sell_amount)
        _validate_finite(self.net_amount, self.buy_ratio, self.sell_ratio)
        if abs(self.net_amount - (self.buy_amount - self.sell_amount)) > Decimal("0.01"):
            raise ValueError("dragon tiger seat net must equal buy minus sell")
        if self.buy_ratio is not None and self.buy_ratio < 0:
            raise ValueError("dragon tiger buy ratio must be non-negative")
        if self.sell_ratio is not None and self.sell_ratio < 0:
            raise ValueError("dragon tiger sell ratio must be non-negative")


@dataclass(frozen=True, slots=True)
class DragonTigerEvent:
    """表示“证券 × 交易日 × 上榜原因”的龙虎榜披露头及其榜单席位。"""

    source_event_key: str
    source_security_code: str
    trade_date: date
    reason_code: str
    reason_text: str
    close_price: Decimal | None
    buy_amount: Decimal
    sell_amount: Decimal
    net_amount: Decimal
    deal_amount: Decimal
    market_turnover_amount: Decimal | None
    deal_ratio: Decimal | None
    net_ratio: Decimal | None
    turnover_ratio: Decimal | None
    source_published_at: datetime | None
    visible_time_precision: str
    visible_at: datetime
    seats: tuple[DragonTigerSeat, ...]

    def __post_init__(self) -> None:
        """验证事件身份、金额恒等和可见时间；不允许用后续表现替代当日公开事实。"""
        if not all(
            (
                self.source_event_key.strip(),
                self.source_security_code.strip(),
                self.reason_code.strip(),
                self.reason_text.strip(),
            )
        ):
            raise ValueError("dragon tiger event identity fields must not be blank")
        _validate_positive_optional(self.close_price, self.market_turnover_amount)
        _validate_nonnegative(self.buy_amount, self.sell_amount, self.deal_amount)
        _validate_finite(self.net_amount, self.deal_ratio, self.net_ratio, self.turnover_ratio)
        if abs(self.net_amount - (self.buy_amount - self.sell_amount)) > Decimal("0.01"):
            raise ValueError("dragon tiger net must equal buy minus sell")
        if abs(self.deal_amount - (self.buy_amount + self.sell_amount)) > Decimal("0.01"):
            raise ValueError("dragon tiger deal must equal buy plus sell")
        # 买入成交占比与换手率不能为负；净买占比可因卖出额高于买入额为负。
        if any(value is not None and value < 0 for value in (self.deal_ratio, self.turnover_ratio)):
            raise ValueError("dragon tiger deal and turnover ratios must be non-negative")
        _validate_visibility(
            source_published_at=self.source_published_at,
            precision=self.visible_time_precision,
            visible_at=self.visible_at,
        )
        if not self.seats:
            raise ValueError("dragon tiger event requires at least one disclosed seat")
        if len({(seat.list_side, seat.rank) for seat in self.seats}) != len(self.seats):
            raise ValueError("dragon tiger seats must be unique by side and rank")


@dataclass(frozen=True, slots=True)
class BlockTrade:
    """表示 A 股大宗交易的一笔来源成交；相同经济字段的合法重数由 occurrence 保留。"""

    source_trade_key: str
    source_security_code: str
    trade_date: date
    occurrence_no: int
    execution_price: Decimal
    quantity_shares: int
    notional_cny: Decimal
    buyer_seat_code: str | None
    buyer_seat_name: str
    seller_seat_code: str | None
    seller_seat_name: str
    reference_close_price: Decimal | None
    premium_discount_ratio: Decimal | None
    source_daily_rank: int | None
    source_published_at: datetime | None
    visible_time_precision: str
    visible_at: datetime

    def __post_init__(self) -> None:
        """校验逐笔数量、金额与可见时间，不按经济字段哈希去重或推断交易对手身份。"""
        if not all(
            (
                self.source_trade_key.strip(),
                self.source_security_code.strip(),
                self.buyer_seat_name.strip(),
                self.seller_seat_name.strip(),
            )
        ):
            raise ValueError("block trade identity fields must not be blank")
        if self.occurrence_no < 1 or self.quantity_shares < 1:
            raise ValueError("block trade occurrence and quantity must be positive")
        if self.source_daily_rank is not None and self.source_daily_rank < 1:
            raise ValueError("block trade source daily rank must be positive")
        _validate_positive_optional(
            self.execution_price, self.notional_cny, self.reference_close_price
        )
        _validate_finite(self.premium_discount_ratio)
        expected_notional = self.execution_price * self.quantity_shares
        if abs(self.notional_cny - expected_notional) > Decimal("0.01"):
            raise ValueError("block trade notional must match price times quantity")
        _validate_visibility(
            source_published_at=self.source_published_at,
            precision=self.visible_time_precision,
            visible_at=self.visible_at,
        )


def _validate_visibility(
    *, source_published_at: datetime | None, precision: str, visible_at: datetime
) -> None:
    """校验来源发布时间和安全可见时间，日期级披露绝不伪装成当天零点可交易。"""
    if visible_at.tzinfo is None:
        raise ValueError("trading event visible time must include timezone")
    if precision == "EXACT":
        if source_published_at is None or source_published_at.tzinfo is None:
            raise ValueError("exact trading visibility requires source publication time")
        if visible_at < source_published_at:
            raise ValueError("trading event visibility must not precede source publication")
        return
    if precision == "DATE_ONLY":
        if source_published_at is not None:
            raise ValueError("date-only trading visibility must not invent exact source time")
        if visible_at.hour == 0 and visible_at.minute == 0:
            raise ValueError("date-only trading visibility requires a conservative session time")
        return
    if precision == "OBSERVED_ONLY" and source_published_at is None:
        return
    raise ValueError("unsupported trading visibility precision")


def _validate_nonnegative(*values: Decimal) -> None:
    """校验有限且非负的披露金额，零值是可保留的真实交易事实。"""
    if any(not value.is_finite() or value < 0 for value in values):
        raise ValueError("trading amounts must be finite and non-negative")


def _validate_positive_optional(*values: Decimal | None) -> None:
    """校验可选价格或金额；缺失保持空值，出现的值必须是有限正数。"""
    if any(value is not None and (not value.is_finite() or value <= 0) for value in values):
        raise ValueError("trading prices and notionals must be finite and positive")


def _validate_finite(*values: Decimal | None) -> None:
    """拒绝 NaN 和无穷大，同时允许来源报告的负折溢价或净额。"""
    if any(value is not None and not value.is_finite() for value in values):
        raise ValueError("trading values must be finite")
