"""共享来源观测账本的独立身份回归测试。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session
from sqlalchemy.sql import ClauseElement

from service_data_sync.infrastructure.persistence.source_batch import record_source_observation


class FakeResult:
    """提供来源账本写入器读取 `RETURNING` 行所需的最小接口。"""

    def __init__(self) -> None:
        """为每个模拟的 `RETURNING` 调用生成独立来源批次标识。"""
        self._source_batch_id = uuid4()

    def scalar_one(self) -> object:
        """模拟 ORM-enabled `RETURNING source_batch_id` 的标量读取。"""
        return self._source_batch_id


class RecordingConnection:
    """记录 CTE 写入语句与参数，不连接 PostgreSQL。"""

    def __init__(self) -> None:
        """初始化空的 ORM statement 调用记录。"""
        self.calls: list[ClauseElement] = []

    def execute(self, statement: ClauseElement) -> FakeResult:
        """记录一次 ORM-enabled 写入并回传独立的来源批次标识。"""
        self.calls.append(statement)
        return FakeResult()


def test_same_payload_creates_distinct_source_observations() -> None:
    """相同内容连续获取必须保留不同 batch、run 与 request key。"""
    connection = RecordingConnection()
    observed_at = datetime(2026, 7, 27, 9, tzinfo=UTC)

    first = record_source_observation(
        cast(Session, connection),
        provider_id="fixture-provider",
        capability="equity.bar.1d.raw",
        source_payload_sha256="a" * 64,
        raw_uri="s3://fixture/payload.json",
        observed_at=observed_at,
        created_at=observed_at,
    )
    second = record_source_observation(
        cast(Session, connection),
        provider_id="fixture-provider",
        capability="equity.bar.1d.raw",
        source_payload_sha256="a" * 64,
        raw_uri="s3://fixture/payload.json",
        observed_at=observed_at,
        created_at=observed_at,
    )

    first_sql = _compile(connection.calls[0])
    second_sql = _compile(connection.calls[1])
    assert first != second
    assert "ON CONFLICT" not in first_sql
    assert "INSERT INTO sync_run" in first_sql
    assert "INSERT INTO sync_partition" in first_sql
    assert "fixture-provider" not in first_sql
    assert first_sql == second_sql


def test_existing_run_partition_appends_source_evidence_without_creating_new_run() -> None:
    """任务编排已持有 lease 时，来源证据必须沿用该 run/partition 并递增观测序号。"""
    connection = RecordingConnection()
    observed_at = datetime(2026, 7, 27, 9, tzinfo=UTC)
    run_id = uuid4()

    source_batch_id = record_source_observation(
        cast(Session, connection),
        provider_id="fixture-provider",
        capability="sector.membership.snapshot.raw",
        source_payload_sha256="a" * 64,
        raw_uri="s3://fixture/payload.json",
        observed_at=observed_at,
        created_at=observed_at,
        run_id=run_id,
        partition_key="eastmoney.industry:BK0475:2026-07-27",
    )

    sql = _compile(connection.calls[0])
    assert source_batch_id
    assert "INSERT INTO sync_run" not in sql
    assert "max(source_batch.observation_seq)" in sql


def test_source_observation_optionally_links_real_source_dataset() -> None:
    """新能力可以把 adapter 观察关联到真实上游产品，旧调用仍保持兼容。"""
    connection = RecordingConnection()
    observed_at = datetime(2026, 7, 27, 9, tzinfo=UTC)

    record_source_observation(
        cast(Session, connection),
        provider_id="fixture-provider",
        capability="index.catalog.snapshot",
        source_payload_sha256="a" * 64,
        raw_uri="s3://fixture/payload.json",
        observed_at=observed_at,
        created_at=observed_at,
        source_dataset_id=uuid4(),
    )

    assert "source_dataset_id" in _compile(connection.calls[0])


def test_source_observation_requires_complete_execution_context() -> None:
    """run 与 partition 缺一不可，避免来源 batch 指向不存在或错误恢复边界。"""
    connection = RecordingConnection()
    observed_at = datetime(2026, 7, 27, 9, tzinfo=UTC)

    with pytest.raises(ValueError, match="supplied together"):
        record_source_observation(
            cast(Session, connection),
            provider_id="fixture-provider",
            capability="sector.membership.snapshot.raw",
            source_payload_sha256="a" * 64,
            raw_uri="s3://fixture/payload.json",
            observed_at=observed_at,
            created_at=observed_at,
            run_id=uuid4(),
        )


def _compile(statement: ClauseElement) -> str:
    """以 PostgreSQL 方言编译 ORM statement，断言其结构而非私有参数名。"""
    return str(statement.compile(dialect=postgresql.dialect()))
