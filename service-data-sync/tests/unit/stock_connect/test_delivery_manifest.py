"""验证十年互联互通交付证据的分页容量与摘要完整性。"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from uuid import UUID

import pytest

from service_data_sync.application.ports.delivery_manifest import (
    DeliveryManifestIntegrityError,
    DeliveryManifestTradeDate,
    build_immutable_delivery_manifest,
    delivery_manifest_page_byte_size,
    paginate_delivery_manifest_days,
    verify_delivery_manifest_page,
    verify_immutable_delivery_manifest,
)
from service_data_sync.infrastructure.providers.official.stock_connect import (
    stock_connect_bundle_targets_from_evidence,
    stock_connect_delivery_manifest_days_from_evidence,
    stock_connect_delivery_window_from_evidence,
    stock_connect_preflight_evidence_from_delivery_page,
)

_CREATED_AT = datetime(2026, 7, 30, 12, tzinfo=UTC)
_AVAILABLE_UNTIL = _CREATED_AT + timedelta(days=30)
_REQUEST_HASH = "a" * 64


def test_three_thousand_trade_dates_are_bounded_to_immutable_pages() -> None:
    """三千日四通道目标必须自动形成一百五十页，且单页容量保持有界。"""
    days = _stock_connect_days(3_000)

    manifest = build_immutable_delivery_manifest(
        manifest_id=UUID("10000000-0000-4000-8000-000000000001"),
        dataset_code="market.stock_connect.overview.bundle",
        provider_id="official-stock-connect",
        request_hash=_REQUEST_HASH,
        status="ELIGIBLE",
        available_until=_AVAILABLE_UNTIL,
        minimum_remaining_seconds=600,
        created_at=_CREATED_AT,
        days=days,
    )

    assert manifest.target_count == 12_000
    assert manifest.page_count == 150
    assert [page.page_no for page in manifest.pages] == list(range(150))
    assert all(page.trade_date_count == 20 for page in manifest.pages)
    assert all(page.target_count == 80 for page in manifest.pages)
    assert manifest.pages[0].date_to > manifest.pages[-1].date_to
    assert max(delivery_manifest_page_byte_size(page) for page in manifest.pages) < 128 * 1024
    verify_immutable_delivery_manifest(manifest)


def test_manifest_root_is_independent_of_database_identity_and_input_order() -> None:
    """相同业务证据即使输入逆序或使用不同 UUID，也必须得到同一稳定根摘要。"""
    ordered = _stock_connect_days(45)
    first = build_immutable_delivery_manifest(
        manifest_id=UUID("10000000-0000-4000-8000-000000000002"),
        dataset_code="market.stock_connect.overview.bundle",
        provider_id="official-stock-connect",
        request_hash=_REQUEST_HASH,
        status="ELIGIBLE",
        available_until=_AVAILABLE_UNTIL,
        minimum_remaining_seconds=600,
        created_at=_CREATED_AT,
        days=ordered,
    )
    second = build_immutable_delivery_manifest(
        manifest_id=UUID("10000000-0000-4000-8000-000000000003"),
        dataset_code="market.stock_connect.overview.bundle",
        provider_id="official-stock-connect",
        request_hash=_REQUEST_HASH,
        status="ELIGIBLE",
        available_until=_AVAILABLE_UNTIL,
        minimum_remaining_seconds=600,
        created_at=_CREATED_AT,
        days=tuple(reversed(ordered)),
    )

    assert first.root_hash == second.root_hash
    assert [page.page_hash for page in first.pages] == [page.page_hash for page in second.pages]


def test_page_and_root_hashes_reject_evidence_tampering() -> None:
    """页面正文或 header 根摘要任一字节变化都必须在读取前失败关闭。"""
    manifest = build_immutable_delivery_manifest(
        manifest_id=UUID("10000000-0000-4000-8000-000000000004"),
        dataset_code="market.stock_connect.overview.bundle",
        provider_id="official-stock-connect",
        request_hash=_REQUEST_HASH,
        status="ELIGIBLE",
        available_until=_AVAILABLE_UNTIL,
        minimum_remaining_seconds=600,
        created_at=_CREATED_AT,
        days=_stock_connect_days(2),
    )
    tampered_evidence = copy.deepcopy(dict(manifest.pages[0].evidence))
    raw_days = tampered_evidence["days"]
    assert isinstance(raw_days, list)
    first_day = raw_days[0]
    assert isinstance(first_day, dict)
    first_day["targetCount"] = 3
    tampered_page = replace(manifest.pages[0], evidence=tampered_evidence)

    with pytest.raises(DeliveryManifestIntegrityError):
        verify_delivery_manifest_page(tampered_page)
    with pytest.raises(DeliveryManifestIntegrityError):
        verify_immutable_delivery_manifest(replace(manifest, root_hash="b" * 64))


def test_pagination_never_splits_one_trade_date_to_meet_target_limit() -> None:
    """目标上限迫使换页时必须整体移动交易日，不能把同一日证据拆成两页。"""
    days = (
        DeliveryManifestTradeDate(
            trade_date=date(2026, 7, 1),
            target_count=200,
            evidence={"bundleTargets": list(range(200))},
        ),
        DeliveryManifestTradeDate(
            trade_date=date(2026, 7, 2),
            target_count=100,
            evidence={"bundleTargets": list(range(100))},
        ),
        DeliveryManifestTradeDate(
            trade_date=date(2026, 7, 3),
            target_count=56,
            evidence={"bundleTargets": list(range(56))},
        ),
    )

    pages = paginate_delivery_manifest_days(days)

    assert [(page.trade_date_count, page.target_count) for page in pages] == [
        (2, 156),
        (1, 200),
    ]


def test_full_provider_evidence_round_trips_through_one_database_page() -> None:
    """全窗私有证据拆页后必须可恢复批次复核清单，且 header 截止与目标计数不丢失。"""
    evidence = _provider_evidence()
    available_until, minimum_window = stock_connect_delivery_window_from_evidence(evidence)
    days = tuple(
        DeliveryManifestTradeDate(
            trade_date=trade_date,
            target_count=target_count,
            evidence=day_evidence,
        )
        for trade_date, target_count, day_evidence in (
            stock_connect_delivery_manifest_days_from_evidence(evidence)
        )
    )
    manifest = build_immutable_delivery_manifest(
        manifest_id=UUID("10000000-0000-4000-8000-000000000005"),
        dataset_code="market.stock_connect.overview.bundle",
        provider_id="official-stock-connect",
        request_hash=_REQUEST_HASH,
        status="ELIGIBLE",
        available_until=available_until,
        minimum_remaining_seconds=0,
        created_at=_CREATED_AT,
        days=days,
    )

    restored = stock_connect_preflight_evidence_from_delivery_page(manifest.pages[0].evidence)

    assert minimum_window == 4_200
    assert manifest.target_count == 4
    assert manifest.page_count == 1
    assert stock_connect_bundle_targets_from_evidence(restored) == (
        ("SH", "NORTHBOUND", date(2026, 7, 28)),
        ("SH", "SOUTHBOUND", date(2026, 7, 28)),
        ("SH", "NORTHBOUND", date(2026, 7, 29)),
        ("SH", "SOUTHBOUND", date(2026, 7, 29)),
    )
    assert restored["availableUntil"] == "2026-08-29T12:00:00Z"


def _provider_evidence() -> dict[str, object]:
    """构造只含官方交付元数据的两日证据，不使用市场数值或伪造 publication。"""
    dates = ("2026-07-28", "2026-07-29")
    body: dict[str, object] = {
        "schema": "quant-v2.stock-connect-preflight-delivery-manifest.v1",
        "providerId": "official-stock-connect",
        "request": {
            "datasetCode": "market.stock_connect.overview.bundle",
            "mode": "DATE_RANGE",
            "selector": {
                "kind": "STOCK_CONNECT",
                "operation": "MARKET",
                "channel": "SH",
                "direction": None,
            },
            "dateFrom": dates[0],
            "dateTo": dates[-1],
            "observationDate": None,
        },
        "profileManifestSha256": "1" * 64,
        "calendarManifestSha256": "2" * 64,
        "sftpDeliveryManifestRootHash": "3" * 64,
        "statusManifestSha256": "4" * 64,
        "availableUntil": "2026-08-29T12:00:00Z",
        "minimumExecutionWindowSeconds": 4_200,
        "calendarDeliveries": [
            {"year": 2025, "payloadSha256": "5" * 64},
            {"year": 2026, "payloadSha256": "6" * 64},
        ],
        "sftpDeliveries": [
            {
                "deliveryKind": delivery_kind,
                "channel": "SH" if delivery_kind == "DAILY_STATISTICS" else "HKEX",
                "tradeDate": trade_date,
                "issuedDate": (None if delivery_kind == "DAILY_STATISTICS" else trade_date),
                "availableUntil": "2026-08-29T12:00:00Z",
                "available": True,
            }
            for trade_date in dates
            for delivery_kind in ("DAILY_STATISTICS", "SECURITIES_MASTER")
        ],
        "statusDeliveries": [
            {
                "channel": "SH",
                "direction": direction,
                "tradeDate": trade_date,
                "available": True,
            }
            for trade_date in dates
            for direction in ("NORTHBOUND", "SOUTHBOUND")
        ],
        "bundleTargets": [
            {
                "channel": "SH",
                "direction": direction,
                "tradeDate": trade_date,
            }
            for trade_date in dates
            for direction in ("NORTHBOUND", "SOUTHBOUND")
        ],
    }
    encoded = json.dumps(
        body,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return {**body, "manifestHash": hashlib.sha256(encoded).hexdigest()}


def _stock_connect_days(count: int) -> tuple[DeliveryManifestTradeDate, ...]:
    """构造只含版本和摘要元数据的容量样本，不模拟任何市场数值。"""
    start = date(2014, 11, 17)
    channels = (
        ("SH", "NORTHBOUND"),
        ("SH", "SOUTHBOUND"),
        ("SZ", "NORTHBOUND"),
        ("SZ", "SOUTHBOUND"),
    )
    return tuple(
        DeliveryManifestTradeDate(
            trade_date=(trade_date := start + timedelta(days=index)),
            target_count=4,
            evidence={
                "bundleTargets": [
                    {
                        "channel": channel,
                        "direction": direction,
                        "tradeDate": trade_date.isoformat(),
                    }
                    for channel, direction in channels
                ],
                "deliveryVersion": f"{index:064x}",
            },
        )
        for index in range(count)
    )
