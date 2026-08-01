"""已发布证券主数据的 `SQLAlchemy` 只读仓储。

读取只面向冻结的 `canonical publication`，不会读取为兼容展示保留的“当前投影”。目录
身份/名称与生命周期分别使用其固定输入组件的知识截止点；任一版本基数异常即拒绝返回，
避免把跨时间片的字段拼成看似完整的证券。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import Select, String, and_, case, func, literal, or_, select, true
from sqlalchemy import cast as sql_cast
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import aliased

from service_data_sync.application.ports.equity_master_read import (
    EquityMasterPublication,
    EquityMasterReadRepository,
    EquityMasterReadUnavailable,
    EquityPublicationComponent,
    EquitySourceAttribution,
    PublicationScope,
    StoredEquityInstrument,
    StoredListingStatusPeriod,
    TemporalEquityIdentifier,
    TemporalEquityListing,
    TemporalEquityName,
)
from service_data_sync.domain.equity import Exchange

from ..database.connection import DatabaseClient
from ..database.models.equity.identity.equity_identifier_version import (
    EquityIdentifierVersion,
)
from ..database.models.equity.identity.equity_instrument import (
    EquityInstrument,
)
from ..database.models.equity.identity.equity_listing_status_version import (
    EquityListingStatusVersion,
)
from ..database.models.equity.identity.equity_name_version import (
    EquityNameVersion,
)
from ..database.models.provenance.source_batch import SourceBatch
from ..database.models.publication.dataset_publication import (
    DatasetPublication,
)
from ..database.models.publication.dataset_publication_component import (
    DatasetPublicationComponent,
)

_EXCHANGE_DATASET = "equity.master.catalog"
_LIFECYCLE_DATASET = "equity.lifecycle.explicit"
_RESOLVED_DATASET = "equity.master.resolved"
_AGGREGATE_PARTITION = "CN_A_STABLE"
_CATALOG_COMPONENT = "catalog"
_LIFECYCLE_COMPONENT = "lifecycle"
_LISTING_STATUSES = frozenset({"LISTED", "SUSPENDED", "DELISTED"})


class SqlAlchemyEquityMasterReadRepository(EquityMasterReadRepository):
    """从 resolved canonical 双时间表读取冻结发布切片，不读取兼容 current projection。"""

    def __init__(self, database: DatabaseClient) -> None:
        """保存服务自有数据库会话工厂，不向接口层泄漏 ORM 对象。"""
        self._database = database

    def get_current_publication(
        self, *, exchange: Exchange | None
    ) -> EquityMasterPublication | None:
        """读取单所或三所 resolved publication 及其不可变输入组件。"""
        dataset = _RESOLVED_DATASET
        partition_key = _AGGREGATE_PARTITION if exchange is None else exchange.value
        publication = aliased(DatasetPublication, name="publication")
        statement = select(
            publication.data_version,
            publication.published_at,
            publication.effective_as_of,
        ).where(
            publication.dataset == dataset,
            publication.partition_key == partition_key,
            publication.quality_status == "passed",
            publication.effective_as_of.is_not(None),
            publication.superseded_at.is_(None),
        )
        try:
            with self._database.session() as connection:
                row = connection.execute(statement).mappings().one_or_none()
        except SQLAlchemyError as error:
            raise EquityMasterReadUnavailable("equity master publication is unavailable") from error
        if row is None:
            return None
        data_version = UUID(str(row["data_version"]))
        try:
            components = self._components_for_publication(
                data_version=data_version,
                exchange=exchange,
            )
        except SQLAlchemyError as error:
            raise EquityMasterReadUnavailable("equity master components are unavailable") from error
        expected_keys = (
            {"catalog", "lifecycle"}
            if exchange is not None
            else {f"{item.value}.catalog" for item in Exchange}
            | {f"{item.value}.lifecycle" for item in Exchange}
        )
        if {component.component_key for component in components} != expected_keys:
            return None
        scope: PublicationScope = (
            "CN_A_STABLE" if exchange is None else cast(PublicationScope, exchange.value)
        )
        return EquityMasterPublication(
            data_version=data_version,
            published_at=row["published_at"],
            effective_as_of=row["effective_as_of"],
            publication_scope=scope,
            components=components,
        )

    def _components_for_publication(
        self,
        *,
        data_version: UUID,
        exchange: Exchange | None,
    ) -> tuple[EquityPublicationComponent, ...]:
        """展开 leaf 或 aggregate manifest，保留目录/生命周期的独立输入血缘。"""
        publication = aliased(DatasetPublication, name="resolved_publication")
        component = aliased(DatasetPublicationComponent, name="resolved_component")
        child = aliased(DatasetPublication, name="input_publication")
        if exchange is not None:
            statement = (
                select(
                    component.component_partition_key.label("component_key"),
                    child.dataset,
                    child.partition_key,
                    child.data_version,
                    child.published_at,
                    child.effective_as_of,
                    child.knowledge_cutoff,
                    child.quality_status,
                )
                .select_from(publication)
                .join(
                    component,
                    component.aggregate_publication_id == publication.publication_id,
                )
                .join(
                    child,
                    and_(
                        child.data_version == component.component_data_version,
                        child.partition_key == exchange.value,
                        child.dataset.in_((_EXCHANGE_DATASET, _LIFECYCLE_DATASET)),
                        child.quality_status == "passed",
                        child.effective_as_of.is_not(None),
                        child.knowledge_cutoff.is_not(None),
                    ),
                )
                .where(
                    publication.data_version == data_version,
                    publication.dataset == _RESOLVED_DATASET,
                    publication.partition_key == exchange.value,
                    publication.quality_status == "passed",
                )
            )
        else:
            leaf_component = aliased(DatasetPublicationComponent, name="leaf_component")
            leaf = aliased(DatasetPublication, name="resolved_leaf")
            input_component = aliased(DatasetPublicationComponent, name="input_component")
            statement = (
                select(
                    func.concat(
                        leaf.partition_key,
                        literal("."),
                        input_component.component_partition_key,
                    ).label("component_key"),
                    child.dataset,
                    child.partition_key,
                    child.data_version,
                    child.published_at,
                    child.effective_as_of,
                    child.knowledge_cutoff,
                    child.quality_status,
                )
                .select_from(publication)
                .join(
                    leaf_component,
                    leaf_component.aggregate_publication_id == publication.publication_id,
                )
                .join(
                    leaf,
                    and_(
                        leaf.data_version == leaf_component.component_data_version,
                        leaf.dataset == _RESOLVED_DATASET,
                        leaf.partition_key == leaf_component.component_partition_key,
                        leaf.quality_status == "passed",
                    ),
                )
                .join(
                    input_component,
                    input_component.aggregate_publication_id == leaf.publication_id,
                )
                .join(
                    child,
                    and_(
                        child.data_version == input_component.component_data_version,
                        child.partition_key == leaf.partition_key,
                        child.dataset.in_((_EXCHANGE_DATASET, _LIFECYCLE_DATASET)),
                        child.quality_status == "passed",
                        child.effective_as_of.is_not(None),
                        child.knowledge_cutoff.is_not(None),
                    ),
                )
                .where(
                    publication.data_version == data_version,
                    publication.dataset == _RESOLVED_DATASET,
                    publication.partition_key == _AGGREGATE_PARTITION,
                    publication.quality_status == "passed",
                )
            )
        with self._database.session() as connection:
            rows = connection.execute(statement).mappings().all()
        return tuple(
            _publication_component(row)
            for row in sorted(rows, key=lambda value: str(value["component_key"]))
        )

    def list_instruments(
        self,
        *,
        data_version: UUID,
        exchange: Exchange | None,
        statuses: tuple[str, ...],
        query: str | None,
        as_of: date,
        known_at: datetime,
        after_exchange: Exchange | None,
        after_symbol: str | None,
        after_instrument_id: UUID | None,
        limit: int,
    ) -> Sequence[StoredEquityInstrument]:
        """按双时间切片与稳定复合键读取目录页。"""
        _validate_limit(limit)
        _validate_statuses(statuses)
        _validate_instrument_cursor(after_exchange, after_symbol, after_instrument_id)
        scope = _publication_scope(data_version=data_version, exchange=exchange, known_at=known_at)
        statement = _instrument_projection(scope=scope, projection_as_of=as_of).where(
            EquityIdentifierVersion.identity_state == "CONFIRMED",
            EquityIdentifierVersion.effective_from <= as_of,
            or_(
                EquityIdentifierVersion.effective_to.is_(None),
                EquityIdentifierVersion.effective_to > as_of,
            ),
            EquityIdentifierVersion.known_from <= scope.c.catalog_known_at,
            or_(
                EquityIdentifierVersion.known_to.is_(None),
                EquityIdentifierVersion.known_to > scope.c.catalog_known_at,
            ),
        )
        if statuses:
            statement = statement.where(
                or_(
                    statement.selected_columns.listing_match_count != 1,
                    statement.selected_columns.status.in_(statuses),
                )
            )
        query_pattern = _prefix_pattern(query)
        if query_pattern is not None:
            statement = statement.where(
                or_(
                    EquityIdentifierVersion.symbol.like(query_pattern, escape="\\"),
                    func.lower(statement.selected_columns.name).like(
                        func.lower(literal(query_pattern)), escape="\\"
                    ),
                    statement.selected_columns.name_match_count != 1,
                    statement.selected_columns.listing_match_count != 1,
                )
            )
        if after_exchange is not None:
            statement = statement.where(
                or_(
                    EquityIdentifierVersion.exchange > after_exchange.value,
                    and_(
                        EquityIdentifierVersion.exchange == after_exchange.value,
                        EquityIdentifierVersion.symbol > after_symbol,
                    ),
                    and_(
                        EquityIdentifierVersion.exchange == after_exchange.value,
                        EquityIdentifierVersion.symbol == after_symbol,
                        EquityInstrument.instrument_id > after_instrument_id,
                    ),
                )
            )
        rows = self._all(
            statement.order_by(
                EquityIdentifierVersion.exchange,
                EquityIdentifierVersion.symbol,
                EquityInstrument.instrument_id,
            ).limit(limit)
        )
        return _stored_instruments(rows)

    def find_instruments(
        self,
        *,
        data_version: UUID,
        exchange: Exchange,
        symbol: str,
        identifier_as_of: date | None,
        projection_as_of: date,
        known_at: datetime,
        limit: int = 2,
    ) -> Sequence[StoredEquityInstrument]:
        """按历史日期或当前开放标识解析路径身份，并最多返回两行检测冲突。"""
        if not 1 <= limit <= 2:
            raise ValueError("identity resolution limit must be from 1 to 2")
        scope = _publication_scope(data_version=data_version, exchange=exchange, known_at=known_at)
        conditions = [
            EquityIdentifierVersion.identity_state == "CONFIRMED",
            EquityIdentifierVersion.exchange == exchange.value,
            EquityIdentifierVersion.symbol == symbol,
            EquityIdentifierVersion.known_from <= scope.c.catalog_known_at,
            or_(
                EquityIdentifierVersion.known_to.is_(None),
                EquityIdentifierVersion.known_to > scope.c.catalog_known_at,
            ),
        ]
        if identifier_as_of is None:
            conditions.extend(
                [
                    EquityIdentifierVersion.effective_to.is_(None),
                    EquityIdentifierVersion.effective_from <= projection_as_of,
                ]
            )
        else:
            conditions.extend(
                [
                    EquityIdentifierVersion.effective_from <= identifier_as_of,
                    or_(
                        EquityIdentifierVersion.effective_to.is_(None),
                        EquityIdentifierVersion.effective_to > identifier_as_of,
                    ),
                ]
            )
        rows = self._all(
            _instrument_projection(scope=scope, projection_as_of=projection_as_of)
            .where(*conditions)
            .order_by(EquityInstrument.instrument_id)
            .limit(limit)
        )
        return _stored_instruments(rows)

    def list_listing_status_history(
        self,
        *,
        data_version: UUID,
        exchange: Exchange,
        security_id: int,
        known_at: datetime,
        effective_from: date | None,
        effective_to: date | None,
        after_effective_from: date | None,
        after_known_from: datetime | None,
        after_version_id: UUID | None,
        limit: int,
    ) -> Sequence[StoredListingStatusPeriod]:
        """读取知识截止时间前可审计的生命周期修订，并隐藏未来闭合时间。"""
        _validate_limit(limit)
        _validate_history_cursor(after_effective_from, after_known_from, after_version_id)
        scope = _publication_scope(data_version=data_version, exchange=exchange, known_at=known_at)
        statement = (
            select(
                EquityListingStatusVersion.version_id,
                EquityListingStatusVersion.status,
                EquityListingStatusVersion.effective_from,
                EquityListingStatusVersion.effective_to,
                EquityListingStatusVersion.effective_date_precision,
                EquityListingStatusVersion.known_from,
                case(
                    (
                        and_(
                            EquityListingStatusVersion.known_to.is_not(None),
                            EquityListingStatusVersion.known_to <= scope.c.lifecycle_known_at,
                        ),
                        EquityListingStatusVersion.known_to,
                    ),
                    else_=literal(None),
                ).label("visible_known_to"),
                SourceBatch.observed_at,
                EquityListingStatusVersion.evidence_kind,
                sql_cast(EquityListingStatusVersion.source_batch_id, String).label(
                    "source_batch_id"
                ),
                SourceBatch.provider_id,
                SourceBatch.upstream_source,
                scope.c.lifecycle_quality_status.label("quality_status"),
            )
            .select_from(
                scope.join(
                    EquityListingStatusVersion,
                    EquityListingStatusVersion.security_id == security_id,
                ).join(
                    SourceBatch,
                    SourceBatch.source_batch_id == EquityListingStatusVersion.source_batch_id,
                )
            )
            .where(
                scope.c.exchange == exchange.value,
                EquityListingStatusVersion.known_from <= scope.c.lifecycle_known_at,
            )
        )
        if effective_from is not None:
            statement = statement.where(
                or_(
                    EquityListingStatusVersion.effective_to.is_(None),
                    EquityListingStatusVersion.effective_to > effective_from,
                )
            )
        if effective_to is not None:
            statement = statement.where(EquityListingStatusVersion.effective_from < effective_to)
        if after_effective_from is not None:
            assert after_known_from is not None
            assert after_version_id is not None
            statement = statement.where(
                or_(
                    EquityListingStatusVersion.effective_from > after_effective_from,
                    and_(
                        EquityListingStatusVersion.effective_from == after_effective_from,
                        EquityListingStatusVersion.known_from > after_known_from,
                    ),
                    and_(
                        EquityListingStatusVersion.effective_from == after_effective_from,
                        EquityListingStatusVersion.known_from == after_known_from,
                        EquityListingStatusVersion.version_id > after_version_id,
                    ),
                )
            )
        rows = self._all(
            statement.order_by(
                EquityListingStatusVersion.effective_from,
                EquityListingStatusVersion.known_from,
                EquityListingStatusVersion.version_id,
            ).limit(limit)
        )
        return tuple(_stored_listing_period(row) for row in rows)

    def _all(self, statement: Select[Any]) -> Sequence[Mapping[Any, Any]]:
        """执行有界只读 SQL，并把基础设施错误转换为稳定端口失败。"""
        try:
            with self._database.session() as connection:
                return connection.execute(statement).mappings().all()
        except SQLAlchemyError as error:
            raise EquityMasterReadUnavailable("equity master read is unavailable") from error


def _publication_scope(*, data_version: UUID, exchange: Exchange | None, known_at: datetime) -> Any:
    """展开 resolved manifest，并保留目录/生命周期各自的受限知识时间。

    `known_at` 只是调用方请求的上界。每个组件在 SQL 内独立取其 publication cutoff 的较小值，
    因而不会将两个输入错误压成一个共同 cutoff，也不会让较新的官方生命周期越过自身版本。
    """
    publication = aliased(DatasetPublication, name="resolved_publication")
    if exchange is not None:
        catalog_component = aliased(DatasetPublicationComponent, name="catalog_component")
        lifecycle_component = aliased(DatasetPublicationComponent, name="lifecycle_component")
        catalog = aliased(DatasetPublication, name="catalog_publication")
        lifecycle = aliased(DatasetPublication, name="lifecycle_publication")
        return (
            select(
                publication.partition_key.label("exchange"),
                func.least(literal(known_at), catalog.knowledge_cutoff).label("catalog_known_at"),
                func.least(literal(known_at), lifecycle.knowledge_cutoff).label(
                    "lifecycle_known_at"
                ),
                catalog.quality_status.label("catalog_quality_status"),
                lifecycle.quality_status.label("lifecycle_quality_status"),
            )
            .select_from(publication)
            .join(
                catalog_component,
                and_(
                    catalog_component.aggregate_publication_id == publication.publication_id,
                    catalog_component.component_partition_key == _CATALOG_COMPONENT,
                ),
            )
            .join(
                lifecycle_component,
                and_(
                    lifecycle_component.aggregate_publication_id == publication.publication_id,
                    lifecycle_component.component_partition_key == _LIFECYCLE_COMPONENT,
                ),
            )
            .join(
                catalog,
                and_(
                    catalog.data_version == catalog_component.component_data_version,
                    catalog.dataset == _EXCHANGE_DATASET,
                    catalog.partition_key == publication.partition_key,
                    catalog.quality_status == "passed",
                    catalog.effective_as_of.is_not(None),
                    catalog.knowledge_cutoff.is_not(None),
                ),
            )
            .join(
                lifecycle,
                and_(
                    lifecycle.data_version == lifecycle_component.component_data_version,
                    lifecycle.dataset == _LIFECYCLE_DATASET,
                    lifecycle.partition_key == publication.partition_key,
                    lifecycle.quality_status == "passed",
                    lifecycle.effective_as_of.is_not(None),
                    lifecycle.knowledge_cutoff.is_not(None),
                ),
            )
            .where(
                publication.data_version == data_version,
                publication.dataset == _RESOLVED_DATASET,
                publication.partition_key == exchange.value,
                publication.quality_status == "passed",
                publication.effective_as_of.is_not(None),
            )
            .cte("publication_scope")
        )
    aggregate_component = aliased(DatasetPublicationComponent, name="aggregate_component")
    leaf = aliased(DatasetPublication, name="resolved_leaf")
    catalog_component = aliased(DatasetPublicationComponent, name="catalog_component")
    lifecycle_component = aliased(DatasetPublicationComponent, name="lifecycle_component")
    catalog = aliased(DatasetPublication, name="catalog_publication")
    lifecycle = aliased(DatasetPublication, name="lifecycle_publication")
    return (
        select(
            leaf.partition_key.label("exchange"),
            func.least(literal(known_at), catalog.knowledge_cutoff).label("catalog_known_at"),
            func.least(literal(known_at), lifecycle.knowledge_cutoff).label("lifecycle_known_at"),
            catalog.quality_status.label("catalog_quality_status"),
            lifecycle.quality_status.label("lifecycle_quality_status"),
        )
        .select_from(publication)
        .join(
            aggregate_component,
            aggregate_component.aggregate_publication_id == publication.publication_id,
        )
        .join(
            leaf,
            and_(
                leaf.data_version == aggregate_component.component_data_version,
                leaf.dataset == _RESOLVED_DATASET,
                leaf.partition_key == aggregate_component.component_partition_key,
                leaf.quality_status == "passed",
            ),
        )
        .join(
            catalog_component,
            and_(
                catalog_component.aggregate_publication_id == leaf.publication_id,
                catalog_component.component_partition_key == _CATALOG_COMPONENT,
            ),
        )
        .join(
            lifecycle_component,
            and_(
                lifecycle_component.aggregate_publication_id == leaf.publication_id,
                lifecycle_component.component_partition_key == _LIFECYCLE_COMPONENT,
            ),
        )
        .join(
            catalog,
            and_(
                catalog.data_version == catalog_component.component_data_version,
                catalog.dataset == _EXCHANGE_DATASET,
                catalog.partition_key == leaf.partition_key,
                catalog.quality_status == "passed",
                catalog.effective_as_of.is_not(None),
                catalog.knowledge_cutoff.is_not(None),
            ),
        )
        .join(
            lifecycle,
            and_(
                lifecycle.data_version == lifecycle_component.component_data_version,
                lifecycle.dataset == _LIFECYCLE_DATASET,
                lifecycle.partition_key == leaf.partition_key,
                lifecycle.quality_status == "passed",
                lifecycle.effective_as_of.is_not(None),
                lifecycle.knowledge_cutoff.is_not(None),
            ),
        )
        .where(
            publication.data_version == data_version,
            publication.dataset == _RESOLVED_DATASET,
            publication.partition_key == _AGGREGATE_PARTITION,
            publication.quality_status == "passed",
            publication.effective_as_of.is_not(None),
        )
        .cte("publication_scope")
    )


def _instrument_projection(*, scope: Any, projection_as_of: date) -> Select[Any]:
    """构造目录身份/名称与生命周期按各自知识组件读取的只读投影。"""
    identifier_source = aliased(SourceBatch, name="identifier_source")
    name_version = aliased(EquityNameVersion, name="name_version")
    name_source = aliased(SourceBatch, name="name_source")
    listing_version = aliased(EquityListingStatusVersion, name="listing_version")
    listing_source = aliased(SourceBatch, name="listing_source")
    name_projection = (
        select(
            func.count(name_version.version_id).label("name_match_count"),
            func.min(name_version.name).label("name"),
            func.min(name_version.effective_from).label("name_effective_from"),
            func.min(name_version.effective_to).label("name_effective_to"),
            func.min(name_version.effective_date_precision).label("name_date_precision"),
            func.min(name_version.known_from).label("name_known_from"),
            func.min(name_source.observed_at).label("name_observed_at"),
            func.min(sql_cast(name_version.source_batch_id, String)).label("name_source_batch_id"),
            func.min(name_source.provider_id).label("name_provider_id"),
            func.min(name_source.upstream_source).label("name_upstream_source"),
        )
        .select_from(name_version)
        .join(name_source, name_source.source_batch_id == name_version.source_batch_id)
        .where(
            name_version.security_id == EquityInstrument.security_id,
            name_version.effective_from <= projection_as_of,
            or_(name_version.effective_to.is_(None), name_version.effective_to > projection_as_of),
            name_version.known_from <= scope.c.catalog_known_at,
            or_(name_version.known_to.is_(None), name_version.known_to > scope.c.catalog_known_at),
        )
        .lateral("name_projection")
    )
    listing_projection = (
        select(
            func.count(listing_version.version_id).label("listing_match_count"),
            func.min(listing_version.status).label("status"),
            func.min(listing_version.listed_on).label("listed_on"),
            func.min(listing_version.delisted_on).label("delisted_on"),
            func.min(listing_version.effective_from).label("listing_effective_from"),
            func.min(listing_version.effective_to).label("listing_effective_to"),
            func.min(listing_version.effective_date_precision).label("listing_date_precision"),
            func.min(listing_version.known_from).label("listing_known_from"),
            func.min(listing_source.observed_at).label("listing_observed_at"),
            func.min(listing_version.evidence_kind).label("listing_evidence_kind"),
            func.min(sql_cast(listing_version.source_batch_id, String)).label(
                "listing_source_batch_id"
            ),
            func.min(listing_source.provider_id).label("listing_provider_id"),
            func.min(listing_source.upstream_source).label("listing_upstream_source"),
        )
        .select_from(listing_version)
        .join(listing_source, listing_source.source_batch_id == listing_version.source_batch_id)
        .where(
            listing_version.security_id == EquityInstrument.security_id,
            listing_version.effective_from <= projection_as_of,
            or_(
                listing_version.effective_to.is_(None),
                listing_version.effective_to > projection_as_of,
            ),
            listing_version.known_from <= scope.c.lifecycle_known_at,
            or_(
                listing_version.known_to.is_(None),
                listing_version.known_to > scope.c.lifecycle_known_at,
            ),
        )
        .lateral("listing_projection")
    )
    return select(
        EquityInstrument.security_id,
        EquityInstrument.instrument_id,
        EquityIdentifierVersion.exchange,
        func.btrim(EquityIdentifierVersion.symbol).label("symbol"),
        EquityIdentifierVersion.effective_from.label("identifier_effective_from"),
        EquityIdentifierVersion.effective_to.label("identifier_effective_to"),
        EquityIdentifierVersion.effective_date_precision.label("identifier_date_precision"),
        EquityIdentifierVersion.known_from.label("identifier_known_from"),
        identifier_source.observed_at.label("identifier_observed_at"),
        sql_cast(EquityIdentifierVersion.source_batch_id, String).label(
            "identifier_source_batch_id"
        ),
        identifier_source.provider_id.label("identifier_provider_id"),
        identifier_source.upstream_source.label("identifier_upstream_source"),
        scope.c.catalog_quality_status.label("catalog_quality_status"),
        name_projection.c.name_match_count,
        name_projection.c.name,
        name_projection.c.name_effective_from,
        name_projection.c.name_effective_to,
        name_projection.c.name_date_precision,
        name_projection.c.name_known_from,
        name_projection.c.name_observed_at,
        name_projection.c.name_source_batch_id,
        name_projection.c.name_provider_id,
        name_projection.c.name_upstream_source,
        listing_projection.c.listing_match_count,
        listing_projection.c.status,
        listing_projection.c.listed_on,
        listing_projection.c.delisted_on,
        listing_projection.c.listing_effective_from,
        listing_projection.c.listing_effective_to,
        listing_projection.c.listing_date_precision,
        listing_projection.c.listing_known_from,
        listing_projection.c.listing_observed_at,
        listing_projection.c.listing_evidence_kind,
        listing_projection.c.listing_source_batch_id,
        listing_projection.c.listing_provider_id,
        listing_projection.c.listing_upstream_source,
        scope.c.lifecycle_quality_status.label("lifecycle_quality_status"),
    ).select_from(
        scope.join(EquityIdentifierVersion, EquityIdentifierVersion.exchange == scope.c.exchange)
        .join(EquityInstrument, EquityInstrument.security_id == EquityIdentifierVersion.security_id)
        .join(
            identifier_source,
            identifier_source.source_batch_id == EquityIdentifierVersion.source_batch_id,
        )
        .outerjoin(name_projection, true())
        .outerjoin(listing_projection, true())
    )


def _stored_instruments(
    rows: Sequence[Mapping[Any, Any]],
) -> tuple[StoredEquityInstrument, ...]:
    """验证每个已发布身份恰有一组名称和生命周期投影后完成映射。"""
    instruments: list[StoredEquityInstrument] = []
    for row in rows:
        # 已发布身份缺投影或双时间排斥失效都属于发布完整性故障，不能伪装成 404/空页。
        if int(row["name_match_count"]) != 1 or int(row["listing_match_count"]) != 1:
            raise EquityMasterReadUnavailable("published equity projection is inconsistent")
        instruments.append(_stored_instrument(row))
    return tuple(instruments)


def _stored_instrument(row: Mapping[Any, Any]) -> StoredEquityInstrument:
    """将一行三组双时间版本映射为带来源、证据和质量状态的证券投影。"""
    return StoredEquityInstrument(
        security_id=int(row["security_id"]),
        instrument_id=UUID(str(row["instrument_id"])),
        identifier=TemporalEquityIdentifier(
            exchange=Exchange(str(row["exchange"])),
            symbol=str(row["symbol"]).strip(),
            effective_from=row["identifier_effective_from"],
            effective_to=row["identifier_effective_to"],
            date_precision=str(row["identifier_date_precision"]),
            known_from=row["identifier_known_from"],
            observed_at=row["identifier_observed_at"],
            source=_source_attribution(row, prefix="identifier"),
            quality_status=_quality_status(row, key="catalog_quality_status"),
        ),
        name=TemporalEquityName(
            value=str(row["name"]),
            effective_from=row["name_effective_from"],
            effective_to=row["name_effective_to"],
            date_precision=str(row["name_date_precision"]),
            known_from=row["name_known_from"],
            observed_at=row["name_observed_at"],
            source=_source_attribution(row, prefix="name"),
            quality_status=_quality_status(row, key="catalog_quality_status"),
        ),
        listing=TemporalEquityListing(
            status=str(row["status"]),
            listed_on=row["listed_on"],
            delisted_on=row["delisted_on"],
            effective_from=row["listing_effective_from"],
            effective_to=row["listing_effective_to"],
            date_precision=str(row["listing_date_precision"]),
            known_from=row["listing_known_from"],
            observed_at=row["listing_observed_at"],
            evidence_kind=str(row["listing_evidence_kind"]),
            source=_source_attribution(row, prefix="listing"),
            quality_status=_quality_status(row, key="lifecycle_quality_status"),
        ),
    )


def _stored_listing_period(row: Mapping[Any, Any]) -> StoredListingStatusPeriod:
    """将上市状态 SQL 行映射为带稳定内部版本键、来源和证据的历史项。"""
    return StoredListingStatusPeriod(
        version_id=UUID(str(row["version_id"])),
        status=str(row["status"]),
        effective_from=row["effective_from"],
        effective_to=row["effective_to"],
        effective_date_precision=str(row["effective_date_precision"]),
        known_from=row["known_from"],
        known_to=row["visible_known_to"],
        observed_at=row["observed_at"],
        evidence_kind=str(row["evidence_kind"]),
        source=_source_attribution(row, prefix=""),
        quality_status=_quality_status(row, key="quality_status"),
    )


def _publication_component(row: Mapping[Any, Any]) -> EquityPublicationComponent:
    """验证并映射 resolved 输入组件元数据，拒绝残缺 lineage。"""
    effective_as_of = row["effective_as_of"]
    knowledge_cutoff = row["knowledge_cutoff"]
    if (
        not isinstance(effective_as_of, date)
        or not isinstance(knowledge_cutoff, datetime)
        or knowledge_cutoff.tzinfo is None
    ):
        raise EquityMasterReadUnavailable("equity master component metadata is invalid")
    quality_status = str(row["quality_status"])
    if quality_status != "passed":
        raise EquityMasterReadUnavailable("equity master component quality is not publishable")
    return EquityPublicationComponent(
        component_key=str(row["component_key"]),
        dataset=str(row["dataset"]),
        partition_key=str(row["partition_key"]),
        data_version=UUID(str(row["data_version"])),
        published_at=row["published_at"],
        effective_as_of=effective_as_of,
        knowledge_cutoff=knowledge_cutoff,
        quality_status=quality_status,
    )


def _source_attribution(row: Mapping[Any, Any], *, prefix: str) -> EquitySourceAttribution:
    """从 SQL 行构造不含 raw URI 的来源批次锚点。"""
    separator = "" if not prefix else "_"
    batch_id = row[f"{prefix}{separator}source_batch_id"]
    provider_id = row[f"{prefix}{separator}provider_id"]
    upstream_source = row[f"{prefix}{separator}upstream_source"]
    if (
        not isinstance(batch_id, str)
        or not isinstance(provider_id, str)
        or not isinstance(upstream_source, str)
    ):
        raise EquityMasterReadUnavailable("equity source attribution is incomplete")
    try:
        return EquitySourceAttribution(
            source_batch_id=UUID(batch_id),
            provider_id=provider_id,
            upstream_source=upstream_source,
        )
    except ValueError as error:
        raise EquityMasterReadUnavailable("equity source batch identity is invalid") from error


def _quality_status(row: Mapping[Any, Any], *, key: str) -> str:
    """限制读模型只返回已经通过发布门的组件质量状态。"""
    quality_status = row[key]
    if quality_status != "passed":
        raise EquityMasterReadUnavailable("equity component quality is not publishable")
    return "passed"


def _prefix_pattern(value: str | None) -> str | None:
    """转义 SQL LIKE 元字符，使接口前缀查询保持字面语义。"""
    if value is None:
        return None
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"{escaped}%"


def _validate_limit(limit: int) -> None:
    """限制仓储只接受接口层为多取一行预留后的页大小。"""
    if not 1 <= limit <= 201:
        raise ValueError("limit must be from 1 to 201")


def _validate_statuses(statuses: tuple[str, ...]) -> None:
    """拒绝把非上市生命周期状态带入 SQL 数组筛选。"""
    if len(statuses) > 3 or len(set(statuses)) != len(statuses):
        raise ValueError("statuses must contain up to three unique values")
    if any(status not in _LISTING_STATUSES for status in statuses):
        raise ValueError("listing status is invalid")


def _validate_instrument_cursor(
    exchange: Exchange | None,
    symbol: str | None,
    instrument_id: UUID | None,
) -> None:
    """要求目录复合游标的三个排序键同时存在或同时缺失。"""
    present = (exchange is not None, symbol is not None, instrument_id is not None)
    if any(present) and not all(present):
        raise ValueError("instrument cursor keys must be supplied together")


def _validate_history_cursor(
    effective_from: date | None,
    known_from: datetime | None,
    version_id: UUID | None,
) -> None:
    """要求历史复合游标的三个排序键同时存在或同时缺失。"""
    present = (effective_from is not None, known_from is not None, version_id is not None)
    if any(present) and not all(present):
        raise ValueError("history cursor keys must be supplied together")
