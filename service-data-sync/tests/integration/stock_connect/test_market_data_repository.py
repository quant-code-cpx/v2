"""沪深港通 P0 发布和内部 typed reader 的 PostgreSQL 集成测试。"""

from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from hashlib import sha256
from uuid import UUID, uuid4

import pytest
from sqlalchemy import insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from service_data_sync.application.ports.market_data_access import (
    MarketDataFilter,
    MarketDataQuery,
)
from service_data_sync.application.ports.stock_connect import StockConnectSourceObservation
from service_data_sync.bootstrap.settings import load_settings
from service_data_sync.domain.stock_connect import (
    StockConnectActiveSecurity,
    StockConnectCalendarDay,
    StockConnectChannel,
    StockConnectChannelStatus,
    StockConnectMarketDaily,
)
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.database.models.canonical import DataSource
from service_data_sync.infrastructure.database.models.equity.identity.equity_identifier_version import (  # noqa: E501
    EquityIdentifierVersion,
)
from service_data_sync.infrastructure.database.models.equity.identity.equity_instrument import (
    EquityInstrument,
)
from service_data_sync.infrastructure.database.models.market import (
    MarketEntity,
    MarketInstrument,
    StockConnectBundlePublication,
    StockConnectDisclosureRegime,
    StockConnectOverviewGeneration,
    StockConnectOverviewPublication,
    TradingVenue,
)
from service_data_sync.infrastructure.persistence.source_batch import record_source_observation
from service_data_sync.infrastructure.persistence.sqlalchemy_market_data_access_repository import (
    SqlAlchemyMarketDataAccessRepository,
)
from service_data_sync.infrastructure.persistence.stock_connect_center_repository import (
    SqlAlchemyStockConnectCenterRepository,
)
from service_data_sync.infrastructure.persistence.stock_connect_market_data_repository import (
    SqlAlchemyStockConnectMarketDataRepository,
    StockConnectSourceApproval,
)
from service_data_sync.infrastructure.persistence.typed_p0_support import ensure_methodology

_OBSERVED_AT = datetime(2026, 7, 29, 8, tzinfo=UTC)
_CHANNEL = StockConnectChannel("SH", "NORTHBOUND")


@pytest.mark.integration
def test_stock_connect_publications_are_read_from_independent_channel_releases() -> None:
    """验证通道统计和活跃榜具有独立版本，读取器不会混用不同发布或身份分区。"""
    if os.environ.get("DATA_SYNC_RUN_INTEGRATION") != "1":
        pytest.skip("set DATA_SYNC_RUN_INTEGRATION=1 after starting local infrastructure")
    database = DatabaseClient.from_settings(load_settings())
    try:
        venue_id = _seed_venue_and_regime(database)
        instrument_id, symbol = _seed_northbound_instrument(database, venue_id=venue_id)
        repository = SqlAlchemyStockConnectMarketDataRepository(
            database,
            approved_sources={
                "integration-stock-connect": StockConnectSourceApproval(
                    provider_id="integration-stock-connect",
                    source_code="integration_stock_connect_official",
                    legal_name="港通集成测试官方来源",
                    source_kind="official",
                    rights_status="internal",
                    license_scope="integration_test_only",
                    rights_evidence_ref=("license-audit:integration-stock-connect"),
                )
            },
        )
        market = repository.publish_market_daily(
            channel=_CHANNEL,
            records=(_market_record(),),
            source=_source("a" * 64, "b" * 64, "market.stock_connect.market_stat.reported"),
        )
        active = repository.publish_active_securities(
            channel=_CHANNEL,
            records=(_active_record(symbol),),
            source=_source(
                sha256(f"active-raw:{symbol}".encode()).hexdigest(),
                sha256(f"active-normalized:{symbol}".encode()).hexdigest(),
                "market.stock_connect.active_security.snapshot",
            ),
        )
        reader = SqlAlchemyMarketDataAccessRepository(database)
        market_page = reader.query(
            request=_query(
                dataset="market.stock_connect.market_stat.reported",
                fields=("tradeDate", "channel", "direction", "turnover", "netBuy"),
                filters=(
                    MarketDataFilter("channel", "EQ", ("SH",)),
                    MarketDataFilter("direction", "EQ", ("NORTHBOUND",)),
                ),
            ),
            after=None,
        )
        active_page = reader.query(
            request=_query(
                dataset="market.stock_connect.active_security.snapshot",
                fields=("tradeDate", "channel", "direction", "instrumentEntityRef", "rank"),
                filters=(
                    MarketDataFilter("channel", "EQ", ("SH",)),
                    MarketDataFilter("direction", "EQ", ("NORTHBOUND",)),
                    MarketDataFilter("instrumentEntityRef", "EQ", (str(instrument_id),)),
                ),
            ),
            after=None,
        )
        with database.session() as session:
            rights_evidence_ref = session.scalar(
                select(DataSource.rights_evidence_ref).where(
                    DataSource.code == "integration_stock_connect_official"
                )
            )
    finally:
        database.close()

    assert market_page.data_version == market.data_version
    market_values = market_page.items[0]["values"]
    assert isinstance(market_values, Mapping)
    assert market_values["turnover"] == Decimal("300000")
    assert active_page.data_version == active.data_version
    active_values = active_page.items[0]["values"]
    assert isinstance(active_values, Mapping)
    assert active_values["instrumentEntityRef"] == str(instrument_id)
    assert active_values["rank"] == 1
    assert rights_evidence_ref == "license-audit:integration-stock-connect"


