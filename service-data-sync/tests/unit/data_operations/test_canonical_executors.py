"""统一 dispatcher 的 canonical executor 与冻结来源边界测试。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from service_data_sync.application.etf.nav_sync import EtfNavSyncService
from service_data_sync.application.ports.data_source import (
    DataSourcePort,
    ProviderBatch,
    ProviderError,
    ProviderErrorCode,
    SourceRequest,
)
from service_data_sync.application.ports.delivery_manifest import (
    DeliveryManifestIntegrityError,
    DeliveryManifestPageDescriptor,
)
from service_data_sync.application.ports.sector_eod import SectorEodExecutionMode
from service_data_sync.application.source_registry import SourceRegistry
from service_data_sync.bootstrap.container import ServiceContainer
from service_data_sync.domain.equity import EquityIdentifier
from service_data_sync.domain.etf import EtfIdentifier
from service_data_sync.domain.sector import SectorScheme
from service_data_sync.domain.stock_connect import StockConnectChannel
from service_data_sync.infrastructure.data_operations import canonical_executors
from service_data_sync.infrastructure.data_operations.control_plane import (
    DataOperationsControlPlane,
    ExecutionClaim,
)
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.database.fenced_execution import (
    FencedExecution,
    fenced_execution,
)
from service_data_sync.infrastructure.object_storage.client import ObjectStorageClient
from service_data_sync.infrastructure.object_storage.raw_payload_store import S3RawPayloadStore
from service_data_sync.infrastructure.persistence.etf_universe_repository import (
    EtfNavUnsupportedMember,
    EtfUniverseSnapshot,
)
from service_data_sync.infrastructure.persistence.sector_eod_repository import (
    SqlAlchemySectorEodRepository,
)


class FakeProvider:
    """提供固定能力集的 provider 替身，用于验证 run 快照不会在执行时换源。"""

    def __init__(self, provider_id: str, capabilities: frozenset[str]) -> None:
        """保存稳定 provider 标识和可声明能力，测试不会触发网络抓取。"""
        self.provider_id = provider_id
        self._capabilities = capabilities

    def capabilities(self) -> frozenset[str]:
        """返回启动时注册的能力闭集。"""
        return self._capabilities

    async def fetch(self, request: SourceRequest) -> ProviderBatch:
        """若冻结来源测试意外访问 Provider，立即失败而非产生网络副作用。"""
        del request
        raise AssertionError("frozen provider test must not fetch")


class RecordingControlPlane:
    """记录 dispatcher 执行器注册的最小控制面替身。"""

    def __init__(self) -> None:
        """初始化按数据集代码索引的空 executor 注册表。"""
        self.executors: dict[str, object] = {}

    def register_executor(self, dataset_code: str, executor: object) -> None:
        """保存注册请求，供测试验证 sector 数据集不再停留在目录占位状态。"""
        self.executors[dataset_code] = executor


@contextmanager
def _fake_database_session() -> Iterator[object]:
    """为纯执行器单测提供只进入不查询的 session 上下文。"""
    yield object()


def _share_capital_intent(
    entries: tuple[tuple[object, EquityIdentifier], ...],
) -> dict[str, object]:
    """生成与控制面相同规范化算法的冻结股本执行名单。"""
    roster = [
        {
            "instrumentId": str(instrument_id),
            "exchange": identifier.exchange.value,
            "symbol": identifier.symbol,
            "identityAsOf": "2026-07-30",
        }
        for instrument_id, identifier in entries
    ]
    return {
        "equityInstrumentRoster": roster,
        "equityInstrumentRosterHash": hashlib.sha256(
            json.dumps(
                roster,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
        ).hexdigest(),
    }


def test_frozen_provider_uses_run_snapshot_not_current_capability_order() -> None:
    """run 已冻结 PRIMARY provider 后，即使注册表另有同能力来源也不能被执行器自动替换。"""
    registry = SourceRegistry()
    frozen = FakeProvider("frozen", frozenset({"equity.bar.1d.raw"}))
    registry.register(FakeProvider("new-default", frozenset({"equity.bar.1d.raw"})))
    registry.register(frozen)
    container = cast(ServiceContainer, SimpleNamespace(source_registry=registry))
    snapshot = [
        {
            "providerId": "frozen",
            "sourceDataset": "equity.bar.1d.raw",
            "effective": True,
        }
    ]

    selected = canonical_executors._frozen_provider(snapshot, container, "equity.bar.1d.raw")

    assert selected is frozen


def test_frozen_provider_rejects_missing_snapshot_provider() -> None:
    """冻结 provider 从运行时注册表消失时必须安全失败，不能改用另一个可用来源。"""
    registry = SourceRegistry()
    registry.register(FakeProvider("replacement", frozenset({"equity.bar.1d.raw"})))
    container = cast(ServiceContainer, SimpleNamespace(source_registry=registry))
    snapshot = [
        {
            "providerId": "removed-provider",
            "sourceDataset": "equity.bar.1d.raw",
            "effective": True,
        }
    ]

    with pytest.raises(ProviderError) as raised:
        canonical_executors._frozen_provider(snapshot, container, "equity.bar.1d.raw")

    assert raised.value.retryable is True


def test_stock_connect_resume_skips_success_and_keeps_business_days_atomic() -> None:
    """中段 worker 恢复只计划未成功日包，公平批次不得拆开其余同日通道。"""
    channels = tuple(
        StockConnectChannel(channel, direction)
        for channel in ("SH", "SZ")
        for direction in ("NORTHBOUND", "SOUTHBOUND")
    )
    dates = tuple(date(2026, 7, day) for day in range(20, 28))
    tasks = [(channel, trade_date) for trade_date in dates for channel in channels]
    succeeded = frozenset(
        canonical_executors._stock_connect_partition(
            channel=channel,
            trade_date=trade_date,
        )
        for trade_date in dates[:2]
        for channel in channels
    ) | frozenset(
        canonical_executors._stock_connect_partition(
            channel=channel,
            trade_date=dates[2],
        )
        for channel in channels[:2]
    )

    batch, has_more = canonical_executors._stock_connect_next_batch(
        tasks,
        succeeded=succeeded,
    )

    batch_keys = {
        canonical_executors._stock_connect_partition(
            channel=channel,
            trade_date=trade_date,
        )
        for channel, trade_date in batch
    }
    assert not batch_keys & succeeded
    assert {trade_date for _channel, trade_date in batch} == set(dates[-5:])
    assert all(
        {channel for channel, current in batch if current == trade_date} == set(channels)
        for trade_date in dates[-5:]
    )
    assert has_more is True


def test_stock_connect_manifest_reference_selects_only_the_pending_page() -> None:
    """执行意图只携带 header 引用，完成首页面后必须直接选择第二页而不载入正文全集。"""
    manifest_id = uuid4()
    intent = {
        "stockConnectDeliveryManifestRef": {
            "manifestId": str(manifest_id),
            "rootHash": "a" * 64,
            "targetCount": 8,
            "pageCount": 2,
        }
    }
    descriptors = (
        DeliveryManifestPageDescriptor(
            page_no=0,
            date_from=date(2026, 7, 21),
            date_to=date(2026, 7, 21),
            trade_date_count=1,
            target_count=4,
            page_hash="b" * 64,
        ),
        DeliveryManifestPageDescriptor(
            page_no=1,
            date_from=date(2026, 7, 20),
            date_to=date(2026, 7, 20),
            trade_date_count=1,
            target_count=4,
            page_hash="c" * 64,
        ),
    )

    parsed = canonical_executors._stock_connect_manifest_reference(intent)
    pending = canonical_executors._stock_connect_pending_page(
        descriptors,
        completed_partitions=4,
        expected_target_count=8,
        expected_page_count=2,
    )

    assert parsed == (manifest_id, "a" * 64, 8, 2)
    assert pending.page_no == 1
    with pytest.raises(DeliveryManifestIntegrityError):
        canonical_executors._stock_connect_pending_page(
            descriptors,
            completed_partitions=4,
            expected_target_count=9,
            expected_page_count=2,
        )


def test_register_canonical_executors_includes_sector_and_etf_datasets() -> None:
    """控制面 dispatcher 必须实际注册成分、派生、三个既有 sector 与四个 ETF 数据集。"""
    control_plane = RecordingControlPlane()

    canonical_executors.register_canonical_executors(
        cast(DataOperationsControlPlane, control_plane),
        cast(ServiceContainer, object()),
    )

    assert {
        "sector.catalog.raw",
        "sector.membership.release",
        "sector.quote.eod.snapshot",
        "sector.sw.taxonomy",
        "financial.derived-metric",
        "fund.etf.profile.reported",
        "fund.etf.trading_state.reported",
        "fund.etf.bar.1d.reported",
        "fund.etf.nav.1d.reported",
    }.issubset(control_plane.executors)


def test_financial_derived_executor_uses_same_control_plane_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """派生执行器复用 fencing run 身份、零 provider 调用，并在 publication 内武装终态。"""
    captured: dict[str, object] = {}

    def derive(**kwargs: object) -> SimpleNamespace:
        """捕获派生组合根参数并模拟同事务 publication。"""
        captured.update(kwargs)
        callback = kwargs["before_final_publication"]
        assert callable(callback)
        callback()
        return SimpleNamespace(publication=SimpleNamespace(data_version=uuid4(), row_count=24))

    monkeypatch.setattr(canonical_executors, "run_financial_derivation", derive)
    monkeypatch.setattr(canonical_executors, "_cancel_requested", lambda _container: False)
    run_id = uuid4()
    database = cast(DatabaseClient, object())
    execution = FencedExecution(
        database=database,
        run_id=run_id,
        fencing_token=3,
        finalizer=lambda _session, _execution: None,
    )
    claim = ExecutionClaim(
        run_id=run_id,
        dataset_code="financial.derived-metric",
        fencing_token=3,
        target={
            "mode": "FULL",
            "selector": {"kind": "INSTRUMENT", "exchange": "SSE", "symbol": "600519"},
        },
        source_snapshot=[],
    )

    with fenced_execution(execution):
        outcome = canonical_executors._execute_financial_derived_metric(
            claim,
            container=cast(ServiceContainer, SimpleNamespace(database=database)),
        )

    assert outcome.status == "SUCCEEDED"
    assert outcome.processed_records == 24
    assert captured["run_id"] == run_id
    assert captured["exchange"] == EquityIdentifier.parse("SSE.600519").exchange
    assert captured["symbol"] == "600519"
    assert execution.terminal_armed is True


def test_sector_membership_executor_preserves_exact_sector_selector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """成分执行器把单板块选择器原样传给真实 scheme 用例，release 缺失时不会伪装成功。"""
    captured: dict[str, object] = {}
    provider = FakeProvider(
        "akshare-eastmoney-sector-membership",
        frozenset({"sector.membership.snapshot.raw"}),
    )

    def sync_membership(**kwargs: object) -> SimpleNamespace:
        """捕获受控分区并返回无 release 的真实 partial 形状。"""
        captured.update(kwargs)
        return SimpleNamespace(items=(object(),), failures=(), release=None)

    monkeypatch.setattr(canonical_executors, "_frozen_provider", lambda *_args: provider)
    monkeypatch.setattr(
        canonical_executors,
        "SqlAlchemySectorMembershipRepository",
        lambda _database: object(),
    )
    monkeypatch.setattr(canonical_executors, "_membership_partition_count", lambda *_args, **_kw: 1)
    monkeypatch.setattr(canonical_executors, "S3RawPayloadStore", lambda _client: object())
    monkeypatch.setattr(canonical_executors, "_sync_sector_membership", sync_membership)
    monkeypatch.setattr(canonical_executors, "_cancel_requested", lambda _container: False)
    database = cast(DatabaseClient, object())
    claim = ExecutionClaim(
        run_id=uuid4(),
        dataset_code="sector.membership.release",
        fencing_token=5,
        target={
            "mode": "FULL",
            "selector": {
                "kind": "SECTOR",
                "scheme": "eastmoney.industry",
                "sectorCode": "BK1507",
            },
        },
        source_snapshot=[],
    )

    outcome = canonical_executors._execute_sector_membership(
        claim,
        container=cast(
            ServiceContainer,
            SimpleNamespace(database=database, object_storage=object()),
        ),
    )

    assert outcome.status == "PARTIAL"
    assert captured["scheme"] is SectorScheme.EASTMONEY_INDUSTRY
    assert captured["sector_codes"] == ("BK1507",)
    assert captured["final_write"] is True


@pytest.mark.parametrize(
    "capability",
    (
        "sector.catalog.raw",
        "sector.membership.snapshot.raw",
        "sector.quote.eod.snapshot.raw",
        "sector.sw.snapshot.raw",
    ),
)
def test_sector_frozen_provider_uses_the_exact_source_snapshot_binding(capability: str) -> None:
    """三个 sector executor 都必须按 run 快照取回真实 raw capability，禁止执行时换源。"""
    registry = SourceRegistry()
    frozen = FakeProvider(f"frozen-{capability}", frozenset({capability}))
    registry.register(frozen)
    container = cast(ServiceContainer, SimpleNamespace(source_registry=registry))
    snapshot = [
        {
            "providerId": frozen.provider_id,
            "sourceDataset": capability,
            "effective": True,
        }
    ]

    selected = canonical_executors._frozen_provider(snapshot, container, capability)

    assert selected is frozen


def test_sector_eod_recovery_refetches_frozen_provider_without_legacy_raw_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """新 command 的 EOD 恢复必须重抓冻结来源，不能读取成功 raw URI 或调用旧 replay 路径。"""
    calls: list[str] = []

    class FakeSectorEodService:
        """捕获 command executor 的 EOD 调用方式，不触碰数据库或对象存储。"""

        def __init__(self, **kwargs: object) -> None:
            """保存构造参数，确认来源已由失败留证包装器包裹。"""
            source = cast(DataSourcePort, kwargs["source"])
            assert source.provider_id == "frozen-eod"

        async def sync(self, **kwargs: object) -> None:
            """记录重新抓取调用并执行末次 fenced publication 回调。"""
            assert kwargs["execution_mode"] is SectorEodExecutionMode.PUBLISH
            callback = kwargs["before_final_publication"]
            assert callable(callback)
            callback()
            calls.append("sync")

    def finalize(_session: Session, _execution: FencedExecution) -> None:
        """测试不提交数据库，只验证执行器武装终态而不走 legacy replay。"""
        return

    monkeypatch.setattr(
        canonical_executors,
        "SectorEodSnapshotSyncService",
        FakeSectorEodService,
    )
    execution = FencedExecution(
        database=cast(DatabaseClient, object()),
        run_id=uuid4(),
        fencing_token=1,
        finalizer=finalize,
    )
    raw_store = S3RawPayloadStore(cast(ObjectStorageClient, object()))
    container = cast(ServiceContainer, SimpleNamespace(trading_calendar=object()))

    with fenced_execution(execution):
        canonical_executors._sync_sector_eod_snapshot(
            container=container,
            provider=FakeProvider(
                "frozen-eod",
                frozenset({"sector.quote.eod.snapshot.raw"}),
            ),
            repository=cast(SqlAlchemySectorEodRepository, object()),
            raw_store=raw_store,
            scheme=SectorScheme.EASTMONEY_INDUSTRY,
            trade_date=date(2026, 7, 29),
            final_write=True,
        )

    assert calls == ["sync"]
    assert execution.terminal_armed is True


def test_share_capital_partial_run_retries_only_failed_security(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A/B 成功 C 失败后，新 run 继承成功分区且只重新调用 C。"""
    identifiers = tuple(
        EquityIdentifier.parse(value) for value in ("SSE.600001", "SSE.600002", "SSE.600003")
    )
    instrument_ids = tuple(uuid4() for _identifier in identifiers)
    execution_intent = _share_capital_intent(tuple(zip(instrument_ids, identifiers, strict=True)))
    provider_calls: list[str] = []
    partition_states: dict[str, str] = {}
    inherited: frozenset[str] = frozenset()
    failed_symbol: str | None = "600003"

    class FakeShareCapitalService:
        """记录实际进入同步服务的证券，并可为指定代码制造不可重试来源失败。"""

        def __init__(self, **_kwargs: object) -> None:
            """测试不依赖真实 provider、repository 或对象存储。"""

        async def sync(
            self,
            *,
            identifier: EquityIdentifier,
            instrument_id: object,
            identity_as_of: date,
        ) -> SimpleNamespace:
            """记录调用并返回最小发布计数，指定证券则抛出真实 ProviderError。"""
            assert instrument_id in instrument_ids
            assert identity_as_of == date(2026, 7, 30)
            provider_calls.append(identifier.symbol)
            if identifier.symbol == failed_symbol:
                raise ProviderError(
                    ProviderErrorCode.UNAVAILABLE,
                    "fixture source unavailable",
                    retryable=False,
                )
            return SimpleNamespace(inserted_count=1, unchanged_count=0)

    def record_partition(
        _container: ServiceContainer,
        *,
        run_id: object,
        partition_key: str,
        status: str,
        error_code: str | None,
        **_kwargs: object,
    ) -> None:
        """保存每个分区终态；run_id 和错误码只验证调用未遗漏。"""
        del run_id, error_code
        partition_states[partition_key] = status

    monkeypatch.setattr(
        canonical_executors,
        "_prepare_operation_partitions",
        lambda _container, *, run_id, partition_keys, subject: inherited,
    )
    monkeypatch.setattr(
        canonical_executors,
        "_frozen_equity_identity_is_current",
        lambda _container, *, identity: True,
    )
    monkeypatch.setattr(
        canonical_executors,
        "_frozen_provider",
        lambda _snapshot, _container, _capability: object(),
    )
    monkeypatch.setattr(
        canonical_executors,
        "_equity_workspace_repository",
        lambda _container: object(),
    )
    monkeypatch.setattr(canonical_executors, "S3RawPayloadStore", lambda _client: object())
    monkeypatch.setattr(
        canonical_executors,
        "FailureEvidenceDataSource",
        lambda provider, _store: provider,
    )
    monkeypatch.setattr(
        canonical_executors,
        "retain_failure_evidence",
        lambda _store, operation: operation(),
    )
    monkeypatch.setattr(
        canonical_executors,
        "EquityShareCapitalSyncService",
        FakeShareCapitalService,
    )
    monkeypatch.setattr(canonical_executors, "_record_operation_partition", record_partition)
    monkeypatch.setattr(canonical_executors, "_cancel_requested", lambda _container: False)
    container = cast(
        ServiceContainer,
        SimpleNamespace(database=object(), object_storage=object()),
    )
    first_claim = ExecutionClaim(
        run_id=uuid4(),
        dataset_code="equity.share_capital.reported",
        fencing_token=1,
        target={"mode": "FULL", "selector": {"kind": "GLOBAL"}},
        source_snapshot=[],
        execution_intent=execution_intent,
    )
    first_execution = FencedExecution(
        database=cast(DatabaseClient, object()),
        run_id=first_claim.run_id,
        fencing_token=1,
        finalizer=lambda _session, _execution: None,
    )
    with fenced_execution(first_execution):
        first = canonical_executors._execute_equity_share_capital(
            first_claim,
            container=container,
        )

    assert first.status == "PARTIAL"
    assert first.completed_partitions == 2
    assert provider_calls == ["600001", "600002", "600003"]
    assert partition_states == {
        f"security:{instrument_ids[0]}": "SUCCEEDED",
        f"security:{instrument_ids[1]}": "SUCCEEDED",
        f"security:{instrument_ids[2]}": "FAILED",
    }

    inherited = frozenset(key for key, status in partition_states.items() if status == "SUCCEEDED")
    failed_symbol = None
    provider_calls.clear()
    retry_claim = ExecutionClaim(
        run_id=uuid4(),
        dataset_code="equity.share_capital.reported",
        fencing_token=2,
        target=first_claim.target,
        source_snapshot=first_claim.source_snapshot,
        execution_intent=first_claim.execution_intent,
    )
    retry_execution = FencedExecution(
        database=cast(DatabaseClient, object()),
        run_id=retry_claim.run_id,
        fencing_token=2,
        finalizer=lambda _session, _execution: None,
    )
    with fenced_execution(retry_execution):
        retried = canonical_executors._execute_equity_share_capital(
            retry_claim,
            container=container,
        )

    assert retried.status == "SUCCEEDED"
    assert retried.completed_partitions == 3
    assert provider_calls == ["600003"]


