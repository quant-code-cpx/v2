"""融资融券 P0 场所市场汇总的 PostgreSQL 原子发布集成测试。"""

from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from service_data_sync.application.ports.margin_market import MarginSourceObservation
from service_data_sync.application.ports.market_data_access import (
    MarketDataFilter,
    MarketDataQuery,
)
from service_data_sync.bootstrap.settings import load_settings
from service_data_sync.domain.margin import (
    MarginEligibility,
    MarginMarketDaily,
    MarginSecurityDaily,
    MarginVenue,
)
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.database.models.equity.identity.equity_identifier_version import (  # noqa: E501
    EquityIdentifierVersion,
)
from service_data_sync.infrastructure.database.models.equity.identity.equity_instrument import (
    EquityInstrument,
)
from service_data_sync.infrastructure.database.models.market import (
    MarginEligibilityRevision,
    MarginMarketDailyRevision,
    MarginSecurityDailyRevision,
    TradingVenue,
)
from service_data_sync.infrastructure.persistence.margin_market_data_repository import (
    MarginSourceApproval,
    SqlAlchemyMarginMarketDataRepository,
)
from service_data_sync.infrastructure.persistence.source_batch import record_source_observation
from service_data_sync.infrastructure.persistence.sqlalchemy_market_data_access_repository import (
    SqlAlchemyMarketDataAccessRepository,
)

_OBSERVED_AT = datetime(2026, 7, 29, 8, tzinfo=UTC)


