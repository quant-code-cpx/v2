"""数据运维控制面 PostgreSQL 并发、fencing 与持久化计划集成回归。"""

from __future__ import annotations

import json
import os
from collections.abc import Generator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import Barrier, Lock, Thread
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from service_data_sync.application.ports.data_source import ProviderBatch, SourceRequest
from service_data_sync.application.source_registry import SourceRegistry
from service_data_sync.bootstrap.container import ServiceContainer
from service_data_sync.infrastructure.data_operations import canonical_executors
from service_data_sync.infrastructure.data_operations.control_plane import (
    DataOperationsControlPlane,
    DatasetDefinition,
    ExecutionClaim,
    ExecutionOutcome,
)
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.database.fenced_execution import (
    FencedExecution,
    FencingTokenLost,
    current_fenced_execution,
    fenced_execution,
)
from service_data_sync.infrastructure.database.models.operations import (
    DataOperationCommand,
    DataOperationExecutionSlot,
    DataOperationPartition,
    DataOperationRun,
    DataOperationRunSourceBatch,
    DataOperationSchedule,
    DataOperationScheduleFire,
    DataOperationScheduleRevision,
)
from service_data_sync.infrastructure.persistence.etf_universe_repository import (
    load_frozen_etf_universe,
    resolve_current_etf_profile_data_versions,
)
from service_data_sync.infrastructure.persistence.source_batch import (
    record_source_observation,
)

pytestmark = pytest.mark.integration


@dataclass(slots=True)
class _Clock:
    """为 lease、reaper 与 misfire 测试提供可显式推进的 UTC 时钟。"""

    value: datetime

    def now(self) -> datetime:
        """返回测试当前时刻，避免依赖机器时间导致过期边界不稳定。"""
        return self.value


@dataclass(frozen=True, slots=True)
class _Provider:
    """描述测试目录中已注册且不会发起网络访问的 provider 身份。"""

    provider_id: str


class _Registry:
    """提供控制面冻结 sourceSnapshot 所需的最小只读来源注册表。"""

    def provider_ids(self) -> frozenset[str]:
        """返回唯一测试 provider，保证计划入队能冻结来源绑定。"""
        return frozenset({"integration-provider"})

    def for_capability(self, capability: str) -> tuple[_Provider, ...]:
        """为测试 capability 返回同一 provider，不把计划测试变成 adapter 测试。"""
        assert capability == "integration.data-operations"
        return (_Provider("integration-provider"),)


class _OpenCalendar:
    """为当前日期目录计划提供明确开市结论。"""

    def is_open(self, *, trade_date: object) -> bool:
        """所有测试日期均视为已登记开市日。"""
        del trade_date
        return True


class _EtfMasterProvider:
    """按请求场所返回各一只真实标准化 ETF 产品资料，不访问网络。"""

    provider_id = "akshare"

    def __init__(self, symbols: dict[str, str]) -> None:
        """保存双市场显式代码并记录请求场所。"""
        self._symbols = symbols
        self.venues: list[str] = []

    def capabilities(self) -> frozenset[str]:
        """只声明 ETF 产品目录能力。"""
        return frozenset({"fund.etf.master"})

    async def fetch(self, request: SourceRequest) -> ProviderBatch:
        """返回与请求场所、观察日一致的 v2 标准载荷和来源证据。"""
        parameters = dict(request.parameters)
        venue = parameters["venue"]
        observation_date = parameters["observationDate"]
        self.venues.append(venue)
        payload = {
            "schema": "quant-v2.etf-master.v2",
            "venue": venue,
            "profiles": [
                {
                    "symbol": self._symbols[venue],
                    "displayName": f"集成测试{venue}ETF",
                    "etfType": "EQUITY",
                    "managementMode": "PASSIVE",
                    "managerName": "集成测试管理人",
                    "custodianName": None,
                    "establishedOn": None,
                    "listedOn": observation_date,
                    "delistedOn": None,
                    "quoteCurrency": "CNY",
                    "navCurrency": "CNY",
                    "listingStatus": "LISTED",
                    "effectiveFrom": observation_date,
                    "sourceTimePrecision": "DATE_ONLY",
                }
            ],
        }
        encoded = json.dumps(payload, ensure_ascii=False).encode()
        return ProviderBatch(
            provider_id=self.provider_id,
            capability=request.capability,
            payload=encoded,
            raw_payload=encoded,
            raw_content_type="application/json",
            observed_at=datetime.now(UTC),
            upstream_source="integration.official-etf-directory",
            adapter_version="integration-v1",
            schema_fingerprint="8" * 64,
        )


