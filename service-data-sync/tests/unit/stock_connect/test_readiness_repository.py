"""互联互通 readiness 仓储纯语义测试。"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from service_data_sync.application.ports.data_source import ProviderPreflightComponent
from service_data_sync.infrastructure.persistence.stock_connect_readiness_repository import (
    StockConnectReadinessNotObserved,
    _canonical_hash,
    _resolve_snapshot,
    stock_connect_readiness_probe_outcome,
)


def test_readiness_probe_acceptance_waits_for_command_submission() -> None:
    """全部来源探针通过后只进入待提交状态，不虚构已发布结果。"""
    outcome = stock_connect_readiness_probe_outcome(
        (
            ProviderPreflightComponent(
                component="hkex-calendar-https",
                accepted=True,
                reason="CALENDAR_VERIFIED",
            ),
            ProviderPreflightComponent(
                component="sftp-authentication",
                accepted=True,
                reason="SFTP_AUTHENTICATED",
            ),
        )
    )

    assert outcome.status == "PENDING"
    assert outcome.reason_code == "COMMAND_NOT_SUBMITTED"


@pytest.mark.parametrize(
    ("component", "status", "reason_code"),
    (
        ("hkex-calendar-https", "SOURCE_MISSING", "CALENDAR_SOURCE_MISSING"),
        (
            "hkex-sftp-entitlement-manifest",
            "SOURCE_MISSING",
            "DELIVERY_ENTITLEMENT_MISSING",
        ),
        ("status-sh-northbound", "SOURCE_MISSING", "STATUS_SOURCE_MISSING"),
        ("sftp-authentication", "SOURCE_MISSING", "DELIVERY_OBJECT_MISSING"),
        (
            "hkex-daily-statistics-sh-northbound",
            "SOURCE_MISSING",
            "DELIVERY_OBJECT_MISSING",
        ),
        (
            "hkex-securities-master-deliveries",
            "SOURCE_MISSING",
            "DELIVERY_OBJECT_MISSING",
        ),
        ("unclassified-probe", "FAILED", "PREFLIGHT_FAILED"),
    ),
)
def test_readiness_probe_failure_uses_stable_public_reason(
    component: str,
    status: str,
    reason_code: str,
) -> None:
    """不同底层失败只映射为冻结状态和低基数原因码。"""
    outcome = stock_connect_readiness_probe_outcome(
        (
            ProviderPreflightComponent(
                component=component,
                accepted=False,
                reason="PRIVATE_PROVIDER_DETAIL_MUST_NOT_ESCAPE",
            ),
        )
    )

    assert outcome.status == status
    assert outcome.reason_code == reason_code
    assert "PRIVATE_PROVIDER_DETAIL_MUST_NOT_ESCAPE" not in outcome.detail


def test_readiness_probe_failure_priority_is_deterministic() -> None:
    """多组件同时失败时按日历、授权、状态和对象的固定优先级收敛。"""
    outcome = stock_connect_readiness_probe_outcome(
        (
            ProviderPreflightComponent(
                component="sftp-authentication",
                accepted=False,
                reason="SFTP_AUTHENTICATION_FAILED",
            ),
            ProviderPreflightComponent(
                component="hkex-calendar-https",
                accepted=False,
                reason="CALENDAR_DOWNLOAD_FAILED",
            ),
        )
    )

    assert outcome.status == "SOURCE_MISSING"
    assert outcome.reason_code == "CALENDAR_SOURCE_MISSING"


def test_readiness_snapshot_absence_is_not_an_empty_success() -> None:
    """没有任何匹配终态 snapshot 时返回未观察异常，不构造空成功正文。"""
    session = MagicMock()
    session.scalars.return_value.all.return_value = []

    with pytest.raises(
        StockConnectReadinessNotObserved,
        match="has not been observed",
    ):
        _resolve_snapshot(
            session,
            channels=("SH_NORTHBOUND",),
            mode="LATEST",
            exact_date=None,
        )


def test_readiness_canonical_hash_cross_language_vector() -> None:
    """固定含 Unicode、null 与数组的向量，供 Python 和 TypeScript 逐字节对齐。"""
    body: dict[str, object] = {
        "schemaVersion": "quant-v2.stock-connect-readiness.v1",
        "mode": "EXACT",
        "selectedChannels": ["SH_NORTHBOUND", "SZ_SOUTHBOUND"],
        "requestedExactDate": "2026-07-30",
        "candidateTradeDate": "2026-07-30",
        "readyTradeDate": None,
        "observedAt": "2026-07-30T10:00:00Z",
        "calendar": {
            "dataVersion": "a" * 64,
            "observedAt": None,
            "sourceFileSha256": None,
            "sourcePublicationAt": None,
            "publicationAvailability": "SOURCE_MISSING",
        },
        "channels": [
            {
                "channel": "SH_NORTHBOUND",
                "calendarState": "OPEN",
                "state": "READY",
                "reasonCode": "BUNDLE_PUBLISHED",
                "bundleDataVersion": "版本-α",
                "evidenceObservedAt": "2026-07-30T10:00:00Z",
            },
            {
                "channel": "SZ_SOUTHBOUND",
                "calendarState": "UNKNOWN",
                "state": "SOURCE_MISSING",
                "reasonCode": "CALENDAR_SOURCE_MISSING",
                "bundleDataVersion": None,
                "evidenceObservedAt": "2026-07-30T10:00:00Z",
            },
        ],
    }

    assert _canonical_hash(body) == (
        "abe5d1926e56f9f60959b27141e450ad1a0f580437e59a8e737a1efe34276307"
    )
