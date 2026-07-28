"""个股周期行情与参考数据应用用例测试。"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import cast
from uuid import uuid4

import pytest

from service_data_sync.application.equity.market_extension_sync import (
    EquityAdjustmentFactorSyncService,
    EquityCompanyProfileSyncService,
    EquityCorporateActionSyncService,
    EquityPeriodBarSyncService,
    decode_adjustment_factor_batch,
    decode_company_profile_batch,
    decode_corporate_action_batch,
    decode_period_bar_batch,
)
from service_data_sync.application.ports.data_source import ProviderBatch, ProviderError
from service_data_sync.application.ports.market_data import (
    PublishedEquityDataset,
    StoredEquityInstrument,
)
from service_data_sync.domain.equity import (
    EquityAdjustmentFactor,
    EquityBarPeriod,
    EquityCompanyProfile,
    EquityCorporateAction,
    EquityIdentifier,
)


class FakeSource:
    """返回预设标准载荷并记录中立请求。"""

    provider_id = "fake"

    def __init__(self, capability: str, payload: bytes) -> None:
        """保存单一能力与标准载荷。"""
        self.capability = capability
        self.payload = payload
        self.requests: list[object] = []

    def capabilities(self) -> frozenset[str]:
        """声明测试能力。"""
        return frozenset({self.capability})

    async def fetch(self, request: object) -> ProviderBatch:
        """记录请求并返回带 raw evidence 的确定性批次。"""
        self.requests.append(request)
        return ProviderBatch(
            provider_id=self.provider_id,
            capability=self.capability,
            payload=self.payload,
            raw_payload=b'{"raw":true}',
            raw_content_type="application/json",
            observed_at=datetime(2026, 7, 28, tzinfo=UTC),
        )


class FakeRawStore:
    """记录 raw evidence 并返回确定性 URI。"""

    def __init__(self) -> None:
        """初始化空写入列表。"""
        self.values: list[object] = []

    def put(self, payload: object) -> str:
        """保存写入值并返回测试 URI。"""
        self.values.append(payload)
        return "s3://test/raw.json"


class FakeRepository:
    """记录四种发布调用并返回统一发布摘要。"""

    def __init__(self) -> None:
        """初始化空调用列表。"""
        self.calls: list[tuple[str, dict[str, object]]] = []

    def publish_period_bars(self, **kwargs: object) -> PublishedEquityDataset:
        """记录周期行情发布。"""
        return self._publish("period", kwargs)

    def publish_adjustment_factors(self, **kwargs: object) -> PublishedEquityDataset:
        """记录累计因子发布。"""
        return self._publish("factor", kwargs)

    def publish_corporate_actions(self, **kwargs: object) -> PublishedEquityDataset:
        """记录公司行动发布。"""
        return self._publish("action", kwargs)

    def publish_company_profile(self, **kwargs: object) -> PublishedEquityDataset:
        """记录公司概况发布。"""
        return self._publish("profile", kwargs)

    def _publish(
        self,
        kind: str,
        kwargs: dict[str, object],
    ) -> PublishedEquityDataset:
        """返回与传入证券一致的确定性发布。"""
        self.calls.append((kind, kwargs))
        identifier = kwargs["identifier"]
        assert isinstance(identifier, EquityIdentifier)
        return PublishedEquityDataset(
            data_version=uuid4(),
            published_at=datetime(2026, 7, 28, tzinfo=UTC),
            inserted_count=1,
            unchanged_count=0,
            instrument=StoredEquityInstrument(
                security_id=1,
                instrument_id=uuid4(),
                identifier=identifier,
                name="贵州茅台",
                listing_status="LISTED",
            ),
        )


def test_period_sync_archives_raw_and_publishes_direct_weekly_rows() -> None:
    """周线用例必须请求独立能力、先归档 raw，再发布标准周线。"""
    identifier = EquityIdentifier.parse("SSE.600519")
    source = FakeSource(
        "equity.bar.1w.raw",
        _payload(
            "quant-v2.equity-period-bar.v1",
            identifier,
            period="1w",
            bars=[
                {
                    "periodEnd": "2026-07-24",
                    "open": "10",
                    "high": "12",
                    "low": "9",
                    "close": "11",
                    "volumeShares": "1000",
                    "amountCny": "10500",
                    "turnoverRate": "0.01",
                }
            ],
        ),
    )
    repository = FakeRepository()
    raw_store = FakeRawStore()

    result = asyncio.run(
        EquityPeriodBarSyncService(
            source=source,
            repository=repository,  # type: ignore[arg-type]
            raw_payload_store=raw_store,  # type: ignore[arg-type]
        ).sync(
            identifier=identifier,
            period=EquityBarPeriod.WEEK_1,
            start=date(2026, 7, 1),
            end=date(2026, 7, 28),
        )
    )

    assert result.capability == "equity.bar.1w.raw"
    assert len(raw_store.values) == 1
    assert repository.calls[0][0] == "period"
    bars = repository.calls[0][1]["bars"]
    assert isinstance(bars, tuple)
    assert bars[0].period is EquityBarPeriod.WEEK_1


def test_factor_action_and_profile_sync_publish_typed_values() -> None:
    """三类参考数据用例分别解析、归档并调用对应仓储方法。"""
    identifier = EquityIdentifier.parse("SSE.600519")
    repository = FakeRepository()
    raw_store = FakeRawStore()
    factor_source = FakeSource(
        "equity.adjustment_factor",
        _payload(
            "quant-v2.equity-adjustment-factor.v1",
            identifier,
            factors=[
                {"effectiveDate": "2026-06-30", "cumulativeFactor": "2.5"},
            ],
        ),
    )
    action_source = FakeSource(
        "equity.corporate_action",
        _payload(
            "quant-v2.equity-corporate-action.v1",
            identifier,
            actions=[
                {
                    "sourceEventKey": "2025-12-31",
                    "reportPeriod": "2025-12-31",
                    "status": "实施",
                    "announcementDate": "2026-06-01",
                    "recordDate": "2026-06-29",
                    "exDate": "2026-06-30",
                    "cashDividendPer10": "10",
                    "bonusSharesPer10": None,
                    "transferSharesPer10": "1",
                }
            ],
        ),
    )
    profile_source = FakeSource(
        "equity.profile",
        _payload(
            "quant-v2.equity-profile.v1",
            identifier,
            profile={
                "companyName": "贵州茅台酒股份有限公司",
                "englishName": None,
                "industry": "白酒",
                "legalRepresentative": None,
                "establishedOn": "1999-11-20",
                "website": "https://example.test",
                "email": None,
                "phone": None,
                "registeredAddress": "贵州",
                "officeAddress": None,
                "mainBusiness": "白酒",
                "businessScope": None,
                "summary": None,
            },
        ),
    )

    factor_result = asyncio.run(
        EquityAdjustmentFactorSyncService(
            source=factor_source,
            repository=repository,  # type: ignore[arg-type]
            raw_payload_store=raw_store,  # type: ignore[arg-type]
        ).sync(identifier=identifier, start=date(1990, 1, 1), end=date(2026, 7, 28))
    )
    action_result = asyncio.run(
        EquityCorporateActionSyncService(
            source=action_source,
            repository=repository,  # type: ignore[arg-type]
            raw_payload_store=raw_store,  # type: ignore[arg-type]
        ).sync(identifier=identifier, start=date(2025, 1, 1), end=date(2026, 7, 28))
    )
    profile_result = asyncio.run(
        EquityCompanyProfileSyncService(
            source=profile_source,
            repository=repository,  # type: ignore[arg-type]
            raw_payload_store=raw_store,  # type: ignore[arg-type]
        ).sync(identifier=identifier)
    )

    assert factor_result.inserted_count == 1
    assert action_result.capability == "equity.corporate_action"
    assert profile_result.capability == "equity.profile"
    factors = cast(tuple[EquityAdjustmentFactor, ...], repository.calls[0][1]["factors"])
    actions = cast(tuple[EquityCorporateAction, ...], repository.calls[1][1]["actions"])
    profile = cast(EquityCompanyProfile, repository.calls[2][1]["profile"])
    assert factors[0].cumulative_factor == Decimal("2.5")
    assert actions[0].cash_dividend_per_10 == Decimal("10")
    assert profile.industry == "白酒"
    assert len(raw_store.values) == 3


@pytest.mark.parametrize(
    ("decoder", "payload"),
    [
        (
            "period",
            {
                "schema": "quant-v2.equity-period-bar.v1",
                "instrument": "SSE.600519",
                "period": "1mo",
                "bars": [],
            },
        ),
        (
            "factor",
            {
                "schema": "quant-v2.equity-adjustment-factor.v1",
                "instrument": "SSE.600519",
                "factors": [{"effectiveDate": "2026-01-01", "cumulativeFactor": "0"}],
            },
        ),
        (
            "action",
            {
                "schema": "quant-v2.equity-corporate-action.v1",
                "instrument": "SSE.600519",
                "actions": [
                    {
                        "sourceEventKey": "x",
                        "reportPeriod": "bad",
                        "status": "实施",
                    }
                ],
            },
        ),
        (
            "profile",
            {
                "schema": "quant-v2.equity-profile.v1",
                "instrument": "SSE.600519",
                "profile": {"companyName": " "},
            },
        ),
    ],
)
def test_decoders_reject_empty_or_invalid_canonical_payload(
    decoder: str,
    payload: dict[str, object],
) -> None:
    """标准载荷的空关键集合、非正因子、坏日期和空公司名必须隔离。"""
    identifier = EquityIdentifier.parse("SSE.600519")
    encoded = json.dumps(payload).encode()

    with pytest.raises(ProviderError):
        if decoder == "period":
            decode_period_bar_batch(
                encoded,
                identifier=identifier,
                period=EquityBarPeriod.MONTH_1,
            )
        elif decoder == "factor":
            decode_adjustment_factor_batch(encoded, identifier=identifier)
        elif decoder == "action":
            decode_corporate_action_batch(encoded, identifier=identifier)
        else:
            decode_company_profile_batch(encoded, identifier=identifier)


def _payload(
    schema: str,
    identifier: EquityIdentifier,
    **values: object,
) -> bytes:
    """构造带标准 schema 与证券身份的 JSON 载荷。"""
    return json.dumps(
        {
            "schema": schema,
            "instrument": identifier.qualified_symbol,
            **values,
        },
        ensure_ascii=False,
    ).encode()