@pytest.mark.integration
def test_all_four_overview_advances_only_after_missing_channels_are_completed() -> None:
    """第 2/3/4 通道前失败只推进健康单通道，幂等补齐后全四通道一次前移。"""
    if os.environ.get("DATA_SYNC_RUN_INTEGRATION") != "1":
        pytest.skip("set DATA_SYNC_RUN_INTEGRATION=1 after starting local infrastructure")
    database = DatabaseClient.from_settings(load_settings())
    channels = tuple(
        StockConnectChannel(channel, direction)
        for channel in ("SH", "SZ")
        for direction in ("NORTHBOUND", "SOUTHBOUND")
    )
    old_date = date(2099, 7, 27)
    new_date = date(2099, 7, 28)
    full_set = "SH_NORTHBOUND,SH_SOUTHBOUND,SZ_NORTHBOUND,SZ_SOUTHBOUND"
    northbound_set = "SH_NORTHBOUND,SZ_NORTHBOUND"
    overview_channels = tuple(
        sorted(f"{channel.channel}_{channel.direction}" for channel in channels)
    )
    old_generation_id = uuid4()
    new_generation_id = uuid4()
    approval = StockConnectSourceApproval(
        provider_id="integration-stock-connect",
        source_code="integration_stock_connect_official",
        legal_name="港通集成测试官方来源",
        source_kind="official",
        rights_status="internal",
        license_scope="integration_test_only",
        rights_evidence_ref="license-audit:integration-stock-connect",
    )
    market_repository = SqlAlchemyStockConnectMarketDataRepository(
        database,
        approved_sources={"integration-stock-connect": approval},
    )
    center_repository = SqlAlchemyStockConnectCenterRepository(database)
    try:
        for channel in channels:
            _publish_zero_bundle(
                market_repository=market_repository,
                center_repository=center_repository,
                channel=channel,
                trade_date=old_date,
                overview_generation_id=old_generation_id,
                overview_channels=overview_channels,
            )
        old_overview = _latest_overview_date(
            database,
            channel_set=full_set,
        )
        assert old_overview == old_date

        for index, channel in enumerate(channels, start=1):
            _publish_zero_bundle(
                market_repository=market_repository,
                center_repository=center_repository,
                channel=channel,
                trade_date=new_date,
                overview_generation_id=new_generation_id,
                overview_channels=overview_channels,
            )
            current = _latest_overview_date(
                database,
                channel_set=full_set,
            )
            assert current == (new_date if index == 4 else old_date)
            assert _latest_overview_date(
                database,
                channel_set=northbound_set,
            ) == (new_date if index == 4 else old_date)
            if index == 1:
                assert (
                    _latest_bundle_date(
                        database,
                        channel_code="SH_NORTHBOUND",
                    )
                    == new_date
                )

        before_retry = _current_overview_versions(
            database,
            trade_date=new_date,
            channel_set=full_set,
        )
        _publish_zero_bundle(
            market_repository=market_repository,
            center_repository=center_repository,
            channel=channels[-1],
            trade_date=new_date,
            overview_generation_id=new_generation_id,
            overview_channels=overview_channels,
        )
        after_retry = _current_overview_versions(
            database,
            trade_date=new_date,
            channel_set=full_set,
        )
        historical_generation_id = uuid4()
        for channel in channels:
            _publish_zero_bundle(
                market_repository=market_repository,
                center_repository=center_repository,
                channel=channel,
                trade_date=old_date,
                overview_generation_id=historical_generation_id,
                overview_channels=overview_channels,
            )
        with database.session() as session:
            historical_generation = session.get(
                StockConnectOverviewGeneration,
                (historical_generation_id, old_date),
            )
            assert historical_generation is not None
            assert historical_generation.completed_at is not None
        assert _latest_overview_date(database, channel_set=full_set) == new_date
    finally:
        database.close()

    assert len(before_retry) == 1
    assert after_retry == before_retry