def test_share_capital_retry_with_all_partitions_inherited_skips_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """新 run 已继承全部成功分区时直接完成，不构造单证券同步调用。"""
    identifiers = tuple(EquityIdentifier.parse(value) for value in ("SSE.600001", "SSE.600002"))
    instrument_ids = tuple(uuid4() for _identifier in identifiers)
    inherited = frozenset(
        canonical_executors._security_partition(instrument_id) for instrument_id in instrument_ids
    )
    monkeypatch.setattr(
        canonical_executors,
        "_prepare_operation_partitions",
        lambda _container, *, run_id, partition_keys, subject: inherited,
    )
    monkeypatch.setattr(
        canonical_executors,
        "_frozen_provider",
        lambda _snapshot, _container, _capability: object(),
    )
    monkeypatch.setattr(
        canonical_executors,
        "_equity_workspace_repository",
        lambda _container: object(),
    )
    monkeypatch.setattr(canonical_executors, "S3RawPayloadStore", lambda _client: object())
    claim = ExecutionClaim(
        run_id=uuid4(),
        dataset_code="equity.share_capital.reported",
        fencing_token=3,
        target={"mode": "FULL", "selector": {"kind": "GLOBAL"}},
        source_snapshot=[],
        execution_intent=_share_capital_intent(
            tuple(zip(instrument_ids, identifiers, strict=True))
        ),
    )
    execution = FencedExecution(
        database=cast(DatabaseClient, object()),
        run_id=claim.run_id,
        fencing_token=3,
        finalizer=lambda _session, _execution: None,
    )

    with fenced_execution(execution):
        outcome = canonical_executors._execute_equity_share_capital(
            claim,
            container=cast(
                ServiceContainer,
                SimpleNamespace(database=object(), object_storage=object()),
            ),
        )

    assert outcome.status == "SUCCEEDED"
    assert outcome.completed_partitions == 2
    assert outcome.total_partitions == 2


