"""证券主数据内部 API 的认证、双时间、游标与 ETag 回归测试。"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import cast
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from service_data_sync.application.ports.equity_master_read import (
    EquityMasterPublication,
    EquityMasterReadRepository,
    EquityMasterReadUnavailable,
    EquityPublicationComponent,
    EquitySourceAttribution,
    PublicationScope,
    StoredEquityInstrument,
    StoredListingStatusPeriod,
    TemporalEquityIdentifier,
    TemporalEquityListing,
    TemporalEquityName,
)
from service_data_sync.application.ports.sector_market_data import SectorMarketDataRepository
from service_data_sync.bootstrap.settings import load_settings
from service_data_sync.domain.equity import Exchange
from service_data_sync.interfaces.internal_sector_api import create_app

_VERSION = UUID("10000000-0000-4000-8000-000000000001")
_FIRST_ID = UUID("20000000-0000-4000-8000-000000000001")
_SECOND_ID = UUID("20000000-0000-4000-8000-000000000002")
_CUTOFF = datetime(2026, 7, 2, 12, tzinfo=UTC)


class FakeEquityRepository:
    """提供确定性发布、证券投影和生命周期历史，不连接 PostgreSQL。"""

    def __init__(self) -> None:
        """初始化两只证券、两条知识修订，并开放故障注入开关。"""
        self.data_version = _VERSION
        self.publication_exists = True
        self.read_unavailable = False
        self.unavailable_operation: str | None = None
        self.instruments = (
            _instrument(
                security_id=1,
                instrument_id=_FIRST_ID,
                symbol="600000",
                name="浦发银行",
            ),
            _instrument(
                security_id=2,
                instrument_id=_SECOND_ID,
                symbol="600519",
                name="贵州茅台",
            ),
        )
        self.find_rows: tuple[StoredEquityInstrument, ...] | None = None
        self.history = (
            StoredListingStatusPeriod(
                version_id=UUID("30000000-0000-4000-8000-000000000001"),
                status="LISTED",
                effective_from=date(2001, 8, 27),
                effective_to=None,
                effective_date_precision="OFFICIAL_DATE",
                known_from=datetime(2026, 6, 1, tzinfo=UTC),
                known_to=datetime(2026, 6, 20, tzinfo=UTC),
                observed_at=datetime(2026, 6, 1, 1, tzinfo=UTC),
                evidence_kind="CATALOG",
                source=_catalog_source(),
            ),
            StoredListingStatusPeriod(
                version_id=UUID("30000000-0000-4000-8000-000000000002"),
                status="LISTED",
                effective_from=date(2001, 8, 27),
                effective_to=None,
                effective_date_precision="OFFICIAL_DATE",
                known_from=datetime(2026, 6, 20, tzinfo=UTC),
                known_to=None,
                observed_at=datetime(2026, 6, 20, 1, tzinfo=UTC),
                evidence_kind="EXPLICIT_LISTING",
                source=_lifecycle_source(),
            ),
        )
        self.find_identifier_as_of_calls: list[date | None] = []

    def get_current_publication(
        self, *, exchange: Exchange | None
    ) -> EquityMasterPublication | None:
        """按单所或聚合请求返回同一确定性版本，并支持缺发布和依赖故障。"""
        self._raise_if_unavailable("publication")
        if not self.publication_exists:
            return None
        scope = cast(
            PublicationScope,
            "CN_A_STABLE" if exchange is None else exchange.value,
        )
        return EquityMasterPublication(
            data_version=self.data_version,
            published_at=datetime(2026, 7, 2, 12, 5, tzinfo=UTC),
            effective_as_of=date(2026, 7, 1),
            publication_scope=scope,
            components=_publication_components(exchange),
        )

    def list_instruments(
        self,
        *,
        data_version: UUID,
        exchange: Exchange | None,
        statuses: tuple[str, ...],
        query: str | None,
        as_of: date,
        known_at: datetime,
        after_exchange: Exchange | None,
        after_symbol: str | None,
        after_instrument_id: UUID | None,
        limit: int,
    ) -> tuple[StoredEquityInstrument, ...]:
        """模拟筛选、稳定复合游标和接口层多取一行。"""
        self._raise_if_unavailable("list")
        assert data_version == self.data_version
        assert as_of <= date(2026, 7, 1)
        assert known_at <= _CUTOFF
        rows = self.instruments
        if exchange is not None:
            rows = tuple(row for row in rows if row.identifier.exchange is exchange)
        if statuses:
            rows = tuple(row for row in rows if row.listing.status in statuses)
        if query is not None:
            rows = tuple(
                row
                for row in rows
                if row.identifier.symbol.startswith(query) or row.name.value.startswith(query)
            )
        if after_exchange is not None:
            assert after_symbol is not None
            assert after_instrument_id is not None
            position = (after_exchange.value, after_symbol, after_instrument_id)
            rows = tuple(
                row
                for row in rows
                if (
                    row.identifier.exchange.value,
                    row.identifier.symbol,
                    row.instrument_id,
                )
                > position
            )
        return rows[:limit]

    def find_instruments(
        self,
        *,
        data_version: UUID,
        exchange: Exchange,
        symbol: str,
        identifier_as_of: date | None,
        projection_as_of: date,
        known_at: datetime,
        limit: int = 2,
    ) -> tuple[StoredEquityInstrument, ...]:
        """记录身份选择日期，并返回可注入的空、唯一或冲突结果。"""
        self._raise_if_unavailable("find")
        assert data_version == self.data_version
        assert projection_as_of <= date(2026, 7, 1)
        assert known_at <= _CUTOFF
        self.find_identifier_as_of_calls.append(identifier_as_of)
        if self.find_rows is not None:
            return self.find_rows[:limit]
        return tuple(
            row
            for row in self.instruments
            if row.identifier.exchange is exchange and row.identifier.symbol == symbol
        )[:limit]

    def list_listing_status_history(
        self,
        *,
        data_version: UUID,
        exchange: Exchange,
        security_id: int,
        known_at: datetime,
        effective_from: date | None,
        effective_to: date | None,
        after_effective_from: date | None,
        after_known_from: datetime | None,
        after_version_id: UUID | None,
        limit: int,
    ) -> tuple[StoredListingStatusPeriod, ...]:
        """模拟知识修订、有效区间相交和三键历史游标。"""
        self._raise_if_unavailable("history")
        assert data_version == self.data_version
        assert exchange is Exchange.SSE
        assert security_id > 0
        rows = tuple(row for row in self.history if row.known_from <= known_at)
        if effective_from is not None:
            rows = tuple(
                row for row in rows if row.effective_to is None or row.effective_to > effective_from
            )
        if effective_to is not None:
            rows = tuple(row for row in rows if row.effective_from < effective_to)
        if after_effective_from is not None:
            assert after_known_from is not None
            assert after_version_id is not None
            position = (after_effective_from, after_known_from, after_version_id)
            rows = tuple(
                row
                for row in rows
                if (row.effective_from, row.known_from, row.version_id) > position
            )
        return rows[:limit]

    def _raise_if_unavailable(self, operation: str) -> None:
        """在测试要求时模拟 canonical 发布存储不可读取。"""
        if self.read_unavailable or self.unavailable_operation == operation:
            raise EquityMasterReadUnavailable("测试依赖不可用")


def test_equity_routes_require_exact_service_bearer(configured_environment) -> None:
    """三条证券路由都必须拒绝缺失或错误的内部服务凭据。"""
    del configured_environment
    client, _headers = _client(FakeEquityRepository())

    missing = client.get("/internal/v1/equities")
    wrong = client.get(
        "/internal/v1/equities/SSE/600519",
        headers={"Authorization": "Bearer wrong"},
    )

    assert missing.status_code == 401
    assert missing.json()["code"] == "unauthorized"
    assert wrong.status_code == 401


def test_equity_list_pages_with_signed_cursor_etag_and_snapshot_conflict(
    configured_environment,
) -> None:
    """目录页应绑定筛选和版本、支持 ETag，并区分坏游标与旧快照。"""
    del configured_environment
    repository = FakeEquityRepository()
    client, headers = _client(repository)
    endpoint = "/internal/v1/equities?status=LISTED&limit=1"

    first = client.get(endpoint, headers=headers)
    cursor = first.json()["nextCursor"]
    second = client.get(f"{endpoint}&cursor={cursor}", headers=headers)
    cached = client.get(
        endpoint,
        headers={**headers, "If-None-Match": first.headers["etag"]},
    )
    cross_scope = client.get(
        f"{endpoint}&exchange=SSE&cursor={cursor}",
        headers=headers,
    )
    tampered_cursor = f"{cursor[:-3]}{'A' if cursor[-3] != 'A' else 'B'}{cursor[-2:]}"
    tampered = client.get(f"{endpoint}&cursor={tampered_cursor}", headers=headers)
    repository.data_version = uuid4()
    expired = client.get(f"{endpoint}&cursor={cursor}", headers=headers)

    assert first.status_code == 200
    assert first.json()["items"][0]["identifier"]["symbol"] == "600000"
    assert first.json()["publicationScope"] == "CN_A_STABLE"
    assert first.json()["effectiveAsOf"] == "2026-07-01"
    assert first.json()["requestedKnownAt"] == "2026-07-02T12:00:00Z"
    assert len(first.json()["componentPublications"]) == 6
    assert first.json()["items"][0]["listing"]["evidenceKind"] == "EXPLICIT_LISTING"
    assert first.json()["items"][0]["listing"]["source"]["sourceBatchId"] == str(
        UUID("50000000-0000-4000-8000-000000000002")
    )
    assert first.headers["x-data-version"] == str(_VERSION)
    assert first.headers["cache-control"] == "private, max-age=0, must-revalidate"
    assert second.status_code == 200
    assert second.json()["items"][0]["identifier"]["symbol"] == "600519"
    assert cached.status_code == 304
    assert cached.content == b""
    assert cross_scope.status_code == 400
    assert tampered.status_code == 400
    assert expired.status_code == 409
    assert expired.json()["code"] == "snapshot-expired"


def test_equity_detail_resolves_explicit_or_current_identity_and_errors(
    configured_environment,
) -> None:
    """详情应传递身份选择时间、隐藏数据库键，并稳定区分 404 与 409。"""
    del configured_environment
    repository = FakeEquityRepository()
    client, headers = _client(repository)
    endpoint = "/internal/v1/equities/SSE/600519?asOf=2026-06-30&knownAt=2026-07-02T12:00:00Z"

    detail = client.get(endpoint, headers=headers)
    cached = client.get(
        endpoint,
        headers={**headers, "If-None-Match": detail.headers["etag"]},
    )
    current = client.get("/internal/v1/equities/SSE/600519", headers=headers)
    repository.find_rows = ()
    missing = client.get("/internal/v1/equities/SSE/600519", headers=headers)
    repository.find_rows = repository.instruments
    conflict = client.get("/internal/v1/equities/SSE/600519", headers=headers)

    assert detail.status_code == 200
    assert detail.json()["instrumentId"] == str(_SECOND_ID)
    assert "securityId" not in detail.json()
    assert detail.json()["name"]["value"] == "贵州茅台"
    assert detail.json()["listing"]["qualityStatus"] == "passed"
    assert repository.find_identifier_as_of_calls[0] == date(2026, 6, 30)
    assert repository.find_identifier_as_of_calls[2] is None
    assert cached.status_code == 304
    assert current.status_code == 200
    assert missing.status_code == 404
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "identity-resolution-conflict"


def test_listing_history_pages_revisions_and_allows_empty_filter(
    configured_environment,
) -> None:
    """生命周期历史应按三键分页、保留知识修订，并允许合法空页。"""
    del configured_environment
    repository = FakeEquityRepository()
    client, headers = _client(repository)
    endpoint = "/internal/v1/equities/SSE/600519/listing-status-history?limit=1"

    first = client.get(endpoint, headers=headers)
    cursor = first.json()["nextCursor"]
    second = client.get(f"{endpoint}&cursor={cursor}", headers=headers)
    cached = client.get(
        endpoint,
        headers={**headers, "If-None-Match": first.headers["etag"]},
    )
    mismatched = client.get(
        f"{endpoint}&effectiveFrom=2026-01-01&cursor={cursor}",
        headers=headers,
    )
    empty = client.get(
        "/internal/v1/equities/SSE/600519/listing-status-history"
        "?effectiveFrom=1990-01-01&effectiveTo=1991-01-01",
        headers=headers,
    )
    invalid_range = client.get(
        "/internal/v1/equities/SSE/600519/listing-status-history"
        "?effectiveFrom=2026-02-01&effectiveTo=2026-02-01",
        headers=headers,
    )

    assert first.status_code == 200
    assert first.json()["items"][0]["knownTo"] == "2026-06-20T00:00:00Z"
    assert second.status_code == 200
    assert second.json()["items"][0]["knownTo"] is None
    assert cached.status_code == 304
    assert mismatched.status_code == 400
    assert empty.status_code == 200
    assert empty.json()["items"] == []
    assert invalid_range.status_code == 400


def test_equity_routes_validate_filters_times_and_dependency_state(
    configured_environment,
) -> None:
    """非法筛选和越界双时间应为 400，无发布或存储失败应 fail-closed 为 503。"""
    del configured_environment
    repository = FakeEquityRepository()
    client, headers = _client(repository)

    duplicate_status = client.get(
        "/internal/v1/equities?status=LISTED&status=LISTED",
        headers=headers,
    )
    blank_query = client.get("/internal/v1/equities?query=%20", headers=headers)
    bad_exchange = client.get("/internal/v1/equities?exchange=UNKNOWN", headers=headers)
    future_slice = client.get("/internal/v1/equities?asOf=2026-07-02", headers=headers)
    naive_knowledge = client.get(
        "/internal/v1/equities?knownAt=2026-07-01T12:00:00",
        headers=headers,
    )
    late_knowledge = client.get(
        "/internal/v1/equities?knownAt=2026-07-03T00:00:00Z",
        headers=headers,
    )
    repository.publication_exists = False
    unpublished = client.get("/internal/v1/equities", headers=headers)
    repository.publication_exists = True
    repository.read_unavailable = True
    unavailable = client.get("/internal/v1/equities", headers=headers)
    repository.read_unavailable = False
    repository.unavailable_operation = "list"
    broken_list = client.get("/internal/v1/equities", headers=headers)
    repository.unavailable_operation = "find"
    broken_detail = client.get("/internal/v1/equities/SSE/600519", headers=headers)
    repository.unavailable_operation = "history"
    broken_history = client.get(
        "/internal/v1/equities/SSE/600519/listing-status-history",
        headers=headers,
    )

    assert duplicate_status.status_code == 400
    assert blank_query.status_code == 400
    assert bad_exchange.status_code == 400
    assert future_slice.status_code == 400
    assert naive_knowledge.status_code == 400
    assert late_knowledge.status_code == 400
    assert unpublished.status_code == 503
    assert unpublished.headers["retry-after"] == "5"
    assert unpublished.json()["code"] == "publication-unavailable"
    assert unavailable.status_code == 503
    assert unavailable.json()["code"] == "dependency-unavailable"
    assert broken_list.status_code == 503
    assert broken_detail.status_code == 503
    assert broken_history.status_code == 503


def _client(
    repository: FakeEquityRepository,
) -> tuple[TestClient, dict[str, str]]:
    """构造只挂载测试证券仓储的共享应用与合法认证头。"""
    settings = load_settings()
    app = create_app(
        settings=settings,
        repository=cast(SectorMarketDataRepository, object()),
        equity_repository=cast(EquityMasterReadRepository, repository),
    )
    credential = settings.internal_api_bearer_token.get_secret_value()
    return TestClient(app), {"Authorization": f"Bearer {credential}"}


def _instrument(
    *,
    security_id: int,
    instrument_id: UUID,
    symbol: str,
    name: str,
) -> StoredEquityInstrument:
    """构造字段齐全且双时间可序列化的已确认证券投影。"""
    return StoredEquityInstrument(
        security_id=security_id,
        instrument_id=instrument_id,
        identifier=TemporalEquityIdentifier(
            exchange=Exchange.SSE,
            symbol=symbol,
            effective_from=date(2001, 8, 27),
            effective_to=None,
            date_precision="OFFICIAL_DATE",
            known_from=datetime(2026, 6, 1, tzinfo=UTC),
            observed_at=datetime(2026, 6, 1, 1, tzinfo=UTC),
            source=_catalog_source(),
        ),
        name=TemporalEquityName(
            value=name,
            effective_from=date(2001, 8, 27),
            effective_to=None,
            date_precision="OFFICIAL_DATE",
            known_from=datetime(2026, 6, 1, tzinfo=UTC),
            observed_at=datetime(2026, 6, 1, 1, tzinfo=UTC),
            source=_catalog_source(),
        ),
        listing=TemporalEquityListing(
            status="LISTED",
            listed_on=date(2001, 8, 27),
            delisted_on=None,
            effective_from=date(2001, 8, 27),
            effective_to=None,
            date_precision="OFFICIAL_DATE",
            known_from=datetime(2026, 6, 1, tzinfo=UTC),
            observed_at=datetime(2026, 6, 1, 1, tzinfo=UTC),
            evidence_kind="EXPLICIT_LISTING",
            source=_lifecycle_source(),
        ),
    )


def _publication_components(
    exchange: Exchange | None,
) -> tuple[EquityPublicationComponent, ...]:
    """构造目录与生命周期独立 cutoff 的 resolved 输入清单。"""
    exchanges = (exchange,) if exchange is not None else tuple(Exchange)
    return tuple(
        EquityPublicationComponent(
            component_key=(kind if exchange is not None else f"{current_exchange.value}.{kind}"),
            dataset=("equity.master.catalog" if kind == "catalog" else "equity.lifecycle.explicit"),
            partition_key=current_exchange.value,
            data_version=UUID(f"40000000-0000-4000-8000-0000000000{ordinal:02d}"),
            published_at=datetime(2026, 7, 2, 12, 5, tzinfo=UTC),
            effective_as_of=date(2026, 7, 1),
            knowledge_cutoff=(
                datetime(2026, 7, 2, 11, tzinfo=UTC) if kind == "catalog" else _CUTOFF
            ),
            quality_status="passed",
        )
        for ordinal, (current_exchange, kind) in enumerate(
            (
                (current_exchange, kind)
                for current_exchange in exchanges
                for kind in ("catalog", "lifecycle")
            ),
            start=1,
        )
    )


def _catalog_source() -> EquitySourceAttribution:
    """构造脱敏目录来源锚点。"""
    return EquitySourceAttribution(
        source_batch_id=UUID("50000000-0000-4000-8000-000000000001"),
        provider_id="catalog-provider",
        upstream_source="eastmoney.equity-catalog",
    )


def _lifecycle_source() -> EquitySourceAttribution:
    """构造脱敏交易所生命周期来源锚点。"""
    return EquitySourceAttribution(
        source_batch_id=UUID("50000000-0000-4000-8000-000000000002"),
        provider_id="lifecycle-provider",
        upstream_source="sse.lifecycle",
    )
