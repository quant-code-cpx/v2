"""使用 ORM-enabled 表达式实现的个股标准日线版本化仓储。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, insert, or_, select, update
from sqlalchemy.orm import Session

from service_data_sync.application.ports.market_data import (
    EquityMarketDataRepository,
    PublishedDailyBars,
    StoredEquityInstrument,
)
from service_data_sync.domain.equity import EquityDailyBar, EquityIdentifier
from service_data_sync.domain.equity_master import EquityIdentityResolutionStatus
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.persistence.equity_identity_resolver import (
    resolve_identity_on_connection,
)
from service_data_sync.infrastructure.persistence.source_batch import record_source_observation

from ..database.models.equity.identity.equity_identifier_version import (
    EquityIdentifierVersion,
)
from ..database.models.equity.identity.equity_instrument import EquityInstrument
from ..database.models.equity.identity.equity_listing_status_version import (
    EquityListingStatusVersion,
)
from ..database.models.equity.market_data.equity_daily_bar import (
    EquityDailyBar as EquityDailyBarModel,
)
from ..database.models.publication.dataset_publication import DatasetPublication

_DATASET = "equity.bar.1d.raw"


class PossibleCodeReuseError(ValueError):
    """表示退市后出现同代码行情，必须等待主数据显式确认而非误绑旧身份。"""


class SqlAlchemyEquityMarketDataRepository(EquityMarketDataRepository):
    """持久化带来源链接的日线，采用追加修订和原子发布切换。"""

    def __init__(self, database: DatabaseClient) -> None:
        """使用服务私有 Session 工厂，不向应用调用方暴露它。"""
        self._database = database

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
        with self._database.transaction() as connection:
            source_batch_id = self._record_source_batch(
                connection,
                provider_id=provider_id,
                source_payload_sha256=source_payload_sha256,
                raw_uri=raw_uri,
                observed_at=observed_at,
                created_at=now,
            )
            # 日线可能早于交易所主数据到达。
            # 使用带来源证据的 `PENDING` 标识可避免丢失证券或猜测名称、上市状态。
            instrument = self._ensure_instrument(
                connection,
                identifier=identifier,
                fact_date=min(bar.trade_date for bar in bars),
                source_batch_id=source_batch_id,
                now=now,
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
        statement = select(
            EquityInstrument.security_id,
            EquityInstrument.instrument_id,
            EquityInstrument.exchange,
            EquityInstrument.symbol,
            EquityInstrument.name,
            EquityInstrument.listing_status,
        ).where(EquityInstrument.instrument_id == instrument_id)
        with self._database.session() as connection:
            row = connection.execute(statement).mappings().one_or_none()
        return None if row is None else _stored_instrument(row)

    def list_instruments(
        self, *, query: str | None, limit: int
    ) -> Sequence[StoredEquityInstrument]:
        """返回有上限且按交易所、代码排序的证券，供内部目录读取。"""
        normalized = query.strip() if query is not None else None
        statement = select(
            EquityInstrument.security_id,
            EquityInstrument.instrument_id,
            EquityInstrument.exchange,
            EquityInstrument.symbol,
            EquityInstrument.name,
            EquityInstrument.listing_status,
        )
        if normalized:
            pattern = f"{normalized}%"
            statement = statement.where(
                or_(
                    EquityInstrument.symbol.like(pattern),
                    func.coalesce(EquityInstrument.name, "").ilike(pattern),
                )
            )
        statement = statement.order_by(
            EquityInstrument.exchange,
            EquityInstrument.symbol,
            EquityInstrument.instrument_id,
        ).limit(limit)
        with self._database.session() as connection:
            rows = connection.execute(statement).mappings().all()
        return tuple(_stored_instrument(row) for row in rows)

    def list_daily_bars(
        self,
        *,
        instrument_id: UUID,
        start: date,
        end: date,
    ) -> Sequence[tuple[EquityDailyBar, int, bool]]:
        """按交易日升序读取一只证券有界窗口内的当前修订。"""
        statement = (
            select(
                EquityDailyBarModel.trade_date,
                EquityDailyBarModel.open_price,
                EquityDailyBarModel.high_price,
                EquityDailyBarModel.low_price,
                EquityDailyBarModel.close_price,
                EquityDailyBarModel.volume_shares,
                EquityDailyBarModel.amount_cny,
                EquityDailyBarModel.turnover_rate,
                EquityDailyBarModel.revision,
                EquityDailyBarModel.is_final,
            )
            .join(
                EquityInstrument,
                EquityInstrument.security_id == EquityDailyBarModel.security_id,
            )
            .where(
                EquityInstrument.instrument_id == instrument_id,
                EquityDailyBarModel.trade_date >= start,
                EquityDailyBarModel.trade_date <= end,
                EquityDailyBarModel.valid_to.is_(None),
            )
            .order_by(EquityDailyBarModel.trade_date)
        )
        with self._database.session() as connection:
            rows = connection.execute(statement).mappings().all()
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
        connection: Session,
        *,
        identifier: EquityIdentifier,
        fact_date: date,
        source_batch_id: UUID,
        now: datetime,
    ) -> StoredEquityInstrument:
        """日线早于主数据发布时创建带事实日期和证据的 `PENDING` 身份。"""
        resolution = resolve_identity_on_connection(
            connection,
            exchange=identifier.exchange,
            symbol=identifier.symbol,
            fact_date=fact_date,
            known_at=now,
        )
        if resolution.status is EquityIdentityResolutionStatus.CONFLICT:
            # 多个历史身份命中属于 canonical 损坏；不能依名称、状态或排序任取一条写日线。
            raise ValueError("equity identity resolution conflict")
        if resolution.status is EquityIdentityResolutionStatus.RESOLVED:
            if self._is_delisted_on_fact_date(
                connection,
                security_id=resolution.security_id,
                fact_date=fact_date,
                known_at=now,
            ):
                # 退市后同代码行情只能作为可能复用候选隔离，不能借当前标识回写旧证券。
                raise PossibleCodeReuseError("possible code reuse after delisting")
            existing = (
                connection.execute(
                    select(
                        EquityInstrument.security_id,
                        EquityInstrument.instrument_id,
                        EquityInstrument.exchange,
                        EquityInstrument.symbol,
                        EquityInstrument.name,
                        EquityInstrument.listing_status,
                    ).where(EquityInstrument.security_id == resolution.security_id)
                )
                .mappings()
                .one_or_none()
            )
            if existing is None:
                raise ValueError("resolved equity identity anchor is missing")
            return _stored_instrument(existing)
        pending = (
            connection.execute(
                select(
                    EquityInstrument.security_id,
                    EquityInstrument.instrument_id,
                    EquityInstrument.exchange,
                    EquityInstrument.symbol,
                    EquityInstrument.name,
                    EquityInstrument.listing_status,
                )
                .join(
                    EquityIdentifierVersion,
                    EquityIdentifierVersion.security_id == EquityInstrument.security_id,
                )
                .where(
                    EquityIdentifierVersion.exchange == identifier.exchange.value,
                    EquityIdentifierVersion.symbol == identifier.symbol,
                    EquityIdentifierVersion.identity_state == "PENDING",
                    EquityIdentifierVersion.known_to.is_(None),
                )
                .order_by(EquityInstrument.security_id)
            )
            .mappings()
            .one_or_none()
        )
        if pending is not None:
            # 同代码历史占位只能由同一 PENDING 身份复用；绝不从确认身份当前列回退。
            return _stored_instrument(pending)
        instrument_id = uuid4()
        # 主数据同步会补全该占位证券。
        # 行情同步绝不能自行猜测名称或上市状态。
        security_id = connection.execute(
            insert(EquityInstrument)
            .values(
                instrument_id=instrument_id,
                exchange=identifier.exchange.value,
                symbol=identifier.symbol,
                listing_status="PENDING",
                created_at=now,
                updated_at=now,
            )
            .returning(EquityInstrument.security_id)
        ).scalar_one()
        # PENDING 版本只为历史写入保留稳定锚点；主数据确认前绝不对 API 发布。
        connection.execute(
            insert(EquityIdentifierVersion).values(
                version_id=uuid4(),
                security_id=security_id,
                exchange=identifier.exchange.value,
                symbol=identifier.symbol,
                identity_state="PENDING",
                effective_from=fact_date,
                effective_to=None,
                known_from=now,
                known_to=None,
                effective_date_precision="OBSERVATION_DATE",
                source_batch_id=source_batch_id,
                content_sha256=_pending_identity_content_hash(identifier, fact_date),
            )
        )
        return StoredEquityInstrument(
            security_id=int(security_id),
            instrument_id=instrument_id,
            identifier=identifier,
            name=None,
            listing_status="PENDING",
        )

    def _is_delisted_on_fact_date(
        self,
        connection: Session,
        *,
        security_id: int | None,
        fact_date: date,
        known_at: datetime,
    ) -> bool:
        """检查已解析身份在事实日是否已有明确退市状态，不以当前投影替代历史。"""
        if security_id is None:
            raise ValueError("resolved equity identity must include security_id")
        row = connection.execute(
            select(EquityListingStatusVersion.version_id).where(
                EquityListingStatusVersion.security_id == security_id,
                EquityListingStatusVersion.status == "DELISTED",
                EquityListingStatusVersion.effective_range.op("@>")(fact_date),
                EquityListingStatusVersion.knowledge_range.op("@>")(known_at),
            )
        ).scalar_one_or_none()
        return row is not None

    def _record_source_batch(
        self,
        connection: Session,
        *,
        provider_id: str,
        source_payload_sha256: str,
        raw_uri: str,
        observed_at: datetime,
        created_at: datetime,
    ) -> UUID:
        """登记独立外部观测；相同 payload 不得折叠来源批次。"""
        return record_source_observation(
            connection,
            provider_id=provider_id,
            capability=_DATASET,
            source_payload_sha256=source_payload_sha256,
            raw_uri=raw_uri,
            observed_at=observed_at,
            created_at=created_at,
        )

    def _write_revisions(
        self,
        connection: Session,
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
                    select(
                        EquityDailyBarModel.revision,
                        EquityDailyBarModel.content_sha256,
                    ).where(
                        EquityDailyBarModel.security_id == security_id,
                        EquityDailyBarModel.trade_date == bar.trade_date,
                        EquityDailyBarModel.valid_to.is_(None),
                    )
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
                    update(EquityDailyBarModel)
                    .where(
                        EquityDailyBarModel.security_id == security_id,
                        EquityDailyBarModel.trade_date == bar.trade_date,
                        EquityDailyBarModel.valid_to.is_(None),
                    )
                    .values(valid_to=observed_at)
                )
            connection.execute(
                insert(EquityDailyBarModel).values(
                    security_id=security_id,
                    trade_date=bar.trade_date,
                    revision=revision,
                    open_price=bar.open_price,
                    high_price=bar.high_price,
                    low_price=bar.low_price,
                    close_price=bar.close_price,
                    volume_shares=bar.volume_shares,
                    amount_cny=bar.amount_cny,
                    turnover_rate=bar.turnover_rate,
                    is_final=True,
                    content_sha256=content_hash,
                    source_batch_id=source_batch_id,
                    valid_from=observed_at,
                    valid_to=None,
                )
            )
            inserted_count += 1
        return inserted_count, unchanged_count

    def _publish(
        self,
        connection: Session,
        *,
        identifier: EquityIdentifier,
        inserted_count: int,
        published_at: datetime,
    ) -> UUID:
        """仅当标准当前视图变化时推进单证券发布版本。"""
        partition_key = identifier.qualified_symbol
        if inserted_count == 0:
            # 幂等重放不得创建虚假数据版本，从而使 API/客户端缓存失效。
            existing = connection.execute(
                select(DatasetPublication.data_version).where(
                    DatasetPublication.dataset == _DATASET,
                    DatasetPublication.partition_key == partition_key,
                    DatasetPublication.superseded_at.is_(None),
                )
            ).scalar_one_or_none()
            if existing is not None:
                return UUID(str(existing))
        # 每个数据集分区仅有一条当前记录，读取方才能原子选择版本。
        connection.execute(
            update(DatasetPublication)
            .where(
                DatasetPublication.dataset == _DATASET,
                DatasetPublication.partition_key == partition_key,
                DatasetPublication.superseded_at.is_(None),
            )
            .values(superseded_at=published_at)
        )
        data_version = uuid4()
        connection.execute(
            insert(DatasetPublication).values(
                publication_id=uuid4(),
                dataset=_DATASET,
                partition_key=partition_key,
                data_version=data_version,
                quality_status="passed",
                published_at=published_at,
                superseded_at=None,
                effective_as_of=None,
                knowledge_cutoff=None,
            )
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


def _pending_identity_content_hash(identifier: EquityIdentifier, fact_date: date) -> bytes:
    """为行情创建的 PENDING 标识保存可复验的最小业务摘要。"""
    serialized = f"{identifier.qualified_symbol}|{fact_date.isoformat()}|PENDING".encode()
    return hashlib.sha256(serialized).digest()