def test_share_capital_code_reuse_never_inherits_or_publishes_under_old_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """原证券退市且代码复用后，旧 run 安全失败，新 run 使用新 UUID 独立分区。"""
    identifier = EquityIdentifier.parse("SSE.600001")
    old_instrument_id = uuid4()
    new_instrument_id = uuid4()
    current_instrument_id = new_instrument_id
    provider_calls: list[object] = []
    partition_states: dict[str, str] = {}
    prepared_sets: list[frozenset[str]] = []

    class FakeShareCapitalService:
        """记录成功新身份的股本调用，旧身份不得抵达此服务。"""

        def __init__(self, **_kwargs: object) -> None:
            """测试不依赖真实来源和持久化仓储。"""

        async def sync(
            self,
            *,
            identifier: EquityIdentifier,
            instrument_id: object,
            identity_as_of: date,
        ) -> SimpleNamespace:
            """确认新命令仍使用同代码，但永久身份和冻结日期独立。"""
            assert identifier == EquityIdentifier.parse("SSE.600001")
            assert identity_as_of == date(2026, 7, 30)
            provider_calls.append(instrument_id)
            return SimpleNamespace(inserted_count=1, unchanged_count=0)

    def prepare_partitions(
        _container: ServiceContainer,
        *,
        run_id: object,
        partition_keys: frozenset[str],
        subject: str,
    ) -> frozenset[str]:
        """捕获每个 run 的完整冻结 UUID 分区集合。"""
        del run_id
        assert subject == "share capital identity roster"
        prepared_sets.append(partition_keys)
        return frozenset()

    def record_partition(
        _container: ServiceContainer,
        *,
        run_id: object,
        partition_key: str,
        status: str,
        error_code: str | None,
        **_kwargs: object,
    ) -> None:
        """记录代码复用保护触发的稳定失败，不依赖真实控制面。"""
        del run_id
        partition_states[partition_key] = f"{status}:{error_code}"

    monkeypatch.setattr(
        canonical_executors,
        "_prepare_operation_partitions",
        prepare_partitions,
    )
    monkeypatch.setattr(
        canonical_executors,
        "_frozen_equity_identity_is_current",
        lambda _container, *, identity: identity.instrument_id == current_instrument_id,
    )
    monkeypatch.setattr(
        canonical_executors,
        "_frozen_provider",
        lambda _snapshot, _container, _capability: object(),
    )
    monkeypatch.setattr(
        canonical_executors,
        "_equity_workspace_repository",
        lambda _container: object(),
    )
    monkeypatch.setattr(canonical_executors, "S3RawPayloadStore", lambda _client: object())
    monkeypatch.setattr(
        canonical_executors,
        "FailureEvidenceDataSource",
        lambda provider, _store: provider,
    )
    monkeypatch.setattr(
        canonical_executors,
        "retain_failure_evidence",
        lambda _store, operation: operation(),
    )
    monkeypatch.setattr(
        canonical_executors,
        "EquityShareCapitalSyncService",
        FakeShareCapitalService,
    )
    monkeypatch.setattr(canonical_executors, "_record_operation_partition", record_partition)
    monkeypatch.setattr(canonical_executors, "_cancel_requested", lambda _container: False)
    container = cast(
        ServiceContainer,
        SimpleNamespace(database=object(), object_storage=object()),
    )

    old_claim = ExecutionClaim(
        run_id=uuid4(),
        dataset_code="equity.share_capital.reported",
        fencing_token=1,
        target={"mode": "FULL", "selector": {"kind": "GLOBAL"}},
        source_snapshot=[],
        execution_intent=_share_capital_intent(((old_instrument_id, identifier),)),
    )
    old_execution = FencedExecution(
        database=cast(DatabaseClient, object()),
        run_id=old_claim.run_id,
        fencing_token=1,
        finalizer=lambda _session, _execution: None,
    )
    with fenced_execution(old_execution):
        old_outcome = canonical_executors._execute_equity_share_capital(
            old_claim,
            container=container,
        )

    assert old_outcome.status == "PARTIAL"
    assert provider_calls == []
    assert partition_states == {f"security:{old_instrument_id}": "FAILED:IDENTITY_CHANGED"}

    new_claim = ExecutionClaim(
        run_id=uuid4(),
        dataset_code="equity.share_capital.reported",
        fencing_token=2,
        target=old_claim.target,
        source_snapshot=old_claim.source_snapshot,
        execution_intent=_share_capital_intent(((new_instrument_id, identifier),)),
    )
    new_execution = FencedExecution(
        database=cast(DatabaseClient, object()),
        run_id=new_claim.run_id,
        fencing_token=2,
        finalizer=lambda _session, _execution: None,
    )
    with fenced_execution(new_execution):
        new_outcome = canonical_executors._execute_equity_share_capital(
            new_claim,
            container=container,
        )

    assert new_outcome.status == "SUCCEEDED"
    assert provider_calls == [new_instrument_id]
    assert prepared_sets == [
        frozenset({f"security:{old_instrument_id}"}),
        frozenset({f"security:{new_instrument_id}"}),
    ]


