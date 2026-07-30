"""事件 P0 数据集的受控预检、dispatcher 注册与证券过滤测试。"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch
from uuid import uuid4

import pandas as pd
import pytest

from service_data_sync.application.ports.data_source import ProviderBatch, SourceRequest
from service_data_sync.application.source_registry import SourceRegistry
from service_data_sync.bootstrap.container import ServiceContainer
from service_data_sync.bootstrap.settings import load_settings
from service_data_sync.domain.equity import EquityIdentifier
from service_data_sync.infrastructure.data_operations import canonical_executors
from service_data_sync.infrastructure.data_operations.control_plane import (
    DataOperationsControlPlane,
    ExecutionClaim,
    OperationProblem,
    build_catalog,
)
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.database.fenced_execution import (
    FencedExecution,
    fenced_execution,
)
from service_data_sync.infrastructure.providers.akshare.p0_market_data import (
    _block_trades,
    _frame_records,
    _normalize_corporate_events,
    _normalize_dragon_tiger,
)


class FakeEventProvider:
    """声明三个真实事件 capability，预检不得触发网络。"""

    provider_id = "akshare"

    def capabilities(self) -> frozenset[str]:
        """返回业绩、龙虎榜和大宗交易能力闭集。"""
        return frozenset(
            {
                "corporate.disclosure.earnings.p0",
                "market.dragon_tiger.disclosure.1d",
                "market.block_trade.execution.1d",
            }
        )

    async def fetch(self, request: SourceRequest) -> ProviderBatch:
        """预检若错误访问来源则立即失败。"""
        del request
        raise AssertionError("event preflight must not fetch provider data")


class RecordingControlPlane:
    """记录 canonical executor 注册结果。"""

    def __init__(self) -> None:
        """初始化空注册表。"""
        self.executors: dict[str, object] = {}

    def register_executor(self, dataset_code: str, executor: object) -> None:
        """保存数据集执行器。"""
        self.executors[dataset_code] = executor


def test_event_catalog_preflight_is_dispatchable_and_range_bounded(
    configured_environment: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """三类事件可按 600519 预检，超过 31 天的回填在入队前拒绝。"""
    del configured_environment
    monkeypatch.setenv("DATA_SYNC_AKSHARE_ENABLED", "true")
    registry = SourceRegistry()
    registry.register(FakeEventProvider())
    catalog = build_catalog(load_settings(), registry)
    control_plane = object.__new__(DataOperationsControlPlane)
    control_plane._catalog = catalog
    control_plane._source_registry = registry
    datasets = (
        "equity.corporate_event.earnings.reported",
        "equity.dragon_tiger.disclosure.reported",
        "equity.block_trade.execution.reported",
    )

    for dataset_code in datasets:
        target = control_plane._validate_targets(
            [
                {
                    "datasetCode": dataset_code,
                    "mode": "DATE_RANGE",
                    "selector": {
                        "kind": "INSTRUMENT",
                        "exchange": "SSE",
                        "symbol": "600519",
                    },
                    "dateFrom": "2026-07-01",
                    "dateTo": "2026-07-31",
                }
            ]
        )[0]
        result = control_plane._preflight_target(target)
        assert result["eligible"] is True
        assert catalog[dataset_code].dispatcher_ready is True
        assert catalog[dataset_code].max_range_days == 31

    with pytest.raises(OperationProblem) as raised:
        control_plane._validate_targets(
            [
                {
                    "datasetCode": datasets[0],
                    "mode": "DATE_RANGE",
                    "selector": {
                        "kind": "INSTRUMENT",
                        "exchange": "SSE",
                        "symbol": "600519",
                    },
                    "dateFrom": "2026-06-30",
                    "dateTo": "2026-07-31",
                }
            ]
        )
    assert raised.value.code == "invalid-date-range"


def test_event_executors_are_registered_and_pass_exact_instrument(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """dispatcher 注册三项，并把 SSE.600519 与 DATE_RANGE 传到业绩用例。"""
    registered = RecordingControlPlane()
    canonical_executors.register_canonical_executors(
        cast(DataOperationsControlPlane, registered),
        cast(ServiceContainer, object()),
    )
    assert {
        "equity.corporate_event.earnings.reported",
        "equity.dragon_tiger.disclosure.reported",
        "equity.block_trade.execution.reported",
    }.issubset(registered.executors)

    captured: dict[str, Any] = {}

    class FakeCorporateService:
        """捕获 canonical executor 传入的证券、窗口与终态回调。"""

        def __init__(self, **_kwargs: object) -> None:
            """测试不依赖真实仓储或对象存储。"""

        async def sync(self, **kwargs: object) -> SimpleNamespace:
            """武装末次 publication 并返回稳定计数。"""
            captured.update(kwargs)
            callback = kwargs["before_final_publication"]
            assert callable(callback)
            callback()
            return SimpleNamespace(inserted_count=2, unchanged_count=1, excluded_count=0)

    provider = FakeEventProvider()

    def frozen_provider(
        _snapshot: list[dict[str, Any]],
        _container: ServiceContainer,
        capability: str,
    ) -> FakeEventProvider:
        """返回已冻结事件 provider。"""
        assert capability == "corporate.disclosure.earnings.p0"
        return provider

    def raw_store(_client: object) -> object:
        """返回无需网络的对象存储替身。"""
        return object()

    def failure_source(current: object, _store: object) -> object:
        """保持来源对象不变。"""
        return current

    def retain(_store: object, operation: Any) -> Any:
        """同步执行失败留证包装中的操作。"""
        return operation()

    def repository(_container: ServiceContainer, *, provider_id: str) -> object:
        """返回批准仓储替身并核对 provider。"""
        assert provider_id == "akshare"
        return object()

    def not_cancelled(_container: ServiceContainer) -> bool:
        """保持运行未取消。"""
        return False

    def finalize(_session: object, _execution: FencedExecution) -> None:
        """单测只验证终态被武装。"""

    monkeypatch.setattr(canonical_executors, "CorporateEventsSyncService", FakeCorporateService)
    monkeypatch.setattr(canonical_executors, "_frozen_provider", frozen_provider)
    monkeypatch.setattr(canonical_executors, "S3RawPayloadStore", raw_store)
    monkeypatch.setattr(canonical_executors, "FailureEvidenceDataSource", failure_source)
    monkeypatch.setattr(canonical_executors, "retain_failure_evidence", retain)
    monkeypatch.setattr(canonical_executors, "_corporate_events_repository", repository)
    monkeypatch.setattr(canonical_executors, "_cancel_requested", not_cancelled)
    database = cast(DatabaseClient, object())
    container = cast(
        ServiceContainer,
        SimpleNamespace(database=database, object_storage=object()),
    )
    claim = ExecutionClaim(
        run_id=uuid4(),
        dataset_code="equity.corporate_event.earnings.reported",
        fencing_token=7,
        target={
            "mode": "DATE_RANGE",
            "selector": {"kind": "INSTRUMENT", "exchange": "SSE", "symbol": "600519"},
            "dateFrom": "2026-07-01",
            "dateTo": "2026-07-31",
        },
        source_snapshot=[],
    )
    execution = FencedExecution(
        database=database,
        run_id=claim.run_id,
        fencing_token=claim.fencing_token,
        finalizer=finalize,
    )

    with fenced_execution(execution):
        outcome = canonical_executors._execute_corporate_events(claim, container=container)

    assert outcome.status == "SUCCEEDED"
    assert outcome.processed_records == 3
    assert captured["identifier"] == EquityIdentifier.parse("SSE.600519")
    assert captured["start"] == date(2026, 7, 1)
    assert captured["end"] == date(2026, 7, 31)
    assert execution.terminal_armed is True


def test_provider_event_filters_emit_only_600519() -> None:
    """适配器先对账同一全市场龙虎榜批次，再只投影目标证券而不产生 N+1。"""
    guidance = pd.DataFrame(
        [
            {
                "股票代码": symbol,
                "股票简称": symbol,
                "公告日期": date(2026, 7, 28),
                "预测指标": "净利润",
                "预测数值": 100,
                "业绩变动幅度": 10,
                "预告类型": "预增",
                "上年同期值": 90,
            }
            for symbol in ("600519", "000001")
        ]
    )
    head = [
        {
            "SECURITY_CODE": symbol,
            "TRADE_DATE": "2026-07-28 00:00:00",
            "EXPLANATION": "日涨幅偏离值达7%",
            "TRADE_ID": f"event-{symbol}",
            "CLOSE_PRICE": 10,
            "BILLBOARD_NET_AMT": 50,
            "BILLBOARD_BUY_AMT": 100,
            "BILLBOARD_SELL_AMT": 50,
            "BILLBOARD_DEAL_AMT": 150,
            "ACCUM_AMOUNT": 1000,
            "DEAL_NET_RATIO": 5,
            "DEAL_AMOUNT_RATIO": 15,
            "TURNOVERRATE": 1,
        }
        for symbol in ("600519", "000001")
    ]
    buy_seats = [
        {
            "SECURITY_CODE": symbol,
            "TRADE_DATE": "2026-07-28 00:00:00",
            "EXPLANATION": "日涨幅偏离值达7%",
            "TRADE_ID": f"event-{symbol}",
            "OPERATEDEPT_CODE": f"buy-{symbol}",
            "OPERATEDEPT_NAME": "样本买方营业部",
            "BUY": 100,
            "SELL": 0,
            "NET": 100,
            "TOTAL_BUYRIO": 0.1,
            "TOTAL_SELLRIO": 0,
        }
        for symbol in ("600519", "000001")
    ]
    sell_seats = [
        {
            "SECURITY_CODE": symbol,
            "TRADE_DATE": "2026-07-28 00:00:00",
            "EXPLANATION": "日涨幅偏离值达7%",
            "TRADE_ID": f"event-{symbol}",
            "OPERATEDEPT_CODE": f"sell-{symbol}",
            "OPERATEDEPT_NAME": "样本卖方营业部",
            "BUY": 0,
            "SELL": 50,
            "NET": -50,
            "TOTAL_BUYRIO": 0,
            "TOTAL_SELLRIO": 0.05,
        }
        for symbol in ("600519", "000001")
    ]
    trades = pd.DataFrame(
        [
            {
                "交易日期": date(2026, 7, 28),
                "证券代码": symbol,
                "成交价": 10,
                "成交量": 2,
                "成交额": 200000,
                "买方营业部": "买方",
                "卖方营业部": "卖方",
            }
            for symbol in ("600519", "000001")
        ]
    )
    parameters = {
        "start": "2026-07-28",
        "end": "2026-07-28",
        "instrument": "SSE.600519",
    }
    with (
        patch(
            "service_data_sync.infrastructure.providers.akshare.p0_market_data.ak.stock_yjyg_em",
            return_value=guidance,
        ),
        patch(
            "service_data_sync.infrastructure.providers.akshare.p0_market_data.ak.stock_yjkb_em",
            return_value=pd.DataFrame(),
        ),
        patch(
            "service_data_sync.infrastructure.providers.akshare.p0_market_data.ak.stock_dzjy_mrmx",
            return_value=trades,
        ),
    ):
        corporate = _normalize_corporate_events(
            parameters,
            groups=[
                (
                    date(2026, 6, 30),
                    _frame_records(guidance),
                    [],
                )
            ],
        )
        dragon = _normalize_dragon_tiger(
            parameters,
            head_rows=head,
            buy_rows=buy_seats,
            sell_rows=sell_seats,
        )
        block, _ = _block_trades(parameters)

    assert {item["securityCode"] for item in corporate["documents"]} == {"600519"}
    assert {item["securityCode"] for item in dragon["events"]} == {"600519"}
    assert {item["securityCode"] for item in block["trades"]} == {"600519"}
