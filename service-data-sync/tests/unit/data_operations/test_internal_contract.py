"""数据运维内部合同路由与输入边界回归测试。"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from threading import Event, Thread
from typing import Any, cast
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from service_data_sync.application.ports.data_source import ProviderBatch, SourceRequest
from service_data_sync.application.ports.sector_market_data import SectorMarketDataRepository
from service_data_sync.application.source_registry import SourceRegistry
from service_data_sync.bootstrap.settings import load_settings
from service_data_sync.infrastructure.data_operations.control_plane import (
    DataOperationsControlPlane,
    ExecutionClaim,
    ExecutionOutcome,
    OperationProblem,
    build_catalog,
)
from service_data_sync.infrastructure.database.fenced_execution import current_fenced_execution
from service_data_sync.interfaces.internal_sector_api import create_app


class FakeSectorRepository:
    """提供空板块读取端口，使本测试只验证数据运维 POST 路由注册。"""


class FakeEtfProvider:
    """声明四个 ETF 真实同步能力，使目录预检不依赖网络。"""

    provider_id = "akshare"

    def capabilities(self) -> frozenset[str]:
        """返回 ETF 目录、状态、日线和净值的完整 capability 集。"""
        return frozenset(
            {
                "fund.etf.master",
                "fund.etf.trading_state",
                "fund.etf.bar.1d.raw",
                "fund.etf.nav.1d.reported",
            }
        )

    async def fetch(self, request: SourceRequest) -> ProviderBatch:
        """预检不得访问来源；若错误调用则立即暴露测试失败。"""
        del request
        raise AssertionError("ETF preflight must not fetch provider data")


class FakeP0MarketProvider:
    """声明首批两融与真实合约日线能力，预检不得访问真实 AKShare。"""

    provider_id = "akshare"

    def capabilities(self) -> frozenset[str]:
        """返回当前可经统一 dispatcher 调用的四个已审核 P0 capability。"""
        return frozenset(
            {
                "market.margin.market.1d.reported",
                "market.margin.security.1d.reported",
                "market.margin.eligibility.reported",
                "derivative.bar.1d.reported",
            }
        )

    async def fetch(self, request: SourceRequest) -> ProviderBatch:
        """预检不允许抓取来源；若错误触发即失败。"""
        del request
        raise AssertionError("P0 market preflight must not fetch provider data")


class FakeSectorMembershipProvider:
    """声明东财板块成分能力，预检不得触发真实网络。"""

    provider_id = "akshare-eastmoney-sector-membership"

    def capabilities(self) -> frozenset[str]:
        """返回唯一板块成分当前快照能力。"""
        return frozenset({"sector.membership.snapshot.raw"})

    async def fetch(self, request: SourceRequest) -> ProviderBatch:
        """预检若访问来源则立即失败。"""
        del request
        raise AssertionError("sector membership preflight must not fetch provider data")


class FakeControlPlane:
    """提供合同路由最小响应，避免路由声明测试连接真实 PostgreSQL。"""

    def overview(self) -> dict[str, Any]:
        """返回合同总览最小字段。"""
        return {"ok": True}

    def list_datasets(self, _request: dict[str, Any]) -> dict[str, Any]:
        """返回空目录页。"""
        return {
            "items": [],
            "nextCursor": None,
            "totalEstimate": 0,
            "generatedAt": "2026-07-29T00:00:00Z",
        }

    def dataset_detail(self, _dataset_code: str) -> dict[str, Any]:
        """返回空详情对象。"""
        return {"ok": True}

    def preflight(self, _targets: list[dict[str, Any]]) -> dict[str, Any]:
        """返回空预检对象。"""
        return {"ok": True}

    def submit_command(self, **_kwargs: Any) -> dict[str, Any]:
        """返回命令受理对象。"""
        return {"ok": True}

    def command_detail(self, _command_id: UUID) -> dict[str, Any]:
        """返回命令详情对象。"""
        return {"ok": True}

    def cancel_command(self, **_kwargs: Any) -> dict[str, Any]:
        """返回取消受理对象。"""
        return {"ok": True}

    def retry_command(self, **_kwargs: Any) -> dict[str, Any]:
        """返回重试受理对象。"""
        return {"ok": True}

    def list_runs(self, _request: dict[str, Any]) -> dict[str, Any]:
        """返回空运行页。"""
        return {"items": [], "nextCursor": None}

    def run_detail(self, _request: dict[str, Any]) -> dict[str, Any]:
        """返回运行详情对象。"""
        return {"ok": True}

    def list_health_evaluations(self, _request: dict[str, Any]) -> dict[str, Any]:
        """返回空健康评估页。"""
        return {"items": [], "nextCursor": None}

    def health_evaluation_detail(self, _request: dict[str, Any]) -> dict[str, Any]:
        """返回健康评估详情对象。"""
        return {"ok": True}

    def submit_health_check(self, **_kwargs: Any) -> dict[str, Any]:
        """返回健康检查受理对象。"""
        return {"ok": True}

    def health_check_detail(self, _health_check_id: UUID) -> dict[str, Any]:
        """返回健康检查详情对象。"""
        return {"ok": True}

    def list_schedules(self, _request: dict[str, Any]) -> dict[str, Any]:
        """返回空计划页。"""
        return {"items": [], "nextCursor": None}

    def upsert_schedule(self, **_kwargs: Any) -> dict[str, Any]:
        """返回计划对象。"""
        return {"ok": True}

    def set_schedule_enabled(self, **_kwargs: Any) -> dict[str, Any]:
        """返回启停后的计划对象。"""
        return {"ok": True}

    def list_events(self, _request: dict[str, Any]) -> dict[str, Any]:
        """返回空事件页。"""
        return {"items": [], "nextCursor": None}

    def _uuid_field(self, values: dict[str, Any], key: str) -> UUID:
        """模拟控制面的 UUID 参数校验。"""
        try:
            return UUID(str(values[key]))
        except (KeyError, ValueError) as error:
            raise OperationProblem(
                status=400, code="validation-error", detail="UUID is invalid"
            ) from error


def test_operations_contract_registers_exactly_eighteen_post_routes(configured_environment) -> None:
    """0022 的所有控制面路径必须存在且只接受 POST。"""
    del configured_environment
    application = create_app(
        settings=load_settings(),
        repository=cast(SectorMarketDataRepository, FakeSectorRepository()),
        data_operations_control_plane=cast(DataOperationsControlPlane, FakeControlPlane()),
    )
    routes = [
        route
        for route in application.routes
        if getattr(route, "path", "").startswith("/internal/v1/data-operations/")
    ]

    assert len(routes) == 18
    assert all(getattr(route, "methods", set()) == {"POST"} for route in routes)


def test_operations_routes_require_service_bearer(configured_environment) -> None:
    """LOCAL/TEST 未拆分身份时，数据运维总览只能回退既有内部 bearer。"""
    del configured_environment
    settings = load_settings()
    client = TestClient(
        create_app(
            settings=settings,
            repository=cast(SectorMarketDataRepository, FakeSectorRepository()),
            data_operations_control_plane=cast(DataOperationsControlPlane, FakeControlPlane()),
        )
    )

    unauthorized = client.post("/internal/v1/data-operations/overview/query", json={})
    authorized = client.post(
        "/internal/v1/data-operations/overview/query",
        json={},
        headers={
            "Authorization": f"Bearer {settings.internal_api_bearer_token.get_secret_value()}"
        },
    )

    assert unauthorized.status_code == 401
    assert authorized.status_code == 200


def test_operations_routes_separate_read_and_mutation_service_identities(
    configured_environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    """读 bearer 只能访问查询/预检，operations bearer 才能提交有副作用的控制面动作。"""
    del configured_environment
    read_token = "read-service-token-000000000000000000000000000001"
    operations_token = "operations-service-token-0000000000000000000000001"
    monkeypatch.setenv("DATA_SYNC_INTERNAL_READ_API_BEARER_TOKEN", read_token)
    monkeypatch.setenv("DATA_SYNC_INTERNAL_OPERATIONS_API_BEARER_TOKEN", operations_token)
    settings = load_settings()
    client = TestClient(
        create_app(
            settings=settings,
            repository=cast(SectorMarketDataRepository, FakeSectorRepository()),
            data_operations_control_plane=cast(DataOperationsControlPlane, FakeControlPlane()),
        )
    )
    read_headers = {"Authorization": f"Bearer {read_token}"}
    operations_headers = {
        "Authorization": f"Bearer {operations_token}",
        "Idempotency-Key": "operations-auth-boundary",
    }
    legacy_headers = {
        "Authorization": f"Bearer {settings.internal_api_bearer_token.get_secret_value()}"
    }

    assert (
        client.post(
            "/internal/v1/data-operations/overview/query", json={}, headers=read_headers
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/internal/v1/data-operations/overview/query", json={}, headers=operations_headers
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/internal/v1/data-operations/overview/query", json={}, headers=legacy_headers
        ).status_code
        == 401
    )
    # 旧内部读取端点仍由通用 bearer 保护；其业务依赖在该路由替身中可失败，但认证不得改为 401。
    assert client.get("/internal/v1/sectors", headers=legacy_headers).status_code != 401
    assert client.get("/internal/v1/sectors", headers=read_headers).status_code == 401
    assert (
        client.post(
            "/internal/v1/data-operations/commands/submit", json={}, headers=read_headers
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/internal/v1/data-operations/commands/submit",
            json={},
            headers=operations_headers,
        ).status_code
        == 202
    )


def test_catalog_validates_orm_manifest_and_providerless_derivation(configured_environment) -> None:
    """目录校验 ORM manifest，并把 providerless 派生任务声明为零来源调用。"""
    del configured_environment
    catalog = build_catalog(load_settings(), SourceRegistry())
    derived = catalog["financial.derived-metric"]
    resolved = catalog["equity.master.resolved"]

    assert len(catalog) == 46
    assert derived.model_only is False
    assert derived.providerless is True
    assert derived.dispatcher_ready is True
    assert derived.modes == ("FULL", "INCREMENTAL")
    assert derived.schedule_modes == ("INCREMENTAL",)
    assert derived.selector_kinds == ("INSTRUMENT",)
    assert resolved.providerless is True
    assert resolved.dispatcher_ready is True
    assert resolved.modes == ("FULL",)
    assert resolved.schedule_modes == ()
    assert resolved.selector_kinds == ("GLOBAL",)


def test_catalog_registers_complete_canonical_dataset_codes(configured_environment) -> None:
    """完整清单必须覆盖 46 个独立运维资产，包含 resolved 主数据与指数边界。"""
    del configured_environment
    catalog = build_catalog(load_settings(), SourceRegistry())

    assert set(catalog) == {
        "derivative.bar.1d.reported",
        "equity.adjustment_factor",
        "equity.bar.1d.raw",
        "equity.bar.1mo.raw",
        "equity.bar.1w.raw",
        "equity.block_trade.execution.reported",
        "equity.corporate_action",
        "equity.corporate_event.earnings.reported",
        "equity.dragon_tiger.disclosure.reported",
        "equity.discovery.eod",
        "equity.lifecycle.explicit",
        "equity.master.cn-a",
        "equity.master.resolved",
        "equity.profile",
        "equity.share_capital.reported",
        "equity.trading_status.1d",
        "financial.derived-metric",
        "financial.provider-metric",
        "financial.report",
        "financial.valuation",
        "fund.etf.bar.1d.reported",
        "fund.etf.nav.1d.reported",
        "fund.etf.profile.reported",
        "fund.etf.trading_state.reported",
        "index.cni.catalog.snapshot",
        "index.cni.constituent.snapshot",
        "index.cni.weight.snapshot",
        "index.csi.catalog.snapshot",
        "index.csi.constituent.snapshot",
        "index.csi.weight.snapshot",
        "market.margin.eligibility.reported",
        "market.margin.market.1d.reported",
        "market.margin.security.1d.reported",
        "market.overview-and-sectors.bundle",
        "market.stock_connect.market_stat.research",
        "market.stock_connect.overview.bundle",
        "money_flow.daily",
        "money_flow.ranking",
        "sector.bar.1d.raw",
        "sector.bar.1mo.raw",
        "sector.bar.1w.raw",
        "sector.catalog.raw",
        "sector.membership.release",
        "sector.quote.eod.snapshot",
        "sector.sw.taxonomy",
        "sector.sw2021.membership.snapshot",
    }


def test_membership_preflight_rejects_historical_observation_before_queue(
    configured_environment: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """当前集合来源不得把今天的数据倒填为历史 observationDate。"""
    del configured_environment
    monkeypatch.setenv("DATA_SYNC_AKSHARE_ENABLED", "true")
    monkeypatch.setenv("DATA_SYNC_SECTOR_ENABLED", "true")
    monkeypatch.setenv("DATA_SYNC_SECTOR_MEMBERSHIP_ENABLED", "true")
    registry = SourceRegistry()
    registry.register(FakeSectorMembershipProvider())
    control_plane = object.__new__(DataOperationsControlPlane)
    control_plane._catalog = build_catalog(load_settings(), registry)
    control_plane._source_registry = registry
    control_plane._now = lambda: datetime(2099, 1, 1, 8, tzinfo=UTC)
    target = control_plane._validate_targets(
        [
            {
                "datasetCode": "sector.membership.release",
                "mode": "OBSERVATION_DATE",
                "selector": {
                    "kind": "SECTOR",
                    "scheme": "eastmoney.industry",
                    "sectorCode": "BK1507",
                },
                "observationDate": "2026-07-29",
            }
        ]
    )[0]

    result = control_plane._preflight_target(target)

    assert result["eligible"] is False
    assert result["estimatedProviderCalls"] == 0
    assert result["warnings"] == ["东财板块成分仅支持当前观察日，不能伪装为历史快照"]


def test_catalog_keeps_declared_source_when_adapter_is_disabled(configured_environment) -> None:
    """来源开关关闭时仍展示已登记血缘，但明确标记当前绑定未生效。"""
    del configured_environment
    control_plane = object.__new__(DataOperationsControlPlane)
    control_plane._now = lambda: datetime(2099, 1, 1, 8, tzinfo=UTC)
    control_plane._source_registry = SourceRegistry()
    definition = build_catalog(load_settings(), SourceRegistry())["fund.etf.profile.reported"]

    bindings = control_plane._source_bindings(definition, ())

    assert bindings == [
        {
            "providerId": "akshare",
            "upstreamSource": "sse-szse.official-etf-directory",
            "sourceDataset": "fund.etf.master",
            "adapterId": "akshare:fund.etf.master",
            "methodologyCode": "fund.etf.profile.reported",
            "methodologyVersion": 1,
            "approvalStatus": "CANDIDATE",
            "role": "PRIMARY",
            "effective": False,
        }
    ]


def test_etf_catalog_preflight_marks_all_four_real_executors_eligible(
    configured_environment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """四个 ETF 数据集必须同时具备 provider、dispatcher 和可运行目标模式。"""
    del configured_environment
    monkeypatch.setenv("DATA_SYNC_AKSHARE_ENABLED", "true")
    registry = SourceRegistry()
    registry.register(FakeEtfProvider())
    catalog = build_catalog(load_settings(), registry)
    control_plane = object.__new__(DataOperationsControlPlane)
    control_plane._now = lambda: datetime(2099, 1, 1, 8, tzinfo=UTC)
    control_plane._catalog = catalog
    control_plane._source_registry = registry
    selectors = {
        "fund.etf.profile.reported": {
            "kind": "ETF",
            "operation": "MASTER",
            "venue": "SSE",
            "etf": None,
        },
        "fund.etf.trading_state.reported": {
            "kind": "ETF",
            "operation": "STATUS",
            "venue": None,
            "etf": "SSE.510300",
        },
        "fund.etf.bar.1d.reported": {
            "kind": "ETF",
            "operation": "BARS",
            "venue": None,
            "etf": "SSE.510300",
        },
        "fund.etf.nav.1d.reported": {
            "kind": "ETF",
            "operation": "NAV",
            "venue": None,
            "etf": "SSE.510300",
        },
    }
    expected_sources = {
        "fund.etf.profile.reported": "sse-szse.official-etf-directory",
        "fund.etf.trading_state.reported": "eastmoney.etf.nav-json",
        "fund.etf.bar.1d.reported": "tencent.etf-kline",
        "fund.etf.nav.1d.reported": "eastmoney.etf.nav-json",
    }
    expected_titles = {
        "fund.etf.profile.reported": "ETF 产品资料",
        "fund.etf.trading_state.reported": "ETF 报告状态",
        "fund.etf.bar.1d.reported": "ETF 日线行情",
        "fund.etf.nav.1d.reported": "ETF 日频净值",
    }

    for dataset_code, selector in selectors.items():
        result = control_plane._preflight_target(
            {
                "datasetCode": dataset_code,
                "mode": "FULL",
                "selector": selector,
                "dateFrom": None,
                "dateTo": None,
                "observationDate": None,
            }
        )
        assert result["eligible"] is True
        assert catalog[dataset_code].dispatcher_ready is True
        assert catalog[dataset_code].provider_id == "akshare"
        assert catalog[dataset_code].upstream_source == expected_sources[dataset_code]
        assert catalog[dataset_code].lifecycle == "CANDIDATE"
        assert catalog[dataset_code].approval_status == "CANDIDATE"
        assert catalog[dataset_code].display_name == expected_titles[dataset_code]

    assert (
        catalog["fund.etf.trading_state.reported"].description == "ETF 申购、赎回等来源报告状态修订"
    )


def test_etf_single_selector_rejects_conflicting_explicit_venue() -> None:
    """单只 ETF 的 venue 与 qualified identifier 必须一致，不能静默忽略任一身份。"""
    control_plane = object.__new__(DataOperationsControlPlane)
    control_plane._now = lambda: datetime(2099, 1, 1, 8, tzinfo=UTC)

    with pytest.raises(OperationProblem) as raised:
        control_plane._etf_selector(
            {
                "kind": "ETF",
                "operation": "BARS",
                "venue": "SZSE",
                "etf": "SSE.510300",
            },
            profile_versions="FROZEN",
        )

    assert raised.value.code == "invalid-target-selector"


def test_margin_and_derivative_preflight_freeze_batched_windows(
    configured_environment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """四个 P0 数据集必须可调度，并把日期窗和逐日调用规模在预检时固定下来。"""
    del configured_environment
    monkeypatch.setenv("DATA_SYNC_AKSHARE_ENABLED", "true")
    registry = SourceRegistry()
    registry.register(FakeP0MarketProvider())
    control_plane = object.__new__(DataOperationsControlPlane)
    control_plane._now = lambda: datetime(2026, 8, 1, 8, tzinfo=UTC)
    control_plane._catalog = build_catalog(load_settings(), registry)
    control_plane._source_registry = registry
    targets = control_plane._validate_targets(
        [
            {
                "datasetCode": "market.margin.market.1d.reported",
                "mode": "DATE_RANGE",
                "selector": {
                    "kind": "MARGIN",
                    "operation": "MARKET",
                    "venue": "SSE",
                    "security": None,
                },
                "dateFrom": "2026-01-01",
                "dateTo": "2026-01-06",
            },
            {
                "datasetCode": "market.margin.security.1d.reported",
                "mode": "DATE_RANGE",
                "selector": {
                    "kind": "MARGIN",
                    "operation": "SECURITY",
                    "venue": "SZSE",
                    "security": None,
                },
                "dateFrom": "2026-01-01",
                "dateTo": "2026-01-06",
            },
            {
                "datasetCode": "market.margin.eligibility.reported",
                "mode": "OBSERVATION_DATE",
                "selector": {
                    "kind": "MARGIN",
                    "operation": "ELIGIBILITY",
                    "venue": "SZSE",
                    "security": None,
                },
                "observationDate": "2026-01-06",
            },
            {
                "datasetCode": "derivative.bar.1d.reported",
                "mode": "DATE_RANGE",
                "selector": {"kind": "CONTRACT", "venue": "CFFEX", "contract": "IF2608"},
                "dateFrom": "2026-01-01",
                "dateTo": "2026-01-06",
            },
        ]
    )

    results = [control_plane._preflight_target(target) for target in targets]

    assert [item["eligible"] for item in results] == [True, True, True, True]
    assert [item["estimatedPartitions"] for item in results] == [2, 2, 1, 1]
    assert [item["estimatedProviderCalls"] for item in results] == [2, 6, 1, 1]
    assert all(
        control_plane._catalog[str(target["datasetCode"])].dispatcher_ready for target in targets
    )
    assert all(
        item["resolvedDateFrom"] is not None and item["resolvedDateTo"] is not None
        for item in results
    )


@pytest.mark.parametrize(
    ("target", "code"),
    [
        (
            {
                "datasetCode": "market.margin.market.1d.reported",
                "mode": "FULL",
                "selector": {
                    "kind": "MARGIN",
                    "operation": "SECURITY",
                    "venue": "SSE",
                    "security": None,
                },
            },
            "margin-dataset-operation-mismatch",
        ),
        (
            {
                "datasetCode": "market.margin.security.1d.reported",
                "mode": "FULL",
                "selector": {
                    "kind": "MARGIN",
                    "operation": "SECURITY",
                    "venue": "SSE",
                    "security": {"kind": "INSTRUMENT", "exchange": "SSE", "symbol": "600000"},
                },
            },
            "margin-security-selector-unsupported",
        ),
        (
            {
                "datasetCode": "market.margin.eligibility.reported",
                "mode": "FULL",
                "selector": {
                    "kind": "MARGIN",
                    "operation": "ELIGIBILITY",
                    "venue": "SSE",
                    "security": None,
                },
            },
            "margin-eligibility-venue-unsupported",
        ),
    ],
)
def test_margin_target_validation_rejects_unmapped_or_false_scope(
    target: dict[str, Any],
    code: str,
    configured_environment,
) -> None:
    """两融命令必须拒绝 dataset 错配、未实现单证券过滤和没有来源的 SSE 资格名单。"""
    del configured_environment
    control_plane = object.__new__(DataOperationsControlPlane)
    control_plane._now = lambda: datetime(2026, 8, 1, 8, tzinfo=UTC)
    control_plane._catalog = build_catalog(load_settings(), SourceRegistry())

    with pytest.raises(OperationProblem) as raised:
        control_plane._validate_targets([target])

    assert raised.value.code == code


def test_index_research_stock_connect_research_and_bse_margin_targets_are_strictly_bound(
    configured_environment,
) -> None:
    """三个新增 research 路径必须与唯一数据集、能力和真实场所范围一一绑定。"""
    del configured_environment
    control_plane = object.__new__(DataOperationsControlPlane)
    control_plane._now = lambda: datetime(2026, 8, 1, 8, tzinfo=UTC)
    control_plane._catalog = build_catalog(load_settings(), SourceRegistry())

    targets = control_plane._validate_targets(
        [
            {
                "datasetCode": "market.margin.eligibility.reported",
                "mode": "OBSERVATION_DATE",
                "selector": {
                    "kind": "MARGIN",
                    "operation": "ELIGIBILITY",
                    "venue": "BSE",
                    "security": None,
                },
                "observationDate": "2026-07-31",
            },
            {
                "datasetCode": "index.csi.catalog.snapshot",
                "mode": "FULL",
                "selector": {
                    "kind": "INDEX",
                    "administrator": "CSI",
                    "capability": "index.catalog.snapshot",
                    "indexCode": None,
                },
            },
            {
                "datasetCode": "index.cni.constituent.snapshot",
                "mode": "FULL",
                "selector": {
                    "kind": "INDEX",
                    "administrator": "CNI",
                    "capability": "index.constituent.snapshot",
                    "indexCode": "AITCNYG",
                },
            },
            {
                "datasetCode": "market.stock_connect.market_stat.research",
                "mode": "DATE_RANGE",
                "selector": {
                    "kind": "STOCK_CONNECT_RESEARCH",
                    "operation": "MARKET_STAT",
                    "channel": "ALL",
                    "direction": None,
                },
                "dateFrom": "2026-07-30",
                "dateTo": "2026-07-31",
            },
        ]
    )

    assert [target["selector"]["kind"] for target in targets] == [
        "MARGIN",
        "INDEX",
        "INDEX",
        "STOCK_CONNECT_RESEARCH",
    ]

    with pytest.raises(OperationProblem) as index_mismatch:
        control_plane._validate_targets(
            [
                {
                    "datasetCode": "index.csi.catalog.snapshot",
                    "mode": "FULL",
                    "selector": {
                        "kind": "INDEX",
                        "administrator": "CNI",
                        "capability": "index.catalog.snapshot",
                        "indexCode": None,
                    },
                }
            ]
        )
    with pytest.raises(OperationProblem) as research_mismatch:
        control_plane._validate_targets(
            [
                {
                    "datasetCode": "market.stock_connect.overview.bundle",
                    "mode": "DATE_RANGE",
                    "selector": {
                        "kind": "STOCK_CONNECT_RESEARCH",
                        "operation": "MARKET_STAT",
                        "channel": "SH",
                        "direction": "NORTHBOUND",
                    },
                    "dateFrom": "2026-07-31",
                    "dateTo": "2026-07-31",
                }
            ]
        )

    assert index_mismatch.value.code == "index-dataset-selector-mismatch"
    assert research_mismatch.value.code == "unsupported-target-selector"


@pytest.mark.parametrize(
    ("targets", "code"),
    [
        (
            [
                {
                    "datasetCode": "equity.bar.1d.raw",
                    "mode": "FULL",
                    "selector": {"kind": "GLOBAL"},
                },
                {
                    "datasetCode": "equity.bar.1d.raw",
                    "mode": "FULL",
                    "selector": {"kind": "GLOBAL"},
                },
            ],
            "duplicate-dataset-code",
        ),
        (
            [
                {
                    "datasetCode": "equity.bar.1d.raw",
                    "mode": "OBSERVATION_DATE",
                    "selector": {"kind": "GLOBAL"},
                    "observationDate": "2026-07-29",
                }
            ],
            "unsupported-sync-mode",
        ),
    ],
)
def test_target_validation_rejects_duplicate_and_unsupported_mode(
    targets: list[dict[str, Any]], code: str, configured_environment
) -> None:
    """批量 target 必须数据集唯一，模式必须来自服务端 capability。"""
    del configured_environment
    control_plane = object.__new__(DataOperationsControlPlane)
    control_plane._now = lambda: datetime(2099, 1, 1, 8, tzinfo=UTC)
    control_plane._catalog = build_catalog(load_settings(), SourceRegistry())

    with pytest.raises(OperationProblem) as raised:
        control_plane._validate_targets(targets)

    assert raised.value.code == code


@pytest.mark.parametrize(
    ("selector", "expected"),
    [
        ({"kind": "GLOBAL"}, "GLOBAL"),
        ({"kind": "INSTRUMENT", "exchange": "SSE", "symbol": "600000"}, "INSTRUMENT"),
        ({"kind": "SECTOR", "scheme": "SW", "sectorCode": "801010"}, "SECTOR"),
        ({"kind": "SCHEME", "scheme": "SW"}, "SCHEME"),
        ({"kind": "EXCHANGE", "exchange": "SSE"}, "EXCHANGE"),
        ({"kind": "CONTRACT", "venue": "CFFEX", "contract": "IF2408"}, "CONTRACT"),
        ({"kind": "ETF", "operation": "MASTER", "venue": "SSE", "etf": None}, "ETF"),
        (
            {
                "kind": "ETF",
                "operation": "MASTER",
                "scope": "ALL_VENUES",
                "venue": None,
                "etf": None,
            },
            "ETF",
        ),
        (
            {
                "kind": "MARGIN",
                "operation": "SECURITY",
                "venue": "SSE",
                "security": None,
            },
            "MARGIN",
        ),
        (
            {
                "kind": "STOCK_CONNECT",
                "operation": "MARKET",
                "channel": "SH",
                "direction": "NORTHBOUND",
            },
            "STOCK_CONNECT",
        ),
        (
            {
                "kind": "STOCK_CONNECT_RESEARCH",
                "operation": "MARKET_STAT",
                "channel": "ALL",
                "direction": None,
            },
            "STOCK_CONNECT_RESEARCH",
        ),
        ({"kind": "TRADING_EVENT", "operation": "BLOCK_TRADE"}, "TRADING_EVENT"),
        (
            {
                "kind": "INDEX",
                "administrator": "CSI",
                "capability": "index.catalog.snapshot",
                "indexCode": None,
            },
            "INDEX",
        ),
    ],
)
def test_selector_union_is_strict_and_returns_normalized_contract_shape(
    selector: dict[str, Any], expected: str, configured_environment
) -> None:
    """所有合同 selector 分支都能规范化，避免 CLI 继续携带自由 Provider 参数。"""
    del configured_environment
    control_plane = object.__new__(DataOperationsControlPlane)
    control_plane._now = lambda: datetime(2099, 1, 1, 8, tzinfo=UTC)
    definition = build_catalog(load_settings(), SourceRegistry())["equity.bar.1d.raw"]
    definition = replace(
        definition,
        selector_kinds=(
            "GLOBAL",
            "INSTRUMENT",
            "SECTOR",
            "SCHEME",
            "EXCHANGE",
            "CONTRACT",
            "ETF",
            "MARGIN",
            "STOCK_CONNECT",
            "STOCK_CONNECT_RESEARCH",
            "TRADING_EVENT",
            "INDEX",
        ),
    )

    normalized = control_plane._validate_selector(selector, definition)

    assert normalized["kind"] == expected


def test_selector_rejects_unknown_field_and_unsupported_dataset_kind(
    configured_environment,
) -> None:
    """selector 不能透传 URI、凭据或不属于数据集 capability 的种类。"""
    del configured_environment
    control_plane = object.__new__(DataOperationsControlPlane)
    control_plane._now = lambda: datetime(2099, 1, 1, 8, tzinfo=UTC)
    definition = build_catalog(load_settings(), SourceRegistry())["equity.bar.1d.raw"]

    with pytest.raises(OperationProblem) as malformed:
        control_plane._validate_selector(
            {"kind": "GLOBAL", "providerUrl": "https://unsafe.example"}, definition
        )
    with pytest.raises(OperationProblem) as unsupported:
        control_plane._validate_selector(
            {"kind": "SECTOR", "scheme": "SW", "sectorCode": "801010"}, definition
        )

    assert malformed.value.code == "invalid-target-selector"
    assert unsupported.value.code == "unsupported-target-selector"


def test_etf_profile_all_venues_is_exact_two_partition_scope(
    configured_environment,
) -> None:
    """双市场目录必须显式使用 ALL_VENUES，预检按 SSE、SZSE 两个 authority 分区估算。"""
    del configured_environment
    control_plane = object.__new__(DataOperationsControlPlane)
    control_plane._now = lambda: datetime(2099, 1, 1, 8, tzinfo=UTC)
    control_plane._catalog = build_catalog(load_settings(), SourceRegistry())
    definition = control_plane._catalog["fund.etf.profile.reported"]
    selector = {
        "kind": "ETF",
        "operation": "MASTER",
        "scope": "ALL_VENUES",
        "venue": None,
        "etf": None,
    }

    normalized = control_plane._validate_selector(selector, definition)
    estimated = control_plane._estimated_partitions(
        {
            "datasetCode": definition.dataset_code,
            "mode": "OBSERVATION_DATE",
            "selector": normalized,
            "dateFrom": None,
            "dateTo": None,
            "observationDate": "2026-07-30",
        },
        definition=definition,
        eligible=True,
    )

    assert normalized == selector
    assert estimated == 2
    for invalid in (
        {**selector, "scope": "ALL_ETFS"},
        {**selector, "venue": "SSE"},
        {**selector, "operation": "BARS"},
        {**selector, "profileDataVersions": None},
    ):
        with pytest.raises(OperationProblem) as raised:
            control_plane._validate_selector(invalid, definition)
        assert raised.value.code == "invalid-target-selector"


def test_etf_profile_schedule_uses_current_local_date_policy(
    configured_environment,
) -> None:
    """current-only 双市场目录计划只能绑定 scheduled 上海自然日，不能标记最近完成交易日。"""
    del configured_environment
    control_plane = object.__new__(DataOperationsControlPlane)
    control_plane._now = lambda: datetime(2099, 1, 1, 8, tzinfo=UTC)
    definition = build_catalog(load_settings(), SourceRegistry())["fund.etf.profile.reported"]

    assert control_plane._schedule_target_policy_options(definition) == [
        {
            "mode": "OBSERVATION_DATE",
            "policy": {
                "policyVersion": 1,
                "dateResolution": "SCHEDULED_LOCAL_DATE",
            },
            "isDefault": True,
        }
    ]


def test_event_time_filter_rejects_invalid_or_reversed_timestamp_range(
    configured_environment,
) -> None:
    """事件时间边界必须是带时区 RFC 3339，开始时间不得晚于结束时间。"""
    del configured_environment
    control_plane = object.__new__(DataOperationsControlPlane)
    control_plane._now = lambda: datetime(2099, 1, 1, 8, tzinfo=UTC)

    with pytest.raises(OperationProblem) as invalid:
        control_plane._datetime_or_none("2026-07-29T08:00:00", "occurredFrom")

    assert invalid.value.code == "validation-error"


def test_public_submit_rejects_private_legacy_intent_field(configured_environment) -> None:
    """0022 公开 submit 不能通过额外字段注入仅 Python 可用的遗留执行意图。"""
    del configured_environment
    control_plane = object.__new__(DataOperationsControlPlane)
    control_plane._now = lambda: datetime(2099, 1, 1, 8, tzinfo=UTC)

    with pytest.raises(OperationProblem) as rejected:
        control_plane.submit_command(
            request={
                "submissionId": "00000000-0000-0000-0000-000000000001",
                "preflightId": "00000000-0000-0000-0000-000000000002",
                "requestHash": "a" * 64,
                "targets": [],
                "actor": {"actorRef": "system", "role": "SYSTEM", "reason": "测试"},
                "legacyIntent": {"kind": "ROLLBACK", "revision": 1},
            },
            idempotency_key="contract-private-intent",
            request_id="contract-private-intent",
        )

    assert rejected.value.code == "invalid-command-request"


@pytest.mark.parametrize(
    "intent",
    [
        {"kind": "STANDARD"},
        {"kind": "REPLAY_RAW"},
        {"kind": "PUBLISH"},
        {"kind": "REPLAY_AND_PUBLISH"},
        {"kind": "ROLLBACK", "revision": 2},
    ],
)
def test_private_legacy_intent_has_closed_shape(
    intent: dict[str, Any], configured_environment
) -> None:
    """兼容层只接受五种闭集意图，不能携带 URI、凭据或任意 Provider 参数。"""
    del configured_environment
    control_plane = object.__new__(DataOperationsControlPlane)
    control_plane._now = lambda: datetime(2099, 1, 1, 8, tzinfo=UTC)

    assert control_plane._validate_legacy_execution_intent(intent) == intent


def test_private_legacy_intent_rejects_unknown_fields(configured_environment) -> None:
    """私有 intent 也不能成为绕过 target 合同的自由参数通道。"""
    del configured_environment
    control_plane = object.__new__(DataOperationsControlPlane)
    control_plane._now = lambda: datetime(2099, 1, 1, 8, tzinfo=UTC)

    with pytest.raises(OperationProblem) as rejected:
        control_plane._validate_legacy_execution_intent(
            {"kind": "REPLAY_RAW", "rawUri": "s3://sensitive/raw"}
        )

    assert rejected.value.code == "invalid-legacy-intent"


def test_dispatch_failure_disarms_pending_terminal_callback(configured_environment) -> None:
    """末次 canonical 写失败后，失败终态事务不能错误触发此前已 arm 的成功回调。"""
    del configured_environment
    control_plane = object.__new__(DataOperationsControlPlane)
    control_plane._now = lambda: datetime(2099, 1, 1, 8, tzinfo=UTC)
    claim = ExecutionClaim(
        run_id=UUID("00000000-0000-0000-0000-000000000003"),
        dataset_code="equity.bar.1d.raw",
        fencing_token=7,
        target={},
        source_snapshot=[
            {
                "providerId": "licensed-equity-provider",
                "upstreamSource": "licensed.equity-kline",
                "sourceDataset": "equity.bar.1d.raw",
                "adapterId": "licensed-equity-provider:equity.bar.1d.raw",
                "methodologyCode": "equity.bar.1d.raw",
                "methodologyVersion": 1,
                "approvalStatus": "APPROVED",
                "rightsStatus": "licensed",
                "licenseScope": "commercial-redistribution-approved",
                "role": "PRIMARY",
                "effective": True,
            }
        ],
    )
    seen: dict[str, object] = {}

    class FakeThread(Thread):
        """提供 dispatcher finally 所需的最小线程接口。"""

        def join(self, timeout: float | None = None) -> None:
            """测试中无需等待真实心跳线程。"""
            del timeout

    def failing_executor(_claim: ExecutionClaim) -> ExecutionOutcome:
        """模拟 valuation 事务回滚前已 arm，但 canonical 写抛出异常。"""
        execution = current_fenced_execution()
        assert execution is not None
        execution.arm_terminal_write()
        seen["execution"] = execution
        raise RuntimeError("valuation write failed")

    def complete_run(**kwargs: object) -> bool:
        """捕获失败终态，确认不再带有已 arm 的成功回调。"""
        seen["outcome"] = kwargs["outcome"]
        return True

    def claim_next_run(worker_id: str) -> ExecutionClaim | None:
        """固定返回已取得全局槽的测试 run。"""
        del worker_id
        return claim

    def start_heartbeat(claim: ExecutionClaim) -> tuple[Event, Thread]:
        """替换真实心跳线程，避免单元测试等待 lease 周期。"""
        assert claim is not None
        return Event(), FakeThread()

    control_plane._database = cast(Any, object())
    control_plane._executors = {claim.dataset_code: failing_executor}
    control_plane.claim_next_run = claim_next_run
    control_plane._start_heartbeat = start_heartbeat
    control_plane.complete_run = complete_run

    assert control_plane.dispatch_once("test-worker") is True
    assert cast(ExecutionOutcome, seen["outcome"]).status == "FAILED"
    assert cast(Any, seen["execution"]).terminal_armed is False