@pytest.mark.parametrize(
    ("reason_code", "retryable", "expected_calls", "expected_status"),
    (
        ("unavailable", True, 3, "PARTIAL"),
        ("rate_limited", True, 1, "FAILED"),
        ("authentication", False, 1, "FAILED"),
        ("invalid_request", False, 1, "FAILED"),
    ),
)
def test_all_etfs_bounds_systemic_source_failures_and_preserves_retryability(
    monkeypatch: pytest.MonkeyPatch,
    reason_code: str,
    retryable: bool,
    expected_calls: int,
    expected_status: str,
) -> None:
    """全市场来源错误按稳定分类熔断，认证、配置或限流不得放大为逐 ETF 请求风暴。"""
    identifiers = tuple(
        EtfIdentifier.parse(value) for value in ("SSE.510300", "SSE.510500", "SZSE.159919")
    )
    snapshot = EtfUniverseSnapshot(
        profile_data_versions={"SSE": uuid4(), "SZSE": uuid4()},
        identifiers=identifiers,
        universe_hash="u" * 64,
    )
    provider_calls: list[str] = []
    partition_states: dict[str, str] = {}

    def load_universe(_session: object, **_kwargs: object) -> EtfUniverseSnapshot:
        """返回冻结三只 ETF 的目录快照。"""
        return snapshot

    def prepare_partitions(
        _container: ServiceContainer,
        *,
        run_id: object,
        identifiers: tuple[EtfIdentifier, ...],
    ) -> frozenset[str]:
        """声明本次没有继承成功水位。"""
        del run_id
        assert identifiers == snapshot.identifiers
        return frozenset()

    def sync_partition(
        _service: object,
        *,
        identifier: EtfIdentifier,
        **_kwargs: object,
    ) -> SimpleNamespace:
        """首只返回指定来源空态，其余仅在未熔断场景返回成功。"""
        provider_calls.append(identifier.qualified_key)
        if len(provider_calls) == 1:
            return SimpleNamespace(
                availability="source_unavailable",
                reason_code=reason_code,
                retryable=retryable,
                inserted_count=0,
                unchanged_count=0,
            )
        return SimpleNamespace(
            availability="available",
            reason_code=None,
            retryable=False,
            inserted_count=1,
            unchanged_count=0,
        )

    def record_partition(
        _container: ServiceContainer,
        *,
        run_id: object,
        partition_key: str,
        status: str,
        **_kwargs: object,
    ) -> None:
        """记录最后一次分区状态，供结果汇总替身读取。"""
        del run_id
        partition_states[partition_key] = status

    def partition_counts(
        _container: ServiceContainer,
        *,
        run_id: object,
    ) -> tuple[int, int]:
        """按内存分区状态返回成功和失败数。"""
        del run_id
        values = tuple(partition_states.values())
        return values.count("SUCCEEDED"), values.count("FAILED")

    monkeypatch.setattr(canonical_executors, "load_frozen_etf_universe", load_universe)
    monkeypatch.setattr(
        canonical_executors,
        "_prepare_etf_operation_partitions",
        prepare_partitions,
    )
    monkeypatch.setattr(canonical_executors, "_sync_etf_partition", sync_partition)
    monkeypatch.setattr(canonical_executors, "_record_operation_partition", record_partition)
    monkeypatch.setattr(canonical_executors, "_etf_partition_counts", partition_counts)
    monkeypatch.setattr(canonical_executors, "_cancel_requested", lambda _container: False)
    claim = ExecutionClaim(
        run_id=uuid4(),
        dataset_code="fund.etf.bar.1d.reported",
        fencing_token=1,
        target={
            "mode": "FULL",
            "selector": {
                "kind": "ETF",
                "operation": "BARS",
                "venue": None,
                "scope": "ALL_ETFS",
                "etf": None,
                "profileDataVersions": {
                    venue: str(snapshot.profile_data_versions[venue]) for venue in ("SSE", "SZSE")
                },
            },
        },
        source_snapshot=[],
        execution_intent={
            "etfUniverseCount": snapshot.count,
            "etfUniverseHash": snapshot.universe_hash,
        },
    )
    container = cast(
        ServiceContainer,
        SimpleNamespace(database=SimpleNamespace(session=_fake_database_session)),
    )

    outcome = canonical_executors._execute_etf_all(
        claim,
        container=container,
        selector=cast(dict[str, Any], claim.target["selector"]),
        service=cast(Any, object()),
        raw_store=cast(Any, object()),
        start=date(2026, 7, 1),
        end=date(2026, 7, 30),
    )

    assert outcome.status == expected_status
    assert len(provider_calls) == expected_calls
    assert outcome.error is not None
    assert outcome.error["retryable"] is retryable


