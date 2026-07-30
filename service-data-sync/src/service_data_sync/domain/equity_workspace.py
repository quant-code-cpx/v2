"""股票中心新增的普通交易状态、历史股本与申万归属领域值。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from service_data_sync.domain.equity import EquityIdentifier


@dataclass(frozen=True, slots=True)
class EquityTradingStatus:
    """表示来源明确披露的一只证券某日普通停复牌状态。"""

    identifier: EquityIdentifier
    trade_date: date
    status: str
    reason: str | None

    def __post_init__(self) -> None:
        """限制普通交易状态枚举，禁止与暂停上市生命周期混写。"""
        if self.status not in {"SUSPENDED", "RESUMED"}:
            raise ValueError("reported trading status must be SUSPENDED or RESUMED")


@dataclass(frozen=True, slots=True)
class EquityShareCapital:
    """表示来源报告的一个生效日股本结构，所有数量单位为股。"""

    identifier: EquityIdentifier
    effective_on: date
    total_shares: Decimal
    listed_tradable_a_shares: Decimal | None
    restricted_shares: Decimal | None
    change_reason: str | None

    def __post_init__(self) -> None:
        """校验正总股本和不超过总股本的可选组成项。"""
        if not self.total_shares.is_finite() or self.total_shares <= 0:
            raise ValueError("total shares must be finite and positive")
        for value in (self.listed_tradable_a_shares, self.restricted_shares):
            if value is not None and (
                not value.is_finite() or value < 0 or value > self.total_shares
            ):
                raise ValueError("share capital component is outside total shares")


@dataclass(frozen=True, slots=True)
class SwEquityMembership:
    """表示申万三级节点当前快照中的一只已命名证券。"""

    node_code: str
    symbol: str
    name: str
    observed_on: date
    source_included_on: date | None
    level1_name: str | None
    level2_name: str | None
    level3_name: str | None

    def __post_init__(self) -> None:
        """拒绝非六位节点或证券代码以及缺失展示名称。"""
        if len(self.node_code) != 6 or not self.node_code.isdecimal():
            raise ValueError("SW third-level node code must contain six digits")
        if len(self.symbol) != 6 or not self.symbol.isdecimal():
            raise ValueError("SW membership symbol must contain six digits")
        if not self.name.strip():
            raise ValueError("SW membership name must not be blank")
