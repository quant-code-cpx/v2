"""沪深港通 canonical 发布仓储的制度和依赖边界测试。"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest

from service_data_sync.domain.stock_connect import (
    StockConnectActiveSecurity,
    StockConnectChannel,
    StockConnectMarketDaily,
)
from service_data_sync.infrastructure.database.models.market import StockConnectDisclosureRegime
from service_data_sync.infrastructure.persistence.stock_connect_market_data_repository import (
    _active_hash,
    _resolve_active_instrument,
    _validate_regime_value,
)


def test_disclosure_regime_rejects_value_not_published_in_that_period() -> None:
    """制度白名单未包含买入额时，即使数值看似合理也不能作为官方事实发布。"""
    regime = StockConnectDisclosureRegime(
        regime_id=uuid4(),
        channel="SH",
        direction="NORTHBOUND",
        effective_from=date(2024, 8, 19),
        effective_to=None,
        available_fields=["turnover_amount"],
        methodology_version_id=uuid4(),
        evidence_ref="test://disclosure-regime",
    )
    value = StockConnectMarketDaily(
        trade_date=date(2026, 7, 28),
        buy_amount=Decimal("1"),
        sell_amount=None,
        turnover_amount=Decimal("2"),
        net_buy_amount=None,
        quota_balance=None,
        currency="CNY",
        availability_status="PARTIAL",
    )

    with pytest.raises(ValueError, match="contradicts disclosure regime"):
        _validate_regime_value(value, regime)


def test_active_hash_changes_when_bound_market_release_changes() -> None:
    """活跃榜数值不变但同日市场统计版本变化时，必须产生新 revision 以固定依赖关系。"""
    value = StockConnectActiveSecurity(
        source_instrument_code="600000",
        trade_date=date(2026, 7, 28),
        rank_no=1,
        buy_amount=None,
        sell_amount=None,
        turnover_amount=Decimal("100"),
        currency="CNY",
    )

    instrument_id = uuid4()
    first = _active_hash(value, instrument_id, uuid4())
    second = _active_hash(value, instrument_id, uuid4())

    assert first != second


class _EmptyScalarResult:
    """模拟没有任何稳定身份候选的数据库结果。"""

    def scalars(self) -> _EmptyScalarResult:
        """返回自身以匹配 SQLAlchemy scalar result 链。"""
        return self

    def all(self) -> list[object]:
        """返回真实空候选集合。"""
        return []


class _EmptyIdentitySession:
    """仅实现身份解析测试需要的只读查询。"""

    def execute(self, statement: object) -> _EmptyScalarResult:
        """忽略已验证查询结构并返回空身份候选。"""
        del statement
        return _EmptyScalarResult()


def test_recent_source_identity_gap_degrades_without_blocking_fact() -> None:
    """近期港股主档缺件也应保留来源代码并返回空 entity，而不是阻断真实榜单事实。"""
    value = StockConnectActiveSecurity(
        source_instrument_code="00700",
        source_instrument_name="腾讯控股",
        trade_date=date.today(),
        rank_no=1,
        buy_amount=Decimal("120000"),
        sell_amount=Decimal("110000"),
        turnover_amount=Decimal("230000"),
        currency="HKD",
    )

    resolved = _resolve_active_instrument(
        _EmptyIdentitySession(),  # type: ignore[arg-type]
        channel=StockConnectChannel("SH", "SOUTHBOUND"),
        value=value,
    )

    assert resolved is None
    assert value.source_instrument_code == "00700"
    assert value.source_instrument_name == "腾讯控股"
