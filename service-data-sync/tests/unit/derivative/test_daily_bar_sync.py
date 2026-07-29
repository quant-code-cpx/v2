"""衍生品 P0 真实合约日线同步的来源边界与不变量测试。"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import cast
from uuid import uuid4

import pytest

from service_data_sync.application.derivative.daily_bar_sync import (
    DerivativeDailyBarSyncService,
    decode_derivative_daily_bar_batch,
)
from service_data_sync.application.ports.data_source import (
    ProviderBatch,
    ProviderError,
    SourceRequest,
)
from service_data_sync.application.ports.derivative_market import (
    DerivativeSourceObservation,
    PublishedDerivativeDailyBars,
)
from service_data_sync.application.ports.market_data import RawPayload
from service_data_sync.domain.derivative import DerivativeContractIdentifier, DerivativeDailyBar


class FakeSource:
    """提供一个含结算价和持仓量的确定性真实合约标准批次。"""

    provider_id = "fixture-derivative"

    def capabilities(self) -> frozenset[str]:
        """只声明真实合约日行情能力。"""
        return frozenset({"derivative.bar.1d.reported"})

    async def fetch(self, request: SourceRequest) -> ProviderBatch:
        """生成回显标准合约键的 P0 fixture 批次。"""
        contract = dict(request.parameters)["contract"]
        payload = json.dumps(
            {
                "schema": "quant-v2.derivative-daily-bar.v1",
                "contract": contract,
                "contractKind": "REAL",
                "bars": [
                    {
                        "tradeDate": "2026-07-28",
                        "open": "3450",
                        "high": "3490",
                        "low": "3420",
                        "close": "3475",
                        "preClose": "3440",
                        "settlement": "3468",
                        "preSettlement": "3438",
                        "volume": "1200",
                        "openInterest": "800",
                        "turnover": "41600000",
                        "turnoverCurrency": "CNY",
                        "turnoverUnit": "CNY",
                        "tradeStatus": "TRADING",
                    }
                ],
            },
            separators=(",", ":"),
        ).encode()
        return ProviderBatch(
            provider_id=self.provider_id,
            capability=request.capability,
            payload=payload,
            raw_payload=b'{"official":true}',
            raw_content_type="application/json",
            observed_at=datetime(2026, 7, 29, tzinfo=UTC),
        )


class FakeRawPayloadStore:
    """捕获发布前必须保存的来源字节，不进行外部对象存储操作。"""

    def __init__(self) -> None:
        """初始化内存证据收集列表。"""
        self.payloads: list[RawPayload] = []

    def put(self, payload: RawPayload) -> str:
        """保存传入 raw 证据并返回确定性对象定位。"""
        self.payloads.append(payload)
        return f"s3://test/{payload.object_key}"

    def get(self, uri: str) -> bytes:
        """本用例不读取回放对象，误调用时立即失败。"""
        raise AssertionError(f"unexpected raw read: {uri}")


class FakeRepository:
    """捕获真实合约日线发布输入，验证应用层未混入连续序列。"""

    def __init__(self) -> None:
        """初始化空的最近一次发布记录。"""
        self.bars: tuple[DerivativeDailyBar, ...] = ()
        self.normalized_uri: str | None = None

    def publish_daily_bars(self, **kwargs: object) -> PublishedDerivativeDailyBars:
        """保存领域值并返回带稳定 data version 的最小发布结果。"""
        contract = kwargs["contract"]
        bars = kwargs["bars"]
        assert isinstance(contract, DerivativeContractIdentifier)
        assert isinstance(bars, tuple)
        self.bars = bars
        source = cast(DerivativeSourceObservation, kwargs["source"])
        self.normalized_uri = source.normalized_uri
        return PublishedDerivativeDailyBars(
            data_version=uuid4(),
            inserted_count=len(bars),
            unchanged_count=0,
            contract=contract,
        )


class EmptySource(FakeSource):
    """提供一个 schema 合法但没有合约行情的窗口响应。"""

    async def fetch(self, request: SourceRequest) -> ProviderBatch:
        """返回空 bars，模拟合约未上市或窗口无成交。"""
        contract = dict(request.parameters)["contract"]
        return ProviderBatch(
            provider_id=self.provider_id,
            capability=request.capability,
            payload=json.dumps(
                {
                    "schema": "quant-v2.derivative-daily-bar.v1",
                    "contract": contract,
                    "contractKind": "REAL",
                    "bars": [],
                }
            ).encode(),
            observed_at=datetime(2026, 7, 29, tzinfo=UTC),
        )


def test_sync_archives_raw_evidence_before_publishing_real_contract_daily_bars() -> None:
    """真实合约日线必须先固化原始证据，并保留结算与收盘两个不同字段。"""
    raw_store = FakeRawPayloadStore()
    repository = FakeRepository()
    contract = DerivativeContractIdentifier.parse("CFFEX.IF2608")

    result = asyncio.run(
        DerivativeDailyBarSyncService(
            source=FakeSource(),
            repository=repository,
            raw_payload_store=raw_store,
        ).sync(contract=contract, start=date(2026, 7, 1), end=date(2026, 7, 28))
    )

    assert result.contract == contract
    assert result.inserted_count == 1
    assert raw_store.payloads[0].payload == b'{"official":true}'
    assert raw_store.payloads[1].payload.startswith(b'{"schema":"quant-v2.derivative-daily-bar.v1"')
    assert repository.normalized_uri is not None
    assert repository.bars[0].settlement_price == Decimal("3468")
    assert repository.bars[0].close_price == Decimal("3475")


def test_decoder_rejects_a_continuous_series_payload() -> None:
    """连续合约属于 P2 派生能力，不能通过真实合约 P0 日线入口写入。"""
    payload = json.dumps(
        {
            "schema": "quant-v2.derivative-daily-bar.v1",
            "contract": "SHFE.RB2601",
            "contractKind": "CONTINUOUS",
            "bars": [],
        }
    ).encode()

    with pytest.raises(ProviderError, match="real contract"):
        decode_derivative_daily_bar_batch(
            payload, contract=DerivativeContractIdentifier.parse("SHFE.RB2601")
        )


def test_empty_contract_window_does_not_publish_or_archive_source_payload() -> None:
    """合法空 bars 只产生成功空结果，既不创建发布也不保存来源 payload。"""
    raw_store = FakeRawPayloadStore()
    repository = FakeRepository()
    contract = DerivativeContractIdentifier.parse("CFFEX.IF2608")

    result = asyncio.run(
        DerivativeDailyBarSyncService(
            source=EmptySource(),
            repository=repository,
            raw_payload_store=raw_store,
        ).sync(contract=contract, start=date(2026, 7, 1), end=date(2026, 7, 28))
    )

    assert result.data_version is None
    assert result.availability == "empty"
    assert result.inserted_count == 0
    assert raw_store.payloads == []
    assert repository.bars == ()
