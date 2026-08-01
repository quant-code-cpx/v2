"""来源声明审计与技术执行门分离回归测试。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

from service_data_sync.application.ports.data_source import ProviderBatch, SourceRequest
from service_data_sync.application.source_registry import SourceRegistry
from service_data_sync.bootstrap.container import ServiceContainer
from service_data_sync.infrastructure.data_operations import canonical_executors
from service_data_sync.infrastructure.data_operations.control_plane import (
    DataOperationsControlPlane,
    DatasetDefinition,
)
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.database.models.operations import (
    DataOperationCommand,
    DataOperationPreflight,
    DataOperationRun,
)


class TechnicalProvider:
    """声明可执行中立能力的最小 provider，不携带来源准入判断。"""

    @property
    def provider_id(self) -> str:
        """返回控制面目录冻结的唯一 provider 标识。"""
        return "test-equity-provider"

    def capabilities(self) -> frozenset[str]:
        """声明日线能力，供 preflight 和 executor 进行技术匹配。"""
        return frozenset({"equity.bar.1d.raw"})

    async def fetch(self, request: SourceRequest) -> ProviderBatch:
        """本测试不抓取网络；若触发说明控制面越过了预检范围。"""
        del request
        raise AssertionError("technical admission test must not fetch a provider")


class _Rows:
    """提供 command 收据读取所需的最小 SQLAlchemy 标量集合。"""

    def __init__(self, values: list[DataOperationRun]) -> None:
        """保存按提交顺序返回的 child run。"""
        self._values = values

    def all(self) -> list[DataOperationRun]:
        """返回已记录 child run，不执行数据库查询。"""
        return self._values


class _CommandSession:
    """以内存替身覆盖 command 受理所需的最小事务读写语义。"""

    def __init__(self, preflight: DataOperationPreflight) -> None:
        """保存唯一预检记录和本次事务新增对象。"""
        self._preflight = preflight
        self.added: list[object] = []

    def scalar(self, statement: object) -> None:
        """返回空幂等和零队列深度，隔离来源准入回归。"""
        del statement
        return None

    def scalars(self, statement: object) -> _Rows:
        """返回 command 已新增的 child run，供收据验证其冻结快照。"""
        del statement
        return _Rows([value for value in self.added if isinstance(value, DataOperationRun)])

    def get(self, model: type[Any], identity: object, **_kwargs: object) -> object | None:
        """按模型和 UUID 返回预检或新建 command，拒绝未覆盖的读取路径。"""
        if model is DataOperationPreflight:
            return self._preflight if identity == self._preflight.preflight_id else None
        if model is DataOperationCommand:
            return next(
                (
                    value
                    for value in self.added
                    if isinstance(value, DataOperationCommand) and value.command_id == identity
                ),
                None,
            )
        return None

    def add(self, value: object) -> None:
        """记录控制面即将持久化的 command、run 与审计对象。"""
        self.added.append(value)

    def flush(self) -> None:
        """内存对象已带 UUID，测试无需真实数据库 flush。"""
        return None


class _CommandDatabase:
    """只暴露 command 受理事务的数据库替身。"""

    def __init__(self, session: _CommandSession) -> None:
        """保存单一事务会话，确保读取与写入位于同一视图。"""
        self._session = session

    @contextmanager
    def transaction(self) -> Iterator[_CommandSession]:
        """返回不提交外部状态的受控事务上下文。"""
        yield self._session


def _now() -> datetime:
    """返回固定 UTC 时钟，使预检有效期和 command 收据可重复断言。"""
    return datetime(2026, 8, 1, 12, tzinfo=UTC)


def _definition() -> DatasetDefinition:
    """构造带未核验来源声明、但技术能力完整的个股日线目录项。"""
    return DatasetDefinition(
        dataset_code="equity.bar.1d.raw",
        display_name="测试个股日线",
        domain="equity",
        description="验证来源声明不替代技术执行门",
        grain="证券 × 交易日",
        capability="equity.bar.1d.raw",
        modes=("DATE_RANGE",),
        schedule_modes=(),
        selector_kinds=("INSTRUMENT",),
        dispatcher_ready=True,
        config_enabled=True,
        lifecycle="RESEARCH",
        provider_id="test-equity-provider",
        upstream_source="test.equity-kline",
        approval_status="CANDIDATE",
        rights_status="unverified",
        license_scope="unverified",
        data_as_of_kind="TRADING_DATE",
        data_as_of_label="行情交易日",
    )


def _target() -> dict[str, Any]:
    """构造单证券、单交易日的最小可执行控制面目标。"""
    return {
        "datasetCode": "equity.bar.1d.raw",
        "mode": "DATE_RANGE",
        "selector": {"kind": "INSTRUMENT", "exchange": "SSE", "symbol": "600000"},
        "dateFrom": "2026-07-31",
        "dateTo": "2026-07-31",
        "observationDate": None,
    }


def _control_plane(
    *, database: object, registry: SourceRegistry, definition: DatasetDefinition
) -> DataOperationsControlPlane:
    """组装只含一个数据集和一个中立 provider 的控制面。"""
    return DataOperationsControlPlane(
        database=cast(DatabaseClient, database),
        catalog={definition.dataset_code: definition},
        source_registry=registry,
        now=_now,
    )


def test_unverified_source_metadata_remains_audit_only_through_command_and_executor() -> None:
    """未核验声明必须保留在 run 快照，但不能拒绝 preflight、command 或 executor。"""
    definition = _definition()
    provider = TechnicalProvider()
    registry = SourceRegistry()
    registry.register(provider)
    preflight_control_plane = _control_plane(
        database=object(), registry=registry, definition=definition
    )
    target = _target()

    preflight_result = preflight_control_plane._preflight_target(target)

    assert preflight_result["eligible"] is True
    assert preflight_result["estimatedPartitions"] == 1
    preflight = DataOperationPreflight(
        preflight_id=UUID("10000000-0000-4000-8000-000000000001"),
        request_hash="a" * 64,
        targets_json=[target],
        result_json=[preflight_result],
        created_at=_now(),
        expires_at=_now() + timedelta(minutes=5),
    )
    session = _CommandSession(preflight)
    command_control_plane = _control_plane(
        database=_CommandDatabase(session), registry=registry, definition=definition
    )

    receipt = command_control_plane._submit_validated_command(
        targets=[target],
        submission_id=uuid4(),
        preflight_id=preflight.preflight_id,
        request_hash=preflight.request_hash,
        actor={"actorRef": "operator:test", "role": "SYSTEM", "reason": "技术验收"},
        idempotency_key="source-admission-regression-key",
        request_id="source-admission-regression",
        operation_hash="b" * 64,
        execution_intents=(None,),
    )

    run = next(value for value in session.added if isinstance(value, DataOperationRun))
    snapshot = run.source_snapshot
    assert receipt["status"] == "QUEUED"
    assert snapshot[0]["approvalStatus"] == "CANDIDATE"
    assert snapshot[0]["rightsStatus"] == "unverified"
    assert snapshot[0]["licenseScope"] == "unverified"
    # dispatcher 交给 executor 的就是此 immutable run 快照；只校验技术 provider/capability 绑定。
    selected = canonical_executors._frozen_provider(
        snapshot,
        cast(ServiceContainer, SimpleNamespace(source_registry=registry)),
        "equity.bar.1d.raw",
    )
    assert selected is provider


def test_missing_technical_provider_remains_preflight_blocker() -> None:
    """来源声明不阻断执行，但 provider 缺失仍必须使 preflight 不可提交。"""
    definition = _definition()
    control_plane = _control_plane(
        database=object(), registry=SourceRegistry(), definition=definition
    )

    result = control_plane._preflight_target(_target())

    assert result["eligible"] is False
    assert result["estimatedPartitions"] == 0
