"""ETF canonical v2 与 API/Web 公开字符串上限的一致性测试。"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from service_data_sync.domain.etf import (
    EtfDailyBar,
    EtfDailyStatus,
    EtfIdentifier,
    EtfProfile,
)


def test_profile_public_text_accepts_exact_boundary_and_rejects_overflow() -> None:
    """名称类字段按 UTF-16 公开合同计数，边界值保留且超长值不截断。"""
    profile = _profile(
        display_name="😀" * 80,
        manager_name="管" * 160,
        custodian_name="托" * 160,
    )

    assert profile.display_name == "😀" * 80
    for field, value in (
        ("display_name", "😀" * 81),
        ("manager_name", "管" * 161),
        ("custodian_name", "托" * 161),
    ):
        with pytest.raises(ValueError, match="public contract"):
            replace(profile, **{field: value})


def test_profile_classification_rejects_values_above_eighty_units() -> None:
    """ETF 类型和管理方式超过公开八十单位上限时必须在 canonical 发布前失败。"""
    profile = _profile(etf_type="T" * 80, management_mode="M" * 80)

    assert profile.etf_type == "T" * 80
    with pytest.raises(ValueError, match="public contract"):
        replace(profile, etf_type="T" * 81)
    with pytest.raises(ValueError, match="public contract"):
        replace(profile, management_mode="M" * 81)


def test_bar_and_reported_status_reject_values_above_eighty_units() -> None:
    """行情状态和来源报告状态均拒绝超长代码，不把数据库物理余量公开成契约。"""
    bar = _bar(trade_status="T" * 80)
    status = EtfDailyStatus(
        etf=EtfIdentifier("SSE", "510300"),
        status_dimension="SUBSCRIPTION",
        status_code="S" * 80,
        effective_from=date(2026, 7, 28),
        effective_to=date(2026, 7, 29),
        reason=None,
    )

    assert bar.trade_status == "T" * 80
    assert status.status_code == "S" * 80
    with pytest.raises(ValueError, match="public contract"):
        replace(bar, trade_status="T" * 81)
    with pytest.raises(ValueError, match="public contract"):
        replace(status, status_code="S" * 81)


def test_volume_unit_and_status_reason_use_their_independent_public_limits() -> None:
    """成交量单位与状态原因按各自合同宽度校验，来源事实超限时拒绝而不是截断。"""
    bar = _bar(trade_status=None, volume_unit="U" * 40)
    status = EtfDailyStatus(
        etf=EtfIdentifier("SSE", "510300"),
        status_dimension="TRADING",
        status_code="OPEN",
        effective_from=date(2026, 7, 28),
        effective_to=None,
        reason="原" * 500,
    )

    assert bar.volume_unit == "U" * 40
    assert status.reason == "原" * 500
    with pytest.raises(ValueError, match="public contract"):
        replace(bar, volume_unit="U" * 41)
    with pytest.raises(ValueError, match="public contract"):
        replace(status, reason="原" * 501)


def _profile(
    *,
    display_name: str = "沪深300ETF",
    manager_name: str = "测试管理人",
    custodian_name: str = "测试托管人",
    etf_type: str = "STOCK",
    management_mode: str = "PASSIVE",
) -> EtfProfile:
    """构造通过其他领域约束的 profile，使测试只聚焦公开文本边界。"""
    return EtfProfile(
        etf=EtfIdentifier("SSE", "510300"),
        etf_type=etf_type,
        management_mode=management_mode,
        manager_name=manager_name,
        custodian_name=custodian_name,
        established_on=None,
        listed_on=date(2012, 5, 28),
        delisted_on=None,
        quote_currency="CNY",
        nav_currency="CNY",
        listing_status="LISTED",
        effective_from=date(2026, 7, 28),
        source_time_precision="DATE_ONLY",
        display_name=display_name,
    )


def _bar(*, trade_status: str | None, volume_unit: str = "SHARE") -> EtfDailyBar:
    """构造数值自洽的未复权日线，使测试只聚焦交易状态边界。"""
    return EtfDailyBar(
        trade_date=date(2026, 7, 28),
        open_price=Decimal("1.0"),
        high_price=Decimal("1.2"),
        low_price=Decimal("0.9"),
        close_price=Decimal("1.1"),
        volume_value=Decimal("100"),
        volume_unit=volume_unit,
        amount_value=Decimal("110"),
        currency="CNY",
        trade_status=trade_status,
    )
