"""资金流应用用例的 raw-first、方法学隔离和发布测试。"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from service_data_sync.application.money_flow.sync import (
    MoneyFlowSyncService,
    _daily_observations,
    _payload,
    _ranking_snapshot,
    _scope,
)
from service_data_sync.application.ports.data_source import (
    ProviderBatch,
    ProviderError,
    ProviderErrorCode,
    SourceRequest,
)
from service_data_sync.application.ports.market_data import RawPayload
from service_data_sync.application.ports.money_flow import PublishedMoneyFlow
from service_data_sync.domain.money_flow import MoneyFlowScopeType

_OBSERVED_AT = datetime(2026, 7, 24, 10, tzinfo=UTC)


class FakeSource:
    """返回固定标准载荷并记录 provider-neutral 请求。"""

    provider_id = "fake-money-flow"

    def __init__(
        self,
        payload: bytes,
        events: list[str],
        capability: str = "money_flow.order_size.daily.equity.raw",
    ) -> None:
        """保存标准载荷和可观察执行顺序。"""
        self._payload = payload
        self._events = events
        self._capability = capability
        self.requests: list[SourceRequest] = []

    def capabilities(self) -> frozenset[str]:
        """声明测试选择的唯一资金流能力。"""
        return frozenset({self._capability})

    async def fetch(self, request: SourceRequest) -> ProviderBatch:
        """返回带 raw evidence 的固定批次。"""
        self.requests.append(request)
        self._events.append("fetch")
        return ProviderBatch(
            provider_id=self.provider_id,
            capability=request.capability,
            payload=self._payload,
            raw_payload=b'{"vendor":"raw"}',
            raw_content_type="application/json",
            observed_at=datetime(2026, 7, 24, 10, tzinfo=UTC),
            adapter_version="fixture-v1",
            upstream_source="fixture",
            schema_fingerprint="a" * 64,
        )


class FakeRawStore:
    """记录 raw evidence，并返回不可变对象 URI。"""

    def __init__(self, events: list[str]) -> None:
        """保存执行顺序。"""
        self._events = events
        self.payloads: list[RawPayload] = []

    def put(self, payload: RawPayload) -> str:
        """在 canonical 发布前记录 raw。"""
        self._events.append("raw")
        self.payloads.append(payload)
        return f"s3://private/{payload.object_key}"

    def get(self, uri: str) -> bytes:
        """按不可变 URI 返回已归档证据，供离线重放契约使用。"""
        for payload in self.payloads:
            if uri == f"s3://private/{payload.object_key}":
                return payload.payload
        raise KeyError(uri)


class FakeRepository:
    """捕获 canonical 日序列发布输入。"""

    def __init__(self, events: list[str]) -> None:
        """保存执行顺序和最近调用。"""
        self._events = events
        self.call: dict[str, Any] | None = None

    def publish_daily(self, **kwargs: Any) -> PublishedMoneyFlow:
        """记录研究态方法学和日期感知 observation。"""
        self._events.append("publish")
        self.call = kwargs
        return PublishedMoneyFlow(
            data_version=None,
            inserted_count=5,
            revised_count=0,
            unchanged_count=0,
            published=False,
            quality_status="passed",
        )

    def publish_ranking(self, **kwargs: Any) -> PublishedMoneyFlow:
        """测试日序列时禁止误走排行发布。"""
        raise AssertionError(f"unexpected ranking publication: {kwargs}")


class FakeRankingRepository:
    """捕获 canonical 供应商排行发布输入。"""

    def __init__(self, events: list[str]) -> None:
        """保存执行顺序和最近调用。"""
        self._events = events
        self.call: dict[str, Any] | None = None

    def publish_daily(self, **kwargs: Any) -> PublishedMoneyFlow:
        """排行测试不得误走日序列发布。"""
        raise AssertionError(f"unexpected daily publication: {kwargs}")

    def publish_ranking(self, **kwargs: Any) -> PublishedMoneyFlow:
        """记录研究态排行和未验证完整性证据。"""
        self._events.append("publish-ranking")
        self.call = kwargs
        return PublishedMoneyFlow(
            data_version=None,
            inserted_count=0,
            revised_count=0,
            unchanged_count=0,
            published=False,
            quality_status="partial",
        )


def _ranking_payload(*, sector: bool = False) -> dict[str, object]:
    """构造同花顺滚动排行标准载荷，保留供应商未验证完整性。"""
    scope: dict[str, object]
    if sector:
        scope = {
            "scopeType": "sector",
            "scheme": "10jqka.industry",
            "sourceName": "银行",
        }
    else:
        scope = {
            "scopeType": "equity",
            "sourceSymbol": "000001",
            "name": "平安银行",
        }
    return {
        "schema": "quant-v2.money-flow-ranking.v1",
        "methodologyKey": "10jqka-trade-direction",
        "methodologyVersion": "1",
        "targetTradeDate": "2026-07-24",
        "scopeType": "sector" if sector else "equity",
        "universe": "provider-page",
        "windowType": "supplier_rolling",
        "windowSize": 3,
        "rankingBucket": "all",
        "rankingBasis": "supplier_reported_order",
        "completenessBasis": "sdk_returned",
        "isComplete": False,
        "items": [
            {
                "supplierPosition": 1,
                "scope": scope,
                "metrics": [
                    {
                        "bucket": "all",
                        "grossInflow": None,
                        "grossOutflow": None,
                        "netAmount": "100000",
                        "netRatio": None,
                    }
                ],
            }
        ],
    }


def test_sync_archives_raw_before_research_canonical_write() -> None:
    """验证 raw-first、研究态 fail-closed 和显式 run checkpoint 透传。"""
    payload = json.dumps(
        {
            "schema": "quant-v2.money-flow-daily.v1",
            "methodologyKey": "eastmoney-order-size",
            "methodologyVersion": "1",
            "scope": {
                "scopeType": "equity",
                "exchange": "SSE",
                "symbol": "600000",
            },
            "universe": "cn-a",
            "observations": [
                {
                    "tradeDate": "2026-07-24",
                    "bucket": bucket,
                    "grossInflow": None,
                    "grossOutflow": None,
                    "netAmount": "1",
                    "netRatio": "0.01",
                }
                for bucket in ("main", "super_large", "large", "medium", "small")
            ],
        }
    ).encode()
    events: list[str] = []
    source = FakeSource(payload, events)
    raw_store = FakeRawStore(events)
    repository = FakeRepository(events)
    run_id = uuid4()

    result = asyncio.run(
        MoneyFlowSyncService(
            source=source,
            repository=repository,
            raw_payload_store=raw_store,
        ).sync(
            capability="money_flow.order_size.daily.equity.raw",
            parameters=(("exchange", "SSE"), ("symbol", "600000")),
            run_id=run_id,
            partition_key="request:fixture",
        )
    )

    assert events == ["fetch", "raw", "publish"]
    assert result.publication.published is False
    assert repository.call is not None
    assert repository.call["methodology"].status == "research"
    assert repository.call["run_id"] == run_id
    assert repository.call["partition_key"] == "request:fixture"
    assert raw_store.payloads[0].payload == b'{"vendor":"raw"}'


def test_sync_routes_supplier_ranking_without_recomputing_position() -> None:
    """验证排行保留供应商位置、方法学隔离和 raw-first 顺序。"""
    capability = "money_flow.trade_direction.ranking.equity.raw"
    events: list[str] = []
    source = FakeSource(
        json.dumps(_ranking_payload()).encode(),
        events,
        capability=capability,
    )
    raw_store = FakeRawStore(events)
    repository = FakeRankingRepository(events)

    result = asyncio.run(
        MoneyFlowSyncService(
            source=source,
            repository=repository,
            raw_payload_store=raw_store,
        ).sync(
            capability=capability,
            parameters=(("indicator", "3日排行"),),
        )
    )

    assert events == ["fetch", "raw", "publish-ranking"]
    assert result.publication.quality_status == "partial"
    assert repository.call is not None
    methodology = repository.call["methodology"]
    snapshot = repository.call["snapshot"]
    assert methodology.public_key == "10jqka-trade-direction"
    assert snapshot.items[0].supplier_position == 1
    assert snapshot.items[0].scope.scope_type is MoneyFlowScopeType.EQUITY
    assert snapshot.is_complete is False


def test_sync_rejects_capability_not_declared_by_selected_source() -> None:
    """来源未声明能力时不得发出网络请求或写入 raw。"""
    events: list[str] = []
    service = MoneyFlowSyncService(
        source=FakeSource(b"{}", events),
        repository=FakeRepository(events),
        raw_payload_store=FakeRawStore(events),
    )

    with pytest.raises(ProviderError) as captured:
        asyncio.run(
            service.sync(
                capability="money_flow.trade_direction.ranking.equity.raw",
                parameters=(),
            )
        )

    assert captured.value.code is ProviderErrorCode.INVALID_REQUEST
    assert events == []


def test_payload_and_daily_parser_fail_closed_on_schema_drift() -> None:
    """未知 schema、非对象、缺行、混合行和重复事实键都必须拒绝。"""
    with pytest.raises(ProviderError, match="not JSON"):
        _payload(b"{")
    with pytest.raises(ProviderError, match="unexpected"):
        _payload(b"[]")
    with pytest.raises(ProviderError, match="unexpected"):
        _payload(b'{"schema":"unknown"}')

    base = {
        "schema": "quant-v2.money-flow-daily.v1",
        "methodologyKey": "eastmoney-order-size",
        "methodologyVersion": "1",
        "scope": {
            "scopeType": "market",
            "marketCode": "cn-a",
        },
        "observations": [
            {
                "tradeDate": "2026-07-24",
                "bucket": "main",
                "netAmount": "1",
            }
        ],
    }
    with pytest.raises(ProviderError, match="no observations"):
        _daily_observations({**base, "observations": []}, _OBSERVED_AT)
    with pytest.raises(ProviderError, match="observation is invalid"):
        _daily_observations({**base, "observations": [None]}, _OBSERVED_AT)
    records = base["observations"]
    assert isinstance(records, list)
    first_record = records[0]
    assert isinstance(first_record, dict)
    duplicate = [first_record, dict(first_record)]
    with pytest.raises(ProviderError, match="duplicate"):
        _daily_observations({**base, "observations": duplicate}, _OBSERVED_AT)


def test_scope_and_ranking_parser_keep_source_identities_distinct() -> None:
    """三类日 scope 与证券、板块排行身份不得互相 fallback。"""
    equity = _scope(
        {
            "scopeType": "equity",
            "exchange": "BSE",
            "symbol": "830001",
        }
    )
    sector = _scope(
        {
            "scopeType": "sector",
            "scheme": "eastmoney.industry",
            "sectorCode": "BK0475",
        }
    )
    market = _scope({"scopeType": "market", "marketCode": "cn-a"})
    sector_ranking = _ranking_snapshot(_ranking_payload(sector=True), _OBSERVED_AT)

    assert equity.scope_type is MoneyFlowScopeType.EQUITY
    assert sector.scope_type is MoneyFlowScopeType.SECTOR
    assert market.scope_type is MoneyFlowScopeType.MARKET
    assert sector_ranking.items[0].scope.sector_code == "unresolved-name:银行"
    with pytest.raises(ProviderError, match="scope is invalid"):
        _scope(None)
    invalid_symbol = _ranking_payload()
    invalid_items = invalid_symbol["items"]
    assert isinstance(invalid_items, list)
    invalid_item = invalid_items[0]
    assert isinstance(invalid_item, dict)
    invalid_scope = invalid_item["scope"]
    assert isinstance(invalid_scope, dict)
    invalid_scope["sourceSymbol"] = "ABC"
    with pytest.raises(ProviderError, match="source symbol"):
        _ranking_snapshot(invalid_symbol, _OBSERVED_AT)


def test_ranking_parser_rejects_invalid_structure_and_typed_fields() -> None:
    """排行位置、窗口和完整性字段必须保持标准 JSON 原生类型。"""
    invalid_payloads: list[tuple[dict[str, object], str]] = []
    no_items = _ranking_payload()
    no_items["items"] = []
    invalid_payloads.append((no_items, "no items"))
    invalid_item = _ranking_payload()
    invalid_item["items"] = [None]
    invalid_payloads.append((invalid_item, "item is invalid"))
    invalid_metric = _ranking_payload()
    metric_items = invalid_metric["items"]
    assert isinstance(metric_items, list)
    metric_record = metric_items[0]
    assert isinstance(metric_record, dict)
    metric_record["metrics"] = [None]
    invalid_payloads.append((invalid_metric, "metric is invalid"))
    invalid_position = _ranking_payload()
    position_items = invalid_position["items"]
    assert isinstance(position_items, list)
    position_record = position_items[0]
    assert isinstance(position_record, dict)
    position_record["supplierPosition"] = "1"
    invalid_payloads.append((invalid_position, "positive integer"))
    invalid_window = _ranking_payload()
    invalid_window["windowSize"] = True
    invalid_payloads.append((invalid_window, "positive integer"))
    invalid_complete = _ranking_payload()
    invalid_complete["isComplete"] = "false"
    invalid_payloads.append((invalid_complete, "must be boolean"))

    for payload, message in invalid_payloads:
        with pytest.raises(ProviderError, match=message):
            _ranking_snapshot(payload, _OBSERVED_AT)
