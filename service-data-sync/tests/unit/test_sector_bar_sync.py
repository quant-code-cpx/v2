"""板块原始归档、标准解码和发布编排的单元测试。"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime
from typing import cast
from uuid import UUID, uuid4

from service_data_sync.application.ports.data_source import ProviderBatch, SourceRequest
from service_data_sync.application.ports.market_data import RawPayload
from service_data_sync.application.ports.sector_market_data import (
    PublishedSectorBars,
    SectorMarketDataRepository,
    StoredSector,
)
from service_data_sync.application.sector.bar_sync import SectorBarSyncService
from service_data_sync.domain.sector import SectorBar, SectorIdentifier, SectorPeriod, SectorScheme


class FakeSource:
    """为应用用例返回确定性、中立的周线板块批次。"""

    provider_id = "fake-sector-bars"

    def capabilities(self) -> frozenset[str]:
        """声明三种独立周期能力，使测试不依赖供应商实现。"""
        return frozenset(period.capability for period in SectorPeriod)

    async def fetch(self, request: SourceRequest) -> ProviderBatch:
        """回显通用请求字段并构造可解码的单条原生周线。"""
        parameters = dict(request.parameters)
        payload = json.dumps(
            {
                "schema": "quant-v2.sector-bar.v1",
                "sectorScheme": parameters["sectorScheme"],
                "sector": parameters["sector"],
                "period": parameters["period"],
                "bars": [
                    {
                        "periodEnd": "2026-06-26",
                        "open": "10",
                        "high": "11",
                        "low": "9",
                        "close": "10.5",
                        "volumeValue": "1000",
                        "volumeUnit": "provider_native",
                        "amountCny": "10500",
                        "amplitudePercent": "20",
                        "changePercent": "5",
                        "changeAmount": "0.5",
                        "turnoverPercent": "3",
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
            observed_at=datetime(2026, 6, 27, tzinfo=UTC),
        )


class FakeRawPayloadStore:
    """捕获写入顺序前的来源证据，不使用对象存储客户端。"""

    def __init__(self) -> None:
        """创建空的原始证据记录容器。"""
        self.payloads: list[RawPayload] = []

    def put(self, payload: RawPayload) -> str:
        """记录证据并返回可断言的稳定对象 URI。"""
        self.payloads.append(payload)
        return f"s3://test/{payload.object_key}"

    def get(self, uri: str) -> bytes:
        """板块 K 线同步不支持从该替身 replay，调用表示测试流程错误。"""
        raise AssertionError(f"unexpected raw replay read: {uri}")


class FakeRepository:
    """捕获标准写入输入并返回最小发布结果。"""

    def __init__(self, raw_store: FakeRawPayloadStore) -> None:
        """保存原始存储引用，以验证证据归档先于 canonical 发布。"""
        self._raw_store = raw_store
        self.bars: tuple[SectorBar, ...] = ()
        self.period: SectorPeriod | None = None

    def publish_bars(self, **kwargs: object) -> PublishedSectorBars:
        """断言先有 raw 证据，再记录领域行和周期。"""
        assert self._raw_store.payloads
        identifier = kwargs["identifier"]
        assert isinstance(identifier, SectorIdentifier)
        period = kwargs["period"]
        assert isinstance(period, SectorPeriod)
        bars = kwargs["bars"]
        assert isinstance(bars, tuple)
        self.bars = bars
        self.period = period
        return PublishedSectorBars(
            data_version=uuid4(),
            inserted_count=len(bars),
            unchanged_count=0,
            sector=StoredSector(
                sector_key=1,
                sector_id=uuid4(),
                identifier=identifier,
                name=None,
                status="PENDING",
            ),
        )

    def get_sector(self, sector_id: UUID) -> StoredSector | None:
        """提供端口要求但本用例未调用的空板块身份读取。"""
        del sector_id
        return None

    def list_bars(
        self,
        *,
        sector_id: UUID,
        period: SectorPeriod,
        start: date,
        end: date,
    ) -> tuple[tuple[SectorBar, int, bool], ...]:
        """提供端口要求但本用例未调用的空周期行情读取。"""
        del sector_id, period, start, end
        return ()


def test_sync_archives_raw_evidence_and_publishes_direct_weekly_bars() -> None:
    """周线必须从周线 capability 获取、归档并发布，不能由日线产生。"""
    raw_store = FakeRawPayloadStore()
    repository = FakeRepository(raw_store)
    identifier = SectorIdentifier(SectorScheme.EASTMONEY_INDUSTRY, "BK0475")

    result = asyncio.run(
        SectorBarSyncService(
            source=FakeSource(),
            repository=cast(SectorMarketDataRepository, repository),
            raw_payload_store=raw_store,
        ).sync(
            identifier=identifier,
            period=SectorPeriod.WEEK_1,
            start=date(2026, 6, 1),
            end=date(2026, 6, 30),
        )
    )

    assert result.period is SectorPeriod.WEEK_1
    assert result.inserted_count == 1
    assert raw_store.payloads[0].payload == b'{"raw":true}'
    assert repository.period is SectorPeriod.WEEK_1
    assert repository.bars[0].period_end == date(2026, 6, 26)
