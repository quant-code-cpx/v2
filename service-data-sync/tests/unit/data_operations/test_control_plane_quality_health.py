"""数据运维质量门、发布绑定与系统操作者回归测试。"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from service_data_sync.infrastructure.data_operations.control_plane import (
    DataOperationsControlPlane,
    DatasetDefinition,
    PublicationBinding,
    QualityGateBlocked,
)
from service_data_sync.infrastructure.database.models.canonical import DatasetRelease
from service_data_sync.infrastructure.database.models.operations import (
    DataOperationPartition,
    DataOperationRun,
)


class EmptyScalarResult:
    """提供 SQLAlchemy scalar 集合的最小空实现，避免纯规则测试连接 PostgreSQL。"""

    def all(self) -> list[Any]:
        """返回没有领域质量规则的空列表。"""
        return []


class RecordingHealthSession:
    """记录健康事实和问题投影写入，模拟健康规则所需的只读查询结果。"""

    def __init__(self) -> None:
        """初始化无既有质量结果、无开放问题的受控测试会话。"""
        self.added: list[Any] = []

    def scalar(self, _statement: object) -> None:
        """所有领域质量结果和当前问题查询均返回空，触发通用 immutable 规则。"""
        return None

    def scalars(self, _statement: object) -> EmptyScalarResult:
        """返回空质量结果集合，避免测试依赖数据库行。"""
        return EmptyScalarResult()

    def add(self, value: Any) -> None:
        """记录控制面拟提交的 immutable evaluation 或 current issue。"""
        self.added.append(value)


class PublicationViewSession:
    """为最新发布投影提供固定 immutable release 的最小会话替身。"""

    def __init__(self, release: Any) -> None:
        """保存可验证的 release，查询不得返回随机替代 UUID。"""
        self._release = release

    def get(self, model: object, _identity: UUID) -> Any:
        """只响应 DatasetRelease 查询，其他模型代表错误的读取路径。"""
        assert model is DatasetRelease
        return self._release


class BindingSession:
    """提供健康检查版本绑定所需的 publication、release 与 canonical 查询结果。"""

    def __init__(self, publication: Any, release: Any, canonical_dataset: Any) -> None:
        """保存已冻结的三段式绑定，测试不能以当前时间或随机 UUID 代替它。"""
        self._publication = publication
        self._release = release
        self._canonical_dataset = canonical_dataset

    def scalar(self, _statement: object) -> Any:
        """返回当前 production publication，模拟 null health target 的受理期绑定。"""
        return self._publication

    def get(self, model: object, _identity: UUID) -> Any:
        """按模型返回真实 release 或 canonical 数据集。"""
        if model is DatasetRelease:
            return self._release
        return self._canonical_dataset


def test_legacy_submission_uses_namespaced_system_actor() -> None:
    """Python 兼容入口创建的 command 必须标为 system:legacy，不能退回 plain system。"""
    control_plane: Any = object.__new__(DataOperationsControlPlane)
    target = {
        "datasetCode": "equity.bar.1d.raw",
        "mode": "FULL",
        "selector": {"kind": "GLOBAL"},
    }
    captured: dict[str, Any] = {}

    def submit(**kwargs: Any) -> dict[str, Any]:
        """捕获真正提交路径接收的 actor，而不建立数据库连接。"""
        captured.update(kwargs)
        return {"commandId": "accepted"}

    control_plane._validate_targets = lambda values: values
    control_plane._validate_legacy_execution_intent = lambda value: value
    control_plane.preflight = lambda _targets: {
        "preflightId": "10000000-0000-4000-8000-000000000001",
        "requestHash": "a" * 64,
    }
    control_plane._submit_validated_command = submit

    control_plane.submit_system_legacy_command(
        targets=[target],
        intents=[{"kind": "STANDARD"}],
        reason="兼容入口迁移",
        idempotency_key="legacy-actor-regression-key",
        request_id="legacy-actor-regression",
        submission_id=UUID("20000000-0000-4000-8000-000000000001"),
    )

    assert captured["actor"] == {
        "actorRef": "system:legacy",
        "role": "SYSTEM",
        "reason": "兼容入口迁移",
    }
    assert control_plane._schedule_actor_ref(UUID("30000000-0000-4000-8000-000000000001")) == (
        "system:schedule/30000000-0000-4000-8000-000000000001"
    )


def test_quality_gate_blocks_canonical_finalizer_before_run_completion() -> None:
    """质量门 BLOCKED 必须通过异常中止同一 canonical transaction，不能写成功 run。"""
    control_plane: Any = object.__new__(DataOperationsControlPlane)
    run_id = UUID("40000000-0000-4000-8000-000000000001")
    gate = {
        "disposition": "BLOCKED",
        "policyCode": "data-operations-default",
        "policyVersion": 1,
        "affectedCount": 2,
        "error": {
            "code": "ingestion-quality-blocked",
            "stage": "QUALITY_GATE",
            "retryable": False,
            "message": "Ingestion quality gate blocked publication",
        },
    }

    class FinalizerSession:
        """只允许 finalizer 锁定目标 run，确认阻断发生在完成逻辑之前。"""

        def get(self, model: object, identity: UUID, *, with_for_update: bool) -> Any:
            """返回被锁定的 run；错误模型或 UUID 都表示 finalizer 越界。"""
            assert model is DataOperationRun
            assert identity == run_id
            assert with_for_update is True
            return SimpleNamespace(run_id=run_id)

    def gate_for_run(_session: object, _run: object, _execution: object) -> dict[str, Any]:
        """固定返回 BLOCKED，模拟同事务质量事实拒绝新 publication。"""
        return gate

    def should_not_complete(**_kwargs: Any) -> bool:
        """若质量门后仍尝试完成 run，测试立即失败。"""
        raise AssertionError("blocked quality gate must prevent run completion")

    control_plane._ingestion_quality_gate = gate_for_run
    control_plane._complete_run_in_session = should_not_complete
    execution = SimpleNamespace(
        run_id=run_id,
        fencing_token=7,
        checkpoint_kind=None,
        checkpoint_position=None,
    )

    with pytest.raises(QualityGateBlocked) as raised:
        control_plane._finalize_canonical_publication(FinalizerSession(), execution)

    assert raised.value.gate is gate


def test_quality_gate_derives_blocked_from_partial_publication() -> None:
    """候选 publication 明确为 partial 时，控制面必须生成 BLOCKED 而非默认 PASSED。"""
    control_plane: Any = object.__new__(DataOperationsControlPlane)
    publication = SimpleNamespace(quality_status="partial", release_id=None)

    gate = control_plane._quality_gate_for_publication(object(), publication)

    assert gate["disposition"] == "BLOCKED"
    assert gate["error"]["stage"] == "QUALITY_GATE"


def test_health_evaluation_uses_real_publication_and_release_identifiers() -> None:
    """健康事实必须复用 publication 的 dataVersion 和 release UUID，不能生成随机绑定。"""
    control_plane: Any = object.__new__(DataOperationsControlPlane)
    now = datetime(2026, 7, 29, 8, tzinfo=UTC)
    data_version = UUID("50000000-0000-4000-8000-000000000001")
    release_id = UUID("60000000-0000-4000-8000-000000000001")
    control_plane._catalog = {
        "equity.bar.1d.raw": DatasetDefinition(
            dataset_code="equity.bar.1d.raw",
            display_name="测试日线",
            domain="equity",
            description="测试",
            grain="证券 × 交易日",
            capability="equity.bar.1d.raw",
            modes=("FULL",),
            schedule_modes=("FULL",),
        )
    }
    publication = SimpleNamespace(
        dataset="equity.bar.1d.raw",
        data_version=data_version,
        release_id=release_id,
        published_at=now - timedelta(minutes=30),
        quality_status="passed",
        effective_as_of=None,
    )
    release = SimpleNamespace(
        release_id=release_id,
        normalization_run_id=uuid4(),
        record_count=12,
        content_hash="a" * 64,
        fact_min=None,
        fact_max=None,
        dataset_id=uuid4(),
    )
    canonical_dataset = SimpleNamespace(
        code="equity.bar.1d.raw",
        domain="equity",
        status="production",
    )
    binding = PublicationBinding(
        publication=cast(Any, publication),
        release=cast(Any, release),
        canonical_dataset=cast(Any, canonical_dataset),
    )
    session = RecordingHealthSession()

    evaluation = control_plane._record_health_evaluation(
        session,
        binding=binding,
        health_check_id=UUID("70000000-0000-4000-8000-000000000001"),
        now=now,
    )

    assert evaluation.data_version == data_version
    assert evaluation.release_id == release_id
    assert evaluation.health_check_id == UUID("70000000-0000-4000-8000-000000000001")
    assert evaluation.status == "HEALTHY"
    assert {item["ruleCode"] for item in evaluation.results_json} == {
        "freshness",
        "publication-completeness",
        "record-uniqueness",
        "data-validity",
        "schema-compatible",
        "temporal-order",
        "identity-valid",
        "domain-invariants",
    }
    assert session.added[0] is evaluation


def test_health_target_binding_freezes_current_real_publication() -> None:
    """主动检查 null dataVersion 必须绑定当前真实 publication/release，而不是临时 uuid4。"""
    control_plane: Any = object.__new__(DataOperationsControlPlane)
    data_version = UUID("71000000-0000-4000-8000-000000000001")
    release_id = UUID("72000000-0000-4000-8000-000000000001")
    publication = SimpleNamespace(data_version=data_version, release_id=release_id)
    release = SimpleNamespace(release_id=release_id, dataset_id=uuid4())
    canonical_dataset = SimpleNamespace(code="equity.bar.1d.raw", domain="equity")

    binding = control_plane._health_publication_binding(
        BindingSession(publication, release, canonical_dataset),
        "equity.bar.1d.raw",
        None,
    )

    assert binding is not None
    assert binding.publication.data_version == data_version
    assert binding.release.release_id == release_id


def test_publication_view_and_error_stage_never_use_placeholder_values() -> None:
    """目录最新发布只返回可验证 release，且来源失败阶段必须使用合同 PROVIDER_FETCH。"""
    control_plane: Any = object.__new__(DataOperationsControlPlane)
    data_version = UUID("80000000-0000-4000-8000-000000000001")
    release_id = UUID("90000000-0000-4000-8000-000000000001")
    published_at = datetime(2026, 7, 29, 8, tzinfo=UTC)
    publication = SimpleNamespace(
        data_version=data_version,
        release_id=release_id,
        published_at=published_at,
        effective_as_of=None,
    )
    release = SimpleNamespace(release_id=release_id, record_count=23)

    assert control_plane._publication_view(PublicationViewSession(release), publication) == {
        "dataVersion": str(data_version),
        "releaseId": str(release_id),
        "publishedAt": "2026-07-29T08:00:00Z",
        "rowCount": 23,
    }
    assert control_plane._publication_view(PublicationViewSession(release), None) is None
    source = inspect.getsource(DataOperationsControlPlane)
    assert '"PROVIDER_FETCH"' in source
    assert '"SOURCE"' not in source
    assert '"system"' not in source


def test_overview_health_keeps_unknown_when_any_dataset_has_no_evaluation() -> None:
    """没有评估事实的目录项不能因暂无 issue 被总览误标为 HEALTHY。"""
    control_plane: Any = object.__new__(DataOperationsControlPlane)
    control_plane._now = lambda: datetime(2026, 7, 29, 8, tzinfo=UTC)

    result = control_plane._aggregate_health(
        [
            {
                "healthSummary": {
                    "status": "HEALTHY",
                    "warningCount": 0,
                    "criticalCount": 0,
                    "openIssueCount": 0,
                }
            },
            {
                "healthSummary": {
                    "status": "UNKNOWN",
                    "warningCount": 0,
                    "criticalCount": 0,
                    "openIssueCount": 0,
                }
            },
        ]
    )

    assert result["status"] == "UNKNOWN"


def test_retry_partition_inheritance_is_restricted_to_safe_share_capital_checkpoints() -> None:
    """只允许股本成功 canonical security 分区继承，普通失败或默认 checkpoint 均被 SQL 排除。"""
    control_plane: Any = object.__new__(DataOperationsControlPlane)
    inherited = SimpleNamespace(
        partition_key="security:10000000-0000-4000-8000-000000000001",
        status="SUCCEEDED",
        checkpoint_kind="canonical-partition",
    )

    class PartitionScalarResult:
        """返回数据库已按安全谓词筛选的一条成功分区。"""

        def all(self) -> list[Any]:
            """返回唯一允许继承的 canonical 分区。"""
            return [inherited]

    class PartitionSession:
        """记录构造出的 SQL，验证 FAILED/default 不可能进入返回集合。"""

        def __init__(self) -> None:
            """初始化尚未收到查询的会话替身。"""
            self.statement = ""

        def scalars(self, statement: object) -> PartitionScalarResult:
            """保存 SQL 文本并返回受控成功分区。"""
            self.statement = str(statement)
            return PartitionScalarResult()

    session = PartitionSession()
    run = SimpleNamespace(
        run_id=uuid4(),
        dataset_code="equity.share_capital.reported",
        target_json={"selector": {"kind": "GLOBAL"}},
    )

    values = control_plane._retry_partitions(cast(Any, session), cast(Any, run))

    assert values == (inherited,)
    assert "data_operation_partition.status =" in session.statement
    assert "data_operation_partition.checkpoint_kind =" in session.statement
    assert "data_operation_partition.partition_key LIKE" in session.statement
    assert DataOperationPartition.__tablename__ in session.statement
    non_share = SimpleNamespace(
        run_id=uuid4(),
        dataset_code="equity.trading_status.1d",
        target_json={"selector": {"kind": "GLOBAL"}},
    )
    assert control_plane._retry_partitions(cast(Any, session), cast(Any, non_share)) == ()


def test_retry_command_copies_snapshot_target_and_intent_without_mutation() -> None:
    """retry 新 run 必须原样复用旧 target/source/intent，防止跨快照继承分区水位。"""
    source = inspect.getsource(DataOperationsControlPlane.retry_command)

    assert "target_json=old.target_json" in source
    assert "source_snapshot=old.source_snapshot" in source
    assert "execution_intent_json=old.execution_intent_json" in source
    assert "new_run_id = uuid4()" in source
    assert "run_id=new_run_id" in source


def test_share_capital_preflight_freezes_permanent_identity_roster_and_hash() -> None:
    """股本预检必须冻结 UUID、代码和事实日，执行意图不能只保存易复用代码。"""
    instrument_id = UUID("10000000-0000-4000-8000-000000000001")

    class RosterResult:
        """返回一条当前已确认证券身份。"""

        def mappings(self) -> list[dict[str, object]]:
            """模拟 SQLAlchemy mapping 迭代结果。"""
            return [
                {
                    "instrument_id": instrument_id,
                    "exchange": "SSE",
                    "symbol": "600001",
                }
            ]

    class RosterSession:
        """记录冻结名单查询，确保读取的是永久身份和双时态代码表。"""

        def __init__(self) -> None:
            """初始化尚未执行查询的 SQL 文本。"""
            self.statement = ""

        def execute(self, statement: object) -> RosterResult:
            """保存查询并返回固定当前身份。"""
            self.statement = str(statement)
            return RosterResult()

    control_plane: Any = object.__new__(DataOperationsControlPlane)
    session = RosterSession()
    target = {
        "datasetCode": "equity.share_capital.reported",
        "mode": "FULL",
        "selector": {"kind": "GLOBAL"},
    }

    roster = control_plane._freeze_share_capital_roster(
        cast(Any, session),
        target=target,
        identity_as_of=datetime(2026, 7, 30, tzinfo=UTC).date(),
    )
    result = {
        "equityInstrumentRoster": list(roster),
        "equityInstrumentRosterHash": control_plane._hash(roster),
    }
    intent = control_plane._execution_intent_from_preflight(
        target=target,
        result=result,
    )

    assert roster == (
        {
            "instrumentId": str(instrument_id),
            "exchange": "SSE",
            "symbol": "600001",
            "identityAsOf": "2026-07-30",
        },
    )
    assert intent == result
    assert "equity_instrument.instrument_id" in session.statement
    assert "equity_identifier_version.effective_from" in session.statement
    assert "equity_identifier_version.known_to IS NULL" in session.statement
