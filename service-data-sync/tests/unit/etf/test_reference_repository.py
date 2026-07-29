"""ETF 资料与状态 canonical 发布仓储的语义边界测试。"""

from __future__ import annotations

from datetime import date

from service_data_sync.domain.etf import EtfDailyStatus, EtfIdentifier, EtfProfile
from service_data_sync.infrastructure.persistence.etf_reference_repository import (
    _profile_hash,
    _status_hash,
)


def test_profile_hash_changes_for_listing_status_without_catalog_inference() -> None:
    """明确来源状态变更才产生资料 revision；目录中缺席并不会调用此路径。"""
    listed = _profile("LISTED")
    suspended = _profile("SUSPENDED")

    assert _profile_hash(listed) != _profile_hash(suspended)


def test_status_hash_keeps_subscription_separate_from_trading() -> None:
    """相同日期和状态码在不同维度必须生成不同事实，不能把停牌解释为申购暂停。"""
    etf = EtfIdentifier("SSE", "510300")
    trading = EtfDailyStatus(etf, "TRADING", "SUSPENDED", date(2026, 7, 28), None, None)
    subscription = EtfDailyStatus(etf, "SUBSCRIPTION", "SUSPENDED", date(2026, 7, 28), None, None)

    assert _status_hash(trading) != _status_hash(subscription)


def _profile(status: str) -> EtfProfile:
    """构造一条来源明确的 ETF 资料，用于验证显式状态而非目录差集驱动版本。"""
    return EtfProfile(
        etf=EtfIdentifier("SSE", "510300"),
        etf_type="STOCK",
        management_mode="PASSIVE",
        manager_name=None,
        custodian_name=None,
        established_on=None,
        listed_on=date(2012, 1, 1),
        delisted_on=None,
        quote_currency="CNY",
        nav_currency="CNY",
        listing_status=status,
        effective_from=date(2026, 7, 28),
        source_time_precision="DATE_ONLY",
    )
