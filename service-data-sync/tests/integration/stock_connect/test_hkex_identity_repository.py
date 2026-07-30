"""港股通 HKEX 稳定身份、代码复用与快照生命周期集成测试。"""

from __future__ import annotations

import hashlib
import os
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import select

from service_data_sync.application.ports.stock_connect import StockConnectSourceObservation
from service_data_sync.bootstrap.settings import load_settings
from service_data_sync.domain.stock_connect import (
    StockConnectActiveSecurity,
    StockConnectChannel,
    StockConnectInstrumentMaster,
)
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.database.models.market.identity import (
    InstrumentIdentifierVersion,
    InstrumentLifecycleVersion,
    MarketInstrument,
)
from service_data_sync.infrastructure.database.models.market.stock_connect_identity import (
    StockConnectHkexInstrumentIdentity,
)
from service_data_sync.infrastructure.persistence.stock_connect_market_data_repository import (
    SqlAlchemyStockConnectMarketDataRepository,
    StockConnectSourceApproval,
    _resolve_active_instrument,
)

_PROVIDER_ID = "integration-stock-connect-hkex-identity"


@pytest.mark.integration
def test_hkex_stable_identity_handles_code_reuse_change_absence_and_missing_id() -> None:
    """稳定 ID 决定实体，日期化代码和完整快照缺席共同决定可解析生命周期。"""
    if os.environ.get("DATA_SYNC_RUN_INTEGRATION") != "1":
        pytest.skip("set DATA_SYNC_RUN_INTEGRATION=1 after starting local infrastructure")
    database = DatabaseClient.from_settings(load_settings())
    repository = SqlAlchemyStockConnectMarketDataRepository(
        database,
        approved_sources={
            _PROVIDER_ID: StockConnectSourceApproval(
                provider_id=_PROVIDER_ID,
                source_code="integration_stock_connect_hkex_identity",
                legal_name="港股稳定身份集成测试官方来源",
                source_kind="official",
                rights_status="internal",
                license_scope="integration_test_only",
                rights_evidence_ref="license-audit:integration-stock-connect-hkex-identity",
            )
        },
    )
    try:
        first = repository.ensure_hkex_instruments(
            records=(_master("HKID-REUSE-A", "09101", date(2099, 8, 2)),),
            target_source_codes={"09101"},
            source=_source("reuse-a"),
        )
        reused_code = repository.ensure_hkex_instruments(
            records=(_master("HKID-REUSE-B", "09101", date(2099, 8, 3)),),
            target_source_codes={"09101"},
            source=_source("reuse-b"),
        )
        old_code = repository.ensure_hkex_instruments(
            records=(_master("HKID-CODE-CHANGE", "09102", date(2099, 8, 4)),),
            target_source_codes={"09102"},
            source=_source("code-old"),
        )
        new_code = repository.ensure_hkex_instruments(
            records=(_master("HKID-CODE-CHANGE", "09103", date(2099, 8, 5)),),
            target_source_codes={"09103"},
            source=_source("code-new"),
        )
        present = repository.ensure_hkex_instruments(
            records=(_master("HKID-ABSENCE", "09104", date(2099, 8, 6)),),
            target_source_codes={"09104"},
            source=_source("present"),
        )
        repository.ensure_hkex_instruments(
            records=(_master("HKID-OTHER", "09105", date(2099, 8, 7)),),
            target_source_codes=set(),
            source=_source("absent"),
        )
        unresolved = repository.ensure_hkex_instruments(
            records=(_master(None, "09106", date(2099, 8, 8)),),
            target_source_codes={"09106"},
            source=_source("missing-stable-id"),
        )

        assert first["09101"] != reused_code["09101"]
        assert old_code["09102"] == new_code["09103"]
        assert unresolved == {}
        _assert_identity_history(
            database,
            reused_first=first["09101"],
            reused_second=reused_code["09101"],
            changed=old_code["09102"],
            absent=present["09104"],
        )
    finally:
        database.close()


