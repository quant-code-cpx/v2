"""股票行情回填分区的 coverage/dataVersion 双重门禁单元测试。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from types import SimpleNamespace
from typing import cast
from uuid import UUID, uuid4

import pytest

from service_data_sync.infrastructure.data_operations import (
    equity_backfill_checkpoint as checkpoint_module,
)
from service_data_sync.infrastructure.data_operations.control_plane import ExecutionClaim
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.database.fenced_execution import FencedExecution
from service_data_sync.infrastructure.database.models.canonical import DatasetRelease
from service_data_sync.infrastructure.database.models.operations import DataOperationRun
from service_data_sync.infrastructure.database.models.publication.dataset_publication import (
    DatasetPublication,
)


class _Rows:
    """模拟只暴露 `all` 的 SQLAlchemy 多行结果。"""

    def __init__(self, values: list[object]) -> None:
        """保存固定结果，避免测试依赖 PostgreSQL 以外的行为。"""
        self._values = values

    def all(self) -> list[object]:
        """返回已冻结的结果行。"""
        return self._values


class _RecordSession:
    """为分区封印准备最小 run、coverage、publication 与 release 读取替身。"""

    def __init__(
        self,
        *,
        run: object,
        coverage: object,
        publication: object,
        release: object,
    ) -> None:
        """保存与真实记录路径相同的四个权威读取结果。"""
        self._run = run
        self._coverage = coverage
        self._publication = publication
        self._release = release

    def get(self, model: object, _key: object, **_kwargs: object) -> object:
        """按模型返回预置权威行；意外读取说明测试断言已失效。"""
        if model is DataOperationRun:
            return self._run
        if model is DatasetPublication:
            return self._publication
        if model is DatasetRelease:
            return self._release
        raise AssertionError("unexpected record-session model read")

    def scalar(self, _statement: object) -> object:
        """返回唯一 coverage；错配必须在持久化 checkpoint 前被拒绝。"""
        return self._coverage

    def add(self, _value: object) -> None:
        """封印失败路径不得写入 checkpoint。"""
        raise AssertionError("mismatched coverage must not create a checkpoint")


class _FinalizerSession:
    """为最终封印提供已持久化 checkpoint 与 coverage 联合读取替身。"""

    def __init__(self, *, checkpoints: list[object], evidence_rows: list[object]) -> None:
        """保存 roster 读取和终态二次核验的精确联合行。"""
        self._checkpoints = checkpoints
        self._evidence_rows = evidence_rows

    def scalars(self, _statement: object) -> _Rows:
        """返回同一 child target 的已封印分区 roster。"""
        return _Rows(self._checkpoints)

    def execute(self, _statement: object) -> _Rows:
        """返回 coverage、publication、release 的最终验证联合行。"""
        return _Rows(self._evidence_rows)


class _Database:
    """以同一会话模拟 read/transaction 边界，聚焦应用层独立验证。"""

    def __init__(self, session: object) -> None:
        """保存全部读取都使用的固定会话。"""
        self._session = session

    @contextmanager
    def session(self) -> Iterator[object]:
        """提供只读 roster 查询上下文。"""
        yield self._session

    @contextmanager
    def transaction(self) -> Iterator[object]:
        """提供最终校验和终态写入上下文。"""
        yield self._session


class _Execution:
    """记录 finalizer 必需的 fencing、进度和内存终态，不模拟数据库锁细节。"""

    def __init__(self) -> None:
        """初始化未武装的终态和空来源集合。"""
        self.source_batch_ids: list[UUID] = []
        self.terminal_armed = False
        self.terminal_written = False
        self.completed_partitions = 0
        self.processed_records = 0
        self.checkpoint_kind: str | None = None
        self.checkpoint_position: str | None = None

    def assert_current(self, _session: object) -> None:
        """测试已隔离数据库 lease，仅确认最终校验发生在 fenced 事务内。"""

    def record_source_batch(self, source_batch_id: UUID) -> None:
        """记录最终 child 实际使用的来源批次。"""
        self.source_batch_ids.append(source_batch_id)

    def record_checkpoint(self, *, kind: str, position: str) -> None:
        """保存终态将引用的 coverage checkpoint。"""
        self.checkpoint_kind = kind
        self.checkpoint_position = position

    def arm_terminal_write(self) -> None:
        """模拟最终写入已武装。"""
        self.terminal_armed = True

    def finalize_if_armed(self, _session: object) -> None:
        """仅在所有验证通过后模拟写入成功终态。"""
        if self.terminal_armed:
            self.terminal_written = True

    def rollback_terminal_write(self) -> None:
        """模拟最终证据失败后清除内存终态标记。"""
        self.terminal_armed = False
        self.terminal_written = False


def test_record_partition_rejects_coverage_data_version_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """分区封印须独立拒绝 coverage 与 publication 匹配、但调用 dataVersion 不匹配的伪证据。"""
    claim, identity_version_id = _claim()
    source_batch_id = uuid4()
    published_data_version = uuid4()
    coverage = SimpleNamespace(
        capability=claim.dataset_code,
        period="1d",
        security_id=9_300_000_001,
        identifier_version_id=identity_version_id,
        coverage_from=date(2026, 7, 1),
        coverage_to=date(2026, 7, 31),
        publication_id=uuid4(),
        # 故意提供不同版本，确认应用层不依赖数据库触发器兜底。
        data_version=uuid4(),
        source_batch_id=source_batch_id,
        publication_kind="DATA",
        record_count=1,
    )
    publication = SimpleNamespace(
        publication_id=coverage.publication_id,
        dataset=claim.dataset_code,
        data_version=published_data_version,
        quality_status="passed",
        release_id=uuid4(),
    )
    release = SimpleNamespace(release_id=publication.release_id)
    session = _RecordSession(
        run=SimpleNamespace(
            fencing_token=claim.fencing_token,
            dataset_code=claim.dataset_code,
            target_index=0,
        ),
        coverage=coverage,
        publication=publication,
        release=release,
    )
    database = _Database(session)
    monkeypatch.setattr(
        checkpoint_module,
        "_binding",
        lambda _database, *, claim: checkpoint_module._Binding(
            child_id=uuid4(), command_id=uuid4()
        ),
    )

    with pytest.raises(RuntimeError, match="output cannot be verified"):
        checkpoint_module.record_equity_bar_partition(
            cast(DatabaseClient, database),
            claim=claim,
            execution=cast(FencedExecution, _Execution()),
            partition_key="equity.bar.1d.raw:SSE:600519:2026-07-01:2026-07-31",
            window_from=date(2026, 7, 1),
            window_to=date(2026, 7, 31),
            coverage_version=uuid4(),
            data_version=published_data_version,
            source_batch_ids=(source_batch_id,),
            publication_kind="DATA",
            record_count=1,
        )


def test_finalizer_rejects_checkpoint_when_coverage_data_version_mismatches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """最终 child 封印在 coverage 版本与 checkpoint/publication 不一致时 fail-closed。"""
    claim, _identity_version_id = _claim()
    source_batch_id = uuid4()
    published_data_version = uuid4()
    coverage_version = uuid4()
    publication_id = uuid4()
    release_id = uuid4()
    checkpoint = SimpleNamespace(
        partition_key="equity.bar.1d.raw:SSE:600519:2026-07-01:2026-07-31",
        dataset_code=claim.dataset_code,
        checkpoint_kind="BAR_COVERAGE_VERSION",
        coverage_version=coverage_version,
        data_version=published_data_version,
        publication_id=publication_id,
        release_id=release_id,
        source_batch_ids_json=[str(source_batch_id)],
        publication_kind="DATA",
        record_count=1,
        window_from=date(2026, 7, 1),
        window_to=date(2026, 7, 31),
    )
    coverage = SimpleNamespace(
        coverage_version=coverage_version,
        capability=claim.dataset_code,
        coverage_from=checkpoint.window_from,
        coverage_to=checkpoint.window_to,
        publication_id=publication_id,
        # 数据库门禁被绕过的损坏行也不能通过最终应用层验证。
        data_version=uuid4(),
        source_batch_id=source_batch_id,
        quality_status="passed",
        publication_kind="DATA",
        record_count=1,
    )
    publication = SimpleNamespace(
        dataset=claim.dataset_code,
        data_version=published_data_version,
        release_id=release_id,
        quality_status="passed",
    )
    release = SimpleNamespace(release_id=release_id)
    database = _Database(
        _FinalizerSession(
            checkpoints=[checkpoint],
            evidence_rows=[(coverage, publication, release)],
        )
    )
    execution = _Execution()
    monkeypatch.setattr(
        checkpoint_module,
        "_binding",
        lambda _database, *, claim: checkpoint_module._Binding(
            child_id=uuid4(), command_id=uuid4()
        ),
    )

    with pytest.raises(RuntimeError, match="final output cannot be verified"):
        checkpoint_module.finalize_equity_bar_partitions(
            cast(DatabaseClient, database),
            claim=claim,
            execution=cast(FencedExecution, execution),
            ordered_partition_keys=(checkpoint.partition_key,),
        )

    assert execution.terminal_armed is False
    assert execution.terminal_written is False


def _claim() -> tuple[ExecutionClaim, UUID]:
    """构造拥有冻结身份与单一日线 target 的最小回填 claim。"""
    identity_version_id = uuid4()
    return (
        ExecutionClaim(
            run_id=uuid4(),
            dataset_code="equity.bar.1d.raw",
            fencing_token=7,
            target={"selector": {"kind": "INSTRUMENT", "exchange": "SSE", "symbol": "600519"}},
            source_snapshot=[],
            execution_intent={
                "kind": "EQUITY_BACKFILL",
                "targetIndex": 0,
                "identity": {
                    "securityId": 9_300_000_001,
                    "identifierVersionId": str(identity_version_id),
                    "exchange": "SSE",
                    "symbol": "600519",
                },
            },
        ),
        identity_version_id,
    )