class _MemoryRawStore:
    """为目录集成链路保存摘要 URI，避免依赖外部对象存储。"""

    def __init__(self, _client: object) -> None:
        """接受生产构造参数但不持有网络客户端。"""

    def put(self, payload: object) -> str:
        """返回稳定测试 URI，canonical 仍会写入真实 PostgreSQL 血缘。"""
        return f"s3://integration-etf/{hash(repr(payload)) & 0xFFFF:x}.json"

    def stage_batch(self, _batch: ProviderBatch) -> None:
        """模拟成功批次短暂登记；集成测试不连接外部对象存储。"""

    def stage_failure_summary(
        self,
        _payload: bytes,
        _content_type: str,
        *,
        capability: str | None = None,
    ) -> None:
        """接受脱敏失败摘要接口，当前成功链路不会生成持久对象。"""
        del capability

    def persist_failure(self, _error: Exception) -> None:
        """模拟失败清单持久化并返回空定位。"""

    def discard(self) -> None:
        """清空内存暂存的生产语义在此无状态替身中为空操作。"""


@pytest.fixture
def database() -> Generator[DatabaseClient]:
    """连接显式启用的隔离 PostgreSQL 集成库，测试结束后释放连接池。"""
    if os.environ.get("DATA_SYNC_RUN_INTEGRATION") != "1":
        pytest.skip("requires DATA_SYNC_RUN_INTEGRATION=1")
    database_url = os.environ.get("DATA_SYNC_DATABASE_URL")
    if database_url is None:
        pytest.skip("requires DATA_SYNC_DATABASE_URL")
    client = DatabaseClient(create_engine(database_url, pool_pre_ping=True))
    try:
        yield client
    finally:
        client.close()


def _definition(dataset_code: str) -> DatasetDefinition:
    """构造可被 dispatcher 与计划器执行的最小冻结数据集目录项。"""
    return DatasetDefinition(
        dataset_code=dataset_code,
        display_name="集成控制面数据集",
        domain="integration",
        description="仅验证 PostgreSQL 控制面状态转换",
        grain="测试目标",
        capability="integration.data-operations",
        modes=("FULL", "INCREMENTAL", "OBSERVATION_DATE"),
        schedule_modes=("FULL",),
        source_capabilities=("integration.data-operations",),
        selector_kinds=("GLOBAL",),
        dispatcher_ready=True,
        config_enabled=True,
    )


def _control_plane(
    database: DatabaseClient, clock: _Clock, *dataset_codes: str
) -> DataOperationsControlPlane:
    """创建只含测试目录的控制面，所有状态仍经真实 PostgreSQL 事务写入。"""
    return DataOperationsControlPlane(
        database=database,
        catalog={dataset_code: _definition(dataset_code) for dataset_code in dataset_codes},
        source_registry=_Registry(),  # type: ignore[arg-type]  # 测试只使用只读目录方法。
        now=clock.now,
    )


def _ensure_idle_slot(database: DatabaseClient) -> None:
    """为竞争测试建立空闲全局槽，绝不覆盖正在运行的其他测试 worker。"""
    with database.transaction() as session:
        slot = session.get(DataOperationExecutionSlot, "global", with_for_update=True)
        if slot is None:
            session.add(
                DataOperationExecutionSlot(
                    slot_key="global",
                    state="IDLE",
                    run_id=None,
                    dataset_code=None,
                    lease_until=None,
                    heartbeat_at=None,
                    fencing_token=0,
                )
            )
            return
        assert slot.state == "IDLE", "集成库必须隔离，不能抢占已有全局同步槽"


def _insert_queued_run(
    database: DatabaseClient, *, dataset_code: str, now: datetime
) -> tuple[UUID, UUID]:
    """插入一个已入账 queued child run，使测试只聚焦 dispatcher 的数据库竞争。"""
    command_id = uuid4()
    run_id = uuid4()
    with database.transaction() as session:
        session.add(
            DataOperationCommand(
                command_id=command_id,
                submission_id=None,
                status="QUEUED",
                actor_ref="system:integration",
                actor_role="SYSTEM",
                reason="集成控制面并发验证",
                request_id=f"integration:{command_id}",
                retry_of_command_id=None,
                error_json=None,
                requested_at=now,
                started_at=None,
                finished_at=None,
            )
        )
        session.add(
            DataOperationRun(
                run_id=run_id,
                command_id=command_id,
                target_index=0,
                dataset_code=dataset_code,
                mode="FULL",
                target_json={
                    "datasetCode": dataset_code,
                    "mode": "FULL",
                    "selector": {"kind": "GLOBAL"},
                    "dateFrom": None,
                    "dateTo": None,
                    "observationDate": None,
                },
                source_snapshot=[],
                execution_intent_json=None,
                status="QUEUED",
                queue_position=1,
                attempt=0,
                recovery_attempts=0,
                completed_partitions=0,
                total_partitions=1,
                processed_records=0,
                estimated_records=None,
                fencing_token=None,
                cancel_requested=False,
                error_json=None,
                quality_gate_json={"disposition": "NOT_EVALUATED", "rules": []},
                requested_at=now,
                started_at=None,
                finished_at=None,
            )
        )
    return command_id, run_id


