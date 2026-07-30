"""ETF P0 未复权日线的 adapter 合同、raw-first 与口径边界测试。"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import cast
from uuid import uuid4

import pytest

from service_data_sync.application.etf.daily_bar_sync import (
    EtfDailyBarSyncService,
    decode_etf_daily_bar_batch,
)
from service_data_sync.application.ports.data_source import (
    ProviderBatch,
    ProviderError,
    ProviderErrorCode,
    SourceRequest,
)
from service_data_sync.application.ports.dataset_availability import DatasetAvailability
from service_data_sync.application.ports.etf_market import (
    EtfSourceObservation,
    PublishedEtfDailyBars,
)
from service_data_sync.application.ports.market_data import RawPayload
from service_data_sync.domain.etf import EtfDailyBar, EtfIdentifier


class FakeSource:
    """返回一个未经复权的沪市 ETF fixture，模拟已通过 adapter 标准化的载荷。"""

    provider_id = "fixture-etf"

    def capabilities(self) -> frozenset[str]:
        """仅声明 ETF P0 日线能力。"""
        return frozenset({"fund.etf.bar.1d.raw"})

    async def fetch(self, request: SourceRequest) -> ProviderBatch:
        """按请求 ETF 回显确定性标准 JSON，同时保留不同的上游 raw 字节。"""
        etf = dict(request.parameters)["etf"]
        payload = json.dumps(
            {
                "schema": "quant-v2.etf-daily-bar.v1",
                "etf": etf,
                "priceBasis": "UNADJUSTED",
                "bars": [
                    {
                        "tradeDate": "2026-07-28",
                        "open": "4.120",
                        "high": "4.180",
                        "low": "4.100",
                        "close": "4.160",
                        "volume": "1200000",
                        "volumeUnit": "SHARE",
                        "amount": "4992000",
                        "currency": "CNY",
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
    """记录 raw 与 normalized 两份对象，确保应用层未将标准载荷当作唯一原始证据。"""

    def __init__(self) -> None:
        """创建内存对象列表。"""
        self.payloads: list[RawPayload] = []

    def put(self, payload: RawPayload) -> str:
        """保存对象并回传稳定 URI。"""
        self.payloads.append(payload)
        return f"s3://test/{payload.object_key}"

    def get(self, uri: str) -> bytes:
        """本测试不读取对象，错误调用应立即暴露。"""
        raise AssertionError(f"unexpected raw read: {uri}")


class FakeRepository:
    """捕获领域日线和双载荷来源观察，不访问真实数据库。"""

    def __init__(self) -> None:
        """初始化空的捕获状态。"""
        self.bars: tuple[EtfDailyBar, ...] = ()
        self.normalized_uri: str | None = None

    def publish_daily_bars(self, **kwargs: object) -> PublishedEtfDailyBars:
        """记录调用值并回传最小成功发布结果。"""
        etf = kwargs["etf"]
        bars = kwargs["bars"]
        source = cast(EtfSourceObservation, kwargs["source"])
        assert isinstance(etf, EtfIdentifier)
        assert isinstance(bars, tuple)
        self.bars = bars
        self.normalized_uri = source.normalized_uri
        return PublishedEtfDailyBars(
            data_version=uuid4(),
            inserted_count=len(bars),
            unchanged_count=0,
            etf=etf,
        )


class FakeAvailabilityRepository:
    """记录 ETF 空状态和成功发布后的终结动作，不访问数据库。"""

    def __init__(self) -> None:
        """初始化最近一次观测和终结标记。"""
        self.observation: DatasetAvailability | None = None
        self.cleared = False

    def record(self, **kwargs: object) -> DatasetAvailability:
        """捕获空观测写入参数，并构造端口要求的稳定返回值。"""
        assert kwargs["availability"] in {"empty", "source_unavailable"}
        observed_at = kwargs["observed_at"]
        assert isinstance(observed_at, datetime)
        self.observation = DatasetAvailability(
            availability=str(kwargs["availability"]),
            reason_code=str(kwargs["reason_code"]),
            observed_at=observed_at,
            entity_partition=str(kwargs["entity_partition"]),
            coverage_from=cast(date, kwargs["coverage_from"]),
            coverage_to=cast(date, kwargs["coverage_to"]),
        )
        return self.observation

    def clear(self, **kwargs: object) -> None:
        """记录真实发布会终结旧观测，避免测试替身遗漏端口调用。"""
        assert isinstance(kwargs["cleared_at"], datetime)
        self.cleared = True


def test_sync_stages_payloads_then_publishes_unadjusted_bars_and_clears_old_empty_state() -> None:
    """P0 日线只发布未复权价格，成功发布还必须终结相同窗口的旧空状态。"""
    etf = EtfIdentifier.parse("SSE.510300")
    raw_store = FakeRawPayloadStore()
    repository = FakeRepository()
    availability_repository = FakeAvailabilityRepository()

    result = asyncio.run(
        EtfDailyBarSyncService(
            source=FakeSource(),
            repository=repository,
            raw_payload_store=raw_store,
            availability_repository=availability_repository,
        ).sync(etf=etf, start=date(2026, 7, 1), end=date(2026, 7, 28))
    )

    assert result.etf == etf
    assert result.inserted_count == 1
    assert raw_store.payloads[0].payload == b'{"official":true}'
    assert raw_store.payloads[1].payload.startswith(b'{"schema":"quant-v2.etf-daily-bar.v1"')
    assert repository.normalized_uri is not None
    assert repository.bars[0].close_price == Decimal("4.160")
    assert repository.bars[0].volume_unit == "SHARE"
    assert availability_repository.cleared is True


def test_sync_records_legal_empty_payload_without_staging_success_bytes() -> None:
    """ETF 合法空集只写可用性元数据，不向事实表或成功 raw 路径写入对象。"""
    etf = EtfIdentifier.parse("SSE.510300")
    raw_store = FakeRawPayloadStore()
    repository = FakeRepository()
    availability_repository = FakeAvailabilityRepository()

    result = asyncio.run(
        EtfDailyBarSyncService(
            source=EmptySource(),
            repository=repository,
            raw_payload_store=raw_store,
            availability_repository=availability_repository,
        ).sync(etf=etf, start=date(2026, 7, 1), end=date(2026, 7, 28))
    )

    assert result.data_version is None
    assert result.availability == "empty"
    assert availability_repository.observation is not None
    assert availability_repository.observation.reason_code == "no_matching_facts"
    assert availability_repository.observation.entity_partition == "etf:SSE.510300"
    assert availability_repository.observation.coverage_from == date(2026, 7, 1)
    assert availability_repository.observation.coverage_to == date(2026, 7, 28)
    assert raw_store.payloads == []


@pytest.mark.parametrize(
    ("code", "retryable"),
    (
        (ProviderErrorCode.RATE_LIMITED, True),
        (ProviderErrorCode.AUTHENTICATION, False),
        (ProviderErrorCode.INVALID_REQUEST, False),
    ),
)
def test_sync_preserves_source_failure_reason_and_retryability(
    code: ProviderErrorCode,
    retryable: bool,
) -> None:
    """来源空态 DTO 必须保留稳定原因与重试语义，executor 不得把永久错误改成可重试。"""
    result = asyncio.run(
        EtfDailyBarSyncService(
            source=FailingSource(code=code, retryable=retryable),
            repository=FakeRepository(),
            raw_payload_store=FakeRawPayloadStore(),
        ).sync(
            etf=EtfIdentifier.parse("SSE.510300"),
            start=date(2026, 7, 1),
            end=date(2026, 7, 28),
        )
    )

    assert result.availability == "source_unavailable"
    assert result.reason_code == code.value
    assert result.retryable is retryable


def test_decoder_rejects_adjusted_etf_prices() -> None:
    """复权口径属于独立派生版本，不能通过 P0 `fund.etf.bar.1d.raw` 入口混入原始价格。"""
    payload = json.dumps(
        {
            "schema": "quant-v2.etf-daily-bar.v1",
            "etf": "SSE.510300",
            "priceBasis": "QFQ",
            "bars": [],
        }
    ).encode()

    with pytest.raises(ProviderError, match="UNADJUSTED"):
        decode_etf_daily_bar_batch(payload, etf=EtfIdentifier.parse("SSE.510300"))


class EmptySource(FakeSource):
    """返回结构合法但没有匹配 ETF 日线的来源批次。"""

    async def fetch(self, request: SourceRequest) -> ProviderBatch:
        """输出空 bars，模拟请求窗口内未披露 ETF 日线。"""
        etf = dict(request.parameters)["etf"]
        return ProviderBatch(
            provider_id=self.provider_id,
            capability=request.capability,
            payload=json.dumps(
                {
                    "schema": "quant-v2.etf-daily-bar.v1",
                    "etf": etf,
                    "priceBasis": "UNADJUSTED",
                    "bars": [],
                }
            ).encode(),
            observed_at=datetime(2026, 7, 29, tzinfo=UTC),
        )


class FailingSource(FakeSource):
    """按测试参数返回来源级失败，用于冻结跨层 reason/retryable 语义。"""

    def __init__(self, *, code: ProviderErrorCode, retryable: bool) -> None:
        """保存待抛出的稳定错误分类和是否可重试。"""
        self._code = code
        self._retryable = retryable

    async def fetch(self, request: SourceRequest) -> ProviderBatch:
        """拒绝请求并抛出不含 Provider 原文的标准失败。"""
        del request
        raise ProviderError(self._code, "fixture source failure", retryable=self._retryable)
