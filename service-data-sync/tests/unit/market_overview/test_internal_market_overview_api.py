"""市场概览与行业板块内部资源路由契约测试。"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from typing import cast
from uuid import UUID, uuid5

import pytest
from fastapi.testclient import TestClient

from service_data_sync.application.ports.financial_read import FinancialReadRepository
from service_data_sync.application.ports.market_overview import (
    MarketOverviewRepository,
    StoredMarketBundle,
    StoredMarketComponent,
    StoredMarketSnapshot,
)
from service_data_sync.application.ports.sector_market_data import SectorMarketDataRepository
from service_data_sync.bootstrap.settings import load_settings
from service_data_sync.interfaces.internal_sector_api import create_app

_NAMESPACE = UUID("12caaf00-a74e-4cf2-8a98-ac3b1cfa0731")
_TRADE_DATE = date(2026, 7, 28)
_PUBLISHED_AT = datetime(2026, 7, 28, 8, tzinfo=UTC)


class FakeMarketOverviewRepository:
    """按精确日期和 active 历史组件提供确定性完整包读取。"""

    def __init__(
        self,
        snapshot: StoredMarketSnapshot,
        history: dict[str, tuple[StoredMarketComponent, ...]],
    ) -> None:
        """保存 current/精确快照与按数据集分组的 active component 历史。"""
        self.current = snapshot
        self.snapshots = {snapshot.bundle.trade_date: snapshot}
        self.history = history
        self.requested_snapshot_dates: list[date | None] = []

    def get_snapshot(self, *, trade_date: date | None) -> StoredMarketSnapshot | None:
        """只返回 current 或精确日期，不回退邻近 publication。"""
        self.requested_snapshot_dates.append(trade_date)
        return self.current if trade_date is None else self.snapshots.get(trade_date)

    def list_components(
        self,
        *,
        dataset_code: str,
        start: date | None,
        end: date | None,
    ) -> tuple[StoredMarketComponent, ...]:
        """按组件事实日过滤 active 历史，并保持稳定升序。"""
        return tuple(
            component
            for component in self.history.get(dataset_code, ())
            if (start is None or component.trade_date is None or component.trade_date >= start)
            and (end is None or component.trade_date is None or component.trade_date <= end)
        )

    def replace_snapshot_component(self, replacement: StoredMarketComponent) -> None:
        """只替换同数据集 snapshot 组件，用于验证单侧修订会改变 composite。"""
        components = tuple(
            replacement if component.dataset_code == replacement.dataset_code else component
            for component in self.current.components
        )
        self.current = StoredMarketSnapshot(bundle=self.current.bundle, components=components)
        self.snapshots[self.current.bundle.trade_date] = self.current
        self.history[replacement.dataset_code] = (replacement,)


@pytest.fixture
def market_client(
    configured_environment: None,
) -> tuple[TestClient, dict[str, str], FakeMarketOverviewRepository]:
    """构造只注入市场完整包假仓储的内部 FastAPI 客户端。"""
    del configured_environment
    repository = _repository()
    settings = load_settings()
    app = create_app(
        settings=settings,
        repository=cast(SectorMarketDataRepository, object()),
        financial_repository=cast(FinancialReadRepository, object()),
        market_overview_repository=cast(MarketOverviewRepository, repository),
    )
    credential = settings.internal_api_bearer_token.get_secret_value()
    headers = {
        "Authorization": f"Bearer {credential}",
        "X-Request-Id": "market-contract-test-1",
    }
    return TestClient(app), headers, repository


def test_overview_historical_status_units_headers_and_304(
    market_client: tuple[TestClient, dict[str, str], FakeMarketOverviewRepository],
) -> None:
    """首页历史读取冻结收盘状态，保留指数手单位，并完整回显缓存与请求标识。"""
    client, headers, _repository_value = market_client

    response = client.get(
        "/internal/v1/market/overview-bundles/2026-07-28",
        headers=headers,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == {
        "marketState": "closed",
        "marketStateAsOf": "2026-07-28T15:00:00+08:00",
        "marketStateMethodology": "calendar_schedule_derived",
        "eodEligibilityScheduleVersion": "cn-a-eod-eligibility-2026-v1",
        "freshness": "stale",
        "latestEligibleTradeDate": "2026-07-28",
        "latestAttemptedTradeDate": None,
        "lagTradingDays": 0,
        "freshnessReason": "historical_snapshot",
        "quality": "passed",
    }
    assert {row["volumeUnit"] for row in body["indices"]} == {"lot"}
    assert response.headers["x-data-version"] == body["dataVersion"]
    assert response.headers["x-request-id"] == headers["X-Request-Id"]
    not_modified = client.get(
        "/internal/v1/market/overview-bundles/2026-07-28",
        headers={**headers, "If-None-Match": response.headers["etag"]},
    )
    assert not_modified.status_code == 304
    assert not_modified.headers["x-data-version"] == body["dataVersion"]
    assert not_modified.headers["x-request-id"] == headers["X-Request-Id"]


def test_index_history_is_active_composite_with_lot_unit_and_stable_lineage(
    market_client: tuple[TestClient, dict[str, str], FakeMarketOverviewRepository],
) -> None:
    """指数历史跨 active 组件去重，并把请求、组件集合、headers 与游标绑定到同一版本。"""
    client, headers, repository = market_client
    path = (
        "/internal/v1/market/indices/sse-composite/bars"
        "?period=1d&start=2026-07-26&end=2026-07-28&limit=2"
    )

    first_page = client.get(path, headers=headers)

    assert first_page.status_code == 200
    body = first_page.json()
    assert body["volumeUnit"] == "lot"
    assert body["inputDataVersions"] == [
        str(_version("index-old")),
        str(_version("index-new")),
    ]
    assert [item["tradeDate"] for item in body["items"]] == [
        "2026-07-26",
        "2026-07-27",
    ]
    assert body["nextCursor"] is not None
    assert first_page.headers["x-data-version"] == body["dataVersion"]
    second_page = client.get(
        f"{path}&cursor={body['nextCursor']}",
        headers=headers,
    )
    assert [item["tradeDate"] for item in second_page.json()["items"]] == ["2026-07-28"]
    assert second_page.json()["dataVersion"] == body["dataVersion"]

    old = repository.history["index.bar.1d"][0]
    revised = replace(
        old,
        data_version=_version("index-old-revised"),
        payload={
            **old.payload,
            "records": [
                {**old.payload["records"][0], "close": "3501"},
            ],
        },
    )
    repository.history["index.bar.1d"] = (
        revised,
        repository.history["index.bar.1d"][1],
    )
    revised_response = client.get(path, headers=headers)
    assert revised_response.json()["dataVersion"] != body["dataVersion"]


def test_equity_rankings_money_flow_and_calendar_use_complete_bundle_components(
    market_client: tuple[TestClient, dict[str, str], FakeMarketOverviewRepository],
) -> None:
    """证券排行、单侧资金流和日历都读取同一完整包组件并返回可缓存版本头。"""
    client, headers, _repository_value = market_client

    ranking = client.get(
        "/internal/v1/market/equity-rankings?metric=changePercent&order=desc",
        headers=headers,
    )
    money_flow = client.get(
        "/internal/v1/market/money-flow/equity-rankings?direction=inflow",
        headers=headers,
    )
    calendar = client.get(
        "/internal/v1/market/calendar?venues=SSE,SZSE&start=2026-07-28&end=2026-07-28",
        headers=headers,
    )

    assert ranking.status_code == money_flow.status_code == calendar.status_code == 200
    assert ranking.json()["items"][0]["symbol"] == "600001"
    assert money_flow.json()["items"][0]["netAmountCny"] == "10000000"
    assert {item["venue"] for item in calendar.json()["items"]} == {"SSE", "SZSE"}
    for response in (ranking, money_flow, calendar):
        assert response.headers["x-data-version"] == response.json()["dataVersion"]
        assert response.headers["x-request-id"] == headers["X-Request-Id"]


def test_sw_resources_use_provider_native_volume_half_open_membership_and_composites(
    market_client: tuple[TestClient, dict[str, str], FakeMarketOverviewRepository],
) -> None:
    """申万 K 线、成分和估值分别暴露单位边界、半开区间与完整 composite lineage。"""
    client, headers, _repository_value = market_client

    bars = client.get(
        "/internal/v1/market/industries/sw/801010.SI/bars"
        "?period=1d&start=2026-07-28&end=2026-07-28",
        headers=headers,
    )
    constituents = client.get(
        "/internal/v1/market/industries/sw/801010.SI/constituents?asOf=2026-07-28",
        headers=headers,
    )
    valuation = client.get(
        "/internal/v1/market/industries/sw/801010.SI/valuation?asOf=2026-07-28",
        headers=headers,
    )

    assert bars.status_code == constituents.status_code == valuation.status_code == 200
    assert bars.json()["volumeUnit"] == "provider_native"
    assert bars.json()["items"][0]["amountCny"] == "120000000"
    assert len(bars.json()["inputDataVersions"]) == 1
    assert constituents.json()["inputDataVersions"] == [
        str(_version("sw-membership")),
        str(_version("sw-taxonomy")),
    ]
    assert [item["symbol"] for item in constituents.json()["items"]] == ["600001"]
    assert valuation.json()["inputDataVersions"] == [
        str(_version("sw-market")),
        str(_version("sw-taxonomy")),
    ]
    assert valuation.headers["x-data-version"] == valuation.json()["dataVersion"]


def test_strength_sector_flow_and_eod_responses_have_strict_methodology_and_composite(
    market_client: tuple[TestClient, dict[str, str], FakeMarketOverviewRepository],
) -> None:
    """强弱、板块资金流与 EOD 都返回可复验方法学，EOD 单侧修订会推进 composite。"""
    client, headers, repository = market_client

    strength = client.get(
        "/internal/v1/market/sectors/strength?scheme=eastmoney.industry&window=5&order=desc",
        headers=headers,
    )
    sector_flow = client.get(
        "/internal/v1/market/sectors/money-flow-rankings?scheme=eastmoney.industry&order=desc",
        headers=headers,
    )
    eod_path = (
        "/internal/v1/sectors/eod-snapshots?scheme=eastmoney.industry&sort=changePercent&order=desc"
    )
    eod = client.get(eod_path, headers=headers)

    assert strength.status_code == sector_flow.status_code == eod.status_code == 200
    assert len(strength.json()["inputDataVersions"]) == 5
    assert strength.json()["quality"]["validUniverseCount"] == 1
    assert sector_flow.json()["methodology"]["semanticFamily"] == "trade_direction_flow"
    assert sector_flow.json()["methodology"]["status"] == "source_reported"
    assert len(eod.json()["inputDataVersions"]) == 2
    assert eod.json()["items"][0]["marketValueUnit"] == "CNY"
    assert eod.headers["x-data-version"] == eod.json()["dataVersion"]
    previous_version = eod.json()["dataVersion"]

    strength_component = next(
        component
        for component in repository.current.components
        if component.dataset_code == "sector.strength.eod"
    )
    revised_strength = replace(
        strength_component,
        data_version=_version("strength-revised"),
        published_at=strength_component.published_at.replace(minute=30),
        payload={
            **strength_component.payload,
            "records": [
                {
                    **row,
                    "leadingEquity": {
                        "exchange": "SSE",
                        "symbol": "600002",
                        "name": "新龙头",
                        "changePercent": "3.2",
                    },
                }
                if row["window"] == 1
                else row
                for row in strength_component.payload["records"]
            ],
        },
    )
    repository.replace_snapshot_component(revised_strength)

    revised_eod = client.get(eod_path, headers=headers)
    assert revised_eod.json()["dataVersion"] != previous_version
    assert revised_eod.json()["items"][0]["leaderName"] == "新龙头"


def test_membership_as_of_uses_exact_shanghai_date_without_fallback(
    market_client: tuple[TestClient, dict[str, str], FakeMarketOverviewRepository],
) -> None:
    """带偏移的日末 asOf 精确落到上海交易日，缺失日期返回 404 而非 current。"""
    client, headers, repository = market_client

    exact = client.get(
        "/internal/v1/sectors/eastmoney.industry/BK0001/constituents"
        "?asOf=2026-07-28T23%3A59%3A59%2B08%3A00",
        headers=headers,
    )
    missing = client.get(
        "/internal/v1/sectors/eastmoney.industry/BK0001/constituents"
        "?asOf=2026-07-27T23%3A59%3A59%2B08%3A00",
        headers=headers,
    )

    assert exact.status_code == 200
    assert missing.status_code == 404
    assert repository.requested_snapshot_dates[-2:] == [
        date(2026, 7, 28),
        date(2026, 7, 27),
    ]


def _repository() -> FakeMarketOverviewRepository:
    """构造覆盖首页、指数、资金流、板块和申万资源的确定性 publication 集。"""
    source = _source("bundle")
    components = (
        _component("sector.catalog.dc", _sector_catalog_payload(), "sector-catalog"),
        _component("sector.quote.eod.dc", _sector_quote_payload(), "sector-quote"),
        _component("sector.strength.eod", _strength_payload(), "strength"),
        _component("sector.money-flow.dc.eod", _sector_flow_payload(), "sector-flow"),
        _component("sector.membership.dc", _sector_membership_payload(), "sector-membership"),
        _component("equity.catalog", _equity_catalog_payload(), "equity-catalog"),
        _component("equity.suspension.eod", {"records": []}, "equity-suspension"),
        _component(
            "equity.market-ranking.eod",
            _equity_ranking_payload(),
            "equity-ranking",
        ),
        _component(
            "money-flow.equity-ranking.eod",
            _equity_money_flow_ranking_payload(),
            "equity-money-flow-ranking",
        ),
        _component("market.calendar", _calendar_payload(), "market-calendar"),
        _component("sw.taxonomy", _sw_taxonomy_payload(), "sw-taxonomy"),
        _component("sw.membership", _sw_membership_payload(), "sw-membership"),
        _component("sw.market-data", _sw_market_payload(), "sw-market"),
    )
    bundle = StoredMarketBundle(
        data_version=_version("bundle"),
        trade_date=_TRADE_DATE,
        published_at=_PUBLISHED_AT,
        payload=_overview_payload(source),
        active_action="publish",
        active_changed_at=_PUBLISHED_AT,
    )
    snapshot = StoredMarketSnapshot(bundle=bundle, components=components)
    history: dict[str, tuple[StoredMarketComponent, ...]] = {
        component.dataset_code: (component,) for component in components
    }
    history["index.bar.1d"] = (
        _component(
            "index.bar.1d",
            {
                "source": _source("index_daily", observed_at="2026-07-27T16:00:00+08:00"),
                "records": [_index_row("2026-07-26", "3490")],
            },
            "index-old",
            trade_date=date(2026, 7, 27),
            published_at=datetime(2026, 7, 27, 8, tzinfo=UTC),
            observed_at="2026-07-27T16:00:00+08:00",
        ),
        _component(
            "index.bar.1d",
            {
                "source": _source("index_daily"),
                "records": [
                    _index_row("2026-07-27", "3500"),
                    _index_row("2026-07-28", "3510"),
                ],
            },
            "index-new",
        ),
    )
    history["sw.bar.1d"] = (
        _component(
            "sw.bar.1d",
            {
                "source": _source("sw_daily"),
                "inputDataVersions": [str(_version("sw-market"))],
                "records": [_sw_bar_row()],
            },
            "sw-bar",
        ),
    )
    return FakeMarketOverviewRepository(snapshot, history)


def _component(
    dataset_code: str,
    payload: dict[str, object],
    version_label: str,
    *,
    trade_date: date = _TRADE_DATE,
    published_at: datetime = _PUBLISHED_AT,
    observed_at: str = "2026-07-28T16:00:00+08:00",
) -> StoredMarketComponent:
    """构造带完整来源六字段的 active component。"""
    return StoredMarketComponent(
        data_version=_version(version_label),
        dataset_code=dataset_code,
        partition_key=trade_date.isoformat(),
        trade_date=trade_date,
        published_at=published_at,
        payload=payload,
        source=_source(
            str(payload.get("sourceDataset", dataset_code)),
            observed_at=observed_at,
        ),
        methodology={"id": f"{dataset_code}-method", "version": "1"},
        quality={"status": "passed"},
    )


def _version(label: str) -> UUID:
    """由稳定标签生成 RFC 4122 UUID。"""
    return uuid5(_NAMESPACE, label)


def _source(
    dataset: str,
    *,
    observed_at: str = "2026-07-28T16:00:00+08:00",
) -> dict[str, str]:
    """构造内部与公开 reader 共用的真实来源元数据形状。"""
    return {
        "provider": "tushare-pro",
        "upstreamSource": "tushare.pro",
        "sourceDataset": dataset,
        "observedAt": observed_at,
        "adapterVersion": "market-overview-v1",
        "schemaFingerprint": "a" * 64,
    }


def _index_row(trade_date: str, close: str) -> dict[str, object]:
    """构造一条固定上证指数来源日线。"""
    return {
        "indexId": "sse-composite",
        "indexCode": "000001.SH",
        "name": "上证指数",
        "tradeDate": trade_date,
        "open": "3485",
        "high": "3520",
        "low": "3470",
        "close": close,
        "previousClose": "3480",
        "change": "20",
        "changePercent": "0.57",
        "volume": "300000000",
        "amountCny": "450000000000",
        "finality": "final",
    }


def _sw_bar_row() -> dict[str, object]:
    """构造同步期已物化的申万日 K 线。"""
    return {
        "code": "801010.SI",
        "period": "1d",
        "periodKey": "2026-07-28",
        "periodStart": "2026-07-28",
        "periodEnd": "2026-07-28",
        "open": "1000",
        "high": "1020",
        "low": "990",
        "close": "1010",
        "change": "10",
        "changePercent": "1",
        "volume": "30000",
        "amountCny": "120000000",
        "previousClose": "1000",
        "amplitudePercent": "3",
        "turnoverPercent": "0.8",
        "isFinal": True,
    }


def _sw_taxonomy_payload() -> dict[str, object]:
    """构造申万一级 taxonomy。"""
    return {
        "records": [
            {
                "code": "801010.SI",
                "name": "农林牧渔",
                "level": 1,
                "parentCode": None,
            }
        ]
    }


def _sw_membership_payload() -> dict[str, object]:
    """构造含一个有效与一个当日失效成分的正式区间。"""
    common = {
        "l1Code": "801010.SI",
        "l2Code": "801011.SI",
        "l3Code": "850111.SI",
        "inDate": "2026-01-01",
    }
    return {
        "snapshotDate": "2026-07-28",
        "historyMode": "latest_revision_effective_interval",
        "knowledgeCutoff": "2026-07-28T16:00:00+08:00",
        "records": [
            {
                **common,
                "tsCode": "600001.SH",
                "name": "有效成分",
                "outDate": "2026-07-29",
            },
            {
                **common,
                "tsCode": "600002.SH",
                "name": "当日失效",
                "outDate": "2026-07-28",
            },
        ],
    }


def _sw_market_payload() -> dict[str, object]:
    """构造申万来源估值与当日行情。"""
    return {
        "records": [
            {
                "code": "801010.SI",
                "name": "农林牧渔",
                "tradeDate": "2026-07-28",
                "pe": "18.2",
                "pb": "2.1",
            }
        ]
    }


def _sector_catalog_payload() -> dict[str, object]:
    """构造东财行业目录。"""
    return {
        "records": [
            {
                "scheme": "eastmoney.industry",
                "sectorCode": "BK0001",
                "name": "银行",
            }
        ]
    }


def _sector_quote_payload() -> dict[str, object]:
    """构造东财行业 EOD 报价。"""
    return {
        "tradeDate": "2026-07-28",
        "records": [
            {
                "scheme": "eastmoney.industry",
                "sectorCode": "BK0001",
                "name": "银行",
                "close": "1050",
                "change": "10",
                "changePercent": "0.96",
                "totalMarketValueCny": "8000000000000",
                "turnoverPercent": "0.7",
                "advancing": 30,
                "declining": 10,
            }
        ],
    }


def _strength_payload() -> dict[str, object]:
    """构造一日 EOD 龙头与五日完整强弱 publication。"""
    input_versions = [str(_version(f"strength-input-{index}")) for index in range(5)]
    base = {
        "scheme": "eastmoney.industry",
        "sectorCode": "BK0001",
        "name": "银行",
        "changePercent": "0.96",
        "turnoverPercent": "0.7",
        "amountCny": "90000000000",
        "cumulativeReturn": "2.5",
        "upDays": 4,
        "medianRank": "3",
        "coverage": "1",
        "availability": "available",
        "leadingEquity": {
            "exchange": "SSE",
            "symbol": "600001",
            "name": "旧龙头",
            "changePercent": "2.1",
        },
    }
    return {
        "tradeDate": "2026-07-28",
        "methodologyVersion": "sector-relative-strength-v1",
        "source": _source("dc_daily"),
        "inputDataVersionsByWindow": {
            "1": [input_versions[-1]],
            "5": input_versions,
        },
        "quality": {
            "validUniverseCountByScheme": {
                "eastmoney.industry": {"1": 1, "5": 1},
            }
        },
        "records": [
            {**base, "window": 1, "validSamples": 1},
            {**base, "window": 5, "validSamples": 5},
        ],
    }


def _sector_flow_payload() -> dict[str, object]:
    """构造显式供应商语义的东财板块资金流。"""
    return {
        "tradeDate": "2026-07-28",
        "methodologyId": "eastmoney-sector-flow-dc",
        "methodologyVersion": "unknown",
        "semanticFamily": "trade_direction_flow",
        "methodologyStatus": "source_reported",
        "records": [
            {
                "scheme": "eastmoney.industry",
                "sectorCode": "BK0001",
                "name": "银行",
                "close": "1050",
                "changePercent": "0.96",
                "netAmountCny": "5000000000",
                "rank": 1,
            }
        ],
    }


def _sector_membership_payload() -> dict[str, object]:
    """构造东财板块观察快照。"""
    return {
        "records": [
            {
                "scheme": "eastmoney.industry",
                "sectorCode": "BK0001",
                "tsCode": "600001.SH",
                "name": "测试银行",
            }
        ]
    }


def _equity_catalog_payload() -> dict[str, object]:
    """构造成分身份与上市状态目录。"""
    return {
        "records": [
            {
                "tsCode": "600001.SH",
                "name": "测试银行",
                "listStatus": "L",
            }
        ]
    }


def _equity_ranking_payload() -> dict[str, object]:
    """构造冻结全市场横截面派生的四类证券排行。"""
    base = {
        "rank": 1,
        "exchange": "SSE",
        "symbol": "600001",
        "name": "测试银行",
        "close": "10.5",
        "changePercent": "1.2",
        "amountCny": "1000000000",
        "turnoverPercent": "0.5",
    }
    return {
        "tradeDate": "2026-07-28",
        "source": _source("daily"),
        "universe": "CN-A-SSE-SZSE-ELIGIBLE",
        "coverage": "1",
        "finality": "final",
        "quality": {
            "status": "passed",
            "universeVersion": "CN-A-2026-07-28",
            "checks": ["equity-ranking-complete"],
        },
        "gainers": [base],
        "losers": [{**base, "changePercent": "-1.2"}],
        "amount": [base],
        "turnover": [base],
    }


def _equity_money_flow_ranking_payload() -> dict[str, object]:
    """构造订单规模方法学下正负严格分侧的个股资金流排行。"""
    base = {
        "rank": 1,
        "exchange": "SSE",
        "symbol": "600001",
        "name": "测试银行",
        "netAmountCny": "10000000",
        "buyLargeAmountCny": "20000000",
        "sellLargeAmountCny": "10000000",
        "changePercent": "1.2",
    }
    return {
        "tradeDate": "2026-07-28",
        "source": _source("moneyflow"),
        "methodologyId": "tushare-order-size-flow",
        "methodologyVersion": "1",
        "universe": "CN-A-SSE-SZSE-TRADED",
        "coverage": "1",
        "finality": "final",
        "quality": {
            "status": "passed",
            "checks": [
                {
                    "code": "money-flow-ranking-complete",
                    "status": "passed",
                    "actual": "passed",
                    "expected": "passed",
                }
            ],
        },
        "inflow": [base],
        "outflow": [{**base, "netAmountCny": "-10000000"}],
    }


def _calendar_payload() -> dict[str, object]:
    """构造覆盖同一日期沪深两场所的版本化交易日历。"""
    return {
        "timezone": "Asia/Shanghai",
        "sessionScheduleVersion": "cn-a-cash-2026-v1",
        "source": _source("trade_cal"),
        "records": [
            {
                "venue": venue,
                "tradeDate": "2026-07-28",
                "isTradingDay": True,
                "previousTradingDate": "2026-07-27",
                "sessions": [
                    {"name": "morning", "start": "09:30:00", "end": "11:30:00"},
                    {"name": "afternoon", "start": "13:00:00", "end": "15:00:00"},
                ],
            }
            for venue in ("SSE", "SZSE")
        ],
    }


def _overview_payload(source: dict[str, str]) -> dict[str, object]:
    """构造会被 reader 覆盖历史状态的最小 strict 首页 bundle。"""
    index_values = {
        "point": "3500.5",
        "previousClose": "3480",
        "change": "20.5",
        "changePercent": "0.59",
        "open": "3485",
        "high": "3510",
        "low": "3475",
        "volume": "300000000",
        "volumeUnit": "lot",
        "amountCny": "450000000000",
        "source": _source("index_daily"),
    }
    equity = {
        "rank": 1,
        "exchange": "SSE",
        "symbol": "600001",
        "name": "测试银行",
        "close": "10.5",
        "changePercent": "1.2",
        "amountCny": "1000000000",
        "turnoverPercent": "0.5",
    }
    flow = {
        "rank": 1,
        "exchange": "SSE",
        "symbol": "600001",
        "name": "测试银行",
        "netAmountCny": "10000000",
        "buyLargeAmountCny": "20000000",
        "sellLargeAmountCny": "10000000",
        "changePercent": "1.2",
    }
    sector = {
        "rank": 1,
        "sectorCode": "BK0001",
        "name": "银行",
        "changePercent": "1.5",
        "turnoverPercent": "0.8",
        "amountCny": "50000000000",
        "leadingEquity": {
            "exchange": "SSE",
            "symbol": "600001",
            "name": "测试银行",
            "changePercent": "1.2",
        },
        "validSamples": 20,
    }
    return {
        "dataVersion": str(_version("bundle")),
        "tradeDate": "2026-07-28",
        "publishedAt": "2026-07-28T16:00:00+08:00",
        "finality": "final",
        "status": {"market": "closed", "freshness": "current", "quality": "passed"},
        "indices": [
            {"indexId": "sse-composite", "name": "上证指数", **index_values},
            {"indexId": "szse-component", "name": "深证成指", **index_values},
            {"indexId": "csi-300", "name": "沪深300", **index_values},
            {"indexId": "chinext", "name": "创业板指", **index_values},
        ],
        "turnover": {
            "label": "沪深 A 股成交额",
            "universe": "CN-A-SSE-SZSE",
            "methodologyId": "sum-tushare-daily-a-share-amount-cny-v1",
            "sseAmountCny": "500000000000",
            "szseAmountCny": "600000000000",
            "totalAmountCny": "1100000000000",
            "previousTotalAmountCny": "1000000000000",
            "changeAmountCny": "100000000000",
            "changePercent": "10",
        },
        "breadth": {
            "eligible": 5250,
            "advancing": 3000,
            "flat": 200,
            "declining": 2000,
            "suspended": 50,
            "unknown": 0,
        },
        "limits": {"limitUp": 80, "limitDown": 5, "rulesVersion": "cn-a-limits-2026-01"},
        "marketMoneyFlow": {
            "source": _source("moneyflow_mkt_dc"),
            "methodologyId": "eastmoney-market-flow-dc",
            "methodologyVersion": "unknown",
            "netAmountCny": "5000000000",
        },
        "equityMoneyFlowRankings": {
            "source": _source("moneyflow"),
            "methodologyId": "tushare-order-size-flow",
            "methodologyVersion": "1",
            "universe": "CN-A-SSE-SZSE-TRADED",
            "coverage": "1",
            "inflow": [flow],
            "outflow": [{**flow, "netAmountCny": "-10000000"}],
        },
        "equityRankings": {
            "gainers": [equity],
            "losers": [{**equity, "changePercent": "-1.2"}],
            "amount": [equity],
            "turnover": [equity],
        },
        "sectorRankings": {
            "eastmoneyIndustry": {
                "strongest": [sector],
                "weakest": [{**sector, "changePercent": "-1.5"}],
            },
            "eastmoneyConcept": {
                "strongest": [{**sector, "sectorCode": "BK1001", "name": "机器人"}],
                "weakest": [
                    {
                        **sector,
                        "sectorCode": "BK1002",
                        "name": "低空经济",
                        "changePercent": "-1.1",
                    }
                ],
            },
        },
        "attentionSignals": [],
        "quality": {
            "componentCount": 1,
            "passedCount": 1,
            "universeVersion": "CN-A-2026-07-28",
            "sourceBindings": [{"role": "external", "component": "equity.quote.eod", **source}],
            "checks": [
                {
                    "code": "equity-universe-coverage",
                    "status": "passed",
                    "actual": "1",
                    "expected": "1",
                }
            ],
        },
    }
