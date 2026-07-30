"""市场完整包仓储发布前质量证据验证测试。"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import cast
from uuid import uuid4

import pytest

from service_data_sync.application.ports.market_overview import MarketComponentCandidate
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.persistence.market_overview_repository import (
    SqlAlchemyMarketOverviewRepository,
)


def test_publish_rejects_empty_quality_checks_before_database_access() -> None:
    """空质量检查不能形成显示为 passed 的首页 publication。"""
    repository = SqlAlchemyMarketOverviewRepository(cast(DatabaseClient, object()))
    trade_date = date(2026, 7, 28)
    component = _component(trade_date)
    overview = {
        "tradeDate": trade_date.isoformat(),
        "quality": {
            "componentCount": 1,
            "passedCount": 1,
            "sourceBindings": [{"component": component.dataset_code}],
            "checks": [],
        },
    }

    with pytest.raises(ValueError, match="requires passed quality checks"):
        repository.publish_complete_bundle(
            trade_date=trade_date,
            components=(component,),
            overview=overview,
        )


def _component(trade_date: date) -> MarketComponentCandidate:
    """构造无需数据库即可触发发布前验证的单组件候选。"""
    observed_at = datetime(2026, 7, 28, 11, tzinfo=UTC)
    return MarketComponentCandidate(
        data_version=uuid4(),
        dataset_code="equity.quote.eod",
        partition_key=trade_date.isoformat(),
        trade_date=trade_date,
        payload={"records": []},
        source={"provider": "test"},
        methodology={},
        quality={"status": "passed"},
        observed_at=observed_at,
    )
