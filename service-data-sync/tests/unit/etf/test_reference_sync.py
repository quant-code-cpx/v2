"""ETF P0 产品资料与状态标准载荷的边界测试。"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime
from typing import cast

import pytest

from service_data_sync.application.etf.reference_sync import (
    EtfMasterSyncService,
    decode_etf_master_batch,
    decode_etf_status_batch,
)
from service_data_sync.application.ports.data_source import (
    DataSourcePort,
    ProviderBatch,
    ProviderError,
    SourceRequest,
)
from service_data_sync.application.ports.dataset_availability import DatasetAvailability
from service_data_sync.application.ports.etf_market import EtfReferenceRepository
from service_data_sync.application.ports.market_data import RawPayloadStore
from service_data_sync.domain.etf import EtfIdentifier


def test_master_decoder_does_not_treat_catalog_as_delisting_inference() -> None:
    """目录快照只能保存来源明确的上市状态，不能由缺席或零成交擅自生成摘牌。"""
    payload = json.dumps(
        {
            "schema": "quant-v2.etf-master.v1",
            "venue": "SSE",
            "profiles": [
                {
                    "symbol": "510300",
                    "etfType": "EQUITY",
                    "managementMode": "PASSIVE",
                    "managerName": None,
                    "custodianName": None,
                    "establishedOn": None,
                    "listedOn": "2012-05-28",
                    "delistedOn": None,
                    "quoteCurrency": "CNY",
                    "navCurrency": "CNY",
                    "listingStatus": "LISTED",
                    "effectiveFrom": "2026-07-28",
                    "sourceTimePrecision": "DATE_ONLY",
                }
            ],
        }
    ).encode()

    profiles = decode_etf_master_batch(payload, venue="SSE")

    assert profiles[0].etf == EtfIdentifier.parse("SSE.510300")
    assert profiles[0].delisted_on is None


def test_status_decoder_keeps_trading_subscription_and_redemption_separate() -> None:
    """交易暂停不等于申购或赎回暂停，来源必须逐维度明确提供状态。"""
    payload = json.dumps(
        {
            "schema": "quant-v2.etf-trading-state.v1",
            "etf": "SSE.510300",
            "statuses": [
                {
                    "dimension": "TRADING",
                    "statusCode": "HALTED",
                    "effectiveFrom": "2026-07-28",
                    "effectiveTo": None,
                    "reason": "停牌",
                },
                {
                    "dimension": "SUBSCRIPTION",
                    "statusCode": "OPEN",
                    "effectiveFrom": "2026-07-28",
                    "effectiveTo": None,
                    "reason": None,
                },
                {
                    "dimension": "REDEMPTION",
                    "statusCode": "OPEN",
                    "effectiveFrom": "2026-07-28",
                    "effectiveTo": None,
                    "reason": None,
                },
            ],
        }
    ).encode()

    statuses = decode_etf_status_batch(payload, etf=EtfIdentifier.parse("SSE.510300"))

    assert [item.status_dimension for item in statuses] == ["REDEMPTION", "SUBSCRIPTION", "TRADING"]


def test_status_decoder_rejects_unexpected_spot_field() -> None:
    """非权威实时行情字段不属于 P0 日级状态，出现时必须等待 adapter/schema 评审。"""
    payload = json.dumps(
        {
            "schema": "quant-v2.etf-trading-state.v1",
            "etf": "SSE.510300",
            "statuses": [
                {
                    "dimension": "TRADING",
                    "statusCode": "OPEN",
                    "effectiveFrom": "2026-07-28",
                    "effectiveTo": None,
                    "reason": None,
                    "lastPrice": "4.0",
                }
            ],
        }
    ).encode()

    with pytest.raises(ProviderError, match="value is invalid"):
        decode_etf_status_batch(payload, etf=EtfIdentifier.parse("SSE.510300"))


def test_reference_decoders_accept_legal_empty_arrays() -> None:
    """目录或状态窗口为空不等于退市、停牌或 schema 漂移。"""
    master_payload = json.dumps(
        {"schema": "quant-v2.etf-master.v1", "venue": "SSE", "profiles": []}
    ).encode()
    status_payload = json.dumps(
        {"schema": "quant-v2.etf-trading-state.v1", "etf": "SSE.510300", "statuses": []}
    ).encode()

    assert decode_etf_master_batch(master_payload, venue="SSE") == ()
    assert decode_etf_status_batch(status_payload, etf=EtfIdentifier.parse("SSE.510300")) == ()


def test_master_service_treats_an_empty_directory_as_source_unavailable() -> None:
    """整市场目录空批不能表示零产品或退市，必须阻止下游把缺 publication 当作完整 authority。"""
    availability = CapturedAvailabilityRepository()
    result = asyncio.run(
        EtfMasterSyncService(
            source=cast(DataSourcePort, EmptyMasterSource()),
            repository=cast(EtfReferenceRepository, object()),
            raw_payload_store=cast(RawPayloadStore, object()),
            availability_repository=availability,
        ).sync(venue="SSE", observation_date=date(2026, 7, 30))
    )

    assert result.availability == "source_unavailable"
    assert result.reason_code == "directory_publication_unavailable"
    assert result.retryable is True
    assert availability.observation is not None
    assert availability.observation.reason_code == "directory_publication_unavailable"


class EmptyMasterSource:
    """返回结构合法但没有任何产品的目录批次，模拟 publication 尚未发布。"""

    provider_id = "fixture-empty-master"

    def capabilities(self) -> frozenset[str]:
        """只声明 ETF 产品目录能力。"""
        return frozenset({"fund.etf.master"})

    async def fetch(self, request: SourceRequest) -> ProviderBatch:
        """返回空目录标准载荷，测试应用层不得把它解释成完整零产品市场。"""
        venue = dict(request.parameters)["venue"]
        return ProviderBatch(
            provider_id=self.provider_id,
            capability=request.capability,
            payload=json.dumps(
                {
                    "schema": "quant-v2.etf-master.v2",
                    "venue": venue,
                    "profiles": [],
                }
            ).encode(),
            observed_at=datetime(2026, 7, 30, tzinfo=UTC),
        )


class CapturedAvailabilityRepository:
    """捕获目录来源不可用观察，不访问数据库。"""

    def __init__(self) -> None:
        """初始化尚无观察的测试状态。"""
        self.observation: DatasetAvailability | None = None

    def record(self, **kwargs: object) -> DatasetAvailability:
        """保存受控空态字段并返回端口 DTO。"""
        self.observation = DatasetAvailability(
            availability=str(kwargs["availability"]),
            reason_code=str(kwargs["reason_code"]),
            observed_at=cast(datetime, kwargs["observed_at"]),
            entity_partition=cast(str, kwargs["entity_partition"]),
            coverage_from=cast(date, kwargs["coverage_from"]),
            coverage_to=cast(date, kwargs["coverage_to"]),
        )
        return self.observation

    def clear(self, **kwargs: object) -> None:
        """本场景没有 publication，任何 clear 都表示实现误判。"""
        raise AssertionError(f"unexpected availability clear: {kwargs}")
