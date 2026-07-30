"""0028 内部市场数据 POST catalog/query 合同、认证和游标边界测试。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from service_data_sync.application.ports.market_data_access import (
    MarketDataAccessRepository,
    MarketDataAccessUnavailable,
    MarketDataDatasetDescriptor,
    MarketDataFieldDescriptor,
    MarketDataFilterDescriptor,
    MarketDataQuery,
    MarketDataQueryPage,
    MarketDataSourceDescriptor,
)
from service_data_sync.application.ports.sector_market_data import SectorMarketDataRepository
from service_data_sync.bootstrap.settings import load_settings
from service_data_sync.interfaces.internal_sector_api import create_app

_DATA_VERSION = UUID("10000000-0000-4000-8000-000000000099")
_CONTRACT_ENTITY = UUID("10000000-0000-4000-8000-000000000100")
_TEST_SOURCE = MarketDataSourceDescriptor(
    "src_test_provider", "测试来源", "测试日行情", True, "INTERNAL_ONLY"
)


class FakeMarketDataRepository:
    """提供一个 release-aware typed reader 替身，避免路由测试访问数据库或 Provider。"""

    def __init__(self) -> None:
        """初始化固定 dataset 描述和记录调用的查询参数。"""
        self.queries: list[tuple[MarketDataQuery, str | None]] = []

    def search_datasets(
        self,
        *,
        priorities: frozenset[str],
        availability: frozenset[str],
        query: str | None,
    ) -> tuple[MarketDataDatasetDescriptor, ...]:
        """按简单优先级和可用性过滤返回唯一 P0 描述。"""
        descriptor = MarketDataDatasetDescriptor(
            code="derivative.bar.1d.reported",
            schema_version=1,
            title="真实衍生品合约日行情",
            domain="DERIVATIVE",
            priority="P0",
            availability="AVAILABLE",
            allowed_time_dimensions=("TRADE_DATE",),
            visibility_modes=("CURRENT", "PUBLIC_PIT"),
            fields=(
                MarketDataFieldDescriptor("tradeDate", "DATE", False, True, None, sortable=True),
                MarketDataFieldDescriptor("close", "DECIMAL_STRING", False, True, None),
                MarketDataFieldDescriptor("settlement", "DECIMAL_STRING", True, True, None),
                MarketDataFieldDescriptor("contract", "CODE", False, True, None, ("EQ", "IN")),
            ),
            filters=(
                MarketDataFilterDescriptor("contract", ("EQ", "IN")),
                MarketDataFilterDescriptor("tradeDate", ("EQ", "GTE", "LTE", "RANGE")),
            ),
            allowed_sort_fields=("tradeDate",),
            sources=(_TEST_SOURCE,),
            methodologies=({"code": "test-method", "version": "1", "kind": "REPORTED"},),
        )
        if priorities and descriptor.priority not in priorities:
            return ()
        if availability and descriptor.availability not in availability:
            return ()
        if query is not None and query.lower() not in descriptor.code:
            return ()
        return (descriptor,)

    def query(self, *, request: MarketDataQuery, after: str | None) -> MarketDataQueryPage:
        """返回固定 data version 的两页结果并记录路由解码出的继续位置。"""
        self.queries.append((request, after))
        if after is None:
            items = (_record("2026-07-28", "3475", "3468"),)
            next_position = "2026-07-28"
        else:
            items = (_record("2026-07-29", "3500", "3492"),)
            next_position = None
        return MarketDataQueryPage(
            data_version=_DATA_VERSION,
            published_at=datetime(2026, 7, 29, tzinfo=UTC),
            knowledge_cutoff=datetime(2026, 7, 29, tzinfo=UTC),
            public_usable_at=datetime(2026, 7, 29, tzinfo=UTC),
            quality_status="passed",
            completeness="COMPLETE",
            items=items,
            next_position=next_position,
            methodology={"code": "test-method", "version": "1", "kind": "REPORTED"},
            sources=(_TEST_SOURCE,),
            coverage={
                "from": "2026-07-28",
                "to": "2026-07-29",
                "pitCoverage": "COMPLETE",
                "gaps": [],
            },
        )


class FakeUnavailableMarketDataRepository(FakeMarketDataRepository):
    """按测试指定的精确空态拒绝查询，用于验证 HTTP 空态映射。"""

    def __init__(self, error: MarketDataAccessUnavailable) -> None:
        """保存待抛出的安全可用性结论。"""
        super().__init__()
        self._error = error

    def query(self, *, request: MarketDataQuery, after: str | None) -> MarketDataQueryPage:
        """抛出精确空态，模拟 typed reader 没有可返回 publication。"""
        self.queries.append((request, after))
        raise self._error


def _record(trade_date: str, close: str, settlement: str) -> dict[str, object]:
    """构造符合 v1 `DerivativeRecord` 的最小完整记录，验证路由不生成非契约字段。"""
    observed_at = "2026-07-29T00:00:00Z"
    return {
        "recordRef": f"derivative:{trade_date}:{uuid4()}",
        "recordType": "DERIVATIVE",
        "entity": {
            "entityRef": str(_CONTRACT_ENTITY),
            "entityType": "FUTURE_CONTRACT",
            "identifiers": [{"scheme": "venue_contract_code", "value": "CFFEX.IF2608"}],
        },
        "time": {"tradeDate": trade_date},
        "publicUsableAt": observed_at,
        "availabilityBasis": "EXACT",
        "sourcePublishedAt": observed_at,
        "observedAt": observed_at,
        "dataVersion": str(_DATA_VERSION),
        "sourceRef": "src_test_provider",
        "methodologyVersion": "1",
        "qualityStatus": "PASSED",
        "revision": {"revisionNumber": 1, "currentInPublication": True},
        "values": {"tradeDate": trade_date, "close": close, "settlement": settlement},
    }


def _client(
    repository: FakeMarketDataRepository | None = None,
) -> tuple[TestClient, dict[str, str], FakeMarketDataRepository]:
    """构造共享异常处理和精确 bearer 校验均已注册的内部测试应用。"""
    settings = load_settings()
    selected_repository = repository or FakeMarketDataRepository()
    app = create_app(
        settings=settings,
        repository=cast(SectorMarketDataRepository, object()),
        market_data_repository=cast(MarketDataAccessRepository, selected_repository),
    )
    token = settings.internal_api_bearer_token.get_secret_value()
    return TestClient(app), {"Authorization": f"Bearer {token}"}, selected_repository


def _query_body(*, cursor: str | None = None, limit: int = 1) -> dict[str, object]:
    """构造字段、筛选、排序和分页均受限的单 dataset 查询请求。"""
    return {
        "dataset": {"code": "derivative.bar.1d.reported", "schemaVersion": 1},
        "businessScope": "CONTRACT",
        "identity": {"identifiers": [{"scheme": "venue_contract_code", "value": "CFFEX.IF2608"}]},
        "time": {"dimension": "TRADE_DATE", "from": "2026-07-28", "to": "2026-07-29"},
        "visibility": {"mode": "CURRENT"},
        "selection": {"qualityStatuses": ["PASSED"]},
        "fields": ["tradeDate", "close", "settlement"],
        "filters": [{"field": "contract", "operator": "EQ", "values": ["CFFEX.IF2608"]}],
        "sort": [{"field": "tradeDate", "direction": "ASC"}],
        "page": {"limit": limit, **({} if cursor is None else {"cursor": cursor})},
    }


def _etf_v2_query_body() -> dict[str, object]:
    """构造只用于 HTTP 边界测试的 ETF v2 current 查询。"""
    body = _query_body()
    body["dataset"] = {"code": "fund.etf.profile.reported", "schemaVersion": 2}
    body["businessScope"] = "ETF"
    body["identity"] = None
    body["time"] = {
        "dimension": "EFFECTIVE_AT",
        "from": "2026-07-29",
        "to": "2026-07-29",
    }
    body["fields"] = ["symbol", "displayName"]
    body["filters"] = [{"field": "exchange", "operator": "EQ", "values": ["SSE"]}]
    body["sort"] = [{"field": "symbol", "direction": "ASC"}]
    return body


def test_market_data_routes_are_post_only_authenticated_and_version_bound(
    configured_environment: None,
) -> None:
    """catalog/query 必须仅接受精确 bearer 的 POST，并在多页间固定 data version。"""
    del configured_environment
    client, headers, repository = _client()

    catalog = client.post(
        "/internal/v1/market-data/datasets/search",
        headers=headers,
        json={"priorities": ["P0"], "availability": ["AVAILABLE"], "page": {"limit": 10}},
    )
    first = client.post("/internal/v1/market-data/query", headers=headers, json=_query_body())
    cursor = first.json()["meta"]["page"]["nextCursor"]
    second = client.post(
        "/internal/v1/market-data/query", headers=headers, json=_query_body(cursor=cursor)
    )
    unauthenticated = client.post("/internal/v1/market-data/query", json=_query_body())
    wrong_method = client.get("/internal/v1/market-data/query", headers=headers)

    assert catalog.status_code == 200
    assert catalog.json()["datasets"][0]["dataset"]["code"] == "derivative.bar.1d.reported"
    assert first.status_code == 200
    assert first.headers["x-data-version"] == str(_DATA_VERSION)
    assert first.json()["records"][0]["values"]["settlement"] == "3468"
    assert second.status_code == 200
    assert second.json()["records"][0]["values"]["tradeDate"] == "2026-07-29"
    assert repository.queries[1][1] == "2026-07-28"
    assert unauthenticated.status_code == 401
    assert wrong_method.status_code == 405


def test_market_data_cursor_rejects_a_changed_normalized_request(
    configured_environment: None,
) -> None:
    """HMAC 游标绑定字段、筛选、排序和页大小，任何变更都不能混入旧版本续页。"""
    del configured_environment
    client, headers, _repository = _client()
    first = client.post("/internal/v1/market-data/query", headers=headers, json=_query_body())
    cursor = first.json()["meta"]["page"]["nextCursor"]
    changed = client.post(
        "/internal/v1/market-data/query",
        headers=headers,
        json=_query_body(cursor=cursor, limit=2),
    )

    assert changed.status_code == 409
    assert changed.json()["code"] == "cursor-mismatch"


def test_market_data_query_accepts_etf_business_scope(configured_environment: None) -> None:
    """ETF dataset 的请求必须穿过 HTTP 合同白名单，不能只在仓储直调时可用。"""
    del configured_environment
    client, headers, repository = _client()
    body = _query_body()
    body["businessScope"] = "ETF"

    response = client.post("/internal/v1/market-data/query", headers=headers, json=body)

    assert response.status_code == 200
    assert repository.queries[0][0].business_scope == "ETF"


def test_etf_v2_http_boundary_rejects_unsupported_visibility_scope_and_selection(
    configured_environment: None,
) -> None:
    """ETF v2 必须在 HTTP 入口 fail-closed，不能依赖具体仓储二次校验。"""
    del configured_environment
    public_pit = _etf_v2_query_body()
    public_pit["visibility"] = {
        "mode": "PUBLIC_PIT",
        "asOf": "2026-07-30T00:00:00Z",
        "knownAt": "2026-07-30T00:00:00Z",
    }
    operational_replay = _etf_v2_query_body()
    operational_replay["visibility"] = {
        "mode": "OPERATIONAL_REPLAY",
        "asOf": "2026-07-30T00:00:00Z",
        "knownAt": "2026-07-30T00:00:00Z",
    }
    known_version = _etf_v2_query_body()
    known_version["selection"] = {
        "qualityStatuses": ["PASSED"],
        "knownDataVersion": str(uuid4()),
    }
    methodology = _etf_v2_query_body()
    methodology["selection"] = {
        "qualityStatuses": ["PASSED"],
        "methodology": {"code": "reported", "version": "1", "kind": "REPORTED"},
    }
    fund_scope = _etf_v2_query_body()
    fund_scope["businessScope"] = "FUND"
    silent_identity = _etf_v2_query_body()
    silent_identity["identity"] = {
        "identifiers": [{"scheme": "venue_symbol", "value": "SSE.510300"}]
    }
    oversized_profile_page = _etf_v2_query_body()
    oversized_profile_page["page"] = {"limit": 51}
    oversized_daily_range = _etf_v2_query_body()
    oversized_daily_range["dataset"] = {
        "code": "fund.etf.bar.1d.reported",
        "schemaVersion": 2,
    }
    oversized_daily_range["time"] = {
        "dimension": "TRADE_DATE",
        "from": "2025-07-29",
        "to": "2026-07-30",
    }
    oversized_daily_range["fields"] = ["tradeDate", "close"]
    oversized_daily_range["filters"] = [
        {
            "field": "etfEntityRef",
            "operator": "EQ",
            "values": [str(uuid4())],
        }
    ]
    oversized_daily_range["sort"] = [{"field": "tradeDate", "direction": "ASC"}]
    profile_multi_day = _etf_v2_query_body()
    profile_multi_day["time"] = {
        "dimension": "EFFECTIVE_AT",
        "from": "2026-07-28",
        "to": "2026-07-29",
    }
    profile_without_exchange = _etf_v2_query_body()
    profile_without_exchange["filters"] = []
    unsupported_field = _etf_v2_query_body()
    unsupported_field["fields"] = ["symbol", "trackingError"]
    unsupported_filter = _etf_v2_query_body()
    unsupported_filter["filters"] = [
        {"field": "exchange", "operator": "EQ", "values": ["SSE"]},
        {"field": "fundType", "operator": "EQ", "values": ["ETF"]},
    ]
    unsupported_sort = _etf_v2_query_body()
    unsupported_sort["sort"] = [{"field": "listedOn", "direction": "ASC"}]
    nav_without_kind = _etf_v2_query_body()
    nav_without_kind["dataset"] = {
        "code": "fund.etf.nav.1d.reported",
        "schemaVersion": 2,
    }
    nav_without_kind["time"] = {
        "dimension": "TRADE_DATE",
        "from": "2026-07-29",
        "to": "2026-07-29",
    }
    nav_without_kind["fields"] = ["navDate", "nav"]
    nav_without_kind["filters"] = [
        {
            "field": "etfEntityRef",
            "operator": "EQ",
            "values": [str(uuid4())],
        }
    ]
    nav_without_kind["sort"] = [{"field": "navDate", "direction": "ASC"}]

    for body in (
        public_pit,
        operational_replay,
        known_version,
        methodology,
        fund_scope,
        silent_identity,
        oversized_profile_page,
        oversized_daily_range,
        profile_multi_day,
        profile_without_exchange,
        unsupported_field,
        unsupported_filter,
        unsupported_sort,
        nav_without_kind,
    ):
        client, headers, repository = _client()
        response = client.post("/internal/v1/market-data/query", headers=headers, json=body)

        assert response.status_code == 400
        assert response.json()["code"] == "validation-error"
        assert repository.queries == []


def test_etf_v2_http_boundary_accepts_current_with_optional_exact_version(
    configured_environment: None,
) -> None:
    """ETF v2 的 CURRENT 查询可携带精确 dataVersion，并继续进入 typed reader。"""
    del configured_environment
    client, headers, repository = _client()
    body = _etf_v2_query_body()
    selected_version = uuid4()
    body["selection"] = {
        "qualityStatuses": ["PASSED", "WARNED"],
        "dataVersion": str(selected_version),
    }

    response = client.post("/internal/v1/market-data/query", headers=headers, json=body)

    assert response.status_code == 200
    request = repository.queries[0][0]
    assert request.visibility == {"mode": "CURRENT"}
    assert request.selection["dataVersion"] == str(selected_version)


def test_market_data_request_id_accepts_only_the_contract_safe_alphabet(
    configured_environment: None,
) -> None:
    """内部接口只回显协议允许字符；空白或非法首字符必须替换为 UUID。"""
    del configured_environment
    client, headers, _repository = _client()
    valid_request_id = "api/etf:bar-1.test"
    valid = client.post(
        "/internal/v1/market-data/query",
        headers={**headers, "X-Request-Id": valid_request_id},
        json=_query_body(),
    )
    invalid = client.post(
        "/internal/v1/market-data/query",
        headers={**headers, "X-Request-Id": "ETF invalid"},
        json=_query_body(),
    )

    assert valid.headers["x-request-id"] == valid_request_id
    generated = invalid.headers["x-request-id"]
    assert UUID(generated).version == 4


def test_market_data_query_distinguishes_legal_empty_from_source_unavailable(
    configured_environment: None,
) -> None:
    """HTTP 空页必须保留 typed reader 的精确类别、稳定原因、时间和覆盖窗口。"""
    del configured_environment
    observed_at = datetime(2026, 7, 30, 8, tzinfo=UTC)
    cases = (
        (
            MarketDataAccessUnavailable(
                "legal empty",
                availability="EMPTY",
                reason_code="NO_MATCHING_FACTS",
                observed_at=observed_at,
                warnings=("legal_empty_observation",),
            ),
            "EMPTY",
            "EMPTY",
        ),
        (
            MarketDataAccessUnavailable(
                "source unavailable",
                availability="SOURCE_UNAVAILABLE",
                reason_code="PROVIDER_UNAVAILABLE",
                observed_at=observed_at,
                warnings=("source_unavailable",),
            ),
            "SOURCE_UNAVAILABLE",
            "UNKNOWN",
        ),
        (
            MarketDataAccessUnavailable(
                "unsupported NAV semantics",
                availability="CURRENTLY_UNSUPPORTED",
                reason_code="NAV_SEMANTICS_UNSUPPORTED_MONEY_MARKET",
                observed_at=observed_at,
                warnings=("currently_unsupported",),
            ),
            "CURRENTLY_UNSUPPORTED",
            "UNKNOWN",
        ),
    )

    for error, availability, pit_coverage in cases:
        client, headers, _repository = _client(FakeUnavailableMarketDataRepository(error))
        response = client.post(
            "/internal/v1/market-data/query",
            headers=headers,
            json=_query_body(),
        )
        payload = response.json()

        assert response.status_code == 200
        assert "x-data-version" not in response.headers
        assert payload["meta"]["availability"] == availability
        assert payload["meta"]["release"] == {
            "state": availability,
            "observedAt": "2026-07-30T08:00:00Z",
            "reasonCode": error.reason_code,
        }
        assert payload["meta"]["coverage"] == {
            "from": "2026-07-28",
            "to": "2026-07-29",
            "pitCoverage": pit_coverage,
            "gaps": [],
        }
        assert payload["meta"]["warnings"] == list(error.warnings)
        assert payload["records"] == []


def test_default_catalog_returns_successful_empty_page_when_p0_data_is_not_published(
    configured_environment: None,
) -> None:
    """来源未就绪时仍让 API 消费者得到可显示的空页，而不是跨服务 503。"""
    del configured_environment
    settings = load_settings()
    app = create_app(settings=settings, repository=cast(SectorMarketDataRepository, object()))
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {settings.internal_api_bearer_token.get_secret_value()}"}
    catalog = client.post(
        "/internal/v1/market-data/datasets/search",
        headers=headers,
        json={"availability": ["DISABLED"], "page": {"limit": 100}},
    )
    body = _query_body()
    body["filters"] = [{"field": "contractEntityRef", "operator": "EQ", "values": ["CFFEX.IF2608"]}]
    unavailable = client.post("/internal/v1/market-data/query", headers=headers, json=body)

    assert catalog.status_code == 200
    assert any(
        item["dataset"]["code"] == "derivative.bar.1d.reported"
        for item in catalog.json()["datasets"]
    )
    assert unavailable.status_code == 200
    assert "x-data-version" not in unavailable.headers
    assert unavailable.json()["meta"]["availability"] == "SOURCE_UNAVAILABLE"
    assert unavailable.json()["meta"]["release"] == {
        "state": "SOURCE_UNAVAILABLE",
        "observedAt": None,
        "reasonCode": "PUBLICATION_NOT_AVAILABLE",
    }
    assert unavailable.json()["records"] == []