@pytest.mark.integration
def test_overview_generation_honors_single_channel_and_direction_pair_selection() -> None:
    """同向 pair 必须同 run 齐备后推进，单通道 generation 则可独立形成 overview。"""
    if os.environ.get("DATA_SYNC_RUN_INTEGRATION") != "1":
        pytest.skip("set DATA_SYNC_RUN_INTEGRATION=1 after starting local infrastructure")
    database = DatabaseClient.from_settings(load_settings())
    pair = (
        StockConnectChannel("SH", "NORTHBOUND"),
        StockConnectChannel("SZ", "NORTHBOUND"),
    )
    pair_codes = ("SH_NORTHBOUND", "SZ_NORTHBOUND")
    pair_set = ",".join(pair_codes)
    old_date = date(2099, 8, 9)
    pair_date = date(2099, 8, 10)
    single_date = date(2099, 8, 11)
    approval = StockConnectSourceApproval(
        provider_id="integration-stock-connect",
        source_code="integration_stock_connect_official",
        legal_name="港通集成测试官方来源",
        source_kind="official",
        rights_status="internal",
        license_scope="integration_test_only",
        rights_evidence_ref="license-audit:integration-stock-connect",
    )
    market_repository = SqlAlchemyStockConnectMarketDataRepository(
        database,
        approved_sources={"integration-stock-connect": approval},
    )
    center_repository = SqlAlchemyStockConnectCenterRepository(database)
    try:
        old_generation = uuid4()
        for channel in pair:
            _publish_zero_bundle(
                market_repository=market_repository,
                center_repository=center_repository,
                channel=channel,
                trade_date=old_date,
                overview_generation_id=old_generation,
                overview_channels=pair_codes,
            )
        pair_generation = uuid4()
        _publish_zero_bundle(
            market_repository=market_repository,
            center_repository=center_repository,
            channel=pair[0],
            trade_date=pair_date,
            overview_generation_id=pair_generation,
            overview_channels=pair_codes,
        )
        assert _latest_bundle_date(database, channel_code="SH_NORTHBOUND") == pair_date
        assert _latest_overview_date(database, channel_set=pair_set) == old_date
        _publish_zero_bundle(
            market_repository=market_repository,
            center_repository=center_repository,
            channel=pair[1],
            trade_date=pair_date,
            overview_generation_id=pair_generation,
            overview_channels=pair_codes,
        )
        assert _latest_overview_date(database, channel_set=pair_set) == pair_date

        _publish_zero_bundle(
            market_repository=market_repository,
            center_repository=center_repository,
            channel=pair[0],
            trade_date=single_date,
            overview_generation_id=uuid4(),
            overview_channels=("SH_NORTHBOUND",),
        )
        assert _latest_overview_date(database, channel_set="SH_NORTHBOUND") == single_date
    finally:
        database.close()


