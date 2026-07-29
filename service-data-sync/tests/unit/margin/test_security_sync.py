"""融资融券 P0 证券明细与资格标准载荷的边界测试。"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from service_data_sync.application.margin.security_sync import (
    decode_margin_eligibility_batch,
    decode_margin_security_daily_batch,
)
from service_data_sync.application.ports.data_source import ProviderError
from service_data_sync.domain.margin import MarginVenue


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
