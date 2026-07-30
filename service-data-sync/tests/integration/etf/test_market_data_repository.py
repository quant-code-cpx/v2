"""ETF P0 日线和 NAV 的 PostgreSQL 原子发布集成测试。"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from service_data_sync.application.ports.etf_market import EtfSourceObservation
from service_data_sync.application.ports.market_data_access import (
    MarketDataAccessUnavailable,
    MarketDataFilter,
    MarketDataQuery,
)
from service_data_sync.bootstrap.settings import load_settings
from service_data_sync.domain.etf import (
    EtfDailyBar,
    EtfDailyStatus,
    EtfIdentifier,
    EtfNav,
    EtfProfile,
)
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.database.models.canonical import CanonicalDataset
from service_data_sync.infrastructure.database.models.etf import (
    EtfDailyBarRevision,
    EtfNavRevision,
    EtfProfileVersion,
)
from service_data_sync.infrastructure.database.models.market.identity import (
    EtfListing,
    FundLegalEntity,
    FundShareClass,
    InstrumentIdentifierVersion,
    MarketEntity,
    MarketInstrument,
    TradingVenue,
)
from service_data_sync.infrastructure.database.models.publication.dataset_availability_observation import (  # noqa: E501
    DatasetAvailabilityObservation,
)
from service_data_sync.infrastructure.database.models.publication.dataset_publication import (
    DatasetPublication,
)
from service_data_sync.infrastructure.persistence.dataset_availability_repository import (
    SqlAlchemyDatasetAvailabilityRepository,
)
from service_data_sync.infrastructure.persistence.etf_market_data_repository import (
    EtfSourceApproval,
    SqlAlchemyEtfMarketDataRepository,
)
from service_data_sync.infrastructure.persistence.etf_reference_repository import (
    EtfReferenceSourceApproval,
    SqlAlchemyEtfReferenceRepository,
)
from service_data_sync.infrastructure.persistence.etf_universe_repository import (
    load_frozen_etf_universe,
    resolve_current_etf_profile_data_versions,
)
from service_data_sync.infrastructure.persistence.source_batch import record_source_observation
from service_data_sync.infrastructure.persistence.sqlalchemy_market_data_access_repository import (
    SqlAlchemyMarketDataAccessRepository,
)

_OBSERVED_AT = datetime(2026, 7, 29, 8, tzinfo=UTC)


@pytest.mark.integration
def test_etf_empty_observation_is_replaced_and_success_clear_removes_current_state() -> None:
    """ETF 空集和来源不可用只写元数据；真实发布后不应留下遮蔽读取的当前状态。"""
    if os.environ.get("DATA_SYNC_RUN_INTEGRATION") != "1":
        pytest.skip("set DATA_SYNC_RUN_INTEGRATION=1 after starting local infrastructure")
    database = DatabaseClient.from_settings(load_settings())
    dataset = "fund.etf.bar.1d.reported"
    partition_key = "SSE.510300:2030-01-01:2030-01-31"
    repository = SqlAlchemyDatasetAvailabilityRepository(database)
    try:
        repository.record(
            dataset=dataset,
            partition_key=partition_key,
            availability="empty",
            reason_code="no_matching_facts",
            provider_id="integration-etf",
            observed_at=datetime(2030, 2, 1, tzinfo=UTC),
        )
        repository.record(
            dataset=dataset,
            partition_key=partition_key,
            availability="source_unavailable",
            reason_code="unavailable",
            provider_id="integration-etf",
            observed_at=datetime(2030, 2, 2, tzinfo=UTC),
        )
        repository.clear(
            dataset=dataset,
            partition_key=partition_key,
            cleared_at=datetime(2030, 2, 3, tzinfo=UTC),
        )
        with database.session() as session:
            current = session.execute(
                select(DatasetAvailabilityObservation).where(
                    DatasetAvailabilityObservation.dataset == dataset,
                    DatasetAvailabilityObservation.partition_key == partition_key,
                    DatasetAvailabilityObservation.superseded_at.is_(None),
                )
            ).scalar_one_or_none()
    finally:
        with database.transaction() as session:
            session.execute(
                delete(DatasetAvailabilityObservation).where(
                    DatasetAvailabilityObservation.dataset == dataset,
                    DatasetAvailabilityObservation.partition_key == partition_key,
                )
            )
        database.close()

    assert current is None


@pytest.mark.integration
def test_etf_daily_bar_and_nav_are_revisioned_and_replay_is_idempotent() -> None:
    """验证 ETF 身份、双证据、强类型 revision、release 与相同内容重放的完整事务边界。"""
    if os.environ.get("DATA_SYNC_RUN_INTEGRATION") != "1":
        pytest.skip("set DATA_SYNC_RUN_INTEGRATION=1 after starting local infrastructure")
    database = DatabaseClient.from_settings(load_settings())
    etf_id = uuid4()
    etf = EtfIdentifier("SSE", f"{etf_id.int % 1_000_000:06d}")
    try:
        _seed_etf_identity(database, etf_id=etf_id, etf=etf)
        repository = SqlAlchemyEtfMarketDataRepository(
            database,
            approved_sources={
                "integration-etf": EtfSourceApproval(
                    provider_id="integration-etf",
                    source_code="integration_etf_official",
                    legal_name="ETF 集成测试官方来源",
                    source_kind="official",
                    rights_status="internal",
                    license_scope="integration_test_only",
                )
            },
        )
        first_bar = repository.publish_daily_bars(
            etf=etf, bars=(_bar(),), source=_source("a" * 64, "b" * 64, "fund.etf.bar.1d.raw")
        )
        repeated_bar = repository.publish_daily_bars(
            etf=etf, bars=(_bar(),), source=_source("c" * 64, "d" * 64, "fund.etf.bar.1d.raw")
        )
        first_nav = repository.publish_navs(
            etf=etf,
            navs=(_nav("UNIT", "4.210"), _nav("ACCUMULATED", "4.321")),
            source=_source("e" * 64, "f" * 64, "fund.etf.nav.1d.reported"),
        )
        reference_repository = SqlAlchemyEtfReferenceRepository(
            database,
            approved_sources={
                "integration-etf": EtfReferenceSourceApproval(
                    provider_id="integration-etf",
                    source_code="integration_etf_official",
                    legal_name="ETF 集成测试官方来源",
                    source_kind="official",
                    rights_status="internal",
                    license_scope="integration_test_only",
                )
            },
        )
        profile = reference_repository.publish_profiles(
            profiles=(_profile(etf),),
            source=_source("1" * 64, "2" * 64, "fund.etf.master"),
        )
        status = reference_repository.publish_statuses(
            etf=etf,
            statuses=(_status(etf),),
            source=_source("3" * 64, "4" * 64, "fund.etf.trading_state"),
        )
        reader = SqlAlchemyMarketDataAccessRepository(database)
        page = reader.query(request=_bar_query(etf_id), after=None)
        nav_page = reader.query(request=_nav_query(etf_id), after=None)
        profile_page = reader.query(request=_profile_query(etf_id), after=None)
        profile_v2_page = reader.query(request=_profile_v2_query(etf), after=None)
        status_page = reader.query(request=_status_query(etf_id), after=None)
        availability_repository = SqlAlchemyDatasetAvailabilityRepository(database)
        availability_repository.record(
            dataset="fund.etf.bar.1d.reported",
            partition_key=f"{etf.qualified_key}:2026-06-28:2026-07-28",
            availability="source_unavailable",
            reason_code="rate_limited",
            provider_id="integration-etf",
            observed_at=datetime(2031, 1, 1, tzinfo=UTC),
            entity_partition=f"etf:{etf.qualified_key}",
            coverage_from=date(2026, 6, 28),
            coverage_to=date(2026, 7, 28),
        )
        stale_bar_page = reader.query(request=_bar_365d_query(etf_id), after=None)
        availability_repository.clear(
            dataset="fund.etf.bar.1d.reported",
            partition_key=f"{etf.qualified_key}:2026-06-28:2026-07-28",
            cleared_at=datetime(2031, 1, 2, tzinfo=UTC),
        )
        replacement_symbol = f"{(int(etf.symbol) + 400_000) % 1_000_000:06d}"
        with database.transaction() as session:
            publication_cutoff = session.scalar(
                select(DatasetPublication.knowledge_cutoff).where(
                    DatasetPublication.data_version == first_bar.data_version
                )
            )
            current_identifier = session.scalar(
                select(InstrumentIdentifierVersion).where(
                    InstrumentIdentifierVersion.entity_id == etf_id,
                    InstrumentIdentifierVersion.identifier_scheme == "venue_symbol",
                    InstrumentIdentifierVersion.known_to.is_(None),
                )
            )
            assert publication_cutoff is not None
            assert current_identifier is not None
            identity_changed_at = publication_cutoff + timedelta(microseconds=1)
            session.execute(
                update(InstrumentIdentifierVersion)
                .where(InstrumentIdentifierVersion.version_id == current_identifier.version_id)
                .values(
                    effective_to=date(2026, 7, 29),
                    known_to=identity_changed_at,
                )
            )
            session.execute(
                insert(InstrumentIdentifierVersion).values(
                    version_id=uuid4(),
                    entity_id=etf_id,
                    entity_kind="ETF_LISTING",
                    venue_id=current_identifier.venue_id,
                    identifier_scheme="venue_symbol",
                    identifier_value=replacement_symbol,
                    effective_from=date(2026, 7, 29),
                    effective_to=None,
                    known_from=identity_changed_at,
                    known_to=None,
                    source_time_precision="DATE_ONLY",
                    source_batch_id=current_identifier.source_batch_id,
                )
            )
        historic_bar_page = reader.query(
            request=replace(
                _bar_query(etf_id),
                selection={
                    "qualityStatuses": ("PASSED",),
                    "dataVersion": str(first_bar.data_version),
                },
                request_fingerprint="integration-etf-historic-reader",
            ),
            after=None,
        )
        with database.session() as session:
            bar_rows = (
                session.execute(
                    select(EtfDailyBarRevision).where(
                        EtfDailyBarRevision.etf_id == etf_id,
                        EtfDailyBarRevision.known_to.is_(None),
                    )
                )
                .scalars()
                .all()
            )
            etf_dataset_versions = set(
                session.execute(
                    select(CanonicalDataset.code, CanonicalDataset.schema_version).where(
                        CanonicalDataset.code.in_(
                            {
                                "fund.etf.profile.reported",
                                "fund.etf.trading_state.reported",
                                "fund.etf.bar.1d.reported",
                                "fund.etf.nav.1d.reported",
                            }
                        )
                    )
                ).all()
            )
            nav_rows = (
                session.execute(
                    select(EtfNavRevision).where(
                        EtfNavRevision.etf_id == etf_id,
                        EtfNavRevision.known_to.is_(None),
                    )
                )
                .scalars()
                .all()
            )
    finally:
        database.close()

    assert first_bar.inserted_count == 1
    assert repeated_bar.inserted_count == 0
    assert repeated_bar.unchanged_count == 1
    assert repeated_bar.data_version == first_bar.data_version
    assert first_nav.inserted_count == 2
    assert page.data_version == first_bar.data_version
    assert len(page.sources) == 1
    page_values = page.items[0]["values"]
    assert isinstance(page_values, Mapping)
    assert page_values["close"] == Decimal("4.100")
    assert page.sources[0].publisher == "腾讯证券"
    assert page.sources[0].authoritative is False
    assert page.sources[0].coverage_note
    assert page.items[0]["sourceRef"] == "src_tencent_etf_kline"
    assert historic_bar_page.data_version == first_bar.data_version
    historic_bar_entity = historic_bar_page.items[0]["entity"]
    assert isinstance(historic_bar_entity, Mapping)
    assert historic_bar_entity["identifiers"] == [
        {"scheme": "venue_symbol", "value": etf.qualified_key}
    ]
    assert stale_bar_page.data_version == page.data_version
    assert stale_bar_page.items == page.items
    assert stale_bar_page.warnings == (
        "serving_previous_publication",
        "latest_sync_source_unavailable",
        "publication_coverage_not_proven_complete",
        "request_exceeds_publication_fact_range",
    )
    assert stale_bar_page.completeness == "PARTIAL"
    assert stale_bar_page.coverage == {
        "from": "2026-07-28",
        "to": "2026-07-28",
        "pitCoverage": "UNKNOWN",
        "gaps": [],
    }
    assert nav_page.data_version == first_nav.data_version
    assert len(nav_page.sources) == 1
    assert nav_page.sources[0].publisher == "东方财富"
    assert nav_page.sources[0].authoritative is False
    assert nav_page.sources[0].coverage_note
    assert nav_page.items[0]["sourceRef"] == "src_eastmoney_etf_nav"
    assert profile_page.data_version == profile.data_version
    profile_values = profile_page.items[0]["values"]
    assert isinstance(profile_values, Mapping)
    assert profile_values["listingStatus"] == "LISTED"
    profile_v2_values = profile_v2_page.items[0]["values"]
    assert isinstance(profile_v2_values, Mapping)
    assert profile_v2_values["displayName"] == "集成测试沪深300ETF"
    profile_v2_entity = profile_v2_page.items[0]["entity"]
    assert isinstance(profile_v2_entity, Mapping)
    assert profile_v2_entity["identifiers"] == [
        {"scheme": "venue_symbol", "value": etf.qualified_key}
    ]
    assert profile_v2_page.sources[0].publisher == "上海证券交易所"
    assert len(profile_v2_page.sources) == 1
    assert profile_v2_page.sources[0].authoritative is True
    assert profile_v2_page.sources[0].coverage_note
    assert profile_v2_page.items[0]["sourceRef"] == "src_sse_etf_directory"
    assert status_page.data_version == status.data_version
    assert len(status_page.sources) == 1
    status_values = status_page.items[0]["values"]
    assert isinstance(status_values, Mapping)
    assert status_values["stateDimension"] == "SUBSCRIPTION"
    assert status_values["state"] == "SUSPENDED"
    assert status_values["effectiveTo"] == "2026-07-29"
    assert status_page.sources[0].publisher == "东方财富"
    assert status_page.sources[0].authoritative is False
    assert status_page.sources[0].coverage_note
    assert status_page.items[0]["sourceRef"] == "src_eastmoney_etf_nav"
    assert len(bar_rows) == 1
    assert bar_rows[0].close_price == Decimal("4.100")
    assert {(item.nav_kind, item.nav_value) for item in nav_rows} == {
        ("UNIT", Decimal("4.210")),
        ("ACCUMULATED", Decimal("4.321")),
    }
    assert {
        ("fund.etf.profile.reported", 2),
        ("fund.etf.trading_state.reported", 2),
        ("fund.etf.bar.1d.reported", 2),
        ("fund.etf.nav.1d.reported", 2),
    } <= etf_dataset_versions


@pytest.mark.integration
def test_etf_v2_reader_returns_exact_legal_empty_without_a_publication() -> None:
    """无 publication 时按 qualified symbol 与窗口读取合法空态，不退化为统一未发布。"""
    if os.environ.get("DATA_SYNC_RUN_INTEGRATION") != "1":
        pytest.skip("set DATA_SYNC_RUN_INTEGRATION=1 after starting local infrastructure")
    database = DatabaseClient.from_settings(load_settings())
    etf_id = uuid4()
    etf = EtfIdentifier("SZSE", f"{etf_id.int % 1_000_000:06d}")
    partition_key = f"{etf.qualified_key}:2026-07-28:2026-07-28"
    try:
        _seed_etf_identity(database, etf_id=etf_id, etf=etf)
        availability_repository = SqlAlchemyDatasetAvailabilityRepository(database)
        availability_repository.record(
            dataset="fund.etf.nav.1d.reported",
            partition_key=partition_key,
            availability="empty",
            reason_code="no_matching_facts",
            provider_id="integration-etf",
            observed_at=datetime(2026, 7, 30, 8, tzinfo=UTC),
            entity_partition=f"etf:{etf.qualified_key}",
            coverage_from=date(2026, 7, 28),
            coverage_to=date(2026, 7, 28),
        )
        reader = SqlAlchemyMarketDataAccessRepository(database)

        with pytest.raises(MarketDataAccessUnavailable) as captured:
            reader.query(request=_nav_query(etf_id), after=None)
    finally:
        SqlAlchemyDatasetAvailabilityRepository(database).clear(
            dataset="fund.etf.nav.1d.reported",
            partition_key=partition_key,
            cleared_at=datetime(2026, 7, 30, 9, tzinfo=UTC),
        )
        database.close()

    assert captured.value.availability == "EMPTY"
    assert captured.value.reason_code == "NO_MATCHING_FACTS"
    assert captured.value.observed_at == datetime(2026, 7, 30, 8, tzinfo=UTC)
    assert captured.value.warnings == ("legal_empty_observation",)


@pytest.mark.integration
def test_money_market_nav_overlap_blocks_an_older_publication() -> None:
    """货币 ETF 暂不支持是产品语义；部分重叠窗口也不得回退并泄露旧 NAV publication。"""
    if os.environ.get("DATA_SYNC_RUN_INTEGRATION") != "1":
        pytest.skip("set DATA_SYNC_RUN_INTEGRATION=1 after starting local infrastructure")
    database = DatabaseClient.from_settings(load_settings())
    etf_id = uuid4()
    etf = EtfIdentifier("SZSE", f"{etf_id.int % 1_000_000:06d}")
    unsupported_partition = f"{etf.qualified_key}:2026-07-01:2026-07-28"
    try:
        _seed_etf_identity(database, etf_id=etf_id, etf=etf)
        repository = SqlAlchemyEtfMarketDataRepository(
            database,
            approved_sources={
                "integration-etf": EtfSourceApproval(
                    provider_id="integration-etf",
                    source_code="integration_etf_official",
                    legal_name="ETF 集成测试官方来源",
                    source_kind="official",
                    rights_status="internal",
                    license_scope="integration_test_only",
                )
            },
        )
        repository.publish_navs(
            etf=etf,
            navs=(_nav("UNIT", "1.000"),),
            source=_source("7" * 64, "6" * 64, "fund.etf.nav.1d.reported"),
        )
        SqlAlchemyDatasetAvailabilityRepository(database).record(
            dataset="fund.etf.nav.1d.reported",
            partition_key=unsupported_partition,
            availability="currently_unsupported",
            reason_code="NAV_SEMANTICS_UNSUPPORTED_MONEY_MARKET",
            provider_id="integration-etf",
            observed_at=datetime(2026, 7, 30, 10, tzinfo=UTC),
            entity_partition=f"etf:{etf.qualified_key}",
            coverage_from=date(2026, 7, 1),
            coverage_to=date(2026, 7, 28),
        )
        query = replace(
            _nav_query(etf_id),
            time={
                "dimension": "TRADE_DATE",
                "from": "2026-06-01",
                "to": "2026-07-28",
            },
            request_fingerprint="integration-etf-nav-overlap-reader",
        )

        with pytest.raises(MarketDataAccessUnavailable) as captured:
            SqlAlchemyMarketDataAccessRepository(database).query(request=query, after=None)
    finally:
        SqlAlchemyDatasetAvailabilityRepository(database).clear(
            dataset="fund.etf.nav.1d.reported",
            partition_key=unsupported_partition,
            cleared_at=datetime(2026, 7, 30, 11, tzinfo=UTC),
        )
        database.close()

    assert captured.value.availability == "CURRENTLY_UNSUPPORTED"
    assert captured.value.reason_code == "NAV_SEMANTICS_UNSUPPORTED_MONEY_MARKET"
    assert captured.value.observed_at == datetime(2026, 7, 30, 10, tzinfo=UTC)
    assert captured.value.warnings == ("currently_unsupported",)


@pytest.mark.integration
def test_etf_master_first_publish_creates_listing_identity() -> None:
    """验证目录首次发布用明确上市日创建身份，并可承接早于目录观察日的历史事实。"""
    if os.environ.get("DATA_SYNC_RUN_INTEGRATION") != "1":
        pytest.skip("set DATA_SYNC_RUN_INTEGRATION=1 after starting local infrastructure")
    database = DatabaseClient.from_settings(load_settings())
    etf = EtfIdentifier("SSE", f"{uuid4().int % 1_000_000:06d}")
    reference_repository = SqlAlchemyEtfReferenceRepository(
        database,
        approved_sources={
            "integration-etf": EtfReferenceSourceApproval(
                provider_id="integration-etf",
                source_code="integration_etf_official",
                legal_name="ETF 集成测试官方来源",
                source_kind="official",
                rights_status="internal",
                license_scope="integration_test_only",
            )
        },
    )
    market_repository = SqlAlchemyEtfMarketDataRepository(
        database,
        approved_sources={
            "integration-etf": EtfSourceApproval(
                provider_id="integration-etf",
                source_code="integration_etf_official",
                legal_name="ETF 集成测试官方来源",
                source_kind="official",
                rights_status="internal",
                license_scope="integration_test_only",
            )
        },
    )
    profile = replace(_profile(etf), effective_from=date(2026, 7, 30))
    try:
        published = reference_repository.publish_profiles(
            profiles=(profile,),
            source=_source("9" * 64, "8" * 64, "fund.etf.master"),
        )
        bars = market_repository.publish_daily_bars(
            etf=etf,
            bars=(_bar(),),
            source=_source("7" * 64, "6" * 64, "fund.etf.bar.1d.raw"),
        )
        navs = market_repository.publish_navs(
            etf=etf,
            navs=(_nav("UNIT", "4.210"),),
            source=_source("5" * 64, "4" * 64, "fund.etf.nav.1d.reported"),
        )
        statuses = reference_repository.publish_statuses(
            etf=etf,
            statuses=(_status(etf),),
            source=_source("3" * 64, "2" * 64, "fund.etf.trading_state.raw"),
        )
        with database.session() as session:
            instrument_id = _etf_id(session, etf)
            listing = session.execute(
                select(EtfListing).where(EtfListing.instrument_id == instrument_id)
            ).scalar_one()
            identifier = session.execute(
                select(InstrumentIdentifierVersion).where(
                    InstrumentIdentifierVersion.entity_id == instrument_id,
                    InstrumentIdentifierVersion.known_to.is_(None),
                )
            ).scalar_one()
    finally:
        database.close()

    assert published.inserted_count == 1
    assert listing.management_mode == "PASSIVE"
    assert identifier.effective_from == profile.listed_on
    assert bars.inserted_count == 1
    assert navs.inserted_count == 1
    assert statuses.inserted_count == 1


@pytest.mark.integration
def test_etf_profile_correction_backfills_an_existing_identifier_start() -> None:
    """验证迟到的官方上市日以知识修订回填代码起点，不覆盖旧版本或伪造新工具。"""
    if os.environ.get("DATA_SYNC_RUN_INTEGRATION") != "1":
        pytest.skip("set DATA_SYNC_RUN_INTEGRATION=1 after starting local infrastructure")
    database = DatabaseClient.from_settings(load_settings())
    etf = EtfIdentifier("SSE", f"{uuid4().int % 1_000_000:06d}")
    reference_repository = SqlAlchemyEtfReferenceRepository(
        database,
        approved_sources={
            "integration-etf": EtfReferenceSourceApproval(
                provider_id="integration-etf",
                source_code="integration_etf_official",
                legal_name="ETF 集成测试官方来源",
                source_kind="official",
                rights_status="internal",
                license_scope="integration_test_only",
            )
        },
    )
    market_repository = SqlAlchemyEtfMarketDataRepository(
        database,
        approved_sources={
            "integration-etf": EtfSourceApproval(
                provider_id="integration-etf",
                source_code="integration_etf_official",
                legal_name="ETF 集成测试官方来源",
                source_kind="official",
                rights_status="internal",
                license_scope="integration_test_only",
            )
        },
    )
    first_profile = replace(
        _profile(etf),
        listed_on=None,
        effective_from=date(2026, 7, 29),
    )
    corrected_profile = replace(
        first_profile,
        listed_on=date(2012, 5, 28),
        effective_from=date(2026, 7, 30),
    )
    try:
        reference_repository.publish_profiles(
            profiles=(first_profile,),
            source=_source("1" * 64, "2" * 64, "fund.etf.master"),
        )
        reference_repository.publish_profiles(
            profiles=(corrected_profile,),
            source=_source("3" * 64, "4" * 64, "fund.etf.master"),
        )
        bars = market_repository.publish_daily_bars(
            etf=etf,
            bars=(_bar(),),
            source=_source("5" * 64, "6" * 64, "fund.etf.bar.1d.raw"),
        )
        with database.session() as session:
            instrument_id = _etf_id(session, etf)
            identifiers = session.scalars(
                select(InstrumentIdentifierVersion).where(
                    InstrumentIdentifierVersion.entity_id == instrument_id
                )
            ).all()
            instrument = session.execute(
                select(MarketInstrument).where(MarketInstrument.instrument_id == instrument_id)
            ).scalar_one()
    finally:
        database.close()

    current = [value for value in identifiers if value.known_to is None]
    historical = [value for value in identifiers if value.known_to is not None]
    assert len(current) == 1
    assert len(historical) == 1
    assert current[0].effective_from == corrected_profile.listed_on
    assert instrument.tradable_from == corrected_profile.listed_on
    assert bars.inserted_count == 1


@pytest.mark.integration
def test_profile_publication_deduplicates_daily_observations_and_freezes_two_venue_universe() -> (
    None
):
    """连续目录观察按实体保留唯一当前资料，冻结全集纳入暂停/未知并排除明确退市。"""
    if os.environ.get("DATA_SYNC_RUN_INTEGRATION") != "1":
        pytest.skip("set DATA_SYNC_RUN_INTEGRATION=1 after starting local infrastructure")
    database = DatabaseClient.from_settings(load_settings())
    seed = uuid4().int
    listed = EtfIdentifier("SSE", f"{seed % 1_000_000:06d}")
    delisted = EtfIdentifier("SSE", f"{(seed + 1) % 1_000_000:06d}")
    unknown = EtfIdentifier("SZSE", f"{(seed + 2) % 1_000_000:06d}")
    repository = SqlAlchemyEtfReferenceRepository(
        database,
        approved_sources={
            "integration-etf": EtfReferenceSourceApproval(
                provider_id="integration-etf",
                source_code="integration_etf_official",
                legal_name="ETF 集成测试官方来源",
                source_kind="official",
                rights_status="internal",
                license_scope="integration_test_only",
            )
        },
    )
    listed_first = replace(
        _profile(listed),
        display_name="目录去重上市 ETF",
        effective_from=date(2026, 7, 28),
    )
    delisted_first = replace(
        _profile(delisted),
        display_name="目录明确退市 ETF",
        listing_status="DELISTED",
        delisted_on=date(2026, 7, 27),
        effective_from=date(2026, 7, 28),
    )
    unknown_first = replace(
        _profile(unknown),
        display_name="目录状态未知 ETF",
        etf_type="货币市场基金",
        listing_status="UNKNOWN",
        effective_from=date(2026, 7, 28),
    )
    try:
        repository.publish_profiles(
            profiles=(listed_first, delisted_first),
            source=_source("5" * 64, "6" * 64, "fund.etf.master"),
        )
        repository.publish_profiles(
            profiles=(unknown_first,),
            source=_source("7" * 64, "8" * 64, "fund.etf.master"),
        )
        repeated = repository.publish_profiles(
            profiles=(
                replace(listed_first, effective_from=date(2026, 7, 29)),
                replace(delisted_first, effective_from=date(2026, 7, 29)),
            ),
            source=_source("9" * 64, "a" * 64, "fund.etf.master"),
        )
        changed = repository.publish_profiles(
            profiles=(
                replace(
                    listed_first,
                    listing_status="SUSPENDED",
                    manager_name="目录变更后的管理人",
                    effective_from=date(2026, 7, 30),
                ),
            ),
            source=_source("b" * 64, "c" * 64, "fund.etf.master"),
        )
        reader_page = SqlAlchemyMarketDataAccessRepository(database).query(
            request=_profile_v2_exact_query(listed),
            after=None,
        )
        with database.session() as session:
            versions = resolve_current_etf_profile_data_versions(session)
            snapshot = load_frozen_etf_universe(
                session,
                profile_data_versions=versions,
            )
            repeated_snapshot = load_frozen_etf_universe(
                session,
                profile_data_versions=versions,
            )
            listed_id = _etf_id(session, listed)
            current_rows = session.scalars(
                select(EtfProfileVersion).where(
                    EtfProfileVersion.etf_id == listed_id,
                    EtfProfileVersion.known_to.is_(None),
                )
            ).all()
    finally:
        database.close()

    qualified = {identifier.qualified_key for identifier in snapshot.identifiers}
    assert repeated.inserted_count == 0
    assert changed.inserted_count == 1
    assert len(reader_page.items) == 1
    assert listed.qualified_key in qualified
    assert unknown.qualified_key in qualified
    assert delisted.qualified_key not in qualified
    assert snapshot.count == len(qualified)
    assert snapshot.nav_eligible_count + snapshot.nav_unsupported_count == snapshot.count
    assert snapshot.nav_unsupported_count == 1
    assert snapshot.nav_unsupported[0].identifier == unknown
    assert snapshot.nav_unsupported[0].reason_code == "NAV_SEMANTICS_UNSUPPORTED_MONEY_MARKET"
    assert snapshot.universe_hash == repeated_snapshot.universe_hash
    assert len(current_rows) == 2
    assert {row.listing_status for row in current_rows} == {"LISTED", "SUSPENDED"}
    assert {row.effective_to for row in current_rows} == {date(2026, 7, 30), None}


def _seed_etf_identity(database: DatabaseClient, *, etf_id: UUID, etf: EtfIdentifier) -> None:
    """建立一条预治理的基金、份额和 ETF 上市工具链，仓储不得从代码临时伪造该身份。"""
    fund_id = uuid4()
    share_class_id = uuid4()
    venue_attributes = {
        "SSE": ("XSHG", "上海证券交易所"),
        "SZSE": ("XSHE", "深圳证券交易所"),
    }
    mic, venue_name = venue_attributes[etf.venue]
    with database.transaction() as session:
        source_batch_id = record_source_observation(
            session,
            provider_id="integration-etf-catalog",
            capability="fund.etf.master",
            source_payload_sha256="0" * 64,
            raw_uri="s3://integration/etf-catalog.json",
            observed_at=_OBSERVED_AT,
            created_at=_OBSERVED_AT,
        )
        session.execute(
            pg_insert(TradingVenue)
            .values(
                venue_id=uuid4(),
                mic=mic,
                code=etf.venue,
                name=venue_name,
                timezone="Asia/Shanghai",
                country="CN",
                active=True,
            )
            .on_conflict_do_nothing(index_elements=("code",))
        )
        venue_id = UUID(
            str(
                session.execute(
                    select(TradingVenue.venue_id).where(TradingVenue.code == etf.venue)
                ).scalar_one()
            )
        )
        session.execute(
            insert(MarketEntity).values(
                [
                    {
                        "entity_id": fund_id,
                        "entity_kind": "FUND",
                        "created_at": _OBSERVED_AT,
                        "retired_at": None,
                    },
                    {
                        "entity_id": share_class_id,
                        "entity_kind": "FUND_SHARE",
                        "created_at": _OBSERVED_AT,
                        "retired_at": None,
                    },
                    {
                        "entity_id": etf_id,
                        "entity_kind": "ETF_LISTING",
                        "created_at": _OBSERVED_AT,
                        "retired_at": None,
                    },
                ]
            )
        )
        session.execute(
            insert(FundLegalEntity).values(
                entity_id=fund_id,
                fund_type="ETF",
                base_currency="CNY",
            )
        )
        session.execute(
            insert(FundShareClass).values(
                entity_id=share_class_id,
                fund_entity_id=fund_id,
                share_class_code="TEST-A",
                currency="CNY",
                accumulation_kind="ACCUMULATING",
            )
        )
        session.execute(
            insert(MarketInstrument).values(
                instrument_id=etf_id,
                instrument_kind="ETF_LISTING",
                primary_venue_id=venue_id,
                tradable_from=date(2012, 5, 28),
                tradable_to=None,
            )
        )
        session.execute(
            insert(EtfListing).values(
                instrument_id=etf_id,
                share_class_entity_id=share_class_id,
                venue_id=venue_id,
                management_mode="PASSIVE",
            )
        )
        session.execute(
            insert(InstrumentIdentifierVersion).values(
                version_id=uuid4(),
                entity_id=etf_id,
                entity_kind="ETF_LISTING",
                venue_id=venue_id,
                identifier_scheme="venue_symbol",
                identifier_value=etf.symbol,
                effective_from=date(2012, 5, 28),
                effective_to=None,
                known_from=_OBSERVED_AT,
                known_to=None,
                source_time_precision="DATE_ONLY",
                source_batch_id=source_batch_id,
            )
        )


def _etf_id(session: Session, etf: EtfIdentifier) -> UUID:
    """按场所和代码读取刚由目录创建的 ETF 工具 UUID，验证身份链而非 profile 偶然写入。"""
    value = session.execute(
        select(InstrumentIdentifierVersion.entity_id)
        .join(TradingVenue, TradingVenue.venue_id == InstrumentIdentifierVersion.venue_id)
        .where(
            TradingVenue.code == etf.venue,
            InstrumentIdentifierVersion.entity_kind == "ETF_LISTING",
            InstrumentIdentifierVersion.identifier_scheme == "venue_symbol",
            InstrumentIdentifierVersion.identifier_value == etf.symbol,
            InstrumentIdentifierVersion.known_to.is_(None),
        )
    ).scalar_one()
    return UUID(str(value))


def _bar() -> EtfDailyBar:
    """构造一条不经复权的 ETF 日线，状态缺失不由成交量推断。"""
    return EtfDailyBar(
        trade_date=date(2026, 7, 28),
        open_price=Decimal("4.000"),
        high_price=Decimal("4.200"),
        low_price=Decimal("3.900"),
        close_price=Decimal("4.100"),
        volume_value=Decimal("100000"),
        volume_unit="SHARE",
        amount_value=Decimal("410000"),
        currency="CNY",
        trade_status=None,
    )


def _nav(kind: str, value: str) -> EtfNav:
    """构造一项来源直报 ETF NAV，单位和累计类型必须作为不同逻辑事实发布。"""
    return EtfNav(
        nav_date=date(2026, 7, 28),
        nav_kind=kind,
        nav_value=Decimal(value),
        currency="CNY",
        finality="FINAL",
    )


def _profile(etf: EtfIdentifier) -> EtfProfile:
    """构造一份明确上市状态的 ETF 资料，代码与法律基金身份已由测试夹具预先治理。"""
    return EtfProfile(
        etf=etf,
        etf_type="STOCK",
        management_mode="PASSIVE",
        manager_name="集成测试管理人",
        custodian_name="集成测试托管人",
        established_on=date(2012, 5, 1),
        listed_on=date(2012, 5, 28),
        delisted_on=None,
        quote_currency="CNY",
        nav_currency="CNY",
        listing_status="LISTED",
        effective_from=date(2026, 7, 28),
        source_time_precision="DATE_ONLY",
        display_name="集成测试沪深300ETF",
    )


def _status(etf: EtfIdentifier) -> EtfDailyStatus:
    """构造东财来源的单日申购状态，验证半开区间不会被无限延续。"""
    return EtfDailyStatus(
        etf=etf,
        status_dimension="SUBSCRIPTION",
        status_code="SUSPENDED",
        effective_from=date(2026, 7, 28),
        effective_to=date(2026, 7, 29),
        reason="集成测试暂停申购",
    )


def _source(raw_hash: str, normalized_hash: str, capability: str) -> EtfSourceObservation:
    """构造具备 raw/normalized 双对象的来源观察，便于检验 release 血缘。"""
    return EtfSourceObservation(
        provider_id="integration-etf",
        capability=capability,
        raw_payload_sha256=raw_hash,
        raw_uri=f"s3://integration/{raw_hash}/raw.json",
        raw_content_type="application/json",
        raw_byte_size=100,
        normalized_payload_sha256=normalized_hash,
        normalized_uri=f"s3://integration/{normalized_hash}/normalized.json",
        normalized_content_type="application/json",
        normalized_byte_size=80,
        observed_at=_OBSERVED_AT,
        upstream_source="integration-etf-official",
        adapter_version="integration-v1",
        schema_fingerprint="9" * 64,
    )


def _bar_query(etf_id: UUID) -> MarketDataQuery:
    """构造固定 ETF 与日期范围的内部 typed reader 请求，验证发布和读取共享 data version。"""
    return MarketDataQuery(
        dataset_code="fund.etf.bar.1d.reported",
        schema_version=2,
        business_scope="ETF",
        identity=None,
        time={"dimension": "TRADE_DATE", "from": "2026-07-28", "to": "2026-07-28"},
        visibility={"mode": "CURRENT"},
        selection={"qualityStatuses": ("PASSED",)},
        fields=("tradeDate", "etfEntityRef", "close", "volume"),
        filters=(MarketDataFilter("etfEntityRef", "EQ", (str(etf_id),)),),
        sort=(("tradeDate", "ASC"),),
        limit=10,
        request_fingerprint="integration-etf-reader",
    )


def _bar_365d_query(etf_id: UUID) -> MarketDataQuery:
    """构造覆盖 365 日的 ETF v2 行情查询，验证最近 31 日失败会提示旧 publication。"""
    return MarketDataQuery(
        dataset_code="fund.etf.bar.1d.reported",
        schema_version=2,
        business_scope="ETF",
        identity=None,
        time={"dimension": "TRADE_DATE", "from": "2025-07-29", "to": "2026-07-28"},
        visibility={"mode": "CURRENT"},
        selection={"qualityStatuses": ("PASSED",)},
        fields=("tradeDate", "etfEntityRef", "close", "volume"),
        filters=(MarketDataFilter("etfEntityRef", "EQ", (str(etf_id),)),),
        sort=(("tradeDate", "ASC"),),
        limit=366,
        request_fingerprint="integration-etf-365d-reader",
    )


def _nav_query(etf_id: UUID) -> MarketDataQuery:
    """构造 ETF v2 NAV 查询，验证运行时来源和记录来源引用冻结为东方财富。"""
    return MarketDataQuery(
        dataset_code="fund.etf.nav.1d.reported",
        schema_version=2,
        business_scope="ETF",
        identity=None,
        time={"dimension": "TRADE_DATE", "from": "2026-07-28", "to": "2026-07-28"},
        visibility={"mode": "CURRENT"},
        selection={"qualityStatuses": ("PASSED",)},
        fields=("navDate", "etfEntityRef", "navKind", "nav", "currency", "finality"),
        filters=(
            MarketDataFilter("etfEntityRef", "EQ", (str(etf_id),)),
            MarketDataFilter("navKind", "IN", ("UNIT", "ACCUMULATED")),
        ),
        sort=(("navDate", "ASC"),),
        limit=10,
        request_fingerprint="integration-etf-nav-v2-reader",
    )


def _profile_query(etf_id: UUID) -> MarketDataQuery:
    """构造场所限定的 ETF 产品资料查询，目录快照 publication 不与单上市工具状态混用。"""
    return MarketDataQuery(
        dataset_code="fund.etf.profile.reported",
        schema_version=1,
        business_scope="ETF",
        identity=None,
        time={"dimension": "EFFECTIVE_AT", "from": "2026-07-28", "to": "2026-07-28"},
        visibility={"mode": "CURRENT"},
        selection={"qualityStatuses": ("PASSED",)},
        fields=("etfEntityRef", "exchange", "symbol", "listingStatus"),
        filters=(
            MarketDataFilter("exchange", "EQ", ("SSE",)),
            MarketDataFilter("etfEntityRef", "EQ", (str(etf_id),)),
        ),
        sort=(),
        limit=10,
        request_fingerprint="integration-etf-profile-reader",
    )


def _profile_v2_query(etf: EtfIdentifier) -> MarketDataQuery:
    """查询前一日已生效的当前 profile，验证次日不会因 observation 日期不同而假空。"""
    return MarketDataQuery(
        dataset_code="fund.etf.profile.reported",
        schema_version=2,
        business_scope="ETF",
        identity=None,
        time={"dimension": "EFFECTIVE_AT", "from": "2026-07-29", "to": "2026-07-29"},
        visibility={"mode": "CURRENT"},
        selection={"qualityStatuses": ("PASSED",)},
        fields=(
            "etfEntityRef",
            "exchange",
            "symbol",
            "displayName",
            "etfType",
            "managementMode",
            "managerName",
            "custodianName",
            "listedOn",
            "delistedOn",
            "listingStatus",
            "quoteCurrency",
            "navCurrency",
            "sourceTimePrecision",
        ),
        filters=(
            MarketDataFilter("exchange", "EQ", ("SSE",)),
            MarketDataFilter("symbol", "PREFIX", (etf.symbol[:3],)),
            MarketDataFilter("displayName", "CONTAINS", ("沪深300",)),
            MarketDataFilter("listingStatus", "IN", ("LISTED", "UNKNOWN")),
        ),
        sort=(("displayName", "ASC"), ("symbol", "DESC")),
        limit=10,
        request_fingerprint="integration-etf-profile-v2-reader",
    )


def _profile_v2_exact_query(etf: EtfIdentifier) -> MarketDataQuery:
    """按来源给出的场所与完整代码查询唯一最新目录资料，不靠代码前缀判断类别。"""
    return MarketDataQuery(
        dataset_code="fund.etf.profile.reported",
        schema_version=2,
        business_scope="ETF",
        identity=None,
        time={"dimension": "EFFECTIVE_AT", "from": "2026-07-30", "to": "2026-07-30"},
        visibility={"mode": "CURRENT"},
        selection={"qualityStatuses": ("PASSED",)},
        fields=(
            "etfEntityRef",
            "exchange",
            "symbol",
            "displayName",
            "etfType",
            "managementMode",
            "managerName",
            "custodianName",
            "listedOn",
            "delistedOn",
            "listingStatus",
            "quoteCurrency",
            "navCurrency",
            "sourceTimePrecision",
        ),
        filters=(
            MarketDataFilter("exchange", "EQ", (etf.venue,)),
            MarketDataFilter("symbol", "EQ", (etf.symbol,)),
        ),
        sort=(("symbol", "ASC"),),
        limit=10,
        request_fingerprint="integration-etf-profile-v2-exact-reader",
    )


def _status_query(etf_id: UUID) -> MarketDataQuery:
    """构造 ETF v2 来源报告状态查询，读取器按半开有效区间定位事实。"""
    return MarketDataQuery(
        dataset_code="fund.etf.trading_state.reported",
        schema_version=2,
        business_scope="ETF",
        identity=None,
        time={"dimension": "EFFECTIVE_AT", "from": "2026-07-28", "to": "2026-07-28"},
        visibility={"mode": "CURRENT"},
        selection={"qualityStatuses": ("PASSED",)},
        fields=(
            "etfEntityRef",
            "stateDimension",
            "state",
            "effectiveFrom",
            "effectiveTo",
            "reason",
        ),
        filters=(MarketDataFilter("etfEntityRef", "EQ", (str(etf_id),)),),
        sort=(),
        limit=10,
        request_fingerprint="integration-etf-status-reader",
    )
