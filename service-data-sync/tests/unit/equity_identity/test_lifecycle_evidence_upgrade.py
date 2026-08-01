"""目录生命周期证据升级的回归测试。"""

from __future__ import annotations

from datetime import date
from uuid import uuid4

from service_data_sync.domain.equity import EquityIdentifier
from service_data_sync.domain.equity_master import (
    EquityLifecycleEntry,
    EquityLifecycleEvidenceKind,
    EquityLifecycleStatus,
)
from service_data_sync.infrastructure.persistence.equity_lifecycle_repository import (
    _is_catalog_evidence_upgrade,
    _matches_current_entry,
)


def test_same_catalog_fact_is_replaced_by_official_listing_evidence() -> None:
    """业务字段相同的目录 LISTED 必须让官方证据产生知识版本升级。"""
    current = {
        "version_id": uuid4(),
        "status": "LISTED",
        "listed_on": date(1999, 11, 10),
        "delisted_on": None,
        "effective_from": date(1999, 11, 10),
        "effective_to": None,
        "evidence_kind": "CATALOG",
    }
    entry = EquityLifecycleEntry(
        identifier=EquityIdentifier.parse("SSE.600000"),
        status=EquityLifecycleStatus.LISTED,
        effective_on=date(1999, 11, 10),
        evidence_kind=EquityLifecycleEvidenceKind.EXPLICIT_LISTING,
        listed_on=date(1999, 11, 10),
    )

    assert _matches_current_entry(current, entry) is False
    assert _is_catalog_evidence_upgrade(current, entry) is True


def test_same_official_fact_is_idempotent_but_changed_fact_is_not_upgrade() -> None:
    """相同官方事实重放不造修订；不同市场有效日仍必须走状态机校验。"""
    current = {
        "version_id": uuid4(),
        "status": "LISTED",
        "listed_on": date(1999, 11, 10),
        "delisted_on": None,
        "effective_from": date(1999, 11, 10),
        "effective_to": None,
        "evidence_kind": "EXPLICIT_LISTING",
    }
    repeated = EquityLifecycleEntry(
        identifier=EquityIdentifier.parse("SSE.600000"),
        status=EquityLifecycleStatus.LISTED,
        effective_on=date(1999, 11, 10),
        evidence_kind=EquityLifecycleEvidenceKind.EXPLICIT_LISTING,
        listed_on=date(1999, 11, 10),
    )
    changed_date = EquityLifecycleEntry(
        identifier=EquityIdentifier.parse("SSE.600000"),
        status=EquityLifecycleStatus.LISTED,
        effective_on=date(2000, 1, 1),
        evidence_kind=EquityLifecycleEvidenceKind.EXPLICIT_LISTING,
        listed_on=date(1999, 11, 10),
    )

    assert _matches_current_entry(current, repeated) is True
    assert _is_catalog_evidence_upgrade(current, changed_date) is False
