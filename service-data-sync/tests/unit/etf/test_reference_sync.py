"""ETF P0 产品资料与状态标准载荷的边界测试。"""

from __future__ import annotations

import json

import pytest

from service_data_sync.application.etf.reference_sync import (
    decode_etf_master_batch,
    decode_etf_status_batch,
)
from service_data_sync.application.ports.data_source import ProviderError
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
