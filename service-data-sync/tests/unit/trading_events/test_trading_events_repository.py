"""交易公开信息 canonical 发布仓储的重数与原因映射边界测试。"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from service_data_sync.domain.trading_events import BlockTrade
from service_data_sync.infrastructure.persistence.trading_events_repository import (
    _block_economic_fingerprint,
    _block_hash,
)


def test_block_trade_occurrence_is_not_part_of_economic_fingerprint() -> None:
    """经济字段相同的两笔大宗交易保留同一经济摘要，但 occurrence 仍使完整 revision 内容不同。"""
    first = _trade(occurrence_no=1)
    repeated = _trade(occurrence_no=2)

    fingerprint = _block_economic_fingerprint(first)

    assert fingerprint == _block_economic_fingerprint(repeated)
    assert _block_hash(first, fingerprint) != _block_hash(repeated, fingerprint)


def _trade(*, occurrence_no: int) -> BlockTrade:
    """构造经济字段相同但来源 occurrence 不同的合法大宗逐笔成交。"""
    return BlockTrade(
        source_trade_key="trade-1",
        source_security_code="600000",
        trade_date=date(2026, 7, 28),
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
