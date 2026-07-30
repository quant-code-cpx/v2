"""交易公开信息 canonical 发布仓储的重数与原因映射边界测试。"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

import pytest

from service_data_sync.domain.trading_events import BlockTrade
from service_data_sync.infrastructure.persistence.event_window_coverage import (
    EventCoverageIdentity,
)
from service_data_sync.infrastructure.persistence.trading_events_repository import (
    _block_economic_fingerprint,
    _block_hash,
    _block_roster_values,
)


def test_block_trade_occurrence_is_not_part_of_economic_fingerprint() -> None:
    """经济字段相同的两笔大宗交易保留同一经济摘要，但 occurrence 仍使完整 revision 内容不同。"""
    first = _trade(occurrence_no=1)
    repeated = _trade(occurrence_no=2)

    fingerprint = _block_economic_fingerprint(first)

    assert fingerprint == _block_economic_fingerprint(repeated)
    assert _block_hash(first, fingerprint) != _block_hash(repeated, fingerprint)


def test_global_trading_roster_excludes_valid_non_target_security() -> None:
    """合法六位目标外成交应保留 raw 并计排除，不得阻断冻结 A 股 roster。"""
    target = _trade(occurrence_no=1)
    outside = _trade(occurrence_no=2, security_code="400055")

    accepted, excluded = _block_roster_values(
        values=(target, outside),
        identities=(_identity("600000"),),
        identifier=None,
    )

    assert accepted == (target,)
    assert excluded == 1


def test_global_trading_roster_rejects_malformed_security_code() -> None:
    """来源代码坏格式仍属于 schema 风险，不能作为目标外合法行静默排除。"""
    malformed = _trade(occurrence_no=1, security_code="60X000")

    with pytest.raises(ValueError, match="malformed"):
        _block_roster_values(
            values=(malformed,),
            identities=(_identity("600000"),),
            identifier=None,
        )


def test_trading_roster_rejects_target_fact_outside_coverage_window() -> None:
    """冻结 roster 内证券的窗外成交必须失败，不能把它写入事实却发布合法空 coverage。"""
    outside_window = _trade(occurrence_no=1, trade_date=date(2026, 8, 1))

    with pytest.raises(ValueError, match="outside the requested coverage window"):
        _block_roster_values(
            values=(outside_window,),
            identities=(_identity("600000"),),
            identifier=None,
        )


def _trade(
    *,
    occurrence_no: int,
    security_code: str = "600000",
    trade_date: date = date(2026, 7, 28),
) -> BlockTrade:
    """构造经济字段相同但来源 occurrence 不同的合法大宗逐笔成交。"""
    return BlockTrade(
        source_trade_key="trade-1",
        source_security_code=security_code,
        trade_date=trade_date,
        occurrence_no=occurrence_no,
        execution_price=Decimal("10"),
        quantity_shares=100,
        notional_cny=Decimal("1000"),
        buyer_seat_code=None,
        buyer_seat_name="买方席位",
        seller_seat_code=None,
        seller_seat_name="卖方席位",
        reference_close_price=None,
        premium_discount_ratio=None,
        source_daily_rank=None,
        source_published_at=datetime(2026, 7, 28, 18, tzinfo=UTC),
        visible_time_precision="EXACT",
        visible_at=datetime(2026, 7, 28, 18, tzinfo=UTC),
    )


def _identity(symbol: str) -> EventCoverageIdentity:
    """构造完整覆盖测试窗口的冻结证券身份分段。"""
    return EventCoverageIdentity(
        security_id=7,
        identifier_version_id=UUID("10000000-0000-4000-8000-000000000007"),
        exchange="SSE",
        symbol=symbol,
        coverage_from=date(2026, 7, 1),
        coverage_to=date(2026, 7, 31),
    )