def _publish_zero_bundle(
    *,
    market_repository: SqlAlchemyStockConnectMarketDataRepository,
    center_repository: SqlAlchemyStockConnectCenterRepository,
    channel: StockConnectChannel,
    trade_date: date,
    overview_generation_id: UUID,
    overview_channels: tuple[str, ...],
) -> None:
    """发布一个零成交真实日包，使测试只聚焦 channel-set 可见性边界。"""
    seed = f"{trade_date.isoformat()}:{channel.channel}:{channel.direction}"
    raw_hash = sha256(f"raw:{seed}".encode()).hexdigest()
    normalized_hash = sha256(f"normalized:{seed}".encode()).hexdigest()
    market = market_repository.publish_market_daily(
        channel=channel,
        records=(
            StockConnectMarketDaily(
                trade_date=trade_date,
                buy_amount=None,
                sell_amount=None,
                turnover_amount=Decimal("0"),
                net_buy_amount=None,
                quota_balance=None,
                currency=("CNY" if channel.direction == "NORTHBOUND" else "HKD"),
                availability_status="PARTIAL",
                field_availability=(
                    ("turnoverAmount", "REPORTED"),
                    ("buyAmount", "SOURCE_MISSING"),
                    ("sellAmount", "SOURCE_MISSING"),
                    ("netBuyAmount", "NOT_APPLICABLE"),
                    ("tradeCount", "SOURCE_MISSING"),
                    ("etfTurnoverAmount", "SOURCE_MISSING"),
                ),
            ),
        ),
        source=_source(
            raw_hash,
            normalized_hash,
            "market.stock_connect.market_stat.reported",
        ),
    )
    calendar_ref = _official_ref(
        source_code="HKEX_CALENDAR",
        product_name=f"{trade_date.year}-calendar.csv",
        digest=sha256(f"calendar:{trade_date.year}".encode()).hexdigest(),
    )
    status_digest = sha256(f"status:{seed}".encode()).hexdigest()
    status = StockConnectChannelStatus(
        trade_date=trade_date,
        channel=channel.channel,
        direction=channel.direction,
        trading_day=True,
        session_state="CLOSED",
        session_availability=("DERIVED" if channel.direction == "NORTHBOUND" else "REPORTED"),
        buy_order_accepted=None,
        sell_order_accepted=None,
        quota_state="SUFFICIENT",
        quota_balance=None,
        quota_currency="CNY",
        observed_at=_OBSERVED_AT,
        source_code=(
            "HKEX_OMDC"
            if channel.direction == "NORTHBOUND"
            else "SSE_MDGW"
            if channel.channel == "SH"
            else "SZSE_STEP"
        ),
        product_name=f"integration-status-{channel.channel}-{channel.direction}",
        source_publication_at=_OBSERVED_AT,
        source_file_sha256=status_digest,
    )
    center_repository.publish_bundle(
        channel=channel,
        overview_generation_id=overview_generation_id,
        overview_channels=overview_channels,
        market_data_version=market.data_version,
        active_data_version=None,
        calendar=StockConnectCalendarDay(
            calendar_date=trade_date,
            northbound_trading=True,
            southbound_trading=True,
            hong_kong_state="OPEN",
            mainland_state="OPEN",
        ),
        calendar_source_ref=calendar_ref,
        calendar_observed_at=_OBSERVED_AT,
        status=status,
        quality_issues=(),
        source_refs=(
            calendar_ref,
            _official_ref(
                source_code="HKEX_DATA_MARKETPLACE",
                product_name=f"integration-daily-{seed}",
                digest=raw_hash,
            ),
            _official_ref(
                source_code=status.source_code,
                product_name=status.product_name,
                digest=status_digest,
            ),
        ),
    )


def _official_ref(
    *,
    source_code: str,
    product_name: str,
    digest: str,
) -> dict[str, object]:
    """构造 publication 与 observed 语义分离的最小官方来源引用。"""
    return {
        "sourceCode": source_code,
        "productName": product_name,
        "sourcePublicationAvailability": "REPORTED",
        "sourcePublicationAt": _OBSERVED_AT.isoformat().replace("+00:00", "Z"),
        "sourceObservedAt": _OBSERVED_AT.isoformat().replace("+00:00", "Z"),
        "sourceFileSha256": digest,
    }


def _latest_overview_date(
    database: DatabaseClient,
    *,
    channel_set: str,
) -> date | None:
    """读取指定通道集合当前可见的最近共同交易日。"""
    with database.session() as session:
        return session.scalar(
            select(StockConnectOverviewPublication.trade_date)
            .where(
                StockConnectOverviewPublication.channel_set == channel_set,
                StockConnectOverviewPublication.superseded_at.is_(None),
            )
            .order_by(
                StockConnectOverviewPublication.trade_date.desc(),
                StockConnectOverviewPublication.published_at.desc(),
            )
            .limit(1)
        )


def _latest_bundle_date(
    database: DatabaseClient,
    *,
    channel_code: str,
) -> date | None:
    """读取单通道当前 bundle 日期，证明 partial generation 不阻塞健康通道详情。"""
    channel, direction = channel_code.split("_", maxsplit=1)
    with database.session() as session:
        return session.scalar(
            select(StockConnectBundlePublication.trade_date)
            .where(
                StockConnectBundlePublication.channel == channel,
                StockConnectBundlePublication.direction == direction,
                StockConnectBundlePublication.superseded_at.is_(None),
            )
            .order_by(
                StockConnectBundlePublication.trade_date.desc(),
                StockConnectBundlePublication.published_at.desc(),
            )
            .limit(1)
        )


def _current_overview_versions(
    database: DatabaseClient,
    *,
    trade_date: date,
    channel_set: str,
) -> tuple[str, ...]:
    """读取同日同集合的当前版本，幂等补跑不得产生第二个 current。"""
    with database.session() as session:
        return tuple(
            session.scalars(
                select(StockConnectOverviewPublication.data_version).where(
                    StockConnectOverviewPublication.trade_date == trade_date,
                    StockConnectOverviewPublication.channel_set == channel_set,
                    StockConnectOverviewPublication.superseded_at.is_(None),
                )
            )
        )


