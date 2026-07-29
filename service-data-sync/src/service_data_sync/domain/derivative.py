"""真实衍生品合约及其交易所日行情的领域值对象。

身份必须同时包含交易场所和真实合约代码，不能用连续合约、产品简称或推测出的交割月份替代。
日行情保留收盘价、结算价、成交量和持仓量各自的来源语义，防止不同口径被错误合并。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class DerivativeContractIdentifier:
    """表示由交易场所和真实合约代码共同限定的可交易衍生品合约。"""

    venue: str
    contract_code: str

    def __post_init__(self) -> None:
        """拒绝空白、非大写或包含连续序列语义的模糊合约身份。"""
        if (
            not self.venue
            or self.venue != self.venue.strip().upper()
            or not self.venue.isascii()
            or not self.venue.replace("_", "").isalnum()
        ):
            raise ValueError("derivative venue must be an uppercase identifier")
        if (
            not self.contract_code
            or self.contract_code != self.contract_code.strip().upper()
            or len(self.contract_code) > 64
            or not self.contract_code.isascii()
            or not self.contract_code.replace("-", "").replace("_", "").isalnum()
        ):
            raise ValueError("derivative contract code must be an uppercase identifier")

    @property
    def qualified_key(self) -> str:
        """生成 adapter 请求、分区和审计日志共用的稳定合约键。"""
        return f"{self.venue}.{self.contract_code}"

    @classmethod
    def parse(cls, value: str) -> DerivativeContractIdentifier:
        """解析 `VENUE.CONTRACT_CODE`，不从字符串推断产品、月份或期权结构。"""
        venue, separator, contract_code = value.strip().upper().partition(".")
        if separator != ".":
            raise ValueError("derivative contract must use VENUE.CONTRACT_CODE format")
        return cls(venue=venue, contract_code=contract_code)


@dataclass(frozen=True, slots=True)
class DerivativeDailyBar:
    """表示一个真实合约交易日的 reported OHLC、结算、成交与持仓事实。"""

    trade_date: date
    open_price: Decimal
    high_price: Decimal
    low_price: Decimal
    close_price: Decimal
    pre_close_price: Decimal | None
    settlement_price: Decimal | None
    pre_settlement_price: Decimal | None
    volume_value: Decimal
    open_interest_value: Decimal
    turnover_value: Decimal | None
    turnover_currency: str | None
    turnover_unit: str | None
    trade_status: str | None

    def __post_init__(self) -> None:
        """校验价格、成交和持仓不变量，保持结算价与收盘价的独立语义。"""
        prices = (
            self.open_price,
            self.high_price,
            self.low_price,
            self.close_price,
            self.pre_close_price,
            self.settlement_price,
            self.pre_settlement_price,
        )
        if any(price is not None and (not price.is_finite() or price < 0) for price in prices):
            raise ValueError("derivative prices must be finite and non-negative")
        if self.low_price > min(self.open_price, self.close_price):
            raise ValueError("derivative low price exceeds open or close")
        if self.high_price < max(self.open_price, self.close_price):
            raise ValueError("derivative high price is below open or close")
        if (
            not self.volume_value.is_finite()
            or not self.open_interest_value.is_finite()
            or self.volume_value < 0
            or self.open_interest_value < 0
        ):
            raise ValueError("derivative volume and open interest must be finite and non-negative")
        if self.turnover_value is not None and (
            not self.turnover_value.is_finite() or self.turnover_value < 0
        ):
            raise ValueError("derivative turnover must be finite and non-negative")
        if (self.turnover_currency is None) != (self.turnover_value is None):
            raise ValueError("derivative turnover currency must match turnover presence")
        if self.turnover_currency is not None and (
            len(self.turnover_currency) != 3
            or self.turnover_currency != self.turnover_currency.upper()
            or not self.turnover_currency.isascii()
        ):
            raise ValueError("derivative turnover currency must be an ISO uppercase code")
        if self.turnover_unit is not None and not self.turnover_unit.strip():
            raise ValueError("derivative turnover unit must not be blank when provided")
        if self.trade_status is not None and not self.trade_status.strip():
            raise ValueError("derivative trade status must not be blank when provided")
