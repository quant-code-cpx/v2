"""共享来源观测账本的 SQL 写入器。"""

from __future__ import annotations

import hashlib
from datetime import datetime
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.engine import Connection

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_UNVERSIONED_ADAPTER = "unversioned"


def record_source_observation(
    connection: Connection,
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
    run_id: UUID | None = None,
    partition_key: str | None = None,
) -> UUID:
    """登记不可折叠来源观测；任务已存在时复用其 run/partition，否则创建手工账本。"""
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
            connection,
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
            run_id=run_id,
            partition_key=partition_key,
        )
    manual_run_id = uuid4()
    manual_partition_key = f"manual:{capability}:{source_batch_id}"
    row = (
        connection.execute(
            text(
                """
                WITH created_run AS (
                  INSERT INTO sync_run (
                    run_id, capability, mode, request_key, target_date, status,
                    requested_at, started_at, finished_at, created_at
                  ) VALUES (
                    :run_id, :capability, 'manual', :request_key, :target_date, 'succeeded',
                    :created_at, :created_at, :created_at, :created_at
                  )
                  RETURNING run_id
                ), created_partition AS (
                  INSERT INTO sync_partition (
                    run_id, partition_key, status, attempt, updated_at
                  )
                  SELECT run_id, :partition_key, 'succeeded', 1, :created_at
                  FROM created_run
                  RETURNING run_id
                )
                INSERT INTO source_batch (
                  source_batch_id, provider_id, capability, payload_sha256, raw_uri,
                  observed_at, created_at, run_id, partition_key, observation_seq,
                  upstream_source, adapter_version, schema_fingerprint
                )
                SELECT
                  :source_batch_id, :provider_id, :capability, :payload_sha256, :raw_uri,
                  :observed_at, :created_at, run_id, :partition_key, 1,
                  :upstream_source, :adapter_version, :schema_fingerprint
                FROM created_partition
                RETURNING source_batch_id
                """
            ),
            {
                "source_batch_id": source_batch_id,
                "run_id": manual_run_id,
                "capability": capability,
                "request_key": f"source-batch:{source_batch_id}",
                "target_date": observed_at.astimezone(_SHANGHAI).date(),
                "partition_key": manual_partition_key,
                "provider_id": provider_id,
                "payload_sha256": source_payload_sha256,
                "raw_uri": raw_uri,
                "observed_at": observed_at,
                "created_at": created_at,
                "upstream_source": upstream_source or provider_id,
                "adapter_version": adapter_version,
                "schema_fingerprint": resolved_schema_fingerprint,
            },
        )
        .mappings()
        .one()
    )
    return UUID(str(row["source_batch_id"]))


def _record_in_existing_partition(
    connection: Connection,
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
    run_id: UUID,
    partition_key: str,
) -> UUID:
    """在已有 lease 分区中追加来源观测序号，避免重跑覆盖原始证据。"""
    row = (
        connection.execute(
            text(
                """
                INSERT INTO source_batch (
                  source_batch_id, provider_id, capability, payload_sha256, raw_uri,
                  observed_at, created_at, run_id, partition_key, observation_seq,
                  upstream_source, adapter_version, schema_fingerprint
                ) VALUES (
                  :source_batch_id, :provider_id, :capability, :payload_sha256, :raw_uri,
                  :observed_at, :created_at, :run_id, :partition_key,
                  COALESCE((
                    SELECT MAX(observation_seq) + 1
                    FROM source_batch
                    WHERE run_id = :run_id AND partition_key = :partition_key
                  ), 1),
                  :upstream_source, :adapter_version, :schema_fingerprint
                )
                RETURNING source_batch_id
                """
            ),
            {
                "source_batch_id": source_batch_id,
                "provider_id": provider_id,
                "capability": capability,
                "payload_sha256": source_payload_sha256,
                "raw_uri": raw_uri,
                "observed_at": observed_at,
                "created_at": created_at,
                "run_id": run_id,
                "partition_key": partition_key,
                "upstream_source": upstream_source,
                "adapter_version": adapter_version,
                "schema_fingerprint": schema_fingerprint,
            },
        )
        .mappings()
        .one()
    )
    return UUID(str(row["source_batch_id"]))
