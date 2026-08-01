"""板块成分仓储的发布门、release 固定性与只读投影单元测试。"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, date, datetime
from typing import cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy.orm import Session
from sqlalchemy.sql import ClauseElement

from service_data_sync.application.ports.canonical_release import PublishedCanonicalRelease
from service_data_sync.application.ports.sector_market_data import StoredSector
from service_data_sync.domain.equity import Exchange
from service_data_sync.domain.sector import (
    SectorIdentifier,
    SectorMembershipCandidate,
    SectorScheme,
)
from service_data_sync.infrastructure.persistence import sector_membership_repository
from service_data_sync.infrastructure.persistence.sector_membership_repository import (
    SqlAlchemySectorMembershipRepository,
)


class FakeResult:
    """模拟 SQLAlchemy mapping result，按测试预置行返回所有读取形态。"""

    def __init__(self, rows: list[dict[str, object]]) -> None:
        """保存一条或多条字典形式数据库行。"""
        self._rows = rows

    def mappings(self) -> FakeResult:
        """返回当前结果自身，保持 SQLAlchemy 的映射读取链。"""
        return self

    def all(self) -> list[dict[str, object]]:
        """返回预置的所有行。"""
        return self._rows

    def one_or_none(self) -> dict[str, object] | None:
        """返回零或一行，测试数据不允许出现多行。"""
        assert len(self._rows) <= 1
        return self._rows[0] if self._rows else None

    def one(self) -> dict[str, object]:
        """返回唯一行，缺失时测试应立即失败。"""
        assert len(self._rows) == 1
        return self._rows[0]

    def scalar_one(self) -> object:
        """返回 ORM `RETURNING` 或单列查询要求的唯一标量。"""
        row = self.one()
        for key in ("run_id", "quality_status"):
            if key in row:
                return row[key]
        return row


class FakeConnection(AbstractContextManager["FakeConnection"]):
    """按调用顺序消费预置 SQL 结果，同时记录写入语句。"""

    def __init__(self, responses: list[FakeResult]) -> None:
        """保存待消费结果队列和执行记录。"""
        self._responses = responses
        self.executions: list[tuple[object, dict[str, object] | None]] = []

    def __enter__(self) -> FakeConnection:
        """进入同步连接上下文。"""
        return self

    def __exit__(self, *arguments: object) -> None:
        """退出同步连接上下文，不吞掉测试异常。"""
        del arguments

    def execute(self, statement: object, parameters: dict[str, object] | None = None) -> FakeResult:
        """记录 SQL，并按顺序返回预置结果或空写入结果。"""
        self.executions.append((statement, parameters))
        return self._responses.pop(0) if self._responses else FakeResult([])

    def scalar(self, statement: object) -> None:
        """记录查询当前 release 的标量读取；本伪连接默认不存在同 ID 领域 release。"""
        self.executions.append((statement, None))
        return None


class FakeDatabase:
    """以短生命周期 Session 接口模拟 `DatabaseClient`。"""

    def __init__(self, connection: FakeConnection) -> None:
        """保存所有测试调用共用的确定性连接。"""
        self._connection = connection

    def session(self) -> FakeConnection:
        """返回只读路径所需的模拟 Session。"""
        return self._connection

    def transaction(self) -> FakeConnection:
        """返回写入路径所需的模拟 Session。"""
        return self._connection


def test_repository_reads_only_rows_pinned_by_release_manifest() -> None:
    """所有读取都应从 release 关联快照投影，返回稳定排序和已确认身份字段。"""
    sector = _sector("BK0475", 1)
    release_id = uuid4()
    data_version = uuid4()
    instrument_id = uuid4()
    timestamp = datetime(2026, 7, 27, 10, tzinfo=UTC)
    connection = FakeConnection(
        [
            FakeResult([_sector_row(sector)]),
            FakeResult([_release_row(release_id, data_version, timestamp)]),
            FakeResult(
                [_sector_row(sector, snapshot_observed_at=timestamp, carried_forward=False)]
            ),
            FakeResult(
                [
                    {
                        "instrument_id": instrument_id,
                        "exchange": "SSE",
                        "symbol": "600000",
                        "name": "浦发银行",
                        "status": "LISTED",
                        "observed_from": timestamp,
                        "observed_to": None,
                    }
                ]
            ),
            FakeResult(
                [
                    {
                        "instrument_id": instrument_id,
                        "exchange": "SSE",
                        "symbol": "600000",
                        "name": "浦发银行",
                        "status": "LISTED",
                    }
                ]
            ),
            FakeResult(
                [
                    {
                        **_sector_row(sector),
                        "observed_from": timestamp,
                        "observed_to": None,
                        "snapshot_observed_at": timestamp,
                        "carried_forward": False,
                    }
                ]
            ),
        ]
    )
    repository = _repository(connection)

    sectors = repository.list_active_sectors(scheme=SectorScheme.EASTMONEY_INDUSTRY)
    release = repository.get_release(
        scheme=SectorScheme.EASTMONEY_INDUSTRY,
        as_of=None,
        data_version=data_version,
    )
    release_sector = repository.get_release_sector(
        release_id=release_id,
        identifier=sector.identifier,
    )
    constituents = repository.list_constituents(
        release_id=release_id,
        identifier=sector.identifier,
        after_exchange=None,
        after_symbol=None,
        limit=2,
    )
    equity = repository.resolve_equity_identity(
        exchange=Exchange.SSE,
        symbol="600000",
        identity_as_of=date(1999, 11, 10),
        known_at=timestamp,
    )
    memberships = repository.list_equity_memberships(
        release_id=release_id,
        instrument_id=instrument_id,
        after_sector_code=None,
        limit=2,
    )

    assert sectors == (sector,)
    assert release is not None and release.data_version == data_version
    assert release_sector == (sector, timestamp, False)
    assert constituents[0].instrument_id == instrument_id
    assert equity is not None and equity.exchange is Exchange.SSE
    assert memberships[0].sector.identifier == sector.identifier
    release_sql = str(connection.executions[1][0])
    identity_sql = str(connection.executions[4][0])
    assert "sector_membership_release.data_version =" in release_sql
    assert "equity_identifier_version.effective_from <=" in identity_sql
    assert "equity_identifier_version.known_from <=" in identity_sql
    assert "sector_membership_release.release_as_of" not in identity_sql


def test_publish_snapshot_advances_intervals_only_for_fully_verified_quality_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """完整 verified 快照才可写正式 item 并关闭缺席区间，warn 仍可作为完整观察发布。"""
    connection = FakeConnection([])
    repository = _repository(connection)
    sector = _sector("BK0475", 1)
    observed_at = datetime(2026, 7, 27, 10, tzinfo=UTC)
    calls: list[str] = []

    def record_observation(*arguments: object, **keywords: object) -> UUID:
        """绕开共享账本实现，返回可追踪 source batch 身份。"""
        del arguments, keywords
        return uuid4()

    def lock(*arguments: object, **keywords: object) -> None:
        """记录分区锁已在 snapshot 事务起点获取。"""
        del arguments, keywords
        calls.append("lock")

    def no_existing(*arguments: object, **keywords: object) -> None:
        """声明当前逻辑幂等键尚无既有 snapshot。"""
        del arguments, keywords
        return None

    def resolve(
        *arguments: object, **keywords: object
    ) -> tuple[list[tuple[int, SectorMembershipCandidate]], list[object], list[object]]:
        """返回一个唯一 confirmed 身份，且没有 pending/quarantine。"""
        del arguments, keywords
        return [(11, SectorMembershipCandidate("600000", "浦发银行"))], [], []

    def quality(*arguments: object, **keywords: object) -> list[tuple[str, str, str, int, int]]:
        """模拟可发布但需提醒的 churn 警告。"""
        del arguments, keywords
        return [("CHURN", "warn", "publish", 2, 10)]

    def inserted_snapshot(*arguments: object, **keywords: object) -> None:
        """记录快照头必须先于成员、区间写入。"""
        del arguments, keywords
        calls.append("snapshot")

    def ignored(*arguments: object, **keywords: object) -> None:
        """接受本例中为空的隔离或质量持久化调用。"""
        del arguments, keywords

    def partition(*arguments: object, **keywords: object) -> None:
        """记录正式 item 月分区已确保存在。"""
        del arguments, keywords
        calls.append("partition")

    def inserted_items(*arguments: object, **keywords: object) -> None:
        """记录只有完整快照才能写正式 verified 成员。"""
        del arguments, keywords
        calls.append("items")

    def advance(*arguments: object, **keywords: object) -> tuple[int, int]:
        """模拟新增一个开放关系并关闭一个旧关系。"""
        del arguments, keywords
        calls.append("interval")
        return 1, 1

    monkeypatch.setattr(
        sector_membership_repository, "record_source_observation", record_observation
    )
    monkeypatch.setattr(repository, "_lock_sector", lock)
    monkeypatch.setattr(repository, "_existing_snapshot", no_existing)
    monkeypatch.setattr(repository, "_resolve_candidates", resolve)
    monkeypatch.setattr(repository, "_quality_results", quality)
    monkeypatch.setattr(repository, "_insert_snapshot", inserted_snapshot)
    monkeypatch.setattr(repository, "_insert_pending", ignored)
    monkeypatch.setattr(repository, "_insert_quarantine", ignored)
    monkeypatch.setattr(repository, "_insert_quality_results", ignored)
    monkeypatch.setattr(repository, "_ensure_item_partition", partition)
    monkeypatch.setattr(repository, "_insert_items", inserted_items)
    monkeypatch.setattr(repository, "_advance_intervals", advance)

    result = repository.publish_snapshot(
        sector=sector,
        observation_date=date(2026, 7, 27),
        candidates=(SectorMembershipCandidate("600000", "浦发银行"),),
        provider_id="test",
        source_payload_sha256="a" * 64,
        raw_uri="s3://test/raw.json",
        observed_at=observed_at,
        upstream_source="test",
        adapter_version="test-v1",
        schema_fingerprint="b" * 64,
        run_id=uuid4(),
        partition_key="eastmoney.industry:BK0475:2026-07-27",
    )

    assert result.complete is True
    assert (result.inserted_interval_count, result.closed_interval_count) == (1, 1)
    assert calls == ["lock", "snapshot", "partition", "items", "interval"]


def test_start_run_rejects_an_unexpired_partition_lease() -> None:
    """同一 scheme 市场日存在有效租约时，第二个 worker 不能重置运行账本或重复抓取。"""
    sector = _sector("BK0475", 1)
    connection = FakeConnection(
        [
            FakeResult([]),
            FakeResult([{"partition_key": "eastmoney.industry:BK0475:2026-07-27"}]),
        ]
    )
    repository = _repository(connection)

    with pytest.raises(RuntimeError, match="already leased"):
        repository.start_run(
            scheme=SectorScheme.EASTMONEY_INDUSTRY,
            observation_date=date(2026, 7, 27),
            sectors=(sector,),
        )

    statements = "\n".join(str(statement) for statement, _ in connection.executions)
    assert "lease_until > :lease_until" in statements
    assert "INSERT INTO sync_run" not in statements


def test_publish_snapshot_quarantine_never_writes_items_or_intervals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """身份或质量 error 必须只留证据快照，不能因为坏响应关闭任何开放关系。"""
    connection = FakeConnection([])
    repository = _repository(connection)
    sector = _sector("BK0475", 1)
    written: list[str] = []

    def record_observation(*arguments: object, **keywords: object) -> UUID:
        """返回隔离快照关联的来源批次身份。"""
        del arguments, keywords
        return uuid4()

    def ignore(*arguments: object, **keywords: object) -> None:
        """接受无副作用的锁或 staging 写入调用。"""
        del arguments, keywords

    def no_existing(*arguments: object, **keywords: object) -> None:
        """声明该日期尚未建立可复用 snapshot。"""
        del arguments, keywords
        return None

    def resolve(
        *arguments: object, **keywords: object
    ) -> tuple[list[object], list[object], list[object]]:
        """模拟无法确认的来源成员，确保身份覆盖质量门触发。"""
        del arguments, keywords
        return (
            [],
            [
                (
                    1,
                    SectorMembershipCandidate("600000", "浦发银行"),
                    Exchange.SSE,
                    "IDENTITY_PENDING",
                )
            ],
            [],
        )

    def rejected(*arguments: object, **keywords: object) -> list[tuple[str, str, str, int, None]]:
        """返回阻断质量结论。"""
        del arguments, keywords
        return [("IDENTITY_COVERAGE", "error", "quarantine", 0, None)]

    def forbidden(*arguments: object, **keywords: object) -> None:
        """若调用正式 item 或区间路径则立即使测试失败。"""
        del arguments, keywords
        written.append("forbidden")
        raise AssertionError("bad snapshot must not write canonical membership")

    monkeypatch.setattr(
        sector_membership_repository, "record_source_observation", record_observation
    )
    monkeypatch.setattr(repository, "_lock_sector", ignore)
    monkeypatch.setattr(repository, "_existing_snapshot", no_existing)
    monkeypatch.setattr(repository, "_resolve_candidates", resolve)
    monkeypatch.setattr(repository, "_quality_results", rejected)
    monkeypatch.setattr(repository, "_insert_snapshot", ignore)
    monkeypatch.setattr(repository, "_insert_pending", ignore)
    monkeypatch.setattr(repository, "_insert_quarantine", ignore)
    monkeypatch.setattr(repository, "_insert_quality_results", ignore)
    monkeypatch.setattr(repository, "_ensure_item_partition", forbidden)
    monkeypatch.setattr(repository, "_insert_items", forbidden)
    monkeypatch.setattr(repository, "_advance_intervals", forbidden)

    result = repository.publish_snapshot(
        sector=sector,
        observation_date=date(2026, 7, 27),
        candidates=(SectorMembershipCandidate("600000", "浦发银行"),),
        provider_id="test",
        source_payload_sha256="a" * 64,
        raw_uri="s3://test/raw.json",
        observed_at=datetime(2026, 7, 27, 10, tzinfo=UTC),
        upstream_source="test",
        adapter_version="test-v1",
        schema_fingerprint="b" * 64,
        run_id=uuid4(),
        partition_key="eastmoney.industry:BK0475:2026-07-27",
    )

    assert result.complete is False
    assert result.pending_count == 1
    assert written == []


def test_publish_snapshot_reuses_existing_idempotency_partition_but_keeps_new_source_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一板块同日重跑必须返回原快照，不能重写 item 或重复推进观测区间。"""
    repository = _repository(FakeConnection([]))
    sector = _sector("BK0475", 1)
    snapshot_id = uuid4()
    observed_at = datetime(2026, 7, 27, 10, tzinfo=UTC)

    def record_observation(*arguments: object, **keywords: object) -> UUID:
        """返回新抓取证据的来源批次，证明 evidence 与逻辑快照可独立存在。"""
        del arguments, keywords
        return uuid4()

    def lock(*arguments: object, **keywords: object) -> None:
        """接受同一 sector 分区的事务锁。"""
        del arguments, keywords

    def existing(*arguments: object, **keywords: object) -> dict[str, object]:
        """返回既有完整 snapshot，模拟重复幂等请求。"""
        del arguments, keywords
        return {
            "snapshot_id": snapshot_id,
            "observed_at": observed_at,
            "status": "COMPLETE",
            "pending_count": 0,
            "quarantine_count": 0,
        }

    monkeypatch.setattr(
        sector_membership_repository, "record_source_observation", record_observation
    )
    monkeypatch.setattr(repository, "_lock_sector", lock)
    monkeypatch.setattr(repository, "_existing_snapshot", existing)

    result = repository.publish_snapshot(
        sector=sector,
        observation_date=date(2026, 7, 27),
        candidates=(SectorMembershipCandidate("600000", "浦发银行"),),
        provider_id="test",
        source_payload_sha256="a" * 64,
        raw_uri="s3://test/raw.json",
        observed_at=observed_at,
        upstream_source="test",
        adapter_version="test-v1",
        schema_fingerprint="b" * 64,
        run_id=uuid4(),
        partition_key="eastmoney.industry:BK0475:2026-07-27",
    )

    assert result.snapshot_id == snapshot_id
    assert result.complete is True
    assert result.inserted_interval_count == 0