def _seed_venue_and_regime(database: DatabaseClient) -> UUID:
    """建立上交所和覆盖测试事实日的披露制度，缺制度时发布器必须 fail-closed。"""
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
        methodology_id = ensure_methodology(
            session,
            code="stock-connect-channel-reported",
            semantic_family="reported-stock-connect-channel",
            mapping_version="stock-connect-channel-v1",
            documentation_ref="docs/service-data-sync/0022-stock-connect/index.html",
        )
        session.execute(
            pg_insert(StockConnectDisclosureRegime)
            .values(
                regime_id=uuid4(),
                channel="SH",
                direction="NORTHBOUND",
                effective_from=date(2014, 11, 17),
                effective_to=None,
                available_fields=["turnover_amount", "net_buy_amount"],
                methodology_version_id=methodology_id,
                evidence_ref="integration://stock-connect-regime",
            )
            .on_conflict_do_nothing()
        )
        return UUID(
            str(
                session.execute(
                    select(TradingVenue.venue_id).where(TradingVenue.code == "SSE")
                ).scalar_one()
            )
        )


def _seed_northbound_instrument(database: DatabaseClient, *, venue_id: UUID) -> tuple[UUID, str]:
    """建立确认的 A 股和市场工具链，活跃榜解析不得仅因六码代码相同就跨市场合并。"""
    instrument_id = uuid4()
    symbol = f"{uuid4().int % 1_000_000:06d}"
    with database.transaction() as session:
        source_batch_id = record_source_observation(
            session,
            provider_id="integration-stock-connect-equity",
            capability="equity.catalog",
            source_payload_sha256="e" * 64,
            raw_uri=f"s3://integration/stock-connect-{symbol}.json",
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
                    name="港通集成测试证券",
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
            insert(MarketEntity).values(
                entity_id=instrument_id,
                entity_kind="EQUITY",
                created_at=_OBSERVED_AT,
                retired_at=None,
            )
        )
        session.execute(
            insert(MarketInstrument).values(
                instrument_id=instrument_id,
                instrument_kind="EQUITY",
                primary_venue_id=venue_id,
                tradable_from=date(2020, 1, 1),
                tradable_to=None,
            )
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
                content_sha256=b"e" * 32,
            )
        )
    return instrument_id, symbol


def _market_record() -> StockConnectMarketDaily:
    """构造只含制度允许金额字段的北向通道统计。"""
    return StockConnectMarketDaily(
        trade_date=date(2026, 7, 28),
        buy_amount=None,
        sell_amount=None,
        turnover_amount=Decimal("300000"),
        net_buy_amount=Decimal("10000"),
        quota_balance=None,
        currency="CNY",
        availability_status="COMPLETE",
    )


def _active_record(symbol: str) -> StockConnectActiveSecurity:
    """构造一条同日活跃榜事实，它依赖已发布的同通道市场统计 release。"""
    return StockConnectActiveSecurity(
        source_instrument_code=symbol,
        trade_date=date(2026, 7, 28),
        rank_no=1,
        buy_amount=Decimal("120000"),
        sell_amount=Decimal("110000"),
        turnover_amount=Decimal("230000"),
        currency="CNY",
    )


def _source(raw_hash: str, normalized_hash: str, capability: str) -> StockConnectSourceObservation:
    """构造一份 raw/normalized 双证据来源观察，测试不依赖网络 Provider。"""
    return StockConnectSourceObservation(
        provider_id="integration-stock-connect",
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
        upstream_source="integration-stock-connect-official",
        adapter_version="integration-v1",
        schema_fingerprint="9" * 64,
    )


def _query(
    *,
    dataset: str,
    fields: tuple[str, ...],
    filters: tuple[MarketDataFilter, ...],
) -> MarketDataQuery:
    """构造固定通道方向的内部 reader 请求，发布日期选择始终由 publication 解析。"""
    return MarketDataQuery(
        dataset_code=dataset,
        schema_version=1,
        business_scope="CHANNEL",
        identity=None,
        time={"dimension": "TRADE_DATE", "from": "2026-07-28", "to": "2026-07-28"},
        visibility={"mode": "CURRENT"},
        selection={"qualityStatuses": ("PASSED",)},
        fields=fields,
        filters=filters,
        sort=(),
        limit=10,
        request_fingerprint="integration-stock-connect-reader",
    )
