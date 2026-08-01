"""Fenced canonical 发布回滚后控制面失败终态的 PostgreSQL 集成回归。"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable, Generator, Sequence
from datetime import UTC, date, datetime
from types import SimpleNamespace
from typing import NoReturn, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, func, select, text

from service_data_sync.application.ports.data_source import ProviderBatch, SourceRequest
from service_data_sync.application.ports.market_data import RawPayload
from service_data_sync.application.ports.sector_eod import (
    ArchivedSectorEodObservation,
    PublishedSectorEodSnapshot,
    SectorEodExecutionMode,
    SectorEodHistoricalReference,
    SectorEodQualityResult,
    SectorEodRepository,
    SectorEodRun,
)
from service_data_sync.application.ports.sector_market_data import StoredSector
from service_data_sync.application.ports.sector_membership import (
    PublishedSectorMembershipSnapshot,
    SectorMembershipRepository,
    SectorMembershipRun,
)
from service_data_sync.application.sector.eod_snapshot_sync import (
    SectorEodSnapshotSyncService,
)
from service_data_sync.application.sector.membership_sync import (
    SectorMembershipSyncService,
)
from service_data_sync.application.source_registry import SourceRegistry
from service_data_sync.bootstrap import financial_derived
from service_data_sync.bootstrap.container import ServiceContainer
from service_data_sync.domain.sector import SectorIdentifier, SectorScheme
from service_data_sync.infrastructure.data_operations import canonical_executors
from service_data_sync.infrastructure.data_operations.control_plane import (
    DataOperationsControlPlane,
    DatasetDefinition,
    ExecutionClaim,
    ExecutionOutcome,
)
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.database.fenced_execution import (
    current_fenced_execution,
)
from service_data_sync.infrastructure.database.models.operations import (
    DataOperationCommand,
    DataOperationExecutionSlot,
    DataOperationRun,
)
from service_data_sync.infrastructure.database.models.publication.dataset_publication import (
    DatasetPublication,
)

pytestmark = pytest.mark.integration

_OBSERVED_AT = datetime(2026, 7, 30, 9, tzinfo=UTC)
_TARGET_DATE = date(2026, 7, 30)


class _UnusedRegistry:
    """为直接插入的 queued run 提供不会被访问的最小来源注册表。"""

    def provider_ids(self) -> frozenset[str]:
        """返回空来源集合；本文件执行器均不经控制面冻结来源。"""
        return frozenset()


class _RawStore:
    """为应用编排返回不可回放摘要 URI，不写外部对象存储。"""

    def put(self, payload: RawPayload) -> str:
        """基于真实内容摘要返回成功路径不可回放引用。"""
        return f"unretained://sha256/{payload.content_sha256}"

    def get(self, uri: str) -> bytes:
        """本文件不覆盖 replay，意外读取应立即失败。"""
        raise AssertionError(f"unexpected replay: {uri}")


class _MembershipSource:
    """返回一个可解析的行业成分标准批次。"""

    provider_id = "integration-sector-membership"

    def capabilities(self) -> frozenset[str]:
        """仅声明板块成分快照能力。"""
        return frozenset({"sector.membership.snapshot.raw"})

    async def fetch(self, request: SourceRequest) -> ProviderBatch:
        """按请求板块返回一个成员，避免发布回滚测试依赖网络。"""
        parameters = dict(request.parameters)
        payload = json.dumps(
            {
                "schema": "quant-v2.sector-membership-snapshot.v1",
                "sectorScheme": parameters["sectorScheme"],
                "sector": parameters["sector"],
                "members": [{"sourceSymbol": "600519", "sourceName": "贵州茅台"}],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        return ProviderBatch(
            provider_id=self.provider_id,
            capability=request.capability,
            payload=payload,
            raw_payload=payload,
            raw_content_type="application/json",
            observed_at=_OBSERVED_AT,
            upstream_source="integration.membership",
            adapter_version="integration-v1",
            schema_fingerprint="1" * 64,
        )


class _FailingMembershipRepository:
    """在 release SQL 已执行后失败，并用新事务记录 scheme run 终态。"""

    def __init__(self, database: DatabaseClient, marker: str) -> None:
        """保存真实数据库与只属于当前测试的 publication 标记。"""
        self._database = database
        self._marker = marker
        self._sector = StoredSector(
            sector_key=1,
            sector_id=uuid4(),
            identifier=SectorIdentifier(SectorScheme.EASTMONEY_INDUSTRY, "BKTEST"),
            name="集成行业",
            status="ACTIVE",
        )
        self.finished_status: str | None = None

    def list_active_sectors(self, *, scheme: SectorScheme) -> Sequence[StoredSector]:
        """返回一个 ACTIVE 行业分区。"""
        assert scheme is SectorScheme.EASTMONEY_INDUSTRY
        return (self._sector,)

    def start_run(
        self,
        *,
        scheme: SectorScheme,
        observation_date: date,
        sectors: Sequence[StoredSector],
    ) -> SectorMembershipRun:
        """返回冻结分区对应的稳定应用 run。"""
        assert tuple(sectors) == (self._sector,)
        return SectorMembershipRun(uuid4(), scheme, observation_date)

    def publish_snapshot(self, **_kwargs: object) -> PublishedSectorMembershipSnapshot:
        """先返回完整分区，使用例进入最终 release 事务。"""
        return PublishedSectorMembershipSnapshot(
            snapshot_id=uuid4(),
            observed_at=_OBSERVED_AT,
            complete=True,
            inserted_interval_count=1,
            closed_interval_count=0,
            pending_count=0,
            quarantine_count=0,
        )

    def mark_partition_completed(
        self,
        *,
        run: SectorMembershipRun,
        sector: StoredSector,
        publication: PublishedSectorMembershipSnapshot,
    ) -> None:
        """确认完整分区 checkpoint 已到达 release 前。"""
        assert run.scheme is SectorScheme.EASTMONEY_INDUSTRY
        assert sector == self._sector
        assert publication.complete is True

    def mark_partition_failed(self, **_kwargs: object) -> None:
        """本回归只覆盖 publication 持久化失败，不应进入来源失败分支。"""
        raise AssertionError("unexpected membership provider failure")

    def publish_release(
        self,
        *,
        scheme: SectorScheme,
        observation_date: date,
        before_final_publication: Callable[[], None] | None = None,
    ) -> NoReturn:
        """武装终态并写入候选 publication 后制造事务内持久化失败。"""
        assert scheme is SectorScheme.EASTMONEY_INDUSTRY
        assert observation_date == _TARGET_DATE
        assert before_final_publication is not None
        with self._database.transaction() as session:
            before_final_publication()
            _insert_marker(session, self._marker)
            session.flush()
            raise RuntimeError("membership publication failed after arm")

    def finish_run(self, *, run: SectorMembershipRun, status: str) -> None:
        """在独立账本事务中记录失败，复现原先会误触 finalizer 的边界。"""
        assert run.scheme is SectorScheme.EASTMONEY_INDUSTRY
        with self._database.transaction() as session:
            session.execute(text("SELECT 1"))
        self.finished_status = status


class _EodSource:
    """返回一个可通过 EOD 质量门的最小标准横截面。"""

    provider_id = "integration-sector-eod"

    def capabilities(self) -> frozenset[str]:
        """仅声明 EOD 横截面能力。"""
        return frozenset({"sector.quote.eod.snapshot.raw"})

    async def fetch(self, request: SourceRequest) -> ProviderBatch:
        """返回与目标日和分类体系严格一致的标准载荷。"""
        payload = json.dumps(
            {
                "schema": "quant-v2.sector-eod-snapshot.v1",
                "sectorScheme": "eastmoney.industry",
                "tradeDate": _TARGET_DATE.isoformat(),
                "quotes": [
                    {
                        "code": "BKTEST",
                        "name": "集成行业",
                        "latestValue": "1000",
                        "changeValue": "10",
                        "changePercent": "1",
                        "marketValue": "1000000",
                        "turnoverPercent": "3",
                        "advancers": 10,
                        "decliners": 3,
                        "leaderName": "贵州茅台",
                        "leaderChangePercent": "2",
                    }
                ],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        return ProviderBatch(
            provider_id=self.provider_id,
            capability=request.capability,
            payload=payload,
            raw_payload=payload,
            raw_content_type="application/json",
            observed_at=_OBSERVED_AT,
            upstream_source="integration.sector-eod",
            adapter_version="integration-v1",
            schema_fingerprint="2" * 64,
        )


class _OpenCalendar:
    """把唯一目标日明确标记为开市日。"""

    def is_open(self, *, trade_date: date) -> bool:
        """只接受本回归冻结的目标日。"""
        assert trade_date == _TARGET_DATE
        return True


class _FailingEodRepository:
    """在 EOD publication SQL 后失败，并以新事务记录失败 checkpoint。"""

    def __init__(self, database: DatabaseClient, marker: str) -> None:
        """保存真实数据库、publication 标记和当前应用 run。"""
        self._database = database
        self._marker = marker
        self._run: SectorEodRun | None = None
        self.failed_code: str | None = None

    def start_run(
        self, *, scheme: SectorScheme, trade_date: date, reuse_archived_raw: bool
    ) -> SectorEodRun:
        """创建本次 EOD 分区租约。"""
        assert reuse_archived_raw is False
        self._run = SectorEodRun(uuid4(), uuid4(), scheme, trade_date)
        return self._run

    def renew_lease(self, *, run: SectorEodRun) -> None:
        """确认所有长步骤仍使用同一租约。"""
        assert run == self._run

    def mark_fetched(self, *, run: SectorEodRun) -> None:
        """确认来源阶段仍属于当前租约。"""
        assert run == self._run

    def record_archived_observation(
        self, *, run: SectorEodRun, **kwargs: object
    ) -> ArchivedSectorEodObservation:
        """把已归档摘要投影为后续规范化所需的来源观察。"""
        assert run == self._run
        observed_at = kwargs["observed_at"]
        assert isinstance(observed_at, datetime)
        return ArchivedSectorEodObservation(
            source_batch_id=uuid4(),
            raw_uri=str(kwargs["raw_uri"]),
            provider_id=str(kwargs["provider_id"]),
            observed_at=observed_at,
            adapter_version=str(kwargs["adapter_version"]),
            schema_fingerprint=str(kwargs["schema_fingerprint"]),
        )

    def get_historical_reference(
        self, *, scheme: SectorScheme, before_trade_date: date
    ) -> SectorEodHistoricalReference | None:
        """首个测试快照没有跨日参考。"""
        assert scheme is SectorScheme.EASTMONEY_INDUSTRY
        assert before_trade_date == _TARGET_DATE
        return None

    def mark_normalized(self, *, run: SectorEodRun) -> None:
        """确认规范化阶段仍属于当前租约。"""
        assert run == self._run

    def store_quarantined_snapshot(self, **_kwargs: object) -> None:
        """合法最小横截面不应进入质量隔离。"""
        raise AssertionError("unexpected EOD quarantine")

    def publish_snapshot(
        self,
        *,
        quality_results: Sequence[SectorEodQualityResult],
        **_kwargs: object,
    ) -> PublishedSectorEodSnapshot:
        """写入候选 publication 后制造事务内持久化失败。"""
        assert quality_results
        with self._database.transaction() as session:
            _insert_marker(session, self._marker)
            session.flush()
            raise RuntimeError("sector EOD publication failed after arm")

    def mark_failed(self, *, run: SectorEodRun, error_code: str) -> None:
        """用独立 checkpoint 事务复现原先会误写控制面成功的路径。"""
        assert run == self._run
        with self._database.transaction() as session:
            session.execute(text("SELECT 1"))
        self.failed_code = error_code


@pytest.fixture
def database() -> Generator[DatabaseClient]:
    """连接显式启用的隔离 PostgreSQL 集成库。"""
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


def test_financial_derived_failure_after_arm_finishes_failed_without_publication(
    database: DatabaseClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """衍生财务发布事务失败后，失败账本不得把控制面 run 误写成功。"""
    dataset_code = "financial.derived-metric"
    marker = f"integration.financial.rollback.{uuid4()}"
    run_id = _insert_queued_run(
        database,
        dataset_code=dataset_code,
        selector={"kind": "INSTRUMENT", "exchange": "SSE", "symbol": "600519"},
    )

    class _FailingDerivedService:
        """在 callback 武装后写入 publication 标记并制造回滚。"""

        def __init__(self, *, repository: object) -> None:
            """接受生产组合根依赖但由测试直接控制事务故障。"""
            del repository

        def derive(
            self,
            *,
            before_final_publication: Callable[[], None] | None = None,
            **_kwargs: object,
        ) -> NoReturn:
            """在真实数据库事务中复现 publication-after-arm 失败。"""
            assert before_final_publication is not None
            with database.transaction() as session:
                before_final_publication()
                _insert_marker(session, marker)
                session.flush()
                raise RuntimeError("financial derived publication failed after arm")

    monkeypatch.setattr(financial_derived, "FinancialDerivedMetricService", _FailingDerivedService)
    control_plane = _control_plane(database, dataset_code)
    container = cast(ServiceContainer, SimpleNamespace(database=database))

    def execute(claim: ExecutionClaim) -> ExecutionOutcome:
        """调用生产衍生指标 canonical executor。"""
        return canonical_executors._execute_financial_derived_metric(claim, container=container)

    control_plane.register_executor(dataset_code, execute)
    assert control_plane.dispatch_once("integration-financial-rollback") is True

    _assert_failed_and_released(database, run_id=run_id, marker=marker)


def test_membership_release_failure_after_arm_finishes_failed_without_publication(
    database: DatabaseClient,
) -> None:
    """成分 release 回滚后的 finally 账本事务不得触发控制面成功 finalizer。"""
    dataset_code = "integration.sector-membership.rollback"
    marker = f"integration.membership.rollback.{uuid4()}"
    run_id = _insert_queued_run(
        database,
        dataset_code=dataset_code,
        selector={"kind": "GLOBAL"},
    )
    repository = _FailingMembershipRepository(database, marker)
    service = SectorMembershipSyncService(
        source=_MembershipSource(),
        repository=cast(SectorMembershipRepository, repository),
        raw_payload_store=_RawStore(),
        retry_delay_seconds=0,
    )
    control_plane = _control_plane(database, dataset_code)

    def execute(_claim: ExecutionClaim) -> ExecutionOutcome:
        """调用生产成员同步用例并把 release callback 绑定当前 fenced execution。"""
        execution = current_fenced_execution()
        assert execution is not None
        asyncio.run(
            service.sync_scheme(
                scheme=SectorScheme.EASTMONEY_INDUSTRY,
                observation_date=_TARGET_DATE,
                before_final_publication=execution.arm_terminal_write,
            )
        )
        raise AssertionError("membership failure must propagate")

    control_plane.register_executor(dataset_code, execute)
    assert control_plane.dispatch_once("integration-membership-rollback") is True

    _assert_failed_and_released(database, run_id=run_id, marker=marker)
    assert repository.finished_status == "partial"


def test_sector_eod_failure_after_arm_finishes_failed_without_publication(
    database: DatabaseClient,
) -> None:
    """EOD publication 回滚后的 mark_failed 事务不得触发控制面成功 finalizer。"""
    dataset_code = "integration.sector-eod.rollback"
    marker = f"integration.sector-eod.rollback.{uuid4()}"
    run_id = _insert_queued_run(
        database,
        dataset_code=dataset_code,
        selector={"kind": "GLOBAL"},
    )
    repository = _FailingEodRepository(database, marker)
    service = SectorEodSnapshotSyncService(
        source=_EodSource(),
        repository=cast(SectorEodRepository, repository),
        raw_payload_store=_RawStore(),
        trading_calendar=_OpenCalendar(),
    )
    control_plane = _control_plane(database, dataset_code)

    def execute(_claim: ExecutionClaim) -> ExecutionOutcome:
        """调用生产 EOD 用例并把 publication callback 绑定当前 fenced execution。"""
        execution = current_fenced_execution()
        assert execution is not None
        asyncio.run(
            service.sync(
                scheme=SectorScheme.EASTMONEY_INDUSTRY,
                trade_date=_TARGET_DATE,
                source_cutoff_at=datetime(2026, 7, 30, 8, 30, tzinfo=UTC),
                execution_mode=SectorEodExecutionMode.PUBLISH,
                before_final_publication=execution.arm_terminal_write,
            )
        )
        raise AssertionError("sector EOD failure must propagate")

    control_plane.register_executor(dataset_code, execute)
    assert control_plane.dispatch_once("integration-sector-eod-rollback") is True

    _assert_failed_and_released(database, run_id=run_id, marker=marker)
    assert repository.failed_code == "persistence-or-quality"


def _definition(dataset_code: str) -> DatasetDefinition:
    """构造只供直接 queued run 使用的 providerless 测试目录项。"""
    return DatasetDefinition(
        dataset_code=dataset_code,
        display_name="Fenced 回滚集成数据集",
        domain="integration",
        description="验证 publication 回滚后的控制面失败终态",
        grain="测试运行",
        capability=None,
        modes=("FULL",),
        schedule_modes=(),
        selector_kinds=("GLOBAL", "INSTRUMENT"),
        dispatcher_ready=True,
        config_enabled=True,
        providerless=True,
    )


def _control_plane(database: DatabaseClient, dataset_code: str) -> DataOperationsControlPlane:
    """创建只含一个回归数据集的真实 PostgreSQL 控制面。"""
    return DataOperationsControlPlane(
        database=database,
        catalog={dataset_code: _definition(dataset_code)},
        source_registry=cast(SourceRegistry, _UnusedRegistry()),
    )


def _insert_queued_run(
    database: DatabaseClient,
    *,
    dataset_code: str,
    selector: dict[str, object],
) -> UUID:
    """插入一个 queued child run，并要求共享测试库的全局槽处于空闲。"""
    command_id = uuid4()
    run_id = uuid4()
    now = datetime.now(UTC)
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
        else:
            assert slot.state == "IDLE"
        session.add(
            DataOperationCommand(
                command_id=command_id,
                submission_id=None,
                status="QUEUED",
                actor_ref="system:fenced-rollback-test",
                actor_role="SYSTEM",
                reason="验证发布回滚不会产生伪成功",
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
                    "selector": selector,
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
    return run_id


def _insert_marker(session: object, marker: str) -> None:
    """向当前事务加入只属于该回归的 publication 行。"""
    assert hasattr(session, "add")
    session.add(  # type: ignore[union-attr]  # 运行时由真实 SQLAlchemy Session 提供。
        DatasetPublication(
            publication_id=uuid4(),
            dataset=marker,
            partition_key="default",
            data_version=uuid4(),
            release_id=None,
            quality_status="passed",
            effective_as_of=_TARGET_DATE,
            knowledge_cutoff=_OBSERVED_AT,
            published_at=_OBSERVED_AT,
            superseded_at=None,
        )
    )


def _assert_failed_and_released(
    database: DatabaseClient,
    *,
    run_id: UUID,
    marker: str,
) -> None:
    """断言 run 失败、全局槽释放且回滚 publication 对消费者不可见。"""
    with database.session() as session:
        run = session.get(DataOperationRun, run_id)
        slot = session.get(DataOperationExecutionSlot, "global")
        publication_count = session.scalar(
            select(func.count())
            .select_from(DatasetPublication)
            .where(DatasetPublication.dataset == marker)
        )
        assert run is not None
        assert run.status == "FAILED"
        assert run.error_json is not None
        assert slot is not None
        assert slot.state == "IDLE"
        assert slot.run_id is None
        assert publication_count == 0