def test_publish_snapshot_rejects_invalid_inputs_before_opening_transaction() -> None:
    """空集合、无时区时间、非 ACTIVE 板块和重复代码必须在任何 canonical 写入前失败。"""
    repository = _repository(FakeConnection([]))
    active = _sector("BK0475", 1)
    inactive = StoredSector(
        sector_key=active.sector_key,
        sector_id=active.sector_id,
        identifier=active.identifier,
        name=active.name,
        status="PENDING",
    )
    base = {
        "observation_date": date(2026, 7, 27),
        "provider_id": "test",
        "source_payload_sha256": "a" * 64,
        "raw_uri": "s3://test/raw.json",
        "observed_at": datetime(2026, 7, 27, 10, tzinfo=UTC),
        "upstream_source": "test",
        "adapter_version": "test-v1",
        "schema_fingerprint": "b" * 64,
        "run_id": uuid4(),
        "partition_key": "eastmoney.industry:BK0475:2026-07-27",
    }

    with pytest.raises(ValueError, match="must not be empty"):
        repository.publish_snapshot(sector=active, candidates=(), **base)
    with pytest.raises(ValueError, match="timezone"):
        repository.publish_snapshot(
            sector=active,
            candidates=(SectorMembershipCandidate("600000", "浦发银行"),),
            **{**base, "observed_at": datetime(2026, 7, 27, 10)},
        )
    with pytest.raises(ValueError, match="active sector"):
        repository.publish_snapshot(
            sector=inactive,
            candidates=(SectorMembershipCandidate("600000", "浦发银行"),),
            **base,
        )
    with pytest.raises(ValueError, match="unique symbols"):
        repository.publish_snapshot(
            sector=active,
            candidates=(
                SectorMembershipCandidate("600000", "浦发银行"),
                SectorMembershipCandidate("600000", "浦发银行"),
            ),
            **base,
        )