def test_all_etfs_nav_skips_frozen_money_market_semantics_without_opening_circuit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """货币 ETF 按官方 profile 冻结为审计 SKIPPED，其余支持集全量完成且 run 成功。"""
    identifiers = tuple(
        EtfIdentifier.parse(value) for value in ("SSE.510300", "SSE.511600", "SZSE.159919")
    )
    unsupported = EtfNavUnsupportedMember(
        identifier=identifiers[1],
        profile_type="交易型货币基金",
    )
    snapshot = EtfUniverseSnapshot(
        profile_data_versions={"SSE": uuid4(), "SZSE": uuid4()},
        identifiers=identifiers,
        universe_hash="n" * 64,
        nav_unsupported=(unsupported,),
    )
    provider_calls: list[str] = []
    unsupported_calls: list[str] = []
    partition_states: dict[str, str] = {}
    partition_errors: dict[str, tuple[str | None, str]] = {}
    partition_checkpoints: dict[str, dict[str, object] | None] = {}

    def load_universe(_session: object, **_kwargs: object) -> EtfUniverseSnapshot:
        """返回含一个官方货币类型成员的冻结目录快照。"""
        return snapshot

    def prepare_partitions(
        _container: ServiceContainer,
        *,
        run_id: object,
        identifiers: tuple[EtfIdentifier, ...],
    ) -> frozenset[str]:
        """声明本次没有继承完成水位。"""
        del run_id
        assert identifiers == snapshot.identifiers
        return frozenset()

    def mark_unsupported(
        _service: EtfNavSyncService,
        *,
        etf: EtfIdentifier,
        **_kwargs: object,
    ) -> SimpleNamespace:
        """记录暂不支持观察，不触发 Provider。"""
        unsupported_calls.append(etf.qualified_key)
        return SimpleNamespace(availability="currently_unsupported")

    def sync_partition(
        _service: object,
        *,
        identifier: EtfIdentifier,
        **_kwargs: object,
    ) -> SimpleNamespace:
        """支持集返回真实成功。"""
        provider_calls.append(identifier.qualified_key)
        return SimpleNamespace(
            availability="available",
            reason_code=None,
            retryable=False,
            inserted_count=1,
            unchanged_count=0,
        )

    def record_partition(
        _container: ServiceContainer,
        *,
        run_id: object,
        partition_key: str,
        status: str,
        error_code: str | None,
        error_stage: str = "PROVIDER_FETCH",
        checkpoint_evidence: dict[str, object] | None = None,
        **_kwargs: object,
    ) -> None:
        """保存分区终态与稳定审计原因。"""
        del run_id
        partition_states[partition_key] = status
        partition_errors[partition_key] = (error_code, error_stage)
        partition_checkpoints[partition_key] = checkpoint_evidence

    def partition_counts(
        _container: ServiceContainer,
        *,
        run_id: object,
    ) -> tuple[int, int]:
        """把 SUCCEEDED 与 SKIPPED 都计入已完成总数。"""
        del run_id
        values = tuple(partition_states.values())
        return (
            values.count("SUCCEEDED") + values.count("SKIPPED"),
            values.count("FAILED"),
        )

    monkeypatch.setattr(canonical_executors, "load_frozen_etf_universe", load_universe)
    monkeypatch.setattr(
        canonical_executors,
        "_prepare_etf_operation_partitions",
        prepare_partitions,
    )
    monkeypatch.setattr(EtfNavSyncService, "mark_currently_unsupported", mark_unsupported)
    monkeypatch.setattr(canonical_executors, "_sync_etf_partition", sync_partition)
    monkeypatch.setattr(canonical_executors, "_record_operation_partition", record_partition)
    monkeypatch.setattr(canonical_executors, "_etf_partition_counts", partition_counts)
    monkeypatch.setattr(canonical_executors, "_cancel_requested", lambda _container: False)
    claim = ExecutionClaim(
        run_id=uuid4(),
        dataset_code="fund.etf.nav.1d.reported",
        fencing_token=1,
        target={
            "mode": "FULL",
            "selector": {
                "kind": "ETF",
                "operation": "NAV",
                "venue": None,
                "scope": "ALL_ETFS",
                "etf": None,
                "profileDataVersions": {
                    venue: str(snapshot.profile_data_versions[venue]) for venue in ("SSE", "SZSE")
                },
            },
        },
        source_snapshot=[],
        execution_intent={
            "etfUniverseCount": snapshot.count,
            "etfUniverseHash": snapshot.universe_hash,
            "etfNavEligibleCount": 2,
            "etfNavUnsupportedCount": 1,
        },
    )
    container = cast(
        ServiceContainer,
        SimpleNamespace(database=SimpleNamespace(session=_fake_database_session)),
    )
    service = object.__new__(EtfNavSyncService)

    outcome = canonical_executors._execute_etf_all(
        claim,
        container=container,
        selector=cast(dict[str, Any], claim.target["selector"]),
        service=service,
        raw_store=cast(Any, object()),
        start=date(2026, 7, 1),
        end=date(2026, 7, 30),
    )

    assert outcome.status == "SUCCEEDED"
    assert outcome.completed_partitions == 3
    assert outcome.total_partitions == 3
    assert provider_calls == ["SSE.510300", "SZSE.159919"]
    assert unsupported_calls == ["SSE.511600"]
    assert partition_states["etf:SSE.511600"] == "SKIPPED"
    assert partition_errors["etf:SSE.511600"] == (
        "NAV_SEMANTICS_UNSUPPORTED_MONEY_MARKET",
        "ELIGIBILITY",
    )
    assert partition_checkpoints["etf:SSE.511600"] == {
        "profileDataVersions": {
            venue: str(snapshot.profile_data_versions[venue]) for venue in ("SSE", "SZSE")
        },
        "etfUniverseHash": snapshot.universe_hash,
        "etf": "SSE.511600",
        "reasonCode": "NAV_SEMANTICS_UNSUPPORTED_MONEY_MARKET",
    }


