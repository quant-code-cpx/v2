"""板块成分 internal API 的 release、游标、ETag 与身份投影回归测试。"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

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
        self.calls: list[str] = []
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
        self.same_day_release = replace(
            self.release,
            release_id=uuid4(),
            data_version=uuid4(),
            published_at=self.release.published_at + timedelta(minutes=5),
        )
        self.equity = StoredMembershipEquity(
            instrument_id=uuid4(),
            exchange=Exchange.SSE,
            symbol="600000",
            name="浦发银行",
            listing_status="LISTED",
        )
        self.old_equity = replace(
            self.equity,
            instrument_id=uuid4(),
            name="代码复用前证券",
        )
        self.old_sector = StoredSector(
            sector_key=2,
            sector_id=uuid4(),
            identifier=SectorIdentifier(SectorScheme.EASTMONEY_INDUSTRY, "BK0001"),
            name="历史行业",
            status="ACTIVE",
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
        self,
        *,
        scheme: SectorScheme,
        as_of: datetime | None,
        data_version: UUID | None = None,
    ) -> StoredSectorMembershipRelease | None:
        """按精确版本返回同业务时点的 release，并回显旧式请求时刻。"""
        self.calls.append("release")
        if scheme is not SectorScheme.EASTMONEY_INDUSTRY:
            return None
        if data_version is not None:
            for release in (self.release, self.same_day_release):
                if release.data_version == data_version:
                    return replace(release, requested_as_of=as_of)
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

    def resolve_equity_identity(
        self,
        *,
        exchange: Exchange,
        symbol: str,
        identity_as_of: date | None,
        known_at: datetime | None,
    ) -> StoredMembershipEquity | None:
        """按独立身份业务日解析代码复用前后证券，并记录知识时刻。"""
        self.calls.append("identity")
        del known_at
        if (exchange, symbol) != (self.equity.exchange, self.equity.symbol):
            return None
        if identity_as_of is not None and identity_as_of < date(2020, 1, 1):
            return self.old_equity
        return self.equity

    def list_equity_memberships(
        self,
        *,
        release_id: object,
        instrument_id: object,
        after_sector_code: str | None,
        limit: int,
    ) -> tuple[StoredEquityMembership, ...]:
        """返回反向归属，已知证券无后续页时保留空 cursor。"""
        self.calls.append("memberships")
        if release_id not in {self.release.release_id, self.same_day_release.release_id}:
            return ()
        if instrument_id == self.old_equity.instrument_id:
            sectors = (self.old_sector, self.sector)
        elif instrument_id == self.equity.instrument_id:
            sectors = (self.sector,)
        else:
            return ()
        rows = tuple(
            StoredEquityMembership(
                sector=sector,
                observed_from=datetime(2026, 7, 20, 10, tzinfo=UTC),
                observed_to=None,
                snapshot_observed_at=datetime(2026, 7, 27, 10, tzinfo=UTC),
                carried_forward=False,
            )
            for sector in sectors
            if after_sector_code is None or sector.identifier.code > after_sector_code
        )
        return rows[:limit]


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
    assert first.headers["x-data-version"] == str(repository.release.data_version)
    assert cached.status_code == 304
    assert cached.headers["x-data-version"] == str(repository.release.data_version)

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
    endpoint = (
        "/internal/v1/equities/SSE/600000/sectors?scheme=eastmoney.industry"
        f"&dataVersion={repository.release.data_version}&identityAsOf=2026-07-29"
    )

    response = client.get(endpoint, headers=headers)
    naive = client.get(f"{endpoint}&asOf=2026-07-27T10:00:00", headers=headers)

    assert response.status_code == 200
    assert response.json()["equity"]["listingStatus"] == "LISTED"
    assert response.json()["identityAsOf"] == "2026-07-29"
    assert response.json()["dataVersion"] == str(repository.release.data_version)
    assert response.json()["items"][0]["code"] == "BK0475"
    assert response.headers["x-data-version"] == str(repository.release.data_version)
    assert naive.status_code == 400


def test_reverse_membership_pins_release_and_resolves_code_reuse_before_reading_facts(
    configured_environment,
) -> None:
    """精确 release 与身份时点必须独立，旧代码身份不得读到复用后的新证券归属。"""
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
    base = (
        "/internal/v1/equities/SSE/600000/sectors?scheme=eastmoney.industry"
        f"&dataVersion={repository.same_day_release.data_version}"
        "&knownAt=2026-07-30T00:00:00Z"
    )

    old = client.get(f"{base}&identityAsOf=2019-12-31&limit=1", headers=headers)
    old_cursor = old.json()["nextCursor"]
    new = client.get(f"{base}&identityAsOf=2026-07-29", headers=headers)
    crossed_cursor = client.get(
        f"{base}&identityAsOf=2026-07-29&limit=1&cursor={old_cursor}",
        headers=headers,
    )

    assert old.status_code == 200
    assert old.json()["identityAsOf"] == "2019-12-31"
    assert old.json()["dataVersion"] == str(repository.same_day_release.data_version)
    assert old.json()["release"]["dataVersion"] == str(repository.same_day_release.data_version)
    assert old.json()["equity"]["name"] == "代码复用前证券"
    assert old.json()["items"][0]["code"] == "BK0001"
    assert old_cursor is not None
    assert new.status_code == 200
    assert new.json()["equity"]["name"] == "浦发银行"
    assert new.json()["items"][0]["code"] == "BK0475"
    assert crossed_cursor.status_code == 409
    assert crossed_cursor.json()["code"] == "snapshot-expired"
    assert repository.calls[:3] == ["identity", "release", "memberships"]


def test_reverse_membership_rejects_missing_exact_release(
    configured_environment,
) -> None:
    """调用方指定的 publication 不存在时必须冲突失败，不能静默回退到当前 release。"""
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

    response = client.get(
        "/internal/v1/equities/SSE/600000/sectors?scheme=eastmoney.industry"
        f"&dataVersion={uuid4()}&identityAsOf=2026-07-29",
        headers=headers,
    )

    assert response.status_code == 409
    assert response.json()["code"] == "snapshot-expired"
    assert repository.calls == ["identity", "release"]


def test_reverse_membership_requires_both_snapshot_and_identity_anchor(
    configured_environment,
) -> None:
    """证券反向归属缺少任一精确锚点时必须在执行仓储查询前失败。"""
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

    missing_version = client.get(f"{endpoint}&identityAsOf=2026-07-29", headers=headers)
    missing_identity = client.get(
        f"{endpoint}&dataVersion={repository.release.data_version}",
        headers=headers,
    )

    assert missing_version.status_code == 400
    assert missing_identity.status_code == 400
    assert repository.calls == []


def test_reverse_membership_etag_and_cursor_bind_resolved_permanent_identity(
    configured_environment,
) -> None:
    """同一日期身份更正后 ETag 必须变化，旧身份游标也不能续读新永久证券。"""
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
    endpoint = (
        "/internal/v1/equities/SSE/600000/sectors?scheme=eastmoney.industry"
        f"&dataVersion={repository.release.data_version}&identityAsOf=2019-12-31&limit=1"
    )

    before = client.get(endpoint, headers=headers)
    old_cursor = before.json()["nextCursor"]
    repository.old_equity = replace(repository.old_equity, instrument_id=uuid4())
    after = client.get(endpoint, headers=headers)
    crossed = client.get(f"{endpoint}&cursor={old_cursor}", headers=headers)

    assert before.status_code == 200
    assert after.status_code == 200
    assert before.headers["etag"] != after.headers["etag"]
    assert old_cursor is not None
    assert crossed.status_code == 409
    assert crossed.json()["code"] == "snapshot-expired"
