"""公司公告与业绩事件 `P0` 的领域值。

本模块把官方公告、业绩预告和业绩快报建模为可追溯事实：结构化指标必须回指同批公告证据。
公开时间区分精确时间、仅有日期和仅观察到三种情况，避免把未知发布时间误当成可用于历史决策的时点。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

_GUIDANCE_METRICS = {"NET_PROFIT", "DEDUCTED_NET_PROFIT"}
_EXPRESS_METRICS = {
    "REVENUE",
    "OPERATING_PROFIT",
    "TOTAL_PROFIT",
    "NET_PROFIT",
    "DEDUCTED_NET_PROFIT",
    "TOTAL_ASSETS",
    "NET_ASSETS",
    "EPS",
    "BOOK_VALUE_PER_SHARE",
    "ROE",
}


@dataclass(frozen=True, slots=True)
class DisclosureDocument:
    """表示一份官方公告目录记录；来源文档 ID 是证据身份而非业务事件身份。"""

    source_document_id: str
    source_security_code: str
    title: str
    category: str
    official_url: str
    announced_on: date
    source_visible_at: datetime | None
    visible_time_precision: str
    public_usable_at: datetime
    content_sha256: str

    def __post_init__(self) -> None:
        """校验文档身份和时间证据，避免抓取时间或标题 hash 替代官方公告身份。"""
        if not all(
            (
                self.source_document_id.strip(),
                self.source_security_code.strip(),
                self.title.strip(),
                self.category.strip(),
            )
        ):
            raise ValueError("disclosure document identity fields must not be blank")
        if not self.official_url.startswith(("https://", "http://")):
            raise ValueError("disclosure document requires an official HTTP URL")
        if len(self.content_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in self.content_sha256.lower()
        ):
            raise ValueError("disclosure document content hash must be SHA-256")
        _validate_visibility(
            source_visible_at=self.source_visible_at,
            precision=self.visible_time_precision,
            public_usable_at=self.public_usable_at,
        )


@dataclass(frozen=True, slots=True)
class EarningsGuidanceMetric:
    """表示业绩预告中的一个预测指标；区间和单值保持来源原貌，不伪造另一端。"""

    source_document_id: str
    source_security_code: str
    report_period: date
    guidance_type: str
    metric_code: str
    amount_low: Decimal | None
    amount_high: Decimal | None
    yoy_low: Decimal | None
    yoy_high: Decimal | None
    prior_period_value: Decimal | None
    currency: str

    def __post_init__(self) -> None:
        """限制首期指标、校验区间方向，并保留金额和同比的空值语义。"""
        if not self.source_document_id.strip() or not self.source_security_code.strip():
            raise ValueError("guidance requires source document and security identities")
        if self.metric_code not in _GUIDANCE_METRICS:
            raise ValueError("guidance metric is outside P0 whitelist")
        if not self.guidance_type.strip():
            raise ValueError("guidance type must not be blank")
        _validate_optional_numbers(
            self.amount_low,
            self.amount_high,
            self.yoy_low,
            self.yoy_high,
            self.prior_period_value,
        )
        if (
            self.amount_low is not None
            and self.amount_high is not None
            and self.amount_low > self.amount_high
        ):
            raise ValueError("guidance amount low must not exceed high")
        if self.yoy_low is not None and self.yoy_high is not None and self.yoy_low > self.yoy_high:
            raise ValueError("guidance YoY low must not exceed high")
        if all(
            value is None
            for value in (
                self.amount_low,
                self.amount_high,
                self.yoy_low,
                self.yoy_high,
                self.prior_period_value,
            )
        ):
            raise ValueError("guidance metric requires at least one reported value")
        _validate_currency(self.currency)


@dataclass(frozen=True, slots=True)
class EarningsExpressMetric:
    """表示业绩快报的一项初步财务指标，永远不等同于正式财务报表。"""

    source_document_id: str
    source_security_code: str
    report_period: date
    metric_code: str
    current_value: Decimal
    prior_value: Decimal | None
    unit: str
    currency: str | None
    preliminary_status: str

    def __post_init__(self) -> None:
        """校验首期快报白名单和单位，防止不同量纲或正式值混入同一事实集。"""
        if not self.source_document_id.strip() or not self.source_security_code.strip():
            raise ValueError("express requires source document and security identities")
        if self.metric_code not in _EXPRESS_METRICS:
            raise ValueError("express metric is outside P0 whitelist")
        if self.unit not in {"CNY", "CNY_PER_SHARE", "FRACTION"}:
            raise ValueError("express metric has an unsupported unit")
        if self.unit == "FRACTION" and self.currency is not None:
            raise ValueError("fraction express metric must not carry currency")
        if self.unit != "FRACTION" and self.currency is None:
            raise ValueError("monetary express metric requires currency")
        if self.currency is not None:
            _validate_currency(self.currency)
        _validate_optional_numbers(self.current_value, self.prior_value)
        if self.preliminary_status not in {"PRELIMINARY", "UNAUDITED"}:
            raise ValueError("express P0 values must remain preliminary")


def _validate_visibility(
    *, source_visible_at: datetime | None, precision: str, public_usable_at: datetime
) -> None:
    """校验精确和日期级公开时间，日期级输入不允许伪装为当天零点精确发布。"""
    if public_usable_at.tzinfo is None:
        raise ValueError("public usable time must include timezone")
    if precision == "EXACT":
        if source_visible_at is None or source_visible_at.tzinfo is None:
            raise ValueError("exact visibility requires timezone-aware source time")
        if public_usable_at < source_visible_at:
            raise ValueError("public usable time must not precede source visibility")
        return
    if precision == "DATE_ONLY":
        if source_visible_at is not None:
            raise ValueError("date-only visibility must not invent an exact source time")
        if public_usable_at.hour == 0 and public_usable_at.minute == 0:
            raise ValueError("date-only visibility requires a conservative usable session time")
        return
    if precision == "OBSERVED_ONLY" and source_visible_at is None:
        return
    raise ValueError("unsupported disclosure visibility precision")


def _validate_optional_numbers(*values: Decimal | None) -> None:
    """拒绝 NaN 和无穷大，允许负利润、负同比及来源没有披露的空值。"""
    if any(value is not None and not value.is_finite() for value in values):
        raise ValueError("corporate metric values must be finite")


def _validate_currency(value: str) -> None:
    """校验 ISO 大写币种，避免 Provider 本地标签泄漏到 canonical 语义。"""
    if len(value) != 3 or value != value.upper() or not value.isascii():
        raise ValueError("currency must be an ISO uppercase code")