@pytest.mark.parametrize(
    ("inherited", "expected_calls"),
    [
        (frozenset({"venue:SSE"}), ["SZSE"]),
        (frozenset({"venue:SSE", "venue:SZSE"}), []),
    ],
)
def test_all_venues_retry_inherits_successful_profile_publication(
    monkeypatch: pytest.MonkeyPatch,
    inherited: frozenset[str],
    expected_calls: list[str],
) -> None:
    """双市场目录 retry 继承成功水位；崩溃后全完成时不得再请求 Provider。"""
    provider_calls: list[str] = []
    partition_states = {partition_key: "SUCCEEDED" for partition_key in inherited}

    class FakeMasterService:
        """记录真正重新请求的目录场所。"""

        async def sync(self, *, venue: str, observation_date: date) -> SimpleNamespace:
            """为待重试场所返回一个真实 publication 计数。"""
            assert observation_date == date(2026, 7, 30)
            provider_calls.append(venue)
            return SimpleNamespace(
                availability="available",
                reason_code=None,
                retryable=False,
                inserted_count=1,
                unchanged_count=0,
            )

    def prepare_partitions(
        _container: ServiceContainer,
        *,
        run_id: object,
        partition_keys: frozenset[str],
        subject: str,
    ) -> frozenset[str]:
        """确认固定双市场集合并返回继承的 SSE 水位。"""
        del run_id
        assert partition_keys == frozenset({"venue:SSE", "venue:SZSE"})
        assert subject == "ETF profile venues"
        return inherited

    def record_partition(
        _container: ServiceContainer,
        *,
        run_id: object,
        partition_key: str,
        status: str,
        **_kwargs: object,
    ) -> None:
        """记录 SZSE 重试状态。"""
        del run_id
        partition_states[partition_key] = status

    def partition_counts(
        _container: ServiceContainer,
        *,
        run_id: object,
        prefix: str,
    ) -> tuple[int, int]:
        """汇总双市场目录分区状态。"""
        del run_id
        assert prefix == "venue:"
        values = tuple(partition_states.values())
        return values.count("SUCCEEDED"), values.count("FAILED")

    monkeypatch.setattr(canonical_executors, "_prepare_operation_partitions", prepare_partitions)
    monkeypatch.setattr(canonical_executors, "_record_operation_partition", record_partition)
    monkeypatch.setattr(canonical_executors, "_operation_partition_counts", partition_counts)
    monkeypatch.setattr(canonical_executors, "_cancel_requested", lambda _container: False)
    monkeypatch.setattr(
        canonical_executors,
        "retain_failure_evidence",
        lambda _store, operation: operation(),
    )
    claim = ExecutionClaim(
        run_id=uuid4(),
        dataset_code="fund.etf.profile.reported",
        fencing_token=2,
        target={
            "mode": "FULL",
            "selector": {
                "kind": "ETF",
                "operation": "MASTER",
                "scope": "ALL_VENUES",
                "venue": None,
                "etf": None,
            },
        },
        source_snapshot=[],
    )

    outcome = canonical_executors._execute_etf_master_all_venues(
        claim,
        container=cast(ServiceContainer, object()),
        service=cast(Any, FakeMasterService()),
        raw_store=cast(Any, object()),
        observation_date=date(2026, 7, 30),
    )

    assert outcome.status == "SUCCEEDED"
    assert outcome.completed_partitions == 2
    assert outcome.total_partitions == 2
    assert provider_calls == expected_calls


