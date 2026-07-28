"""使用 ORM-enabled 表达式保存板块三周期行情、血缘与版本化发布。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import insert, or_, select, update
from sqlalchemy.orm import Session

from service_data_sync.application.ports.sector_market_data import (
    DatasetPublication,
    PublishedSectorBars,
    PublishedSectorCatalog,
    SectorMarketDataRepository,
    StoredSector,
)
from service_data_sync.domain.sector import (
    SectorBar,
    SectorCatalogEntry,
    SectorIdentifier,
    SectorPeriod,
    SectorScheme,
)
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.persistence.source_batch import record_source_observation

from ..database.models.publication.dataset_publication import (
    DatasetPublication as DatasetPublicationModel,
)
from ..database.models.sector.catalog.sector_entity import SectorEntity
from ..database.models.sector.market_data.sector_daily_bar import SectorDailyBar
from ..database.models.sector.market_data.sector_monthly_bar import SectorMonthlyBar
from ..database.models.sector.market_data.sector_weekly_bar import SectorWeeklyBar

_MODEL_BY_PERIOD = {
    SectorPeriod.DAY_1: SectorDailyBar,
    SectorPeriod.WEEK_1: SectorWeeklyBar,
    SectorPeriod.MONTH_1: SectorMonthlyBar,
}
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_CATALOG_DATASET = "sector.catalog.raw"


class SqlAlchemySectorMarketDataRepository(SectorMarketDataRepository):
    """用独立物理表保存日、周、月上游行情，追加修订后原子发布。"""

    def __init__(self, database: DatabaseClient) -> None:
        """使用服务自有 Session 工厂，不向应用调用方泄漏数据库实现。"""
        self._database = database

    def publish_bars(
        self,
        *,
        identifier: SectorIdentifier,
        period: SectorPeriod,
        bars: Sequence[SectorBar],
        provider_id: str,
        source_payload_sha256: str,
        raw_uri: str,
        observed_at: datetime,
    ) -> PublishedSectorBars:
        """归档来源血缘、追加变化行，并推进指定周期的当前发布。"""
        if not bars:
            raise ValueError("bars must not be empty")
        if observed_at.tzinfo is None:
            raise ValueError("observed_at must include a timezone")
        now = datetime.now(UTC)
        with self._database.transaction() as connection:
            sector = self._ensure_sector(connection, identifier=identifier, now=now)
            source_batch_id = self._record_source_batch(
                connection,
                provider_id=provider_id,
                capability=period.capability,
                source_payload_sha256=source_payload_sha256,
                raw_uri=raw_uri,
                observed_at=observed_at,
                created_at=now,
            )
            inserted_count, unchanged_count = self._write_revisions(
                connection,
                sector_key=sector.sector_key,
                period=period,
                bars=bars,
                source_batch_id=source_batch_id,
                observed_at=observed_at,
            )
            data_version = self._publish(
                connection,
                identifier=identifier,
                period=period,
                inserted_count=inserted_count,
                published_at=now,
            )
        return PublishedSectorBars(
            data_version=data_version,
            inserted_count=inserted_count,
            unchanged_count=unchanged_count,
            sector=sector,
        )

    def publish_catalog(
        self,
        *,
        scheme: SectorScheme,
        entries: Sequence[SectorCatalogEntry],
        provider_id: str,
        source_payload_sha256: str,
        raw_uri: str,
        observed_at: datetime,
    ) -> PublishedSectorCatalog:
        """归档目录血缘、激活已确认名称，并推进分类体系当前发布。"""
        if not entries:
            raise ValueError("entries must not be empty")
        if observed_at.tzinfo is None:
            raise ValueError("observed_at must include a timezone")
        if any(entry.identifier.scheme is not scheme for entry in entries):
            raise ValueError("catalog entries must use the requested scheme")
        now = datetime.now(UTC)
        with self._database.transaction() as connection:
            self._record_source_batch(
                connection,
                provider_id=provider_id,
                capability=_CATALOG_DATASET,
                source_payload_sha256=source_payload_sha256,
                raw_uri=raw_uri,
                observed_at=observed_at,
                created_at=now,
            )
            inserted_count, unchanged_count = self._activate_catalog_entries(
                connection, entries=entries, now=now
            )
            data_version = self._publish_dataset(
                connection,
                dataset=_CATALOG_DATASET,
                partition_key=scheme.value,
                changed_count=inserted_count,
                published_at=now,
            )
        return PublishedSectorCatalog(
            data_version=data_version,
            inserted_count=inserted_count,
            unchanged_count=unchanged_count,
        )

    def get_sector(self, sector_id: UUID) -> StoredSector | None:
        """按公开 UUID 返回一个标准板块身份，不连接供应商专有表。"""
        statement = select(
            SectorEntity.sector_key,
            SectorEntity.sector_id,
            SectorEntity.scheme,
            SectorEntity.sector_code,
            SectorEntity.name,
            SectorEntity.status,
        ).where(SectorEntity.sector_id == sector_id)
        with self._database.session() as connection:
            row = connection.execute(statement).mappings().one_or_none()
        return None if row is None else _stored_sector(row)

    def get_sector_by_identifier(self, identifier: SectorIdentifier) -> StoredSector | None:
        """按分类体系和代码返回一个标准板块身份，不连接供应商专有表。"""
        statement = select(
            SectorEntity.sector_key,
            SectorEntity.sector_id,
            SectorEntity.scheme,
            SectorEntity.sector_code,
            SectorEntity.name,
            SectorEntity.status,
        ).where(
            SectorEntity.scheme == identifier.scheme.value,
            SectorEntity.sector_code == identifier.code,
        )
        with self._database.session() as connection:
            row = connection.execute(statement).mappings().one_or_none()
        return None if row is None else _stored_sector(row)

    def list_active_sectors(
        self,
        *,
        scheme: SectorScheme,
        query: str | None,
        after_code: str | None,
        after_sector_id: UUID | None,
        limit: int,
    ) -> Sequence[StoredSector]:
        """按代码、UUID 稳定排序读取一个分类体系的可公开目录页。"""
        if not 1 <= limit <= 101:
            raise ValueError("limit must be from 1 to 101")
        if (after_code is None) != (after_sector_id is None):
            raise ValueError("cursor code and sector id must be supplied together")
        normalized_query = None if query is None else query.strip()
        if normalized_query == "":
            normalized_query = None
        statement = select(
            SectorEntity.sector_key,
            SectorEntity.sector_id,
            SectorEntity.scheme,
            SectorEntity.sector_code,
            SectorEntity.name,
            SectorEntity.status,
        ).where(
            SectorEntity.scheme == scheme.value,
            SectorEntity.status == "ACTIVE",
        )
        if normalized_query is not None:
            pattern = f"{normalized_query}%"
            statement = statement.where(
                or_(
                    SectorEntity.sector_code.ilike(pattern),
                    SectorEntity.name.ilike(pattern),
                )
            )
        if after_code is not None and after_sector_id is not None:
            statement = statement.where(
                or_(
                    SectorEntity.sector_code > after_code,
                    (SectorEntity.sector_code == after_code)
                    & (SectorEntity.sector_id > after_sector_id),
                )
            )
        statement = statement.order_by(SectorEntity.sector_code, SectorEntity.sector_id).limit(
            limit
        )
        with self._database.session() as connection:
            rows = connection.execute(statement).mappings().all()
        return tuple(_stored_sector(row) for row in rows)

    def get_current_publication(
        self, *, dataset: str, partition_key: str
    ) -> DatasetPublication | None:
        """读取未被替代的数据集发布；不存在时不把未发布数据暴露给读取方。"""
        statement = select(
            DatasetPublicationModel.data_version,
            DatasetPublicationModel.published_at,
        ).where(
            DatasetPublicationModel.dataset == dataset,
            DatasetPublicationModel.partition_key == partition_key,
            DatasetPublicationModel.superseded_at.is_(None),
        )
        with self._database.session() as connection:
            row = connection.execute(statement).mappings().one_or_none()
        if row is None:
            return None
        return DatasetPublication(
            data_version=UUID(str(row["data_version"])), published_at=row["published_at"]
        )

    def list_bars(
        self,
        *,
        sector_id: UUID,
        period: SectorPeriod,
        start: date,
        end: date,
    ) -> Sequence[tuple[SectorBar, int, bool]]:
        """按周期结束日升序读取指定物理表中的当前修订。"""
        if start > end:
            raise ValueError("start must not be after end")
        model = _model_for_period(period)
        statement = (
            select(
                model.period_end,
                model.open_price,
                model.high_price,
                model.low_price,
                model.close_price,
                model.volume_value,
                model.volume_unit,
                model.amount_cny,
                model.amplitude_percent,
                model.change_percent,
                model.change_amount,
                model.turnover_percent,
                model.revision,
                model.is_final,
            )
            .join(SectorEntity, SectorEntity.sector_key == model.sector_key)
            .where(
                SectorEntity.sector_id == sector_id,
                model.period_end >= start,
                model.period_end <= end,
                model.valid_to.is_(None),
            )
            .order_by(model.period_end)
        )
        with self._database.session() as connection:
            rows = connection.execute(statement).mappings().all()
        return tuple(
            (
                SectorBar(
                    period_end=row["period_end"],
                    open_price=Decimal(row["open_price"]),
                    high_price=Decimal(row["high_price"]),
                    low_price=Decimal(row["low_price"]),
                    close_price=Decimal(row["close_price"]),
                    volume_value=Decimal(row["volume_value"]),
                    volume_unit=str(row["volume_unit"]),
                    amount_cny=Decimal(row["amount_cny"]),
                    amplitude_percent=_decimal_or_none(row["amplitude_percent"]),
                    change_percent=_decimal_or_none(row["change_percent"]),
                    change_amount=_decimal_or_none(row["change_amount"]),
                    turnover_percent=_decimal_or_none(row["turnover_percent"]),
                ),
                int(row["revision"]),
                bool(row["is_final"]),
            )
            for row in rows
        )

    def _ensure_sector(
        self,
        connection: Session,
        *,
        identifier: SectorIdentifier,
        now: datetime,
    ) -> StoredSector:
        """在目录同步前以 `PENDING` 身份创建最小板块占位记录。"""
        existing = (
            connection.execute(
                select(
                    SectorEntity.sector_key,
                    SectorEntity.sector_id,
                    SectorEntity.scheme,
                    SectorEntity.sector_code,
                    SectorEntity.name,
                    SectorEntity.status,
                ).where(
                    SectorEntity.scheme == identifier.scheme.value,
                    SectorEntity.sector_code == identifier.code,
                )
            )
            .mappings()
            .one_or_none()
        )
        if existing is not None:
            return _stored_sector(existing)
        sector_id = uuid4()
        # 行情输入只有稳定代码；目录任务才有权确认名称、状态或分类层级。
        connection.execute(
            insert(SectorEntity).values(
                sector_id=sector_id,
                scheme=identifier.scheme.value,
                sector_code=identifier.code,
                name=None,
                status="PENDING",
                created_at=now,
                updated_at=now,
            )
        )
        created = (
            connection.execute(
                select(
                    SectorEntity.sector_key,
                    SectorEntity.sector_id,
                    SectorEntity.scheme,
                    SectorEntity.sector_code,
                    SectorEntity.name,
                    SectorEntity.status,
                ).where(SectorEntity.sector_id == sector_id)
            )
            .mappings()
            .one()
        )
        return _stored_sector(created)

    def _activate_catalog_entries(
        self,
        connection: Session,
        *,
        entries: Sequence[SectorCatalogEntry],
        now: datetime,
    ) -> tuple[int, int]:
        """逐条激活目录项，仅在名称或状态变化时修改 canonical 身份。"""
        inserted_count = 0
        unchanged_count = 0
        for entry in entries:
            existing = (
                connection.execute(
                    select(
                        SectorEntity.sector_key,
                        SectorEntity.name,
                        SectorEntity.status,
                    ).where(
                        SectorEntity.scheme == entry.identifier.scheme.value,
                        SectorEntity.sector_code == entry.identifier.code,
                    )
                )
                .mappings()
                .one_or_none()
            )
            if existing is None:
                connection.execute(
                    insert(SectorEntity).values(
                        sector_id=uuid4(),
                        scheme=entry.identifier.scheme.value,
                        sector_code=entry.identifier.code,
                        name=entry.name,
                        status="ACTIVE",
                        created_at=now,
                        updated_at=now,
                    )
                )
                inserted_count += 1
                continue
            if existing["name"] == entry.name and existing["status"] == "ACTIVE":
                unchanged_count += 1
                continue
            # 行情先创建的 PENDING 身份保留 UUID，只提升目录确认后的名称与状态。
            connection.execute(
                update(SectorEntity)
                .where(SectorEntity.sector_key == existing["sector_key"])
                .values(name=entry.name, status="ACTIVE", updated_at=now)
            )
            inserted_count += 1
        return inserted_count, unchanged_count

    def _record_source_batch(
        self,
        connection: Session,
        *,
        provider_id: str,
        capability: str,
        source_payload_sha256: str,
        raw_uri: str,
        observed_at: datetime,
        created_at: datetime,
    ) -> UUID:
        """登记独立外部观测；同内容重抓仍保留完整血缘。"""
        return record_source_observation(
            connection,
            provider_id=provider_id,
            capability=capability,
            source_payload_sha256=source_payload_sha256,
            raw_uri=raw_uri,
            observed_at=observed_at,
            created_at=created_at,
        )

    def _write_revisions(
        self,
        connection: Session,
        *,
        sector_key: int,
        period: SectorPeriod,
        bars: Sequence[SectorBar],
        source_batch_id: UUID,
        observed_at: datetime,
    ) -> tuple[int, int]:
        """仅为值或终态变化的周期结束日关闭当前行并追加后继修订。"""
        model = _model_for_period(period)
        inserted_count = 0
        unchanged_count = 0
        observed_date = observed_at.astimezone(_SHANGHAI).date()
        for bar in bars:
            # 没有交易日历时，对观测当日及未来端点保守标记为未终态，后续重跑再升级。
            is_final = bar.period_end < observed_date
            content_hash = _bar_content_hash(bar, is_final=is_final)
            current = (
                connection.execute(
                    select(model.revision, model.content_sha256).where(
                        model.sector_key == sector_key,
                        model.period_end == bar.period_end,
                        model.valid_to.is_(None),
                    )
                )
                .mappings()
                .one_or_none()
            )
            if current is not None and current["content_sha256"] == content_hash:
                unchanged_count += 1
                continue
            revision = 1 if current is None else int(current["revision"]) + 1
            if current is not None:
                # 已发布观测绝不覆盖；先闭合旧行才能保持单一当前修订不变量。
                connection.execute(
                    update(model)
                    .where(
                        model.sector_key == sector_key,
                        model.period_end == bar.period_end,
                        model.valid_to.is_(None),
                    )
                    .values(valid_to=observed_at)
                )
            connection.execute(
                insert(model).values(
                    sector_key=sector_key,
                    period_end=bar.period_end,
                    revision=revision,
                    open_price=bar.open_price,
                    high_price=bar.high_price,
                    low_price=bar.low_price,
                    close_price=bar.close_price,
                    volume_value=bar.volume_value,
                    volume_unit=bar.volume_unit,
                    amount_cny=bar.amount_cny,
                    amplitude_percent=bar.amplitude_percent,
                    change_percent=bar.change_percent,
                    change_amount=bar.change_amount,
                    turnover_percent=bar.turnover_percent,
                    is_final=is_final,
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
        identifier: SectorIdentifier,
        period: SectorPeriod,
        inserted_count: int,
        published_at: datetime,
    ) -> UUID:
        """仅在当前标准视图变化时推进该分类体系、代码、周期的发布版本。"""
        return self._publish_dataset(
            connection,
            dataset=period.capability,
            partition_key=identifier.qualified_key,
            changed_count=inserted_count,
            published_at=published_at,
        )

    def _publish_dataset(
        self,
        connection: Session,
        *,
        dataset: str,
        partition_key: str,
        changed_count: int,
        published_at: datetime,
    ) -> UUID:
        """仅在某个数据集分区当前视图变化时推进其发布版本。"""
        if changed_count == 0:
            existing = connection.execute(
                select(DatasetPublicationModel.data_version).where(
                    DatasetPublicationModel.dataset == dataset,
                    DatasetPublicationModel.partition_key == partition_key,
                    DatasetPublicationModel.superseded_at.is_(None),
                )
            ).scalar_one_or_none()
            if existing is not None:
                return UUID(str(existing))
        connection.execute(
            update(DatasetPublicationModel)
            .where(
                DatasetPublicationModel.dataset == dataset,
                DatasetPublicationModel.partition_key == partition_key,
                DatasetPublicationModel.superseded_at.is_(None),
            )
            .values(superseded_at=published_at)
        )
        data_version = uuid4()
        connection.execute(
            insert(DatasetPublicationModel).values(
                publication_id=uuid4(),
                dataset=dataset,
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


def _model_for_period(
    period: SectorPeriod,
) -> type[SectorDailyBar | SectorWeeklyBar | SectorMonthlyBar]:
    """从封闭枚举选择对应 ORM 模型，杜绝外部输入插入 SQL 标识符。"""
    return _MODEL_BY_PERIOD[period]


def _stored_sector(row: Mapping[Any, Any]) -> StoredSector:
    """将 SQL 映射行转换为无供应商依赖的标准板块身份。"""
    return StoredSector(
        sector_key=int(row["sector_key"]),
        sector_id=UUID(str(row["sector_id"])),
        identifier=SectorIdentifier(
            scheme=SectorScheme(str(row["scheme"])), code=str(row["sector_code"])
        ),
        name=None if row["name"] is None else str(row["name"]),
        status=str(row["status"]),
    )


def _decimal_or_none(value: object) -> Decimal | None:
    """将数据库可空数值字段转换为领域使用的精确小数。"""
    return None if value is None else Decimal(str(value))


def _bar_content_hash(bar: SectorBar, *, is_final: bool) -> bytes:
    """对业务字段和终态计算摘要，避免重放制造伪修订。"""
    serialized = json.dumps(
        {
            "periodEnd": bar.period_end.isoformat(),
            "open": str(bar.open_price),
            "high": str(bar.high_price),
            "low": str(bar.low_price),
            "close": str(bar.close_price),
            "volumeValue": str(bar.volume_value),
            "volumeUnit": bar.volume_unit,
            "amountCny": str(bar.amount_cny),
            "amplitudePercent": _decimal_text_or_none(bar.amplitude_percent),
            "changePercent": _decimal_text_or_none(bar.change_percent),
            "changeAmount": _decimal_text_or_none(bar.change_amount),
            "turnoverPercent": _decimal_text_or_none(bar.turnover_percent),
            "isFinal": is_final,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(serialized).digest()


def _decimal_text_or_none(value: Decimal | None) -> str | None:
    """把可空精确数值稳定投影为摘要计算用的 JSON 标量。"""
    return None if value is None else str(value)
