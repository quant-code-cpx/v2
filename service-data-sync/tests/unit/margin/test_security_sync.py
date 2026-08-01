"""融资融券 P0 证券明细与资格标准载荷的边界测试。"""

from __future__ import annotations

import asyncio
import json
from datetime import date
from decimal import Decimal
from typing import cast

import pytest

from service_data_sync.application.margin.security_sync import (
    MarginSecurityDailySyncService,
    decode_margin_eligibility_batch,
    decode_margin_security_daily_batch,
)
from service_data_sync.application.ports.data_source import (
    DataSourcePort,
    ProviderBatch,
    ProviderError,
    ProviderErrorCode,
    SourceRequest,
)
from service_data_sync.application.ports.margin_market import MarginSecurityDailyRepository
from service_data_sync.application.ports.market_data import RawPayloadStore
from service_data_sync.domain.margin import MarginVenue


class _NoCallMarginSource:
    """验证北交所证券日明细在应用层被拒绝，不能绕过 adapter 的能力边界。"""

    provider_id = "no-call-margin-source"

    def __init__(self) -> None:
        """初始化来源调用计数，供 BSE 明确不支持测试使用。"""
        self.capabilities_calls = 0
        self.fetch_calls = 0

    def capabilities(self) -> frozenset[str]:
        """记录 capability 查询；边界正确时此方法不应被调用。"""
        self.capabilities_calls += 1
        return frozenset({"market.margin.security.1d.reported"})

    async def fetch(self, request: SourceRequest) -> ProviderBatch:
        """禁止测试路径真正请求来源，若执行说明 BSE 能力门禁失效。"""
        self.fetch_calls += 1
        raise AssertionError(f"unexpected source request: {request.capability}")


def test_security_decoder_keeps_missing_reported_repayment_as_null() -> None:
    """深市未直报偿还金额应保持空值，而不是由前后余额自动写入派生金额。"""
    payload = json.dumps(
        {
            "schema": "quant-v2.margin-security-daily.v1",
            "venue": "SZSE",
            "valueKind": "REPORTED",
            "records": [
                {
                    "securityCode": "000001",
                    "tradeDate": "2026-07-28",
                    "financingBalance": "0",
                    "financingBuyAmount": "100",
                    "financingRepaymentReported": None,
                    "financingRepaymentDerived": None,
                    "lendingBalanceQty": None,
                    "quantityUnit": None,
                    "currency": "CNY",
                    "nullReason": "NOT_PUBLISHED",
                }
            ],
        }
    ).encode()

    records = decode_margin_security_daily_batch(payload, venue=MarginVenue("SZSE"))

    assert records[0].financing_balance == Decimal("0")
    assert records[0].financing_repayment_reported is None
    assert records[0].financing_repayment_derived is None


def test_security_decoder_rejects_derived_repayment_in_p0() -> None:
    """差分算出的偿还额必须去独立 P1 派生版本，不能伪装成 P0 直报事实。"""
    payload = json.dumps(
        {
            "schema": "quant-v2.margin-security-daily.v1",
            "venue": "SSE",
            "valueKind": "REPORTED",
            "records": [
                {
                    "securityCode": "600000",
                    "tradeDate": "2026-07-28",
                    "financingBalance": "100",
                    "financingBuyAmount": None,
                    "financingRepaymentReported": None,
                    "financingRepaymentDerived": "20",
                    "lendingBalanceQty": None,
                    "quantityUnit": None,
                    "currency": "CNY",
                    "nullReason": None,
                }
            ],
        }
    ).encode()

    with pytest.raises(ProviderError, match="value is invalid"):
        decode_margin_security_daily_batch(payload, venue=MarginVenue("SSE"))


def test_eligibility_decoder_requires_announcement_date_for_official_evidence() -> None:
    """官方资格变更必须带公告日期；当前名单只能声明为 observation-based 证据。"""
    payload = json.dumps(
        {
            "schema": "quant-v2.margin-eligibility.v1",
            "venue": "SSE",
            "records": [
                {
                    "securityCode": "600000",
                    "status": "ELIGIBLE",
                    "effectiveFrom": "2026-07-28",
                    "effectiveTo": None,
                    "announcementOn": None,
                    "evidenceBasis": "OFFICIAL_ANNOUNCEMENT",
                }
            ],
        }
    ).encode()

    with pytest.raises(ProviderError, match="value is invalid"):
        decode_margin_eligibility_batch(payload, venue=MarginVenue("SSE"))


