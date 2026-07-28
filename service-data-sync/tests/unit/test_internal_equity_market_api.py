"""方案 0011 内部行情、因子、公司行动与概况路由测试。"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import cast
from uuid import UUID

from fastapi.testclient import TestClient

from service_data_sync.application.ports.financial_read import FinancialReadRepository
from service_data_sync.application.ports.market_data import (
    EquityDatasetPublication,
    EquityIdentityReadConflictError,
    EquityMarketDataRepository,
    StoredAdjustmentFactor,
    StoredCompanyProfile,
    StoredCorporateAction,
    StoredEquityBar,
    StoredEquityInstrument,
)
from service_data_sync.application.ports.sector_market_data import SectorMarketDataRepository
from service_data_sync.bootstrap.settings import load_settings
from service_data_sync.domain.equity import (
    EquityAdjustmentFactor,
    EquityBarPeriod,
    EquityCompanyProfile,
    EquityCorporateAction,
    EquityIdentifier,
    EquityPeriodBar,
)
from service_data_sync.interfaces.internal_sector_api import create_app

_BAR_VERSION = UUID("10000000-0000-4000-8000-000000000001")
_FACTOR_VERSION = UUID("10000000-0000-4000-8000-000000000002")
_ACTION_VERSION = UUID("10000000-0000-4000-8000-000000000003")
_PROFILE_VERSION = UUID("10000000-0000-4000-8000-000000000004")


class FakeMarketRepository:
    """提供四种已发布个股数据和可切换缺发布状态。"""

    def __init__(self) -> None:
        """构造单证券、两条周线、两点因子、事件与概况。"""
        identifier = EquityIdentifier.parse("SSE.600519")
        self.instrument = StoredEquityInstrument(
            security_id=1,
            instrument_id=UUID("20000000-0000-4000-8000-000000000001"),
            identifier=identifier,
            name="贵州茅台",
            listing_status="LISTED",
        )
        self.missing_dataset: str | None = None
        self.identity_conflict = False
        self.identity_requests: list[tuple[date | None, date | None]] = []
        self.publications = {
            "equity.bar.1w.raw": _publication(_BAR_VERSION),
            "equity.adjustment_factor": _publication(_FACTOR_VERSION),
            "equity.corporate_action": _publication(_ACTION_VERSION),
            "equity.profile": _publication(_PROFILE_VERSION),
        }

    def get_instrument_by_identifier(
        self,
        identifier: EquityIdentifier,
        *,
        fact_start: date | None,
        fact_end: date | None,
    ) -> StoredEquityInstrument | None:
        """记录事实窗口并仅返回测试证券。"""
        self.identity_requests.append((fact_start, fact_end))
        if self.identity_conflict:
            raise EquityIdentityReadConflictError("test identity boundary")
        return self.instrument if identifier == self.instrument.identifier else None

    def get_current_publication(
        self,
        *,
        dataset: str,
        instrument: StoredEquityInstrument,
    ) -> EquityDatasetPublication | None:
        """按数据集返回当前发布，并验证永久证券分区。"""
        assert instrument.security_id == 1
        return None if dataset == self.missing_dataset else self.publications.get(dataset)

    def list_bars(
        self,
        *,
        security_id: int,
        period: EquityBarPeriod,
        start: date,
        end: date,
    ) -> tuple[StoredEquityBar, ...]:
        """返回窗口内两条上游原生周线。"""
        assert security_id == 1
        assert period is EquityBarPeriod.WEEK_1
        rows = (
            _stored_weekly_bar(date(2025, 12, 26), Decimal("10")),
            _stored_weekly_bar(date(2026, 7, 24), Decimal("20")),
        )
        return tuple(
            row for row in rows if start <= cast(EquityPeriodBar, row.bar).period_end <= end
        )

    def list_adjustment_factors(
        self,
        *,
        security_id: int,
        end: date,
    ) -> tuple[StoredAdjustmentFactor, ...]:
        """返回截止锚点前的稀疏累计因子。"""
        assert security_id == 1
        rows = (
            StoredAdjustmentFactor(
                factor=EquityAdjustmentFactor(
                    effective_date=date(2020, 1, 1),
                    cumulative_factor=Decimal("1"),
                ),
                revision=1,
                factor_version=_FACTOR_VERSION,
            ),
            StoredAdjustmentFactor(
                factor=EquityAdjustmentFactor(
                    effective_date=date(2026, 1, 1),
                    cumulative_factor=Decimal("2"),
                ),
                revision=1,
                factor_version=_FACTOR_VERSION,
            ),
        )
        return tuple(row for row in rows if row.factor.effective_date <= end)

    def list_corporate_actions(
        self,
        *,
        security_id: int,
        start: date | None,
        end: date | None,
    ) -> tuple[StoredCorporateAction, ...]:
        """返回一条实施完成的现金分红事件。"""
        del start, end
        assert security_id == 1
        return (
            StoredCorporateAction(
                action_id=UUID("30000000-0000-4000-8000-000000000001"),
                revision=2,
                action=EquityCorporateAction(
                    source_event_key="2025-12-31",
                    report_period=date(2025, 12, 31),
                    status="实施",
                    announcement_date=date(2026, 6, 1),
                    record_date=date(2026, 6, 29),
                    ex_date=date(2026, 6, 30),
                    cash_dividend_per_10=Decimal("10"),
                    bonus_shares_per_10=None,
                    transfer_shares_per_10=None,
                ),
            ),
        )

    def get_company_profile(self, *, security_id: int) -> StoredCompanyProfile | None:
        """返回当前公司概况。"""
        assert security_id == 1
        return StoredCompanyProfile(
            profile=EquityCompanyProfile(
                company_name="贵州茅台酒股份有限公司",
                english_name=None,
                industry="白酒",
                legal_representative=None,
                established_on=date(1999, 11, 20),
                website="https://example.test",
                email=None,
                phone=None,
                registered_address="贵州",
                office_address=None,
                main_business="白酒",
                business_scope=None,
                summary=None,
            ),
            revision=3,
        )


def test_bars_use_direct_weekly_rows_adjustment_and_conditional_etag(
    configured_environment: None,
) -> None:
    """周线响应必须来自周线记录，并按因子版本复权且支持 304。"""
    del configured_environment
    client, headers, repository = _client()
    endpoint = (
        "/internal/v1/equities/SSE/600519/bars"
        "?period=1w&start=2025-01-01&end=2026-07-28&adjust=qfq&limit=10"
    )

    response = client.get(endpoint, headers=headers)
    cached = client.get(
        endpoint,
        headers={**headers, "If-None-Match": response.headers["etag"]},
    )

    assert response.status_code == 200
    assert response.json()["period"] == "1w"
    assert response.json()["factorVersion"] == str(_FACTOR_VERSION)
    assert response.json()["formulaVersion"] == "cumulative-hfq-v1"
    assert response.json()["items"][0]["open"] == "5.000000"
    assert response.json()["items"][1]["open"] == "20.000000"
    assert response.headers["x-data-version"] == str(_BAR_VERSION)
    assert cached.status_code == 304
    assert cached.content == b""
    assert repository.identity_requests == [
        (date(2025, 1, 1), date(2026, 7, 28)),
        (date(2025, 1, 1), date(2026, 7, 28)),
    ]


def test_bar_pages_use_signed_cursor_bound_to_snapshot_and_query(
    configured_environment: None,
) -> None:
    """行情多页读取不截断，并拒绝篡改或跨查询复用游标。"""
    del configured_environment
    client, headers, repository = _client()
    endpoint = (
        "/internal/v1/equities/SSE/600519/bars?period=1w&start=2025-01-01&end=2026-07-28&limit=1"
    )

    first = client.get(endpoint, headers=headers)
    cursor = first.json()["nextCursor"]
    second = client.get(f"{endpoint}&cursor={cursor}", headers=headers)
    tampered = client.get(f"{endpoint}&cursor={cursor}x", headers=headers)
    changed_query = client.get(
        (
            "/internal/v1/equities/SSE/600519/bars"
            f"?period=1w&start=2026-01-01&end=2026-07-28&limit=1&cursor={cursor}"
        ),
        headers=headers,
    )

    assert first.status_code == 200
    assert cursor is not None
    assert first.json()["items"][0]["periodEnd"] == "2025-12-26"
    assert second.status_code == 200
    assert second.json()["items"][0]["periodEnd"] == "2026-07-24"
    assert second.json()["nextCursor"] is None
    assert tampered.status_code == 400
    assert changed_query.status_code == 400


def test_factor_action_and_profile_routes_return_published_contracts(
    configured_environment: None,
) -> None:
    """三类参考数据端点返回版本、精确字符串和真实空值。"""
    del configured_environment
    client, headers, repository = _client()

    factors = client.get(
        "/internal/v1/equities/SSE/600519/adjustment-factors?end=2026-07-28",
        headers=headers,
    )
    actions = client.get(
        "/internal/v1/equities/SSE/600519/corporate-actions",
        headers=headers,
    )
    profile = client.get(
        "/internal/v1/equities/SSE/600519/company-profile",
        headers=headers,
    )

    assert factors.status_code == 200
    assert factors.json()["items"][1]["cumulativeFactor"] == "2"
    assert actions.status_code == 200
    assert actions.json()["items"][0]["cashDividendPer10"] == "10"
    assert actions.json()["items"][0]["bonusSharesPer10"] is None
    assert profile.status_code == 200
    assert profile.json()["profile"]["industry"] == "白酒"
    assert profile.json()["revision"] == 3
    assert repository.identity_requests[0] == (None, date(2026, 7, 28))
    assert repository.identity_requests[1] == (None, None)
    profile_window = repository.identity_requests[2]
    assert profile_window[0] is not None and profile_window[0] == profile_window[1]


def test_market_routes_reject_identity_windows_crossing_code_reuse(
    configured_environment: None,
) -> None:
    """事实窗口覆盖两只复用同代码的证券时必须返回稳定 409。"""
    del configured_environment
    client, headers, repository = _client()
    repository.identity_conflict = True

    response = client.get(
        "/internal/v1/equities/SSE/600519/bars?period=1w&start=2020-01-01&end=2026-07-28",
        headers=headers,
    )

    assert response.status_code == 409
    assert response.json()["code"] == "identity-boundary-conflict"


def test_market_routes_fail_closed_for_auth_validation_and_missing_publication(
    configured_environment: None,
) -> None:
    """内部凭据、倒置日期和缺 publication 分别返回 401、400、503。"""
    del configured_environment
    client, headers, repository = _client()
    missing_auth = client.get(
        "/internal/v1/equities/SSE/600519/company-profile",
    )
    bad_range = client.get(
        "/internal/v1/equities/SSE/600519/bars?period=1w&start=2026-07-28&end=2026-01-01",
        headers=headers,
    )
    repository.missing_dataset = "equity.bar.1w.raw"
    unpublished = client.get(
        "/internal/v1/equities/SSE/600519/bars?period=1w&start=2026-01-01&end=2026-07-28",
        headers=headers,
    )

    assert missing_auth.status_code == 401
    assert bad_range.status_code == 400
    assert unpublished.status_code == 503
    assert unpublished.headers["retry-after"] == "5"


def _client() -> tuple[TestClient, dict[str, str], FakeMarketRepository]:
    """构造只挂载测试市场仓储的共享内部应用。"""
    settings = load_settings()
    repository = FakeMarketRepository()
    app = create_app(
        settings=settings,
        repository=cast(SectorMarketDataRepository, object()),
        financial_repository=cast(FinancialReadRepository, object()),
        equity_market_repository=cast(EquityMarketDataRepository, repository),
    )
    credential = settings.internal_api_bearer_token.get_secret_value()
    return (
        TestClient(app),
        {"Authorization": f"Bearer {credential}"},
        repository,
    )


def _publication(data_version: UUID) -> EquityDatasetPublication:
    """构造确定性当前发布。"""
    return EquityDatasetPublication(
        data_version=data_version,
        published_at=datetime(2026, 7, 28, 12, tzinfo=UTC),
    )


def _stored_weekly_bar(period_end: date, open_price: Decimal) -> StoredEquityBar:
    """构造一条有效上游原生周线读取记录。"""
    return StoredEquityBar(
        bar=EquityPeriodBar(
            period=EquityBarPeriod.WEEK_1,
            period_end=period_end,
            open_price=open_price,
            high_price=open_price + Decimal("2"),
            low_price=open_price - Decimal("1"),
            close_price=open_price + Decimal("1"),
            volume_shares=1_000,
            amount_cny=open_price * Decimal("1000"),
            turnover_rate=Decimal("0.01"),
        ),
        revision=1,
        is_final=True,
    )
