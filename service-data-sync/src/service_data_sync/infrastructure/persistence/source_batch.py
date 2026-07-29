"""共享来源观测账本的 ORM-enabled 写入器。"""

from __future__ import annotations

import hashlib
from datetime import datetime
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import func, insert, literal, select
from sqlalchemy.orm import Session

from service_data_sync.infrastructure.database.models.execution.sync_partition import SyncPartition
from service_data_sync.infrastructure.database.models.execution.sync_run import SyncRun
from service_data_sync.infrastructure.database.models.provenance.source_batch import SourceBatch

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_UNVERSIONED_ADAPTER = "unversioned"


def record_source_observation(
    session: Session,
    *,
    provider_id: str,
    capability: str,
    source_payload_sha256: str,
    raw_uri: str,
    observed_at: datetime,
    created_at: datetime,
    upstream_source: str | None = None,
    adapter_version: str = _UNVERSIONED_ADAPTER,
    schema_fingerprint: str | None = None,
    source_dataset_id: UUID | None = None,
    run_id: UUID | None = None,
    partition_key: str | None = None,
) -> UUID:
    """登记不可折叠来源观测；既有 run 分区复用，否则创建手工账本。"""
    if observed_at.tzinfo is None or created_at.tzinfo is None:
        raise ValueError("source observation timestamps must include a timezone")
    if (run_id is None) != (partition_key is None):
        raise ValueError("run_id and partition_key must be supplied together")

    source_batch_id = uuid4()
    resolved_schema_fingerprint = (
        schema_fingerprint or hashlib.sha256(f"{capability}:{adapter_version}".encode()).hexdigest()
    )
    if run_id is not None and partition_key is not None:
        return _record_in_existing_partition(
            session,
            source_batch_id=source_batch_id,
            provider_id=provider_id,
            capability=capability,
            source_payload_sha256=source_payload_sha256,
            raw_uri=raw_uri,
            observed_at=observed_at,
            created_at=created_at,
            upstream_source=upstream_source or provider_id,
            adapter_version=adapter_version,
            schema_fingerprint=resolved_schema_fingerprint,
            source_dataset_id=source_dataset_id,
            run_id=run_id,
            partition_key=partition_key,
        )

    manual_run_id = uuid4()
    manual_partition_key = f"manual:{capability}:{source_batch_id}"
    created_run = (
        insert(SyncRun)
        .values(
            run_id=manual_run_id,
            capability=capability,
            mode="manual",
            request_key=f"source-batch:{source_batch_id}",
            target_date=observed_at.astimezone(_SHANGHAI).date(),
            status="succeeded",
            requested_at=created_at,
            started_at=created_at,
            finished_at=created_at,
            created_at=created_at,
        )
        .returning(SyncRun.run_id)
        .cte("created_run")
    )
    created_partition = (
        insert(SyncPartition)
        .from_select(
            ("run_id", "partition_key", "status", "attempt", "updated_at"),
            select(
                created_run.c.run_id,
                literal(manual_partition_key),
                literal("succeeded"),
                literal(1),
                literal(created_at),
            ),
        )
        .returning(SyncPartition.run_id)
        .cte("created_partition")
    )
    statement = (
        insert(SourceBatch)
        .from_select(
            (
                "source_batch_id",
                "provider_id",
                "capability",
                "source_dataset_id",
                "payload_sha256",
                "raw_uri",
                "observed_at",
                "created_at",
                "run_id",
                "partition_key",
                "observation_seq",
                "upstream_source",
                "adapter_version",
                "schema_fingerprint",
            ),
            select(
                literal(source_batch_id),
                literal(provider_id),
                literal(capability),
                literal(source_dataset_id),
                literal(source_payload_sha256),
                literal(raw_uri),
                literal(observed_at),
                literal(created_at),
                created_partition.c.run_id,
                literal(manual_partition_key),
                literal(1),
                literal(upstream_source or provider_id),
                literal(adapter_version),
                literal(resolved_schema_fingerprint),
            ),
        )
        .returning(SourceBatch.source_batch_id)
    )
    return UUID(str(session.execute(statement).scalar_one()))


def _record_in_existing_partition(
    session: Session,
    *,
    source_batch_id: UUID,
    provider_id: str,
    capability: str,
    source_payload_sha256: str,
    raw_uri: str,
    observed_at: datetime,
    created_at: datetime,
    upstream_source: str,
    adapter_version: str,
    schema_fingerprint: str,
    source_dataset_id: UUID | None,
    run_id: UUID,
    partition_key: str,
) -> UUID:
    """在已有 lease 分区中追加来源观测序号，避免重跑覆盖原始证据。"""
    next_observation_sequence = (
        select(func.coalesce(func.max(SourceBatch.observation_seq) + 1, 1))
        .where(
            SourceBatch.run_id == run_id,
            SourceBatch.partition_key == partition_key,
        )
        .scalar_subquery()
    )
    statement = (
        insert(SourceBatch)
        .values(
            source_batch_id=source_batch_id,
            provider_id=provider_id,
            capability=capability,
            source_dataset_id=source_dataset_id,
            payload_sha256=source_payload_sha256,
            raw_uri=raw_uri,
            observed_at=observed_at,
            created_at=created_at,
            run_id=run_id,
            partition_key=partition_key,
            observation_seq=next_observation_sequence,
            upstream_source=upstream_source,
            adapter_version=adapter_version,
            schema_fingerprint=schema_fingerprint,
        )
        .returning(SourceBatch.source_batch_id)
    )
    return UUID(str(session.execute(statement).scalar_one()))