def _frequency() -> dict[str, Any]:
    """返回严格完整的 DAILY 频率，所有不适用字段均显式为 null。"""
    return {
        "kind": "DAILY",
        "timezone": "Asia/Shanghai",
        "localTime": "09:00",
        "dayOfWeek": None,
        "dayOfMonth": None,
        "intervalMinutes": None,
        "calendarCode": None,
    }


def _insert_schedule(
    database: DatabaseClient,
    *,
    dataset_code: str,
    now: datetime,
    first_due: datetime,
    misfire_policy: str,
    coalesce: bool,
) -> UUID:
    """插入带 immutable revision 的到期计划，覆盖 scheduler fire 外键与冻结快照。"""
    schedule_id = uuid4()
    revision_id = uuid4()
    selector = {"kind": "GLOBAL"}
    policy = {"policyVersion": 1, "dateResolution": "NONE"}
    frequency = _frequency()
    with database.transaction() as session:
        session.add(
            DataOperationSchedule(
                schedule_id=schedule_id,
                dataset_code=dataset_code,
                mode="FULL",
                selector_json=selector,
                target_policy_json=policy,
                frequency_json=frequency,
                misfire_policy=misfire_policy,
                coalesce=coalesce,
                enabled=True,
                version=1,
                revision_id=revision_id,
                recent_run_at=None,
                next_run_at=first_due,
                updated_at=now,
                updated_by_actor_ref="system:integration",
            )
        )
        session.flush()
        session.add(
            DataOperationScheduleRevision(
                revision_id=revision_id,
                schedule_id=schedule_id,
                version=1,
                change_kind="UPSERT",
                dataset_code=dataset_code,
                mode="FULL",
                selector_json=selector,
                target_policy_json=policy,
                frequency_json=frequency,
                misfire_policy=misfire_policy,
                coalesce=coalesce,
                enabled=True,
                before_hash="0" * 64,
                after_hash="1" * 64,
                actor_ref="system:integration",
                request_id=f"integration:{schedule_id}",
                created_at=now,
            )
        )
    return schedule_id


def _unexpected_finalizer(_session: Session, _execution: FencedExecution) -> None:
    """确保陈旧 worker 在进入 canonical 发布事务前已被 fence 拒绝。"""
    raise AssertionError("陈旧 worker 不得执行 canonical finalizer")


