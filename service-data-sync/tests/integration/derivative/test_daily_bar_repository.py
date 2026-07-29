"""衍生品 P0 真实合约日线的 PostgreSQL 原子发布与 typed reader 集成测试。"""

from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import insert, select

from service_data_sync.application.ports.derivative_market import DerivativeSourceObservation
from service_data_sync.application.ports.market_data_access import MarketDataFilter, MarketDataQuery
from service_data_sync.bootstrap.settings import load_settings
from service_data_sync.domain.derivative import DerivativeContractIdentifier, DerivativeDailyBar
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.database.models.market.derivative_revisions import (
    DerivativeDailyBarRevision,
)
from service_data_sync.infrastructure.database.models.market.identity import (
    DerivativeContract,
    DerivativeProduct,
    InstrumentIdentifierVersion,
    MarketEntity,
    MarketInstrument,
    TradingVenue,
)
from service_data_sync.infrastructure.database.models.provenance.source_batch import SourceBatch
from service_data_sync.infrastructure.persistence.derivative_market_data_repository import (
    DerivativeSourceApproval,
    SqlAlchemyDerivativeDailyBarRepository,
)
from service_data_sync.infrastructure.persistence.source_batch import record_source_observation
from service_data_sync.infrastructure.persistence.sqlalchemy_market_data_access_repository import (
    SqlAlchemyMarketDataAccessRepository,
)

_CONTRACT = DerivativeContractIdentifier.parse("CFFEX.IF2608")
_MINIMAL_CONTRACT = DerivativeContractIdentifier.parse("CFFEX.TST2608")
_OBSERVED_AT = datetime(2026, 7, 29, 8, tzinfo=UTC)


@pytest.mark.integration
def test_real_contract_daily_bar_is_revisioned_published_and_read_from_a_fixed_version() -> None:
    """验证一条 P0 日线贯穿双证据、release、同内容重放和内部 typed reader。"""
    if os.environ.get("DATA_SYNC_RUN_INTEGRATION") != "1":
        pytest.skip("set DATA_SYNC_RUN_INTEGRATION=1 after starting local infrastructure")
    database = DatabaseClient.from_settings(load_settings())
    contract_id = uuid4()
    try:
        _seed_contract_identity(database, contract_id=contract_id)
        repository = SqlAlchemyDerivativeDailyBarRepository(
            database,
            approved_sources={
                "integration-derivative": DerivativeSourceApproval(
                    provider_id="integration-derivative",
                    source_code="integration_derivative_official",
                    legal_name="衍生品集成测试官方来源",
                    source_kind="official",
                    rights_status="internal",
                    license_scope="integration_test_only",
                )
            },
        )
        first = repository.publish_daily_bars(
            contract=_CONTRACT,
            bars=(_bar(),),
            source=_source("a" * 64, "b" * 64),
        )
        repeated = repository.publish_daily_bars(
            contract=_CONTRACT,
            bars=(_bar(),),
            source=_source("c" * 64, "d" * 64),
        )
        page = SqlAlchemyMarketDataAccessRepository(database).query(
            request=_query(contract_id), after=None
        )
        with database.session() as session:
            rows = (
                session.execute(
                    select(DerivativeDailyBarRevision).where(
                        DerivativeDailyBarRevision.contract_id == contract_id,
                        DerivativeDailyBarRevision.known_to.is_(None),
                    )
                )
                .scalars()
                .all()
            )
            source_batch_count = session.execute(
                select(SourceBatch.source_batch_id).where(
                    SourceBatch.capability == "derivative.bar.1d.reported"
                )
            ).all()
    finally:
        database.close()

    assert first.inserted_count == 1
    assert repeated.inserted_count == 0
    assert repeated.unchanged_count == 1
    assert repeated.data_version == first.data_version
    assert len(rows) == 1
    assert len(source_batch_count) >= 2
    assert page.data_version == first.data_version
    page_values = page.items[0]["values"]
    assert isinstance(page_values, Mapping)
    assert page_values["settlement"] == Decimal("3468")


@pytest.mark.integration
def test_first_daily_bar_creates_a_minimal_contract_identity_when_catalog_fields_are_unknown() -> (
    None
):
    """验证个人 AKShare 日线可先入库，未知产品和挂牌日保持空而不是阻断整个链路。"""
    if os.environ.get("DATA_SYNC_RUN_INTEGRATION") != "1":
        pytest.skip("set DATA_SYNC_RUN_INTEGRATION=1 after starting local infrastructure")
    database = DatabaseClient.from_settings(load_settings())
    product_entity_id: UUID | None = None
    listed_date: date | None = None
    page_count = 0
    try:
        repository = SqlAlchemyDerivativeDailyBarRepository(
            database,
            approved_sources={
                "integration-derivative": DerivativeSourceApproval(
                    provider_id="integration-derivative",
                    source_code="integration_derivative_official",
                    legal_name="衍生品集成测试官方来源",
                    source_kind="official",
                    rights_status="internal",
                    license_scope="integration_test_only",
                )
            },
        )
        publication = repository.publish_daily_bars(
            contract=_MINIMAL_CONTRACT,
            bars=(_bar(),),
            source=_source("0" * 64, "1" * 64),
        )
        with database.session() as session:
            identity = session.execute(
                select(DerivativeContract)
                .join(
                    InstrumentIdentifierVersion,
                    InstrumentIdentifierVersion.entity_id == DerivativeContract.instrument_id,
                )
                .where(
                    InstrumentIdentifierVersion.identifier_scheme == "venue_contract_code",
                    InstrumentIdentifierVersion.identifier_value == _MINIMAL_CONTRACT.contract_code,
                    InstrumentIdentifierVersion.known_to.is_(None),
                )
            ).scalar_one()
            page = SqlAlchemyMarketDataAccessRepository(database).query(
                request=_query(identity.instrument_id), after=None
            )
            product_entity_id = identity.product_entity_id
            listed_date = identity.listed_date
            page_count = len(page.items)
    finally:
        database.close()

    assert publication.inserted_count == 1
    assert product_entity_id is None
    assert listed_date is None
    assert page_count == 1


