"""互联互通实时来源预检与命令拒绝门禁测试。"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

import pytest

from service_data_sync.application.ports.data_source import (
    ProviderBatch,
    ProviderPreflightComponent,
    ProviderPreflightReport,
    ProviderPreflightRequest,
    ProviderStatusCoverageBoundary,
    SourceRequest,
)
from service_data_sync.application.source_registry import SourceRegistry
from service_data_sync.infrastructure.data_operations.control_plane import (
    DataOperationsControlPlane,
    DatasetDefinition,
    OperationProblem,
)
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.database.models.operations import (
    DataOperationPreflight,
)
from service_data_sync.infrastructure.persistence.stock_connect_readiness_repository import (
    StockConnectReadinessProbeOutcome,
)
from service_data_sync.infrastructure.persistence.stock_connect_status_boundary_repository import (
    StockConnectStatusBoundarySnapshot,
    StockConnectStatusBoundaryViolation,
)


class RejectedStockConnectProvider:
    """声明完整能力但模拟 strict SFTP entitlement 未获授权。"""

    def __init__(self) -> None:
        """初始化可断言边界拒绝发生在任何来源命令之前的探针计数。"""
        self.preflight_calls = 0

    @property
    def provider_id(self) -> str:
        """返回目录冻结的官方来源标识。"""
        return "official-stock-connect"

    def capabilities(self) -> frozenset[str]:
        """声明完整包执行所需五项官方能力。"""
        return frozenset(
            {
                "market.stock_connect.market_stat.reported",
                "market.stock_connect.active_security.snapshot",
                "market.stock_connect.trading_calendar",
                "market.stock_connect.instrument_master.reported",
                "market.stock_connect.channel_status.eod",
            }
        )

    def status_coverage_boundary(self) -> ProviderStatusCoverageBoundary:
        """返回本地清单声明的稳定历史边界和摘要。"""
        return ProviderStatusCoverageBoundary(
            required_from=date(2020, 1, 1),
            manifest_sha256="a" * 64,
        )

    def preflight_probe(
        self,
        request: ProviderPreflightRequest,
    ) -> ProviderPreflightReport:
        """返回分组件授权失败，证明静态 capability 不能替代在线 preflight。"""
        assert request.dataset_code == "market.stock_connect.overview.bundle"
        self.preflight_calls += 1
        return ProviderPreflightReport(
            components=(
                ProviderPreflightComponent(
                    component="fixed-length-profile-manifest",
                    accepted=True,
                    reason="PROFILE_MANIFEST_VERIFIED",
                ),
                ProviderPreflightComponent(
                    component="sftp-authentication",
                    accepted=False,
                    reason="SFTP_AUTHENTICATION_FAILED",
                ),
            ),
        )

    async def fetch(self, request: SourceRequest) -> ProviderBatch:
        """预检不得抓取或写业务数据。"""
        del request
        raise AssertionError("stock-connect preflight must use the read-only probe")


class AkshareCapabilityImpersonator(RejectedStockConnectProvider):
    """模拟 AKShare 错误声明官方 capability，验证目录来源身份仍 fail-closed。"""

    @property
    def provider_id(self) -> str:
        """返回未获互联互通目录批准的 provider 标识。"""
        return "akshare"

    def preflight_probe(
        self,
        request: ProviderPreflightRequest,
    ) -> ProviderPreflightReport:
        """即使自报预检通过，也不得越过目录固定 provider 身份。"""
        del request
        return ProviderPreflightReport(
            components=(
                ProviderPreflightComponent(
                    component="impersonated-source",
                    accepted=True,
                    reason="SHOULD_NOT_BE_CALLED",
                ),
            ),
        )


class FakeSession:
    """只提供提交拒绝发生前需要的 preflight 与幂等读取。"""

    def __init__(self, preflight: DataOperationPreflight) -> None:
        """保存未通过来源门禁的预检记录和新增对象捕获。"""
        self._preflight = preflight
        self.added: list[object] = []

    def scalar(self, statement: object) -> None:
        """返回无幂等重放记录。"""
        del statement
        return None

    def get(self, model: object, key: object) -> DataOperationPreflight | None:
        """仅允许按 UUID 读取预置 preflight。"""
        del model
        return self._preflight if key == self._preflight.preflight_id else None

    def add(self, value: object) -> None:
        """捕获潜在写入；拒绝路径必须保持为空。"""
        self.added.append(value)


class FakeDatabase:
    """提供不实际提交的事务上下文。"""

    def __init__(self, session: FakeSession) -> None:
        """保存唯一测试会话。"""
        self._session = session

    @contextmanager
    def transaction(self) -> Iterator[FakeSession]:
        """返回同一会话并在退出时保持所有捕获。"""
        yield self._session


class AcceptingStatusBoundaryRepository:
    """用内存返回值替换 PostgreSQL 锁，隔离控制面分支单元测试。"""

    def claim(
        self,
        *,
        required_from: date,
        manifest_sha256: str,
        observed_at: datetime,
    ) -> StockConnectStatusBoundarySnapshot:
        """接受候选并返回与输入相同的首次锁定快照。"""
        assert manifest_sha256 == "a" * 64
        return StockConnectStatusBoundarySnapshot(
            scope_key="test",
            required_from=required_from,
            first_locked_at=observed_at,
            tightened_at=observed_at,
        )


class RejectingStatusBoundaryRepository:
    """模拟环境变量或清单试图把既有 requiredFrom 后移。"""

    def claim(
        self,
        *,
        required_from: date,
        manifest_sha256: str,
        observed_at: datetime,
    ) -> StockConnectStatusBoundarySnapshot:
        """在任何外部探针执行前返回稳定后移拒绝码。"""
        del required_from, manifest_sha256, observed_at
        raise StockConnectStatusBoundaryViolation("STATUS_BOUNDARY_MOVED_LATER")


class RecordingReadinessRepository:
    """以内存记录 readiness begin/finish，隔离控制面分支单元测试。"""

    def __init__(self) -> None:
        """初始化严格的开始与终结计数。"""
        self.begun = 0
        self.finished = 0
        self.outcomes: list[StockConnectReadinessProbeOutcome] = []

    def begin(
        self,
        *,
        snapshot_id: UUID,
        request_hash: str,
        selected_channels: Sequence[str],
        observed_at: datetime,
    ) -> None:
        """记录远端探针前已请求持久化 snapshot。"""
        del snapshot_id, selected_channels, observed_at
        assert request_hash
        self.begun += 1

    def finish(
        self,
        *,
        snapshot_id: UUID,
        outcome: StockConnectReadinessProbeOutcome,
        evidence: Mapping[str, object] | None,
        manifest_id: UUID | None,
        completed_at: datetime,
        request_hash: str,
    ) -> None:
        """记录拒绝或成功探针已形成明确 snapshot 终态。"""
        del snapshot_id, evidence, manifest_id, completed_at
        assert outcome and request_hash
        self.finished += 1
        self.outcomes.append(outcome)


def _now() -> datetime:
    """返回稳定的带时区测试时钟。"""
    return datetime(2026, 7, 29, 12, tzinfo=UTC)


def _target() -> dict[str, object]:
    """构造四通道完整包的人工增量同步目标。"""
    return {
        "datasetCode": "market.stock_connect.overview.bundle",
        "mode": "INCREMENTAL",
        "selector": {
            "kind": "STOCK_CONNECT",
            "operation": "MARKET",
            "channel": "ALL",
            "direction": None,
        },
        "dateFrom": None,
        "dateTo": None,
        "observationDate": None,
    }


def _definition() -> DatasetDefinition:
    """构造只依赖官方五项能力的互联互通完整包目录项。"""
    return DatasetDefinition(
        dataset_code="market.stock_connect.overview.bundle",
        display_name="沪深港通中心完整包",
        domain="stock_connect",
        description="测试官方交付预检门禁",
        grain="通道 × 方向 × 官方交易日",
        capability="market.stock_connect.market_stat.reported",
        modes=("FULL", "INCREMENTAL", "DATE_RANGE"),
        schedule_modes=(),
        source_capabilities=(
            "market.stock_connect.market_stat.reported",
            "market.stock_connect.active_security.snapshot",
            "market.stock_connect.trading_calendar",
            "market.stock_connect.instrument_master.reported",
            "market.stock_connect.channel_status.eod",
        ),
        selector_kinds=("STOCK_CONNECT",),
        dispatcher_ready=True,
        config_enabled=True,
        provider_id="official-stock-connect",
    )


def test_failed_live_entitlement_marks_preflight_ineligible(
    configured_environment: None,
) -> None:
    """授权链失败必须分组件返回并把估算分区归零。"""
    del configured_environment
    registry = SourceRegistry()
    registry.register(RejectedStockConnectProvider())
    definition = _definition()
    readiness = RecordingReadinessRepository()
    control_plane = DataOperationsControlPlane(
        database=cast(DatabaseClient, object()),
        catalog={definition.dataset_code: definition},
        source_registry=registry,
        now=_now,
        stock_connect_status_boundary_repository=AcceptingStatusBoundaryRepository(),
        stock_connect_readiness_repository=readiness,
    )

    result = control_plane._preflight_target(_target())

    assert result["eligible"] is False
    assert result["estimatedPartitions"] == 0
    assert result["sourceChecks"][-1] == {
        "component": "sftp-authentication",
        "accepted": False,
        "reason": "SFTP_AUTHENTICATION_FAILED",
    }
    assert result["warnings"] == ["互联互通官方来源实时预检未通过，命令不会进入队列"]
    assert (readiness.begun, readiness.finished) == (1, 1)
    assert readiness.outcomes == [
        StockConnectReadinessProbeOutcome(
            status="SOURCE_MISSING",
            reason_code="DELIVERY_OBJECT_MISSING",
            detail="Official stock-connect readiness preflight did not complete",
        )
    ]


def test_akshare_cannot_replace_the_approved_official_provider(
    configured_environment: None,
) -> None:
    """同名 capability 不构成授权，stock-connect 目录只接受 official provider。"""
    del configured_environment
    registry = SourceRegistry()
    registry.register(AkshareCapabilityImpersonator())
    definition = _definition()
    control_plane = DataOperationsControlPlane(
        database=cast(DatabaseClient, object()),
        catalog={definition.dataset_code: definition},
        source_registry=registry,
        now=_now,
        stock_connect_status_boundary_repository=AcceptingStatusBoundaryRepository(),
    )

    result = control_plane._preflight_target(_target())

    assert control_plane._providers_for(definition) == ()
    assert result["eligible"] is False
    assert "sourceChecks" not in result


def test_moved_later_status_boundary_rejects_before_provider_commands(
    configured_environment: None,
) -> None:
    """持久化边界后的 env/manifest 后移必须零来源命令、零可提交分区地失败关闭。"""
    del configured_environment
    provider = RejectedStockConnectProvider()
    registry = SourceRegistry()
    registry.register(provider)
    definition = _definition()
    readiness = RecordingReadinessRepository()
    control_plane = DataOperationsControlPlane(
        database=cast(DatabaseClient, object()),
        catalog={definition.dataset_code: definition},
        source_registry=registry,
        now=_now,
        stock_connect_status_boundary_repository=RejectingStatusBoundaryRepository(),
        stock_connect_readiness_repository=readiness,
    )

    result = control_plane._preflight_target(_target())

    assert provider.preflight_calls == 0
    assert result["eligible"] is False
    assert result["estimatedPartitions"] == 0
    assert result["sourceChecks"] == [
        {
            "component": "stock-connect-status-boundary-lock",
            "accepted": False,
            "reason": "STATUS_BOUNDARY_MOVED_LATER",
        }
    ]
    assert (readiness.begun, readiness.finished) == (1, 1)
    assert readiness.outcomes == [
        StockConnectReadinessProbeOutcome(
            status="SOURCE_MISSING",
            reason_code="STATUS_SOURCE_MISSING",
            detail="Official stock-connect readiness preflight did not complete",
        )
    ]


def test_rejected_preflight_cannot_create_a_queued_command() -> None:
    """持久化预检含任一 eligible=false 时，提交事务必须在写 command 前终止。"""
    target = _target()
    preflight = DataOperationPreflight(
        preflight_id=UUID("40000000-0000-4000-8000-000000000001"),
        request_hash="a" * 64,
        targets_json=[target],
        result_json=[{"eligible": False}],
        created_at=_now(),
        expires_at=_now() + timedelta(minutes=5),
    )
    session = FakeSession(preflight)
    control_plane = DataOperationsControlPlane(
        database=cast(DatabaseClient, FakeDatabase(session)),
        catalog={},
        source_registry=SourceRegistry(),
        now=_now,
    )

    with pytest.raises(OperationProblem) as rejected:
        control_plane._submit_validated_command(
            targets=[target],
            submission_id=uuid4(),
            preflight_id=preflight.preflight_id,
            request_hash=preflight.request_hash,
            actor={
                "actorRef": "operator:test",
                "role": "OPERATOR",
                "reason": "验证来源授权拒绝",
            },
            idempotency_key="stock-connect-preflight-rejected",
            request_id="stock-connect-preflight-rejected",
            operation_hash="b" * 64,
            execution_intents=(None,),
        )

    assert rejected.value.code == "preflight-rejected"
    assert session.added == []