def test_publish_release_warns_for_warned_component_and_pins_manifest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """完整板块快照应原子形成 manifest；任一 warned 快照必须使 release 明示 warned。"""
    connection = FakeConnection([])
    repository = _repository(connection)
    first = _sector("BK0001", 1)
    second = _sector("BK0002", 2)
    first_snapshot = _snapshot_row(date(2026, 7, 27), "passed")
    second_snapshot = _snapshot_row(date(2026, 7, 27), "warned")
    published: list[tuple[SectorScheme, str]] = []
    timeline: list[str] = []

    def lock(*arguments: object, **keywords: object) -> None:
        """接受 scheme 级事务锁调用。"""
        del arguments, keywords

    def active(*arguments: object, **keywords: object) -> tuple[StoredSector, ...]:
        """返回 release reducer 事务内冻结的两个 ACTIVE 板块。"""
        del arguments, keywords
        return first, second

    def latest(*arguments: object, **keywords: object) -> dict[str, object]:
        """按板块键返回当日完整 snapshot，其中第二个携带 warn。"""
        del keywords
        assert len(arguments) == 2
        return first_snapshot if arguments[1] == first.sector_key else second_snapshot

    def no_current(*arguments: object, **keywords: object) -> None:
        """声明尚无 current release 可复用。"""
        del arguments, keywords
        return None

    def publish(*arguments: object, **keywords: object) -> PublishedCanonicalRelease:
        """模拟统一发布器在最终可见性前武装栅栏并返回真实 release 身份。"""
        del arguments
        before = cast(object, keywords["before_final_publication"])
        assert callable(before)
        before()
        timeline.append("publication")
        published.append((SectorScheme.EASTMONEY_INDUSTRY, str(keywords["quality_status"])))
        return PublishedCanonicalRelease(
            release_id=uuid4(),
            data_version=uuid4(),
            reused_release=False,
            reused_publication=False,
            published_at=cast(datetime, keywords["now"]),
        )

    def arm_terminal() -> None:
        """记录控制面终态只在 reducer 已通过全部质量门后武装。"""
        timeline.append("arm")

    monkeypatch.setattr(repository, "_lock_scheme", lock)
    monkeypatch.setattr(repository, "_active_sectors_on_connection", active)
    monkeypatch.setattr(repository, "_latest_complete_snapshot", latest)
    monkeypatch.setattr(repository, "_current_release", no_current)
    monkeypatch.setattr(sector_membership_repository, "publish_legacy_snapshot", publish)

    result = repository.publish_release(
        scheme=SectorScheme.EASTMONEY_INDUSTRY,
        observation_date=date(2026, 7, 27),
        before_final_publication=arm_terminal,
    )

    assert result is not None
    assert result.quality_status == "warned"
    assert result.fresh_sector_count == 2
    assert published == [(SectorScheme.EASTMONEY_INDUSTRY, "warned")]
    assert timeline == ["arm", "publication"]
    assert len(connection.executions) == 0