def test_retryable_all_etfs_run_requeues_same_frozen_run_and_inherits_success(
    database: DatabaseClient,
) -> None:
    """可重试全集失败自动续跑同一 run，成功分区不重做且周期 tick 可继续领取。"""
    clock = _Clock(datetime.now(UTC) + timedelta(days=2, hours=9))
    dataset_code = "fund.etf.bar.1d.reported"
    _ensure_idle_slot(database)
    command_id, run_id = _insert_queued_run(
        database,
        dataset_code=dataset_code,
        now=clock.now(),
    )
    with database.transaction() as session:
        run = session.get(DataOperationRun, run_id)
        assert run is not None
        run.target_json = {
            **run.target_json,
            "selector": {
                "kind": "ETF",
                "operation": "BARS",
                "venue": None,
                "scope": "ALL_ETFS",
                "etf": None,
                "profileDataVersions": {
                    "SSE": str(uuid4()),
                    "SZSE": str(uuid4()),
                },
            },
        }
    control_plane = DataOperationsControlPlane(
        database=database,
        catalog={dataset_code: _definition(dataset_code)},
        source_registry=_Registry(),  # type: ignore[arg-type]
        now=clock.now,
        etf_auto_retry_max_attempts=2,
    )
    calls: list[int] = []

    def executor(claim: ExecutionClaim) -> ExecutionOutcome:
        """首轮制造可重试部分失败，次轮验证并继承成功分区后完成。"""
        calls.append(claim.attempt)
        with database.transaction() as session:
            succeeded = session.get(
                DataOperationPartition,
                {"run_id": run_id, "partition_key": "etf:SSE.510300"},
            )
            failed = session.get(
                DataOperationPartition,
                {"run_id": run_id, "partition_key": "etf:SZSE.159919"},
            )
            if claim.attempt == 1:
                assert succeeded is None and failed is None
                session.add(
                    DataOperationPartition(
                        run_id=run_id,
                        partition_key="etf:SSE.510300",
                        status="SUCCEEDED",
                        attempt=1,
                        checkpoint_hash="1" * 64,
                        checkpoint_kind="canonical-partition",
                        checkpoint_updated_at=clock.now(),
                        error_json=None,
                    )
                )
                session.add(
                    DataOperationPartition(
                        run_id=run_id,
                        partition_key="etf:SZSE.159919",
                        status="FAILED",
                        attempt=1,
                        checkpoint_hash=None,
                        checkpoint_kind=None,
                        checkpoint_updated_at=None,
                        error_json={"code": "RATE_LIMITED", "retryable": True},
                    )
                )
            else:
                assert succeeded is not None and succeeded.status == "SUCCEEDED"
                assert failed is not None
                failed.status = "SUCCEEDED"
                failed.attempt = claim.attempt
                failed.checkpoint_hash = "2" * 64
                failed.checkpoint_kind = "canonical-partition"
                failed.checkpoint_updated_at = clock.now()
                failed.error_json = None
        if claim.attempt == 1:
            return ExecutionOutcome(
                status="PARTIAL",
                completed_partitions=1,
                total_partitions=2,
                processed_records=1,
                error={
                    "code": "rate-limited",
                    "stage": "PROVIDER_FETCH",
                    "retryable": True,
                    "message": "ETF data source is unavailable",
                },
            )
        return ExecutionOutcome(
            status="SUCCEEDED",
            completed_partitions=2,
            total_partitions=2,
            processed_records=1,
        )

    control_plane.register_executor(dataset_code, executor)

    assert control_plane.dispatch_once("integration:auto-retry:first") is True
    with database.session() as session:
        first = session.get(DataOperationRun, run_id)
        command = session.get(DataOperationCommand, command_id)
        assert first is not None and command is not None
        assert first.status == "QUEUED"
        assert first.attempt == 1
        assert command.status == "QUEUED"
    assert control_plane.dispatch_once("integration:auto-retry:second") is True
    assert control_plane.dispatch_once("integration:auto-retry:empty") is False

    with database.session() as session:
        finished = session.get(DataOperationRun, run_id)
        command = session.get(DataOperationCommand, command_id)
        assert finished is not None and command is not None
        assert finished.status == "SUCCEEDED"
        assert finished.attempt == 2
        assert finished.completed_partitions == 2
        assert finished.processed_records == 2
        assert command.status == "SUCCEEDED"
        assert {
            partition.partition_key: partition.status
            for partition in session.scalars(
                select(DataOperationPartition).where(DataOperationPartition.run_id == run_id)
            ).all()
        } == {
            "etf:SSE.510300": "SUCCEEDED",
            "etf:SZSE.159919": "SUCCEEDED",
        }
    assert calls == [1, 2]


