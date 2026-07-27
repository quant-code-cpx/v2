"""板块身份、独立周期与历史行情值对象。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum


class SectorScheme(StrEnum):
    """区分不可混用的东财行业与概念板块分类体系。"""

    EASTMONEY_INDUSTRY = "eastmoney.industry"
    EASTMONEY_CONCEPT = "eastmoney.concept"

    @property
    def catalog_capability(self) -> str:
        """返回该分类体系共用的板块目录原始能力名称。"""
        return "sector.catalog.raw"


class SectorPeriod(StrEnum):
    """声明由上游直接提供、禁止相互推导的板块 K 线周期。"""

    DAY_1 = "1d"
    WEEK_1 = "1w"
    MONTH_1 = "1mo"

    @property
    def capability(self) -> str:
        """返回该周期对应的 provider-neutral 原始能力名称。"""
        return {
            SectorPeriod.DAY_1: "sector.bar.1d.raw",
            SectorPeriod.WEEK_1: "sector.bar.1w.raw",
            SectorPeriod.MONTH_1: "sector.bar.1mo.raw",
        }[self]


class SectorMembershipResolution(StrEnum):
    """描述一条来源成分在冻结身份知识视图下的解析结果。"""

    VERIFIED = "VERIFIED"
    PENDING = "PENDING"
    QUARANTINED = "QUARANTINED"


@dataclass(frozen=True, slots=True)
class SectorIdentifier:
    """表示一个分类体系内稳定且不可与其他体系混淆的板块代码。"""

    scheme: SectorScheme
    code: str

    def __post_init__(self) -> None:
        """拒绝空白、超长或包含控制字符的板块代码。"""
        if not self.code or self.code != self.code.strip() or len(self.code) > 64:
            raise ValueError("sector code must be a trimmed string no longer than 64 characters")
        if any(character.isspace() and character != " " for character in self.code):
            raise ValueError("sector code must not contain control whitespace")

    @property
    def qualified_key(self) -> str:
        """生成供发布分区、日志和内部契约使用的稳定复合身份。"""
        return f"{self.scheme.value}:{self.code}"


@dataclass(frozen=True, slots=True)
class SectorCatalogEntry:
    """表示供应商目录中已确认名称的稳定板块身份。"""

    identifier: SectorIdentifier
    name: str

    def __post_init__(self) -> None:
        """拒绝空白或过长名称，避免未确认目录进入可公开状态。"""
        if self.name != self.name.strip() or not self.name or len(self.name) > 200:
            raise ValueError("sector name must be a trimmed string from 1 to 200 characters")


@dataclass(frozen=True, slots=True)
class SectorMembershipCandidate:
    """表示来源当前快照中的一条板块成分，不声明真实调入或调出时间。"""

    source_symbol: str
    source_name: str

    def __post_init__(self) -> None:
        """限制中立来源标识格式，避免脏行绕过快照质量门。"""
        if len(self.source_symbol) != 6 or not self.source_symbol.isdigit():
            raise ValueError("membership source symbol must be six digits")
        if self.source_name != self.source_name.strip() or not self.source_name:
            raise ValueError("membership source name must not be blank")
        if len(self.source_name) > 200:
            raise ValueError("membership source name must not exceed 200 characters")


@dataclass(frozen=True, slots=True)
class SectorBar:
    """保存一个上游直接给出的板块日、周或月历史行情观测。"""

    period_end: date
    open_price: Decimal
    high_price: Decimal
    low_price: Decimal
    close_price: Decimal
    volume_value: Decimal
    volume_unit: str
    amount_cny: Decimal
    amplitude_percent: Decimal | None
    change_percent: Decimal | None
    change_amount: Decimal | None
    turnover_percent: Decimal | None

    def __post_init__(self) -> None:
        """在写入前校验 OHLC、非负成交字段和来源原生单位约束。"""
        _require_non_negative(self.open_price, "open_price")
        _require_non_negative(self.high_price, "high_price")
        _require_non_negative(self.low_price, "low_price")
        _require_non_negative(self.close_price, "close_price")
        _require_non_negative(self.volume_value, "volume_value")
        _require_non_negative(self.amount_cny, "amount_cny")
        if self.low_price > min(self.open_price, self.close_price):
            raise ValueError("low price exceeds open or close")
        if self.high_price < max(self.open_price, self.close_price):
            raise ValueError("high price is below open or close")
        # 东财板块历史接口未承诺跨品类统一成交量单位；P0 原样保留，禁止相加或换算。
        if self.volume_unit != "provider_native":
            raise ValueError("sector volume unit must be provider_native")
        _require_optional_non_negative(self.amplitude_percent, "amplitude_percent")
        _require_optional_non_negative(self.turnover_percent, "turnover_percent")
        _require_optional_finite(self.change_percent, "change_percent")
        _require_optional_finite(self.change_amount, "change_amount")


def _require_non_negative(value: Decimal, field_name: str) -> None:
    """拒绝 NaN、无穷和负数，防止数值异常进入可发布数据集。"""
    if not value.is_finite() or value < 0:
        raise ValueError(f"{field_name} must be finite and non-negative")


def _require_optional_non_negative(value: Decimal | None, field_name: str) -> None:
    """校验可选的百分比字段在存在时具有可比较的非负数值。"""
    if value is not None:
        _require_non_negative(value, field_name)


def _require_optional_finite(value: Decimal | None, field_name: str) -> None:
    """校验可正可负的变动字段在存在时不是 NaN 或无穷。"""
    if value is not None and not value.is_finite():
        raise ValueError(f"{field_name} must be finite")
