"""0028 已发布市场数据的 SQLAlchemy typed reader，只读取 immutable canonical publication。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.engine import Row
from sqlalchemy.orm import Session

from service_data_sync.application.ports.market_data_access import (
    MarketDataAccessRepository,
    MarketDataAccessUnavailable,
    MarketDataDatasetDescriptor,
    MarketDataDatasetNotFound,
    MarketDataQuery,
    MarketDataQueryPage,
    MarketDataRequestValidationError,
    MarketDataSourceDescriptor,
)
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.database.models.canonical import DatasetRelease
from service_data_sync.infrastructure.database.models.equity.identity.equity_instrument import (
    EquityInstrument,
)
from service_data_sync.infrastructure.database.models.etf import (
    EtfDailyBarRevision,
    EtfNavRevision,
    EtfProfileVersion,
    EtfStatusRevision,
)
from service_data_sync.infrastructure.database.models.market.derivative_revisions import (
    DerivativeDailyBarRevision,
)
from service_data_sync.infrastructure.database.models.market.equity_expansion import (
    BlockTradeExecutionRevision,
    CorporateEvent,
    CorporateEventRevision,
    DisclosureDocument,
    DragonTigerEventRevision,
)
from service_data_sync.infrastructure.database.models.market.identity import (
    EtfListing,
    InstrumentIdentifierVersion,
    TradingVenue,
)
from service_data_sync.infrastructure.database.models.market.margin_stock_connect import (
    MarginEligibilityRevision,
    MarginMarketDailyRevision,
    MarginSecurityDailyRevision,
    StockConnectActiveSecurityRevision,
    StockConnectChannelDailyRevision,
)
from service_data_sync.infrastructure.database.models.provenance.source_batch import SourceBatch
from service_data_sync.infrastructure.database.models.publication.dataset_publication import (
    DatasetPublication,
)
from service_data_sync.infrastructure.persistence.market_data_access_repository import (
    CatalogMarketDataAccessRepository,
    default_market_data_descriptors,
)

_DERIVATIVE_DAILY_BAR = "derivative.bar.1d.reported"
_ETF_DAILY_BAR = "fund.etf.bar.1d.reported"
_ETF_NAV = "fund.etf.nav.1d.reported"
_ETF_STATUS = "fund.etf.trading_state.reported"
_ETF_PROFILE = "fund.etf.profile.reported"
_MARGIN_MARKET = "market.margin.market.1d.reported"
_MARGIN_SECURITY = "market.margin.security.1d.reported"
_MARGIN_ELIGIBILITY = "market.margin.eligibility.reported"
_STOCK_CONNECT_MARKET = "market.stock_connect.market_stat.reported"
_STOCK_CONNECT_ACTIVE = "market.stock_connect.active_security.snapshot"
_DRAGON_TIGER = "equity.dragon_tiger.disclosure.reported"
_BLOCK_TRADE = "equity.block_trade.execution.reported"
_CORPORATE_EARNINGS = "equity.corporate_event.earnings.reported"
_READABLE_DATASETS = frozenset(
    {
        _DERIVATIVE_DAILY_BAR,
        _ETF_DAILY_BAR,
        _ETF_NAV,
        _ETF_STATUS,
        _ETF_PROFILE,
        _MARGIN_MARKET,
        _MARGIN_SECURITY,
        _MARGIN_ELIGIBILITY,
        _STOCK_CONNECT_MARKET,
        _STOCK_CONNECT_ACTIVE,
        _DRAGON_TIGER,
        _BLOCK_TRADE,
        _CORPORATE_EARNINGS,
    }
)


class SqlAlchemyMarketDataAccessRepository(MarketDataAccessRepository):
    """以运行时 catalog 约束所有读取，只从已发布数据版本投影 typed canonical record。"""

    def __init__(self, database: DatabaseClient) -> None:
        """接收服务私有数据库，不允许 API 层直接拼接表、列或 Provider 查询。"""
        self._database = database
        self._catalog = CatalogMarketDataAccessRepository(default_market_data_descriptors())

    def search_datasets(
        self,
        *,
        priorities: frozenset[str],
        availability: frozenset[str],
        query: str | None,
    ) -> Sequence[MarketDataDatasetDescriptor]:
        """根据当前合格 publication 动态公布 `AVAILABLE`，其余 P0 项仍显示为禁用。"""
        descriptors = self._catalog.search_datasets(
            priorities=priorities, availability=frozenset(), query=query
        )
        with self._database.session() as session:
            published_codes = set(
                session.execute(
                    select(DatasetPublication.dataset).where(
                        DatasetPublication.superseded_at.is_(None),
                        DatasetPublication.quality_status.in_(("passed", "warned")),
                    )
                ).scalars()
            )
        resolved = tuple(
            replace(
                descriptor,
                availability=(
                    "AVAILABLE"
                    if descriptor.code in published_codes and descriptor.code in _READABLE_DATASETS
                    else "DISABLED"
                ),
                availability_reason=(
                    None
                    if descriptor.code in published_codes and descriptor.code in _READABLE_DATASETS
                    else "等待读取实现，或来源许可、连续探针、PIT 和 shadow 质量门禁。"
                ),
            )
            for descriptor in descriptors
        )
        return tuple(
            descriptor
            for descriptor in resolved
            if not availability or descriptor.availability in availability
        )

    def query(self, *, request: MarketDataQuery, after: str | None) -> MarketDataQueryPage:
        """选择 immutable publication 后读取已实现 typed dataset，其他目录项保持严格不可用。"""
        if request.dataset_code == _ETF_DAILY_BAR:
            return self._query_etf_daily_bars(request=request, after=after)
        if request.dataset_code == _ETF_NAV:
            return self._query_etf_navs(request=request, after=after)
        if request.dataset_code == _ETF_STATUS:
            return self._query_etf_statuses(request=request, after=after)
        if request.dataset_code == _ETF_PROFILE:
            return self._query_etf_profiles(request=request, after=after)
        if request.dataset_code == _MARGIN_MARKET:
            return self._query_margin_market_daily(request=request, after=after)
        if request.dataset_code == _MARGIN_SECURITY:
            return self._query_margin_security_daily(request=request, after=after)
        if request.dataset_code == _MARGIN_ELIGIBILITY:
            return self._query_margin_eligibility(request=request, after=after)
        if request.dataset_code == _STOCK_CONNECT_MARKET:
            return self._query_stock_connect_market_daily(request=request, after=after)
        if request.dataset_code == _STOCK_CONNECT_ACTIVE:
            return self._query_stock_connect_active_security(request=request, after=after)
        if request.dataset_code == _DRAGON_TIGER:
            return self._query_dragon_tiger(request=request, after=after)
        if request.dataset_code == _BLOCK_TRADE:
            return self._query_block_trades(request=request, after=after)
        if request.dataset_code == _CORPORATE_EARNINGS:
            return self._query_corporate_earnings(request=request, after=after)
        if request.dataset_code != _DERIVATIVE_DAILY_BAR:
            return self._catalog.query(request=request, after=after)
        descriptor = next(
            (
                item
                for item in default_market_data_descriptors()
                if item.code == request.dataset_code
                and item.schema_version == request.schema_version
            ),
            None,
        )
        if descriptor is None:
            raise MarketDataDatasetNotFound(request.dataset_code)
        self._catalog._assert_contract(descriptor, request)
        if request.business_scope != "CONTRACT":
            raise MarketDataRequestValidationError("derivative daily bars require CONTRACT scope")
        contract_id = _contract_filter(request)
        start, end = _trade_date_range(request)
        position = _after_trade_date(after)
        with self._database.session() as session:
            publication = _select_publication(session, request=request, contract_id=contract_id)
            if publication is None or publication.release_id is None:
                raise MarketDataAccessUnavailable("derivative daily-bar publication is unavailable")
            query = (
                select(DerivativeDailyBarRevision, SourceBatch.provider_id)
                .join(
                    SourceBatch,
                    SourceBatch.source_batch_id == DerivativeDailyBarRevision.source_batch_id,
                )
                .join(DatasetRelease, DatasetRelease.release_id == publication.release_id)
                .where(
                    DerivativeDailyBarRevision.contract_id == contract_id,
                    DerivativeDailyBarRevision.methodology_version_id
                    == DatasetRelease.methodology_version_id,
                )
            )
            # `release_id` 不等于方法学；使用 publication 的知识时间冻结逻辑 revision 快照。
            query = query.where(
                DerivativeDailyBarRevision.known_from <= publication.knowledge_cutoff,
                or_(
                    DerivativeDailyBarRevision.known_to.is_(None),
                    DerivativeDailyBarRevision.known_to > publication.knowledge_cutoff,
                ),
                DerivativeDailyBarRevision.trade_date >= start,
                DerivativeDailyBarRevision.trade_date <= end,
            )
            if position is not None:
                query = query.where(DerivativeDailyBarRevision.trade_date > position)
            rows = session.execute(
                query.order_by(DerivativeDailyBarRevision.trade_date).limit(request.limit + 1)
            ).all()
            identifier = _current_identifier(session, contract_id=contract_id)
        visible = rows[: request.limit]
        next_position = (
            visible[-1][0].trade_date.isoformat() if len(rows) > request.limit and visible else None
        )
        return MarketDataQueryPage(
            data_version=UUID(str(publication.data_version)),
            published_at=_utc(publication.published_at),
            knowledge_cutoff=_utc(publication.knowledge_cutoff),
            public_usable_at=_utc(publication.published_at),
            quality_status=str(publication.quality_status),
            completeness="COMPLETE",
            items=tuple(
                _record(
                    row=row,
                    provider_id=provider_id,
                    data_version=UUID(str(publication.data_version)),
                    contract_id=contract_id,
                    identifier=identifier,
                    selected_fields=request.fields,
                )
                for row, provider_id in visible
            ),
            next_position=next_position,
            methodology={
                "code": "derivative-real-contract-daily-bar",
                "version": "1",
                "kind": "REPORTED",
            },
            sources=tuple(
                MarketDataSourceDescriptor(
                    source_ref=_source_ref(provider_id),
                    publisher="已批准衍生品来源",
                    source_dataset="真实合约日线",
                    authoritative=True,
                    redistribution="INTERNAL_ONLY",
                )
                for provider_id in sorted({provider_id for _row, provider_id in visible})
            )
            or (
                MarketDataSourceDescriptor(
                    source_ref="src_approved_derivative",
                    publisher="已批准衍生品来源",
                    source_dataset="真实合约日线",
                    authoritative=True,
                    redistribution="INTERNAL_ONLY",
                ),
            ),
            coverage={
                "from": start.isoformat(),
                "to": end.isoformat(),
                "pitCoverage": "COMPLETE",
                "gaps": [],
            },
        )

    def _query_etf_daily_bars(
        self, *, request: MarketDataQuery, after: str | None
    ) -> MarketDataQueryPage:
        """读取已发布 ETF 未复权日线，分区、知识截点与数据版本始终绑定同一上市工具。"""
        descriptor = _descriptor(request)
        self._catalog._assert_contract(descriptor, request)
        if request.business_scope not in {"ETF", "FUND"}:
            raise MarketDataRequestValidationError("ETF daily bars require ETF or FUND scope")
        etf_id = _entity_filter(request, field="etfEntityRef", label="ETF daily bars")
        start, end = _trade_date_range(request)
        position = _after_trade_date(after)
        with self._database.session() as session:
            publication = _select_entity_publication(
                session, request=request, dataset=_ETF_DAILY_BAR, entity_id=etf_id
            )
            if publication is None or publication.release_id is None:
                raise MarketDataAccessUnavailable("ETF daily-bar publication is unavailable")
            statement = (
                select(EtfDailyBarRevision, SourceBatch.provider_id)
                .join(
                    SourceBatch, SourceBatch.source_batch_id == EtfDailyBarRevision.source_batch_id
                )
                .join(DatasetRelease, DatasetRelease.release_id == publication.release_id)
                .where(
                    EtfDailyBarRevision.etf_id == etf_id,
                    EtfDailyBarRevision.methodology_version_id
                    == DatasetRelease.methodology_version_id,
                    EtfDailyBarRevision.known_from <= publication.knowledge_cutoff,
                    or_(
                        EtfDailyBarRevision.known_to.is_(None),
                        EtfDailyBarRevision.known_to > publication.knowledge_cutoff,
                    ),
                    EtfDailyBarRevision.trade_date >= start,
                    EtfDailyBarRevision.trade_date <= end,
                )
            )
            if position is not None:
                statement = statement.where(EtfDailyBarRevision.trade_date > position)
            rows = session.execute(
                statement.order_by(EtfDailyBarRevision.trade_date).limit(request.limit + 1)
            ).all()
            identifier = _current_etf_identifier(session, etf_id=etf_id)
        visible = rows[: request.limit]
        next_position = (
            visible[-1][0].trade_date.isoformat() if len(rows) > request.limit and visible else None
        )
        return _etf_page(
            publication=publication,
            rows=visible,
            data_version=UUID(str(publication.data_version)),
            etf_id=etf_id,
            identifier=identifier,
            selected_fields=request.fields,
            start=start,
            end=end,
            next_position=next_position,
            kind="bar",
        )

    def _query_etf_navs(
        self, *, request: MarketDataQuery, after: str | None
    ) -> MarketDataQueryPage:
        """读取已发布 ETF 单位/累计 NAV，日期和 NAV 类型共同构成稳定续页位置。"""
        descriptor = _descriptor(request)
        self._catalog._assert_contract(descriptor, request)
        if request.business_scope not in {"ETF", "FUND"}:
            raise MarketDataRequestValidationError("ETF NAVs require ETF or FUND scope")
        etf_id = _entity_filter(request, field="etfEntityRef", label="ETF NAVs")
        start, end = _trade_date_range(request, date_field="navDate")
        position = _after_nav_position(after)
        with self._database.session() as session:
            publication = _select_entity_publication(
                session, request=request, dataset=_ETF_NAV, entity_id=etf_id
            )
            if publication is None or publication.release_id is None:
                raise MarketDataAccessUnavailable("ETF NAV publication is unavailable")
            statement = (
                select(EtfNavRevision, SourceBatch.provider_id)
                .join(SourceBatch, SourceBatch.source_batch_id == EtfNavRevision.source_batch_id)
                .join(DatasetRelease, DatasetRelease.release_id == publication.release_id)
                .where(
                    EtfNavRevision.etf_id == etf_id,
                    EtfNavRevision.methodology_version_id == DatasetRelease.methodology_version_id,
                    EtfNavRevision.known_from <= publication.knowledge_cutoff,
                    or_(
                        EtfNavRevision.known_to.is_(None),
                        EtfNavRevision.known_to > publication.knowledge_cutoff,
                    ),
                    EtfNavRevision.nav_date >= start,
                    EtfNavRevision.nav_date <= end,
                )
            )
            if position is not None:
                statement = statement.where(
                    (EtfNavRevision.nav_date > position[0])
                    | (
                        (EtfNavRevision.nav_date == position[0])
                        & (EtfNavRevision.nav_kind > position[1])
                    )
                )
            rows = session.execute(
                statement.order_by(EtfNavRevision.nav_date, EtfNavRevision.nav_kind).limit(
                    request.limit + 1
                )
            ).all()
            identifier = _current_etf_identifier(session, etf_id=etf_id)
        visible = rows[: request.limit]
        next_position = (
            f"{visible[-1][0].nav_date.isoformat()}|{visible[-1][0].nav_kind}"
            if len(rows) > request.limit and visible
            else None
        )
        return _etf_page(
            publication=publication,
            rows=visible,
            data_version=UUID(str(publication.data_version)),
            etf_id=etf_id,
            identifier=identifier,
            selected_fields=request.fields,
            start=start,
            end=end,
            next_position=next_position,
            kind="nav",
        )

    def _query_etf_statuses(
        self, *, request: MarketDataQuery, after: str | None
    ) -> MarketDataQueryPage:
        """读取 ETF 三个独立状态维度，交易停牌不会被解释为申购或赎回状态。"""
        descriptor = _descriptor(request)
        self._catalog._assert_contract(descriptor, request)
        if request.business_scope not in {"ETF", "FUND"}:
            raise MarketDataRequestValidationError("ETF statuses require ETF or FUND scope")
        etf_id = _entity_filter(request, field="etfEntityRef", label="ETF statuses")
        start, end = _date_range(request, dimension="EFFECTIVE_AT", label="ETF statuses")
        position = _after_status_position(after)
        with self._database.session() as session:
            publication = _select_entity_publication(
                session, request=request, dataset=_ETF_STATUS, entity_id=etf_id
            )
            if publication is None or publication.release_id is None:
                raise MarketDataAccessUnavailable("ETF status publication is unavailable")
            statement = (
                select(EtfStatusRevision, SourceBatch.provider_id)
                .join(SourceBatch, SourceBatch.source_batch_id == EtfStatusRevision.source_batch_id)
                .join(DatasetRelease, DatasetRelease.release_id == publication.release_id)
                .where(
                    EtfStatusRevision.etf_id == etf_id,
                    EtfStatusRevision.methodology_version_id
                    == DatasetRelease.methodology_version_id,
                    EtfStatusRevision.known_from <= publication.knowledge_cutoff,
                    or_(
                        EtfStatusRevision.known_to.is_(None),
                        EtfStatusRevision.known_to > publication.knowledge_cutoff,
                    ),
                    EtfStatusRevision.effective_from <= end,
                    or_(
                        EtfStatusRevision.effective_to.is_(None),
                        EtfStatusRevision.effective_to > start,
                    ),
                )
            )
            if position is not None:
                statement = statement.where(
                    (EtfStatusRevision.effective_from > position[0])
                    | (
                        (EtfStatusRevision.effective_from == position[0])
                        & (EtfStatusRevision.status_dimension > position[1])
                    )
                )
            rows = session.execute(
                statement.order_by(
                    EtfStatusRevision.effective_from,
                    EtfStatusRevision.status_dimension,
                ).limit(request.limit + 1)
            ).all()
            identifier = _current_etf_identifier(session, etf_id=etf_id)
        visible = rows[: request.limit]
        return _revision_page(
            publication=publication,
            items=tuple(
                _etf_status_record(
                    row=row,
                    provider_id=provider_id,
                    data_version=UUID(str(publication.data_version)),
                    etf_id=etf_id,
                    identifier=identifier,
                    selected_fields=request.fields,
                )
                for row, provider_id in visible
            ),
            provider_ids=tuple(provider_id for _row, provider_id in visible),
            start=start,
            end=end,
            next_position=(
                f"{visible[-1][0].effective_from.isoformat()}|{visible[-1][0].status_dimension}"
                if len(rows) > request.limit and visible
                else None
            ),
            methodology_code="etf-trading-state-reported",
            source_dataset="ETF 日级交易、申购和赎回状态",
        )

    def _query_etf_profiles(
        self, *, request: MarketDataQuery, after: str | None
    ) -> MarketDataQueryPage:
        """读取一个场所目录 publication 内的 ETF 产品资料，目录差集不会被推断为退市。"""
        descriptor = _descriptor(request)
        self._catalog._assert_contract(descriptor, request)
        if request.business_scope not in {"ETF", "FUND"}:
            raise MarketDataRequestValidationError("ETF profiles require ETF or FUND scope")
        exchange = _exact_text_filter(request, field="exchange", allowed={"SSE", "SZSE"})
        etf_ids = _optional_uuid_filter_values(request, field="etfEntityRef")
        start, end = _date_range(request, dimension="EFFECTIVE_AT", label="ETF profiles")
        position = _after_date_uuid_position(after, label="ETF profile")
        with self._database.session() as session:
            publication = _select_partition_publication(
                session,
                request=request,
                dataset=_ETF_PROFILE,
                partition_key=f"venue:{exchange}",
            )
            if publication is None or publication.release_id is None:
                raise MarketDataAccessUnavailable("ETF profile publication is unavailable")
            statement = (
                select(
                    EtfProfileVersion,
                    TradingVenue.code,
                    InstrumentIdentifierVersion.identifier_value,
                    SourceBatch.provider_id,
                )
                .join(EtfListing, EtfListing.instrument_id == EtfProfileVersion.etf_id)
                .join(TradingVenue, TradingVenue.venue_id == EtfListing.venue_id)
                .outerjoin(
                    InstrumentIdentifierVersion,
                    (InstrumentIdentifierVersion.entity_id == EtfProfileVersion.etf_id)
                    & (InstrumentIdentifierVersion.identifier_scheme == "venue_symbol")
                    & (InstrumentIdentifierVersion.known_from <= publication.knowledge_cutoff)
                    & (
                        InstrumentIdentifierVersion.known_to.is_(None)
                        | (InstrumentIdentifierVersion.known_to > publication.knowledge_cutoff)
                    )
                    & (
                        InstrumentIdentifierVersion.effective_from
                        <= EtfProfileVersion.effective_from
                    )
                    & (
                        InstrumentIdentifierVersion.effective_to.is_(None)
                        | (
                            InstrumentIdentifierVersion.effective_to
                            > EtfProfileVersion.effective_from
                        )
                    ),
                )
                .join(SourceBatch, SourceBatch.source_batch_id == EtfProfileVersion.source_batch_id)
                .join(DatasetRelease, DatasetRelease.release_id == publication.release_id)
                .where(
                    TradingVenue.code == exchange,
                    EtfProfileVersion.methodology_version_id
                    == DatasetRelease.methodology_version_id,
                    EtfProfileVersion.known_from <= publication.knowledge_cutoff,
                    or_(
                        EtfProfileVersion.known_to.is_(None),
                        EtfProfileVersion.known_to > publication.knowledge_cutoff,
                    ),
                    EtfProfileVersion.effective_from <= end,
                    or_(
                        EtfProfileVersion.effective_to.is_(None),
                        EtfProfileVersion.effective_to > start,
                    ),
                )
            )
            if etf_ids:
                statement = statement.where(EtfProfileVersion.etf_id.in_(etf_ids))
            if position is not None:
                statement = statement.where(
                    (EtfProfileVersion.effective_from > position[0])
                    | (
                        (EtfProfileVersion.effective_from == position[0])
                        & (EtfProfileVersion.profile_version_id > position[1])
                    )
                )
            rows = session.execute(
                statement.order_by(
                    EtfProfileVersion.effective_from,
                    EtfProfileVersion.profile_version_id,
                ).limit(request.limit + 1)
            ).all()
        visible = rows[: request.limit]
        return _revision_page(
            publication=publication,
            items=tuple(
                _etf_profile_record(
                    row=row,
                    exchange=venue_code,
                    symbol=str(symbol) if symbol is not None else None,
                    provider_id=provider_id,
                    data_version=UUID(str(publication.data_version)),
                    selected_fields=request.fields,
                )
                for row, venue_code, symbol, provider_id in visible
            ),
            provider_ids=tuple(provider_id for _row, _venue_code, _symbol, provider_id in visible),
            start=start,
            end=end,
            next_position=(
                f"{visible[-1][0].effective_from.isoformat()}|{visible[-1][0].profile_version_id}"
                if len(rows) > request.limit and visible
                else None
            ),
            methodology_code="etf-profile-reported",
            source_dataset="ETF 产品资料与上市生命周期",
        )

    def _query_margin_market_daily(
        self, *, request: MarketDataQuery, after: str | None
    ) -> MarketDataQueryPage:
        """读取固定场所 publication 内的两融市场汇总，禁止用证券明细聚合替代。"""
        descriptor = _descriptor(request)
        self._catalog._assert_contract(descriptor, request)
        if request.business_scope != "MARKET":
            raise MarketDataRequestValidationError("margin market daily requires MARKET scope")
        venue_id = _entity_filter(request, field="venueEntityRef", label="margin market daily")
        start, end = _trade_date_range(request)
        position = _after_trade_date(after)
        with self._database.session() as session:
            publication = _select_partition_publication(
                session,
                request=request,
                dataset=_MARGIN_MARKET,
                partition_key=_venue_partition_key(venue_id),
            )
            if publication is None or publication.release_id is None:
                raise MarketDataAccessUnavailable("margin market publication is unavailable")
            statement = (
                select(MarginMarketDailyRevision, SourceBatch.provider_id)
                .join(
                    SourceBatch,
                    SourceBatch.source_batch_id == MarginMarketDailyRevision.source_batch_id,
                )
                .join(DatasetRelease, DatasetRelease.release_id == publication.release_id)
                .where(
                    MarginMarketDailyRevision.venue_id == venue_id,
                    MarginMarketDailyRevision.methodology_version_id
                    == DatasetRelease.methodology_version_id,
                    MarginMarketDailyRevision.known_from <= publication.knowledge_cutoff,
                    or_(
                        MarginMarketDailyRevision.known_to.is_(None),
                        MarginMarketDailyRevision.known_to > publication.knowledge_cutoff,
                    ),
                    MarginMarketDailyRevision.trade_date >= start,
                    MarginMarketDailyRevision.trade_date <= end,
                )
            )
            if position is not None:
                statement = statement.where(MarginMarketDailyRevision.trade_date > position)
            rows = session.execute(
                statement.order_by(MarginMarketDailyRevision.trade_date).limit(request.limit + 1)
            ).all()
        visible = rows[: request.limit]
        return _revision_page(
            publication=publication,
            items=tuple(
                _margin_market_record(
                    row=row,
                    provider_id=provider_id,
                    data_version=UUID(str(publication.data_version)),
                    venue_id=venue_id,
                    selected_fields=request.fields,
                )
                for row, provider_id in visible
            ),
            provider_ids=tuple(provider_id for _row, provider_id in visible),
            start=start,
            end=end,
            next_position=(
                visible[-1][0].trade_date.isoformat()
                if len(rows) > request.limit and visible
                else None
            ),
            methodology_code="margin-venue-daily-reported",
            source_dataset="两融市场汇总",
        )

    def _query_margin_security_daily(
        self, *, request: MarketDataQuery, after: str | None
    ) -> MarketDataQueryPage:
        """读取场所分区内的两融证券直报明细，派生偿还值不会进入 P0 投影。"""
        descriptor = _descriptor(request)
        self._catalog._assert_contract(descriptor, request)
        if request.business_scope != "SECURITY":
            raise MarketDataRequestValidationError("margin security daily requires SECURITY scope")
        venue_id = _entity_filter(request, field="venueEntityRef", label="margin security daily")
        instrument_ids = _optional_uuid_filter_values(request, field="equityEntityRef")
        start, end = _trade_date_range(request)
        position = _after_security_trade_position(after)
        with self._database.session() as session:
            publication = _select_partition_publication(
                session,
                request=request,
                dataset=_MARGIN_SECURITY,
                partition_key=_venue_partition_key(venue_id),
            )
            if publication is None or publication.release_id is None:
                raise MarketDataAccessUnavailable("margin security publication is unavailable")
            venue_code = _venue_code(session, venue_id=venue_id)
            statement = (
                select(
                    MarginSecurityDailyRevision,
                    EquityInstrument.instrument_id,
                    SourceBatch.provider_id,
                )
                .join(
                    EquityInstrument,
                    EquityInstrument.security_id == MarginSecurityDailyRevision.security_id,
                )
                .join(
                    SourceBatch,
                    SourceBatch.source_batch_id == MarginSecurityDailyRevision.source_batch_id,
                )
                .join(DatasetRelease, DatasetRelease.release_id == publication.release_id)
                .where(
                    EquityInstrument.exchange == venue_code,
                    MarginSecurityDailyRevision.methodology_version_id
                    == DatasetRelease.methodology_version_id,
                    MarginSecurityDailyRevision.known_from <= publication.knowledge_cutoff,
                    or_(
                        MarginSecurityDailyRevision.known_to.is_(None),
                        MarginSecurityDailyRevision.known_to > publication.knowledge_cutoff,
                    ),
                    MarginSecurityDailyRevision.trade_date >= start,
                    MarginSecurityDailyRevision.trade_date <= end,
                )
            )
            if instrument_ids:
                statement = statement.where(EquityInstrument.instrument_id.in_(instrument_ids))
            if position is not None:
                statement = statement.where(
                    (MarginSecurityDailyRevision.trade_date > position[0])
                    | (
                        (MarginSecurityDailyRevision.trade_date == position[0])
                        & (MarginSecurityDailyRevision.security_id > position[1])
                    )
                )
            rows = session.execute(
                statement.order_by(
                    MarginSecurityDailyRevision.trade_date,
                    MarginSecurityDailyRevision.security_id,
                ).limit(request.limit + 1)
            ).all()
        visible = rows[: request.limit]
        return _revision_page(
            publication=publication,
            items=tuple(
                _margin_security_record(
                    row=row,
                    instrument_id=instrument_id,
                    provider_id=provider_id,
                    data_version=UUID(str(publication.data_version)),
                    selected_fields=request.fields,
                )
                for row, instrument_id, provider_id in visible
            ),
            provider_ids=tuple(provider_id for _row, _instrument_id, provider_id in visible),
            start=start,
            end=end,
            next_position=(
                f"{visible[-1][0].trade_date.isoformat()}|{visible[-1][0].security_id}"
                if len(rows) > request.limit and visible
                else None
            ),
            methodology_code="margin-security-daily-reported",
            source_dataset="两融证券日明细",
        )

    def _query_margin_eligibility(
        self, *, request: MarketDataQuery, after: str | None
    ) -> MarketDataQueryPage:
        """读取场所分区的两融资格知识版本，不把当前目录缺席解释为历史撤销。"""
        descriptor = _descriptor(request)
        self._catalog._assert_contract(descriptor, request)
        if request.business_scope != "SECURITY":
            raise MarketDataRequestValidationError("margin eligibility requires SECURITY scope")
        venue_id = _entity_filter(request, field="venueEntityRef", label="margin eligibility")
        instrument_ids = _optional_uuid_filter_values(request, field="equityEntityRef")
        start, end = _date_range(request, dimension="EFFECTIVE_AT", label="margin eligibility")
        position = _after_security_trade_position(after)
        with self._database.session() as session:
            publication = _select_partition_publication(
                session,
                request=request,
                dataset=_MARGIN_ELIGIBILITY,
                partition_key=_venue_partition_key(venue_id),
            )
            if publication is None or publication.release_id is None:
                raise MarketDataAccessUnavailable("margin eligibility publication is unavailable")
            venue_code = _venue_code(session, venue_id=venue_id)
            statement = (
                select(
                    MarginEligibilityRevision,
                    EquityInstrument.instrument_id,
                    SourceBatch.provider_id,
                )
                .join(
                    EquityInstrument,
                    EquityInstrument.security_id == MarginEligibilityRevision.security_id,
                )
                .join(
                    SourceBatch,
                    SourceBatch.source_batch_id == MarginEligibilityRevision.source_batch_id,
                )
                .join(DatasetRelease, DatasetRelease.release_id == publication.release_id)
                .where(
                    EquityInstrument.exchange == venue_code,
                    MarginEligibilityRevision.methodology_version_id
                    == DatasetRelease.methodology_version_id,
                    MarginEligibilityRevision.known_from <= publication.knowledge_cutoff,
                    or_(
                        MarginEligibilityRevision.known_to.is_(None),
                        MarginEligibilityRevision.known_to > publication.knowledge_cutoff,
                    ),
                    MarginEligibilityRevision.effective_from <= end,
                    or_(
                        MarginEligibilityRevision.effective_to.is_(None),
                        MarginEligibilityRevision.effective_to > start,
                    ),
                )
            )
            if instrument_ids:
                statement = statement.where(EquityInstrument.instrument_id.in_(instrument_ids))
            if position is not None:
                statement = statement.where(
                    (MarginEligibilityRevision.effective_from > position[0])
                    | (
                        (MarginEligibilityRevision.effective_from == position[0])
                        & (MarginEligibilityRevision.security_id > position[1])
                    )
                )
            rows = session.execute(
                statement.order_by(
                    MarginEligibilityRevision.effective_from,
                    MarginEligibilityRevision.security_id,
                ).limit(request.limit + 1)
            ).all()
        visible = rows[: request.limit]
        return _revision_page(
            publication=publication,
            items=tuple(
                _margin_eligibility_record(
                    row=row,
                    instrument_id=instrument_id,
                    provider_id=provider_id,
                    data_version=UUID(str(publication.data_version)),
                    selected_fields=request.fields,
                )
                for row, instrument_id, provider_id in visible
            ),
            provider_ids=tuple(provider_id for _row, _instrument_id, provider_id in visible),
            start=start,
            end=end,
            next_position=(
                f"{visible[-1][0].effective_from.isoformat()}|{visible[-1][0].security_id}"
                if len(rows) > request.limit and visible
                else None
            ),
            methodology_code="margin-eligibility-reported",
            source_dataset="两融资格",
        )

    def _query_stock_connect_market_daily(
        self, *, request: MarketDataQuery, after: str | None
    ) -> MarketDataQueryPage:
        """读取一个通道方向的市场统计发布，制度性未披露字段保持空值。"""
        descriptor = _descriptor(request)
        self._catalog._assert_contract(descriptor, request)
        if request.business_scope != "CHANNEL":
            raise MarketDataRequestValidationError(
                "stock-connect market daily requires CHANNEL scope"
            )
        channel, direction = _channel_filters(request)
        start, end = _trade_date_range(request)
        position = _after_trade_date(after)
        with self._database.session() as session:
            publication = _select_partition_publication(
                session,
                request=request,
                dataset=_STOCK_CONNECT_MARKET,
                partition_key=_channel_partition_key(channel, direction),
            )
            if publication is None or publication.release_id is None:
                raise MarketDataAccessUnavailable("stock-connect market publication is unavailable")
            statement = (
                select(StockConnectChannelDailyRevision, SourceBatch.provider_id)
                .join(
                    SourceBatch,
                    SourceBatch.source_batch_id == StockConnectChannelDailyRevision.source_batch_id,
                )
                .join(DatasetRelease, DatasetRelease.release_id == publication.release_id)
                .where(
                    StockConnectChannelDailyRevision.channel == channel,
                    StockConnectChannelDailyRevision.direction == direction,
                    StockConnectChannelDailyRevision.methodology_version_id
                    == DatasetRelease.methodology_version_id,
                    StockConnectChannelDailyRevision.known_from <= publication.knowledge_cutoff,
                    or_(
                        StockConnectChannelDailyRevision.known_to.is_(None),
                        StockConnectChannelDailyRevision.known_to > publication.knowledge_cutoff,
                    ),
                    StockConnectChannelDailyRevision.trade_date >= start,
                    StockConnectChannelDailyRevision.trade_date <= end,
                )
            )
            if position is not None:
                statement = statement.where(StockConnectChannelDailyRevision.trade_date > position)
            rows = session.execute(
                statement.order_by(StockConnectChannelDailyRevision.trade_date).limit(
                    request.limit + 1
                )
            ).all()
        visible = rows[: request.limit]
        return _revision_page(
            publication=publication,
            items=tuple(
                _stock_connect_market_record(
                    row=row,
                    provider_id=provider_id,
                    data_version=UUID(str(publication.data_version)),
                    selected_fields=request.fields,
                )
                for row, provider_id in visible
            ),
            provider_ids=tuple(provider_id for _row, provider_id in visible),
            start=start,
            end=end,
            next_position=(
                visible[-1][0].trade_date.isoformat()
                if len(rows) > request.limit and visible
                else None
            ),
            methodology_code="stock-connect-channel-reported",
            source_dataset="沪深港通通道日终统计",
        )

    def _query_stock_connect_active_security(
        self, *, request: MarketDataQuery, after: str | None
    ) -> MarketDataQueryPage:
        """读取一个通道方向的活跃榜发布，榜单与市场统计维持独立 publication。"""
        descriptor = _descriptor(request)
        self._catalog._assert_contract(descriptor, request)
        if request.business_scope != "CHANNEL":
            raise MarketDataRequestValidationError(
                "stock-connect active security requires CHANNEL scope"
            )
        channel, direction = _channel_filters(request)
        instrument_ids = _optional_uuid_filter_values(request, field="instrumentEntityRef")
        start, end = _trade_date_range(request)
        position = _after_rank_position(after)
        with self._database.session() as session:
            publication = _select_partition_publication(
                session,
                request=request,
                dataset=_STOCK_CONNECT_ACTIVE,
                partition_key=_channel_partition_key(channel, direction),
            )
            if publication is None or publication.release_id is None:
                raise MarketDataAccessUnavailable(
                    "stock-connect active-security publication is unavailable"
                )
            statement = (
                select(StockConnectActiveSecurityRevision, SourceBatch.provider_id)
                .join(
                    SourceBatch,
                    SourceBatch.source_batch_id
                    == StockConnectActiveSecurityRevision.source_batch_id,
                )
                .join(DatasetRelease, DatasetRelease.release_id == publication.release_id)
                .where(
                    StockConnectActiveSecurityRevision.channel == channel,
                    StockConnectActiveSecurityRevision.direction == direction,
                    StockConnectActiveSecurityRevision.methodology_version_id
                    == DatasetRelease.methodology_version_id,
                    StockConnectActiveSecurityRevision.known_from <= publication.knowledge_cutoff,
                    or_(
                        StockConnectActiveSecurityRevision.known_to.is_(None),
                        StockConnectActiveSecurityRevision.known_to > publication.knowledge_cutoff,
                    ),
                    StockConnectActiveSecurityRevision.trade_date >= start,
                    StockConnectActiveSecurityRevision.trade_date <= end,
                )
            )
            if instrument_ids:
                statement = statement.where(
                    StockConnectActiveSecurityRevision.instrument_id.in_(instrument_ids)
                )
            if position is not None:
                statement = statement.where(
                    (StockConnectActiveSecurityRevision.trade_date > position[0])
                    | (
                        (StockConnectActiveSecurityRevision.trade_date == position[0])
                        & (StockConnectActiveSecurityRevision.rank_no > position[1])
                    )
                )
            rows = session.execute(
                statement.order_by(
                    StockConnectActiveSecurityRevision.trade_date,
                    StockConnectActiveSecurityRevision.rank_no,
                ).limit(request.limit + 1)
            ).all()
        visible = rows[: request.limit]
        return _revision_page(
            publication=publication,
            items=tuple(
                _stock_connect_active_record(
                    row=row,
                    provider_id=provider_id,
                    data_version=UUID(str(publication.data_version)),
                    selected_fields=request.fields,
                )
                for row, provider_id in visible
            ),
            provider_ids=tuple(provider_id for _row, provider_id in visible),
            start=start,
            end=end,
            next_position=(
                f"{visible[-1][0].trade_date.isoformat()}|{visible[-1][0].rank_no}"
                if len(rows) > request.limit and visible
                else None
            ),
            methodology_code="stock-connect-active-security-reported",
            source_dataset="沪深港通活跃证券榜",
        )

    def _query_dragon_tiger(
        self, *, request: MarketDataQuery, after: str | None
    ) -> MarketDataQueryPage:
        """读取唯一合格来源分区的龙虎榜事件，席位明细不会被扁平化成未知字段。"""
        descriptor = _descriptor(request)
        self._catalog._assert_contract(descriptor, request)
        if request.business_scope != "EVENT":
            raise MarketDataRequestValidationError("dragon-tiger events require EVENT scope")
        instrument_ids = _optional_uuid_filter_values(request, field="equityEntityRef")
        reason_codes = _optional_text_filter_values(request, field="reasonCode")
        start, end = _trade_date_range(request)
        position = _after_date_uuid_position(after, label="dragon-tiger")
        with self._database.session() as session:
            publication = _select_single_partition_publication(
                session, request=request, dataset=_DRAGON_TIGER
            )
            if publication is None or publication.release_id is None:
                raise MarketDataAccessUnavailable("dragon-tiger publication is unavailable")
            statement = (
                select(
                    DragonTigerEventRevision,
                    EquityInstrument.instrument_id,
                    SourceBatch.provider_id,
                )
                .join(
                    EquityInstrument,
                    EquityInstrument.security_id == DragonTigerEventRevision.security_id,
                )
                .join(
                    SourceBatch,
                    SourceBatch.source_batch_id == DragonTigerEventRevision.source_batch_id,
                )
                .join(DatasetRelease, DatasetRelease.release_id == publication.release_id)
                .where(
                    DragonTigerEventRevision.methodology_version_id
                    == DatasetRelease.methodology_version_id,
                    DragonTigerEventRevision.known_from <= publication.knowledge_cutoff,
                    or_(
                        DragonTigerEventRevision.known_to.is_(None),
                        DragonTigerEventRevision.known_to > publication.knowledge_cutoff,
                    ),
                    DragonTigerEventRevision.trade_date >= start,
                    DragonTigerEventRevision.trade_date <= end,
                )
            )
            if instrument_ids:
                statement = statement.where(EquityInstrument.instrument_id.in_(instrument_ids))
            if reason_codes:
                statement = statement.where(DragonTigerEventRevision.reason_code.in_(reason_codes))
            if position is not None:
                statement = statement.where(
                    (DragonTigerEventRevision.trade_date > position[0])
                    | (
                        (DragonTigerEventRevision.trade_date == position[0])
                        & (DragonTigerEventRevision.event_revision_id > position[1])
                    )
                )
            rows = session.execute(
                statement.order_by(
                    DragonTigerEventRevision.trade_date,
                    DragonTigerEventRevision.event_revision_id,
                ).limit(request.limit + 1)
            ).all()
        visible = rows[: request.limit]
        return _revision_page(
            publication=publication,
            items=tuple(
                _dragon_tiger_record(
                    row=row,
                    instrument_id=instrument_id,
                    provider_id=provider_id,
                    data_version=UUID(str(publication.data_version)),
                    selected_fields=request.fields,
                )
                for row, instrument_id, provider_id in visible
            ),
            provider_ids=tuple(provider_id for _row, _instrument_id, provider_id in visible),
            start=start,
            end=end,
            next_position=(
                f"{visible[-1][0].trade_date.isoformat()}|{visible[-1][0].event_revision_id}"
                if len(rows) > request.limit and visible
                else None
            ),
            methodology_code="dragon-tiger-disclosure-reported",
            source_dataset="龙虎榜公开交易信息",
        )

    def _query_block_trades(
        self, *, request: MarketDataQuery, after: str | None
    ) -> MarketDataQueryPage:
        """读取唯一合格来源分区的大宗逐笔成交，合法重复经济成交依靠 occurrence 保留。"""
        descriptor = _descriptor(request)
        self._catalog._assert_contract(descriptor, request)
        if request.business_scope != "EVENT":
            raise MarketDataRequestValidationError("block trades require EVENT scope")
        instrument_ids = _optional_uuid_filter_values(request, field="equityEntityRef")
        start, end = _trade_date_range(request)
        position = _after_date_uuid_position(after, label="block-trade")
        with self._database.session() as session:
            publication = _select_single_partition_publication(
                session, request=request, dataset=_BLOCK_TRADE
            )
            if publication is None or publication.release_id is None:
                raise MarketDataAccessUnavailable("block-trade publication is unavailable")
            statement = (
                select(
                    BlockTradeExecutionRevision,
                    EquityInstrument.instrument_id,
                    SourceBatch.provider_id,
                )
                .join(
                    EquityInstrument,
                    EquityInstrument.security_id == BlockTradeExecutionRevision.security_id,
                )
                .join(
                    SourceBatch,
                    SourceBatch.source_batch_id == BlockTradeExecutionRevision.source_batch_id,
                )
                .join(DatasetRelease, DatasetRelease.release_id == publication.release_id)
                .where(
                    BlockTradeExecutionRevision.methodology_version_id
                    == DatasetRelease.methodology_version_id,
                    BlockTradeExecutionRevision.known_from <= publication.knowledge_cutoff,
                    or_(
                        BlockTradeExecutionRevision.known_to.is_(None),
                        BlockTradeExecutionRevision.known_to > publication.knowledge_cutoff,
                    ),
                    BlockTradeExecutionRevision.trade_date >= start,
                    BlockTradeExecutionRevision.trade_date <= end,
                )
            )
            if instrument_ids:
                statement = statement.where(EquityInstrument.instrument_id.in_(instrument_ids))
            if position is not None:
                statement = statement.where(
                    (BlockTradeExecutionRevision.trade_date > position[0])
                    | (
                        (BlockTradeExecutionRevision.trade_date == position[0])
                        & (BlockTradeExecutionRevision.execution_revision_id > position[1])
                    )
                )
            rows = session.execute(
                statement.order_by(
                    BlockTradeExecutionRevision.trade_date,
                    BlockTradeExecutionRevision.execution_revision_id,
                ).limit(request.limit + 1)
            ).all()
        visible = rows[: request.limit]
        return _revision_page(
            publication=publication,
            items=tuple(
                _block_trade_record(
                    row=row,
                    instrument_id=instrument_id,
                    provider_id=provider_id,
                    data_version=UUID(str(publication.data_version)),
                    selected_fields=request.fields,
                )
                for row, instrument_id, provider_id in visible
            ),
            provider_ids=tuple(provider_id for _row, _instrument_id, provider_id in visible),
            start=start,
            end=end,
            next_position=(
                f"{visible[-1][0].trade_date.isoformat()}|{visible[-1][0].execution_revision_id}"
                if len(rows) > request.limit and visible
                else None
            ),
            methodology_code="block-trade-execution-reported",
            source_dataset="大宗交易逐笔成交",
        )

    def _query_corporate_earnings(
        self, *, request: MarketDataQuery, after: str | None
    ) -> MarketDataQueryPage:
        """读取唯一合格来源分区的业绩预告或快报事件，文档日期与事件状态均来自同一证据链。"""
        descriptor = _descriptor(request)
        self._catalog._assert_contract(descriptor, request)
        if request.business_scope != "EVENT":
            raise MarketDataRequestValidationError("corporate earnings events require EVENT scope")
        instrument_ids = _optional_uuid_filter_values(request, field="equityEntityRef")
        event_kinds = _optional_text_filter_values(request, field="eventKind")
        start, end = _date_range(request, dimension="EVENT_DATE", label="corporate earnings")
        position = _after_date_uuid_position(after, label="corporate-earnings")
        with self._database.session() as session:
            publication = _select_single_partition_publication(
                session, request=request, dataset=_CORPORATE_EARNINGS
            )
            if publication is None or publication.release_id is None:
                raise MarketDataAccessUnavailable("corporate earnings publication is unavailable")
            statement = (
                select(
                    CorporateEventRevision,
                    CorporateEvent,
                    DisclosureDocument,
                    EquityInstrument.instrument_id,
                    SourceBatch.provider_id,
                )
                .join(CorporateEvent, CorporateEvent.event_id == CorporateEventRevision.event_id)
                .join(
                    DisclosureDocument,
                    DisclosureDocument.document_id == CorporateEventRevision.primary_document_id,
                )
                .join(EquityInstrument, EquityInstrument.security_id == CorporateEvent.security_id)
                .join(
                    SourceBatch,
                    SourceBatch.source_batch_id == CorporateEventRevision.source_batch_id,
                )
                .join(DatasetRelease, DatasetRelease.release_id == publication.release_id)
                .where(
                    CorporateEventRevision.methodology_version_id
                    == DatasetRelease.methodology_version_id,
                    CorporateEventRevision.known_from <= publication.knowledge_cutoff,
                    or_(
                        CorporateEventRevision.known_to.is_(None),
                        CorporateEventRevision.known_to > publication.knowledge_cutoff,
                    ),
                    DisclosureDocument.announced_on >= start,
                    DisclosureDocument.announced_on <= end,
                )
            )
            if instrument_ids:
                statement = statement.where(EquityInstrument.instrument_id.in_(instrument_ids))
            if event_kinds:
                statement = statement.where(CorporateEvent.event_family.in_(event_kinds))
            if position is not None:
                statement = statement.where(
                    (DisclosureDocument.announced_on > position[0])
                    | (
                        (DisclosureDocument.announced_on == position[0])
                        & (CorporateEventRevision.event_revision_id > position[1])
                    )
                )
            rows = session.execute(
                statement.order_by(
                    DisclosureDocument.announced_on,
                    CorporateEventRevision.event_revision_id,
                ).limit(request.limit + 1)
            ).all()
        visible = rows[: request.limit]
        return _revision_page(
            publication=publication,
            items=tuple(
                _corporate_earnings_record(
                    revision=revision,
                    event=event,
                    document=document,
                    instrument_id=instrument_id,
                    provider_id=provider_id,
                    data_version=UUID(str(publication.data_version)),
                    selected_fields=request.fields,
                )
                for revision, event, document, instrument_id, provider_id in visible
            ),
            provider_ids=tuple(
                provider_id for _revision, _event, _document, _instrument_id, provider_id in visible
            ),
            start=start,
            end=end,
            next_position=(
                f"{visible[-1][2].announced_on.isoformat()}|{visible[-1][0].event_revision_id}"
                if len(rows) > request.limit and visible
                else None
            ),
            methodology_code="corporate-earnings-disclosure-reported",
            source_dataset="业绩预告与业绩快报公告",
        )


def _contract_filter(request: MarketDataQuery) -> UUID:
    """要求唯一 `contractEntityRef` 精确过滤，避免全市场日线读请求绕过成本和权限边界。"""
    matching = tuple(item for item in request.filters if item.field == "contractEntityRef")
    if (
        len(matching) != 1
        or matching[0].operator not in {"EQ", "IN"}
        or len(matching[0].values) != 1
    ):
        raise MarketDataRequestValidationError(
            "derivative daily bars require one contractEntityRef filter"
        )
    try:
        return UUID(str(matching[0].values[0]))
    except ValueError as error:
        raise MarketDataRequestValidationError("contractEntityRef must be a UUID") from error


def _descriptor(request: MarketDataQuery) -> MarketDataDatasetDescriptor:
    """读取请求精确 schema 对应的目录项，未登记 dataset 不能绕过 typed 字段白名单。"""
    descriptor = next(
        (
            item
            for item in default_market_data_descriptors()
            if item.code == request.dataset_code and item.schema_version == request.schema_version
        ),
        None,
    )
    if descriptor is None:
        raise MarketDataDatasetNotFound(request.dataset_code)
    return descriptor


def _entity_filter(request: MarketDataQuery, *, field: str, label: str) -> UUID:
    """读取唯一实体 UUID 过滤，拒绝全市场查询绕开 dataset 分区和权限边界。"""
    matching = tuple(item for item in request.filters if item.field == field)
    if (
        len(matching) != 1
        or matching[0].operator not in {"EQ", "IN"}
        or len(matching[0].values) != 1
    ):
        raise MarketDataRequestValidationError(f"{label} require one {field} filter")
    try:
        return UUID(str(matching[0].values[0]))
    except ValueError as error:
        raise MarketDataRequestValidationError(f"{field} must be a UUID") from error


def _trade_date_range(
    request: MarketDataQuery, *, date_field: str = "tradeDate"
) -> tuple[date, date]:
    """读取已由 HTTP 层验证的交易日窗口，并再次限制 reader 不接受其它时间维度。"""
    if request.time.get("dimension") != "TRADE_DATE":
        raise MarketDataRequestValidationError(f"{date_field} reader requires TRADE_DATE")
    try:
        start = date.fromisoformat(str(request.time["from"]))
        end = date.fromisoformat(str(request.time["to"]))
    except (KeyError, ValueError) as error:
        raise MarketDataRequestValidationError(f"{date_field} range is invalid") from error
    return start, end


def _after_trade_date(value: str | None) -> date | None:
    """将 HMAC 已验证的续页位置解析为交易日，不允许游标携带 SQL 或复合排序表达式。"""
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise MarketDataRequestValidationError("derivative cursor position is invalid") from error


def _after_nav_position(value: str | None) -> tuple[date, str] | None:
    """解析日期和 NAV 类型构成的 HMAC 已验证游标位置，避免同日两类净值漏页或重页。"""
    if value is None:
        return None
    date_text, separator, nav_kind = value.partition("|")
    if separator != "|" or nav_kind not in {"UNIT", "ACCUMULATED"}:
        raise MarketDataRequestValidationError("ETF NAV cursor position is invalid")
    try:
        return date.fromisoformat(date_text), nav_kind
    except ValueError as error:
        raise MarketDataRequestValidationError("ETF NAV cursor position is invalid") from error


def _after_status_position(value: str | None) -> tuple[date, str] | None:
    """解析 ETF 状态日期和独立维度构成的签名游标，三种状态同日均可续页。"""
    if value is None:
        return None
    date_text, separator, dimension = value.partition("|")
    if separator != "|" or dimension not in {"TRADING", "SUBSCRIPTION", "REDEMPTION"}:
        raise MarketDataRequestValidationError("ETF status cursor position is invalid")
    try:
        return date.fromisoformat(date_text), dimension
    except ValueError as error:
        raise MarketDataRequestValidationError("ETF status cursor position is invalid") from error


def _quality_statuses(request: MarketDataQuery) -> tuple[str, ...]:
    """提取已在合同层校验的质量状态，避免未收窄 JSON 值进入 SQL `IN` 条件。"""
    values = request.selection.get("qualityStatuses")
    if (
        not isinstance(values, (list, tuple))
        or not values
        or not all(isinstance(value, str) for value in values)
    ):
        raise MarketDataRequestValidationError("quality statuses are invalid")
    return tuple(value.lower() for value in values)


def _select_publication(
    session: Session, *, request: MarketDataQuery, contract_id: UUID
) -> DatasetPublication | None:
    """选择唯一 current 或调用方固定的数据版本；合约分区和质量选择必须同时满足。"""
    partition_key = _contract_partition_key(contract_id)
    statement = select(DatasetPublication).where(
        DatasetPublication.dataset == _DERIVATIVE_DAILY_BAR,
        DatasetPublication.partition_key == partition_key,
        DatasetPublication.quality_status.in_(_quality_statuses(request)),
    )
    data_version = request.selection.get("dataVersion")
    if data_version is None:
        statement = statement.where(DatasetPublication.superseded_at.is_(None))
    else:
        statement = statement.where(DatasetPublication.data_version == UUID(str(data_version)))
    return session.execute(statement).scalar_one_or_none()


def _select_entity_publication(
    session: Session, *, request: MarketDataQuery, dataset: str, entity_id: UUID
) -> DatasetPublication | None:
    """选择实体分区的 current 或固定 publication，质量筛选与 dataVersion 必须同时满足。"""
    statement = select(DatasetPublication).where(
        DatasetPublication.dataset == dataset,
        DatasetPublication.partition_key == _etf_partition_key(entity_id),
        DatasetPublication.quality_status.in_(_quality_statuses(request)),
    )
    data_version = request.selection.get("dataVersion")
    if data_version is None:
        statement = statement.where(DatasetPublication.superseded_at.is_(None))
    else:
        statement = statement.where(DatasetPublication.data_version == UUID(str(data_version)))
    return session.execute(statement).scalar_one_or_none()


def _current_identifier(session: Session, *, contract_id: UUID) -> dict[str, str]:
    """读取当前受控合约标识，无法唯一解析时拒绝向消费者返回裸 UUID。"""
    row = session.execute(
        select(
            InstrumentIdentifierVersion.identifier_scheme,
            InstrumentIdentifierVersion.identifier_value,
        ).where(
            InstrumentIdentifierVersion.entity_id == contract_id,
            InstrumentIdentifierVersion.identifier_scheme == "venue_contract_code",
            InstrumentIdentifierVersion.known_to.is_(None),
        )
    ).one_or_none()
    if row is None:
        raise MarketDataAccessUnavailable("derivative contract identifier is unavailable")
    return {"scheme": str(row.identifier_scheme), "value": str(row.identifier_value)}


def _current_etf_identifier(session: Session, *, etf_id: UUID) -> dict[str, str]:
    """读取当前 ETF 受控场所代码；没有唯一代码时不向消费者投影裸 UUID。"""
    row = session.execute(
        select(
            InstrumentIdentifierVersion.identifier_scheme,
            InstrumentIdentifierVersion.identifier_value,
        ).where(
            InstrumentIdentifierVersion.entity_id == etf_id,
            InstrumentIdentifierVersion.identifier_scheme == "venue_symbol",
            InstrumentIdentifierVersion.known_to.is_(None),
        )
    ).one_or_none()
    if row is None:
        raise MarketDataAccessUnavailable("ETF identifier is unavailable")
    return {"scheme": str(row.identifier_scheme), "value": str(row.identifier_value)}


def _record(
    *,
    row: DerivativeDailyBarRevision,
    provider_id: str,
    data_version: UUID,
    contract_id: UUID,
    identifier: dict[str, str],
    selected_fields: tuple[str, ...],
) -> dict[str, object]:
    """投影记录级 canonical 基础字段与已选择业务值，禁止 source batch、数据库键和 raw URI 外泄。"""
    all_values: dict[str, object] = {
        "tradeDate": row.trade_date.isoformat(),
        "contractEntityRef": str(contract_id),
        "close": row.close_price,
        "settlement": row.settlement_price,
    }
    values = {field: all_values[field] for field in selected_fields if field in all_values}
    return {
        "recordRef": f"derivative:{contract_id}:{row.trade_date.isoformat()}:{row.revision_no}",
        "recordType": "DERIVATIVE",
        "entity": {
            "entityRef": str(contract_id),
            "entityType": "FUTURE_CONTRACT",
            "identifiers": [identifier],
        },
        "time": {"tradeDate": row.trade_date.isoformat()},
        "publicUsableAt": _utc(row.public_usable_at),
        "availabilityBasis": row.availability_basis,
        "sourcePublishedAt": _utc_or_none(row.source_published_at),
        "observedAt": _utc(row.known_from),
        "dataVersion": str(data_version),
        "sourceRef": _source_ref(provider_id),
        "methodologyVersion": "1",
        "qualityStatus": row.quality_status.upper(),
        "revision": {"revisionNumber": row.revision_no, "currentInPublication": True},
        "values": values,
    }


def _etf_page(
    *,
    publication: DatasetPublication,
    rows: Sequence[Row[tuple[Any, str]]],
    data_version: UUID,
    etf_id: UUID,
    identifier: dict[str, str],
    selected_fields: tuple[str, ...],
    start: date,
    end: date,
    next_position: str | None,
    kind: str,
) -> MarketDataQueryPage:
    """投影 ETF 日线或 NAV 的统一发布页；业务字段白名单仍由调用前 catalog 校验。"""
    if kind not in {"bar", "nav"}:
        raise AssertionError("unsupported ETF reader kind")
    items = tuple(
        _etf_record(
            row=row,
            provider_id=provider_id,
            data_version=data_version,
            etf_id=etf_id,
            identifier=identifier,
            selected_fields=selected_fields,
            kind=kind,
        )
        for row, provider_id in rows
    )
    source_dataset = "ETF 未复权日线" if kind == "bar" else "ETF 单位/累计 NAV"
    methodology = (
        {"code": "etf-unadjusted-daily-bar", "version": "1", "kind": "REPORTED"}
        if kind == "bar"
        else {"code": "etf-reported-daily-nav", "version": "1", "kind": "REPORTED"}
    )
    return MarketDataQueryPage(
        data_version=data_version,
        published_at=_utc(publication.published_at),
        knowledge_cutoff=_utc(publication.knowledge_cutoff),
        public_usable_at=_utc(publication.published_at),
        quality_status=str(publication.quality_status),
        completeness="COMPLETE",
        items=items,
        next_position=next_position,
        methodology=methodology,
        sources=tuple(
            MarketDataSourceDescriptor(
                source_ref=_source_ref(provider_id),
                publisher="已批准 ETF 来源",
                source_dataset=source_dataset,
                authoritative=True,
                redistribution="INTERNAL_ONLY",
            )
            for provider_id in sorted({provider_id for _row, provider_id in rows})
        )
        or (
            MarketDataSourceDescriptor(
                source_ref="src_approved_etf",
                publisher="已批准 ETF 来源",
                source_dataset=source_dataset,
                authoritative=True,
                redistribution="INTERNAL_ONLY",
            ),
        ),
        coverage={
            "from": start.isoformat(),
            "to": end.isoformat(),
            "pitCoverage": "COMPLETE",
            "gaps": [],
        },
    )


def _etf_record(
    *,
    row: Any,
    provider_id: str,
    data_version: UUID,
    etf_id: UUID,
    identifier: dict[str, str],
    selected_fields: tuple[str, ...],
    kind: str,
) -> dict[str, object]:
    """投影 ETF 强类型 revision，内部 release/source batch/raw 定位永远不暴露给消费者。"""
    if kind == "bar":
        all_values: dict[str, object] = {
            "tradeDate": row.trade_date.isoformat(),
            "etfEntityRef": str(etf_id),
            "close": row.close_price,
            "volume": row.volume_value,
        }
        time = {"tradeDate": row.trade_date.isoformat()}
        record_ref = f"etf-bar:{etf_id}:{row.trade_date.isoformat()}:{row.revision_no}"
    else:
        all_values = {
            "navDate": row.nav_date.isoformat(),
            "etfEntityRef": str(etf_id),
            "navKind": row.nav_kind,
            "nav": row.nav_value,
        }
        time = {"navDate": row.nav_date.isoformat()}
        record_ref = f"etf-nav:{etf_id}:{row.nav_date.isoformat()}:{row.nav_kind}:{row.revision_no}"
    return {
        "recordRef": record_ref,
        "recordType": "ETF",
        "entity": {
            "entityRef": str(etf_id),
            "entityType": "ETF_LISTING",
            "identifiers": [identifier],
        },
        "time": time,
        "publicUsableAt": _utc(row.public_usable_at),
        "availabilityBasis": row.availability_basis,
        "sourcePublishedAt": _utc(row.source_published_at)
        if row.source_published_at is not None
        else None,
        "observedAt": _utc(row.known_from),
        "dataVersion": str(data_version),
        "sourceRef": _source_ref(provider_id),
        "methodologyVersion": "1",
        "qualityStatus": row.quality_status.upper(),
        "revision": {"revisionNumber": row.revision_no, "currentInPublication": True},
        "values": {field: all_values[field] for field in selected_fields if field in all_values},
    }


def _etf_status_record(
    *,
    row: EtfStatusRevision,
    provider_id: str,
    data_version: UUID,
    etf_id: UUID,
    identifier: dict[str, str],
    selected_fields: tuple[str, ...],
) -> dict[str, object]:
    """投影 ETF 状态知识版本；没有可验证公开时间时只使用系统观察时间并明确其依据。"""
    values: dict[str, object] = {
        "etfEntityRef": str(etf_id),
        "stateDimension": row.status_dimension,
        "state": row.status_code,
        "effectiveFrom": row.effective_from.isoformat(),
    }
    return {
        "recordRef": (
            f"etf-status:{etf_id}:{row.status_dimension}:{row.effective_from.isoformat()}:{row.revision_no}"
        ),
        "recordType": "ETF_STATUS",
        "entity": {
            "entityRef": str(etf_id),
            "entityType": "ETF_LISTING",
            "identifiers": [identifier],
        },
        "time": {"effectiveFrom": row.effective_from.isoformat()},
        "publicUsableAt": _utc(row.known_from),
        "availabilityBasis": "OBSERVED_ONLY",
        "sourcePublishedAt": None,
        "observedAt": _utc(row.known_from),
        "dataVersion": str(data_version),
        "sourceRef": _source_ref(provider_id),
        "methodologyVersion": "1",
        "qualityStatus": row.quality_status.upper(),
        "revision": {"revisionNumber": row.revision_no, "currentInPublication": True},
        "values": {field: values[field] for field in selected_fields if field in values},
    }


def _etf_profile_record(
    *,
    row: EtfProfileVersion,
    exchange: str,
    symbol: str | None,
    provider_id: str,
    data_version: UUID,
    selected_fields: tuple[str, ...],
) -> dict[str, object]:
    """投影 ETF 产品资料版本，找不到同一知识截点的代码时显式保留空值而不猜测。"""
    values: dict[str, object] = {
        "etfEntityRef": str(row.etf_id),
        "exchange": exchange,
        "symbol": symbol,
        "listingStatus": row.listing_status,
    }
    return {
        "recordRef": f"etf-profile:{row.profile_version_id}",
        "recordType": "ETF_PROFILE",
        "entity": {"entityRef": str(row.etf_id), "entityType": "ETF_LISTING", "identifiers": []},
        "time": {"effectiveFrom": row.effective_from.isoformat()},
        "publicUsableAt": _utc(row.known_from),
        "availabilityBasis": "OBSERVED_ONLY",
        "sourcePublishedAt": None,
        "observedAt": _utc(row.known_from),
        "dataVersion": str(data_version),
        "sourceRef": _source_ref(provider_id),
        "methodologyVersion": "1",
        "qualityStatus": "PASSED",
        "revision": {"revisionNumber": 1, "currentInPublication": True},
        "values": {field: values[field] for field in selected_fields if field in values},
    }


def _select_partition_publication(
    session: Session,
    *,
    request: MarketDataQuery,
    dataset: str,
    partition_key: str,
) -> DatasetPublication | None:
    """选择一个精确分区的 current 或固定 publication，禁止跨分区拼接当前行。"""
    statement = select(DatasetPublication).where(
        DatasetPublication.dataset == dataset,
        DatasetPublication.partition_key == partition_key,
        DatasetPublication.quality_status.in_(_quality_statuses(request)),
    )
    data_version = request.selection.get("dataVersion")
    if data_version is None:
        statement = statement.where(DatasetPublication.superseded_at.is_(None))
    else:
        statement = statement.where(DatasetPublication.data_version == UUID(str(data_version)))
    return session.execute(statement).scalar_one_or_none()


def _select_single_partition_publication(
    session: Session, *, request: MarketDataQuery, dataset: str
) -> DatasetPublication | None:
    """在尚未公开来源选择器的初始 P0 合同中只允许唯一合格分区被读取。"""
    statement = select(DatasetPublication).where(
        DatasetPublication.dataset == dataset,
        DatasetPublication.quality_status.in_(_quality_statuses(request)),
    )
    data_version = request.selection.get("dataVersion")
    if data_version is None:
        statement = statement.where(DatasetPublication.superseded_at.is_(None))
    else:
        statement = statement.where(DatasetPublication.data_version == UUID(str(data_version)))
    rows = session.execute(statement).scalars().all()
    if len(rows) > 1:
        raise MarketDataAccessUnavailable("dataset has multiple source partitions")
    return rows[0] if rows else None


def _optional_uuid_filter_values(request: MarketDataQuery, *, field: str) -> tuple[UUID, ...]:
    """读取可选的实体 UUID 精确集合，拒绝范围或非 UUID 值绕过分区读取约束。"""
    matching = tuple(item for item in request.filters if item.field == field)
    if not matching:
        return ()
    item = matching[0]
    if item.operator not in {"EQ", "IN"}:
        raise MarketDataRequestValidationError(f"{field} requires EQ or IN")
    try:
        values = tuple(UUID(str(value)) for value in item.values)
    except ValueError as error:
        raise MarketDataRequestValidationError(f"{field} must contain UUID values") from error
    if not values:
        raise MarketDataRequestValidationError(f"{field} must not be empty")
    return values


def _optional_text_filter_values(request: MarketDataQuery, *, field: str) -> tuple[str, ...]:
    """读取可选的受控文本精确集合，不允许范围条件成为来源字段模糊搜索。"""
    matching = tuple(item for item in request.filters if item.field == field)
    if not matching:
        return ()
    item = matching[0]
    if item.operator not in {"EQ", "IN"}:
        raise MarketDataRequestValidationError(f"{field} requires EQ or IN")
    values = tuple(str(value) for value in item.values)
    if not values or any(not value.strip() for value in values):
        raise MarketDataRequestValidationError(f"{field} must contain non-blank values")
    return values


def _exact_text_filter(request: MarketDataQuery, *, field: str, allowed: set[str]) -> str:
    """读取一个必填受控代码过滤器，禁止列表或未知值扩展为跨分区全表读取。"""
    matching = tuple(item for item in request.filters if item.field == field)
    if len(matching) != 1 or matching[0].operator not in {"EQ", "IN"}:
        raise MarketDataRequestValidationError(f"{field} requires one exact value")
    item = matching[0]
    if len(item.values) != 1 or str(item.values[0]) not in allowed:
        raise MarketDataRequestValidationError(f"{field} is invalid")
    return str(item.values[0])


def _date_range(request: MarketDataQuery, *, dimension: str, label: str) -> tuple[date, date]:
    """读取一个指定业务日期维度的有界日期窗口，不接受 timestamp 伪装日期。"""
    if request.time.get("dimension") != dimension:
        raise MarketDataRequestValidationError(f"{label} requires {dimension}")
    try:
        start = date.fromisoformat(str(request.time["from"]))
        end = date.fromisoformat(str(request.time["to"]))
    except (KeyError, ValueError) as error:
        raise MarketDataRequestValidationError(f"{label} range is invalid") from error
    return start, end


def _after_security_trade_position(value: str | None) -> tuple[date, int] | None:
    """解析日期与内部稳定证券键构成的签名续页位置，保证同日多证券不漏页。"""
    if value is None:
        return None
    date_text, separator, security_text = value.partition("|")
    if separator != "|":
        raise MarketDataRequestValidationError("margin cursor position is invalid")
    try:
        security_id = int(security_text)
        if security_id < 1:
            raise ValueError
        return date.fromisoformat(date_text), security_id
    except ValueError as error:
        raise MarketDataRequestValidationError("margin cursor position is invalid") from error


def _after_rank_position(value: str | None) -> tuple[date, int] | None:
    """解析日期与排行位置构成的签名续页位置，避免通道榜单分页重叠或漏行。"""
    if value is None:
        return None
    date_text, separator, rank_text = value.partition("|")
    if separator != "|":
        raise MarketDataRequestValidationError("stock-connect cursor position is invalid")
    try:
        rank_no = int(rank_text)
        if rank_no < 1:
            raise ValueError
        return date.fromisoformat(date_text), rank_no
    except ValueError as error:
        raise MarketDataRequestValidationError(
            "stock-connect cursor position is invalid"
        ) from error


def _after_date_uuid_position(value: str | None, *, label: str) -> tuple[date, UUID] | None:
    """解析事实日期与 immutable revision UUID 组成的签名游标，支持同日多事件稳定续页。"""
    if value is None:
        return None
    date_text, separator, revision_text = value.partition("|")
    if separator != "|":
        raise MarketDataRequestValidationError(f"{label} cursor position is invalid")
    try:
        return date.fromisoformat(date_text), UUID(revision_text)
    except ValueError as error:
        raise MarketDataRequestValidationError(f"{label} cursor position is invalid") from error


def _venue_partition_key(venue_id: UUID) -> str:
    """生成与两融发布器一致的场所分区键，场所展示名称变化不会影响读取版本。"""
    return f"venue:{venue_id}"


def _channel_partition_key(channel: str, direction: str) -> str:
    """生成与港通发布器一致的通道方向分区键，禁止南北向或沪深通道混读。"""
    return f"channel:{channel}:direction:{direction}"


def _venue_code(session: Session, *, venue_id: UUID) -> str:
    """解析场所永久 UUID 到受控代码，未知场所不能退化为全市场证券查询。"""
    value = session.execute(
        select(TradingVenue.code).where(TradingVenue.venue_id == venue_id)
    ).scalar_one_or_none()
    if value is None:
        raise MarketDataAccessUnavailable("margin venue is unavailable")
    return str(value)


def _channel_filters(request: MarketDataQuery) -> tuple[str, str]:
    """读取精确港通通道和方向过滤，拒绝由名称、实体 UUID 或默认方向猜测分区。"""
    values = {
        item.field: item for item in request.filters if item.field in {"channel", "direction"}
    }
    if set(values) != {"channel", "direction"}:
        raise MarketDataRequestValidationError(
            "stock-connect requires channel and direction filters"
        )
    channel_filter = values["channel"]
    direction_filter = values["direction"]
    if (
        channel_filter.operator not in {"EQ", "IN"}
        or len(channel_filter.values) != 1
        or direction_filter.operator not in {"EQ", "IN"}
        or len(direction_filter.values) != 1
    ):
        raise MarketDataRequestValidationError("stock-connect filters must contain one exact value")
    channel = str(channel_filter.values[0])
    direction = str(direction_filter.values[0])
    if channel not in {"SH", "SZ"} or direction not in {"NORTHBOUND", "SOUTHBOUND"}:
        raise MarketDataRequestValidationError("stock-connect channel or direction is invalid")
    return channel, direction


def _revision_page(
    *,
    publication: DatasetPublication,
    items: tuple[dict[str, object], ...],
    provider_ids: tuple[str, ...],
    start: date,
    end: date,
    next_position: str | None,
    methodology_code: str,
    source_dataset: str,
) -> MarketDataQueryPage:
    """构造 revision 型 P0 读取页，版本、知识截点和来源描述只来自同一 publication。"""
    return MarketDataQueryPage(
        data_version=UUID(str(publication.data_version)),
        published_at=_utc(publication.published_at),
        knowledge_cutoff=_utc(publication.knowledge_cutoff),
        public_usable_at=_utc(publication.published_at),
        quality_status=str(publication.quality_status),
        completeness="COMPLETE",
        items=items,
        next_position=next_position,
        methodology={"code": methodology_code, "version": "1", "kind": "REPORTED"},
        sources=tuple(
            MarketDataSourceDescriptor(
                source_ref=_source_ref(provider_id),
                publisher="已批准 P0 来源",
                source_dataset=source_dataset,
                authoritative=True,
                redistribution="INTERNAL_ONLY",
            )
            for provider_id in sorted(set(provider_ids))
        )
        or (
            MarketDataSourceDescriptor(
                source_ref="src_approved_p0",
                publisher="已批准 P0 来源",
                source_dataset=source_dataset,
                authoritative=True,
                redistribution="INTERNAL_ONLY",
            ),
        ),
        coverage={
            "from": start.isoformat(),
            "to": end.isoformat(),
            "pitCoverage": "COMPLETE",
            "gaps": [],
        },
    )


def _revision_record(
    *,
    row: Any,
    provider_id: str,
    data_version: UUID,
    record_ref: str,
    record_type: str,
    entity_ref: str,
    entity_type: str,
    time: dict[str, object],
    values: dict[str, object],
    selected_fields: tuple[str, ...],
) -> dict[str, object]:
    """投影共享 revision 血缘字段与白名单业务值，绝不返回 source batch、raw URI 或内部行键。"""
    return {
        "recordRef": record_ref,
        "recordType": record_type,
        "entity": {"entityRef": entity_ref, "entityType": entity_type, "identifiers": []},
        "time": time,
        "publicUsableAt": _utc(row.public_usable_at),
        "availabilityBasis": row.availability_basis,
        "sourcePublishedAt": _utc(row.source_published_at)
        if row.source_published_at is not None
        else None,
        "observedAt": _utc(row.known_from),
        "dataVersion": str(data_version),
        "sourceRef": _source_ref(provider_id),
        "methodologyVersion": "1",
        "qualityStatus": row.quality_status.upper(),
        "revision": {"revisionNumber": row.revision_no, "currentInPublication": True},
        "values": {field: values[field] for field in selected_fields if field in values},
    }


def _margin_market_record(
    *,
    row: MarginMarketDailyRevision,
    provider_id: str,
    data_version: UUID,
    venue_id: UUID,
    selected_fields: tuple[str, ...],
) -> dict[str, object]:
    """投影两融场所汇总，保留来源汇总值而不由证券明细回算。"""
    return _revision_record(
        row=row,
        provider_id=provider_id,
        data_version=data_version,
        record_ref=f"margin-market:{venue_id}:{row.trade_date.isoformat()}:{row.revision_no}",
        record_type="MARGIN_MARKET",
        entity_ref=str(venue_id),
        entity_type="TRADING_VENUE",
        time={"tradeDate": row.trade_date.isoformat()},
        values={
            "tradeDate": row.trade_date.isoformat(),
            "venueEntityRef": str(venue_id),
            "marginBalance": row.total_balance,
            "shortBalance": row.lending_balance_amount,
        },
        selected_fields=selected_fields,
    )


def _dragon_tiger_record(
    *,
    row: DragonTigerEventRevision,
    instrument_id: UUID,
    provider_id: str,
    data_version: UUID,
    selected_fields: tuple[str, ...],
) -> dict[str, object]:
    """投影龙虎榜事件事实，不将席位原文或未来统计写入顶层 P0 记录。"""
    return _revision_record(
        row=row,
        provider_id=provider_id,
        data_version=data_version,
        record_ref=f"dragon-tiger:{row.event_revision_id}",
        record_type="DRAGON_TIGER_EVENT",
        entity_ref=str(instrument_id),
        entity_type="EQUITY",
        time={"tradeDate": row.trade_date.isoformat()},
        values={
            "tradeDate": row.trade_date.isoformat(),
            "equityEntityRef": str(instrument_id),
            "reasonCode": row.reason_code,
            "netAmount": row.net_amount,
        },
        selected_fields=selected_fields,
    )


def _block_trade_record(
    *,
    row: BlockTradeExecutionRevision,
    instrument_id: UUID,
    provider_id: str,
    data_version: UUID,
    selected_fields: tuple[str, ...],
) -> dict[str, object]:
    """投影大宗逐笔成交，同日相同经济字段的 occurrence 仍由独立 revision 记录保留。"""
    return _revision_record(
        row=row,
        provider_id=provider_id,
        data_version=data_version,
        record_ref=f"block-trade:{row.execution_revision_id}",
        record_type="BLOCK_TRADE_EXECUTION",
        entity_ref=str(instrument_id),
        entity_type="EQUITY",
        time={"tradeDate": row.trade_date.isoformat()},
        values={
            "tradeDate": row.trade_date.isoformat(),
            "equityEntityRef": str(instrument_id),
            "price": row.price,
            "quantity": row.quantity,
        },
        selected_fields=selected_fields,
    )


def _corporate_earnings_record(
    *,
    revision: CorporateEventRevision,
    event: CorporateEvent,
    document: DisclosureDocument,
    instrument_id: UUID,
    provider_id: str,
    data_version: UUID,
    selected_fields: tuple[str, ...],
) -> dict[str, object]:
    """投影业绩事件与主公告的共同事实，不把公告 URL、原文或指标行越权输出。"""
    return _revision_record(
        row=revision,
        provider_id=provider_id,
        data_version=data_version,
        record_ref=f"corporate-earnings:{revision.event_revision_id}",
        record_type="CORPORATE_EARNINGS_EVENT",
        entity_ref=str(instrument_id),
        entity_type="EQUITY",
        time={"eventDate": document.announced_on.isoformat()},
        values={
            "eventRef": str(event.event_id),
            "equityEntityRef": str(instrument_id),
            "announcementAt": document.announced_on.isoformat(),
            "eventKind": event.event_family,
        },
        selected_fields=selected_fields,
    )


def _margin_security_record(
    *,
    row: MarginSecurityDailyRevision,
    instrument_id: UUID,
    provider_id: str,
    data_version: UUID,
    selected_fields: tuple[str, ...],
) -> dict[str, object]:
    """投影证券两融直报字段，禁止输出 repository 明确隔离的派生偿还金额。"""
    return _revision_record(
        row=row,
        provider_id=provider_id,
        data_version=data_version,
        record_ref=f"margin-security:{instrument_id}:{row.trade_date.isoformat()}:{row.revision_no}",
        record_type="MARGIN_SECURITY",
        entity_ref=str(instrument_id),
        entity_type="EQUITY",
        time={"tradeDate": row.trade_date.isoformat()},
        values={
            "tradeDate": row.trade_date.isoformat(),
            "equityEntityRef": str(instrument_id),
            "financingBalance": row.financing_balance,
            "lendingBalanceQty": row.lending_balance_qty,
        },
        selected_fields=selected_fields,
    )


def _margin_eligibility_record(
    *,
    row: MarginEligibilityRevision,
    instrument_id: UUID,
    provider_id: str,
    data_version: UUID,
    selected_fields: tuple[str, ...],
) -> dict[str, object]:
    """投影两融资格有效区间，开放结束日期仍表示来源尚未确认终止。"""
    return _revision_record(
        row=row,
        provider_id=provider_id,
        data_version=data_version,
        record_ref=(
            f"margin-eligibility:{instrument_id}:{row.effective_from.isoformat()}:{row.revision_no}"
        ),
        record_type="MARGIN_ELIGIBILITY",
        entity_ref=str(instrument_id),
        entity_type="EQUITY",
        time={"effectiveFrom": row.effective_from.isoformat()},
        values={
            "equityEntityRef": str(instrument_id),
            "eligibilityStatus": row.status,
            "effectiveFrom": row.effective_from.isoformat(),
            "effectiveTo": row.effective_to.isoformat() if row.effective_to is not None else None,
        },
        selected_fields=selected_fields,
    )


def _stock_connect_market_record(
    *,
    row: StockConnectChannelDailyRevision,
    provider_id: str,
    data_version: UUID,
    selected_fields: tuple[str, ...],
) -> dict[str, object]:
    """投影港通通道统计，制度缺失金额继续以空值和状态向消费者表达。"""
    channel_ref = f"{row.channel}:{row.direction}"
    return _revision_record(
        row=row,
        provider_id=provider_id,
        data_version=data_version,
        record_ref=f"stock-connect-market:{channel_ref}:{row.trade_date.isoformat()}:{row.revision_no}",
        record_type="STOCK_CONNECT_MARKET",
        entity_ref=channel_ref,
        entity_type="STOCK_CONNECT_CHANNEL",
        time={"tradeDate": row.trade_date.isoformat()},
        values={
            "tradeDate": row.trade_date.isoformat(),
            "channel": row.channel,
            "direction": row.direction,
            "turnover": row.turnover_amount,
            "netBuy": row.net_buy_amount,
        },
        selected_fields=selected_fields,
    )


def _stock_connect_active_record(
    *,
    row: StockConnectActiveSecurityRevision,
    provider_id: str,
    data_version: UUID,
    selected_fields: tuple[str, ...],
) -> dict[str, object]:
    """投影港通活跃榜行，通道与证券身份保持在不同字段且不按代码合并。"""
    channel_ref = f"{row.channel}:{row.direction}"
    return _revision_record(
        row=row,
        provider_id=provider_id,
        data_version=data_version,
        record_ref=(
            f"stock-connect-active:{channel_ref}:{row.trade_date.isoformat()}:{row.rank_no}:{row.revision_no}"
        ),
        record_type="STOCK_CONNECT_ACTIVE_SECURITY",
        entity_ref=str(row.instrument_id),
        entity_type="MARKET_INSTRUMENT",
        time={"tradeDate": row.trade_date.isoformat()},
        values={
            "tradeDate": row.trade_date.isoformat(),
            "channel": row.channel,
            "direction": row.direction,
            "instrumentEntityRef": str(row.instrument_id),
            "rank": row.rank_no,
            "turnover": row.turnover_amount,
        },
        selected_fields=selected_fields,
    )


def _contract_partition_key(contract_id: UUID) -> str:
    """生成仅依赖永久合约 UUID 的 publication 分区键，避免代码复用或名称变化改变查询范围。"""
    return f"contract:{contract_id}"


def _etf_partition_key(etf_id: UUID) -> str:
    """生成仅依赖 ETF 上市工具 UUID 的 publication 分区键，代码变化不会改变读取边界。"""
    return f"etf:{etf_id}"


def _source_ref(provider_id: str) -> str:
    """将技术 provider 标识投影为受控 source ref，不泄漏 URL、凭据或内部适配器路径。"""
    normalized = "".join(char if char.isalnum() else "_" for char in provider_id.lower())
    return f"src_{normalized[:72] or 'unknown'}"


def _utc(value: datetime | None) -> datetime:
    """保证端口时间带 UTC 时区；数据库旧数据缺时区时拒绝而不猜测服务器本地时间。"""
    if value is None or value.tzinfo is None:
        raise MarketDataAccessUnavailable("market-data timestamp is unavailable")
    return value.astimezone(UTC)


def _utc_or_none(value: datetime | None) -> datetime | None:
    """保留来源未公开发布时间为空的事实，并继续拒绝无时区的非空时间。"""
    return None if value is None else _utc(value)
