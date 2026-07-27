"""板块成分 internal API 的 release、游标、ETag 与身份投影回归测试。"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import cast
from uuid import uuid4

from fastapi.testclient import TestClient

from service_data_sync.application.ports.sector_market_data import (
    SectorMarketDataRepository,
    StoredSector,
)
from service_data_sync.application.ports.sector_membership import (
    SectorMembershipRepository,
    StoredEquityMembership,
    StoredMembershipConstituent,
    StoredMembershipEquity,
    StoredSectorMembershipRelease,
)
from service_data_sync.bootstrap.settings import load_settings
from service_data_sync.domain.equity import Exchange
from service_data_sync.domain.sector import SectorIdentifier, SectorScheme
from service_data_sync.interfaces.internal_sector_api import create_app


class FakeMembershipRepository:
    """提供一个 immutable release 的双向板块成分读取替身。"""

    def __init__(self) -> None:
        """构造行业 release、板块、证券和两条稳定排序成员。"""
        self.sector = StoredSector(
            sector_key=1,
            sector_id=uuid4(),
            identifier=SectorIdentifier(SectorScheme.EASTMONEY_INDUSTRY, "BK0475"),
            name="证券",
            status="ACTIVE",
        )
        self.release = StoredSectorMembershipRelease(
            release_id=uuid4(),
            scheme=SectorScheme.EASTMONEY_INDUSTRY,
            requested_as_of=None,
            resolved_as_of=datetime(2026, 7, 27, 10, tzinfo=UTC),
            coverage_start=datetime(2026, 7, 20, 10, tzinfo=UTC),
            data_version=uuid4(),
            quality_status="passed",
            carried_forward_sector_count=0,
            published_at=datetime(2026, 7, 27, 11, tzinfo=UTC),
        )
        self.equity = StoredMembershipEquity(
            instrument_id=uuid4(),
            exchange=Exchange.SSE,
            symbol="600000",
            name="浦发银行",
            listing_status="LISTED",
        )
        self.constituents = (
            StoredMembershipConstituent(
                instrument_id=self.equity.instrument_id,
                exchange=Exchange.SSE,
                symbol="600000",
                name="浦发银行",
                listing_status="LISTED",
                observed_from=datetime(2026, 7, 20, 10, tzinfo=UTC),
                observed_to=None,
            ),
            StoredMembershipConstituent(
                instrument_id=uuid4(),
                exchange=Exchange.SZSE,
                symbol="000001",
                name="平安银行",
                listing_status="LISTED",
                observed_from=datetime(2026, 7, 20, 10, tzinfo=UTC),
                observed_to=None,
            ),
        )

    def get_release(
        self, *, scheme: SectorScheme, as_of: datetime | None
    ) -> StoredSectorMembershipRelease | None:
        """只为行业返回当前固定 release，并回显请求时刻。"""
        if scheme is not SectorScheme.EASTMONEY_INDUSTRY:
            return None
        return replace(self.release, requested_as_of=as_of)

    def get_release_sector(
        self, *, release_id: object, identifier: SectorIdentifier
    ) -> tuple[StoredSector, datetime, bool] | None:
        """按固定 release 返回唯一板块及其新鲜快照元数据。"""
        if release_id != self.release.release_id or identifier != self.sector.identifier:
            return None
        return self.sector, datetime(2026, 7, 27, 10, tzinfo=UTC), False

    def list_constituents(
        self,
        *,
        release_id: object,
        identifier: SectorIdentifier,
        after_exchange: Exchange | None,
        after_symbol: str | None,
        limit: int,
    ) -> tuple[StoredMembershipConstituent, ...]:
        """按交易所代码游标返回固定成分，模拟稳定数据库排序。"""
        assert release_id == self.release.release_id
        assert identifier == self.sector.identifier
        rows = self.constituents
        if after_exchange is not None and after_symbol is not None:
            rows = tuple(
                row
                for row in rows
                if (row.exchange.value, row.symbol) > (after_exchange.value, after_symbol)
            )
        return rows[:limit]

    def get_release_equity(
        self, *, release_id: object, exchange: Exchange, symbol: str
    ) -> StoredMembershipEquity | None:
        """仅解析 release 中已确认的证券身份。"""
        if release_id == self.release.release_id and (exchange, symbol) == (
            self.equity.exchange,
            self.equity.symbol,
        ):
            return self.equity
        return None

    def list_equity_memberships(
        self,
        *,
        release_id: object,
        instrument_id: object,
        after_sector_code: str | None,
        limit: int,
    ) -> tuple[StoredEquityMembership, ...]:
        """返回反向归属，已知证券无后续页时保留空 cursor。"""
        if release_id != self.release.release_id or instrument_id != self.equity.instrument_id:
            return ()
        if after_sector_code is not None:
            return ()
        return (
            StoredEquityMembership(
                sector=self.sector,
                observed_from=datetime(2026, 7, 20, 10, tzinfo=UTC),
                observed_to=None,
                snapshot_observed_at=datetime(2026, 7, 27, 10, tzinfo=UTC),
                carried_forward=False,
            ),
        )[:limit]


def test_membership_api_pages_fixed_release_and_honors_etag(configured_environment) -> None:
    """内部读取必须只投影 confirmed 身份，并使游标和 ETag 绑定同一 dataVersion。"""
    del configured_environment
    settings = load_settings()
    repository = FakeMembershipRepository()
    client = TestClient(
        create_app(
            settings=settings,
            repository=cast(SectorMarketDataRepository, object()),
            membership_repository=cast(SectorMembershipRepository, repository),
        )
    )
    headers = {"Authorization": f"Bearer {settings.internal_api_bearer_token.get_secret_value()}"}
    endpoint = "/internal/v1/sectors/eastmoney.industry/BK0475/constituents?limit=1"

    first = client.get(endpoint, headers=headers)
    cached = client.get(endpoint, headers={**headers, "If-None-Match": first.headers["etag"]})
    assert first.status_code == 200
    assert first.json()["items"][0]["instrumentId"] == str(repository.equity.instrument_id)
    assert first.json()["release"]["membershipSemantics"] == "observed"
    assert first.json()["nextCursor"] is not None
    assert cached.status_code == 304

    repository.release = replace(repository.release, data_version=uuid4())
    expired = client.get(f"{endpoint}&cursor={first.json()['nextCursor']}", headers=headers)

    assert expired.status_code == 409
    assert expired.json()["code"] == "snapshot-expired"


def test_membership_api_reads_reverse_membership_and_rejects_naive_as_of(
    configured_environment,
) -> None:
    """反向读取应保留 release 语义，且无时区历史选择不能默默依赖进程时区。"""
    del configured_environment
    settings = load_settings()
    repository = FakeMembershipRepository()
    client = TestClient(
        create_app(
            settings=settings,
            repository=cast(SectorMarketDataRepository, object()),
            membership_repository=cast(SectorMembershipRepository, repository),
        )
    )
    headers = {"Authorization": f"Bearer {settings.internal_api_bearer_token.get_secret_value()}"}
    endpoint = "/internal/v1/equities/SSE/600000/sectors?scheme=eastmoney.industry"

    response = client.get(endpoint, headers=headers)
    naive = client.get(f"{endpoint}&asOf=2026-07-27T10:00:00", headers=headers)

    assert response.status_code == 200
    assert response.json()["equity"]["listingStatus"] == "LISTED"
    assert response.json()["items"][0]["code"] == "BK0475"
    assert naive.status_code == 400