def test_publish_release_switches_domain_pointer_before_inserting_manifest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """新 canonical release 应先关闭旧领域指针，避免 current 唯一索引与 publication 脱节。"""
    connection = FakeConnection([])
    repository = _repository(connection)
    first = _sector("BK0001", 1)
    second = _sector("BK0002", 2)
    first_snapshot = _snapshot_row(date(2026, 7, 27), "passed")
    second_snapshot = _snapshot_row(date(2026, 7, 27), "passed")
    old_release_id = uuid4()
    new_release_id = uuid4()
    new_data_version = uuid4()

    def lock(*arguments: object, **keywords: object) -> None:
        """接受 scheme 级事务锁调用。"""
        del arguments, keywords

    def active(*arguments: object, **keywords: object) -> tuple[StoredSector, ...]:
        """返回 reducer 事务内冻结的两个 ACTIVE 板块。"""
        del arguments, keywords
        return first, second

    def latest(*arguments: object, **keywords: object) -> dict[str, object]:
        """按板块键返回完整 snapshot。"""
        del keywords
        assert len(arguments) == 2
        return first_snapshot if arguments[1] == first.sector_key else second_snapshot

    def current(*arguments: object, **keywords: object) -> dict[str, object]:
        """返回待被同事务替代的领域 current release。"""
        del arguments, keywords
        return _release_row(old_release_id, uuid4(), datetime(2026, 7, 27, 10, tzinfo=UTC))

    def publish(*arguments: object, **keywords: object) -> PublishedCanonicalRelease:
        """模拟统一发布器在新 publication 已落库后执行领域可见性回调。"""
        del arguments
        visibility = cast(
            Callable[[Session, UUID, UUID, UUID], None],
            keywords["write_visibility"],
        )
        visibility(
            _as_connection(connection),
            uuid4(),
            new_data_version,
            new_release_id,
        )
        return PublishedCanonicalRelease(
            release_id=new_release_id,
            data_version=new_data_version,
            reused_release=False,
            reused_publication=False,
            published_at=cast(datetime, keywords["now"]),
        )

    monkeypatch.setattr(repository, "_lock_scheme", lock)
    monkeypatch.setattr(repository, "_active_sectors_on_connection", active)
    monkeypatch.setattr(repository, "_latest_complete_snapshot", latest)
    monkeypatch.setattr(repository, "_current_release", current)
    monkeypatch.setattr(sector_membership_repository, "publish_legacy_snapshot", publish)

    result = repository.publish_release(
        scheme=SectorScheme.EASTMONEY_INDUSTRY,
        observation_date=date(2026, 7, 27),
    )

    assert result is not None
    statements = [str(statement) for statement, _parameters in connection.executions]
    old_pointer_update = next(
        index
        for index, statement in enumerate(statements)
        if statement.startswith("UPDATE sector_membership_release")
    )
    new_manifest_insert = next(
        index
        for index, statement in enumerate(statements)
        if statement.startswith("INSERT INTO sector_membership_release")
    )
    assert old_pointer_update < new_manifest_insert