@pytest.mark.integration
def test_margin_market_daily_is_revisioned_published_and_replayed_idempotently() -> None:
    """验证两融市场汇总不会从证券明细补值，且同内容重放复用稳定 data version。"""
    if os.environ.get("DATA_SYNC_RUN_INTEGRATION") != "1":
        pytest.skip("set DATA_SYNC_RUN_INTEGRATION=1 after starting local infrastructure")
    database = DatabaseClient.from_settings(load_settings())
    try:
        venue_id = _seed_venue(database)
        repository = SqlAlchemyMarginMarketDataRepository(
            database,
            approved_sources={
                "integration-margin": MarginSourceApproval(
                    provider_id="integration-margin",
                    source_code="integration_margin_official",
                    legal_name="两融集成测试官方来源",
                    source_kind="official",
                    rights_status="internal",
                    license_scope="integration_test_only",
                )
            },
        )
        first = repository.publish_market_daily(
            venue=MarginVenue("SSE"), records=(_record(),), source=_source("a" * 64, "b" * 64)
        )
        repeated = repository.publish_market_daily(
            venue=MarginVenue("SSE"), records=(_record(),), source=_source("c" * 64, "d" * 64)
        )
        security_id, instrument_id, symbol = _seed_security_identity(database)
        first_security = repository.publish_security_daily(
            venue=MarginVenue("SSE"),
            records=(_security_record(symbol),),
            source=_source("e" * 64, "f" * 64, "market.margin.security.1d.reported"),
        )
        repeated_security = repository.publish_security_daily(
            venue=MarginVenue("SSE"),
            records=(_security_record(symbol),),
            source=_source("1" * 64, "2" * 64, "market.margin.security.1d.reported"),
        )
        first_eligibility = repository.publish_eligibility(
            venue=MarginVenue("SSE"),
            records=(_eligibility_record(symbol),),
            source=_source("4" * 64, "5" * 64, "market.margin.eligibility.reported"),
        )
        repeated_eligibility = repository.publish_eligibility(
            venue=MarginVenue("SSE"),
            records=(_eligibility_record(symbol),),
            source=_source("6" * 64, "7" * 64, "market.margin.eligibility.reported"),
        )
        with database.session() as session:
            rows = (
                session.execute(
                    select(MarginMarketDailyRevision).where(
                        MarginMarketDailyRevision.venue_id == venue_id,
                        MarginMarketDailyRevision.known_to.is_(None),
                    )
                )
                .scalars()
                .all()
            )
            security_rows = (
                session.execute(
                    select(MarginSecurityDailyRevision).where(
                        MarginSecurityDailyRevision.security_id == security_id,
                        MarginSecurityDailyRevision.known_to.is_(None),
                    )
                )
                .scalars()
                .all()
            )
            eligibility_rows = (
                session.execute(
                    select(MarginEligibilityRevision).where(
                        MarginEligibilityRevision.security_id == security_id,
                        MarginEligibilityRevision.known_to.is_(None),
                    )
                )
                .scalars()
                .all()
            )
        reader = SqlAlchemyMarketDataAccessRepository(database)
        market_page = reader.query(
            request=_query(
                dataset="market.margin.market.1d.reported",
                business_scope="MARKET",
                dimension="TRADE_DATE",
                fields=("tradeDate", "marginBalance"),
                filters=(MarketDataFilter("venueEntityRef", "EQ", (str(venue_id),)),),
            ),
            after=None,
        )
        security_page = reader.query(
            request=_query(
                dataset="market.margin.security.1d.reported",
                business_scope="SECURITY",
                dimension="TRADE_DATE",
                fields=("tradeDate", "equityEntityRef", "financingBalance"),
                filters=(
                    MarketDataFilter("venueEntityRef", "EQ", (str(venue_id),)),
                    MarketDataFilter("equityEntityRef", "EQ", (str(instrument_id),)),
                ),
            ),
            after=None,
        )
        eligibility_page = reader.query(
            request=_query(
                dataset="market.margin.eligibility.reported",
                business_scope="SECURITY",
                dimension="EFFECTIVE_AT",
                fields=("equityEntityRef", "eligibilityStatus", "effectiveFrom"),
                filters=(
                    MarketDataFilter("venueEntityRef", "EQ", (str(venue_id),)),
                    MarketDataFilter("equityEntityRef", "EQ", (str(instrument_id),)),
                ),
            ),
            after=None,
        )
    finally:
        database.close()

    assert first.inserted_count == 1
    assert repeated.inserted_count == 0
    assert repeated.unchanged_count == 1
    assert first.data_version == repeated.data_version
    assert len(rows) == 1
    assert rows[0].financing_balance == Decimal("1000000")
    assert rows[0].financing_repayment_amount is None
    assert first_security.inserted_count == 1
    assert repeated_security.inserted_count == 0
    assert repeated_security.data_version == first_security.data_version
    assert len(security_rows) == 1
    assert security_rows[0].financing_repayment_reported is None
    assert first_eligibility.inserted_count == 1
    assert repeated_eligibility.inserted_count == 0
    assert repeated_eligibility.data_version == first_eligibility.data_version
    assert len(eligibility_rows) == 1
    assert eligibility_rows[0].evidence_basis == "OFFICIAL_ANNOUNCEMENT"
    assert market_page.data_version == first.data_version
    market_values = market_page.items[0]["values"]
    assert isinstance(market_values, Mapping)
    assert market_values["marginBalance"] == Decimal("1200000")
    assert security_page.data_version == first_security.data_version
    security_values = security_page.items[0]["values"]
    assert isinstance(security_values, Mapping)
    assert security_values["equityEntityRef"] == str(instrument_id)
    assert eligibility_page.data_version == first_eligibility.data_version
    eligibility_values = eligibility_page.items[0]["values"]
    assert isinstance(eligibility_values, Mapping)
    assert eligibility_values["eligibilityStatus"] == "ELIGIBLE"


def _seed_venue(database: DatabaseClient) -> UUID:
    """建立或复用上交所场所身份，仓储不能因同步输入临时创建非治理场所。"""
    with database.transaction() as session:
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
        return UUID(
            str(
                session.execute(
                    select(TradingVenue.venue_id).where(TradingVenue.code == "SSE")
                ).scalar_one()
            )
        )


def _record() -> MarginMarketDaily:
    """构造一条含真实零、空直报偿还和精确币种单位的场所汇总。"""
    return MarginMarketDaily(
        trade_date=date(2026, 7, 28),
        financing_balance=Decimal("1000000"),
        financing_buy_amount=Decimal("0"),
        financing_repayment_amount=None,
        lending_balance_amount=Decimal("200000"),
        lending_balance_qty=Decimal("30000"),
        lending_sell_qty=Decimal("0"),
        lending_repayment_qty=None,
        total_balance=Decimal("1200000"),
        currency="CNY",
        quantity_unit="SHARE",
    )


