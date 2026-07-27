"""共享来源观测账本的独立身份回归测试。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy.engine import Connection

from service_data_sync.infrastructure.persistence.source_batch import record_source_observation


class FakeResult:
    """提供来源账本写入器读取 `RETURNING` 行所需的最小接口。"""

    def __init__(self, row: dict[str, Any]) -> None:
        """保存每次写入对应的伪造数据库返回行。"""
        self._row = row

    def mappings(self) -> FakeResult:
        """返回已经是映射结构的测试结果。"""
        return self

    def one(self) -> dict[str, Any]:
        """模拟 `RETURNING source_batch_id` 的单行结果。"""
        return self._row


class RecordingConnection:
    """记录 CTE 写入语句与参数，不连接 PostgreSQL。"""

    def __init__(self) -> None:
        """初始化空的 SQL 调用记录。"""
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def execute(self, statement: object, parameters: dict[str, Any]) -> FakeResult:
        """记录一次事务内账本写入并回传调用方生成的批次标识。"""
        self.calls.append((str(statement), parameters))
        return FakeResult({"source_batch_id": parameters["source_batch_id"]})


def test_same_payload_creates_distinct_source_observations() -> None:
    """相同内容连续获取必须保留不同 batch、run 与 request key。"""
    connection = RecordingConnection()
    observed_at = datetime(2026, 7, 27, 9, tzinfo=UTC)

    first = record_source_observation(
        cast(Connection, connection),
        provider_id="fixture-provider",
        capability="equity.bar.1d.raw",
        source_payload_sha256="a" * 64,
        raw_uri="s3://fixture/payload.json",
        observed_at=observed_at,
        created_at=observed_at,
    )
    second = record_source_observation(
        cast(Connection, connection),
        provider_id="fixture-provider",
        capability="equity.bar.1d.raw",
        source_payload_sha256="a" * 64,
        raw_uri="s3://fixture/payload.json",
        observed_at=observed_at,
        created_at=observed_at,
    )

    first_sql, first_parameters = connection.calls[0]
    _, second_parameters = connection.calls[1]
    assert first != second
    assert first_parameters["run_id"] != second_parameters["run_id"]
    assert first_parameters["request_key"] != second_parameters["request_key"]
    assert first_parameters["payload_sha256"] == second_parameters["payload_sha256"]
    assert "ON CONFLICT" not in first_sql
    assert "INSERT INTO sync_run" in first_sql
    assert "INSERT INTO sync_partition" in first_sql


def test_existing_run_partition_appends_source_evidence_without_creating_new_run() -> None:
    """任务编排已持有 lease 时，来源证据必须沿用该 run/partition 并递增观测序号。"""
    connection = RecordingConnection()
    observed_at = datetime(2026, 7, 27, 9, tzinfo=UTC)
    run_id = uuid4()

    source_batch_id = record_source_observation(
        cast(Connection, connection),
        provider_id="fixture-provider",
        capability="sector.membership.snapshot.raw",
        source_payload_sha256="a" * 64,
        raw_uri="s3://fixture/payload.json",
        observed_at=observed_at,
        created_at=observed_at,
        run_id=run_id,
        partition_key="eastmoney.industry:BK0475:2026-07-27",
    )

    sql, parameters = connection.calls[0]
    assert source_batch_id == parameters["source_batch_id"]
    assert parameters["run_id"] == run_id
    assert "INSERT INTO sync_run" not in sql
    assert "MAX(observation_seq) + 1" in sql


def test_source_observation_requires_complete_execution_context() -> None:
    """run 与 partition 缺一不可，避免来源 batch 指向不存在或错误恢复边界。"""
    connection = RecordingConnection()
    observed_at = datetime(2026, 7, 27, 9, tzinfo=UTC)

    with pytest.raises(ValueError, match="supplied together"):
        record_source_observation(
            cast(Connection, connection),
            provider_id="fixture-provider",
            capability="sector.membership.snapshot.raw",
            source_payload_sha256="a" * 64,
            raw_uri="s3://fixture/payload.json",
            observed_at=observed_at,
            created_at=observed_at,
            run_id=uuid4(),
        )
