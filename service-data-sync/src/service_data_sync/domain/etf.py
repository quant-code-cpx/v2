"""`ETF` `P0` 的来源无关领域值：日行情、净值、产品资料和日级状态。

不同字段使用不同来源口径：未复权成交行情、单位/累计净值、交易状态和申赎状态不能彼此推导。
目录快照只说明已观察到的信息，缺失条目不能被解释为基金摘牌或状态变化。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class EtfIdentifier:
    """表示由沪深交易场所和六位代码限定的 ETF 上市工具，不能以基金名称或当前目录替代。"""

    venue: str
    symbol: str

    def __post_init__(self) -> None:
        """只接受 P0 明确覆盖的沪深场所与稳定六位交易代码。"""
        if self.venue not in {"SSE", "SZSE"}:
            raise ValueError("ETF venue must be SSE or SZSE")
        if len(self.symbol) != 6 or not self.symbol.isdecimal():
            raise ValueError("ETF symbol must be a six-digit code")

    @property
    def qualified_key(self) -> str:
        """生成 adapter、分区与审计记录共用的场所限定标识。"""
        return f"{self.venue}.{self.symbol}"

    @classmethod
    def parse(cls, value: str) -> EtfIdentifier:
        """解析 `SSE.510300` 形态；缺场所时拒绝避免沪深代码误绑。"""
        venue, separator, symbol = value.strip().upper().partition(".")
        if separator != ".":
            raise ValueError("ETF identifier must use VENUE.SYMBOL format")
        return cls(venue=venue, symbol=symbol)


@dataclass(frozen=True, slots=True)
class EtfDailyBar:
    """表示 ETF 未复权日 OHLCV 与成交额，成交量单位和状态均保留来源语义。"""

    trade_date: date
    open_price: Decimal
    high_price: Decimal
    low_price: Decimal
    close_price: Decimal
    volume_value: Decimal
    volume_unit: str
    amount_value: Decimal
    currency: str
    trade_status: str | None

    def __post_init__(self) -> None:
        """校验价格、成交字段和币种，不从零成交或缺行推断停牌。"""
        prices = (self.open_price, self.high_price, self.low_price, self.close_price)
        if any(not value.is_finite() or value < 0 for value in prices):
            raise ValueError("ETF prices must be finite and non-negative")
        if self.low_price > min(self.open_price, self.close_price):
            raise ValueError("ETF low price exceeds open or close")
        if self.high_price < max(self.open_price, self.close_price):
            raise ValueError("ETF high price is below open or close")
        if not self.volume_value.is_finite() or self.volume_value < 0:
            raise ValueError("ETF volume must be finite and non-negative")
        if not self.amount_value.is_finite() or self.amount_value < 0:
            raise ValueError("ETF amount must be finite and non-negative")
        if not self.volume_unit.strip():
            raise ValueError("ETF volume unit is required")
        if (
            len(self.currency) != 3
            or self.currency != self.currency.upper()
            or not self.currency.isascii()
        ):
            raise ValueError("ETF currency must be an ISO uppercase code")
        if self.trade_status is not None and not self.trade_status.strip():
            raise ValueError("ETF trade status must not be blank")


@dataclass(frozen=True, slots=True)
class EtfNav:
    """表示来源直报的单位或累计净值，P0 不把 IOPV 或收盘价伪装成 NAV。"""

    nav_date: date
    nav_kind: str
    nav_value: Decimal
    currency: str
    finality: str

    def __post_init__(self) -> None:
        """校验净值类型、正值、币种和终态，未知公开时间由来源观察而非数值本身表示。"""
        if self.nav_kind not in {"UNIT", "ACCUMULATED"}:
            raise ValueError("ETF P0 NAV kind must be UNIT or ACCUMULATED")
        if not self.nav_value.is_finite() or self.nav_value <= 0:
            raise ValueError("ETF NAV must be finite and positive")
        if (
            len(self.currency) != 3
            or self.currency != self.currency.upper()
            or not self.currency.isascii()
        ):
            raise ValueError("ETF NAV currency must be an ISO uppercase code")
        if self.finality not in {"FINAL", "PROVISIONAL", "UNKNOWN"}:
            raise ValueError("ETF NAV finality is invalid")


@dataclass(frozen=True, slots=True)
class EtfProfile:
    """表示 ETF 上市工具的 P0 产品资料，不以当前目录差集推断终止或状态变化。"""

    etf: EtfIdentifier
    etf_type: str
    management_mode: str
    manager_name: str | None
    custodian_name: str | None
    established_on: date | None
    listed_on: date | None
    delisted_on: date | None
    quote_currency: str
    nav_currency: str
    listing_status: str
    effective_from: date
    source_time_precision: str

    def __post_init__(self) -> None:
        """校验产品与生命周期字段，缺失目录项绝不能自动写为摘牌或暂停。"""
        if not self.etf_type.strip() or not self.management_mode.strip():
            raise ValueError("ETF profile type and management mode must not be blank")
        if (
            self.delisted_on is not None
            and self.listed_on is not None
            and self.delisted_on < self.listed_on
        ):
            raise ValueError("ETF delisting date must not precede listing date")
        if self.listing_status not in {"LISTED", "SUSPENDED", "DELISTED", "UNKNOWN"}:
            raise ValueError("ETF listing status is invalid")
        if self.source_time_precision not in {"EXACT", "DATE_ONLY", "UNKNOWN"}:
            raise ValueError("ETF profile source time precision is invalid")
        _validate_currency(self.quote_currency)
        _validate_currency(self.nav_currency)


@dataclass(frozen=True, slots=True)
class EtfDailyStatus:
    """表示 ETF 日级交易、申购或赎回状态，三个维度不可由彼此推断。"""

    etf: EtfIdentifier
    status_dimension: str
    status_code: str
    effective_from: date
    effective_to: date | None
    reason: str | None

    def __post_init__(self) -> None:
        """校验状态范围，停牌、申购暂停和赎回暂停必须保持独立事实。"""
        if self.status_dimension not in {"TRADING", "SUBSCRIPTION", "REDEMPTION"}:
            raise ValueError("ETF status dimension is invalid")
        if not self.status_code.strip():
            raise ValueError("ETF status code must not be blank")
        if self.effective_to is not None and self.effective_to <= self.effective_from:
            raise ValueError("ETF status effective range is invalid")
        if self.reason is not None and not self.reason.strip():
            raise ValueError("ETF status reason must not be blank")


def _validate_currency(value: str) -> None:
    """校验 ETF 报价或净值币种为 ISO 大写代码，避免来源本地标签进入 canonical 字段。"""
    if len(value) != 3 or value != value.upper() or not value.isascii():
        raise ValueError("ETF currency must be an ISO uppercase code")