def test_eligibility_decoder_rejects_competing_evidence_for_one_effective_fact() -> None:
    """同一证券和生效日只能有一个可发布证据，避免双时间约束产生互相重叠的当前事实。"""
    payload = json.dumps(
        {
            "schema": "quant-v2.margin-eligibility.v1",
            "venue": "SSE",
            "records": [
                {
                    "securityCode": "600000",
                    "status": "ELIGIBLE",
                    "effectiveFrom": "2026-07-28",
                    "effectiveTo": None,
                    "announcementOn": "2026-07-27",
                    "evidenceBasis": "OFFICIAL_ANNOUNCEMENT",
                },
                {
                    "securityCode": "600000",
                    "status": "ELIGIBLE",
                    "effectiveFrom": "2026-07-28",
                    "effectiveTo": None,
                    "announcementOn": None,
                    "evidenceBasis": "OBSERVED_LIST",
                },
            ],
        }
    ).encode()

    with pytest.raises(ProviderError, match="duplicate identities"):
        decode_margin_eligibility_batch(payload, venue=MarginVenue("SSE"))


def test_bse_eligibility_decoder_preserves_all_four_observed_list_statuses() -> None:
    """北交所标的观察的四种资格均可经通用应用解码，且不伪造公告日期。"""
    payload = json.dumps(
        {
            "schema": "quant-v2.margin-eligibility.v1",
            "venue": "BSE",
            "records": [
                {
                    "securityCode": "920000",
                    "status": "ELIGIBLE",
                    "effectiveFrom": "2026-07-31",
                    "effectiveTo": None,
                    "announcementOn": None,
                    "evidenceBasis": "OBSERVED_LIST",
                },
                {
                    "securityCode": "920001",
                    "status": "FINANCING_ONLY",
                    "effectiveFrom": "2026-07-31",
                    "effectiveTo": None,
                    "announcementOn": None,
                    "evidenceBasis": "OBSERVED_LIST",
                },
                {
                    "securityCode": "920002",
                    "status": "LENDING_ONLY",
                    "effectiveFrom": "2026-07-31",
                    "effectiveTo": None,
                    "announcementOn": None,
                    "evidenceBasis": "OBSERVED_LIST",
                },
                {
                    "securityCode": "920003",
                    "status": "INELIGIBLE",
                    "effectiveFrom": "2026-07-31",
                    "effectiveTo": None,
                    "announcementOn": None,
                    "evidenceBasis": "OBSERVED_LIST",
                },
            ],
        }
    ).encode()

    records = decode_margin_eligibility_batch(payload, venue=MarginVenue("BSE"))

    assert [record.status for record in records] == [
        "ELIGIBLE",
        "FINANCING_ONLY",
        "LENDING_ONLY",
        "INELIGIBLE",
    ]
    assert all(record.evidence_basis == "OBSERVED_LIST" for record in records)
    assert all(record.announcement_on is None for record in records)


def test_security_and_eligibility_decoders_accept_legal_empty_windows() -> None:
    """无证券明细或资格变更是可观测空集，不能被当作来源 schema 漂移。"""
    security_payload = json.dumps(
        {
            "schema": "quant-v2.margin-security-daily.v1",
            "venue": "SSE",
            "valueKind": "REPORTED",
            "records": [],
        }
    ).encode()
    eligibility_payload = json.dumps(
        {"schema": "quant-v2.margin-eligibility.v1", "venue": "SSE", "records": []}
    ).encode()

    assert decode_margin_security_daily_batch(security_payload, venue=MarginVenue("SSE")) == ()
    assert decode_margin_eligibility_batch(eligibility_payload, venue=MarginVenue("SSE")) == ()


def test_security_daily_service_rejects_bse_before_calling_the_source() -> None:
    """北交所资格清单不能扩写成证券日余额，应用服务必须在 source 调用前失败关闭。"""
    source = _NoCallMarginSource()
    service = MarginSecurityDailySyncService(
        source=cast(DataSourcePort, source),
        repository=cast(MarginSecurityDailyRepository, object()),
        raw_payload_store=cast(RawPayloadStore, object()),
    )

    with pytest.raises(ProviderError) as captured:
        asyncio.run(
            service.sync(
                venue=MarginVenue("BSE"),
                start=date(2026, 7, 31),
                end=date(2026, 7, 31),
            )
        )

    assert captured.value.code is ProviderErrorCode.CURRENTLY_UNSUPPORTED
    assert source.capabilities_calls == 0
    assert source.fetch_calls == 0
