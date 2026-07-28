"""内部板块只读 HTTP 接口的认证、目录、ETag 和快照游标回归测试。"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import cast
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from service_data_sync.application.ports.sector_eod import RankedSectorEodQuote, SectorEodRepository
from service_data_sync.application.ports.sector_market_data import (
    DatasetPublication,
    SectorMarketDataRepository,
    StoredSector,
)
from service_data_sync.bootstrap.settings import load_settings
from service_data_sync.domain.sector import (
    SectorBar,
    SectorEodFinality,
    SectorEodQuote,
    SectorEodSnapshot,
    SectorEodSort,
    SectorIdentifier,
    SectorPeriod,
    SectorScheme,
    SortOrder,
)
from service_data_sync.interfaces.internal_sector_api import create_app


class FakeRepository:
    """提供内部 API 所需的已发布目录和各物理周期行情，不连接数据库。"""

    def __init__(self) -> None:
        """构造两个 ACTIVE 行业板块与可替换的目录、K 线发布版本。"""
        self.first = _sector("BK0001", "银行")
        self.second = _sector("BK0002", "证券")
        self.catalog_publication = DatasetPublication(uuid4(), datetime(2026, 7, 1, tzinfo=UTC))
        self.bar_publication = DatasetPublication(uuid4(), datetime(2026, 7, 2, tzinfo=UTC))
        self.bars = (_bar(date(2026, 6, 30)), _bar(date(2026, 7, 1)))

    def get_current_publication(
        self, *, dataset: str, partition_key: str
    ) -> DatasetPublication | None:
        """按目录体系或指定代码周期返回确定性当前发布。"""
        if dataset == "sector.catalog.raw" and partition_key == "eastmoney.industry":
            return self.catalog_publication
        if (
            dataset == SectorPeriod.DAY_1.capability
            and partition_key == "eastmoney.industry:BK0001"
        ):
            return self.bar_publication
        return None

    def list_active_sectors(
        self,
        *,
        scheme: SectorScheme,
        query: str | None,
        after_code: str | None,
        after_sector_id: UUID | None,
        limit: int,
    ) -> tuple[StoredSector, ...]:
        """模拟稳定排序、前缀查询与代码 UUID 游标。"""
        assert scheme is SectorScheme.EASTMONEY_INDUSTRY
        assert limit <= 101
        rows = (self.first, self.second)
        if query is not None:
            rows = tuple(
                row
                for row in rows
                if row.identifier.code.startswith(query) or (row.name or "").startswith(query)
            )
        if after_code is not None:
            assert after_sector_id is not None
            rows = tuple(
                row
                for row in rows
                if row.identifier.code > after_code
                or (row.identifier.code == after_code and row.sector_id > after_sector_id)
            )
        return rows[:limit]

    def get_sector_by_identifier(self, identifier: SectorIdentifier) -> StoredSector | None:
        """按稳定身份返回目录已激活板块或空结果。"""
        return next(
            (sector for sector in (self.first, self.second) if sector.identifier == identifier),
            None,
        )

    def list_bars(
        self,
        *,
        sector_id: UUID,
        period: SectorPeriod,
        start: date,
        end: date,
    ) -> tuple[tuple[SectorBar, int, bool], ...]:
        """仅为首个板块的日线返回包含端范围内上游行。"""
        assert sector_id == self.first.sector_id
        assert period is SectorPeriod.DAY_1
        return tuple((bar, 1, True) for bar in self.bars if start <= bar.period_end <= end)


class FakeEodRepository:
    """提供 EOD 内部 API 所需的不可变快照和确定性排行，不连接数据库。"""

    def __init__(self) -> None:
        """构造两条同一行业快照报价，用于分页、ETag 和 cursor 回归。"""
        self.snapshot = SectorEodSnapshot(
            snapshot_id=uuid4(),
            data_version=uuid4(),
            scheme=SectorScheme.EASTMONEY_INDUSTRY,
            trade_date=date(2026, 7, 27),
            source_cutoff_at=datetime(2026, 7, 27, 8, 15, tzinfo=UTC),
            observed_at=datetime(2026, 7, 27, 8, 20, tzinfo=UTC),
            finality=SectorEodFinality.POST_CLOSE_OBSERVATION,
            quality_status="passed",
            published_at=datetime(2026, 7, 27, 8, 21, tzinfo=UTC),
        )
        self.rows = (
            _eod_quote("BK0001", "银行", Decimal("3"), 1, 1),
            _eod_quote("BK0002", "证券", Decimal("2"), 2, 2),
        )

    def get_published_snapshot(
        self, *, scheme: SectorScheme, trade_date: date | None
    ) -> SectorEodSnapshot | None:
        """只在请求行业且 latest 或精确日期匹配时返回当前快照。"""
        if scheme is not SectorScheme.EASTMONEY_INDUSTRY:
            return None
        if trade_date is not None and trade_date != self.snapshot.trade_date:
            return None
        return self.snapshot

    def list_ranked_quotes(
        self,
        *,
        snapshot_id: UUID,
        sort: SectorEodSort,
        order: SortOrder,
        after_position: int | None,
        limit: int,
    ) -> tuple[RankedSectorEodQuote, ...]:
        """模拟同一 dataVersion 中按 position 续页的稳定排行结果。"""
        assert snapshot_id == self.snapshot.snapshot_id
        assert sort is SectorEodSort.CHANGE_PERCENT
        assert order is SortOrder.DESC
        return tuple(row for row in self.rows if row.position > (after_position or 0))[:limit]

    def get_snapshot_quote(
        self, *, snapshot_id: UUID, identifier: SectorIdentifier
    ) -> RankedSectorEodQuote | None:
        """按快照身份和 scheme/code 返回单板块报价。"""
        assert snapshot_id == self.snapshot.snapshot_id
        return next((row for row in self.rows if row.quote.identifier == identifier), None)


def test_internal_api_requires_service_bearer(configured_environment) -> None:
    """所有 `/internal` 板块路由都应拒绝缺失或错误的服务凭据。"""
    del configured_environment
    client = TestClient(
        create_app(
            settings=load_settings(), repository=cast(SectorMarketDataRepository, FakeRepository())
        )
    )

    response = client.get("/internal/v1/sectors?scheme=eastmoney.industry")

    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "unauthorized"


def test_internal_api_pages_catalog_and_honors_representation_etag(configured_environment) -> None:
    """目录页应仅返回 ACTIVE 名称、绑定游标筛选条件并支持精确 ETag 复验。"""
    del configured_environment
    settings = load_settings()
    client = TestClient(
        create_app(settings=settings, repository=cast(SectorMarketDataRepository, FakeRepository()))
    )
    headers = {"Authorization": f"Bearer {settings.internal_api_bearer_token.get_secret_value()}"}

    first = client.get("/internal/v1/sectors?scheme=eastmoney.industry&limit=1", headers=headers)

    assert first.status_code == 200
    assert first.json()["items"][0]["name"] == "银行"
    assert first.json()["nextCursor"] is not None
    assert first.headers["cache-control"] == "private, max-age=0, must-revalidate"

    second = client.get(
        "/internal/v1/sectors?scheme=eastmoney.industry&limit=1&cursor="
        f"{first.json()['nextCursor']}",
        headers=headers,
    )
    cached = client.get(
        "/internal/v1/sectors?scheme=eastmoney.industry&limit=1",
        headers={**headers, "If-None-Match": first.headers["etag"]},
    )

    assert second.status_code == 200
    assert second.json()["items"][0]["name"] == "证券"
    assert cached.status_code == 304


def test_internal_api_reads_direct_bars_and_rejects_expired_snapshot_cursor(
    configured_environment,
) -> None:
    """K 线页应保留原生周期间隔，并在当前发布替代时返回快照冲突。"""
    del configured_environment
    settings = load_settings()
    repository = FakeRepository()
    client = TestClient(
        create_app(settings=settings, repository=cast(SectorMarketDataRepository, repository))
    )
    headers = {"Authorization": f"Bearer {settings.internal_api_bearer_token.get_secret_value()}"}
    endpoint = (
        "/internal/v1/sectors/eastmoney.industry/BK0001/bars?period=1d&start=2026-06-01"
        "&end=2026-07-02&limit=1"
    )

    first = client.get(endpoint, headers=headers)
    sector = client.get("/internal/v1/sectors/eastmoney.industry/BK0001", headers=headers)
    repository.bar_publication = DatasetPublication(uuid4(), datetime(2026, 7, 3, tzinfo=UTC))
    expired = client.get(f"{endpoint}&cursor={first.json()['nextCursor']}", headers=headers)
    invalid_range = client.get(
        "/internal/v1/sectors/eastmoney.industry/BK0001/bars?period=1d&start=2026-07-02"
        "&end=2026-06-01",
        headers=headers,
    )

    assert first.status_code == 200
    assert first.json()["items"][0]["volumeUnit"] == "provider_native"
    assert first.json()["nextCursor"] is not None
    assert sector.status_code == 200
    assert sector.json()["sectorId"] == str(repository.first.sector_id)
    assert expired.status_code == 409
    assert expired.json()["code"] == "snapshot-expired"
    assert invalid_range.status_code == 400


def test_internal_api_rejects_unpublished_or_malformed_read_requests(
    configured_environment,
) -> None:
    """未发布目录、错误枚举和损坏游标必须转化为稳定且无内部细节的问题响应。"""
    del configured_environment
    settings = load_settings()
    repository = FakeRepository()
    client = TestClient(
        create_app(settings=settings, repository=cast(SectorMarketDataRepository, repository))
    )
    headers = {"Authorization": f"Bearer {settings.internal_api_bearer_token.get_secret_value()}"}

    bad_scheme = client.get("/internal/v1/sectors?scheme=unknown", headers=headers)
    bad_cursor = client.get(
        "/internal/v1/sectors?scheme=eastmoney.industry&cursor=not-a-cursor", headers=headers
    )
    blank_query = client.get(
        "/internal/v1/sectors?scheme=eastmoney.industry&query=%20", headers=headers
    )
    blank_code = client.get("/internal/v1/sectors/eastmoney.industry/%20", headers=headers)
    bad_period = client.get(
        "/internal/v1/sectors/eastmoney.industry/BK0001/bars?period=2d&start=2026-06-01"
        "&end=2026-07-01",
        headers=headers,
    )
    unpublished_bars = client.get(
        "/internal/v1/sectors/eastmoney.industry/BK0002/bars?period=1d&start=2026-06-01"
        "&end=2026-07-01",
        headers=headers,
    )
    repository.catalog_publication = None  # type: ignore[assignment]
    unpublished_catalog = client.get(
        "/internal/v1/sectors?scheme=eastmoney.industry", headers=headers
    )

    assert bad_scheme.status_code == 400
    assert bad_cursor.status_code == 400
    assert blank_query.status_code == 400
    assert blank_code.status_code == 400
    assert bad_period.status_code == 400
    assert unpublished_bars.status_code == 404
    assert unpublished_catalog.status_code == 503


def test_internal_api_reads_eod_page_with_bound_cursor_and_internal_identifier(
    configured_environment,
) -> None:
    """EOD 路由应返回内部 UUID、null-safe 排行字段，并拒绝已替代版本的 cursor。"""
    del configured_environment
    settings = load_settings()
    eod_repository = FakeEodRepository()
    client = TestClient(
        create_app(
            settings=settings,
            repository=cast(SectorMarketDataRepository, FakeRepository()),
            eod_repository=cast(SectorEodRepository, eod_repository),
        )
    )
    headers = {"Authorization": f"Bearer {settings.internal_api_bearer_token.get_secret_value()}"}
    endpoint = "/internal/v1/sectors/eod-snapshots?scheme=eastmoney.industry&limit=1"

    first = client.get(endpoint, headers=headers)
    cached = client.get(endpoint, headers={**headers, "If-None-Match": first.headers["etag"]})
    single = client.get(
        "/internal/v1/sectors/eastmoney.industry/BK0001/eod-snapshot",
        headers=headers,
    )
    invalid_sort = client.get(
        "/internal/v1/sectors/eod-snapshots?scheme=eastmoney.industry&sort=unknown",
        headers=headers,
    )
    invalid_scheme = client.get(
        "/internal/v1/sectors/eod-snapshots?scheme=unknown",
        headers=headers,
    )
    invalid_order = client.get(
        "/internal/v1/sectors/eod-snapshots?scheme=eastmoney.industry&order=unknown",
        headers=headers,
    )
    eod_repository.snapshot = replace(eod_repository.snapshot, data_version=uuid4())
    expired = client.get(f"{endpoint}&cursor={first.json()['nextCursor']}", headers=headers)
    exact_missing = client.get(
        "/internal/v1/sectors/eastmoney.industry/BK0001/eod-snapshot?asOf=2026-07-26",
        headers=headers,
    )

    assert first.status_code == 200
    assert first.json()["items"][0]["sectorId"] == str(eod_repository.rows[0].sector_id)
    assert first.json()["finality"] == "post_close_observation"
    assert cached.status_code == 304
    assert single.status_code == 200
    assert single.json()["code"] == "BK0001"
    assert "rank" not in single.json()
    assert invalid_sort.status_code == 400
    assert invalid_scheme.status_code == 400
    assert invalid_order.status_code == 400
    assert expired.status_code == 409
    assert expired.json()["code"] == "snapshot-expired"
    assert exact_missing.status_code == 404


def _sector(code: str, name: str) -> StoredSector:
    """构造目录已确认的行业板块身份。"""
    return StoredSector(
        sector_key=1,
        sector_id=uuid4(),
        identifier=SectorIdentifier(SectorScheme.EASTMONEY_INDUSTRY, code),
        name=name,
        status="ACTIVE",
    )


def _bar(period_end: date) -> SectorBar:
    """构造一条符合领域约束的直接上游日线。"""
    return SectorBar(
        period_end=period_end,
        open_price=Decimal("10"),
        high_price=Decimal("11"),
        low_price=Decimal("9"),
        close_price=Decimal("10.5"),
        volume_value=Decimal("1000"),
        volume_unit="provider_native",
        amount_cny=Decimal("10500"),
        amplitude_percent=Decimal("20"),
        change_percent=Decimal("5"),
        change_amount=Decimal("0.5"),
        turnover_percent=Decimal("3"),
    )


def _eod_quote(
    code: str, name: str, change_percent: Decimal, rank: int, position: int
) -> RankedSectorEodQuote:
    """构造一条同快照 EOD 排行结果，保留内部 UUID 仅供服务间测试断言。"""
    return RankedSectorEodQuote(
        sector_id=uuid4(),
        quote=SectorEodQuote(
            identifier=SectorIdentifier(SectorScheme.EASTMONEY_INDUSTRY, code),
            name=name,
            latest_value=Decimal("1000"),
            change_value=Decimal("10"),
            change_percent=change_percent,
            market_value=Decimal("1000000"),
            turnover_percent=Decimal("3"),
            advancers=10,
            decliners=3,
            leader_name="示例证券",
            leader_change_percent=Decimal("5"),
        ),
        rank=rank,
        position=position,
    )
