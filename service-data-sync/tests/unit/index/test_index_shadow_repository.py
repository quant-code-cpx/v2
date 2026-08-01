"""指数影子仓储确定性规范化运行的无数据库单元测试。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast
from uuid import UUID

from sqlalchemy.orm import Session

from service_data_sync.application.ports.index_shadow import IndexShadowSourceObservation
from service_data_sync.infrastructure.persistence import index_shadow_repository


class _ScalarResult:
    """模拟本测试所需的最小 SQLAlchemy 标量查询结果。"""

    def __init__(self, value: UUID | None) -> None:
        """保存预设的插入或查询结果。"""
        self._value = value

    def scalar_one_or_none(self) -> UUID | None:
        """返回可选插入结果，冲突时对应空值。"""
        return self._value

    def scalar_one(self) -> UUID:
        """返回已存在的唯一规范化运行标识。"""
        if self._value is None:
            raise AssertionError("测试预设缺少既有规范化运行标识")
        return self._value


class _RecordingSession:
    """记录仓储构造的 SQL 语句，并按顺序返回冲突或既有运行结果。"""

    def __init__(self, results: list[UUID | None]) -> None:
        """接收每次 `execute` 应返回的确定性标量结果。"""
        self._results = results
        self.statements: list[object] = []

    def execute(self, statement: object) -> _ScalarResult:
        """保存 SQL 语句并返回预设结果，避免单元测试连接 PostgreSQL。"""
        self.statements.append(statement)
        return _ScalarResult(self._results.pop(0))


def test_record_normalization_reuses_existing_run_after_unique_conflict() -> None:
    """相同输入冲突后必须查询并复用既有运行，不能再次写入质量结论。"""
    existing_run_id = UUID("10000000-0000-4000-8000-000000000001")
    session = _RecordingSession([None, existing_run_id])

    normalization_run_id, created = index_shadow_repository._record_normalization(
        cast(Session, session),
        dataset_id=UUID("10000000-0000-4000-8000-000000000002"),
        run_id=UUID("10000000-0000-4000-8000-000000000003"),
        partition_key="CNI:399002:weight_snapshot",
        source=_source(raw_payload_sha256="a" * 64, normalized_payload_sha256="b" * 64),
        now=datetime(2026, 8, 1, 8, tzinfo=UTC),
    )

    assert normalization_run_id == existing_run_id
    assert created is False
    assert len(session.statements) == 2
    rendered_insert = str(session.statements[0])
    assert "ON CONFLICT" in rendered_insert
    assert "DO NOTHING" in rendered_insert


def test_normalization_input_set_hash_changes_when_either_payload_changes() -> None:
    """raw 或标准载荷任一摘要变化都必须产生不同重放身份。"""
    baseline = index_shadow_repository._normalization_input_set_hash(
        _source(raw_payload_sha256="a" * 64, normalized_payload_sha256="b" * 64)
    )
    raw_changed = index_shadow_repository._normalization_input_set_hash(
        _source(raw_payload_sha256="c" * 64, normalized_payload_sha256="b" * 64)
    )
    normalized_changed = index_shadow_repository._normalization_input_set_hash(
        _source(raw_payload_sha256="a" * 64, normalized_payload_sha256="d" * 64)
    )

    assert baseline != raw_changed
    assert baseline != normalized_changed


def _source(
    *, raw_payload_sha256: str, normalized_payload_sha256: str
) -> IndexShadowSourceObservation:
    """构造只包含确定性输入身份的最小指数来源观察。"""
    return IndexShadowSourceObservation(
        provider_id="unit-index",
        capability="index.weight.snapshot",
        raw_payload_sha256=raw_payload_sha256,
        raw_uri=f"unretained://sha256/{raw_payload_sha256}",
        raw_content_type="application/json",
        raw_byte_size=1,
        normalized_payload_sha256=normalized_payload_sha256,
        normalized_uri=f"unretained://sha256/{normalized_payload_sha256}",
        normalized_content_type="application/json",
        normalized_byte_size=1,
        observed_at=datetime(2026, 8, 1, 8, tzinfo=UTC),
        upstream_source="cnindex",
        adapter_version="unit-v1",
        schema_fingerprint="e" * 64,
    )
