"""财务与估值内部 `API` 暗发布契约边界测试。"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import cast
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from service_data_sync.application.ports.financial_read import (
    FinancialCapability,
    FinancialPublicationSnapshot,
    FinancialReadRepository,
    PublishedFinancialMetric,
    PublishedFinancialReport,
    PublishedFinancialReportDetail,
    PublishedFinancialStatementFact,
    PublishedValuationObservation,
)
from service_data_sync.application.ports.sector_market_data import SectorMarketDataRepository
from service_data_sync.bootstrap.settings import load_settings
from service_data_sync.domain.equity import Exchange
from service_data_sync.interfaces.internal_sector_api import create_app

_REPORT_VERSION = UUID("10000000-0000-4000-8000-000000000016")
_PROVIDER_METRIC_VERSION = UUID("10000000-0000-4000-8000-000000000017")
_VALUATION_VERSION = UUID("10000000-0000-4000-8000-000000000018")
_DERIVED_METRIC_VERSION = UUID("10000000-0000-4000-8000-000000000019")


class RecordingFinancialRepository:
    """记录暗发布解析请求，并稳定模拟不存在生产发布。"""

    def __init__(self) -> None:
        """初始化空调用记录，不构造数据库或来源连接。"""
        self.calls: list[
            tuple[
                Exchange,
                str,
                FinancialCapability,
                str,
                int,
                date | None,
                datetime | None,
            ]
        ] = []

    def get_current_publication(
        self,
        *,
        exchange: Exchange,
        symbol: str,
        capability: FinancialCapability,
        methodology_code: str,
        methodology_version: int,
        as_of: date | None = None,
        known_at: datetime | None = None,
    ) -> FinancialPublicationSnapshot | None:
        """记录精确 `publication` 身份并返回空值，保持路由拒绝读取。"""
        self.calls.append(
            (
                exchange,
                symbol,
                capability,
                methodology_code,
                methodology_version,
                as_of,
                known_at,
            )
        )
        return None


class PublishedFinancialRepository:
    """返回已冻结发布中的确定性报表页，并记录接口传入的读取视图。"""

    def __init__(
        self,
        reports: tuple[PublishedFinancialReport, ...],
        facts: tuple[PublishedFinancialStatementFact, ...] = (),
        metrics: tuple[PublishedFinancialMetric, ...] = (),
        derived_metrics: tuple[PublishedFinancialMetric, ...] = (),
        valuations: tuple[PublishedValuationObservation, ...] = (),
    ) -> None:
        """保存有序财务投影，避免测试接触真实数据库或来源适配器。"""
        self._reports = reports
        self._facts = facts
        self._metrics = metrics
        self._derived_metrics = derived_metrics
        self._valuations = valuations
        self.calls: list[dict[str, object]] = []
        self.fact_calls: list[dict[str, object]] = []
        self.metric_calls: list[dict[str, object]] = []
        self.derived_metric_calls: list[dict[str, object]] = []
        self.valuation_calls: list[dict[str, object]] = []

    def get_current_publication(
        self,
        *,
        exchange: Exchange,
        symbol: str,
        capability: FinancialCapability,
        methodology_code: str,
        methodology_version: int,
        as_of: date | None = None,
        known_at: datetime | None = None,
    ) -> FinancialPublicationSnapshot | None:
        """只接受测试声明的精确发布身份，模拟生产选择器的防猜测边界。"""
        del as_of, known_at
        publications = {
            ("financial.report", "eastmoney.reported", 2): _publication(),
            (
                "financial.provider-metric",
                "eastmoney.provider-metric",
                2,
            ): _metric_publication(),
            (
                "financial.derived-metric",
                "platform.financial-derivation",
                1,
            ): _derived_metric_publication(),
            ("financial.valuation", "eastmoney.valuation", 2): _valuation_publication(),
        }
        if exchange is Exchange.SSE and symbol == "600519":
            return publications.get((capability, methodology_code, methodology_version))
        return None

    def list_reports(
        self,
        *,
        publication: FinancialPublicationSnapshot,
        as_of: date,
        known_at: datetime,
        statement_types: tuple[str, ...],
        period_bases: tuple[str, ...],
        statement_scope: str | None,
        report_period_from: date | None,
        report_period_to: date | None,
        after_report_period: date | None,
        after_statement_type: str | None,
        after_report_ref: UUID | None,
        limit: int,
    ) -> tuple[PublishedFinancialReport, ...]:
        """记录完整视图，并按接收的末行键返回模拟下一页。"""
        self.calls.append(
            {
                "publication": publication,
                "as_of": as_of,
                "known_at": known_at,
                "statement_types": statement_types,
                "period_bases": period_bases,
                "statement_scope": statement_scope,
                "report_period_from": report_period_from,
                "report_period_to": report_period_to,
                "after_report_period": after_report_period,
                "after_statement_type": after_statement_type,
                "after_report_ref": after_report_ref,
                "limit": limit,
            }
        )
        if after_report_ref is None:
            return self._reports[:limit]
        for index, report in enumerate(self._reports):
            if report.report_ref == after_report_ref:
                return self._reports[index + 1 : index + 1 + limit]
        return ()

    def get_current_report_publication(
        self,
        *,
        exchange: Exchange,
        symbol: str,
        report_ref: UUID,
        as_of: date | None = None,
        known_at: datetime | None = None,
    ) -> FinancialPublicationSnapshot | None:
        """按公开引用返回测试发布，模拟详情路由不接收方法学猜测参数。"""
        del as_of, known_at
        if exchange is Exchange.SSE and symbol == "600519":
            if any(report.report_ref == report_ref for report in self._reports):
                return _publication()
        return None

    def get_report_detail(
        self,
        *,
        publication: FinancialPublicationSnapshot,
        report_ref: UUID,
        as_of: date,
        known_at: datetime,
    ) -> PublishedFinancialReportDetail | None:
        """返回与发布和双时态视图匹配的固定 revision，不允许跨报表读取。"""
        if publication == _publication() and known_at <= publication.knowledge_cutoff:
            for report in self._reports:
                if (
                    report.report_ref == report_ref
                    and report.effective_from <= as_of
                    and report.known_from <= known_at
                ):
                    return PublishedFinancialReportDetail(
                        report=report,
                        revision_id=UUID("60000000-0000-4000-8000-000000000001"),
                    )
        return None

    def list_report_facts(
        self,
        *,
        detail: PublishedFinancialReportDetail,
        metric_codes: tuple[str, ...],
        after_metric_code: str | None,
        limit: int,
    ) -> tuple[PublishedFinancialStatementFact, ...]:
        """记录详情页筛选与续页键，并返回已按字段代码排序的确定性行项目。"""
        self.fact_calls.append(
            {
                "detail": detail,
                "metric_codes": metric_codes,
                "after_metric_code": after_metric_code,
                "limit": limit,
            }
        )
        filtered = tuple(
            fact for fact in self._facts if not metric_codes or fact.metric_code in metric_codes
        )
        if after_metric_code is None:
            return filtered[:limit]
        for index, fact in enumerate(filtered):
            if fact.metric_code == after_metric_code:
                return filtered[index + 1 : index + 1 + limit]
        return ()

    def list_provider_metrics(
        self,
        *,
        publication: FinancialPublicationSnapshot,
        as_of: date,
        known_at: datetime,
        metric_codes: tuple[str, ...],
        period_bases: tuple[str, ...],
        report_period_from: date | None,
        report_period_to: date | None,
        after_report_period: date | None,
        after_metric_code: str | None,
        limit: int,
    ) -> tuple[PublishedFinancialMetric, ...]:
        """记录指标页完整范围，并按合同规定的报告期和字段代码升序续页。"""
        self.metric_calls.append(
            {
                "publication": publication,
                "as_of": as_of,
                "known_at": known_at,
                "metric_codes": metric_codes,
                "period_bases": period_bases,
                "report_period_from": report_period_from,
                "report_period_to": report_period_to,
                "after_report_period": after_report_period,
                "after_metric_code": after_metric_code,
                "limit": limit,
            }
        )
        rows = tuple(
            metric
            for metric in self._metrics
            if (not metric_codes or metric.metric_code in metric_codes)
            and (not period_bases or metric.period_basis in period_bases)
            and (report_period_from is None or metric.report_period >= report_period_from)
            and (report_period_to is None or metric.report_period <= report_period_to)
        )
        if after_report_period is None:
            return rows[:limit]
        assert after_metric_code is not None
        return tuple(
            metric
            for metric in rows
            if (metric.report_period, metric.metric_code) > (after_report_period, after_metric_code)
        )[:limit]

    def list_valuations(
        self,
        *,
        publication: FinancialPublicationSnapshot,
        as_of: date,
        known_at: datetime,
        metric_codes: tuple[str, ...],
        start: date,
        end: date,
        after_observation_date: date | None,
        after_metric_code: str | None,
        limit: int,
    ) -> tuple[PublishedValuationObservation, ...]:
        """记录估值页范围，并按日期和字段代码升序稳定续页。"""
        self.valuation_calls.append(
            {
                "publication": publication,
                "as_of": as_of,
                "known_at": known_at,
                "metric_codes": metric_codes,
                "start": start,
                "end": end,
                "after_observation_date": after_observation_date,
                "after_metric_code": after_metric_code,
                "limit": limit,
            }
        )
        rows = tuple(
            valuation
            for valuation in self._valuations
            if valuation.metric_code in metric_codes and start <= valuation.observation_date <= end
        )
        if after_observation_date is None:
            return rows[:limit]
        assert after_metric_code is not None
        return tuple(
            valuation
            for valuation in rows
            if (valuation.observation_date, valuation.metric_code)
            > (after_observation_date, after_metric_code)
        )[:limit]

    def list_derived_metrics(
        self,
        *,
        publication: FinancialPublicationSnapshot,
        as_of: date,
        known_at: datetime,
        metric_codes: tuple[str, ...],
        period_bases: tuple[str, ...],
        report_period_from: date | None,
        report_period_to: date | None,
        after_report_period: date | None,
        after_metric_code: str | None,
        limit: int,
    ) -> tuple[PublishedFinancialMetric, ...]:
        """记录派生指标点时读取，并按报告期、指标代码复合键稳定续页。"""
        self.derived_metric_calls.append(
            {
                "publication": publication,
                "as_of": as_of,
                "known_at": known_at,
                "metric_codes": metric_codes,
                "period_bases": period_bases,
                "report_period_from": report_period_from,
                "report_period_to": report_period_to,
                "after_report_period": after_report_period,
                "after_metric_code": after_metric_code,
                "limit": limit,
            }
        )
        rows = tuple(
            metric
            for metric in self._derived_metrics
            if (not metric_codes or metric.metric_code in metric_codes)
            and (not period_bases or metric.period_basis in period_bases)
            and (report_period_from is None or metric.report_period >= report_period_from)
            and (report_period_to is None or metric.report_period <= report_period_to)
        )
        if after_report_period is None:
            return rows[:limit]
        assert after_metric_code is not None
        return tuple(
            metric
            for metric in rows
            if (metric.report_period, metric.metric_code) > (after_report_period, after_metric_code)
        )[:limit]


@pytest.fixture
def financial_client(configured_environment: None) -> tuple[TestClient, dict[str, str]]:
    """构造不连接 `canonical` 数据库的内部 `API` client 与有效 `service Bearer`。"""
    settings = load_settings()
    client = TestClient(
        create_app(settings=settings, repository=cast(SectorMarketDataRepository, object()))
    )
    headers = {"Authorization": f"Bearer {settings.internal_api_bearer_token.get_secret_value()}"}
    return client, headers


@pytest.mark.parametrize(
    "path",
    [
        "/internal/v1/equities/SSE/600519/financial-reports"
        f"?methodologyCode=research.v1&methodologyVersion=1&dataVersion={_REPORT_VERSION}",
        "/internal/v1/equities/SSE/600519/financial-reports/"
        f"00000000-0000-4000-8000-000000000001?dataVersion={_REPORT_VERSION}",
        "/internal/v1/equities/SSE/600519/financial-metrics?origin=PROVIDER_REPORTED"
        f"&methodologyCode=research.v1&methodologyVersion=1&metric=net_income"
        f"&dataVersion={_PROVIDER_METRIC_VERSION}",
        "/internal/v1/equities/SSE/600519/valuations?methodologyCode=research.v1"
        f"&methodologyVersion=1&metric=pe_ttm&start=2026-01-01&end=2026-01-31"
        f"&dataVersion={_VALUATION_VERSION}",
    ],
)
def test_financial_routes_fail_closed_without_publication(
    financial_client: tuple[TestClient, dict[str, str]],
    path: str,
) -> None:
    """四条 0013 路径在无 `publication` 时只返回脱敏 503 与有界重试提示。"""
    client, headers = financial_client

    response = client.get(path, headers=headers)

    assert response.status_code == 503
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["retry-after"] == "5"
    assert response.json()["code"] == "financial-publication-unavailable"


def test_financial_routes_require_exact_service_bearer(
    financial_client: tuple[TestClient, dict[str, str]],
) -> None:
    """财务读取路径必须在参数与 `publication` 判断前拒绝匿名访问。"""
    client, _headers = financial_client

    response = client.get(
        "/internal/v1/equities/SSE/600519/financial-reports?methodologyCode=research.v1"
        "&methodologyVersion=1"
    )

    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"


def test_financial_routes_reject_invalid_contract_ranges_before_unavailable(
    financial_client: tuple[TestClient, dict[str, str]],
) -> None:
    """无 `publication` 不能掩盖非法交易所、反向报告期或超限估值窗口。"""
    client, headers = financial_client

    bad_exchange = client.get(
        "/internal/v1/equities/US/600519/financial-reports?methodologyCode=research.v1"
        f"&methodologyVersion=1&dataVersion={_REPORT_VERSION}",
        headers=headers,
    )
    reversed_period = client.get(
        "/internal/v1/equities/SSE/600519/financial-reports?methodologyCode=research.v1"
        f"&methodologyVersion=1&reportPeriodFrom=2026-06-30&reportPeriodTo=2026-03-31"
        f"&dataVersion={_REPORT_VERSION}",
        headers=headers,
    )
    excessive_valuation = client.get(
        "/internal/v1/equities/SSE/600519/valuations?methodologyCode=research.v1"
        f"&methodologyVersion=1&metric=pe_ttm&start=2010-01-01&end=2026-01-01"
        f"&dataVersion={_VALUATION_VERSION}",
        headers=headers,
    )

    assert bad_exchange.status_code == 400
    assert reversed_period.status_code == 400
    assert excessive_valuation.status_code == 400


@pytest.mark.parametrize(
    ("path", "capability"),
    [
        (
            "/internal/v1/equities/SSE/600519/financial-reports?methodologyCode=research.v1"
            f"&methodologyVersion=1&dataVersion={_REPORT_VERSION}",
            "financial.report",
        ),
        (
            "/internal/v1/equities/SSE/600519/financial-metrics?origin=PLATFORM_DERIVED"
            f"&methodologyCode=research.v1&methodologyVersion=1&metric=roe"
            f"&dataVersion={_DERIVED_METRIC_VERSION}",
            "financial.derived-metric",
        ),
        (
            "/internal/v1/equities/SSE/600519/valuations?methodologyCode=research.v1"
            f"&methodologyVersion=1&metric=pe_ttm&start=2026-01-01&end=2026-01-31"
            f"&dataVersion={_VALUATION_VERSION}",
            "financial.valuation",
        ),
    ],
)
def test_financial_list_routes_resolve_exact_publication_before_fail_closed(
    configured_environment: None,
    path: str,
    capability: FinancialCapability,
) -> None:
    """列表路由在返回版本冲突前必须传入完整身份，禁止按最新方法学猜测。"""
    settings = load_settings()
    repository = RecordingFinancialRepository()
    client = TestClient(
        create_app(
            settings=settings,
            repository=cast(SectorMarketDataRepository, object()),
            financial_repository=cast(FinancialReadRepository, repository),
        )
    )

    response = client.get(
        path + "&asOf=2026-01-15&knownAt=2026-07-28T08:00:00Z",
        headers={
            "Authorization": f"Bearer {settings.internal_api_bearer_token.get_secret_value()}"
        },
    )

    assert response.status_code == 409
    assert response.json()["code"] == "snapshot-expired"
    assert repository.calls == [
        (
            Exchange.SSE,
            "600519",
            capability,
            "research.v1",
            1,
            date(2026, 1, 15),
            datetime(2026, 7, 28, 8, tzinfo=UTC),
        )
    ]


def test_financial_report_list_returns_frozen_page_and_honors_conditional_get(
    configured_environment: None,
) -> None:
    """已发布报表页应公开完整快照身份，并对同一表示返回安全的 304。"""
    settings = load_settings()
    repository = PublishedFinancialRepository((_report("1"), _report("2")))
    client = TestClient(
        create_app(
            settings=settings,
            repository=cast(SectorMarketDataRepository, object()),
            financial_repository=repository,
        )
    )
    headers = {"Authorization": f"Bearer {settings.internal_api_bearer_token.get_secret_value()}"}
    path = (
        "/internal/v1/equities/SSE/600519/financial-reports?methodologyCode=eastmoney.reported"
        f"&methodologyVersion=2&limit=1&dataVersion={_REPORT_VERSION}"
    )

    response = client.get(path, headers=headers)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, max-age=0, must-revalidate"
    assert response.headers["x-data-version"] == "10000000-0000-4000-8000-000000000016"
    assert response.headers["x-request-id"]
    assert response.json()["items"][0]["reportRef"] == "50000000-0000-4000-8000-000000000001"
    assert response.json()["items"][0]["qualityStatus"] == "PASSED"
    assert response.json()["items"][0]["sourceCode"] == "eastmoney"
    assert response.json()["nextCursor"]
    assert repository.calls[0]["as_of"] == date(2026, 7, 27)
    assert repository.calls[0]["known_at"] == datetime(2026, 7, 28, 8, tzinfo=UTC)
    assert repository.calls[0]["limit"] == 2

    not_modified = client.get(path, headers={**headers, "If-None-Match": response.headers["etag"]})

    assert not_modified.status_code == 304
    assert not_modified.headers["etag"] == response.headers["etag"]
    assert not_modified.headers["x-data-version"] == response.headers["x-data-version"]


def test_financial_routes_reject_status_version_drift_before_reading_rows(
    configured_environment: None,
) -> None:
    """状态版本与当前 publication 不同必须返回 409，禁止默取最新财务事实。"""
    settings = load_settings()
    repository = PublishedFinancialRepository((_report("1"),))
    client = TestClient(
        create_app(
            settings=settings,
            repository=cast(SectorMarketDataRepository, object()),
            financial_repository=repository,
        )
    )
    headers = {"Authorization": f"Bearer {settings.internal_api_bearer_token.get_secret_value()}"}
    wrong_version = UUID("10000000-0000-4000-8000-000000000099")

    report_list = client.get(
        "/internal/v1/equities/SSE/600519/financial-reports"
        "?methodologyCode=eastmoney.reported&methodologyVersion=2"
        f"&dataVersion={wrong_version}",
        headers=headers,
    )
    report_detail = client.get(
        "/internal/v1/equities/SSE/600519/financial-reports/"
        "50000000-0000-4000-8000-000000000001"
        f"?dataVersion={wrong_version}",
        headers=headers,
    )

    assert report_list.status_code == 409
    assert report_list.json()["code"] == "snapshot-expired"
    assert report_detail.status_code == 409
    assert report_detail.json()["code"] == "snapshot-expired"
    assert repository.calls == []
    assert repository.fact_calls == []


def test_financial_report_list_cursor_rejects_changed_scope_and_reads_next_page(
    configured_environment: None,
) -> None:
    """游标必须绑定筛选视图；同一游标仅能继续读取对应发布中的下一页。"""
    settings = load_settings()
    repository = PublishedFinancialRepository((_report("1"), _report("2")))
    client = TestClient(
        create_app(
            settings=settings,
            repository=cast(SectorMarketDataRepository, object()),
            financial_repository=repository,
        )
    )
    headers = {"Authorization": f"Bearer {settings.internal_api_bearer_token.get_secret_value()}"}
    path = (
        "/internal/v1/equities/SSE/600519/financial-reports?methodologyCode=eastmoney.reported"
        f"&methodologyVersion=2&limit=1&dataVersion={_REPORT_VERSION}"
    )
    first = client.get(path, headers=headers)
    cursor = first.json()["nextCursor"]

    next_page = client.get(f"{path}&cursor={cursor}", headers=headers)
    changed_scope = client.get(f"{path}&cursor={cursor}&scope=PARENT", headers=headers)
    tampered_cursor = client.get(f"{path}&cursor={cursor}x", headers=headers)
    future_as_of = client.get(f"{path}&asOf=2026-07-28", headers=headers)

    assert next_page.status_code == 200
    assert next_page.json()["items"][0]["reportRef"] == "50000000-0000-4000-8000-000000000002"
    assert repository.calls[-1]["after_report_ref"] == UUID("50000000-0000-4000-8000-000000000001")
    assert changed_scope.status_code == 409
    assert changed_scope.json()["code"] == "cursor-mismatch"
    assert tampered_cursor.status_code == 400
    assert tampered_cursor.json()["code"] == "validation-error"
    assert future_as_of.status_code == 400
    assert future_as_of.json()["code"] == "validation-error"


def test_financial_report_detail_returns_frozen_facts_and_binds_cursor(
    configured_environment: None,
) -> None:
    """报表详情必须固定同一 revision 的治理字段，并阻止跨字段筛选复用游标。"""
    settings = load_settings()
    repository = PublishedFinancialRepository((_report("1"),), (_fact("1"), _fact("2")))
    client = TestClient(
        create_app(
            settings=settings,
            repository=cast(SectorMarketDataRepository, object()),
            financial_repository=repository,
        )
    )
    headers = {"Authorization": f"Bearer {settings.internal_api_bearer_token.get_secret_value()}"}
    path = (
        "/internal/v1/equities/SSE/600519/financial-reports/"
        f"50000000-0000-4000-8000-000000000001?limit=1&dataVersion={_REPORT_VERSION}"
    )

    response = client.get(path, headers=headers)

    assert response.status_code == 200
    assert response.json()["report"]["reportRef"] == "50000000-0000-4000-8000-000000000001"
    assert response.json()["items"][0]["metricCode"] == "assets"
    assert response.json()["items"][0]["value"] == "123.45"
    assert response.json()["items"][0]["scaleFactor"] == "1.0000"
    assert response.json()["nextCursor"]
    assert repository.fact_calls[0]["metric_codes"] == ()
    assert repository.fact_calls[0]["limit"] == 2

    not_modified = client.get(path, headers={**headers, "If-None-Match": response.headers["etag"]})
    cursor = response.json()["nextCursor"]
    next_page = client.get(f"{path}&cursor={cursor}", headers=headers)
    changed_filter = client.get(f"{path}&cursor={cursor}&metric=revenue", headers=headers)
    not_visible = client.get(f"{path}&asOf=2026-04-27", headers=headers)

    assert not_modified.status_code == 304
    assert next_page.status_code == 200
    assert next_page.json()["items"][0]["metricCode"] == "revenue"
    assert repository.fact_calls[-1]["after_metric_code"] == "assets"
    assert changed_filter.status_code == 409
    assert changed_filter.json()["code"] == "cursor-mismatch"
    assert not_visible.status_code == 404
    assert not_visible.json()["code"] == "financial-report-not-found"


def test_financial_metric_page_reads_frozen_provider_values_and_binds_cursor(
    configured_environment: None,
) -> None:
    """供应商指标页必须固定来源、方法学、双时态视图和报告期升序复合游标。"""
    settings = load_settings()
    repository = PublishedFinancialRepository(
        (),
        metrics=(_provider_metric("1"), _provider_metric("2")),
    )
    client = TestClient(
        create_app(
            settings=settings,
            repository=cast(SectorMarketDataRepository, object()),
            financial_repository=repository,
        )
    )
    headers = {"Authorization": f"Bearer {settings.internal_api_bearer_token.get_secret_value()}"}
    path = (
        "/internal/v1/equities/SSE/600519/financial-metrics?origin=PROVIDER_REPORTED"
        "&methodologyCode=eastmoney.provider-metric&methodologyVersion=2"
        f"&metric=net_income&metric=roe&limit=1&dataVersion={_PROVIDER_METRIC_VERSION}"
    )

    first = client.get(path, headers=headers)

    assert first.status_code == 200
    assert first.json()["origin"] == "PROVIDER_REPORTED"
    assert first.json()["items"][0]["metricCode"] == "net_income"
    assert first.json()["items"][0]["formulaVersion"] is None
    assert first.json()["items"][0]["value"] == "12.34"
    assert first.json()["nextCursor"]
    assert repository.metric_calls[0]["limit"] == 2
    assert repository.metric_calls[0]["metric_codes"] == ("net_income", "roe")

    not_modified = client.get(path, headers={**headers, "If-None-Match": first.headers["etag"]})
    next_page = client.get(f"{path}&cursor={first.json()['nextCursor']}", headers=headers)
    changed_scope = client.get(
        f"{path}&cursor={first.json()['nextCursor']}&basis=TTM",
        headers=headers,
    )

    assert not_modified.status_code == 304
    assert next_page.status_code == 200
    assert next_page.json()["items"][0]["metricCode"] == "roe"
    assert repository.metric_calls[-1]["after_metric_code"] == "net_income"
    assert changed_scope.status_code == 409
    assert changed_scope.json()["code"] == "cursor-mismatch"


def test_financial_metric_page_reads_platform_derived_values_without_mixing_origin(
    configured_environment: None,
) -> None:
    """平台派生页必须选择独立方法学和 publication，并暴露固定公式版本。"""
    settings = load_settings()
    repository = PublishedFinancialRepository(
        (),
        derived_metrics=(_derived_metric(),),
    )
    client = TestClient(
        create_app(
            settings=settings,
            repository=cast(SectorMarketDataRepository, object()),
            financial_repository=repository,
        )
    )
    headers = {"Authorization": f"Bearer {settings.internal_api_bearer_token.get_secret_value()}"}
    path = (
        "/internal/v1/equities/SSE/600519/financial-metrics?origin=PLATFORM_DERIVED"
        "&methodologyCode=platform.financial-derivation&methodologyVersion=1"
        f"&metric=platform.operating_revenue.ttm&basis=TTM"
        f"&dataVersion={_DERIVED_METRIC_VERSION}"
    )

    response = client.get(path, headers=headers)

    assert response.status_code == 200
    assert response.json()["origin"] == "PLATFORM_DERIVED"
    assert response.json()["items"] == [
        {
            "metricCode": "platform.operating_revenue.ttm",
            "label": "营业收入（TTM）",
            "origin": "PLATFORM_DERIVED",
            "reportPeriod": "2026-03-31",
            "periodBasis": "TTM",
            "statementScope": "CONSOLIDATED",
            "value": "680.00",
            "unit": "yuan",
            "currency": "CNY",
            "currencyNullReason": None,
            "methodologyCode": "platform.financial-derivation",
            "methodologyVersion": 1,
            "formulaVersion": 1,
            "effectiveFrom": "2026-04-28",
            "knownFrom": "2026-04-29T08:00:00Z",
            "knowledgeBasis": "OBSERVED_AT",
            "knowledgeConfidence": "CONSERVATIVE",
            "observedAt": "2026-04-28T08:00:00Z",
            "revision": 1,
        }
    ]
    assert repository.derived_metric_calls[0]["period_bases"] == ("TTM",)
    assert not repository.metric_calls


def test_valuation_page_reads_frozen_observations_and_binds_cursor(
    configured_environment: None,
) -> None:
    """估值页必须保留上游观察 finality，并拒绝跨指标集或窗口复用游标。"""
    settings = load_settings()
    repository = PublishedFinancialRepository(
        (),
        valuations=(_valuation("1"), _valuation("2")),
    )
    client = TestClient(
        create_app(
            settings=settings,
            repository=cast(SectorMarketDataRepository, object()),
            financial_repository=repository,
        )
    )
    headers = {"Authorization": f"Bearer {settings.internal_api_bearer_token.get_secret_value()}"}
    path = (
        "/internal/v1/equities/SSE/600519/valuations?methodologyCode=eastmoney.valuation"
        "&methodologyVersion=2&metric=pe_ttm&metric=pb"
        f"&start=2026-07-01&end=2026-07-31&limit=1&dataVersion={_VALUATION_VERSION}"
    )

    first = client.get(path, headers=headers)

    assert first.status_code == 200
    assert first.json()["items"][0]["metricCode"] == "pe_ttm"
    assert first.json()["items"][0]["finality"] == "PROVIDER_OBSERVATION"
    assert first.json()["items"][0]["value"] == "18.2500"
    assert first.json()["nextCursor"]
    assert repository.valuation_calls[0]["limit"] == 2

    next_page = client.get(f"{path}&cursor={first.json()['nextCursor']}", headers=headers)
    changed_scope = client.get(
        f"{path}&cursor={first.json()['nextCursor']}&end=2026-07-30",
        headers=headers,
    )

    assert next_page.status_code == 200
    assert next_page.json()["items"][0]["metricCode"] == "pb"
    assert repository.valuation_calls[-1]["after_metric_code"] == "pe_ttm"
    assert changed_scope.status_code == 409
    assert changed_scope.json()["code"] == "cursor-mismatch"


def _publication() -> FinancialPublicationSnapshot:
    """构造已验证的唯一测试发布快照，使响应可回链到固定 `data_version`。"""
    return FinancialPublicationSnapshot(
        data_version=_REPORT_VERSION,
        security_id=8,
        instrument_id=UUID("30000000-0000-4000-8000-000000000016"),
        methodology_id=UUID("20000000-0000-4000-8000-000000000016"),
        capability="financial.report",
        methodology_code="eastmoney.reported",
        methodology_version=2,
        source_code="eastmoney",
        published_at=datetime(2026, 7, 28, 8, 5, tzinfo=UTC),
        effective_as_of=date(2026, 7, 27),
        knowledge_cutoff=datetime(2026, 7, 28, 8, tzinfo=UTC),
        row_count=2,
        content_sha256="a" * 64,
    )


def _metric_publication() -> FinancialPublicationSnapshot:
    """构造供应商直接指标的独立测试发布，不与三表 dataVersion 或方法学混用。"""
    return FinancialPublicationSnapshot(
        data_version=_PROVIDER_METRIC_VERSION,
        security_id=8,
        instrument_id=UUID("30000000-0000-4000-8000-000000000016"),
        methodology_id=UUID("20000000-0000-4000-8000-000000000017"),
        capability="financial.provider-metric",
        methodology_code="eastmoney.provider-metric",
        methodology_version=2,
        source_code="eastmoney",
        published_at=datetime(2026, 7, 28, 8, 6, tzinfo=UTC),
        effective_as_of=date(2026, 7, 27),
        knowledge_cutoff=datetime(2026, 7, 28, 8, tzinfo=UTC),
        row_count=2,
        content_sha256="b" * 64,
    )


def _valuation_publication() -> FinancialPublicationSnapshot:
    """构造历史估值独立测试发布，确保估值读取不会误用指标的消费者版本。"""
    return FinancialPublicationSnapshot(
        data_version=_VALUATION_VERSION,
        security_id=8,
        instrument_id=UUID("30000000-0000-4000-8000-000000000016"),
        methodology_id=UUID("20000000-0000-4000-8000-000000000018"),
        capability="financial.valuation",
        methodology_code="eastmoney.valuation",
        methodology_version=2,
        source_code="eastmoney",
        published_at=datetime(2026, 7, 28, 8, 7, tzinfo=UTC),
        effective_as_of=date(2026, 7, 27),
        knowledge_cutoff=datetime(2026, 7, 28, 8, tzinfo=UTC),
        row_count=2,
        content_sha256="c" * 64,
    )


def _derived_metric_publication() -> FinancialPublicationSnapshot:
    """构造平台派生指标独立发布，固定方法学版本且不冒充外部来源口径。"""
    return FinancialPublicationSnapshot(
        data_version=_DERIVED_METRIC_VERSION,
        security_id=8,
        instrument_id=UUID("30000000-0000-4000-8000-000000000016"),
        methodology_id=UUID("20000000-0000-4000-8000-000000000019"),
        capability="financial.derived-metric",
        methodology_code="platform.financial-derivation",
        methodology_version=1,
        source_code="platform",
        published_at=datetime(2026, 4, 29, 8, tzinfo=UTC),
        effective_as_of=date(2026, 4, 28),
        knowledge_cutoff=datetime(2026, 4, 29, 8, tzinfo=UTC),
        row_count=1,
        content_sha256="d" * 64,
    )


def _report(suffix: str) -> PublishedFinancialReport:
    """构造按报告期和报表类型稳定排序的可见已发布报表头。"""
    return PublishedFinancialReport(
        report_ref=UUID(f"50000000-0000-4000-8000-00000000000{suffix}"),
        statement_type="INCOME_STATEMENT",
        report_period=date(2026, 3, 31) if suffix == "1" else date(2025, 12, 31),
        period_basis="YEAR_TO_DATE",
        statement_scope="CONSOLIDATED",
        currency="CNY",
        currency_null_reason=None,
        report_type="QUARTERLY",
        audit_status="UNAUDITED",
        announcement_date=date(2026, 4, 28),
        provider_update_at=datetime(2026, 4, 28, 8, tzinfo=UTC),
        effective_from=date(2026, 4, 28),
        effective_to=None,
        known_from=datetime(2026, 4, 28, 8, tzinfo=UTC),
        known_to=None,
        knowledge_basis="ANNOUNCEMENT",
        knowledge_confidence="HIGH",
        observed_at=datetime(2026, 4, 28, 8, tzinfo=UTC),
        revision=1,
        quality_status="passed",
    )


def _fact(suffix: str) -> PublishedFinancialStatementFact:
    """构造按字段代码排序的已治理行项目，保留精确小数和受控单位信息。"""
    return PublishedFinancialStatementFact(
        metric_code="assets" if suffix == "1" else "revenue",
        label="资产合计" if suffix == "1" else "营业收入",
        value=Decimal("123.45") if suffix == "1" else Decimal("67.89"),
        null_reason=None,
        currency="CNY",
        currency_null_reason=None,
        original_unit="yuan",
        canonical_unit="yuan",
        scale_factor=Decimal("1.0000"),
        sign_convention="AS_REPORTED",
    )


def _provider_metric(suffix: str) -> PublishedFinancialMetric:
    """构造报告期升序供应商指标，用于验证不与平台派生指标混合的响应投影。"""
    return PublishedFinancialMetric(
        metric_code="net_income" if suffix == "1" else "roe",
        label="净利润" if suffix == "1" else "净资产收益率",
        origin="PROVIDER_REPORTED",
        report_period=date(2026, 3, 31),
        period_basis="YEAR_TO_DATE",
        statement_scope="UNKNOWN",
        value=Decimal("12.34") if suffix == "1" else Decimal("10.2500"),
        unit="source_unknown",
        currency=None,
        currency_null_reason="UNKNOWN_SOURCE",
        formula_version=None,
        effective_from=date(2026, 3, 31),
        known_from=datetime(2026, 4, 28, 8, tzinfo=UTC),
        knowledge_basis="OBSERVED_AT",
        knowledge_confidence="CONSERVATIVE",
        observed_at=datetime(2026, 4, 28, 8, tzinfo=UTC),
        revision=1,
    )


def _derived_metric() -> PublishedFinancialMetric:
    """构造带固定公式版本的平台派生指标，验证来源隔离与精确数值投影。"""
    return PublishedFinancialMetric(
        metric_code="platform.operating_revenue.ttm",
        label="营业收入（TTM）",
        origin="PLATFORM_DERIVED",
        report_period=date(2026, 3, 31),
        period_basis="TTM",
        statement_scope="CONSOLIDATED",
        value=Decimal("680.00"),
        unit="yuan",
        currency="CNY",
        currency_null_reason=None,
        formula_version=1,
        effective_from=date(2026, 4, 28),
        known_from=datetime(2026, 4, 29, 8, tzinfo=UTC),
        knowledge_basis="OBSERVED_AT",
        knowledge_confidence="CONSERVATIVE",
        observed_at=datetime(2026, 4, 28, 8, tzinfo=UTC),
        revision=1,
    )


def _valuation(suffix: str) -> PublishedValuationObservation:
    """构造按日期升序的估值观察，检验价格类序列的稳定续页。"""
    return PublishedValuationObservation(
        observation_date=date(2026, 7, 26) if suffix == "1" else date(2026, 7, 27),
        metric_code="pe_ttm" if suffix == "1" else "pb",
        value=Decimal("18.2500") if suffix == "1" else Decimal("4.1250"),
        unit="ratio",
        currency=None,
        currency_null_reason="NOT_APPLICABLE",
        finality="PROVIDER_OBSERVATION",
        effective_from=date(2026, 7, 26) if suffix == "1" else date(2026, 7, 27),
        known_from=datetime(2026, 7, 28, 8, tzinfo=UTC),
        knowledge_basis="OBSERVED_AT",
        knowledge_confidence="CONSERVATIVE",
        observed_at=datetime(2026, 7, 28, 8, tzinfo=UTC),
        revision=1,
    )
