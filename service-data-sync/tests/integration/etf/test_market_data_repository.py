"""ETF P0 日线和 NAV 的 PostgreSQL 原子发布集成测试。"""

from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from service_data_sync.application.ports.etf_market import EtfSourceObservation
from service_data_sync.application.ports.market_data_access import MarketDataFilter, MarketDataQuery
from service_data_sync.bootstrap.settings import load_settings
from service_data_sync.domain.etf import (
    EtfDailyBar,
    EtfDailyStatus,
    EtfIdentifier,
    EtfNav,
    EtfProfile,
)
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.database.models.etf import EtfDailyBarRevision, EtfNavRevision
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
        profile_page = reader.query(request=_profile_query(etf_id), after=None)
        status_page = reader.query(request=_status_query(etf_id), after=None)
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
    page_values = page.items[0]["values"]
    assert isinstance(page_values, Mapping)
    assert page_values["close"] == Decimal("4.100")
    assert profile_page.data_version == profile.data_version
    profile_values = profile_page.items[0]["values"]
    assert isinstance(profile_values, Mapping)
    assert profile_values["listingStatus"] == "LISTED"
    assert status_page.data_version == status.data_version
    status_values = status_page.items[0]["values"]
    assert isinstance(status_values, Mapping)
    assert status_values["state"] == "HALTED"
    assert len(bar_rows) == 1
    assert bar_rows[0].close_price == Decimal("4.100")
    assert {(item.nav_kind, item.nav_value) for item in nav_rows} == {
        ("UNIT", Decimal("4.210")),
        ("ACCUMULATED", Decimal("4.321")),
    }


@pytest.mark.integration
def test_etf_master_first_publish_creates_listing_identity() -> None:
    """验证目录首次发布可原子创建 ETF 身份，不要求人工预插基金、份额或代码链。"""
    if os.environ.get("DATA_SYNC_RUN_INTEGRATION") != "1":
        pytest.skip("set DATA_SYNC_RUN_INTEGRATION=1 after starting local infrastructure")
    database = DatabaseClient.from_settings(load_settings())
    etf = EtfIdentifier("SSE", f"{uuid4().int % 1_000_000:06d}")
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
    try:
        published = repository.publish_profiles(
            profiles=(_profile(etf),),
            source=_source("9" * 64, "8" * 64, "fund.etf.master"),
        )
        with database.session() as session:
            instrument_id = _etf_id(session, etf)
            listing = session.execute(
                select(EtfListing).where(EtfListing.instrument_id == instrument_id)
            ).scalar_one()
    finally:
        database.close()

    assert published.inserted_count == 1
    assert listing.management_mode == "PASSIVE"


def _seed_etf_identity(database: DatabaseClient, *, etf_id: UUID, etf: EtfIdentifier) -> None:
    """建立一条预治理的基金、份额和 ETF 上市工具链，仓储不得从代码临时伪造该身份。"""
    fund_id = uuid4()
    share_class_id = uuid4()
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
                mic="XSHG",
                code="SSE",
                name="上海证券交易所",
                timezone="Asia/Shanghai",
                country="CN",
                active=True,
            )
            .on_conflict_do_nothing(index_elements=("code",))
        )
        venue_id = UUID(
            str(
                session.execute(
                    select(TradingVenue.venue_id).where(TradingVenue.code == "SSE")
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
    )


def _status(etf: EtfIdentifier) -> EtfDailyStatus:
    """构造交易状态事实，未声明申购或赎回状态以验证三个维度独立发布。"""
    return EtfDailyStatus(
        etf=etf,
        status_dimension="TRADING",
        status_code="HALTED",
        effective_from=date(2026, 7, 28),
        effective_to=None,
        reason="集成测试停牌",
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
        schema_version=1,
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


def _status_query(etf_id: UUID) -> MarketDataQuery:
    """构造 ETF 交易状态查询，读取器以状态维度和有效日期而非成交量定位事实。"""
    return MarketDataQuery(
        dataset_code="fund.etf.trading_state.reported",
        schema_version=1,
        business_scope="ETF",
        identity=None,
        time={"dimension": "EFFECTIVE_AT", "from": "2026-07-28", "to": "2026-07-28"},
        visibility={"mode": "CURRENT"},
        selection={"qualityStatuses": ("PASSED",)},
        fields=("etfEntityRef", "stateDimension", "state", "effectiveFrom"),
        filters=(MarketDataFilter("etfEntityRef", "EQ", (str(etf_id),)),),
        sort=(),
        limit=10,
        request_fingerprint="integration-etf-status-reader",
    )