def test_quality_results_warn_for_midrange_churn_and_do_not_block_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """10%–25% churn 是审计警告而非隔离，超过范围才应阻断正式观测。"""
    prior_snapshot = _snapshot_row(date(2026, 7, 26), "passed")
    connection = FakeConnection(
        [FakeResult([{"security_id": security_id} for security_id in range(1, 11)])]
    )
    repository = _repository(connection)

    def previous(*arguments: object, **keywords: object) -> dict[str, object]:
        """提供十只证券的前一完整快照。"""
        del arguments, keywords
        return prior_snapshot

    monkeypatch.setattr(repository, "_latest_complete_snapshot", previous)

    results = repository._quality_results(
        _as_connection(connection),
        sector_key=1,
        observation_date=date(2026, 7, 27),
        verified_security_ids={1, 2, 3, 4, 5, 6, 7, 8, 9, 11},
        has_unresolved=False,
    )

    assert results == [("CHURN", "warn", "publish", 2, 10)]


def test_quality_results_reject_unresolved_out_of_order_empty_and_extreme_snapshots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """身份不全、时间倒序、空基线和极端数量或 churn 必须全部进入隔离质量结果。"""
    repository = _repository(FakeConnection([]))
    observed_at = date(2026, 7, 27)

    unresolved = repository._quality_results(
        _as_connection(FakeConnection([])),
        sector_key=1,
        observation_date=observed_at,
        verified_security_ids={1},
        has_unresolved=True,
    )
    assert unresolved[0][0:3] == ("IDENTITY_COVERAGE", "error", "quarantine")

    def no_previous(*arguments: object, **keywords: object) -> None:
        """模拟首个完整快照前没有任何可比较基线。"""
        del arguments, keywords
        return None

    monkeypatch.setattr(repository, "_latest_complete_snapshot", no_previous)
    assert (
        repository._quality_results(
            _as_connection(FakeConnection([])),
            sector_key=1,
            observation_date=observed_at,
            verified_security_ids={1},
            has_unresolved=False,
        )
        == []
    )

    def current_day_previous(*arguments: object, **keywords: object) -> dict[str, object]:
        """模拟同日或迟到快照，要求在线差分拒绝它。"""
        del arguments, keywords
        return _snapshot_row(observed_at, "passed")

    monkeypatch.setattr(repository, "_latest_complete_snapshot", current_day_previous)
    out_of_order = repository._quality_results(
        _as_connection(FakeConnection([])),
        sector_key=1,
        observation_date=observed_at,
        verified_security_ids={1},
        has_unresolved=False,
    )
    assert out_of_order[0][0] == "OBSERVATION_ORDER"

    def prior_day_previous(*arguments: object, **keywords: object) -> dict[str, object]:
        """模拟正常前一日完整快照，供空基线和异常波动检查复用。"""
        del arguments, keywords
        return _snapshot_row(date(2026, 7, 26), "passed")

    monkeypatch.setattr(repository, "_latest_complete_snapshot", prior_day_previous)
    empty_baseline = repository._quality_results(
        _as_connection(FakeConnection([FakeResult([])])),
        sector_key=1,
        observation_date=observed_at,
        verified_security_ids={1},
        has_unresolved=False,
    )
    assert empty_baseline[0][0] == "PREVIOUS_SNAPSHOT_EMPTY"

    extreme = repository._quality_results(
        _as_connection(
            FakeConnection(
                [FakeResult([{"security_id": security_id} for security_id in range(1, 11)])]
            )
        ),
        sector_key=1,
        observation_date=observed_at,
        verified_security_ids={99},
        has_unresolved=False,
    )
    assert {result[0] for result in extreme} == {"COUNT_CHANGE", "CHURN"}


