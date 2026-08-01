"""板块原始归档、标准解码和发布编排的单元测试。"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime
from typing import cast
from uuid import UUID, uuid4

from service_data_sync.application.ports.data_source import ProviderBatch, SourceRequest
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


class FakeRepository:
    """捕获标准写入输入并返回最小发布结果。"""

    def __init__(self) -> None:
        """保存最后一次 canonical 发布输入。"""
        self.bars: tuple[SectorBar, ...] = ()
        self.period: SectorPeriod | None = None
        self.call: dict[str, object] | None = None

    def publish_bars(self, **kwargs: object) -> PublishedSectorBars:
        """记录领域行、周期和不可回放来源引用。"""
        self.call = kwargs
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


def test_sync_keeps_success_payload_unretained_and_publishes_direct_weekly_bars() -> None:
    """周线必须从周线 capability 获取并入库，成功来源字节不能落对象存储。"""
    repository = FakeRepository()
    identifier = SectorIdentifier(SectorScheme.EASTMONEY_INDUSTRY, "BK0475")

    result = asyncio.run(
        SectorBarSyncService(
            source=FakeSource(),
            repository=cast(SectorMarketDataRepository, repository),
        ).sync(
            identifier=identifier,
            period=SectorPeriod.WEEK_1,
            start=date(2026, 6, 1),
            end=date(2026, 6, 30),
        )
    )

    assert result.period is SectorPeriod.WEEK_1
    assert result.inserted_count == 1
    assert repository.call is not None
    assert str(repository.call["raw_uri"]).startswith("unretained://sha256/")
    assert str(repository.call["normalized_uri"]).startswith("unretained://sha256/")
    assert repository.period is SectorPeriod.WEEK_1
    assert repository.bars[0].period_end == date(2026, 6, 26)