def test_profile_schedule_dispatches_two_venues_and_publishes_frozen_universe(
    database: DatabaseClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """到期 profile 计划必须经统一 dispatcher 发布双市场并形成可冻结全集。"""
    clock = _Clock(datetime.now(UTC))
    _ensure_idle_slot(database)
    suffix = f"{uuid4().int % 1_000_000:06d}"
    symbols = {"SSE": suffix, "SZSE": f"{(int(suffix) + 1) % 1_000_000:06d}"}
    provider = _EtfMasterProvider(symbols)
    registry = SourceRegistry()
    registry.register(provider)
    dataset_code = "fund.etf.profile.reported"
    definition = DatasetDefinition(
        dataset_code,
        "ETF 产品资料",
        "fund",
        "沪深 ETF 产品目录与报告资料",
        "交易所 × ETF × 观察日",
        "fund.etf.master",
        ("FULL", "OBSERVATION_DATE"),
        ("OBSERVATION_DATE",),
        selector_kinds=("ETF",),
        dispatcher_ready=True,
        config_enabled=True,
        lifecycle="CANDIDATE",
        provider_id="akshare",
        upstream_source="sse-szse.official-etf-directory",
        approval_status="CANDIDATE",
        data_as_of_label="产品观察日",
    )
    control_plane = DataOperationsControlPlane(
        database=database,
        catalog={dataset_code: definition},
        source_registry=registry,
        trading_calendar=cast(Any, _OpenCalendar()),
        now=clock.now,
    )
    container = cast(
        ServiceContainer,
        SimpleNamespace(
            database=database,
            source_registry=registry,
            object_storage=object(),
            settings=SimpleNamespace(),
        ),
    )
    monkeypatch.setattr(canonical_executors, "S3RawPayloadStore", _MemoryRawStore)

    def execute_profile(claim: ExecutionClaim) -> ExecutionOutcome:
        """把计划生成的 claim 交给真实 ETF canonical executor。"""
        return canonical_executors._execute_etf(
            claim,
            container=container,
            dataset_code=dataset_code,
        )

    control_plane.register_executor(dataset_code, execute_profile)
    schedule_id = uuid4()
    revision_id = uuid4()
    selector = {
        "kind": "ETF",
        "operation": "MASTER",
        "scope": "ALL_VENUES",
        "venue": None,
        "etf": None,
    }
    frequency = {
        "kind": "TRADING_DAY",
        "timezone": "Asia/Shanghai",
        "localTime": clock.now().astimezone().strftime("%H:%M"),
        "dayOfWeek": None,
        "dayOfMonth": None,
        "intervalMinutes": None,
        "calendarCode": "SSE-SZSE",
    }
    policy = {"policyVersion": 1, "dateResolution": "SCHEDULED_LOCAL_DATE"}
    with database.transaction() as session:
        session.add(
            DataOperationSchedule(
                schedule_id=schedule_id,
                dataset_code=dataset_code,
                mode="OBSERVATION_DATE",
                selector_json=selector,
                target_policy_json=policy,
                frequency_json=frequency,
                misfire_policy="RUN_ONCE",
                coalesce=True,
                enabled=True,
                version=1,
                revision_id=revision_id,
                recent_run_at=None,
                next_run_at=clock.now(),
                updated_at=clock.now(),
                updated_by_actor_ref="system:integration",
            )
        )
        session.flush()
        session.add(
            DataOperationScheduleRevision(
                revision_id=revision_id,
                schedule_id=schedule_id,
                version=1,
                change_kind="UPSERT",
                dataset_code=dataset_code,
                mode="OBSERVATION_DATE",
                selector_json=selector,
                target_policy_json=policy,
                frequency_json=frequency,
                misfire_policy="RUN_ONCE",
                coalesce=True,
                enabled=True,
                before_hash="3" * 64,
                after_hash="4" * 64,
                actor_ref="system:integration",
                request_id=f"integration:{schedule_id}",
                created_at=clock.now(),
            )
        )

    assert control_plane.scheduler_tick() == 1
    assert control_plane.dispatch_once("integration:etf-profile") is True

    with database.session() as session:
        versions = resolve_current_etf_profile_data_versions(session)
        universe = load_frozen_etf_universe(session, profile_data_versions=versions)
        run = session.scalars(
            select(DataOperationRun)
            .where(DataOperationRun.dataset_code == dataset_code)
            .order_by(DataOperationRun.requested_at.desc())
        ).first()
        assert run is not None
        partitions = {
            partition.partition_key: partition.status
            for partition in session.scalars(
                select(DataOperationPartition).where(DataOperationPartition.run_id == run.run_id)
            ).all()
        }

    assert provider.venues == ["SSE", "SZSE"]
    assert run.status == "SUCCEEDED"
    assert run.total_partitions == 2
    assert partitions == {"venue:SSE": "SUCCEEDED", "venue:SZSE": "SUCCEEDED"}
    assert universe.count >= 2
    assert {identifier.qualified_key for identifier in universe.identifiers} >= {
        f"SSE.{symbols['SSE']}",
        f"SZSE.{symbols['SZSE']}",
    }


def test_yielded_long_run_moves_to_queue_tail_and_keeps_same_run(
    database: DatabaseClient,
) -> None:
    """内部批次让位后先执行已等待命令，再以同一 run 和累计进度续跑。"""
    clock = _Clock(datetime.now(UTC) + timedelta(days=2, hours=9))
    long_dataset = f"integration.stock-connect-long.{uuid4()}"
    short_dataset = f"integration.short.{uuid4()}"
    _ensure_idle_slot(database)
    long_command_id, long_run_id = _insert_queued_run(
        database,
        dataset_code=long_dataset,
        now=clock.now() - timedelta(hours=1),
    )
    short_command_id, _short_run_id = _insert_queued_run(
        database,
        dataset_code=short_dataset,
        now=clock.now() - timedelta(minutes=30),
    )
    control_plane = _control_plane(
        database,
        clock,
        long_dataset,
        short_dataset,
    )
    execution_order: list[str] = []
    long_attempts = 0

    def execute_long(claim: ExecutionClaim) -> ExecutionOutcome:
        """首批主动让位，第二次领取同一 run 后完成剩余分区。"""
        nonlocal long_attempts
        assert claim.run_id == long_run_id
        long_attempts += 1
        execution_order.append("long")
        if long_attempts == 1:
            return ExecutionOutcome(
                status="YIELDED",
                completed_partitions=5,
                total_partitions=12,
                processed_records=5,
            )
        return ExecutionOutcome(
            status="SUCCEEDED",
            completed_partitions=12,
            total_partitions=12,
            processed_records=7,
        )

    def execute_short(_claim: ExecutionClaim) -> ExecutionOutcome:
        """记录短命令在长任务两个内部批次之间获得全局槽。"""
        execution_order.append("short")
        return ExecutionOutcome(status="SUCCEEDED")

    control_plane.register_executor(long_dataset, execute_long)
    control_plane.register_executor(short_dataset, execute_short)

    assert control_plane.dispatch_once("integration:yield:first") is True
    assert control_plane.dispatch_once("integration:yield:short") is True
    assert control_plane.dispatch_once("integration:yield:resume") is True

    assert execution_order == ["long", "short", "long"]
    with database.session() as session:
        long_run = session.get(DataOperationRun, long_run_id)
        long_command = session.get(DataOperationCommand, long_command_id)
        short_command = session.get(DataOperationCommand, short_command_id)
        assert long_run is not None
        assert long_run.status == "SUCCEEDED"
        assert long_run.attempt == 2
        assert long_run.recovery_attempts == 0
        assert long_run.completed_partitions == 12
        assert long_run.processed_records == 12
        assert long_command is not None and long_command.status == "SUCCEEDED"
        assert short_command is not None and short_command.status == "SUCCEEDED"


def test_postgres_slot_serializes_workers_reaps_lease_and_rejects_stale_fence(
    database: DatabaseClient,
) -> None:
    """验证双 worker 仅一人 claim，租约恢复后旧 token 不能写终态、checkpoint 或发布。"""
    clock = _Clock(datetime.now(UTC) + timedelta(days=2, hours=9))
    dataset_code = f"integration.slot.{uuid4()}"
    control_plane = _control_plane(database, clock, dataset_code)
    _ensure_idle_slot(database)
    _command_id, run_id = _insert_queued_run(database, dataset_code=dataset_code, now=clock.now())

    barrier = Barrier(3)
    guard = Lock()
    claims: list[ExecutionClaim | None] = []
    failures: list[BaseException] = []

    def claim(worker_id: str) -> None:
        """让两个独立线程同时通过 PostgreSQL 行锁竞争同一个 child run。"""
        try:
            barrier.wait(timeout=10)
            result = control_plane.claim_next_run(worker_id)
            with guard:
                claims.append(result)
        except BaseException as error:  # pragma: no cover - 仅保留线程异常供主断言。
            with guard:
                failures.append(error)

    workers = [Thread(target=claim, args=(f"integration-worker-{index}",)) for index in range(2)]
    for worker in workers:
        worker.start()
    barrier.wait(timeout=10)
    for worker in workers:
        worker.join(timeout=10)

    assert not failures
    assert not any(worker.is_alive() for worker in workers)
    active_claims = [claim for claim in claims if claim is not None]
    assert len(active_claims) == 1
    first_claim = active_claims[0]
    assert first_claim.run_id == run_id

    # worker 崩溃后等待 lease 失效；reaper 必须保留 run UUID 和冻结输入，仅重新入队。
    clock.value += timedelta(seconds=61)
    assert control_plane.reap_expired_slots() == 1
    recovered_claim = control_plane.claim_next_run("integration-worker-recovered")
    assert recovered_claim is not None
    assert recovered_claim.run_id == run_id
    assert recovered_claim.fencing_token > first_claim.fencing_token

    stale_outcome = ExecutionOutcome(
        status="SUCCEEDED",
        checkpoint_kind="opaque",
        checkpoint_position="stale-checkpoint-must-not-persist",
    )
    assert (
        control_plane.complete_run(
            run_id=run_id,
            fencing_token=first_claim.fencing_token,
            outcome=stale_outcome,
        )
        is False
    )

    stale_execution = FencedExecution(
        database=database,
        run_id=run_id,
        fencing_token=first_claim.fencing_token,
        finalizer=_unexpected_finalizer,
    )
    with pytest.raises(FencingTokenLost):
        with fenced_execution(stale_execution):
            with database.transaction():
                pass

    with database.session() as session:
        run = session.get(DataOperationRun, run_id)
        assert run is not None
        assert run.status == "RUNNING"
        assert run.fencing_token == recovered_claim.fencing_token
        assert run.recovery_attempts == 1
        assert (
            session.get(DataOperationPartition, {"run_id": run_id, "partition_key": "default"})
            is None
        )

    # 当前 owner 能正常完成，证明 stale 拒绝没有破坏恢复后的有效执行。
    assert (
        control_plane.complete_run(
            run_id=run_id,
            fencing_token=recovered_claim.fencing_token,
            outcome=ExecutionOutcome(
                status="FAILED",
                error={
                    "code": "integration-stop",
                    "stage": "RECOVERY",
                    "retryable": False,
                    "message": "integration verification completed",
                },
            ),
        )
        is True
    )


def test_many_normal_yields_do_not_consume_worker_loss_recovery_budget(
    database: DatabaseClient,
) -> None:
    """正常让位超过旧 attempt 上限后，首次真实租约丢失仍须恢复同一 run。"""
    clock = _Clock(datetime.now(UTC) + timedelta(days=2, hours=14))
    dataset_code = f"integration.recovery-budget.{uuid4()}"
    control_plane = _control_plane(database, clock, dataset_code)
    _ensure_idle_slot(database)
    _command_id, run_id = _insert_queued_run(
        database,
        dataset_code=dataset_code,
        now=clock.now(),
    )

    def yield_batch(_claim: ExecutionClaim) -> ExecutionOutcome:
        """模拟一个已持久化分区后主动释放全局槽的正常批次。"""
        return ExecutionOutcome(
            status="YIELDED",
            completed_partitions=1,
            total_partitions=10,
            processed_records=1,
        )

    control_plane.register_executor(dataset_code, yield_batch)
    for index in range(4):
        assert control_plane.dispatch_once(f"integration:yield:{index}") is True

    crashed_claim = control_plane.claim_next_run("integration:crashed-after-many-yields")
    assert crashed_claim is not None
    assert crashed_claim.attempt == 5
    clock.value += timedelta(seconds=61)
    assert control_plane.reap_expired_slots() == 1

    with database.session() as session:
        recovered = session.get(DataOperationRun, run_id)
        assert recovered is not None
        assert recovered.status == "QUEUED"
        assert recovered.attempt == 5
        assert recovered.recovery_attempts == 1

    final_claim = control_plane.claim_next_run("integration:recovered-after-many-yields")
    assert final_claim is not None
    assert control_plane.complete_run(
        run_id=run_id,
        fencing_token=final_claim.fencing_token,
        outcome=ExecutionOutcome(
            status="FAILED",
            error={
                "code": "integration-stop",
                "stage": "RECOVERY",
                "retryable": False,
                "message": "integration verification completed",
            },
        ),
    )
    with database.session() as session:
        slot = session.get(DataOperationExecutionSlot, "global")
        run = session.get(DataOperationRun, run_id)
        assert slot is not None and slot.state == "IDLE"
        assert run is not None and run.status == "FAILED"


def test_dispatch_seals_actual_source_batch_on_the_control_run(
    database: DatabaseClient,
) -> None:
    """真实来源观察必须与控制面 run 同事务收敛，且数据库禁止事后改写关系。"""
    clock = _Clock(datetime.now(UTC) + timedelta(days=2, hours=14))
    dataset_code = f"integration.run-source.{uuid4()}"
    control_plane = _control_plane(database, clock, dataset_code)
    _ensure_idle_slot(database)
    _command_id, run_id = _insert_queued_run(
        database,
        dataset_code=dataset_code,
        now=clock.now(),
    )
    source_ids: list[UUID] = []

    def execute_with_source(_claim: ExecutionClaim) -> ExecutionOutcome:
        """登记真实 `SourceBatch`，由当前 fenced execution 自动收集其 UUID。"""
        with database.transaction() as session:
            source_ids.append(
                record_source_observation(
                    session,
                    provider_id="integration-provider",
                    capability="integration.data-operations",
                    source_payload_sha256="a" * 64,
                    raw_uri="unretained://sha256/" + "a" * 64,
                    observed_at=clock.now(),
                    created_at=clock.now(),
                    upstream_source="integration.real-upstream",
                    adapter_version="integration-v1",
                    schema_fingerprint="b" * 64,
                )
            )
        return ExecutionOutcome(status="SUCCEEDED", processed_records=1)

    control_plane.register_executor(dataset_code, execute_with_source)
    assert control_plane.dispatch_once("integration:run-source") is True
    assert len(source_ids) == 1

    with database.session() as session:
        link = session.get(
            DataOperationRunSourceBatch,
            {"run_id": run_id, "source_batch_id": source_ids[0]},
        )
        assert link is not None
        assert link.linked_at == clock.now()

    with pytest.raises(DBAPIError):
        with database.transaction() as session:
            session.execute(
                update(DataOperationRunSourceBatch)
                .where(
                    DataOperationRunSourceBatch.run_id == run_id,
                    DataOperationRunSourceBatch.source_batch_id == source_ids[0],
                )
                .values(linked_at=clock.now() + timedelta(seconds=1))
            )


def test_failed_run_with_rolled_back_source_batch_seals_failed_terminal(
    database: DatabaseClient,
) -> None:
    """执行器失败导致来源批次事务回滚时，控制面必须正常落账失败而不是抛未知批次异常。"""
    # 时钟取未来日期：fenced execution 的 lease 用真实时间校验，落后时钟会让
    # executor 内的 database.transaction() 在进入业务逻辑前就抛 FencingTokenLost。
    clock = _Clock(datetime.now(UTC) + timedelta(days=2, hours=14))
    dataset_code = f"integration.run-fail.{uuid4()}"
    control_plane = _control_plane(database, clock, dataset_code)
    _ensure_idle_slot(database)
    _command_id, run_id = _insert_queued_run(
        database,
        dataset_code=dataset_code,
        now=clock.now(),
    )

    def failing_executor(_claim: ExecutionClaim) -> ExecutionOutcome:
        """与真实 canonical 路径一致：事务内登记来源批次后失败，使批次行随事务回滚。

        批次 UUID 已进入 `FencedExecution` 内存，但对应行不存在；失败终态必须
        忽略这些幽灵引用并正常落账，不能把失败原因掩盖成运行时崩溃。
        """
        execution = current_fenced_execution()
        assert execution is not None
        with database.transaction() as session:
            execution.record_source_batch(
                record_source_observation(
                    session,
                    provider_id="integration-provider",
                    capability=dataset_code,
                    source_payload_sha256="c" * 64,
                    raw_uri="unretained://sha256/" + "c" * 64,
                    observed_at=clock.now(),
                    created_at=clock.now(),
                    upstream_source="integration.real-upstream",
                    adapter_version="integration-v1",
                    schema_fingerprint="d" * 64,
                )
            )
            # 模拟批次登记后的 canonical 写入失败：同一事务回滚。
            raise RuntimeError("simulated canonical write failure")

    control_plane.register_executor(dataset_code, failing_executor)
    assert control_plane.dispatch_once("integration:run-fail") is True

    with database.session() as session:
        run = session.get(DataOperationRun, run_id)
        assert run is not None
        assert run.status == "FAILED"
        assert run.error_json is not None
        assert run.error_json["code"] == "execution-failed"
        # 失败 run 不关联来源批次，也不能有幽灵批次链接。
        assert (
            session.execute(
                select(DataOperationRunSourceBatch).where(
                    DataOperationRunSourceBatch.run_id == run_id
                )
            ).first()
            is None
        )


def test_scheduler_persists_misfire_audit_and_coalesces_one_command_per_fire(
    database: DatabaseClient,
) -> None:
    """验证 SKIP 与 RUN_ONCE/coalesce 产生可审计 fire，且 tick 只入 command 队列。"""
    clock = _Clock(datetime.now(UTC) + timedelta(days=2, hours=2))
    skip_dataset = f"integration.schedule.skip.{uuid4()}"
    run_once_dataset = f"integration.schedule.run-once.{uuid4()}"
    control_plane = _control_plane(database, clock, skip_dataset, run_once_dataset)
    first_due = clock.now() - timedelta(days=3)
    skip_schedule_id = _insert_schedule(
        database,
        dataset_code=skip_dataset,
        now=clock.now(),
        first_due=first_due,
        misfire_policy="SKIP",
        coalesce=False,
    )
    run_once_schedule_id = _insert_schedule(
        database,
        dataset_code=run_once_dataset,
        now=clock.now(),
        first_due=first_due,
        misfire_policy="RUN_ONCE",
        coalesce=True,
    )

    assert control_plane.scheduler_tick() == 1
    assert control_plane.scheduler_tick() == 0

    with database.session() as session:
        skipped = session.scalars(
            select(DataOperationScheduleFire)
            .where(DataOperationScheduleFire.schedule_id == skip_schedule_id)
            .order_by(DataOperationScheduleFire.scheduled_for)
        ).all()
        queued = session.scalars(
            select(DataOperationScheduleFire).where(
                DataOperationScheduleFire.schedule_id == run_once_schedule_id
            )
        ).all()
        assert len(skipped) == 4
        assert all(fire.outcome == "SKIPPED" for fire in skipped)
        assert all(fire.reason_code == "schedule-misfire-skipped" for fire in skipped)
        assert len(queued) == 1
        queued_fire = queued[0]
        assert queued_fire.outcome == "QUEUED"
        assert queued_fire.coalesced_count == 3
        assert queued_fire.command_id is not None
        assert queued_fire.target_json == {
            "datasetCode": run_once_dataset,
            "mode": "FULL",
            "selector": {"kind": "GLOBAL"},
            "dateFrom": None,
            "dateTo": None,
            "observationDate": None,
        }
        command = session.get(DataOperationCommand, queued_fire.command_id)
        assert command is not None
        assert command.status == "QUEUED"
        assert command.actor_ref == f"system:schedule/{run_once_schedule_id}"
        run = session.scalar(
            select(DataOperationRun).where(DataOperationRun.command_id == queued_fire.command_id)
        )
        assert run is not None
        assert run.status == "QUEUED"
        assert run.execution_intent_json is not None
        assert run.execution_intent_json["scheduleFireId"] == str(queued_fire.fire_id)
