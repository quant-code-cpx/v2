"""来源证据、标准解码与幂等发布编排的单元测试。"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from service_data_sync.application.equity.daily_bar_sync import EquityDailyBarSyncService
from service_data_sync.application.ports.data_source import ProviderBatch, SourceRequest
from service_data_sync.application.ports.market_data import (
    PublishedDailyBars,
    RawPayload,
    StoredEquityInstrument,
)
from service_data_sync.domain.equity import EquityDailyBar, EquityIdentifier


class FakeSource:
    """为用例测试返回一个确定性的数据源无关日线批次。"""

    provider_id = "fake-daily-bars"

    def capabilities(self) -> frozenset[str]:
        """仅声明 P0 日线能力。"""
        return frozenset({"equity.bar.1d.raw"})

    async def fetch(self, request: SourceRequest) -> ProviderBatch:
        """构造有效批次，并回显请求中的标准证券身份。"""
        instrument = dict(request.parameters)["instrument"]
        payload = json.dumps(
            {
                "schema": "quant-v2.equity-daily-bar.v1",
                "instrument": instrument,
                "bars": [
                    {
                        "tradeDate": "2026-06-30",
                        "open": "10.0",
                        "high": "11.0",
                        "low": "9.0",
                        "close": "10.5",
                        "volumeShares": "1000",
                        "amountCny": "10500.0",
                        "turnoverRate": None,
                    }
                ],
            },
            separators=(",", ":"),
        ).encode()
        return ProviderBatch(
            provider_id=self.provider_id,
            capability=request.capability,
            payload=payload,
            raw_payload=b'{"raw":true}',
            raw_content_type="application/json",
            observed_at=datetime(2026, 7, 1, tzinfo=UTC),
        )


class FakeRawPayloadStore:
    """记录用例在数据库发布前传入的不可变来源证据。"""

    def __init__(self) -> None:
        """初始化空的内存原始证据捕获容器。"""
        self.payloads: list[RawPayload] = []

    def put(self, payload: RawPayload) -> str:
        """捕获原始字节，不经网络 I/O 返回确定性对象 URI。"""
        self.payloads.append(payload)
        return f"s3://test/{payload.object_key}"


class FakeRepository:
    """捕获标准写入输入，并返回发布形状的响应。"""

    def __init__(self) -> None:
        """初始化保存替身最近一次发布请求的存储。"""
        self.bars: tuple[EquityDailyBar, ...] = ()

    def publish_daily_bars(self, **kwargs: object) -> PublishedDailyBars:
        """保存标准化日线，并返回最小的当前证券发布结果。"""
        identifier = kwargs["identifier"]
        assert isinstance(identifier, EquityIdentifier)
        bars = kwargs["bars"]
        assert isinstance(bars, tuple)
        self.bars = bars
        instrument = StoredEquityInstrument(
            security_id=1,
            instrument_id=uuid4(),
            identifier=identifier,
            name=None,
            listing_status="PENDING",
        )
        return PublishedDailyBars(
            data_version=uuid4(),
            inserted_count=len(bars),
            unchanged_count=0,
            instrument=instrument,
        )

    def get_instrument(self, instrument_id: UUID) -> StoredEquityInstrument | None:
        """在写入编排测试期间保持替身与仓储端口兼容。"""
        del instrument_id
        return None

    def list_instruments(
        self, *, query: str | None, limit: int
    ) -> tuple[StoredEquityInstrument, ...]:
        """提供仓储端口要求、但本测试未使用的空目录读取。"""
        del query, limit
        return ()

    def list_daily_bars(
        self,
        *,
        instrument_id: UUID,
        start: date,
        end: date,
    ) -> tuple[tuple[EquityDailyBar, int, bool], ...]:
        """提供仓储端口要求、但本测试未使用的空日线读取。"""
        del instrument_id, start, end
        return ()


def test_sync_archives_raw_evidence_before_publishing_normalized_daily_bars() -> None:
    """保证用例中适配器证据与标准写入载荷可被分别观察。"""
    raw_store = FakeRawPayloadStore()
    repository = FakeRepository()
    identifier = EquityIdentifier.parse("SSE.600519")

    result = asyncio.run(
        EquityDailyBarSyncService(
            source=FakeSource(),
            repository=repository,
            raw_payload_store=raw_store,
        ).sync(identifier=identifier, start=date(2026, 6, 1), end=date(2026, 6, 30))
    )

    assert result.instrument == identifier
    assert result.inserted_count == 1
    assert raw_store.payloads[0].payload == b'{"raw":true}'
    assert repository.bars[0].close_price == Decimal("10.5")
