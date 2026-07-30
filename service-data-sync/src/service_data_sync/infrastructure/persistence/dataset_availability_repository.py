"""基于 `PostgreSQL` 的空集与来源不可用观测仓储。

“没有事实”和“本次无法从来源取得事实”都不是零行 `canonical release`，而是独立的
可用性观察。它们按数据集和精确分区版本化，真实事实成功发布后必须终结旧观察，避免
消费者把历史空集误读为当前状态。
"""

from __future__ import annotations

from datetime import date, datetime
from uuid import uuid4

from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert

from service_data_sync.application.ports.dataset_availability import (
    DatasetAvailability,
    DatasetAvailabilityRepository,
)
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.database.models.publication.dataset_availability_observation import (  # noqa: E501
    DatasetAvailabilityObservation,
)


class SqlAlchemyDatasetAvailabilityRepository(DatasetAvailabilityRepository):
    """将空同步结果保存为独立元数据，不干扰既有 `canonical release`。

    该仓储不创建虚构事实、不会替代当前 `publication`，只提供可审计的执行结论。
    """

    def __init__(self, database: DatabaseClient) -> None:
        """保存服务私有数据库事务工厂，不向应用层暴露 SQLAlchemy 细节。"""
        self._database = database

    def record(
        self,
        *,
        dataset: str,
        partition_key: str,
        availability: str,
        reason_code: str,
        provider_id: str | None,
        observed_at: datetime,
        entity_partition: str | None = None,
        coverage_from: date | None = None,
        coverage_to: date | None = None,
    ) -> DatasetAvailability:
        """原子替换同分区当前观测；相同来源时刻重试保持幂等。

        三种状态必须由调用方显式区分；暂不支持和来源故障都不能被消费端当成数据零值。
        """
        if availability not in {"empty", "source_unavailable", "currently_unsupported"}:
            raise ValueError("dataset availability is invalid")
        if not dataset.strip() or not partition_key.strip() or not reason_code.strip():
            raise ValueError("dataset availability identity is invalid")
        if observed_at.tzinfo is None:
            raise ValueError("observed_at must include a timezone")
        coverage_values = (entity_partition, coverage_from, coverage_to)
        if any(value is None for value in coverage_values) != all(
            value is None for value in coverage_values
        ):
            raise ValueError("dataset availability coverage must be complete or absent")
        if entity_partition is not None and (
            not entity_partition.strip()
            or len(entity_partition) > 160
            or coverage_from is None
            or coverage_to is None
            or coverage_from > coverage_to
        ):
            raise ValueError("dataset availability coverage is invalid")
        with self._database.transaction() as connection:
            # 先结束旧观察，再写新观察，使任意时刻最多只有一条当前可用性结论。
            connection.execute(
                update(DatasetAvailabilityObservation)
                .where(
                    DatasetAvailabilityObservation.dataset == dataset,
                    DatasetAvailabilityObservation.partition_key == partition_key,
                    DatasetAvailabilityObservation.superseded_at.is_(None),
                )
                .values(superseded_at=observed_at)
            )
            connection.execute(
                postgresql_insert(DatasetAvailabilityObservation)
                .values(
                    observation_id=uuid4(),
                    dataset=dataset,
                    partition_key=partition_key,
                    entity_partition=entity_partition,
                    coverage_from=coverage_from,
                    coverage_to=coverage_to,
                    availability=availability,
                    reason_code=reason_code,
                    provider_id=provider_id,
                    observed_at=observed_at,
                    superseded_at=None,
                    detail=None,
                )
                .on_conflict_do_update(
                    constraint="uq_dataset_availability_observation_time",
                    set_={
                        "availability": availability,
                        "reason_code": reason_code,
                        "provider_id": provider_id,
                        "entity_partition": entity_partition,
                        "coverage_from": coverage_from,
                        "coverage_to": coverage_to,
                        "superseded_at": None,
                        "detail": None,
                    },
                )
            )
        return DatasetAvailability(
            availability=availability,
            reason_code=reason_code,
            observed_at=observed_at,
            entity_partition=entity_partition,
            coverage_from=coverage_from,
            coverage_to=coverage_to,
        )

    def clear(self, *, dataset: str, partition_key: str, cleared_at: datetime) -> None:
        """成功发布事实后终结旧观测，防止消费者被过期空状态遮蔽。"""
        if not dataset.strip() or not partition_key.strip():
            raise ValueError("dataset availability identity is invalid")
        if cleared_at.tzinfo is None:
            raise ValueError("cleared_at must include a timezone")
        with self._database.transaction() as connection:
            connection.execute(
                update(DatasetAvailabilityObservation)
                .where(
                    DatasetAvailabilityObservation.dataset == dataset,
                    DatasetAvailabilityObservation.partition_key == partition_key,
                    DatasetAvailabilityObservation.superseded_at.is_(None),
                )
                .values(superseded_at=cleared_at)
            )
