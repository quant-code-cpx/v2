"""公司事件 canonical 发布仓储的领域字段映射测试。"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from service_data_sync.domain.corporate import DisclosureDocument, EarningsGuidanceMetric
from service_data_sync.infrastructure.persistence.corporate_events_repository import (
    _corporate_roster_values,
    _metric_row,
)
from service_data_sync.infrastructure.persistence.event_window_coverage import (
    EventCoverageIdentity,
)


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


def test_global_corporate_roster_excludes_valid_non_a_share_rows_with_count() -> None:
    """全市场官方样例中的合法三板代码保留 raw 审计，但不阻断 A 股 coverage。"""
    target_document = _document("notice-a", "600519")
    outside_document = _document("notice-outside", "400055")
    target_metric = _guidance("notice-a", "600519")
    outside_metric = _guidance("notice-outside", "400055")

    documents, guidance, express, excluded = _corporate_roster_values(
        documents=(target_document, outside_document),
        guidance=(target_metric, outside_metric),
        express=(),
        identities=(_identity("600519"),),
        identifier=None,
    )

    assert documents == (target_document,)
    assert guidance == (target_metric,)
    assert express == ()
    assert excluded == 2


def test_global_corporate_roster_rejects_malformed_or_ambiguous_target_code() -> None:
    """坏格式或 roster 内歧义仍必须失败关闭，不能被目标外排除规则吞掉。"""
    malformed = _document("notice-bad", "60X519")
    with pytest.raises(ValueError, match="malformed"):
        _corporate_roster_values(
            documents=(malformed,),
            guidance=(),
            express=(),
            identities=(_identity("600519"),),
            identifier=None,
        )

    target = _document("notice-a", "600519")
    with pytest.raises(ValueError, match="ambiguous"):
        _corporate_roster_values(
            documents=(target,),
            guidance=(),
            express=(),
            identities=(_identity("600519"), _identity("600519", security_id=8)),
            identifier=None,
        )


def test_corporate_roster_rejects_target_fact_outside_coverage_window() -> None:
    """冻结 roster 内证券的窗外公告必须失败，不能生成与真实事实矛盾的空 coverage。"""
    outside_window = _document(
        "notice-outside-window",
        "600519",
        announced_on=date(2026, 8, 1),
    )

    with pytest.raises(ValueError, match="outside the requested coverage window"):
        _corporate_roster_values(
            documents=(outside_window,),
            guidance=(),
            express=(),
            identities=(_identity("600519"),),
            identifier=None,
        )


def _document(
    source_document_id: str,
    security_code: str,
    *,
    announced_on: date = date(2026, 7, 28),
) -> DisclosureDocument:
    """构造一个日期级官方公告目录事实。"""
    return DisclosureDocument(
        source_document_id=source_document_id,
        source_security_code=security_code,
        title="业绩预告",
        category="EARNINGS_GUIDANCE",
        official_url=f"https://example.test/{source_document_id}",
        announced_on=announced_on,
        source_visible_at=None,
        visible_time_precision="DATE_ONLY",
        public_usable_at=datetime(2026, 7, 28, 9, 30, tzinfo=UTC),
        content_sha256="a" * 64,
    )


def _guidance(source_document_id: str, security_code: str) -> EarningsGuidanceMetric:
    """构造一项能回指同批公告的业绩预告指标。"""
    return EarningsGuidanceMetric(
        source_document_id=source_document_id,
        source_security_code=security_code,
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


def _identity(symbol: str, *, security_id: int = 7) -> EventCoverageIdentity:
    """构造完整覆盖测试窗口的冻结证券身份分段。"""
    return EventCoverageIdentity(
        security_id=security_id,
        identifier_version_id=UUID(f"10000000-0000-4000-8000-{security_id:012d}"),
        exchange="SSE",
        symbol=symbol,
        coverage_from=date(2026, 7, 1),
        coverage_to=date(2026, 7, 31),
    )