def test_repository_writes_staging_partition_items_and_observed_intervals() -> None:
    """写入辅助路径必须隔离 pending/quarantine，且只用完整快照时刻推进半开区间。"""
    snapshot_id = uuid4()
    connection = FakeConnection(
        [FakeResult([]) for _ in range(7)] + [FakeResult([{"security_id": 1}])]
    )
    repository = _repository(connection)
    observed_at = datetime(2026, 7, 27, 10, tzinfo=UTC)
    candidates = (
        SectorMembershipCandidate("600000", "浦发银行"),
        SectorMembershipCandidate("000001", "平安银行"),
    )

    repository._insert_pending(
        _as_connection(connection),
        snapshot_id=snapshot_id,
        rows=[(1, candidates[0], Exchange.SSE, "IDENTITY_PENDING")],
        now=observed_at,
    )
    repository._insert_quarantine(
        _as_connection(connection),
        snapshot_id=snapshot_id,
        rows=[(2, candidates[1], "IDENTITY_CONFLICT")],
        now=observed_at,
    )
    repository._insert_quality_results(
        _as_connection(connection),
        snapshot_id=snapshot_id,
        results=[("CHURN", "warn", "publish", 2, 10)],
        now=observed_at,
    )
    repository._ensure_item_partition(_as_connection(connection), date(2026, 7, 27))
    repository._insert_items(
        _as_connection(connection),
        snapshot_id=snapshot_id,
        snapshot_date=date(2026, 7, 27),
        verified=[(2, candidates[0]), (3, candidates[1])],
    )
    inserted, closed = repository._advance_intervals(
        _as_connection(connection),
        sector_key=1,
        snapshot_id=snapshot_id,
        observed_at=observed_at,
        security_ids={2, 3},
    )

    assert (inserted, closed) == (2, 1)
    assert len(connection.executions) == 11
    assert sector_membership_repository._infer_exchange("688001") is Exchange.SSE
    assert sector_membership_repository._infer_exchange("300001") is Exchange.SZSE
    assert sector_membership_repository._infer_exchange("830001") is Exchange.BSE
    assert sector_membership_repository._infer_exchange("100001") is None
    assert sector_membership_repository._candidate_hash(
        candidates
    ) == sector_membership_repository._candidate_hash(tuple(reversed(candidates)))


