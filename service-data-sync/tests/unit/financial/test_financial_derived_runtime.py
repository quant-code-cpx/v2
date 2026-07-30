"""平台派生财务运行账本、CLI 与 Celery 显式任务的单元测试。"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest
from celery import Celery
from sqlalchemy.orm import Session

from service_data_sync.application.financial.derived import FinancialDerivationResult
from service_data_sync.application.legacy_entrypoints import (
    LEGACY_ENTRYPOINT_UNAVAILABLE,
    LegacyEntryPointUnavailable,
)
from service_data_sync.application.ports.financial_derived import FinancialDerivedPublication
from service_data_sync.bootstrap import financial_derived as bootstrap
from service_data_sync.bootstrap.settings import Settings
from service_data_sync.domain.equity import Exchange
from service_data_sync.entrypoints import financial_derived as entrypoint
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.messaging import financial_derived_tasks


class ScalarResult:
    """模拟运行账本 SQL 所需的单标量读取接口。"""

    def __init__(self, scalar: object | None = None) -> None:
        """保存本次语句返回的标量。"""
        self.scalar = scalar

    def scalar_one_or_none(self) -> object | None:
        """返回唯一标量或空结果。"""
        return self.scalar


class LedgerConnection:
    """按队列返回运行账本结果，并记录写入语句。"""

    def __init__(self, responses: tuple[ScalarResult, ...]) -> None:
        """初始化结果队列与空语句清单。"""
        self.responses = list(responses)
        self.executed: list[Any] = []

    def execute(self, statement: Any) -> ScalarResult:
        """记录 SQL 并消费一个预置标量结果。"""
        self.executed.append(statement)
        if not self.responses:
            raise AssertionError(f"unexpected SQL execution: {statement}")
        return self.responses.pop(0)


class LedgerDatabase:
    """为运行账本辅助函数提供无网络事务上下文。"""

    def __init__(self, connection: LedgerConnection) -> None:
        """保存唯一记录连接。"""
        self.connection = connection

    @contextmanager
    def transaction(self) -> Iterator[Session]:
        """把记录连接作为事务 Session 暴露。"""
        yield cast(Session, self.connection)


class FakeContainer:
    """暴露派生入口所需数据库，并记录资源关闭。"""

    def __init__(self) -> None:
        """初始化无网络数据库占位和未关闭状态。"""
        self.database = cast(DatabaseClient, object())
        self.closed = False

    def close(self) -> None:
        """记录入口或任务 `finally` 已释放组合根。"""
        self.closed = True


def test_run_financial_derivation_marks_success_and_passes_stable_run_to_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """应用组合根应以同一开始时刻计算，并在发布后落成功终态。"""
    run_id = UUID("70000000-0000-4000-8000-000000000001")
    expected = _result()
    database = cast(DatabaseClient, object())
    finished: list[tuple[UUID, str]] = []

    def start_run(
        received_database: DatabaseClient,
        *,
        mode: str,
        request_key: str,
        started_at: datetime,
    ) -> UUID:
        """验证调用参数并返回稳定运行标识。"""
        assert received_database is database
        assert mode == "backfill"
        assert request_key == "cli:test"
        assert started_at.tzinfo is UTC
        return run_id

    def finish_run(received_database: DatabaseClient, *, run_id: UUID, status: str) -> None:
        """记录运行账本终态。"""
        assert received_database is database
        finished.append((run_id, status))

    class FakeService:
        """记录派生用例收到的证券、运行和计算时刻。"""

        def __init__(self, *, repository: object) -> None:
            """确认组合根仍构造了 SQLAlchemy 派生仓储。"""
            assert isinstance(repository, bootstrap.SqlAlchemyFinancialDerivationRepository)

        def derive(
            self,
            *,
            exchange: Exchange,
            symbol: str,
            derivation_run_id: UUID,
            computed_at: datetime,
        ) -> FinancialDerivationResult:
            """验证派生输入并返回确定性成功摘要。"""
            assert exchange is Exchange.SSE
            assert symbol == "600519"
            assert derivation_run_id == run_id
            assert computed_at.tzinfo is UTC
            return expected

    monkeypatch.setattr(bootstrap, "_start_run", start_run)
    monkeypatch.setattr(bootstrap, "_finish_run", finish_run)
    monkeypatch.setattr(bootstrap, "FinancialDerivedMetricService", FakeService)

    actual = bootstrap.run_financial_derivation(
        database=database,
        exchange=Exchange.SSE,
        symbol="600519",
        mode="backfill",
        request_key="cli:test",
    )

    assert actual is expected
    # 成功终态已由派生仓储与 publication 同事务写入，组合根只补偿失败路径。
    assert finished == []


def test_run_financial_derivation_marks_failure_before_reraising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """派生失败必须先收敛运行账本，避免遗留永久 `running` 状态。"""
    run_id = UUID("70000000-0000-4000-8000-000000000001")
    database = cast(DatabaseClient, object())
    finished: list[str] = []

    def start_run(
        _database: DatabaseClient,
        *,
        mode: str,
        request_key: str,
        started_at: datetime,
    ) -> UUID:
        """返回固定运行标识并验证必要参数非空。"""
        assert mode == "scheduled"
        assert request_key == "celery:task-1"
        assert started_at.tzinfo is UTC
        return run_id

    def finish_run(_database: DatabaseClient, *, run_id: UUID, status: str) -> None:
        """记录失败终态及其运行标识。"""
        assert run_id == UUID("70000000-0000-4000-8000-000000000001")
        finished.append(status)

    class FailingService:
        """在应用服务边界模拟不可用的来源 publication。"""

        def __init__(self, *, repository: object) -> None:
            """接受生产仓储，保持失败发生在派生阶段。"""
            assert isinstance(repository, bootstrap.SqlAlchemyFinancialDerivationRepository)

        def derive(self, **_kwargs: object) -> FinancialDerivationResult:
            """抛出固定异常，验证组合根的失败收敛顺序。"""
            raise RuntimeError("derived inputs unavailable")

    monkeypatch.setattr(bootstrap, "_start_run", start_run)
    monkeypatch.setattr(bootstrap, "_finish_run", finish_run)
    monkeypatch.setattr(bootstrap, "FinancialDerivedMetricService", FailingService)

    with pytest.raises(RuntimeError, match="inputs unavailable"):
        bootstrap.run_financial_derivation(
            database=database,
            exchange=Exchange.SSE,
            symbol="600519",
            mode="scheduled",
            request_key="celery:task-1",
        )

    assert finished == ["failed"]


def test_run_financial_derivation_reuses_control_plane_run_and_arms_publication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """控制面运行标识和终态武装回调必须原样传到派生 publication 边界。"""
    run_id = UUID("70000000-0000-4000-8000-000000000009")
    database = cast(DatabaseClient, object())
    timeline: list[str] = []

    def start_run(
        received_database: DatabaseClient,
        *,
        mode: str,
        request_key: str,
        started_at: datetime,
        requested_run_id: UUID | None = None,
    ) -> UUID:
        """确认桥接账本使用控制面 run_id，避免产生第二套运行身份。"""
        assert received_database is database
        assert mode == "manual"
        assert request_key == "data-operation:run-9:SSE:600519"
        assert started_at.tzinfo is UTC
        assert requested_run_id == run_id
        return run_id

    def arm_publication() -> None:
        """记录 dispatcher 在最终 publication 事务前武装终态。"""
        timeline.append("arm")

    class FakeService:
        """验证控制面 fencing 参数到达派生应用服务。"""

        def __init__(self, *, repository: object) -> None:
            """确认组合根仍使用生产派生仓储。"""
            assert isinstance(repository, bootstrap.SqlAlchemyFinancialDerivationRepository)

        def derive(
            self,
            *,
            exchange: Exchange,
            symbol: str,
            derivation_run_id: UUID,
            computed_at: datetime,
            before_final_publication: object,
        ) -> FinancialDerivationResult:
            """在最终 publication 边界调用控制面武装回调。"""
            assert exchange is Exchange.SSE
            assert symbol == "600519"
            assert derivation_run_id == run_id
            assert computed_at.tzinfo is UTC
            assert before_final_publication is arm_publication
            arm_publication()
            timeline.append("publish")
            return _result()

    monkeypatch.setattr(bootstrap, "_start_run", start_run)
    monkeypatch.setattr(bootstrap, "FinancialDerivedMetricService", FakeService)

    result = bootstrap.run_financial_derivation(
        database=database,
        exchange=Exchange.SSE,
        symbol="600519",
        mode="manual",
        request_key="data-operation:run-9:SSE:600519",
        run_id=run_id,
        before_final_publication=arm_publication,
    )

    assert result.publication.row_count == 4
    assert timeline == ["arm", "publish"]


def test_start_run_inserts_new_ledger_with_capability_scoped_request_key() -> None:
    """首次请求应创建 `running` 账本并把外部幂等键限定到派生能力。"""
    started_at = datetime(2026, 7, 28, 9, tzinfo=UTC)
    connection = LedgerConnection((ScalarResult(None), ScalarResult()))

    run_id = bootstrap._start_run(
        cast(DatabaseClient, LedgerDatabase(connection)),
        mode="manual",
        request_key="cli:request-1",
        started_at=started_at,
    )

    assert isinstance(run_id, UUID)
    assert len(connection.executed) == 2
    parameters = connection.executed[1].compile().params
    assert parameters["capability"] == "financial.derived-metric"
    assert parameters["mode"] == "manual"
    assert parameters["request_key"] == "financial.derived-metric:cli:request-1"
    assert parameters["status"] == "running"


def test_start_run_uses_requested_control_plane_run_id_and_rejects_collision() -> None:
    """控制面运行标识必须成为新账本主键，既有幂等键冲突必须显式失败。"""
    requested_run_id = UUID("70000000-0000-4000-8000-000000000009")
    started_at = datetime(2026, 7, 30, 9, tzinfo=UTC)
    insert_connection = LedgerConnection((ScalarResult(None), ScalarResult()))

    actual = bootstrap._start_run(
        cast(DatabaseClient, LedgerDatabase(insert_connection)),
        mode="manual",
        request_key="data-operation:run-9:SSE:600519",
        started_at=started_at,
        requested_run_id=requested_run_id,
    )

    assert actual == requested_run_id
    parameters = insert_connection.executed[1].compile().params
    assert parameters["run_id"] == requested_run_id

    collision_connection = LedgerConnection(
        (ScalarResult(UUID("70000000-0000-4000-8000-000000000010")),)
    )
    with pytest.raises(RuntimeError, match="belongs to another run"):
        bootstrap._start_run(
            cast(DatabaseClient, LedgerDatabase(collision_connection)),
            mode="manual",
            request_key="data-operation:run-9:SSE:600519",
            started_at=started_at,
            requested_run_id=requested_run_id,
        )

    assert len(collision_connection.executed) == 1


def test_start_run_reuses_idempotent_ledger_and_resets_terminal_fields() -> None:
    """同一请求重试应复用 run_id，并重新进入 `running` 而非创建重复账本。"""
    run_id = UUID("70000000-0000-4000-8000-000000000001")
    started_at = datetime(2026, 7, 28, 9, tzinfo=UTC)
    connection = LedgerConnection((ScalarResult(run_id), ScalarResult()))

    actual = bootstrap._start_run(
        cast(DatabaseClient, LedgerDatabase(connection)),
        mode="scheduled",
        request_key="celery:task-1",
        started_at=started_at,
    )

    assert actual == run_id
    assert len(connection.executed) == 2
    parameters = connection.executed[1].compile().params
    assert parameters["mode"] == "scheduled"
    assert parameters["status"] == "running"
    assert parameters["finished_at"] is None


def test_start_run_rejects_unknown_execution_mode() -> None:
    """运行模式必须属于手工、计划或回补集合，禁止把未知值写入账本。"""
    database = cast(DatabaseClient, LedgerDatabase(LedgerConnection(())))

    with pytest.raises(ValueError, match="unsupported financial derivation mode"):
        bootstrap._start_run(
            database,
            mode="adhoc",
            request_key="invalid",
            started_at=datetime(2026, 7, 28, 9, tzinfo=UTC),
        )


@pytest.mark.parametrize("status", ["succeeded", "failed"])
def test_finish_run_accepts_only_matching_running_ledger(status: str) -> None:
    """成功和失败终态都只能更新同能力、仍在运行的精确 run_id。"""
    run_id = UUID("70000000-0000-4000-8000-000000000001")
    connection = LedgerConnection((ScalarResult(run_id),))

    bootstrap._finish_run(
        cast(DatabaseClient, LedgerDatabase(connection)),
        run_id=run_id,
        status=status,
    )

    parameters = connection.executed[0].compile().params
    assert parameters["status"] == status
    assert cast(datetime, parameters["finished_at"]).tzinfo is UTC


def test_finish_run_rejects_unknown_status_and_ledger_mismatch() -> None:
    """未知终态或未命中运行账本都表示编排错误，不得静默吞掉。"""
    database = cast(DatabaseClient, LedgerDatabase(LedgerConnection(())))
    run_id = UUID("70000000-0000-4000-8000-000000000001")

    with pytest.raises(ValueError, match="terminal status"):
        bootstrap._finish_run(database, run_id=run_id, status="cancelled")

    mismatch_connection = LedgerConnection((ScalarResult(None),))
    with pytest.raises(RuntimeError, match="ledger is inconsistent"):
        bootstrap._finish_run(
            cast(DatabaseClient, LedgerDatabase(mismatch_connection)),
            run_id=run_id,
            status="failed",
        )


@pytest.mark.skip(reason="旧派生 CLI 已安全停用，行为由 data-operations 控制面替代")
def test_cli_runs_single_security_outputs_compact_json_and_closes_container(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """CLI 应解析回补模式、输出 publication 摘要并始终关闭组合根。"""
    container = FakeContainer()
    calls: list[tuple[Exchange, str, str, str]] = []

    def load_enabled_settings() -> SimpleNamespace:
        """返回仅开启财务能力的最小设置。"""
        return SimpleNamespace(financial_enabled=True)

    def configure(_settings: object, *, process_role: str) -> None:
        """验证 CLI 使用独立日志角色。"""
        assert process_role == "financial-derived-cli"

    def build(_settings: object) -> FakeContainer:
        """返回无网络组合根替身。"""
        return container

    def run(
        *,
        database: DatabaseClient,
        exchange: Exchange,
        symbol: str,
        mode: str,
        request_key: str,
    ) -> FinancialDerivationResult:
        """记录 CLI 到应用组合根的边界参数并返回确定性摘要。"""
        assert database is container.database
        calls.append((exchange, symbol, mode, request_key))
        return _result()

    monkeypatch.setattr(entrypoint, "load_settings", load_enabled_settings)
    monkeypatch.setattr(entrypoint, "configure_logging", configure)
    monkeypatch.setattr(entrypoint, "build_container", build)
    monkeypatch.setattr(entrypoint, "run_financial_derivation", run)

    exit_code = entrypoint.main(["--exchange", "SSE", "--symbol", "600519", "--mode", "backfill"])

    rendered = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert rendered == {
        "exchange": "SSE",
        "symbol": "600519",
        "dataVersion": "80000000-0000-4000-8000-000000000001",
        "computed": 4,
        "skipped": 2,
        "rowCount": 4,
    }
    assert calls[0][:3] == (Exchange.SSE, "600519", "backfill")
    assert calls[0][3].startswith("cli:")
    assert container.closed is True


@pytest.mark.skip(reason="旧派生 CLI 已安全停用，行为由 data-operations 控制面替代")
def test_cli_rejects_disabled_financial_capability_before_building_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """关闭开关时 CLI 必须在建立数据库、Redis 或对象存储连接前退出。"""
    build_calls = 0

    def load_disabled_settings() -> SimpleNamespace:
        """返回关闭财务能力的最小设置。"""
        return SimpleNamespace(financial_enabled=False)

    def forbidden_build(_settings: object) -> FakeContainer:
        """记录不应发生的组合根构建。"""
        nonlocal build_calls
        build_calls += 1
        return FakeContainer()

    monkeypatch.setattr(entrypoint, "load_settings", load_disabled_settings)
    monkeypatch.setattr(entrypoint, "build_container", forbidden_build)

    with pytest.raises(SystemExit, match="FINANCIAL_ENABLED"):
        entrypoint.main(["--exchange", "SZSE", "--symbol", "000001"])

    assert build_calls == 0


@pytest.mark.skip(reason="旧派生 CLI 已安全停用，行为由 data-operations 控制面替代")
def test_cli_closes_container_when_derivation_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """应用异常必须透传给调用方，同时 `finally` 仍释放全部组合依赖。"""
    container = FakeContainer()

    def load_enabled_settings() -> SimpleNamespace:
        """返回开启财务能力的最小设置。"""
        return SimpleNamespace(financial_enabled=True)

    def configure(_settings: object, *, process_role: str) -> None:
        """验证失败路径仍配置正确日志角色。"""
        assert process_role == "financial-derived-cli"

    def build(_settings: object) -> FakeContainer:
        """返回将被失败路径关闭的组合根。"""
        return container

    def fail_run(**_kwargs: object) -> FinancialDerivationResult:
        """模拟派生输入已被另一 publication 替换。"""
        raise RuntimeError("publication changed")

    monkeypatch.setattr(entrypoint, "load_settings", load_enabled_settings)
    monkeypatch.setattr(entrypoint, "configure_logging", configure)
    monkeypatch.setattr(entrypoint, "build_container", build)
    monkeypatch.setattr(entrypoint, "run_financial_derivation", fail_run)

    with pytest.raises(RuntimeError, match="publication changed"):
        entrypoint.main(["--exchange", "SSE", "--symbol", "600519"])

    assert container.closed is True


@pytest.mark.skip(reason="旧派生 Celery 任务已安全停用，行为由 data-operations 控制面替代")
def test_celery_task_is_idempotently_registered_and_uses_request_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """显式任务应只注册一次，并把 Celery 请求标识映射为可恢复运行键。"""
    app = Celery("financial-derived-runtime-test")
    settings = cast(Settings, SimpleNamespace(financial_enabled=True))
    container = FakeContainer()
    calls: list[tuple[Exchange, str, str, str]] = []

    def build(_settings: Settings) -> FakeContainer:
        """返回无网络 worker 组合根。"""
        return container

    def run(
        *,
        database: DatabaseClient,
        exchange: Exchange,
        symbol: str,
        mode: str,
        request_key: str,
    ) -> FinancialDerivationResult:
        """记录任务传入的计划模式和 Celery 幂等键。"""
        assert database is container.database
        calls.append((exchange, symbol, mode, request_key))
        return _result()

    monkeypatch.setattr(financial_derived_tasks, "build_container", build)
    monkeypatch.setattr(financial_derived_tasks, "run_financial_derivation", run)
    financial_derived_tasks.register_financial_derived_tasks(app, settings=settings)
    task = app.tasks[financial_derived_tasks._TASK]
    financial_derived_tasks.register_financial_derived_tasks(app, settings=settings)

    task.push_request(id="celery-request-1")
    try:
        result = task.run("SSE", "600519")
    finally:
        task.pop_request()

    assert app.tasks[financial_derived_tasks._TASK] is task
    assert result == {
        "dataVersion": "80000000-0000-4000-8000-000000000001",
        "computed": 4,
        "skipped": 2,
        "rowCount": 4,
    }
    assert calls == [(Exchange.SSE, "600519", "scheduled", "celery:celery-request-1")]
    assert container.closed is True


@pytest.mark.skip(reason="旧派生 Celery 任务已安全停用，行为由 data-operations 控制面替代")
def test_celery_task_uses_fallback_identity_and_closes_container_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """缺少请求 ID 时任务应生成键；派生异常仍必须关闭 worker 组合根。"""
    app = Celery("financial-derived-fallback-test")
    settings = cast(Settings, SimpleNamespace(financial_enabled=True))
    container = FakeContainer()
    request_keys: list[str] = []

    def build(_settings: Settings) -> FakeContainer:
        """返回失败路径使用的无网络组合根。"""
        return container

    def fail_run(*, request_key: str, **_kwargs: object) -> FinancialDerivationResult:
        """记录自动生成的请求键后模拟派生失败。"""
        request_keys.append(request_key)
        raise RuntimeError("derived inputs unavailable")

    monkeypatch.setattr(financial_derived_tasks, "build_container", build)
    monkeypatch.setattr(financial_derived_tasks, "run_financial_derivation", fail_run)
    financial_derived_tasks.register_financial_derived_tasks(app, settings=settings)

    with pytest.raises(RuntimeError, match="inputs unavailable"):
        app.tasks[financial_derived_tasks._TASK].run("SZSE", "000001")

    assert request_keys[0].startswith("celery:")
    assert len(request_keys[0]) > len("celery:")
    assert container.closed is True


@pytest.mark.skip(reason="旧派生 Celery 任务已安全停用，行为由 data-operations 控制面替代")
def test_celery_task_rejects_disabled_financial_capability_before_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """关闭财务开关时任务不得创建任何外部依赖。"""
    app = Celery("financial-derived-disabled-test")
    settings = cast(Settings, SimpleNamespace(financial_enabled=False))
    build_calls = 0

    def forbidden_build(_settings: Settings) -> FakeContainer:
        """记录不应发生的 worker 组合根构建。"""
        nonlocal build_calls
        build_calls += 1
        return FakeContainer()

    monkeypatch.setattr(financial_derived_tasks, "build_container", forbidden_build)
    financial_derived_tasks.register_financial_derived_tasks(app, settings=settings)

    with pytest.raises(RuntimeError, match="financial sync is disabled"):
        app.tasks[financial_derived_tasks._TASK].run("SSE", "600519")

    assert build_calls == 0


def test_legacy_derived_cli_and_task_reject_before_using_runtime() -> None:
    """旧派生入口必须以稳定错误停用，不能调用运行账本或直接发布指标。"""
    with pytest.raises(SystemExit) as cli_error:
        entrypoint.main(["--exchange", "SSE", "--symbol", "600519"])
    assert str(cli_error.value) == (f"{LEGACY_ENTRYPOINT_UNAVAILABLE}: data-sync-financial-derived")

    app = Celery("legacy-financial-derived-runtime")
    financial_derived_tasks.register_financial_derived_tasks(app, settings=cast(Settings, object()))
    with pytest.raises(LegacyEntryPointUnavailable) as task_error:
        app.tasks[financial_derived_tasks._TASK].run("SSE", "600519")
    assert task_error.value.code == LEGACY_ENTRYPOINT_UNAVAILABLE


def _result() -> FinancialDerivationResult:
    """构造派生入口和任务共同使用的确定性 publication 摘要。"""
    return FinancialDerivationResult(
        publication=FinancialDerivedPublication(
            data_version=UUID("80000000-0000-4000-8000-000000000001"),
            inserted_count=4,
            unchanged_count=0,
            row_count=4,
        ),
        computed_count=4,
        skipped_count=2,
    )