def test_stale_all_venues_profile_run_fails_before_provider_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """跨日排队的目录快照必须双分区失败，且不得解析或请求任何 Provider。"""
    partition_states: dict[str, str] = {}

    def current_execution() -> object:
        """提供仅用于跨日拒绝分支的非空 fencing 上下文。"""
        return object()

    def reject_provider_resolution(*_args: object, **_kwargs: object) -> object:
        """若跨日拒绝前触及来源注册表，立即暴露错误执行顺序。"""
        raise AssertionError("stale ETF profile run must not resolve a provider")

    def prepare_partitions(
        _container: ServiceContainer,
        *,
        run_id: object,
        partition_keys: frozenset[str],
        subject: str,
    ) -> frozenset[str]:
        """确认跨日拒绝仍建立固定双市场分区账本。"""
        del run_id
        assert partition_keys == frozenset({"venue:SSE", "venue:SZSE"})
        assert subject == "ETF profile venues"
        return frozenset()

    def record_partition(
        _container: ServiceContainer,
        *,
        run_id: object,
        partition_key: str,
        status: str,
        error_code: str | None,
        error_retryable: bool = False,
    ) -> None:
        """记录跨日拒绝的分区终态与不可重试属性。"""
        del run_id
        assert error_code == "ETF_PROFILE_CURRENT_SNAPSHOT_UNRECOVERABLE"
        assert error_retryable is False
        partition_states[partition_key] = status

    monkeypatch.setattr(canonical_executors, "current_fenced_execution", current_execution)
    monkeypatch.setattr(canonical_executors, "_frozen_provider", reject_provider_resolution)
    monkeypatch.setattr(canonical_executors, "_prepare_operation_partitions", prepare_partitions)
    monkeypatch.setattr(canonical_executors, "_record_operation_partition", record_partition)
    claim = ExecutionClaim(
        run_id=uuid4(),
        dataset_code="fund.etf.profile.reported",
        fencing_token=3,
        target={
            "mode": "OBSERVATION_DATE",
            "observationDate": "2000-01-01",
            "selector": {
                "kind": "ETF",
                "operation": "MASTER",
                "scope": "ALL_VENUES",
                "venue": None,
                "etf": None,
            },
        },
        source_snapshot=[],
    )

    outcome = canonical_executors._execute_etf(
        claim,
        container=cast(ServiceContainer, object()),
        dataset_code="fund.etf.profile.reported",
    )

    assert outcome.status == "FAILED"
    assert outcome.completed_partitions == 0
    assert outcome.total_partitions == 2
    assert outcome.error == {
        "code": "etf-profile-current-snapshot-unrecoverable",
        "stage": "PROVIDER_FETCH",
        "retryable": False,
        "message": "ETF data source is unavailable",
    }
    assert partition_states == {"venue:SSE": "FAILED", "venue:SZSE": "FAILED"}