def test_release_match_and_dataset_pointer_publish_remain_versioned_and_atomic() -> None:
    """相同 manifest 只能复用同一 release；新数据版本必须成对替换通用发布指针。"""
    release_id = uuid4()
    snapshot_ids = tuple(sorted((uuid4(), uuid4())))
    data_version = uuid4()
    connection = FakeConnection(
        [
            FakeResult([{"snapshot_id": snapshot_id} for snapshot_id in snapshot_ids]),
            FakeResult([{"quality_status": "passed"}]),
            FakeResult([]),
            FakeResult([]),
        ]
    )
    repository = _repository(connection)

    assert repository._release_matches(
        _as_connection(connection),
        release_id,
        snapshot_ids,
        "passed",
    )

    repository._publish_dataset(
        _as_connection(connection),
        scheme=SectorScheme.EASTMONEY_INDUSTRY,
        data_version=data_version,
        quality_status="warned",
        effective_as_of=date(2026, 7, 27),
        published_at=datetime(2026, 7, 27, 11, tzinfo=UTC),
    )

    # release 血缘已由正式 release 记录保存；通用指针只原子 supersede 后插入新版本。
    assert len(connection.executions) == 4
    pointer_statements = tuple(str(statement) for statement, _parameters in connection.executions[-2:])
    assert "UPDATE dataset_publication" in pointer_statements[0]
    assert "INSERT INTO dataset_publication" in pointer_statements[1]
    insert_statement = cast(ClauseElement, connection.executions[-1][0])
    assert insert_statement.compile().params["data_version"] == data_version


def test_release_reducer_helpers_read_latest_complete_snapshot_and_current_manifest() -> None:
    """reducer 只可选择 COMPLETE 快照和未 supersede 的当前 manifest，不读取隔离头。"""
    snapshot = _snapshot_row(date(2026, 7, 27), "passed")
    current_release = {
        "release_id": uuid4(),
        "data_version": uuid4(),
        "quality_status": "passed",
        "fresh_sector_count": 1,
        "carried_forward_sector_count": 0,
        "published_at": datetime(2026, 7, 27, 11, tzinfo=UTC),
    }
    connection = FakeConnection([FakeResult([snapshot]), FakeResult([current_release])])
    repository = _repository(connection)

    assert repository._latest_complete_snapshot(_as_connection(connection), 1) == snapshot
    assert (
        repository._current_release(_as_connection(connection), SectorScheme.EASTMONEY_INDUSTRY)
        == current_release
    )


