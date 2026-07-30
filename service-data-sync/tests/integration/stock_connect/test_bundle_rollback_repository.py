"""互联互通完整包 fenced 回滚的 PostgreSQL 集成测试。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from threading import Barrier, Lock, Thread
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from tests.integration.stock_connect.test_market_data_repository import (
    _official_ref,
    _seed_venue_and_regime,
)

from service_data_sync.application.ports.stock_connect import StockConnectSourceObservation
from service_data_sync.bootstrap.settings import load_settings
from service_data_sync.domain.stock_connect import (
    StockConnectCalendarDay,
    StockConnectChannel,
    StockConnectChannelStatus,
    StockConnectMarketDaily,
)
from service_data_sync.infrastructure.data_operations.stock_connect_rollback_operator import (
    StockConnectRollbackOperation,
    StockConnectRollbackOperator,
)
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.database.fenced_execution import (
    FencedExecution,
    FencingTokenLost,
    fenced_execution,
)
from service_data_sync.infrastructure.database.models.market import (
    StockConnectBundlePublication,
    StockConnectBundleRollbackAudit,
    StockConnectOverviewPublication,
)
from service_data_sync.infrastructure.database.models.operations import (
    DataOperationCommand,
    DataOperationExecutionSlot,
    DataOperationRun,
)
from service_data_sync.infrastructure.persistence.stock_connect_center_repository import (
    SqlAlchemyStockConnectCenterRepository,
)
from service_data_sync.infrastructure.persistence.stock_connect_market_data_repository import (
    SqlAlchemyStockConnectMarketDataRepository,
    StockConnectSourceApproval,
)
from service_data_sync.infrastructure.persistence.stock_connect_rollback_repository import (
    RolledBackStockConnectBundle,
    SqlAlchemyStockConnectRollbackRepository,
    StockConnectBundleRollbackRejected,
)

_CHANNEL = StockConnectChannel("SH", "NORTHBOUND")
_ACTOR_REF = "system:stock-connect-rollback-integration"
_REASON = "验证互联互通完整包 fenced 回滚原子性与审计"


@dataclass(frozen=True, slots=True)
class _BundleHistory:
    """保存一个随机交易日的历史目标和当前 bundle。"""

    trade_date: date
    previous_id: UUID
    current_id: UUID


@dataclass(frozen=True, slots=True)
class _FenceLease:
    """保存测试持有的权威 run 和全局 fencing token。"""

    command_id: UUID
    run_id: UUID
    fencing_token: int


@pytest.mark.integration
def test_bundle_rollback_moves_current_to_previous_and_replays_same_run() -> None:
    """验证 current→previous、overview 同步切换和同 run 幂等审计。"""
    database = _database()
    lease: _FenceLease | None = None
    try:
        history = _seed_bundle_history(database)
        lease = _start_fence(database)
        execution = _execution(database, lease)
        repository = SqlAlchemyStockConnectRollbackRepository(database)
        with fenced_execution(execution):
            first = repository.rollback_bundle(
                channel=_CHANNEL,
                trade_date=history.trade_date,
                target_bundle_release_id=history.previous_id,
                actor_ref=_ACTOR_REF,
                reason=_REASON,
            )
            replay = repository.rollback_bundle(
                channel=_CHANNEL,
                trade_date=history.trade_date,
                target_bundle_release_id=history.previous_id,
                actor_ref=_ACTOR_REF,
                reason=_REASON,
            )
        with database.session() as session:
            current = _current_bundle_id(session, trade_date=history.trade_date)
            overview = _single_channel_overview(session, trade_date=history.trade_date)
            audit = session.execute(
                select(StockConnectBundleRollbackAudit).where(
                    StockConnectBundleRollbackAudit.operation_run_id == lease.run_id
                )
            ).scalar_one()
            audit_count = session.scalar(
                select(func.count())
                .select_from(StockConnectBundleRollbackAudit)
                .where(StockConnectBundleRollbackAudit.operation_run_id == lease.run_id)
            )
        assert first.reused is False
        assert replay.reused is True
        assert replay.rollback_id == first.rollback_id
        assert current == history.previous_id
        assert overview.component_bundle_ids == {"SH_NORTHBOUND": str(history.previous_id)}
        assert audit_count == 1
        assert audit.from_bundle_release_id == history.current_id
        assert audit.to_bundle_release_id == history.previous_id
        assert audit.actor_ref == _ACTOR_REF
        assert audit.reason == _REASON
        assert audit.fencing_token == lease.fencing_token
        with pytest.raises(SQLAlchemyError):
            with database.transaction() as session:
                session.execute(
                    update(StockConnectBundleRollbackAudit)
                    .where(StockConnectBundleRollbackAudit.rollback_id == first.rollback_id)
                    .values(reason="不得篡改的替代原因")
                )
    finally:
        if lease is not None:
            _release_fence(database, lease)
        database.close()


@pytest.mark.integration
def test_rollback_operator_writes_atomic_terminal_state_and_replays_operation_id() -> None:
    """验证可调用运维入口把成功终态与审计同事务提交，并安全重放输出丢失的 operation。"""
    database = _database()
    try:
        history = _seed_bundle_history(database)
        operation_id = uuid4()
        operation = StockConnectRollbackOperation(
            operation_id=operation_id,
            channel=_CHANNEL,
            trade_date=history.trade_date,
            target_bundle_release_id=history.previous_id,
            actor_ref=_ACTOR_REF,
            reason=_REASON,
            request_id=f"integration:stock-connect-rollback:{operation_id}",
        )
        operator = StockConnectRollbackOperator(database)

        first = operator.execute(operation)
        replay = operator.execute(operation)

        with database.session() as session:
            run = session.get(DataOperationRun, first.run_id)
            command = session.get(DataOperationCommand, operation_id)
            slot = session.get(DataOperationExecutionSlot, "global")
            audit_count = session.scalar(
                select(func.count())
                .select_from(StockConnectBundleRollbackAudit)
                .where(StockConnectBundleRollbackAudit.operation_run_id == first.run_id)
            )
        assert first.rollback.reused is False
        assert replay.rollback.reused is True
        assert replay.rollback.rollback_id == first.rollback.rollback_id
        assert run is not None and run.status == "SUCCEEDED"
        assert run.completed_partitions == 1
        assert run.quality_gate_json["disposition"] == "ROLLBACK"
        assert command is not None and command.status == "SUCCEEDED"
        assert slot is not None and slot.state == "IDLE"
        assert audit_count == 1
        assert _read_current_bundle_id(database, history.trade_date) == history.previous_id
    finally:
        database.close()


@pytest.mark.integration
def test_bundle_rollback_rejects_wrong_and_incomplete_target_without_pointer_change() -> None:
    """验证跨日目标和内容摘要残缺目标均 fail-closed，当前指针不发生部分切换。"""
    database = _database()
    lease: _FenceLease | None = None
    try:
        history = _seed_bundle_history(database)
        other = _seed_bundle_history(database)
        lease = _start_fence(database)
        repository = SqlAlchemyStockConnectRollbackRepository(database)
        with fenced_execution(_execution(database, lease)):
            with pytest.raises(StockConnectBundleRollbackRejected) as wrong:
                repository.rollback_bundle(
                    channel=_CHANNEL,
                    trade_date=history.trade_date,
                    target_bundle_release_id=other.previous_id,
                    actor_ref=_ACTOR_REF,
                    reason=_REASON,
                )
        assert wrong.value.code == "rollback-target-identity-mismatch"
        assert _read_current_bundle_id(database, history.trade_date) == history.current_id

        with database.transaction() as session:
            target = session.get(StockConnectBundlePublication, history.previous_id)
            assert target is not None
            damaged_summary = dict(target.summary_json)
            damaged_summary.pop("channel")
            session.execute(
                update(StockConnectBundlePublication)
                .where(StockConnectBundlePublication.bundle_release_id == history.previous_id)
                .values(summary_json=damaged_summary)
            )
        with fenced_execution(_execution(database, lease)):
            with pytest.raises(StockConnectBundleRollbackRejected) as incomplete:
                repository.rollback_bundle(
                    channel=_CHANNEL,
                    trade_date=history.trade_date,
                    target_bundle_release_id=history.previous_id,
                    actor_ref=_ACTOR_REF,
                    reason=_REASON,
                )
        assert incomplete.value.code == "rollback-target-incomplete"
        assert _read_current_bundle_id(database, history.trade_date) == history.current_id
    finally:
        if lease is not None:
            _release_fence(database, lease)
        database.close()


@pytest.mark.integration
def test_bundle_rollback_rejects_incomplete_overview_graph() -> None:
    """验证 current overview 组件缺失时不切 bundle，避免通道页和总览指向不同历史。"""
    database = _database()
    lease: _FenceLease | None = None
    try:
        history = _seed_bundle_history(database)
        with database.transaction() as session:
            overview = _single_channel_overview(session, trade_date=history.trade_date)
            session.execute(
                update(StockConnectOverviewPublication)
                .where(
                    StockConnectOverviewPublication.overview_release_id
                    == overview.overview_release_id
                )
                .values(component_bundle_ids={})
            )
        lease = _start_fence(database)
        repository = SqlAlchemyStockConnectRollbackRepository(database)
        with fenced_execution(_execution(database, lease)):
            with pytest.raises(StockConnectBundleRollbackRejected) as rejected:
                repository.rollback_bundle(
                    channel=_CHANNEL,
                    trade_date=history.trade_date,
                    target_bundle_release_id=history.previous_id,
                    actor_ref=_ACTOR_REF,
                    reason=_REASON,
                )
        assert rejected.value.code == "rollback-overview-incomplete"
        assert _read_current_bundle_id(database, history.trade_date) == history.current_id
    finally:
        if lease is not None:
            _release_fence(database, lease)
        database.close()


@pytest.mark.integration
def test_bundle_rollback_rejects_stale_fence_and_serializes_concurrent_replay() -> None:
    """验证陈旧 token 零写入；同一有效 run 并发仅提交一次并由另一调用幂等复用。"""
    database = _database()
    lease: _FenceLease | None = None
    try:
        history = _seed_bundle_history(database)
        lease = _start_fence(database)
        repository = SqlAlchemyStockConnectRollbackRepository(database)
        stale = FencedExecution(
            database=database,
            run_id=lease.run_id,
            fencing_token=lease.fencing_token - 1,
            finalizer=_noop_finalizer,
        )
        with pytest.raises(FencingTokenLost):
            with fenced_execution(stale):
                repository.rollback_bundle(
                    channel=_CHANNEL,
                    trade_date=history.trade_date,
                    target_bundle_release_id=history.previous_id,
                    actor_ref=_ACTOR_REF,
                    reason=_REASON,
                )
        assert _read_current_bundle_id(database, history.trade_date) == history.current_id

        barrier = Barrier(3)
        guard = Lock()
        results: list[RolledBackStockConnectBundle] = []
        failures: list[BaseException] = []

        def worker() -> None:
            """让独立 session 同时竞争全局 slot 和同一 bundle current 指针。"""
            try:
                barrier.wait(timeout=10)
                with fenced_execution(_execution(database, lease)):
                    result = repository.rollback_bundle(
                        channel=_CHANNEL,
                        trade_date=history.trade_date,
                        target_bundle_release_id=history.previous_id,
                        actor_ref=_ACTOR_REF,
                        reason=_REASON,
                    )
                with guard:
                    results.append(result)
            except BaseException as error:  # pragma: no cover - 线程异常由主线程统一断言。
                with guard:
                    failures.append(error)

        threads = [Thread(target=worker) for _index in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait(timeout=10)
        for thread in threads:
            thread.join(timeout=15)

        assert not failures
        assert not any(thread.is_alive() for thread in threads)
        assert sorted(result.reused for result in results) == [False, True]
        assert len({result.rollback_id for result in results}) == 1
        assert _read_current_bundle_id(database, history.trade_date) == history.previous_id
        with database.session() as session:
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(StockConnectBundleRollbackAudit)
                    .where(StockConnectBundleRollbackAudit.operation_run_id == lease.run_id)
                )
                == 1
            )
    finally:
        if lease is not None:
            _release_fence(database, lease)
        database.close()


def _database() -> DatabaseClient:
    """连接显式启用的隔离 PostgreSQL；宿主机从不运行 Python。"""
    if os.environ.get("DATA_SYNC_RUN_INTEGRATION") != "1":
        pytest.skip("requires DATA_SYNC_RUN_INTEGRATION=1")
    return DatabaseClient.from_settings(load_settings())


def _seed_bundle_history(database: DatabaseClient) -> _BundleHistory:
    """通过正式 canonical 发布器生成同日 previous/current 两个真实完整包。"""
    _seed_venue_and_regime(database)
    trade_date = date(2200, 1, 1) + timedelta(days=uuid4().int % 100_000)
    seed = f"{trade_date.isoformat()}:{uuid4()}"
    raw_hash = sha256(f"raw:{seed}".encode()).hexdigest()
    normalized_hash = sha256(f"normalized:{seed}".encode()).hexdigest()
    observed_at = datetime.now(UTC)
    market_repository = SqlAlchemyStockConnectMarketDataRepository(
        database,
        approved_sources={
            "integration-stock-connect-rollback": StockConnectSourceApproval(
                provider_id="integration-stock-connect-rollback",
                source_code="integration_stock_connect_rollback_official",
                legal_name="港通回滚集成测试官方来源",
                source_kind="official",
                rights_status="internal",
                license_scope="integration_test_only",
                rights_evidence_ref="license-audit:integration-stock-connect-rollback",
            )
        },
    )
    market = market_repository.publish_market_daily(
        channel=_CHANNEL,
        records=(
            StockConnectMarketDaily(
                trade_date=trade_date,
                buy_amount=None,
                sell_amount=None,
                turnover_amount=Decimal("0"),
                net_buy_amount=None,
                quota_balance=None,
                currency="CNY",
                availability_status="PARTIAL",
                field_availability=(
                    ("turnoverAmount", "REPORTED"),
                    ("buyAmount", "SOURCE_MISSING"),
                    ("sellAmount", "SOURCE_MISSING"),
                    ("netBuyAmount", "NOT_APPLICABLE"),
                    ("tradeCount", "SOURCE_MISSING"),
                    ("etfTurnoverAmount", "SOURCE_MISSING"),
                ),
            ),
        ),
        source=_source(
            raw_hash=raw_hash,
            normalized_hash=normalized_hash,
            observed_at=observed_at,
        ),
    )
    calendar_digest = sha256(f"calendar:{trade_date.year}".encode()).hexdigest()
    calendar_ref = _official_ref(
        source_code="HKEX_CALENDAR",
        product_name=f"{trade_date.year}-calendar.csv",
        digest=calendar_digest,
    )
    status_digest = sha256(f"status:{seed}".encode()).hexdigest()
    status = StockConnectChannelStatus(
        trade_date=trade_date,
        channel=_CHANNEL.channel,
        direction=_CHANNEL.direction,
        trading_day=True,
        session_state="CLOSED",
        session_availability="DERIVED",
        buy_order_accepted=None,
        sell_order_accepted=None,
        quota_state="SUFFICIENT",
        quota_balance=None,
        quota_currency="CNY",
        observed_at=observed_at,
        source_code="HKEX_OMDC",
        product_name=f"integration-status-{seed}",
        source_publication_at=observed_at,
        source_file_sha256=status_digest,
    )
    source_refs = (
        calendar_ref,
        _official_ref(
            source_code="HKEX_DATA_MARKETPLACE",
            product_name=f"integration-daily-{seed}",
            digest=raw_hash,
        ),
        _official_ref(
            source_code=status.source_code,
            product_name=status.product_name,
            digest=status_digest,
        ),
    )
    center = SqlAlchemyStockConnectCenterRepository(database)
    previous = center.publish_bundle(
        channel=_CHANNEL,
        overview_generation_id=uuid4(),
        overview_channels=("SH_NORTHBOUND",),
        market_data_version=market.data_version,
        active_data_version=None,
        calendar=StockConnectCalendarDay(
            calendar_date=trade_date,
            northbound_trading=True,
            southbound_trading=True,
            hong_kong_state="OPEN",
            mainland_state="OPEN",
        ),
        calendar_source_ref=calendar_ref,
        calendar_observed_at=observed_at,
        status=status,
        quality_issues=(),
        source_refs=source_refs,
    )
    current = center.publish_bundle(
        channel=_CHANNEL,
        overview_generation_id=uuid4(),
        overview_channels=("SH_NORTHBOUND",),
        market_data_version=market.data_version,
        active_data_version=None,
        calendar=StockConnectCalendarDay(
            calendar_date=trade_date,
            northbound_trading=True,
            southbound_trading=True,
            hong_kong_state="OPEN",
            mainland_state="OPEN",
        ),
        calendar_source_ref=calendar_ref,
        calendar_observed_at=observed_at,
        status=status,
        quality_issues=(
            {
                "code": "INTEGRATION_CORRECTION",
                "component": "market-stat",
                "detail": "integration correction makes a second immutable bundle",
            },
        ),
        source_refs=source_refs,
    )
    assert previous.bundle_release_id != current.bundle_release_id
    return _BundleHistory(
        trade_date=trade_date,
        previous_id=previous.bundle_release_id,
        current_id=current.bundle_release_id,
    )


def _source(
    *,
    raw_hash: str,
    normalized_hash: str,
    observed_at: datetime,
) -> StockConnectSourceObservation:
    """构造只供正式仓储发布路径使用的可审计集成来源观察。"""
    return StockConnectSourceObservation(
        provider_id="integration-stock-connect-rollback",
        capability="market.stock_connect.market_stat.reported",
        raw_payload_sha256=raw_hash,
        raw_uri=f"s3://integration/{raw_hash}/raw.json",
        raw_content_type="application/json",
        raw_byte_size=100,
        normalized_payload_sha256=normalized_hash,
        normalized_uri=f"s3://integration/{normalized_hash}/normalized.json",
        normalized_content_type="application/json",
        normalized_byte_size=80,
        observed_at=observed_at,
        upstream_source="integration-stock-connect-rollback-official",
        adapter_version="integration-v1",
        schema_fingerprint="9" * 64,
    )


def _start_fence(database: DatabaseClient) -> _FenceLease:
    """创建 RUNNING command/run 并原子取得隔离测试库全局 slot。"""
    now = datetime.now(UTC)
    command_id = uuid4()
    run_id = uuid4()
    with database.transaction() as session:
        slot = session.get(DataOperationExecutionSlot, "global", with_for_update=True)
        if slot is None:
            slot = DataOperationExecutionSlot(
                slot_key="global",
                state="IDLE",
                run_id=None,
                dataset_code=None,
                lease_until=None,
                heartbeat_at=None,
                fencing_token=0,
            )
            session.add(slot)
            session.flush()
        assert slot.state == "IDLE", "集成库必须隔离，不能抢占其他 worker"
        token = slot.fencing_token + 1
        session.add(
            DataOperationCommand(
                command_id=command_id,
                submission_id=None,
                status="RUNNING",
                actor_ref=_ACTOR_REF,
                actor_role="SYSTEM",
                reason=_REASON,
                request_id=f"integration:{command_id}",
                retry_of_command_id=None,
                error_json=None,
                requested_at=now,
                started_at=now,
                finished_at=None,
            )
        )
        session.add(
            DataOperationRun(
                run_id=run_id,
                command_id=command_id,
                target_index=0,
                dataset_code="market.stock_connect.center.bundle",
                mode="ROLLBACK",
                target_json={"selector": {"kind": "GLOBAL"}},
                source_snapshot=[],
                execution_intent_json=None,
                status="RUNNING",
                queue_position=None,
                attempt=1,
                recovery_attempts=0,
                completed_partitions=0,
                total_partitions=1,
                processed_records=0,
                estimated_records=None,
                fencing_token=token,
                cancel_requested=False,
                error_json=None,
                quality_gate_json={"disposition": "ROLLBACK", "rules": []},
                requested_at=now,
                started_at=now,
                finished_at=None,
            )
        )
        slot.state = "RUNNING"
        slot.run_id = run_id
        slot.dataset_code = "market.stock_connect.center.bundle"
        slot.lease_until = now + timedelta(minutes=5)
        slot.heartbeat_at = now
        slot.fencing_token = token
    return _FenceLease(command_id=command_id, run_id=run_id, fencing_token=token)


def _release_fence(database: DatabaseClient, lease: _FenceLease) -> None:
    """把测试 run 和 command 收口为成功并释放全局 slot，审计外键继续保留。"""
    now = datetime.now(UTC)
    with database.transaction() as session:
        slot = session.get(DataOperationExecutionSlot, "global", with_for_update=True)
        assert slot is not None
        if slot.run_id == lease.run_id:
            slot.state = "IDLE"
            slot.run_id = None
            slot.dataset_code = None
            slot.lease_until = None
            slot.heartbeat_at = None
        run = session.get(DataOperationRun, lease.run_id)
        command = session.get(DataOperationCommand, lease.command_id)
        assert run is not None
        assert command is not None
        run.status = "SUCCEEDED"
        run.completed_partitions = 1
        run.finished_at = now
        command.status = "SUCCEEDED"
        command.finished_at = now


def _execution(database: DatabaseClient, lease: _FenceLease) -> FencedExecution:
    """为一个已持槽 run 创建不写终态的 canonical fencing 上下文。"""
    return FencedExecution(
        database=database,
        run_id=lease.run_id,
        fencing_token=lease.fencing_token,
        finalizer=_noop_finalizer,
    )


def _noop_finalizer(_session: Session, _execution: FencedExecution) -> None:
    """测试不武装终态回调；command/run 由 fixture 收口。"""


def _read_current_bundle_id(database: DatabaseClient, trade_date: date) -> UUID:
    """在独立只读 session 中返回指定日期单通道 current bundle。"""
    with database.session() as session:
        return _current_bundle_id(session, trade_date=trade_date)


def _current_bundle_id(session: Session, *, trade_date: date) -> UUID:
    """返回指定日期 SH 北向唯一 current bundle UUID。"""
    value = session.execute(
        select(StockConnectBundlePublication.bundle_release_id).where(
            StockConnectBundlePublication.trade_date == trade_date,
            StockConnectBundlePublication.channel == _CHANNEL.channel,
            StockConnectBundlePublication.direction == _CHANNEL.direction,
            StockConnectBundlePublication.superseded_at.is_(None),
        )
    ).scalar_one()
    return UUID(str(value))


def _single_channel_overview(
    session: Session, *, trade_date: date
) -> StockConnectOverviewPublication:
    """返回指定日期 SH 北向单通道 current overview。"""
    return session.execute(
        select(StockConnectOverviewPublication).where(
            StockConnectOverviewPublication.trade_date == trade_date,
            StockConnectOverviewPublication.channel_set == "SH_NORTHBOUND",
            StockConnectOverviewPublication.superseded_at.is_(None),
        )
    ).scalar_one()