def test_etf_provider_pacing_and_retry_backoff_are_configurable_and_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """全集请求使用最小间隔，自动续跑退避按 attempt 指数增长且受最大值限制。"""
    monotonic_values = iter((10.0, 10.0, 10.1, 10.25))
    sleeps: list[float] = []

    def fake_monotonic() -> float:
        """返回确定性的单调时钟读数。"""
        return next(monotonic_values)

    def fake_sleep(seconds: float) -> None:
        """记录 pacing 与 backoff，不阻塞单元测试。"""
        sleeps.append(seconds)

    settings = SimpleNamespace(
        etf_provider_min_interval_seconds=0.25,
        etf_auto_retry_base_seconds=2.0,
        etf_auto_retry_max_seconds=5.0,
    )
    container = cast(ServiceContainer, SimpleNamespace(settings=settings))
    monkeypatch.setattr(canonical_executors, "monotonic", fake_monotonic)
    monkeypatch.setattr(canonical_executors, "sleep", fake_sleep)

    first = canonical_executors._pace_etf_provider(
        container,
        last_provider_call_at=None,
    )
    second = canonical_executors._pace_etf_provider(
        container,
        last_provider_call_at=first,
    )
    canonical_executors._wait_for_etf_auto_retry(container, attempt=4)

    assert first == 10.0
    assert second == 10.25
    assert sleeps == pytest.approx([0.15, 5.0])