def test_release_reducer_freezes_active_sector_set_inside_transaction() -> None:
    """release reducer 必须在自己的事务内读取 ACTIVE 集合，不能复用任务开始后的过期目录。"""
    sector = _sector("BK0475", 1)
    connection = FakeConnection([FakeResult([_sector_row(sector)])])
    repository = _repository(connection)

    assert repository._active_sectors_on_connection(
        _as_connection(connection),
        SectorScheme.EASTMONEY_INDUSTRY,
    ) == (sector,)


def test_run_ledger_reclaims_partitions_and_records_checkpoint_failure_and_final_status() -> None:
    """成分任务应在 PostgreSQL 建立 run/lease，并将快照、失败和结束状态分别落入账本。"""
    first = _sector("BK0001", 1)
    second = _sector("BK0002", 2)
    run_id = uuid4()
    connection = FakeConnection([FakeResult([]), FakeResult([]), FakeResult([{"run_id": run_id}])])
    repository = _repository(connection)

    run = repository.start_run(
        scheme=SectorScheme.EASTMONEY_INDUSTRY,
        observation_date=date(2026, 7, 27),
        sectors=(first, second),
    )
    repository.mark_partition_completed(
        run=run,
        sector=first,
        publication=sector_membership_repository.PublishedSectorMembershipSnapshot(
            snapshot_id=uuid4(),
            observed_at=datetime(2026, 7, 27, 10, tzinfo=UTC),
            complete=True,
            inserted_interval_count=1,
            closed_interval_count=0,
            pending_count=0,
            quarantine_count=0,
        ),
    )
    repository.mark_partition_failed(
        run=run,
        sector=second,
        error_code="unavailable",
    )
    repository.finish_run(run=run, status="partial")

    assert run.run_id == run_id
    assert run.scheme is SectorScheme.EASTMONEY_INDUSTRY
    assert len(connection.executions) == 8
    with pytest.raises(ValueError, match="status is invalid"):
        repository.finish_run(run=run, status="running")


def _repository(connection: FakeConnection) -> SqlAlchemySectorMembershipRepository:
    """构造只替换数据库会话边界的仓储实例，避免连接真实 PostgreSQL。"""
    repository = object.__new__(SqlAlchemySectorMembershipRepository)
    repository._database = FakeDatabase(connection)  # type: ignore[assignment]
    repository._release_repository = object()  # type: ignore[assignment]
    return repository


def _as_connection(connection: FakeConnection) -> Session:
    """把仅实现测试所需协议的伪连接显式收窄为仓储内部 SQL 端口。"""
    return cast(Session, connection)


def _sector(code: str, key: int) -> StoredSector:
    """构造当前目录已发布的 ACTIVE 行业板块。"""
    return StoredSector(
        sector_key=key,
        sector_id=uuid4(),
        identifier=SectorIdentifier(SectorScheme.EASTMONEY_INDUSTRY, code),
        name="证券",
        status="ACTIVE",
    )


def _sector_row(
    sector: StoredSector,
    *,
    snapshot_observed_at: datetime | None = None,
    carried_forward: bool | None = None,
) -> dict[str, object]:
    """把端口板块渲染为仓储查询所需的字典数据库行。"""
    row: dict[str, object] = {
        "sector_key": sector.sector_key,
        "sector_id": sector.sector_id,
        "scheme": sector.identifier.scheme.value,
        "sector_code": sector.identifier.code,
        "name": sector.name,
        "status": sector.status,
    }
    if snapshot_observed_at is not None:
        row["snapshot_observed_at"] = snapshot_observed_at
    if carried_forward is not None:
        row["carried_forward"] = carried_forward
    return row


def _release_row(release_id: UUID, data_version: UUID, timestamp: datetime) -> dict[str, object]:
    """构造当前 immutable release 头数据库行。"""
    return {
        "release_id": release_id,
        "scheme": "eastmoney.industry",
        "release_as_of": timestamp,
        "coverage_start": timestamp,
        "data_version": data_version,
        "quality_status": "passed",
        "carried_forward_sector_count": 0,
        "published_at": timestamp,
    }


def _snapshot_row(observation_date: date, quality_status: str) -> dict[str, object]:
    """构造可供 reducer 选择的完整 snapshot 行。"""
    return {
        "snapshot_id": uuid4(),
        "observed_at": datetime(2026, 7, 27, 10, tzinfo=UTC),
        "observation_date": observation_date,
        "member_count": 10,
        "quality_status": quality_status,
        "source_batch_id": uuid4(),
        "content_sha256": bytes.fromhex("a" * 64),
    }
