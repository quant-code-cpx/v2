"""融资融券 `P0` 的场所汇总、证券明细和资格领域值。

交易所日汇总、单证券明细和资格名单是三类独立披露，不应拿其中一类补齐另一类的空字段。
数值空缺表示来源未披露而非零值；派生偿还额也不能混入 `P0` 的直报事实集。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class MarginVenue:
    """表示两融场所身份；北交所当前只允许进入资格名单 capability。

    `BSE` 的当日融资融券标的清单已有来源实证，但没有被本服务映射为场所汇总或
    证券日明细。场所值对象只表达身份合法性，具体 capability 的准入仍由应用服务和
    provider adapter 分别关闭，避免把资格清单误当成余额或成交明细。
    """

    code: str

    def __post_init__(self) -> None:
        """拒绝未治理场所，不让不同交易制度静默进入同一两融领域。"""
        if self.code not in {"SSE", "SZSE", "BSE"}:
            raise ValueError("margin venue must be SSE, SZSE, or BSE")


@dataclass(frozen=True, slots=True)
class MarginMarketDaily:
    """表示交易所直报的两融市场日汇总；缺字段保持空值而不由证券明细反推。"""

    trade_date: date
    financing_balance: Decimal | None
    financing_buy_amount: Decimal | None
    financing_repayment_amount: Decimal | None
    lending_balance_amount: Decimal | None
    lending_balance_qty: Decimal | None
    lending_sell_qty: Decimal | None
    lending_repayment_qty: Decimal | None
    total_balance: Decimal | None
    currency: str
    quantity_unit: str | None

    def __post_init__(self) -> None:
        """校验所有金额和数量非负，零值是事实而空值仅表示来源未披露。"""
        numeric_values = (
            self.financing_balance,
            self.financing_buy_amount,
            self.financing_repayment_amount,
            self.lending_balance_amount,
            self.lending_balance_qty,
            self.lending_sell_qty,
            self.lending_repayment_qty,
            self.total_balance,
        )
        if not any(value is not None for value in numeric_values):
            raise ValueError("margin market daily requires at least one reported value")
        if any(
            value is not None and (not value.is_finite() or value < 0) for value in numeric_values
        ):
            raise ValueError("margin market daily values must be finite and non-negative")
        if (
            len(self.currency) != 3
            or self.currency != self.currency.upper()
            or not self.currency.isascii()
        ):
            raise ValueError("margin market currency must be an ISO uppercase code")
        if self.quantity_unit is not None and not self.quantity_unit.strip():
            raise ValueError("margin quantity unit must not be blank")


@dataclass(frozen=True, slots=True)
class MarginSecurityDaily:
    """表示单一证券的两融日明细；直报偿还和派生偿还在 P0 中不能同时出现。"""

    source_security_code: str
    trade_date: date
    financing_balance: Decimal | None
    financing_buy_amount: Decimal | None
    financing_repayment_reported: Decimal | None
    financing_repayment_derived: Decimal | None
    lending_balance_qty: Decimal | None
    quantity_unit: str | None
    currency: str
    null_reason: str | None

    def __post_init__(self) -> None:
        """校验证券来源身份、直报边界和非负金额；空值不得改写为零或估算值。"""
        if not self.source_security_code.strip():
            raise ValueError("margin security source code must not be blank")
        values = (
            self.financing_balance,
            self.financing_buy_amount,
            self.financing_repayment_reported,
            self.financing_repayment_derived,
            self.lending_balance_qty,
        )
        if not any(value is not None for value in values):
            raise ValueError("margin security daily requires at least one disclosed value")
        if any(value is not None and (not value.is_finite() or value < 0) for value in values):
            raise ValueError("margin security values must be finite and non-negative")
        if (
            self.financing_repayment_reported is not None
            and self.financing_repayment_derived is not None
        ):
            raise ValueError("reported and derived margin repayment must not coexist")
        if self.financing_repayment_derived is not None:
            raise ValueError("derived margin repayment is outside P0 reported dataset")
        if self.lending_balance_qty is not None and self.quantity_unit is None:
            raise ValueError("margin security lending quantity requires a unit")
        if self.quantity_unit is not None and not self.quantity_unit.strip():
            raise ValueError("margin security quantity unit must not be blank")
        if (
            len(self.currency) != 3
            or self.currency != self.currency.upper()
            or not self.currency.isascii()
        ):
            raise ValueError("margin security currency must be an ISO uppercase code")
        if self.null_reason is not None and not self.null_reason.strip():
            raise ValueError("margin security null reason must not be blank")


@dataclass(frozen=True, slots=True)
class MarginEligibility:
    """表示一段来源明确的两融资格；当前名单只能从实际观测时点建立知识版本。"""

    source_security_code: str
    status: str
    effective_from: date
    effective_to: date | None
    announcement_on: date | None
    evidence_basis: str

    def __post_init__(self) -> None:
        """校验资格范围与证据方式，禁止把当前目录差集自动解释为历史调出。"""
        if not self.source_security_code.strip():
            raise ValueError("margin eligibility source code must not be blank")
        if self.status not in {"ELIGIBLE", "FINANCING_ONLY", "LENDING_ONLY", "INELIGIBLE"}:
            raise ValueError("margin eligibility status is invalid")
        if self.effective_to is not None and self.effective_to <= self.effective_from:
            raise ValueError("margin eligibility effective range is invalid")
        if self.evidence_basis not in {"OFFICIAL_ANNOUNCEMENT", "OBSERVED_LIST"}:
            raise ValueError("margin eligibility evidence basis is invalid")
        if self.evidence_basis == "OFFICIAL_ANNOUNCEMENT" and self.announcement_on is None:
            raise ValueError("official margin eligibility requires announcement date")