def _seed_security_identity(database: DatabaseClient) -> tuple[int, UUID, str]:
    """建立当前 CONFIRMED 证券代码版本，验证两融仓储拒绝直接把来源代码作为内部身份。"""
    symbol = f"{uuid4().int % 1_000_000:06d}"
    instrument_id = uuid4()
    with database.transaction() as session:
        source_batch_id = record_source_observation(
            session,
            provider_id="integration-margin-equity-catalog",
            capability="equity.catalog",
            source_payload_sha256="3" * 64,
            raw_uri=f"s3://integration/margin-equity-{symbol}.json",
            observed_at=_OBSERVED_AT,
            created_at=_OBSERVED_AT,
        )
        security_id = int(
            session.execute(
                insert(EquityInstrument)
                .values(
                    instrument_id=instrument_id,
                    exchange="SSE",
                    symbol=symbol,
                    name="两融集成测试证券",
                    listing_status="LISTED",
                    created_at=_OBSERVED_AT,
                    updated_at=_OBSERVED_AT,
                    master_confirmed_at=_OBSERVED_AT,
                    current_master_version=None,
                )
                .returning(EquityInstrument.security_id)
            ).scalar_one()
        )
        session.execute(
            insert(EquityIdentifierVersion).values(
                version_id=uuid4(),
                security_id=security_id,
                exchange="SSE",
                symbol=symbol,
                identity_state="CONFIRMED",
                effective_from=date(2020, 1, 1),
                effective_to=None,
                known_from=_OBSERVED_AT,
                known_to=None,
                effective_date_precision="OFFICIAL_DATE",
                source_batch_id=source_batch_id,
                content_sha256=b"3" * 32,
            )
        )
    return security_id, instrument_id, symbol


def _query(
    *,
    dataset: str,
    business_scope: str,
    dimension: str,
    fields: tuple[str, ...],
    filters: tuple[MarketDataFilter, ...],
) -> MarketDataQuery:
    """构造一个固定当前版本的 P0 reader 集成请求，所有分区过滤均显式传入。"""
    return MarketDataQuery(
        dataset_code=dataset,
        schema_version=1,
        business_scope=business_scope,
        identity=None,
        time={"dimension": dimension, "from": "2026-07-28", "to": "2026-07-28"},
        visibility={"mode": "CURRENT"},
        selection={"qualityStatuses": ("PASSED",)},
        fields=fields,
        filters=filters,
        sort=(),
        limit=10,
        request_fingerprint="a" * 64,
    )


def _security_record(symbol: str) -> MarginSecurityDaily:
    """构造一条证券级来源直报事实，未披露偿还额不会被差分派生写入。"""
    return MarginSecurityDaily(
        source_security_code=symbol,
        trade_date=date(2026, 7, 28),
        financing_balance=Decimal("100"),
        financing_buy_amount=Decimal("0"),
        financing_repayment_reported=None,
        financing_repayment_derived=None,
        lending_balance_qty=Decimal("10"),
        quantity_unit="SHARE",
        currency="CNY",
        null_reason="NOT_PUBLISHED",
    )


def _eligibility_record(symbol: str) -> MarginEligibility:
    """构造一条官方公告资格事实，重放不能因观察批次变化虚增知识版本。"""
    return MarginEligibility(
        source_security_code=symbol,
        status="ELIGIBLE",
        effective_from=date(2026, 7, 28),
        effective_to=None,
        announcement_on=date(2026, 7, 27),
        evidence_basis="OFFICIAL_ANNOUNCEMENT",
    )


def _source(
    raw_hash: str,
    normalized_hash: str,
    capability: str = "market.margin.market.1d.reported",
) -> MarginSourceObservation:
    """构造具备两份对象证据的来源观察，验证重放不依赖上游临时状态。"""
    return MarginSourceObservation(
        provider_id="integration-margin",
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
        upstream_source="integration-margin-official",
        adapter_version="integration-v1",
        schema_fingerprint="9" * 64,
    )
