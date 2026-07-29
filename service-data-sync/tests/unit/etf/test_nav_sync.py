"""ETF P0 单位/累计净值标准载荷的严格解码测试。"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from service_data_sync.application.etf.nav_sync import decode_etf_nav_batch
from service_data_sync.application.ports.data_source import ProviderError
from service_data_sync.domain.etf import EtfIdentifier


def test_decoder_preserves_unit_and_accumulated_nav_as_separate_facts() -> None:
    """同日单位和累计净值可共存，但不能用一个值覆盖另一个类型。"""
    payload = json.dumps(
        {
            "schema": "quant-v2.etf-nav.v1",
            "etf": "SSE.510300",
            "navs": [
                {
                    "navDate": "2026-07-28",
                    "navKind": "ACCUMULATED",
                    "nav": "4.321",
                    "currency": "CNY",
                    "finality": "FINAL",
                },
                {
                    "navDate": "2026-07-28",
                    "navKind": "UNIT",
                    "nav": "4.210",
                    "currency": "CNY",
                    "finality": "FINAL",
                },
            ],
        }
    ).encode()

    navs = decode_etf_nav_batch(payload, etf=EtfIdentifier.parse("SSE.510300"))

    assert [item.nav_kind for item in navs] == ["ACCUMULATED", "UNIT"]
    assert navs[1].nav_value == Decimal("4.210")


def test_decoder_rejects_iopv_from_p0_reported_nav_dataset() -> None:
    """盘中 IOPV 属于 P2，不能经过日终 NAV capability 混入 P0。"""
    payload = json.dumps(
        {
            "schema": "quant-v2.etf-nav.v1",
            "etf": "SSE.510300",
            "navs": [
                {
                    "navDate": "2026-07-28",
                    "navKind": "IOPV",
                    "nav": "4.210",
                    "currency": "CNY",
                    "finality": "PROVISIONAL",
                }
            ],
        }
    ).encode()

    with pytest.raises(ProviderError, match="value is invalid"):
        decode_etf_nav_batch(payload, etf=EtfIdentifier.parse("SSE.510300"))


def test_decoder_accepts_legal_empty_nav_array() -> None:
    """请求窗口没有已披露 NAV 时返回空集，由同步用例记录独立可用性状态。"""
    payload = json.dumps(
        {
            "schema": "quant-v2.etf-nav.v1",
            "etf": "SSE.510300",
            "navs": [],
        }
    ).encode()

    assert decode_etf_nav_batch(payload, etf=EtfIdentifier.parse("SSE.510300")) == ()