def _assert_identity_history(
    database: DatabaseClient,
    *,
    reused_first: UUID,
    reused_second: UUID,
    changed: UUID,
    absent: UUID,
) -> None:
    """核对实体、代码日版本、缺席状态和不可解析缺稳定 ID 的持久化结果。"""
    with database.session() as session:
        identities = {
            str(row.source_security_id): UUID(str(row.instrument_id))
            for row in session.execute(
                select(StockConnectHkexInstrumentIdentity).where(
                    StockConnectHkexInstrumentIdentity.source_security_id.in_(
                        {
                            "HKID-REUSE-A",
                            "HKID-REUSE-B",
                            "HKID-CODE-CHANGE",
                            "HKID-ABSENCE",
                        }
                    )
                )
            )
            .scalars()
            .all()
        }
        assert identities["HKID-REUSE-A"] == reused_first
        assert identities["HKID-REUSE-B"] == reused_second
        assert identities["HKID-CODE-CHANGE"] == changed
        assert identities["HKID-ABSENCE"] == absent
        codes = {
            (UUID(str(row.entity_id)), row.identifier_value, row.effective_from, row.effective_to)
            for row in session.execute(
                select(InstrumentIdentifierVersion).where(
                    InstrumentIdentifierVersion.entity_id.in_(
                        {reused_first, reused_second, changed, absent}
                    ),
                    InstrumentIdentifierVersion.identifier_scheme == "venue_symbol",
                    InstrumentIdentifierVersion.known_to.is_(None),
                )
            )
            .scalars()
            .all()
        }
        assert (reused_first, "09101", date(2099, 8, 2), date(2099, 8, 3)) in codes
        assert (reused_second, "09101", date(2099, 8, 3), date(2099, 8, 4)) in codes
        assert (changed, "09102", date(2099, 8, 4), date(2099, 8, 5)) in codes
        assert (changed, "09103", date(2099, 8, 5), date(2099, 8, 6)) in codes
        absence_states = {
            (row.status_code, row.effective_from, row.effective_to)
            for row in session.execute(
                select(InstrumentLifecycleVersion).where(
                    InstrumentLifecycleVersion.entity_id == absent,
                    InstrumentLifecycleVersion.known_to.is_(None),
                )
            )
            .scalars()
            .all()
        }
        assert ("ACTIVE", date(2099, 8, 6), date(2099, 8, 7)) in absence_states
        assert ("RETIRED", date(2099, 8, 7), date(2099, 8, 8)) in absence_states
        tradable_to = session.execute(
            select(MarketInstrument.tradable_to).where(MarketInstrument.instrument_id == absent)
        ).scalar_one()
        assert tradable_to == date(2099, 8, 7)
        missing_identity = session.execute(
            select(StockConnectHkexInstrumentIdentity).where(
                StockConnectHkexInstrumentIdentity.source_security_id == "09106"
            )
        ).scalar_one_or_none()
        assert missing_identity is None
        assert (
            _resolve_active_instrument(
                session,
                channel=StockConnectChannel("SH", "SOUTHBOUND"),
                value=StockConnectActiveSecurity(
                    source_instrument_code="09106",
                    source_instrument_name="MISSING STABLE ID",
                    trade_date=date(2099, 8, 8),
                    rank_no=1,
                    buy_amount=None,
                    sell_amount=None,
                    turnover_amount=Decimal("1"),
                    currency="HKD",
                ),
            )
            is None
        )


def _master(
    security_id: str | None,
    code: str,
    effective_from: date,
) -> StockConnectInstrumentMaster:
    """构造一个完整主档快照成员，名称不参与实体键。"""
    return StockConnectInstrumentMaster(
        source_security_id=security_id,
        source_instrument_code=code,
        display_name=f"SECURITY {code}",
        effective_from=effective_from,
    )


def _source(seed: str) -> StockConnectSourceObservation:
    """构造具有唯一 raw/normalized 摘要的官方主档观察。"""
    raw_hash = hashlib.sha256(f"raw:{seed}".encode()).hexdigest()
    normalized_hash = hashlib.sha256(f"normalized:{seed}".encode()).hexdigest()
    return StockConnectSourceObservation(
        provider_id=_PROVIDER_ID,
        capability="market.stock_connect.instrument_master.reported",
        raw_payload_sha256=raw_hash,
        raw_uri=f"s3://integration/{raw_hash}/raw.dat",
        raw_content_type="application/octet-stream",
        raw_byte_size=100,
        normalized_payload_sha256=normalized_hash,
        normalized_uri=f"s3://integration/{normalized_hash}/normalized.json",
        normalized_content_type="application/json",
        normalized_byte_size=80,
        observed_at=datetime(2099, 8, 8, 8, tzinfo=UTC),
        upstream_source="HKEX_DATA_MARKETPLACE",
        adapter_version="integration-hkex-master-v2",
        schema_fingerprint="8" * 64,
    )
