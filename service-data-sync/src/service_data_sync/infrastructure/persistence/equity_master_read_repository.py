"""已发布证券主数据的 SQLAlchemy 只读仓储。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.sql.elements import TextClause

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
from service_data_sync.infrastructure.database.connection import DatabaseClient

_EXCHANGE_DATASET = "equity.master.catalog"
_AGGREGATE_DATASET = "equity.master.cn-a"
_AGGREGATE_PARTITION = "CN_A_STABLE"
_LISTING_STATUSES = frozenset({"LISTED", "SUSPENDED", "DELISTED"})

_PUBLICATION_SCOPE_CTE = """
WITH selected_publication AS (
  SELECT publication_id, dataset, partition_key, knowledge_cutoff
  FROM dataset_publication
  WHERE data_version = :data_version
    AND quality_status = 'passed'
    AND effective_as_of IS NOT NULL
    AND knowledge_cutoff IS NOT NULL
    AND (
      (
        :requested_exchange IS NULL
        AND dataset = 'equity.master.cn-a'
        AND partition_key = 'CN_A_STABLE'
      )
      OR (
        :requested_exchange IS NOT NULL
        AND dataset = 'equity.master.catalog'
        AND partition_key = :requested_exchange
      )
    )
),
publication_scope AS (
  SELECT
    component.component_partition_key AS exchange,
    LEAST(:known_at, child.knowledge_cutoff) AS scoped_known_at
  FROM selected_publication AS publication
  INNER JOIN dataset_publication_component AS component
    ON component.aggregate_publication_id = publication.publication_id
  INNER JOIN dataset_publication AS child
    ON child.dataset = 'equity.master.catalog'
   AND child.partition_key = component.component_partition_key
   AND child.data_version = component.component_data_version
   AND child.quality_status = 'passed'
   AND child.effective_as_of IS NOT NULL
   AND child.knowledge_cutoff IS NOT NULL
  WHERE publication.dataset = 'equity.master.cn-a'

  UNION ALL

  SELECT
    publication.partition_key AS exchange,
    LEAST(:known_at, publication.knowledge_cutoff) AS scoped_known_at
  FROM selected_publication AS publication
  WHERE publication.dataset = 'equity.master.catalog'
)
"""

_INSTRUMENT_SELECT = """
SELECT
  anchor.security_id,
  anchor.instrument_id,
  identifier.exchange,
  btrim(identifier.symbol) AS symbol,
  identifier.effective_from AS identifier_effective_from,
  identifier.effective_to AS identifier_effective_to,
  identifier.effective_date_precision AS identifier_date_precision,
  identifier.known_from AS identifier_known_from,
  identifier_source.observed_at AS identifier_observed_at,
  name.match_count AS name_match_count,
  name.name,
  name.effective_from AS name_effective_from,
  name.effective_to AS name_effective_to,
  name.effective_date_precision AS name_date_precision,
  name.known_from AS name_known_from,
  name.observed_at AS name_observed_at,
  listing.match_count AS listing_match_count,
  listing.status,
  listing.listed_on,
  listing.delisted_on,
  listing.effective_from AS listing_effective_from,
  listing.effective_to AS listing_effective_to,
  listing.effective_date_precision AS listing_date_precision,
  listing.known_from AS listing_known_from,
  listing.observed_at AS listing_observed_at
FROM publication_scope AS scope
INNER JOIN equity_identifier_version AS identifier
  ON identifier.exchange = scope.exchange
INNER JOIN equity_instrument AS anchor
  ON anchor.security_id = identifier.security_id
INNER JOIN source_batch AS identifier_source
  ON identifier_source.source_batch_id = identifier.source_batch_id
