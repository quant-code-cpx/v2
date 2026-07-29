"""`A` 股标准证券身份、原生行情、复权与公司事件值对象。

证券代码必须由交易所限定；日、周、月行情是上游各自提供的未复权事实，周/月线绝不能由日线推算。
复权因子、公司行动和公司概况分别保存，供读取端按明确业务需要组合，而不在领域层隐式改写价格。
"""

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


class EquityBarPeriod(StrEnum):
    """封闭个股行情可直接从上游获取的三个物理周期。"""

    DAY_1 = "1d"
    WEEK_1 = "1w"
    MONTH_1 = "1mo"

    @property
    def capability(self) -> str:
        """返回该周期独立的未复权同步能力名。"""
        return f"equity.bar.{self.value}.raw"


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


@dataclass(frozen=True, slots=True)
class EquityPeriodBar:
    """保存上游直接返回的一条未复权周线或月线。"""

    period: EquityBarPeriod
    period_end: date
    open_price: Decimal
    high_price: Decimal
    low_price: Decimal
    close_price: Decimal
    volume_shares: int
    amount_cny: Decimal
    turnover_rate: Decimal | None

    def __post_init__(self) -> None:
        """拒绝日线冒充周期线，并复用行情价格和量纲不变量。"""
        if self.period is EquityBarPeriod.DAY_1:
            raise ValueError("period bar must be weekly or monthly")
        EquityDailyBar(
            trade_date=self.period_end,
            open_price=self.open_price,
            high_price=self.high_price,
            low_price=self.low_price,
            close_price=self.close_price,
            volume_shares=self.volume_shares,
            amount_cny=self.amount_cny,
            turnover_rate=self.turnover_rate,
        )


@dataclass(frozen=True, slots=True)
class EquityAdjustmentFactor:
    """保存一个生效日的稀疏累计后复权因子。"""

    effective_date: date
    cumulative_factor: Decimal

    def __post_init__(self) -> None:
        """复权因子必须为有限正数，避免查询产生无效价格。"""
        if not self.cumulative_factor.is_finite() or self.cumulative_factor <= 0:
            raise ValueError("cumulative_factor must be finite and positive")


@dataclass(frozen=True, slots=True)
class EquityCorporateAction:
    """保存分红、送股和转增方案的标准事件版本。"""

    source_event_key: str
    report_period: date
    status: str
    announcement_date: date | None
    record_date: date | None
    ex_date: date | None
    cash_dividend_per_10: Decimal | None
    bonus_shares_per_10: Decimal | None
    transfer_shares_per_10: Decimal | None

    def __post_init__(self) -> None:
        """校验事件身份、状态和每十股数值，禁止负分配进入 canonical。"""
        if not self.source_event_key.strip() or not self.status.strip():
            raise ValueError("corporate action identity and status must not be blank")
        values = (
            self.cash_dividend_per_10,
            self.bonus_shares_per_10,
            self.transfer_shares_per_10,
        )
        if any(value is not None and (not value.is_finite() or value < 0) for value in values):
            raise ValueError("corporate action values must be finite and non-negative")


@dataclass(frozen=True, slots=True)
class EquityCompanyProfile:
    """保存巨潮公司概况中允许进入标准层的字段。"""

    company_name: str
    english_name: str | None
    industry: str | None
    legal_representative: str | None
    established_on: date | None
    website: str | None
    email: str | None
    phone: str | None
    registered_address: str | None
    office_address: str | None
    main_business: str | None
    business_scope: str | None
    summary: str | None

    def __post_init__(self) -> None:
        """要求公司名称非空，其余字段保留来源真实空值。"""
        if not self.company_name.strip():
            raise ValueError("company_name must not be blank")
