"""验证净额只是同一不可变 bundle 内可追溯的版本化派生投影。"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID

from service_data_sync.domain.stock_connect import StockConnectChannel
from service_data_sync.infrastructure.persistence.stock_connect_center_repository import (
    _derived_net_fact,
    _projection_input_lineage,
)


def test_buy_minus_sell_projection_freezes_methodology_and_all_input_identity() -> None:
    """派生血缘必须同时钉住 release、行、通道、日期、币种与两个输入字段。"""
    lineage = _projection_input_lineage(
        source_kind="active",
        release_id=UUID("10000000-0000-4000-8000-000000000001"),
        row_id=UUID("20000000-0000-4000-8000-000000000001"),
        channel=StockConnectChannel(channel="SH", direction="SOUTHBOUND"),
        trade_date=date(2026, 7, 30),
        currency="HKD",
        rank_no=3,
    )

    fact = _derived_net_fact(
        Decimal("123.45"),
        Decimal("23.45"),
        "HKD",
        {"buyAmount": "REPORTED", "sellAmount": "REPORTED"},
        lineage,
    )

    assert fact["availability"] == "DERIVED"
    assert fact["value"] == {"amount": "100.00", "currency": "HKD", "unit": "BASE"}
    lineage_ref = str(fact["lineageRef"])
    assert "buy-minus-sell-v1" in lineage_ref
    assert "inputs:buyAmount,sellAmount" in lineage_ref
    assert "active-release:10000000-0000-4000-8000-000000000001" in lineage_ref
    assert "row:20000000-0000-4000-8000-000000000001" in lineage_ref
    assert "channel:SH_SOUTHBOUND" in lineage_ref
    assert "trade-date:2026-07-30" in lineage_ref
    assert "currency:HKD" in lineage_ref
    assert "rank:3" in lineage_ref


def test_net_projection_stays_unavailable_when_either_reported_input_is_missing() -> None:
    """缺任一买卖额时不得从成交额或另一字段补算，也不得留下伪派生血缘。"""
    fact = _derived_net_fact(
        Decimal("123.45"),
        None,
        "CNY",
        {"buyAmount": "REPORTED", "sellAmount": "SOURCE_MISSING"},
        "market-release:input",
    )

    assert fact == {
        "availability": "SOURCE_MISSING",
        "value": None,
        "lineageRef": None,
    }
