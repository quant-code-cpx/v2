"""使用 SQLAlchemy Core 实现的个股标准日线版本化仓储。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Connection, Engine, text

from service_data_sync.application.ports.market_data import (
    EquityMarketDataRepository,
    PublishedDailyBars,
    StoredEquityInstrument,
)
from service_data_sync.domain.equity import EquityDailyBar, EquityIdentifier
from service_data_sync.infrastructure.database.connection import DatabaseClient

_DATASET = "equity.bar.1d.raw"


class SqlAlchemyEquityMarketDataRepository(EquityMarketDataRepository):
    """持久化带来源链接的日线，采用追加修订和原子发布切换。"""

    def __init__(self, database: DatabaseClient) -> None:
        """使用服务自有的 SQLAlchemy 引擎，不向应用调用方暴露它。"""
        self._engine: Engine = database.engine

    def publish_daily_bars(
        self,
        *,
        identifier: EquityIdentifier,
        bars: Sequence[EquityDailyBar],
        provider_id: str,
        source_payload_sha256: str,
        raw_uri: str,
        observed_at: datetime,
    ) -> PublishedDailyBars:
        """归档来源血缘、追加变化日线，并原子推进发布版本。"""
        if not bars:
            raise ValueError("bars must not be empty")
        now = datetime.now(UTC)
        with self._engine.begin() as connection:
            # 日线可能早于交易所主数据到达。
            # 使用 `PENDING` 身份可避免丢失证券或猜测名称、上市状态。
            instrument = self._ensure_instrument(connection, identifier, now)
            source_batch_id = self._record_source_batch(
                connection,
                provider_id=provider_id,
                source_payload_sha256=source_payload_sha256,
                raw_uri=raw_uri,
                observed_at=observed_at,
                created_at=now,
            )
            inserted_count, unchanged_count = self._write_revisions(
                connection,
                security_id=instrument.security_id,
                bars=bars,
                source_batch_id=source_batch_id,
                observed_at=observed_at,
            )
            data_version = self._publish(
                connection,
                identifier=identifier,
                inserted_count=inserted_count,
                published_at=now,
            )
        return PublishedDailyBars(
            data_version=data_version,
            inserted_count=inserted_count,
            unchanged_count=unchanged_count,
            instrument=instrument,
        )

    def get_instrument(self, instrument_id: UUID) -> StoredEquityInstrument | None:
        """按公开 UUID 读取一只证券，不关联供应商专有表。"""
        statement = text(
            """
            SELECT security_id, instrument_id, exchange, symbol, name, listing_status
            FROM equity_instrument
            WHERE instrument_id = :instrument_id
            """
        )
        with self._engine.connect() as connection:
            row = (
                connection.execute(statement, {"instrument_id": instrument_id})
                .mappings()
                .one_or_none()
            )
        return None if row is None else _stored_instrument(row)

    def list_instruments(
        self, *, query: str | None, limit: int
    ) -> Sequence[StoredEquityInstrument]:
        """返回有上限且按交易所、代码排序的证券，供内部目录读取。"""
        statement = text(
            """
            SELECT security_id, instrument_id, exchange, symbol, name, listing_status
            FROM equity_instrument
            WHERE :query IS NULL
               OR symbol LIKE :prefix
               OR COALESCE(name, '') ILIKE :name_prefix
            ORDER BY exchange, symbol, instrument_id
            LIMIT :limit
            """
        )
        normalized = query.strip() if query is not None else None
        parameters = {
            "query": normalized or None,
            "prefix": f"{normalized}%" if normalized else None,
            "name_prefix": f"{normalized}%" if normalized else None,
            "limit": limit,
        }
        with self._engine.connect() as connection:
            rows = connection.execute(statement, parameters).mappings().all()
        return tuple(_stored_instrument(row) for row in rows)

    def list_daily_bars(
        self,
        *,
        instrument_id: UUID,
        start: date,
        end: date,
    ) -> Sequence[tuple[EquityDailyBar, int, bool]]:
        """按交易日升序读取一只证券有界窗口内的当前修订。"""
        statement = text(
            """
            SELECT bar.trade_date, bar.open_price, bar.high_price, bar.low_price, bar.close_price,
                   bar.volume_shares, bar.amount_cny, bar.turnover_rate, bar.revision, bar.is_final
            FROM equity_daily_bar AS bar
            INNER JOIN equity_instrument AS instrument ON instrument.security_id = bar.security_id
            WHERE instrument.instrument_id = :instrument_id
              AND bar.trade_date >= :start
              AND bar.trade_date <= :end
              AND bar.valid_to IS NULL
            ORDER BY bar.trade_date ASC
            """
        )
        with self._engine.connect() as connection:
            rows = (
                connection.execute(
                    statement,
                    {"instrument_id": instrument_id, "start": start, "end": end},
                )
                .mappings()
                .all()
            )
        return tuple(
            (
                EquityDailyBar(
                    trade_date=row["trade_date"],
                    open_price=Decimal(row["open_price"]),
                    high_price=Decimal(row["high_price"]),
                    low_price=Decimal(row["low_price"]),
                    close_price=Decimal(row["close_price"]),
                    volume_shares=row["volume_shares"],
                    amount_cny=Decimal(row["amount_cny"]),
                    turnover_rate=(
                        None if row["turnover_rate"] is None else Decimal(row["turnover_rate"])
                    ),
                ),
                row["revision"],
                row["is_final"],
            )
            for row in rows
        )

    def _ensure_instrument(
        self,
        connection: Connection,
        identifier: EquityIdentifier,
        now: datetime,
    ) -> StoredEquityInstrument:
        """日线早于交易所主数据发布时创建 `PENDING` 证券身份。"""
        existing = (
            connection.execute(
                text(
                    """
                SELECT security_id, instrument_id, exchange, symbol, name, listing_status
                FROM equity_instrument
                WHERE exchange = :exchange AND symbol = :symbol
                """
                ),
                {"exchange": identifier.exchange.value, "symbol": identifier.symbol},
            )
            .mappings()
            .one_or_none()
        )
        if existing is not None:
            return _stored_instrument(existing)
        instrument_id = uuid4()
        # 主数据同步会补全该占位证券。
        # 行情同步绝不能自行猜测名称或上市状态。
        connection.execute(
            text(
                """
                INSERT INTO equity_instrument (
                  instrument_id, exchange, symbol, listing_status, created_at, updated_at
                ) VALUES (
                  :instrument_id, :exchange, :symbol, 'PENDING', :created_at, :updated_at
                )
                """
            ),
            {
                "instrument_id": instrument_id,
                "exchange": identifier.exchange.value,
                "symbol": identifier.symbol,
                "created_at": now,
                "updated_at": now,
            },
        )
        created = (
            connection.execute(
                text(
                    """
                SELECT security_id, instrument_id, exchange, symbol, name, listing_status
                FROM equity_instrument
                WHERE instrument_id = :instrument_id
                """
                ),
                {"instrument_id": instrument_id},
            )
            .mappings()
            .one()
        )
        return _stored_instrument(created)

    def _record_source_batch(
        self,
        connection: Connection,
        *,
        provider_id: str,
        source_payload_sha256: str,
        raw_uri: str,
        observed_at: datetime,
        created_at: datetime,
    ) -> UUID:
        """对相同原始证据去重，同时保留稳定的来源批次身份。"""
        source_batch_id = uuid4()
        row = (
            connection.execute(
                text(
                    """
                INSERT INTO source_batch (
                  source_batch_id, provider_id, capability, payload_sha256,
                  raw_uri, observed_at, created_at
                ) VALUES (
                  :source_batch_id, :provider_id, :capability, :payload_sha256,
                  :raw_uri, :observed_at, :created_at
                )
                ON CONFLICT (provider_id, capability, payload_sha256)
                DO UPDATE SET raw_uri = EXCLUDED.raw_uri
                RETURNING source_batch_id
                """
                ),
                {
                    "source_batch_id": source_batch_id,
                    "provider_id": provider_id,
                    "capability": _DATASET,
                    "payload_sha256": source_payload_sha256,
                    "raw_uri": raw_uri,
                    "observed_at": observed_at,
                    "created_at": created_at,
                },
            )
            .mappings()
            .one()
        )
        return row["source_batch_id"]

    def _write_revisions(
        self,
        connection: Connection,
        *,
        security_id: int,
        bars: Sequence[EquityDailyBar],
        source_batch_id: UUID,
        observed_at: datetime,
    ) -> tuple[int, int]:
        """仅关闭发生变化的当前修订，并插入不可变后继版本。"""
        inserted_count = 0
        unchanged_count = 0
        for bar in bars:
            content_hash = _bar_content_hash(bar)
            current = (
                connection.execute(
                    text(
                        """
                    SELECT revision, content_sha256
                    FROM equity_daily_bar
                    WHERE security_id = :security_id
                      AND trade_date = :trade_date
                      AND valid_to IS NULL
                    """
                    ),
                    {"security_id": security_id, "trade_date": bar.trade_date},
                )
                .mappings()
                .one_or_none()
            )
            if current is not None and current["content_sha256"] == content_hash:
                # 业务值相同即使重复抓取，也必须保留原修订和发布版本。
                unchanged_count += 1
                continue
            revision = 1 if current is None else current["revision"] + 1
            if current is not None:
                # 已发布观测永不覆盖。
                # 必须先关闭旧版本，再插入后继修订。
                connection.execute(
                    text(
                        """
                        UPDATE equity_daily_bar
                        SET valid_to = :valid_to
                        WHERE security_id = :security_id
                          AND trade_date = :trade_date
                          AND valid_to IS NULL
                        """
                    ),
                    {
                        "valid_to": observed_at,
                        "security_id": security_id,
                        "trade_date": bar.trade_date,
                    },
                )
            connection.execute(
                text(
                    """
                    INSERT INTO equity_daily_bar (
                      security_id, trade_date, revision, open_price, high_price,
                      low_price, close_price, volume_shares, amount_cny,
                      turnover_rate, is_final, content_sha256,
                      source_batch_id, valid_from, valid_to
                    ) VALUES (
                      :security_id, :trade_date, :revision, :open_price, :high_price,
                      :low_price, :close_price, :volume_shares, :amount_cny,
                      :turnover_rate, TRUE, :content_sha256,
                      :source_batch_id, :valid_from, NULL
                    )
                    """
                ),
                {
                    "security_id": security_id,
                    "trade_date": bar.trade_date,
                    "revision": revision,
                    "open_price": bar.open_price,
                    "high_price": bar.high_price,
                    "low_price": bar.low_price,
                    "close_price": bar.close_price,
                    "volume_shares": bar.volume_shares,
                    "amount_cny": bar.amount_cny,
                    "turnover_rate": bar.turnover_rate,
                    "content_sha256": content_hash,
                    "source_batch_id": source_batch_id,
                    "valid_from": observed_at,
                },
            )
            inserted_count += 1
        return inserted_count, unchanged_count

    def _publish(
        self,
        connection: Connection,
        *,
        identifier: EquityIdentifier,
        inserted_count: int,
        published_at: datetime,
    ) -> UUID:
        """仅当标准当前视图变化时推进单证券发布版本。"""
        partition_key = identifier.qualified_symbol
        if inserted_count == 0:
            # 幂等重放不得创建虚假数据版本，从而使 API/客户端缓存失效。
            existing = (
                connection.execute(
                    text(
                        """
                    SELECT data_version FROM dataset_publication
                    WHERE dataset = :dataset
                      AND partition_key = :partition_key
                      AND superseded_at IS NULL
                    """
                    ),
                    {"dataset": _DATASET, "partition_key": partition_key},
                )
                .mappings()
                .one_or_none()
            )
            if existing is not None:
                return existing["data_version"]
        # 每个数据集分区仅有一条当前记录，读取方才能原子选择版本。
        connection.execute(
            text(
                """
                UPDATE dataset_publication
                SET superseded_at = :published_at
                WHERE dataset = :dataset
                  AND partition_key = :partition_key
                  AND superseded_at IS NULL
                """
            ),
            {"published_at": published_at, "dataset": _DATASET, "partition_key": partition_key},
        )
        data_version = uuid4()
        connection.execute(
            text(
                """
                INSERT INTO dataset_publication (
                  publication_id, dataset, partition_key, data_version, quality_status,
                  published_at, superseded_at
                ) VALUES (
                  :publication_id, :dataset, :partition_key, :data_version, 'passed',
                  :published_at, NULL
                )
                """
            ),
            {
                "publication_id": uuid4(),
                "dataset": _DATASET,
                "partition_key": partition_key,
                "data_version": data_version,
                "published_at": published_at,
            },
        )
        return data_version


def _stored_instrument(row: Mapping[Any, Any]) -> StoredEquityInstrument:
    """将一条 SQL 映射行转换为数据源无关的已存证券记录。"""
    return StoredEquityInstrument(
        security_id=int(row["security_id"]),
        instrument_id=UUID(str(row["instrument_id"])),
        identifier=EquityIdentifier.parse(f"{row['exchange']}.{row['symbol']}"),
        name=None if row["name"] is None else str(row["name"]),
        listing_status=str(row["listing_status"]),
    )


def _bar_content_hash(bar: EquityDailyBar) -> bytes:
    """对标准业务字段计算哈希，避免重复来源批次制造伪修订。"""
    serialized = json.dumps(
        {
            "tradeDate": bar.trade_date.isoformat(),
            "open": str(bar.open_price),
            "high": str(bar.high_price),
            "low": str(bar.low_price),
            "close": str(bar.close_price),
            "volumeShares": bar.volume_shares,
            "amountCny": str(bar.amount_cny),
            "turnoverRate": None if bar.turnover_rate is None else str(bar.turnover_rate),
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(serialized).digest()
