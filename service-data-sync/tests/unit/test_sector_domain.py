"""板块身份、周期和行情不变量的单元测试。"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from service_data_sync.domain.sector import (
    SectorBar,
    SectorIdentifier,
    SectorPeriod,
    SectorScheme,
)


def test_period_capabilities_are_distinct_and_not_derived() -> None:
    """日、周、月必须拥有不同原始能力名，避免运行时错误聚合。"""
    capabilities = {period.capability for period in SectorPeriod}

    assert capabilities == {"sector.bar.1d.raw", "sector.bar.1w.raw", "sector.bar.1mo.raw"}


def test_identifier_keeps_scheme_as_part_of_stable_identity() -> None:
    """相同板块代码落在不同分类体系时必须产生不同发布分区身份。"""
    industry = SectorIdentifier(SectorScheme.EASTMONEY_INDUSTRY, "BK0475")
    concept = SectorIdentifier(SectorScheme.EASTMONEY_CONCEPT, "BK0475")

    assert industry.qualified_key != concept.qualified_key


def test_sector_bar_rejects_non_native_volume_unit() -> None:
    """未确认横向换算口径前，成交量只能保留供应商原生单位。"""
    with pytest.raises(ValueError, match="provider_native"):
        SectorBar(
            period_end=date(2026, 6, 30),
            open_price=Decimal("10"),
            high_price=Decimal("11"),
            low_price=Decimal("9"),
            close_price=Decimal("10.5"),
            volume_value=Decimal("1000"),
            volume_unit="shares",
            amount_cny=Decimal("10500"),
            amplitude_percent=None,
            change_percent=None,
            change_amount=None,
            turnover_percent=None,
        )
