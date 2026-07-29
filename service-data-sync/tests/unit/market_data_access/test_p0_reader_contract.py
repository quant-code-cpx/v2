"""两融和沪深港通 P0 reader 的无数据库合同边界测试。"""

from __future__ import annotations

from uuid import uuid4

import pytest

from service_data_sync.application.ports.market_data_access import (
    MarketDataFilter,
    MarketDataQuery,
    MarketDataRequestValidationError,
)
from service_data_sync.infrastructure.persistence.market_data_access_repository import (
    default_market_data_descriptors,
)
from service_data_sync.infrastructure.persistence.sqlalchemy_market_data_access_repository import (
    _channel_filters,
    _optional_uuid_filter_values,
)


def _request(*, filters: tuple[MarketDataFilter, ...]) -> MarketDataQuery:
    """构造仅用于 reader 参数解析的最小市场数据请求。"""
    return MarketDataQuery(
        dataset_code="market.stock_connect.market_stat.reported",
        schema_version=1,
        business_scope="CHANNEL",
        identity=None,
        time={"dimension": "TRADE_DATE", "from": "2026-07-28", "to": "2026-07-29"},
        visibility={"mode": "CURRENT"},
        selection={"qualityStatuses": ("PASSED",)},
        fields=(),
        filters=filters,
        sort=(),
        limit=100,
        request_fingerprint="a" * 64,
    )


def test_stock_connect_reader_requires_exact_channel_and_direction() -> None:
    """港通 reader 只能选择一个明确通道方向，不能从实体 UUID 或默认值推断分区。"""
    request = _request(
        filters=(
            MarketDataFilter("channel", "EQ", ("SH",)),
            MarketDataFilter("direction", "EQ", ("NORTHBOUND",)),
        )
    )

    assert _channel_filters(request) == ("SH", "NORTHBOUND")
    with pytest.raises(MarketDataRequestValidationError):
        _channel_filters(_request(filters=(MarketDataFilter("channel", "EQ", ("SH",)),)))


def test_optional_entity_filter_accepts_only_uuid_values() -> None:
    """证券或工具过滤器只能传永久 UUID，不能把来源代码混入 SQL reader。"""
    entity_id = uuid4()
    request = _request(filters=(MarketDataFilter("instrumentEntityRef", "EQ", (str(entity_id),)),))

    assert _optional_uuid_filter_values(request, field="instrumentEntityRef") == (entity_id,)
    invalid = _request(filters=(MarketDataFilter("instrumentEntityRef", "EQ", ("SSE.600519",)),))
    with pytest.raises(MarketDataRequestValidationError):
        _optional_uuid_filter_values(invalid, field="instrumentEntityRef")


def test_catalog_exposes_real_partition_filters_for_p0_readers() -> None:
    """两融与港通目录声明 reader 实际需要的分区键，避免公开不可执行的伪过滤器。"""
    descriptors = {item.code: item for item in default_market_data_descriptors()}
    margin_security_filters = {
        item.field for item in descriptors["market.margin.security.1d.reported"].filters
    }
    stock_connect_filters = {
        item.field for item in descriptors["market.stock_connect.market_stat.reported"].filters
    }

    assert "venueEntityRef" in margin_security_filters
    assert stock_connect_filters >= {"channel", "direction"}