LEFT JOIN LATERAL (
  SELECT
    COUNT(*)::INTEGER AS match_count,
    (ARRAY_AGG(version.name ORDER BY version.version_id))[1] AS name,
    (ARRAY_AGG(version.effective_from ORDER BY version.version_id))[1] AS effective_from,
    (ARRAY_AGG(version.effective_to ORDER BY version.version_id))[1] AS effective_to,
    (ARRAY_AGG(version.effective_date_precision ORDER BY version.version_id))[1]
      AS effective_date_precision,
    (ARRAY_AGG(version.known_from ORDER BY version.version_id))[1] AS known_from,
    (ARRAY_AGG(source.observed_at ORDER BY version.version_id))[1] AS observed_at
  FROM equity_name_version AS version
  INNER JOIN source_batch AS source
    ON source.source_batch_id = version.source_batch_id
  WHERE version.security_id = anchor.security_id
    AND version.effective_from <= :projection_as_of
    AND (version.effective_to IS NULL OR version.effective_to > :projection_as_of)
    AND version.known_from <= scope.scoped_known_at
    AND (version.known_to IS NULL OR version.known_to > scope.scoped_known_at)
) AS name ON TRUE
LEFT JOIN LATERAL (
  SELECT
    COUNT(*)::INTEGER AS match_count,
    (ARRAY_AGG(version.status ORDER BY version.version_id))[1] AS status,
    (ARRAY_AGG(version.listed_on ORDER BY version.version_id))[1] AS listed_on,
    (ARRAY_AGG(version.delisted_on ORDER BY version.version_id))[1] AS delisted_on,
    (ARRAY_AGG(version.effective_from ORDER BY version.version_id))[1] AS effective_from,
    (ARRAY_AGG(version.effective_to ORDER BY version.version_id))[1] AS effective_to,
    (ARRAY_AGG(version.effective_date_precision ORDER BY version.version_id))[1]
      AS effective_date_precision,
    (ARRAY_AGG(version.known_from ORDER BY version.version_id))[1] AS known_from,
    (ARRAY_AGG(source.observed_at ORDER BY version.version_id))[1] AS observed_at
  FROM equity_listing_status_version AS version
  INNER JOIN source_batch AS source
    ON source.source_batch_id = version.source_batch_id
  WHERE version.security_id = anchor.security_id
    AND version.effective_from <= :projection_as_of
    AND (version.effective_to IS NULL OR version.effective_to > :projection_as_of)
    AND version.known_from <= scope.scoped_known_at
    AND (version.known_to IS NULL OR version.known_to > scope.scoped_known_at)
) AS listing ON TRUE
"""


class SqlAlchemyEquityMasterReadRepository(EquityMasterReadRepository):
    """从 canonical 双时间表读取冻结发布切片，不读取兼容 current projection。"""

    def __init__(self, database: DatabaseClient) -> None:
        """保存服务自有只读引擎，不向接口层泄漏 SQLAlchemy 对象。"""
        self._engine: Engine = database.engine

    def get_current_publication(
        self, *, exchange: Exchange | None
    ) -> EquityMasterPublication | None:
        """读取单所或三所稳定聚合的当前通过版本。"""
        dataset = _AGGREGATE_DATASET if exchange is None else _EXCHANGE_DATASET
        partition_key = _AGGREGATE_PARTITION if exchange is None else exchange.value
        statement = text(
            """
            SELECT
              publication.data_version,
              publication.published_at,
              publication.effective_as_of,
              publication.knowledge_cutoff,
              CASE
                WHEN publication.dataset = 'equity.master.cn-a' THEN (
                  SELECT
                    COUNT(*) = 3
                    AND COUNT(*) FILTER (
                      WHERE component.component_partition_key IN ('SSE', 'SZSE', 'BSE')
                    ) = 3
                  FROM dataset_publication_component AS component
                  INNER JOIN dataset_publication AS child
                    ON child.dataset = 'equity.master.catalog'
                   AND child.partition_key = component.component_partition_key
                   AND child.data_version = component.component_data_version
                   AND child.quality_status = 'passed'
                   AND child.effective_as_of IS NOT NULL
                   AND child.knowledge_cutoff IS NOT NULL
                  WHERE component.aggregate_publication_id = publication.publication_id
                )
                ELSE TRUE
              END AS components_complete
            FROM dataset_publication AS publication
            WHERE publication.dataset = :dataset
              AND publication.partition_key = :partition_key
              AND publication.quality_status = 'passed'
              AND publication.effective_as_of IS NOT NULL
              AND publication.knowledge_cutoff IS NOT NULL
              AND publication.superseded_at IS NULL
            """
        )
        try:
            with self._engine.connect() as connection:
                row = (
                    connection.execute(
                        statement,
                        {"dataset": dataset, "partition_key": partition_key},
                    )
                    .mappings()
                    .one_or_none()
                )
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
        statement = text(
            _PUBLICATION_SCOPE_CTE
            + _INSTRUMENT_SELECT
            + """
            WHERE identifier.identity_state = 'CONFIRMED'
              AND identifier.effective_from <= :projection_as_of
              AND (
                identifier.effective_to IS NULL
                OR identifier.effective_to > :projection_as_of
              )
              AND identifier.known_from <= scope.scoped_known_at
              AND (
                identifier.known_to IS NULL
                OR identifier.known_to > scope.scoped_known_at
              )
              AND (
                :status_filter = FALSE
                OR listing.status = ANY(CAST(:statuses AS VARCHAR[]))
                OR listing.match_count <> 1
              )
              AND (
                :query_pattern IS NULL
                OR identifier.symbol LIKE :query_pattern ESCAPE '\\'
                OR lower(name.name) LIKE lower(:query_pattern) ESCAPE '\\'
                OR name.match_count <> 1
                OR listing.match_count <> 1
              )
              AND (
                :after_exchange IS NULL
                OR identifier.exchange > :after_exchange
                OR (
                  identifier.exchange = :after_exchange
                  AND identifier.symbol > :after_symbol
                )
                OR (
                  identifier.exchange = :after_exchange
                  AND identifier.symbol = :after_symbol
                  AND anchor.instrument_id > :after_instrument_id
                )
              )
            ORDER BY identifier.exchange, identifier.symbol, anchor.instrument_id
            LIMIT :limit
            """
        )
        parameters = {
            "data_version": data_version,
            "requested_exchange": None if exchange is None else exchange.value,
            "status_filter": bool(statuses),
            "statuses": list(statuses),
            "query_pattern": _prefix_pattern(query),
            "projection_as_of": as_of,
            "known_at": known_at,
            "after_exchange": None if after_exchange is None else after_exchange.value,
            "after_symbol": after_symbol,
            "after_instrument_id": after_instrument_id,
            "limit": limit,
        }
        rows = self._all(statement, parameters)
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
        identifier_predicate = (
            """
              AND identifier.effective_to IS NULL
              AND identifier.effective_from <= :projection_as_of
            """
            if identifier_as_of is None
            else """
              AND identifier.effective_from <= :identifier_as_of
              AND (
                identifier.effective_to IS NULL
                OR identifier.effective_to > :identifier_as_of
              )
            """
        )
        statement = text(
            _PUBLICATION_SCOPE_CTE
            + _INSTRUMENT_SELECT
            + """
            WHERE identifier.identity_state = 'CONFIRMED'
              AND identifier.exchange = :exchange
              AND identifier.symbol = :symbol
              AND identifier.known_from <= scope.scoped_known_at
              AND (
                identifier.known_to IS NULL
                OR identifier.known_to > scope.scoped_known_at
              )
            """
            + identifier_predicate
            + """
            ORDER BY anchor.instrument_id
            LIMIT :limit
            """
        )
        rows = self._all(
            statement,
            {
                "data_version": data_version,
                "requested_exchange": exchange.value,
                "exchange": exchange.value,
                "symbol": symbol,
                "identifier_as_of": identifier_as_of,
                "projection_as_of": projection_as_of,
                "known_at": known_at,
                "limit": limit,
            },
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
        statement = text(
            _PUBLICATION_SCOPE_CTE
            + """
            SELECT
              listing.version_id,
              listing.status,
              listing.effective_from,
              listing.effective_to,
              listing.effective_date_precision,
              listing.known_from,
              CASE
                WHEN listing.known_to IS NOT NULL
                 AND listing.known_to <= scope.scoped_known_at
                THEN listing.known_to
                ELSE NULL
              END AS visible_known_to,
              source.observed_at
            FROM publication_scope AS scope
            INNER JOIN equity_listing_status_version AS listing
              ON listing.security_id = :security_id
            INNER JOIN source_batch AS source
              ON source.source_batch_id = listing.source_batch_id
            WHERE scope.exchange = :exchange
              AND listing.known_from <= scope.scoped_known_at
              AND (
                :effective_from IS NULL
                OR listing.effective_to IS NULL
                OR listing.effective_to > :effective_from
              )
              AND (
                :effective_to IS NULL
                OR listing.effective_from < :effective_to
              )
              AND (
                :after_effective_from IS NULL
                OR listing.effective_from > :after_effective_from
                OR (
                  listing.effective_from = :after_effective_from
                  AND listing.known_from > :after_known_from
                )
                OR (
                  listing.effective_from = :after_effective_from
                  AND listing.known_from = :after_known_from
                  AND listing.version_id > :after_version_id
                )
              )
            ORDER BY listing.effective_from, listing.known_from, listing.version_id
            LIMIT :limit
            """
        )
        rows = self._all(
            statement,
            {
                "data_version": data_version,
                "requested_exchange": exchange.value,
                "exchange": exchange.value,
                "security_id": security_id,
                "known_at": known_at,
                "effective_from": effective_from,
                "effective_to": effective_to,
                "after_effective_from": after_effective_from,
                "after_known_from": after_known_from,
                "after_version_id": after_version_id,
                "limit": limit,
            },
        )
        return tuple(_stored_listing_period(row) for row in rows)

    def _all(
        self, statement: TextClause, parameters: Mapping[str, object]
    ) -> Sequence[Mapping[Any, Any]]:
        """执行有界只读 SQL，并把基础设施错误转换为稳定端口失败。"""
        try:
            with self._engine.connect() as connection:
                return connection.execute(statement, parameters).mappings().all()
        except SQLAlchemyError as error:
            raise EquityMasterReadUnavailable("equity master read is unavailable") from error


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
