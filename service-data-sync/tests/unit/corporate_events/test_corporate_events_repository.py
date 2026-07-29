"""公司事件 canonical 发布仓储的领域字段映射测试。"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import uuid4

from service_data_sync.domain.corporate import EarningsGuidanceMetric
from service_data_sync.infrastructure.persistence.corporate_events_repository import _metric_row


def test_guidance_row_keeps_yoy_interval_and_prior_value_separate() -> None:
    """预告同比上下界和同期基数都必须独立落列，不能压成一个变化率或用零代替缺失端。"""
    metric = EarningsGuidanceMetric(
        source_document_id="notice-1",
        source_security_code="600000",
        report_period=date(2026, 6, 30),
        guidance_type="PRE_INCREASE",
        metric_code="NET_PROFIT",
        amount_low=Decimal("10"),
        amount_high=Decimal("20"),
        yoy_low=Decimal("0.1"),
        yoy_high=Decimal("0.2"),
        prior_period_value=Decimal("8"),
        currency="CNY",
    )

    row = _metric_row(uuid4(), "GUIDANCE", metric)

    assert row["value_low"] == Decimal("10")
    assert row["value_high"] == Decimal("20")
    assert row["change_ratio_low"] == Decimal("0.1")
    assert row["change_ratio_high"] == Decimal("0.2")
    assert row["prior_value"] == Decimal("8")
    assert row["change_ratio"] is None
