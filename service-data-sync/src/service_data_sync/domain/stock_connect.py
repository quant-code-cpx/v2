"""沪深港通 `P0` 的通道统计与活跃证券领域值。

通道、资金方向和市场统计分别建模，防止一个字符串混合多重业务含义。
不同披露制度导致的字段缺失必须保留为空并携带可用性状态，不能用其他榜单、持仓或估算结果补齐。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
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
    trade_count: int | None = None
    etf_turnover_amount: Decimal | None = None
    field_availability: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        """校验金额边界和披露状态，净买允许负值而成交、买卖和额度不能为负。"""
        non_negative = (
            self.buy_amount,
            self.sell_amount,
            self.turnover_amount,
            self.quota_balance,
            self.etf_turnover_amount,
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
                self.trade_count,
                self.etf_turnover_amount,
            )
        ):
            raise ValueError("unavailable stock-connect disclosure must not contain amounts")
        if self.trade_count is not None and self.trade_count < 0:
            raise ValueError("stock-connect trade count must be non-negative")
        availability = dict(self.field_availability)
        if len(availability) != len(self.field_availability):
            raise ValueError("stock-connect field availability contains duplicate fields")
        if not set(availability.values()) <= {
            "REPORTED",
            "NOT_DISCLOSED_BY_REGIME",
            "SOURCE_MISSING",
            "NOT_APPLICABLE",
        }:
            raise ValueError("stock-connect field availability is invalid")


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
    source_instrument_name: str | None = None
    field_availability: tuple[tuple[str, str], ...] = ()

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
        if self.source_instrument_name is not None and not self.source_instrument_name.strip():
            raise ValueError("stock-connect active security source name must not be blank")
        availability = dict(self.field_availability)
        if len(availability) != len(self.field_availability):
            raise ValueError("stock-connect active availability contains duplicate fields")
        if not set(availability.values()) <= {
            "REPORTED",
            "NOT_DISCLOSED_BY_REGIME",
            "SOURCE_MISSING",
            "NOT_APPLICABLE",
        }:
            raise ValueError("stock-connect active availability is invalid")


@dataclass(frozen=True, slots=True)
class StockConnectCalendarDay:
    """表示 HKEX 官方互联互通日历的一天，不用工作日规则推测开闭市。"""

    calendar_date: date
    northbound_trading: bool
    southbound_trading: bool
    hong_kong_state: str
    mainland_state: str


@dataclass(frozen=True, slots=True)
class StockConnectInstrumentMaster:
    """表示 HKEX Securities Master 中可用于解析港股通事实的最小证券身份。"""

    source_security_id: str | None
    source_instrument_code: str
    display_name: str
    effective_from: date

    def __post_init__(self) -> None:
        """校验官方稳定 ID、代码和名称；稳定 ID 缺失时保留可降级记录。"""
        if self.source_security_id is not None and (
            self.source_security_id != self.source_security_id.strip()
            or not 1 <= len(self.source_security_id) <= 64
        ):
            raise ValueError("HKEX source security id is invalid")
        if (
            not self.source_instrument_code.isdigit()
            or not 1 <= len(self.source_instrument_code) <= 6
        ):
            raise ValueError("HKEX instrument code is invalid")
        if not self.display_name.strip():
            raise ValueError("HKEX instrument name must not be blank")


@dataclass(frozen=True, slots=True)
class StockConnectChannelStatus:
    """表示一个通道方向的官方日终状态与 CNY 额度事实。"""

    trade_date: date
    channel: str
    direction: str
    trading_day: bool
    session_state: str
    session_availability: str
    buy_order_accepted: bool | None
    sell_order_accepted: bool | None
    quota_state: str
    quota_balance: Decimal | None
    quota_currency: str
    observed_at: datetime
    source_code: str
    product_name: str
    source_publication_at: datetime | None
    source_file_sha256: str | None

    def __post_init__(self) -> None:
        """校验日终状态、额度和来源时间，禁止把“额度充足”的空余额写成零。"""
        StockConnectChannel(channel=self.channel, direction=self.direction)
        if self.session_state not in {"OPEN", "CLOSED", "HALTED", "NOT_OPEN", "UNKNOWN"}:
            raise ValueError("stock-connect session state is invalid")
        if self.session_availability not in {"DERIVED", "REPORTED", "SOURCE_MISSING"}:
            raise ValueError("stock-connect session availability is invalid")
        if self.session_availability == "SOURCE_MISSING" and self.session_state != "UNKNOWN":
            raise ValueError("missing stock-connect session evidence requires UNKNOWN")
        if self.quota_state not in {
            "SUFFICIENT",
            "ACTUAL_REPORTED",
            "EXHAUSTED",
            "NOT_APPLICABLE",
            "SOURCE_MISSING",
        }:
            raise ValueError("stock-connect quota state is invalid")
        if self.quota_balance is not None and (
            not self.quota_balance.is_finite() or self.quota_balance < 0
        ):
            raise ValueError("stock-connect quota balance must be finite and non-negative")
        if self.quota_currency != "CNY":
            raise ValueError("stock-connect quota currency must be CNY")
        if self.quota_state == "ACTUAL_REPORTED" and self.quota_balance is None:
            raise ValueError("actual stock-connect quota requires a balance")
        if self.quota_state == "EXHAUSTED" and self.quota_balance != Decimal("0"):
            raise ValueError("exhausted stock-connect quota must be zero")
        if self.quota_state in {"SUFFICIENT", "NOT_APPLICABLE", "SOURCE_MISSING"} and (
            self.quota_balance is not None
        ):
            raise ValueError("non-numeric stock-connect quota state must not contain a balance")
        if self.observed_at.tzinfo is None or (
            self.source_publication_at is not None and self.source_publication_at.tzinfo is None
        ):
            raise ValueError("stock-connect status timestamps must include timezone")
        if self.source_file_sha256 is not None and self.source_publication_at is None:
            raise ValueError("stock-connect status source digest requires publication time")
        if self.source_file_sha256 is not None and len(self.source_file_sha256) != 64:
            raise ValueError("stock-connect status source digest is invalid")