def _seed_contract_identity(database: DatabaseClient, *, contract_id: object) -> None:
    """建立 P0 日线允许引用的真实期货合约身份，测试不通过字符串推断产品或月份。"""
    assert isinstance(contract_id, type(uuid4()))
    venue_id = uuid4()
    product_id = uuid4()
    identity_batch_id: object
    with database.transaction() as session:
        identity_batch_id = record_source_observation(
            session,
            provider_id="integration-contract-catalog",
            capability="derivative.contract.catalog",
            source_payload_sha256="e" * 64,
            raw_uri="s3://integration/derivative-contract.json",
            observed_at=_OBSERVED_AT,
            created_at=_OBSERVED_AT,
        )
        session.execute(
            insert(TradingVenue).values(
                venue_id=venue_id,
                mic=None,
                code="CFFEX",
                name="中国金融期货交易所",
                timezone="Asia/Shanghai",
                country="CN",
                active=True,
            )
        )
        session.execute(
            insert(MarketEntity).values(
                [
                    {
                        "entity_id": product_id,
                        "entity_kind": "DERIVATIVE_PRODUCT",
                        "created_at": _OBSERVED_AT,
                        "retired_at": None,
                    },
                    {
                        "entity_id": contract_id,
                        "entity_kind": "FUTURE",
                        "created_at": _OBSERVED_AT,
                        "retired_at": None,
                    },
                ]
            )
        )
        session.execute(
            insert(MarketInstrument).values(
                instrument_id=contract_id,
                instrument_kind="FUTURE",
                primary_venue_id=venue_id,
                tradable_from=date(2026, 1, 1),
                tradable_to=None,
            )
        )
        session.execute(
            insert(DerivativeProduct).values(
                entity_id=product_id,
                venue_id=venue_id,
                product_code="IF",
                asset_kind="FUTURE",
                underlying_entity_id=None,
                currency="CNY",
            )
        )
        session.execute(
            insert(DerivativeContract).values(
                instrument_id=contract_id,
                product_entity_id=product_id,
                expiry_date=date(2026, 8, 21),
                call_put=None,
                strike_price=None,
                underlying_entity_id=None,
                listed_date=date(2026, 1, 1),
            )
        )
        session.execute(
            insert(InstrumentIdentifierVersion).values(
                version_id=uuid4(),
                entity_id=contract_id,
                entity_kind="FUTURE",
                venue_id=venue_id,
                identifier_scheme="venue_contract_code",
                identifier_value="IF2608",
                effective_from=date(2026, 1, 1),
                effective_to=None,
                known_from=_OBSERVED_AT,
                known_to=None,
                source_time_precision="EXACT",
                source_batch_id=identity_batch_id,
            )
        )


def _bar() -> DerivativeDailyBar:
    """构造结算价与收盘价不同的真实合约日线，防止 reader 把两者混成一个字段。"""
    return DerivativeDailyBar(
        trade_date=date(2026, 7, 28),
        open_price=Decimal("3450"),
        high_price=Decimal("3490"),
        low_price=Decimal("3420"),
        close_price=Decimal("3475"),
        pre_close_price=Decimal("3440"),
        settlement_price=Decimal("3468"),
        pre_settlement_price=Decimal("3438"),
        volume_value=Decimal("1200"),
        open_interest_value=Decimal("800"),
        turnover_value=Decimal("41600000"),
        turnover_currency="CNY",
        turnover_unit="CNY",
        trade_status="TRADING",
    )


def _source(raw_hash: str, normalized_hash: str) -> DerivativeSourceObservation:
    """构造一份可审计的 raw/标准载荷双对象来源观察。"""
    return DerivativeSourceObservation(
        provider_id="integration-derivative",
        capability="derivative.bar.1d.reported",
        raw_payload_sha256=raw_hash,
        raw_uri=f"s3://integration/{raw_hash}/raw.json",
        raw_content_type="application/json",
        raw_byte_size=100,
        normalized_payload_sha256=normalized_hash,
        normalized_uri=f"s3://integration/{normalized_hash}/normalized.json",
        normalized_content_type="application/json",
        normalized_byte_size=80,
        observed_at=_OBSERVED_AT,
        upstream_source="integration-derivative-official",
        adapter_version="integration-v1",
        schema_fingerprint="f" * 64,
    )


def _query(contract_id: object) -> MarketDataQuery:
    """构造固定真实合约和交易日范围的 internal typed reader 请求。"""
    assert isinstance(contract_id, type(uuid4()))
    return MarketDataQuery(
        dataset_code="derivative.bar.1d.reported",
        schema_version=1,
        business_scope="CONTRACT",
        identity=None,
        time={"dimension": "TRADE_DATE", "from": "2026-07-28", "to": "2026-07-28"},
        visibility={"mode": "CURRENT"},
        selection={"qualityStatuses": ("PASSED",)},
        fields=("tradeDate", "contractEntityRef", "close", "settlement"),
        filters=(MarketDataFilter("contractEntityRef", "EQ", (str(contract_id),)),),
        sort=(("tradeDate", "ASC"),),
        limit=10,
        request_fingerprint="integration-derivative-reader",
    )
