"""证券目录快照、确认身份与发布版本 SQL 编排的单元测试。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from typing import cast
from uuid import uuid4

import pytest

from service_data_sync.domain.equity import EquityIdentifier, Exchange
from service_data_sync.domain.equity_master import (
    EquityCatalogCompletenessError,
    EquityCatalogEntry,
)
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.persistence.equity_master_repository import (
    SqlAlchemyEquityMasterRepository,
    _catalog_business_hash,
    _effective_date,
)


class FakeResult:
    """在不连接 PostgreSQL 时模拟仓储所需的 SQLAlchemy 映射结果。"""

    def __init__(self, value: object) -> None:
        """保存按 SQL 执行顺序排队的可选或必需映射行。"""
        self._value = value

    def mappings(self) -> FakeResult:
        """已排队值是映射形数据，因此保持当前替身。"""
        return self

    def scalar_one(self) -> object:
        """模拟 ORM-enabled `RETURNING` 的单一标量结果。"""
        if isinstance(self._value, dict):
            for key in ("source_batch_id", "security_id", "data_version"):
                if key in self._value:
                    return self._value[key]
        return self._value

    def scalar_one_or_none(self) -> object | None:
        """模拟可无结果的 ORM 标量查询。"""
        if self._value is None:
            return None
        return self.scalar_one()

    def one_or_none(self) -> object:
        """为可选查询返回排队行或 `None`。"""
        return self._value

    def one(self) -> object:
        """为 INSERT RETURNING 返回必需映射行。"""
        assert self._value is not None
        return self._value

    def all(self) -> list[object]:
        """为聚合发布查询返回确定性组件行。"""
        assert isinstance(self._value, list)
        return self._value


class FakeConnection:
    """记录每条 SQL 文本，并按顺序回放必要的查询响应。"""

    def __init__(self, responses: list[object]) -> None:
        """初始化由事务上下文共享的确定性响应队列。"""
        self._responses = responses
        self.statements: list[str] = []

    def execute(self, statement: object, parameters: object = None) -> FakeResult:
        """记录语句并消费一个可选响应；不验证 SQLAlchemy 参数细节。"""
        self.statements.append(str(statement))
        del parameters
        return FakeResult(self._responses.pop(0) if self._responses else None)


class FakeEngine:
    """提供本仓储事务写入需要的最小 SQLAlchemy 引擎接口。"""

    def __init__(self, responses: list[object]) -> None:
        """创建共享替身连接，使单个事务内调用顺序可被断言。"""
        self.connection = FakeConnection(responses)

    @contextmanager
    def begin(self) -> Iterator[FakeConnection]:
        """产出不执行真实提交的事务形连接。"""
        yield self.connection


class FakeDatabase:
    """以短生命周期 Session 接口模拟 `DatabaseClient`。"""

    def __init__(self, engine: FakeEngine) -> None:
        """保存承载测试响应队列的连接替身。"""
        self._engine = engine

    @contextmanager
    def transaction(self) -> Iterator[FakeConnection]:
        """提供一次原子写入使用的模拟 Session。"""
        with self._engine.begin() as connection:
            yield connection


def test_repository_creates_confirmed_identity_snapshot_and_exchange_publication() -> None:
    """首次目录发现必须同时写确认身份、名称、LISTED 与可审计快照成员。"""
    engine = FakeEngine([None, {"source_batch_id": uuid4()}, None, None, {"security_id": 8}])
    repository = _repository(engine)

    publication = _publish(repository, _entry())

    assert publication.inserted_count == 1
    assert publication.unchanged_count == 0
    joined = "\n".join(engine.connection.statements)
    assert "identity_state" in joined
    assert "listing_status" in joined
    assert "equity_master_snapshot_member" in joined
    assert "INSERT INTO sync_run" in joined


def test_repository_confirms_pending_anchor_without_creating_a_second_security() -> None:
    """行情先到的 PENDING 锚必须关闭旧知识版本并复用其 security_id。"""
    engine = FakeEngine(
        [
            None,
            {"source_batch_id": uuid4()},
            None,
            {"security_id": 8, "identity_state": "PENDING"},
        ]
    )
    repository = _repository(engine)

    publication = _publish(repository, _entry())

    assert publication.inserted_count == 1
    joined = "\n".join(engine.connection.statements)
    assert "SET known_to" in joined
    assert "INSERT INTO equity_instrument" not in joined
    assert "current_master_version" in joined


def test_repository_keeps_data_version_when_catalog_business_content_is_unchanged() -> None:
    """相同目录仍保留独立来源和快照，但不得制造新的 dataVersion。"""
    entry = _entry()
    current_version = uuid4()
    engine = FakeEngine(
        [
            {"business_sha256": _catalog_business_hash((entry,))},
            {"source_batch_id": uuid4()},
            None,
            {"security_id": 8, "identity_state": "CONFIRMED"},
            {"name": entry.name, "effective_from": date(2001, 8, 27)},
            None,
            {"data_version": current_version},
        ]
    )
    repository = _repository(engine)

    publication = _publish(repository, entry)

    assert publication.data_version == current_version
    assert publication.inserted_count == 0
    assert publication.unchanged_count == 1
    joined = "\n".join(engine.connection.statements)
    assert "INSERT INTO equity_master_snapshot" in joined
    assert "UPDATE dataset_publication SET superseded_at" not in joined


def test_repository_rejects_catalog_drop_larger_than_one_percent_before_writes() -> None:
    """目录行数相对稳定基线下降超过百分之一时，不能写入新来源或发布版本。"""
    engine = FakeEngine([{"business_sha256": bytes(32), "row_count": 101}])
    repository = _repository(engine)

    with pytest.raises(EquityCatalogCompletenessError, match="more than one percent"):
        _publish(repository, _entry())

    assert "INSERT INTO source_batch" not in "\n".join(engine.connection.statements)


def test_repository_records_presence_anomalies_without_mutating_listing_status() -> None:
    """目录缺席只能建立缺席观测；没有显式证据时不得将证券改为退市。"""
    engine = FakeEngine([None, {"source_batch_id": uuid4()}, None, None, {"security_id": 8}])
    repository = _repository(engine)

    _publish(repository, _entry())

    joined = "\n".join(engine.connection.statements)
    assert "INSERT INTO equity_presence_anomaly" in joined
    assert "UPDATE equity_presence_anomaly SET" in joined
    assert "DELISTED" not in joined


def test_repository_appends_observation_date_name_version_on_rename() -> None:
    """目录名称变化从本次观察日开始，不会覆盖已有历史名称。"""
    entry = EquityCatalogEntry(
        identifier=EquityIdentifier.parse("SSE.600519"), name="贵州茅台股份", listed_on=None
    )
    engine = FakeEngine(
        [
            None,
            {"source_batch_id": uuid4()},
            None,
            {"security_id": 8, "identity_state": "CONFIRMED"},
            {"version_id": uuid4(), "name": "贵州茅台", "effective_from": date(2026, 7, 26)},
        ]
    )
    repository = _repository(engine)

    publication = _publish(repository, entry)

    assert publication.inserted_count == 1
    joined = "\n".join(engine.connection.statements)
    assert "SET effective_to" in joined
    assert _effective_date(entry, date(2026, 7, 27))[1] == "OBSERVATION_DATE"


def test_repository_publishes_and_reuses_stable_cn_a_aggregate() -> None:
    """三所 child version 完整时原子聚合；组件未变时必须复用原 dataVersion。"""
    effective_as_of = date(2026, 7, 27)
    cutoff = datetime(2026, 7, 27, tzinfo=UTC)
    child_rows = [
        {
            "partition_key": exchange,
            "data_version": uuid4(),
            "effective_as_of": effective_as_of,
            "knowledge_cutoff": cutoff,
        }
        for exchange in ("BSE", "SSE", "SZSE")
    ]
    engine = FakeEngine([child_rows, []])
    repository = _repository(engine)

    publication = repository.publish_cn_a_aggregate()

    assert publication.data_version
    joined = "\n".join(engine.connection.statements)
    assert "dataset_publication_component" in joined
    assert joined.count("INSERT INTO dataset_publication_component") == 3

    existing_version = uuid4()
    engine = FakeEngine(
        [
            child_rows,
            [
                {
                    "component_partition_key": row["partition_key"],
                    "component_data_version": row["data_version"],
                }
                for row in child_rows
            ],
            {"data_version": existing_version, "published_at": cutoff},
        ]
    )
    repository = _repository(engine)

    unchanged = repository.publish_cn_a_aggregate()

    assert unchanged.data_version == existing_version
    assert len(engine.connection.statements) == 3


def test_repository_refuses_mixed_target_dates_for_cn_a_aggregate() -> None:
    """三所 child publication 目标日不一致时，不得伪造全市场稳定版本。"""
    cutoff = datetime(2026, 7, 27, tzinfo=UTC)
    engine = FakeEngine(
        [
            [
                {
                    "partition_key": "BSE",
                    "data_version": uuid4(),
                    "effective_as_of": date(2026, 7, 26),
                    "knowledge_cutoff": cutoff,
                },
                {
                    "partition_key": "SSE",
                    "data_version": uuid4(),
                    "effective_as_of": date(2026, 7, 27),
                    "knowledge_cutoff": cutoff,
                },
                {
                    "partition_key": "SZSE",
                    "data_version": uuid4(),
                    "effective_as_of": date(2026, 7, 27),
                    "knowledge_cutoff": cutoff,
                },
            ]
        ]
    )
    repository = _repository(engine)

    with pytest.raises(ValueError, match="share a target date"):
        repository.publish_cn_a_aggregate()


def _repository(engine: FakeEngine) -> SqlAlchemyEquityMasterRepository:
    """围绕带短 Session 边界的替身数据库构造仓储。"""
    return SqlAlchemyEquityMasterRepository(cast(DatabaseClient, FakeDatabase(engine)))


def _entry() -> EquityCatalogEntry:
    """构造带可靠上市日的确认目录条目。"""
    return EquityCatalogEntry(
        identifier=EquityIdentifier.parse("SSE.600519"),
        name="贵州茅台",
        listed_on=date(2001, 8, 27),
    )


def _publish(repository: SqlAlchemyEquityMasterRepository, entry: EquityCatalogEntry):
    """用稳定来源元数据发布单行目录，避免每个测试重复样板。"""
    return repository.publish_catalog(
        exchange=Exchange.SSE,
        target_date=date(2026, 7, 27),
        entries=(entry,),
        provider_id="test-provider",
        source_payload_sha256="a" * 64,
        raw_uri="s3://test/raw.json",
        observed_at=datetime(2026, 7, 27, tzinfo=UTC),
        upstream_source="test-upstream",
        adapter_version="test-v1",
        schema_fingerprint="b" * 64,
    )
