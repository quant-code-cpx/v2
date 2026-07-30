"""ETF、两融和沪深港通 P0 reader 的无数据库合同边界测试。"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from types import SimpleNamespace
from typing import cast
from uuid import uuid4

import pytest

from service_data_sync.application.etf.query_contract import (
    ETF_V2_FIELDS,
    ETF_V2_FILTERS,
    ETF_V2_SORT_FIELDS,
)
from service_data_sync.application.ports.market_data_access import (
    MarketDataFilter,
    MarketDataQuery,
    MarketDataRequestValidationError,
)
from service_data_sync.infrastructure.database.models.publication.dataset_availability_observation import (  # noqa: E501
    DatasetAvailabilityObservation,
)
from service_data_sync.infrastructure.persistence.market_data_access_repository import (
    CatalogMarketDataAccessRepository,
    default_market_data_descriptors,
)
from service_data_sync.infrastructure.persistence.sqlalchemy_market_data_access_repository import (
    _availability_coverage_overlaps,
    _channel_filters,
    _etf_v2_runtime_sources,
    _optional_uuid_filter_values,
    _public_etf_v2_reason_code,
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
        limit=50,
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


def test_etf_availability_window_matching_uses_structured_date_coverage() -> None:
    """结构化增量空态可提示重叠详情查询，不相交窗口不得污染结果。"""
    query_start = date(2026, 7, 1)
    query_end = date(2026, 7, 30)

    assert _availability_coverage_overlaps(
        coverage_from=date(2026, 7, 15),
        coverage_to=date(2026, 7, 30),
        start=query_start,
        end=query_end,
    )
    assert not _availability_coverage_overlaps(
        coverage_from=date(2026, 5, 1),
        coverage_to=date(2026, 5, 31),
        start=query_start,
        end=query_end,
    )
    assert not _availability_coverage_overlaps(
        coverage_from=date(2026, 8, 1),
        coverage_to=date(2026, 8, 31),
        start=query_start,
        end=query_end,
    )


@pytest.mark.parametrize(
    ("availability", "reason_code", "expected"),
    [
        ("empty", "no_matching_facts", "NO_MATCHING_FACTS"),
        ("source_unavailable", "unavailable", "PROVIDER_UNAVAILABLE"),
        (
            "source_unavailable",
            "capability_not_configured",
            "CAPABILITY_NOT_CONFIGURED",
        ),
        (
            "source_unavailable",
            "directory_publication_unavailable",
            "PUBLICATION_NOT_AVAILABLE",
        ),
        (
            "currently_unsupported",
            "NAV_SEMANTICS_UNSUPPORTED_MONEY_MARKET",
            "NAV_SEMANTICS_UNSUPPORTED_MONEY_MARKET",
        ),
    ],
)
def test_etf_v2_reason_codes_are_closed_at_the_public_boundary(
    availability: str,
    reason_code: str,
    expected: str,
) -> None:
    """内部历史小写原因只能映射到冻结公开枚举，避免三服务运行时合同漂移。"""
    observation = cast(
        DatasetAvailabilityObservation,
        SimpleNamespace(availability=availability, reason_code=reason_code),
    )

    assert _public_etf_v2_reason_code(observation) == expected


def test_etf_v2_reason_code_rejects_an_unknown_internal_value() -> None:
    """未知内部原因必须失败关闭，不能把未评审字符串透传给 strict API。"""
    observation = cast(
        DatasetAvailabilityObservation,
        SimpleNamespace(availability="source_unavailable", reason_code="new_provider_state"),
    )

    with pytest.raises(ValueError, match="not registered"):
        _public_etf_v2_reason_code(observation)


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


def test_etf_v2_catalog_freezes_full_fields_sources_and_safe_text_filters() -> None:
    """ETF v2 目录必须公开完整真实字段，并只允许参数化前缀与包含查询。"""
    all_descriptors = default_market_data_descriptors()
    descriptor_keys = [(item.code, item.schema_version) for item in all_descriptors]
    descriptors = {(item.code, item.schema_version): item for item in all_descriptors}
    assert len(descriptor_keys) == len(set(descriptor_keys))
    for code in (
        "fund.etf.profile.reported",
        "fund.etf.bar.1d.reported",
        "fund.etf.nav.1d.reported",
        "fund.etf.trading_state.reported",
    ):
        assert {
            schema_version
            for dataset_code, schema_version in descriptor_keys
            if dataset_code == code
        } == {1, 2}
    profile = descriptors[("fund.etf.profile.reported", 2)]
    bars = descriptors[("fund.etf.bar.1d.reported", 2)]
    navs = descriptors[("fund.etf.nav.1d.reported", 2)]
    states = descriptors[("fund.etf.trading_state.reported", 2)]
    for code, descriptor in (
        ("fund.etf.profile.reported", profile),
        ("fund.etf.bar.1d.reported", bars),
        ("fund.etf.nav.1d.reported", navs),
        ("fund.etf.trading_state.reported", states),
    ):
        assert {field.name for field in descriptor.fields} == ETF_V2_FIELDS[code]
        assert {
            item.field: frozenset(item.operators) for item in descriptor.filters
        } == ETF_V2_FILTERS[code]
        assert frozenset(descriptor.allowed_sort_fields) == ETF_V2_SORT_FIELDS[code]

    assert {field.name for field in profile.fields} == {
        "etfEntityRef",
        "exchange",
        "symbol",
        "displayName",
        "etfType",
        "managementMode",
        "managerName",
        "custodianName",
        "listedOn",
        "delistedOn",
        "listingStatus",
        "quoteCurrency",
        "navCurrency",
        "sourceTimePrecision",
    }
    assert {source.publisher for source in profile.sources} == {
        "上海证券交易所",
        "深圳证券交易所",
    }
    assert profile.visibility_modes == ("CURRENT",)
    assert {field.name for field in bars.fields} >= {
        "open",
        "high",
        "low",
        "close",
        "volume",
        "volumeUnit",
        "amount",
        "adjustment",
    }
    assert {field.name for field in navs.fields} >= {"navKind", "currency", "finality"}
    assert {field.name for field in states.fields} >= {"effectiveTo", "reason"}

    request = MarketDataQuery(
        dataset_code="fund.etf.profile.reported",
        schema_version=2,
        business_scope="ETF",
        identity=None,
        time={"dimension": "EFFECTIVE_AT", "from": "2026-07-29", "to": "2026-07-29"},
        visibility={"mode": "CURRENT"},
        selection={"qualityStatuses": ("PASSED",)},
        fields=("symbol", "displayName"),
        filters=(
            MarketDataFilter("exchange", "EQ", ("SSE",)),
            MarketDataFilter("symbol", "PREFIX", ("510",)),
            MarketDataFilter("displayName", "CONTAINS", ("沪深",)),
        ),
        sort=(("symbol", "ASC"),),
        limit=50,
        request_fingerprint="etf-v2-catalog-test",
    )
    CatalogMarketDataAccessRepository()._assert_contract(profile, request)

    invalid = replace(
        request,
        filters=(
            MarketDataFilter("exchange", "EQ", ("SSE",)),
            MarketDataFilter("symbol", "PREFIX", ("510", "159")),
        ),
    )
    with pytest.raises(MarketDataRequestValidationError):
        CatalogMarketDataAccessRepository()._assert_contract(profile, invalid)

    for rejected in (
        replace(request, business_scope="FUND"),
        replace(
            request,
            identity={
                "identifiers": [
                    {"scheme": "venue_symbol", "value": "SSE.510300"},
                ]
            },
        ),
        replace(request, limit=51),
        replace(
            request,
            visibility={
                "mode": "PUBLIC_PIT",
                "asOf": "2026-07-29T00:00:00Z",
                "knownAt": "2026-07-29T00:00:00Z",
            },
        ),
        replace(
            request,
            selection={
                "qualityStatuses": ("PASSED",),
                "knownDataVersion": str(uuid4()),
            },
        ),
        replace(
            request,
            selection={
                "qualityStatuses": ("PASSED",),
                "methodology": {
                    "code": "etf-reported-source-contract",
                    "version": "1",
                },
            },
        ),
        replace(request, selection={"qualityStatuses": ("FAILED",)}),
    ):
        with pytest.raises(MarketDataRequestValidationError):
            CatalogMarketDataAccessRepository()._assert_contract(profile, rejected)

    CatalogMarketDataAccessRepository()._assert_contract(
        profile,
        replace(
            request,
            selection={
                "qualityStatuses": ("PASSED", "WARNED"),
                "dataVersion": str(uuid4()),
            },
        ),
    )

    bar_request = replace(
        request,
        dataset_code="fund.etf.bar.1d.reported",
        time={"dimension": "TRADE_DATE", "from": "2025-07-29", "to": "2026-07-30"},
        fields=("tradeDate", "close"),
        filters=(MarketDataFilter("etfEntityRef", "EQ", (str(uuid4()),)),),
        sort=(("tradeDate", "ASC"),),
        limit=366,
    )
    with pytest.raises(MarketDataRequestValidationError):
        CatalogMarketDataAccessRepository()._assert_contract(
            descriptors[("fund.etf.bar.1d.reported", 2)],
            bar_request,
        )

    state_request = replace(
        request,
        dataset_code="fund.etf.trading_state.reported",
        time={"dimension": "EFFECTIVE_AT", "from": "2025-07-29", "to": "2026-07-30"},
        fields=("effectiveFrom", "state"),
        filters=(MarketDataFilter("etfEntityRef", "EQ", (str(uuid4()),)),),
        sort=(("effectiveFrom", "ASC"),),
        limit=500,
    )
    with pytest.raises(MarketDataRequestValidationError):
        CatalogMarketDataAccessRepository()._assert_contract(
            descriptors[("fund.etf.trading_state.reported", 2)],
            state_request,
        )

    nav_without_kind = replace(
        request,
        dataset_code="fund.etf.nav.1d.reported",
        time={"dimension": "TRADE_DATE", "from": "2026-07-29", "to": "2026-07-29"},
        fields=("navDate", "nav"),
        filters=(MarketDataFilter("etfEntityRef", "EQ", (str(uuid4()),)),),
        sort=(("navDate", "ASC"),),
        limit=366,
    )
    with pytest.raises(MarketDataRequestValidationError):
        CatalogMarketDataAccessRepository()._assert_contract(
            descriptors[("fund.etf.nav.1d.reported", 2)],
            nav_without_kind,
        )


def test_etf_v2_runtime_sources_match_the_publication_without_adapter_labels() -> None:
    """ETF v2 运行时来源必须与目录冻结来源一致，并保留授权、权威性和覆盖说明。"""
    descriptors = {
        (item.code, item.schema_version): item for item in default_market_data_descriptors()
    }
    cases = (
        (
            "fund.etf.profile.reported",
            "SSE",
            "src_sse_etf_directory",
            "上海证券交易所",
            True,
        ),
        (
            "fund.etf.profile.reported",
            "SZSE",
            "src_szse_fund_directory",
            "深圳证券交易所",
            True,
        ),
        (
            "fund.etf.bar.1d.reported",
            None,
            "src_tencent_etf_kline",
            "腾讯证券",
            False,
        ),
        (
            "fund.etf.nav.1d.reported",
            None,
            "src_eastmoney_etf_nav",
            "东方财富",
            False,
        ),
        (
            "fund.etf.trading_state.reported",
            None,
            "src_eastmoney_etf_nav",
            "东方财富",
            False,
        ),
    )

    for code, exchange, source_ref, publisher, authoritative in cases:
        sources = _etf_v2_runtime_sources(descriptors[(code, 2)], exchange=exchange)

        assert len(sources) == 1
        assert sources[0].source_ref == source_ref
        assert sources[0].publisher == publisher
        assert sources[0].authoritative is authoritative
        assert sources[0].redistribution == "INTERNAL_ONLY"
        assert sources[0].coverage_note
