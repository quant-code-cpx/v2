"""A 股标准证券身份与日线值对象。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum


class Exchange(StrEnum):
    """使用标准标识枚举当前支持的中国证券交易所。"""

    SSE = "SSE"
    SZSE = "SZSE"
    BSE = "BSE"


@dataclass(frozen=True, slots=True)
class EquityIdentifier:
    """表示一个带交易所限定的六位 A 股代码。"""

    exchange: Exchange
    symbol: str

    def __post_init__(self) -> None:
        """拒绝可能跨交易所或资产类别冲突的证券标识。"""
        if len(self.symbol) != 6 or not self.symbol.isascii() or not self.symbol.isdigit():
            raise ValueError("symbol must be a six-digit code")

    @property
    def qualified_symbol(self) -> str:
        """生成供分区、API 与日志使用的稳定外部代码。"""
        return f"{self.exchange.value}.{self.symbol}"

    @classmethod
    def parse(cls, value: str) -> EquityIdentifier:
        """解析并校验标准的 `EXCHANGE.SYMBOL` 证券标识。"""
        exchange_text, separator, symbol = value.strip().upper().partition(".")
        if separator != ".":
            raise ValueError("instrument must use EXCHANGE.SYMBOL format")
        try:
            exchange = Exchange(exchange_text)
        except ValueError as error:
            raise ValueError("instrument exchange must be SSE, SZSE, or BSE") from error
        return cls(exchange=exchange, symbol=symbol)


@dataclass(frozen=True, slots=True)
class EquityDailyBar:
    """保存单位已规范化的一条标准未复权日线。"""

    trade_date: date
    open_price: Decimal
    high_price: Decimal
    low_price: Decimal
    close_price: Decimal
    volume_shares: int
    amount_cny: Decimal
    turnover_rate: Decimal | None

    def __post_init__(self) -> None:
        """在持久化前强制校验 OHLC 与成交量、成交额非负不变量。"""
        if self.low_price > min(self.open_price, self.close_price):
            raise ValueError("low price exceeds open or close")
        if self.high_price < max(self.open_price, self.close_price):
            raise ValueError("high price is below open or close")
        if self.low_price < 0 or self.high_price < 0 or self.open_price < 0 or self.close_price < 0:
            raise ValueError("prices must be non-negative")
        if self.volume_shares < 0:
            raise ValueError("volume_shares must be non-negative")
        if self.amount_cny < 0:
            raise ValueError("amount_cny must be non-negative")
        if self.turnover_rate is not None and self.turnover_rate < 0:
            raise ValueError("turnover_rate must be non-negative")

    def implied_vwap(self) -> Decimal | None:
        """仅在成交量非零时返回成交额除以股数得到的 VWAP。"""
        if self.volume_shares == 0:
            return None
        return self.amount_cny / Decimal(self.volume_shares)
