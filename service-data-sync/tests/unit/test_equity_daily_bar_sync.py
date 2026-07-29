"""来源证据、标准解码与幂等发布编排的单元测试。"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from service_data_sync.application.equity.daily_bar_sync import EquityDailyBarSyncService
from service_data_sync.application.ports.data_source import (
    ProviderBatch,
    ProviderError,
    ProviderErrorCode,
    SourceRequest,
)
from service_data_sync.application.ports.market_data import (
    EquityAvailabilityObservation,
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

    def get(self, uri: str) -> bytes:
        """旧日线用例不执行 replay；若意外读取则立刻暴露错误路径。"""
        raise AssertionError(f"unexpected raw replay read: {uri}")


class FakeRepository:
    """捕获标准写入输入，并返回发布形状的响应。"""

    def __init__(self) -> None:
        """初始化保存替身最近一次发布请求的存储。"""
        self.bars: tuple[EquityDailyBar, ...] = ()
        self.availability: EquityAvailabilityObservation | None = None
        self.availability_cleared = False

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

    def record_daily_bar_availability(self, **kwargs: object) -> EquityAvailabilityObservation:
        """记录空集或来源不可用结果，验证用例不把它伪装成日线事实。"""
        assert kwargs["availability"] in {"empty", "source_unavailable"}
        observed_at = kwargs["observed_at"]
        assert isinstance(observed_at, datetime)
        self.availability = EquityAvailabilityObservation(
            availability=str(kwargs["availability"]),
            reason_code=str(kwargs["reason_code"]),
            observed_at=observed_at,
        )
        return self.availability

    def clear_daily_bar_availability(self, **kwargs: object) -> None:
        """记录成功发布已终结同窗口旧空状态，保持替身端口完整。"""
        assert isinstance(kwargs["identifier"], EquityIdentifier)
        assert isinstance(kwargs["start"], date)
        assert isinstance(kwargs["end"], date)
        assert isinstance(kwargs["cleared_at"], datetime)
        self.availability_cleared = True

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
    assert result.availability == "available"
    assert raw_store.payloads[0].payload == b'{"raw":true}'
    assert repository.bars[0].close_price == Decimal("10.5")
    assert repository.availability_cleared is True


def test_sync_records_empty_observation_without_persisting_success_payload() -> None:
    """合法空集必须完成同步并记录元数据，不能写入全空 canonical 日线。"""
    raw_store = FakeRawPayloadStore()
    repository = FakeRepository()
    identifier = EquityIdentifier.parse("SSE.600519")
    result = asyncio.run(
        EquityDailyBarSyncService(
            source=EmptySource(), repository=repository, raw_payload_store=raw_store
        ).sync(identifier=identifier, start=date(2026, 7, 1), end=date(2026, 7, 29))
    )

    assert result.data_version is None
    assert result.availability == "empty"
    assert repository.bars == ()
    assert repository.availability is not None
    assert repository.availability.reason_code == "no_matching_facts"
    assert raw_store.payloads == []


def test_sync_records_source_unavailable_as_successful_empty_state() -> None:
    """AKShare 暂不可用不能中断个人使用链路，也不应产生失败 raw 对象。"""
    raw_store = FakeRawPayloadStore()
    repository = FakeRepository()
    result = asyncio.run(
        EquityDailyBarSyncService(
            source=UnavailableSource(), repository=repository, raw_payload_store=raw_store
        ).sync(
            identifier=EquityIdentifier.parse("SSE.600519"),
            start=date(2026, 7, 1),
            end=date(2026, 7, 29),
        )
    )

    assert result.data_version is None
    assert result.availability == "source_unavailable"
    assert repository.availability is not None
    assert repository.availability.reason_code == "unavailable"
    assert raw_store.payloads == []


class EmptySource(FakeSource):
    """返回结构合法但没有匹配日线的来源批次。"""

    async def fetch(self, request: SourceRequest) -> ProviderBatch:
        """输出空 bars 数组，模拟 AKShare 该窗口没有可用记录。"""
        instrument = dict(request.parameters)["instrument"]
        return ProviderBatch(
            provider_id=self.provider_id,
            capability=request.capability,
            payload=json.dumps(
                {
                    "schema": "quant-v2.equity-daily-bar.v1",
                    "instrument": instrument,
                    "bars": [],
                },
                separators=(",", ":"),
            ).encode(),
            observed_at=datetime(2026, 7, 29, tzinfo=UTC),
        )


class UnavailableSource(FakeSource):
    """模拟尚未配置或暂时无法访问的 AKShare 能力。"""

    async def fetch(self, request: SourceRequest) -> ProviderBatch:
        """返回可重试来源不可用错误，不提供任何可留存来源字节。"""
        del request
        raise ProviderError(ProviderErrorCode.UNAVAILABLE, "provider unavailable", retryable=True)
