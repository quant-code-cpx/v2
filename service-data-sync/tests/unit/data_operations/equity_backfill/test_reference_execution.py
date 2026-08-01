"""股票参考数据回填执行器的事件 checkpoint 路由回归测试。"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from typing import cast
from uuid import UUID, uuid4

import pytest

from service_data_sync.bootstrap.container import ServiceContainer
from service_data_sync.infrastructure.data_operations import canonical_executors
from service_data_sync.infrastructure.data_operations.control_plane import ExecutionClaim


class _RecordingExecution:
    """记录执行器写入 source batch 的最小 fenced execution 替身。"""

    def __init__(self) -> None:
        """初始化空的来源批次集合，模拟每个分区完成后新增血缘。"""
        self.source_batch_ids: list[UUID] = []


def _claim(dataset_code: str) -> ExecutionClaim:
    """构造一条含冻结日期窗口的真实股票回填 claim。"""
    return ExecutionClaim(
        run_id=uuid4(),
        dataset_code=dataset_code,
        fencing_token=7,
        target={
            "mode": "FULL",
            "selector": {"kind": "INSTRUMENT", "exchange": "SSE", "symbol": "600000"},
        },
        source_snapshot=[],
        execution_intent={
            "kind": "EQUITY_BACKFILL",
            "backfillDateFrom": "2026-07-01",
            "backfillDateTo": "2026-07-31",
        },
    )


def _container() -> ServiceContainer:
    """构造执行器依赖的最小容器，不连接真实数据库或对象存储。"""
    return cast(ServiceContainer, SimpleNamespace(database=object(), object_storage=object()))


def _install_reference_execution_doubles(
    monkeypatch: pytest.MonkeyPatch,
    *,
    execution: _RecordingExecution,
    source_batch_id: UUID,
    data_version: UUID,
    sync_calls: list[dict[str, object]],
) -> None:
    """安装参考数据执行所需替身，保留 checkpoint 路由由真实函数决定。"""

    class _Repository:
        """占位 canonical 仓储；身份证券由专门替身返回。"""

        def __init__(self, _database: object) -> None:
            """接收真实构造器传入的数据库客户端但不执行访问。"""

    def _identifiers(_selector: object, _repository: object) -> tuple[object, ...]:
        """返回一个稳定证券，避免测试覆盖全市场枚举逻辑。"""
        return (object(),)

    def _raw_store(_object_storage: object) -> object:
        """返回无副作用 raw store 占位，真实同步已由下方替身隔离。"""
        return object()

    def _not_cancelled(_container: object) -> bool:
        """保持执行可继续，以便覆盖正常完成与 checkpoint 收尾。"""
        return False

    def _current_execution() -> _RecordingExecution:
        """向真实执行器提供本测试唯一的 fenced execution 替身。"""
        return execution

    def _sync_reference(**arguments: object) -> tuple[int, int, UUID]:
        """记录真实执行器传入的分区语义，并模拟同步后新增 source batch。"""
        sync_calls.append(
            {
                "capability": arguments["capability"],
                "start": arguments["start"],
                "end": arguments["end"],
                "finalWrite": arguments["final_write"],
            }
        )
        execution.source_batch_ids.append(source_batch_id)
        return 2, 3, data_version

    monkeypatch.setattr(canonical_executors, "SqlAlchemyEquityMarketDataRepository", _Repository)
    monkeypatch.setattr(canonical_executors, "_equity_identifiers", _identifiers)
    monkeypatch.setattr(canonical_executors, "S3RawPayloadStore", _raw_store)
    monkeypatch.setattr(canonical_executors, "_required_execution", _current_execution)
    monkeypatch.setattr(canonical_executors, "_cancel_requested", _not_cancelled)
    monkeypatch.setattr(canonical_executors, "_sync_equity_reference", _sync_reference)


@pytest.mark.parametrize("capability", ("equity.profile", "equity.adjustment_factor"))
def test_non_event_reference_backfill_never_constructs_event_checkpoint_keys(
    monkeypatch: pytest.MonkeyPatch,
    capability: str,
) -> None:
    """公司概况和复权因子回填不得访问公司行动专属 checkpoint 族。"""
    execution = _RecordingExecution()
    sync_calls: list[dict[str, object]] = []
    _install_reference_execution_doubles(
        monkeypatch,
        execution=execution,
        source_batch_id=uuid4(),
        data_version=uuid4(),
        sync_calls=sync_calls,
    )

    def _unexpected_event_checkpoint(*_arguments: object, **_keywords: object) -> object:
        """任何事件 checkpoint 调用都说明非事件 capability 被错误路由。"""
        raise AssertionError("non-event reference backfill must not use event checkpoints")

    monkeypatch.setattr(
        canonical_executors,
        "equity_backfill_event_partition_keys",
        _unexpected_event_checkpoint,
    )
    monkeypatch.setattr(
        canonical_executors,
        "completed_equity_event_partitions",
        _unexpected_event_checkpoint,
    )
    monkeypatch.setattr(
        canonical_executors,
        "record_equity_event_partitions",
        _unexpected_event_checkpoint,
    )
    monkeypatch.setattr(
        canonical_executors,
        "finalize_equity_event_partitions",
        _unexpected_event_checkpoint,
    )

    outcome = canonical_executors._execute_equity_reference(
        _claim(capability),
        container=_container(),
        capability=capability,
    )

    assert outcome.status == "SUCCEEDED"
    assert outcome.completed_partitions == outcome.total_partitions == 1
    assert outcome.processed_records == 5
    assert outcome.checkpoint_kind == "data-version"
    assert len(sync_calls) == 1
    assert sync_calls[0]["capability"] == capability
    assert sync_calls[0]["finalWrite"] is True


def test_corporate_action_backfill_keeps_event_checkpoint_roster_and_finalizer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """公司行动回填仍须逐窗口记录事件 coverage，并以原顺序完成封印。"""
    execution = _RecordingExecution()
    source_batch_id = uuid4()
    data_version = uuid4()
    sync_calls: list[dict[str, object]] = []
    _install_reference_execution_doubles(
        monkeypatch,
        execution=execution,
        source_batch_id=source_batch_id,
        data_version=data_version,
        sync_calls=sync_calls,
    )
    event_key = "equity.corporate_action:CORPORATE_ACTION:2026-07-01:2026-07-31"
    captured: dict[str, object] = {"eventKeyCalls": [], "records": []}

    def _event_keys(
        *,
        dataset_code: str,
        window_from: date,
        window_to: date,
    ) -> tuple[str, ...]:
        """返回固定公司行动窗口键，并记录真实执行器请求的参数。"""
        event_key_calls = captured["eventKeyCalls"]
        assert isinstance(event_key_calls, list)
        event_key_calls.append((dataset_code, window_from, window_to))
        return (event_key,)

    def _completed_partitions(
        _database: object,
        *,
        claim: ExecutionClaim,
        expected_partition_keys: frozenset[str],
    ) -> frozenset[str]:
        """模拟没有已完成分区，并验证 roster 使用公司行动数据集。"""
        assert claim.dataset_code == "equity.corporate_action"
        assert expected_partition_keys == frozenset({event_key})
        return frozenset()

    def _record_partitions(_database: object, **arguments: object) -> None:
        """记录窗口 checkpoint 入参，确认新 source batch 被归入本窗口。"""
        records = captured["records"]
        assert isinstance(records, list)
        records.append(arguments)

    def _finalize_partitions(_database: object, **arguments: object) -> None:
        """保存最终 roster，确认封印仍使用事件窗口键。"""
        captured["finalizer"] = arguments

    monkeypatch.setattr(canonical_executors, "equity_backfill_event_partition_keys", _event_keys)
    monkeypatch.setattr(
        canonical_executors,
        "completed_equity_event_partitions",
        _completed_partitions,
    )
    monkeypatch.setattr(canonical_executors, "record_equity_event_partitions", _record_partitions)
    monkeypatch.setattr(
        canonical_executors,
        "finalize_equity_event_partitions",
        _finalize_partitions,
    )

    outcome = canonical_executors._execute_equity_reference(
        _claim("equity.corporate_action"),
        container=_container(),
        capability="equity.corporate_action",
    )

    assert outcome.status == "SUCCEEDED"
    assert outcome.completed_partitions == outcome.total_partitions == 1
    assert outcome.processed_records == 5
    assert sync_calls == [
        {
            "capability": "equity.corporate_action",
            "start": date(2026, 7, 1),
            "end": date(2026, 7, 31),
            "finalWrite": False,
        }
    ]
    assert captured["eventKeyCalls"] == [
        ("equity.corporate_action", date(2026, 7, 1), date(2026, 7, 31)),
        ("equity.corporate_action", date(2026, 7, 1), date(2026, 7, 31)),
    ]
    records = captured["records"]
    assert isinstance(records, list)
    assert len(records) == 1
    assert records[0]["source_batch_ids"] == (source_batch_id,)
    finalizer = captured["finalizer"]
    assert isinstance(finalizer, dict)
    assert finalizer["ordered_partition_keys"] == (event_key,)
