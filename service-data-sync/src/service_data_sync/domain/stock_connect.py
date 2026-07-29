"""沪深港通 `P0` 的通道统计与活跃证券领域值。

通道、资金方向和市场统计分别建模，防止一个字符串混合多重业务含义。
不同披露制度导致的字段缺失必须保留为空并携带可用性状态，不能用其他榜单、持仓或估算结果补齐。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class StockConnectChannel:
    """表示沪或深通道与北向/南向方向，不能用一个字符串混写通道、方向和市场。"""

    channel: str
    direction: str

    def __post_init__(self) -> None:
        """限制 P0 官方披露的通道和方向组合。"""
        if self.channel not in {"SH", "SZ"}:
            raise ValueError("stock-connect channel must be SH or SZ")
        if self.direction not in {"NORTHBOUND", "SOUTHBOUND"}:
            raise ValueError("stock-connect direction is invalid")


@dataclass(frozen=True, slots=True)
class StockConnectMarketDaily:
    """表示官方通道日终统计，制度未披露的金额保持空值并以状态解释。"""

    trade_date: date
    buy_amount: Decimal | None
    sell_amount: Decimal | None
    turnover_amount: Decimal | None
    net_buy_amount: Decimal | None
    quota_balance: Decimal | None
    currency: str
    availability_status: str

    def __post_init__(self) -> None:
        """校验金额边界和披露状态，净买允许负值而成交、买卖和额度不能为负。"""
        non_negative = (
            self.buy_amount,
            self.sell_amount,
            self.turnover_amount,
            self.quota_balance,
        )
        if any(
            value is not None and (not value.is_finite() or value < 0) for value in non_negative
        ):
            raise ValueError("stock-connect reported amounts must be finite and non-negative")
        if self.net_buy_amount is not None and not self.net_buy_amount.is_finite():
            raise ValueError("stock-connect net buy must be finite")
        if (
            len(self.currency) != 3
            or self.currency != self.currency.upper()
            or not self.currency.isascii()
        ):
            raise ValueError("stock-connect currency must be an ISO uppercase code")
        if self.availability_status not in {"COMPLETE", "PARTIAL", "DISCLOSURE_UNAVAILABLE"}:
            raise ValueError("stock-connect availability status is invalid")
        if self.availability_status == "DISCLOSURE_UNAVAILABLE" and any(
            value is not None
            for value in (
                self.buy_amount,
                self.sell_amount,
                self.turnover_amount,
                self.net_buy_amount,
                self.quota_balance,
            )
        ):
            raise ValueError("unavailable stock-connect disclosure must not contain amounts")


@dataclass(frozen=True, slots=True)
class StockConnectActiveSecurity:
    """表示通道日终活跃证券榜的一行来源事实，证券身份解析在发布前单独执行。"""

    source_instrument_code: str
    trade_date: date
    rank_no: int
    buy_amount: Decimal | None
    sell_amount: Decimal | None
    turnover_amount: Decimal | None
    currency: str

    def __post_init__(self) -> None:
        """校验排行榜粒度与金额，不能以证券代码跨市场静默合并或把缺失额填零。"""
        if not self.source_instrument_code.strip():
            raise ValueError("stock-connect active security source code must not be blank")
        if self.rank_no < 1:
            raise ValueError("stock-connect active security rank must be positive")
        values = (self.buy_amount, self.sell_amount, self.turnover_amount)
        if not any(value is not None for value in values):
            raise ValueError("stock-connect active security requires a reported amount")
        if any(value is not None and (not value.is_finite() or value < 0) for value in values):
            raise ValueError(
                "stock-connect active security amounts must be finite and non-negative"
            )
        if (
            len(self.currency) != 3
            or self.currency != self.currency.upper()
            or not self.currency.isascii()
        ):
            raise ValueError("stock-connect active security currency must be an ISO uppercase code")
