"""已发布证券主数据的 `SQLAlchemy` 只读仓储。

读取只面向冻结的 `canonical publication`，不会读取为兼容展示保留的“当前投影”。交易所、
代码、名称和生命周期必须在相同的市场有效日与知识截止点共同成立；任一版本基数异常
即拒绝返回，避免把跨时间片的字段拼成看似完整的证券。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import Select, and_, case, func, literal, or_, select, true
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import aliased

from service_data_sync.application.ports.equity_master_read import (
    EquityMasterPublication,
    EquityMasterReadRepository,
    EquityMasterReadUnavailable,
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
_AGGREGATE_DATASET = "equity.master.cn-a"
_AGGREGATE_PARTITION = "CN_A_STABLE"
_LISTING_STATUSES = frozenset({"LISTED", "SUSPENDED", "DELISTED"})


class SqlAlchemyEquityMasterReadRepository(EquityMasterReadRepository):
    """从 canonical 双时间表读取冻结发布切片，不读取兼容 current projection。"""

    def __init__(self, database: DatabaseClient) -> None:
        """保存服务自有数据库会话工厂，不向接口层泄漏 ORM 对象。"""
        self._database = database

    def get_current_publication(
        self, *, exchange: Exchange | None
    ) -> EquityMasterPublication | None:
        """读取单所或三所稳定聚合的当前通过版本。"""
        dataset = _AGGREGATE_DATASET if exchange is None else _EXCHANGE_DATASET
        partition_key = _AGGREGATE_PARTITION if exchange is None else exchange.value
        publication = aliased(DatasetPublication, name="publication")
        child = aliased(DatasetPublication, name="child")
        component_count = (
            select(func.count())
            .select_from(
                DatasetPublicationComponent.__table__.join(
                    child,
                    and_(
                        child.dataset == _EXCHANGE_DATASET,
                        child.partition_key == DatasetPublicationComponent.component_partition_key,
                        child.data_version == DatasetPublicationComponent.component_data_version,
                        child.quality_status == "passed",
                        child.effective_as_of.is_not(None),
                        child.knowledge_cutoff.is_not(None),
                    ),
                )
            )
            .where(
                DatasetPublicationComponent.aggregate_publication_id == publication.publication_id
            )
            .scalar_subquery()
        )
        statement = select(
            publication.data_version,
            publication.published_at,
            publication.effective_as_of,
            publication.knowledge_cutoff,
            case(
                (publication.dataset == _AGGREGATE_DATASET, component_count == 3),
                else_=true(),
            ).label("components_complete"),
        ).where(
            publication.dataset == dataset,
            publication.partition_key == partition_key,
            publication.quality_status == "passed",
            publication.effective_as_of.is_not(None),
            publication.knowledge_cutoff.is_not(None),
            publication.superseded_at.is_(None),
        )
        try:
            with self._database.session() as connection:
                row = connection.execute(statement).mappings().one_or_none()
        except SQLAlchemyError as error:
            raise EquityMasterReadUnavailable("equity master publication is unavailable") from error
        if row is None:
            return None
        if exchange is None and row["components_complete"] is not True:
            return None
        scope: PublicationScope = (
            "CN_A_STABLE" if exchange is None else cast(PublicationScope, exchange.value)
        )
        return EquityMasterPublication(
            data_version=UUID(str(row["data_version"])),
            published_at=row["published_at"],
            effective_as_of=row["effective_as_of"],
            knowledge_cutoff=row["knowledge_cutoff"],
            publication_scope=scope,
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
            EquityIdentifierVersion.known_from <= scope.c.scoped_known_at,
            or_(
                EquityIdentifierVersion.known_to.is_(None),
                EquityIdentifierVersion.known_to > scope.c.scoped_known_at,
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
            EquityIdentifierVersion.known_from <= scope.c.scoped_known_at,
            or_(
                EquityIdentifierVersion.known_to.is_(None),
                EquityIdentifierVersion.known_to > scope.c.scoped_known_at,
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
                            EquityListingStatusVersion.known_to <= scope.c.scoped_known_at,
                        ),
                        EquityListingStatusVersion.known_to,
                    ),
                    else_=literal(None),
                ).label("visible_known_to"),
                SourceBatch.observed_at,
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
                EquityListingStatusVersion.known_from <= scope.c.scoped_known_at,
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
    """将单所或聚合发布展开为各交易所固定知识截止时间的可组合 CTE。"""
    publication = aliased(DatasetPublication, name="publication")
    if exchange is not None:
        return (
            select(
                publication.partition_key.label("exchange"),
                func.least(literal(known_at), publication.knowledge_cutoff).label(
                    "scoped_known_at"
                ),
            )
            .where(
                publication.data_version == data_version,
                publication.dataset == _EXCHANGE_DATASET,
                publication.partition_key == exchange.value,
                publication.quality_status == "passed",
                publication.effective_as_of.is_not(None),
                publication.knowledge_cutoff.is_not(None),
            )
            .cte("publication_scope")
        )
    child = aliased(DatasetPublication, name="child_publication")
    return (
        select(
            DatasetPublicationComponent.component_partition_key.label("exchange"),
            func.least(literal(known_at), child.knowledge_cutoff).label("scoped_known_at"),
        )
        .select_from(publication)
        .join(
            DatasetPublicationComponent,
            DatasetPublicationComponent.aggregate_publication_id == publication.publication_id,
        )
        .join(
            child,
            and_(
                child.dataset == _EXCHANGE_DATASET,
                child.partition_key == DatasetPublicationComponent.component_partition_key,
                child.data_version == DatasetPublicationComponent.component_data_version,
                child.quality_status == "passed",
                child.effective_as_of.is_not(None),
                child.knowledge_cutoff.is_not(None),
            ),
        )
        .where(
            publication.data_version == data_version,
            publication.dataset == _AGGREGATE_DATASET,
            publication.partition_key == _AGGREGATE_PARTITION,
            publication.quality_status == "passed",
            publication.effective_as_of.is_not(None),
            publication.knowledge_cutoff.is_not(None),
        )
        .cte("publication_scope")
    )


def _instrument_projection(*, scope: Any, projection_as_of: date) -> Select[Any]:
    """构造身份、名称和生命周期均按相同双时间切片验证基数的只读投影。"""
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
        )
        .select_from(name_version)
        .join(name_source, name_source.source_batch_id == name_version.source_batch_id)
        .where(
            name_version.security_id == EquityInstrument.security_id,
            name_version.effective_from <= projection_as_of,
            or_(name_version.effective_to.is_(None), name_version.effective_to > projection_as_of),
            name_version.known_from <= scope.c.scoped_known_at,
            or_(name_version.known_to.is_(None), name_version.known_to > scope.c.scoped_known_at),
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
            listing_version.known_from <= scope.c.scoped_known_at,
            or_(
                listing_version.known_to.is_(None),
                listing_version.known_to > scope.c.scoped_known_at,
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
        name_projection.c.name_match_count,
        name_projection.c.name,
        name_projection.c.name_effective_from,
        name_projection.c.name_effective_to,
        name_projection.c.name_date_precision,
        name_projection.c.name_known_from,
        name_projection.c.name_observed_at,
        listing_projection.c.listing_match_count,
        listing_projection.c.status,
        listing_projection.c.listed_on,
        listing_projection.c.delisted_on,
        listing_projection.c.listing_effective_from,
        listing_projection.c.listing_effective_to,
        listing_projection.c.listing_date_precision,
        listing_projection.c.listing_known_from,
        listing_projection.c.listing_observed_at,
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
    """将一行三组双时间版本映射为 provider-neutral 证券投影。"""
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
        ),
        name=TemporalEquityName(
            value=str(row["name"]),
            effective_from=row["name_effective_from"],
            effective_to=row["name_effective_to"],
            date_precision=str(row["name_date_precision"]),
            known_from=row["name_known_from"],
            observed_at=row["name_observed_at"],
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
        ),
    )


def _stored_listing_period(row: Mapping[Any, Any]) -> StoredListingStatusPeriod:
    """将上市状态 SQL 行映射为带稳定内部版本键的历史项。"""
    return StoredListingStatusPeriod(
        version_id=UUID(str(row["version_id"])),
        status=str(row["status"]),
        effective_from=row["effective_from"],
        effective_to=row["effective_to"],
        effective_date_precision=str(row["effective_date_precision"]),
        known_from=row["known_from"],
        known_to=row["visible_known_to"],
        observed_at=row["observed_at"],
    )


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
