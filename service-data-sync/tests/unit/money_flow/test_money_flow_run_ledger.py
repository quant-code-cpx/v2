"""资金流共享运行账本的租约、恢复和稳定 checkpoint 测试。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from typing import Any, cast
from uuid import UUID

import pytest

from service_data_sync.application.money_flow.sync import MoneyFlowSyncResult
from service_data_sync.application.ports.money_flow import PublishedMoneyFlow
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.persistence.money_flow_run_ledger import (
    SqlAlchemyMoneyFlowRunLedger,
    _target_date,
)


class FakeResult:
    """模拟 SQLAlchemy 标量、映射和受影响行数结果。"""

    def __init__(
        self,
        *,
        scalar: object | None = None,
        mapping: dict[str, object] | None = None,
        rowcount: int = 1,
    ) -> None:
        """保存单次数据库调用的受控返回。"""
        self._scalar = scalar
        self._mapping = mapping
        self.rowcount = rowcount

    def scalar_one(self) -> object:
        """返回唯一标量。"""
        if self._scalar is None:
            raise AssertionError("scalar result was not configured")
        return self._scalar

    def mappings(self) -> FakeResult:
        """保持链式映射结果接口。"""
        return self

    def one_or_none(self) -> dict[str, object] | None:
        """返回可选唯一映射。"""
        return self._mapping


class FakeSession:
    """按队列返回结果并记录 SQLAlchemy 表达式。"""

    def __init__(self, results: list[FakeResult]) -> None:
        """复制结果队列，防止测试调用间共享状态。"""
        self.results = list(results)
        self.statements: list[object] = []

    def execute(self, statement: object) -> FakeResult:
        """记录表达式并返回下一项结果。"""
        self.statements.append(statement)
        return self.results.pop(0) if self.results else FakeResult()


class FakeDatabase:
    """为账本提供单个短事务会话。"""

    def __init__(self, session: FakeSession) -> None:
        """保存本次测试会话。"""
        self.session_value = session

    @contextmanager
    def transaction(self) -> Iterator[FakeSession]:
        """模拟原子事务边界。"""
        yield self.session_value


def _ledger(session: FakeSession) -> SqlAlchemyMoneyFlowRunLedger:
    """把结构兼容的假数据库注入账本。"""
    database = cast(DatabaseClient, cast(Any, FakeDatabase(session)))
    return SqlAlchemyMoneyFlowRunLedger(database)


def _sync_result(*, published: bool, quality_status: str) -> MoneyFlowSyncResult:
    """构造包含 raw 与 publication checkpoint 的同步结果。"""
    return MoneyFlowSyncResult(
        capability="money_flow.order_size.daily.equity.raw",
        source_payload_sha256="a" * 64,
        raw_uri="s3://private/raw/evidence.json",
        publication=PublishedMoneyFlow(
            data_version=(UUID("00000000-0000-4000-8000-000000000107") if published else None),
            inserted_count=1,
            revised_count=2,
            unchanged_count=3,
            published=published,
            quality_status=quality_status,
        ),
    )


def test_start_creates_stable_partition_and_recovers_expired_attempt() -> None:
    """首次请求 attempt 为一，过期租约以同一请求键递增接管。"""
    run_id = UUID("00000000-0000-4000-8000-000000000105")
    created_session = FakeSession(
        [
            FakeResult(scalar=run_id),
            FakeResult(mapping=None),
            FakeResult(),
        ]
    )
    created = _ledger(created_session).start(
        capability="money_flow.order_size.ranking.equity.raw",
        parameters=(("targetDate", "2026-07-24"), ("indicator", "今日")),
        mode="manual",
    )
    expired_session = FakeSession(
        [
            FakeResult(scalar=run_id),
            FakeResult(
                mapping={
                    "attempt": 2,
                    "lease_until": datetime.now(UTC) - timedelta(minutes=1),
                }
            ),
            FakeResult(),
        ]
    )
    recovered = _ledger(expired_session).start(
        capability="money_flow.order_size.ranking.equity.raw",
        parameters=(("indicator", "今日"), ("targetDate", "2026-07-24")),
        mode="backfill",
    )

    assert created.run_id == run_id
    assert created.attempt == 1
    assert created.partition_key.startswith("request:")
    assert recovered.run_id == run_id
    assert recovered.partition_key == created.partition_key
    assert recovered.attempt == 3
    assert len(created_session.statements) == 3


def test_start_rejects_invalid_mode_and_active_lease() -> None:
    """未知运行模式和仍有效的 fencing 租约均不得接管。"""
    with pytest.raises(ValueError, match="mode"):
        _ledger(FakeSession([])).start(
            capability="money_flow.order_size.daily.market.raw",
            parameters=(),
            mode="automatic",
        )

    run_id = UUID("00000000-0000-4000-8000-000000000106")
    active_session = FakeSession(
        [
            FakeResult(scalar=run_id),
            FakeResult(
                mapping={
                    "attempt": 1,
                    "lease_until": datetime.now(UTC) + timedelta(minutes=10),
                }
            ),
        ]
    )
    with pytest.raises(RuntimeError, match="already leased"):
        _ledger(active_session).start(
            capability="money_flow.order_size.daily.market.raw",
            parameters=(("marketCode", "cn-a"),),
            mode="scheduled",
        )


def test_finish_persists_success_or_partial_and_checks_fencing_owner() -> None:
    """完成时原子释放租约，丢失 fencing owner 时拒绝覆盖新 owner。"""
    run_id = UUID("00000000-0000-4000-8000-000000000108")
    run = _ledger(
        FakeSession(
            [
                FakeResult(scalar=run_id),
                FakeResult(mapping=None),
                FakeResult(),
            ]
        )
    ).start(
        capability="money_flow.order_size.daily.equity.raw",
        parameters=(("symbol", "600000"),),
        mode="manual",
    )
    success_session = FakeSession([FakeResult(rowcount=1), FakeResult()])
    _ledger(success_session).finish(
        run=run,
        result=_sync_result(published=True, quality_status="passed"),
    )
    partial_session = FakeSession([FakeResult(rowcount=1), FakeResult()])
    _ledger(partial_session).finish(
        run=run,
        result=_sync_result(published=False, quality_status="partial"),
    )
    stale_session = FakeSession([FakeResult(rowcount=0)])

    assert len(success_session.statements) == 2
    assert len(partial_session.statements) == 2
    with pytest.raises(RuntimeError, match="no longer active"):
        _ledger(stale_session).finish(
            run=run,
            result=_sync_result(published=False, quality_status="partial"),
        )


def test_fail_persists_retry_policy_and_validates_stable_error_code() -> None:
    """可重试与终止失败均写入账本，空或过长错误码直接拒绝。"""
    run_id = UUID("00000000-0000-4000-8000-000000000109")
    run = _ledger(
        FakeSession(
            [
                FakeResult(scalar=run_id),
                FakeResult(mapping=None),
                FakeResult(),
            ]
        )
    ).start(
        capability="money_flow.trade_direction.ranking.equity.raw",
        parameters=(("indicator", "即时"),),
        mode="scheduled",
    )
    retry_session = FakeSession([FakeResult(rowcount=1), FakeResult()])
    _ledger(retry_session).fail(
        run=run,
        error_code="provider-unavailable",
        retryable=True,
    )
    final_session = FakeSession([FakeResult(rowcount=1), FakeResult()])
    _ledger(final_session).fail(
        run=run,
        error_code="money-flow-sync-failed",
        retryable=False,
    )

    assert len(retry_session.statements) == 2
    assert len(final_session.statements) == 2
    for error_code in ("", "x" * 65):
        with pytest.raises(ValueError, match="error code"):
            _ledger(FakeSession([])).fail(
                run=run,
                error_code=error_code,
                retryable=False,
            )
    with pytest.raises(RuntimeError, match="no longer active"):
        _ledger(FakeSession([FakeResult(rowcount=0)])).fail(
            run=run,
            error_code="provider-schema",
            retryable=False,
        )


def test_target_date_only_uses_explicit_ranking_partition_date() -> None:
    """排行目标日进入账本，历史窗口不得伪装为单目标日。"""
    assert _target_date((("targetDate", "2026-07-24"),)) == date(2026, 7, 24)
    assert _target_date((("start", "2026-07-01"), ("end", "2026-07-24"))) is None
