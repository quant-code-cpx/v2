"""显式上市生命周期状态机和发布 SQL 编排的单元测试。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from typing import cast
from uuid import uuid4

import pytest
from sqlalchemy import Engine

from service_data_sync.domain.equity import EquityIdentifier, Exchange
from service_data_sync.domain.equity_master import (
    EquityLifecycleEntry,
    EquityLifecycleEvidenceKind,
    EquityLifecycleStatus,
)
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.persistence.equity_lifecycle_repository import (
    EquityLifecycleTransitionError,
    SqlAlchemyEquityLifecycleRepository,
)


class FakeResult:
    """提供仓储执行路径所需的最小 SQLAlchemy 映射结果接口。"""

    def __init__(self, value: object) -> None:
        """保存一次 SQL 语句应返回的模拟值。"""
        self._value = value

    def mappings(self) -> FakeResult:
        """测试值已经是映射结构，无需额外转换。"""
        return self

    def scalar_one(self) -> object:
        """模拟 ORM-enabled `RETURNING` 的单一标量结果。"""
        if isinstance(self._value, dict):
            return self._value["source_batch_id"]
        return self._value

    def all(self) -> list[object]:
        """返回身份解析查询的全部候选行。"""
        assert isinstance(self._value, list)
        return self._value

    def one_or_none(self) -> object:
        """返回可选当前状态或发布行。"""
        return self._value

    def one(self) -> object:
        """返回 `RETURNING` 所需的单条来源观测结果。"""
        assert self._value is not None
        return self._value


class FakeConnection:
    """记录 SQL 文本，并按执行顺序供应必要查询结果。"""

    def __init__(self, responses: list[object]) -> None:
        """初始化共享的确定性结果队列。"""
        self._responses = responses
        self.statements: list[str] = []
        self.parameters: list[object] = []

    def execute(self, statement: object, parameters: object = None) -> FakeResult:
        """记录语句并消费一个预置结果，不连接真实 PostgreSQL。"""
        self.statements.append(str(statement))
        self.parameters.append(parameters)
        del parameters
        return FakeResult(self._responses.pop(0) if self._responses else None)


class FakeEngine:
    """暴露生命周期仓储事务所需的最小引擎表面。"""

    def __init__(self, responses: list[object]) -> None:
        """创建一条跨事务上下文复用的模拟连接。"""
        self.connection = FakeConnection(responses)

    @contextmanager
    def begin(self) -> Iterator[FakeConnection]:
        """提供不提交外部状态的事务上下文。"""
        yield self.connection


def test_repository_appends_explicit_delisting_and_advances_exchange_version() -> None:
    """明确退市可关闭 LISTED 有效期、追加 DELISTED 并原子切换交易所版本。"""
    previous_version = uuid4()
    engine = FakeEngine(
        [
            None,
            {"source_batch_id": uuid4()},
            None,
            [{"security_id": 8}],
            {
                "version_id": uuid4(),
                "status": "LISTED",
                "listed_on": date(2001, 8, 27),
                "delisted_on": None,
                "effective_from": date(2001, 8, 27),
                "effective_to": None,
                "evidence_kind": "CATALOG",
            },
            None,
            None,
            None,
            None,
            {"data_version": previous_version},
            None,
            None,
        ]
    )
    repository = _repository(engine)

    publication = repository.publish_lifecycle(
        exchange=Exchange.SSE,
        target_date=date(2026, 7, 2),
        entries=(_delisting_entry(),),
        provider_id="test-provider",
        source_payload_sha256="a" * 64,
        raw_uri="s3://test/raw.json",
        observed_at=datetime(2026, 7, 2, tzinfo=UTC),
        upstream_source="test-exchange",
        adapter_version="test-v1",
        schema_fingerprint="b" * 64,
    )

    assert publication.inserted_count == 1
    joined = "\n".join(engine.connection.statements)
    assert "pg_advisory_xact_lock" in joined
    assert "'LIFECYCLE'" in joined
    assert any(
        isinstance(parameters, dict) and parameters.get("status") == "DELISTED"
        for parameters in engine.connection.parameters
    )
    assert any(
        isinstance(parameters, dict) and parameters.get("evidence_kind") == "EXPLICIT_DELISTING"
        for parameters in engine.connection.parameters
    )


def test_repository_rejects_delisted_to_listed_without_official_correction() -> None:
    """退市是终态；没有更正审批时不得用普通 LISTED 事件恢复。"""
    engine = FakeEngine(
        [
            None,
            {"source_batch_id": uuid4()},
            None,
            [{"security_id": 8}],
            {
                "version_id": uuid4(),
                "status": "DELISTED",
                "listed_on": date(2001, 8, 27),
                "delisted_on": date(2026, 7, 1),
                "effective_from": date(2026, 7, 1),
                "effective_to": None,
                "evidence_kind": "EXPLICIT_DELISTING",
            },
        ]
    )
    repository = _repository(engine)
    entry = EquityLifecycleEntry(
        identifier=EquityIdentifier.parse("SSE.600519"),
        status=EquityLifecycleStatus.LISTED,
        effective_on=date(2026, 7, 2),
        evidence_kind=EquityLifecycleEvidenceKind.EXPLICIT_RESUMPTION,
    )

    with pytest.raises(EquityLifecycleTransitionError, match="not allowed"):
        repository.publish_lifecycle(
            exchange=Exchange.SSE,
            target_date=date(2026, 7, 2),
            entries=(entry,),
            provider_id="test-provider",
            source_payload_sha256="a" * 64,
            raw_uri="s3://test/raw.json",
            observed_at=datetime(2026, 7, 2, tzinfo=UTC),
            upstream_source="test-exchange",
            adapter_version="test-v1",
            schema_fingerprint="b" * 64,
        )


def test_repository_accepts_approved_official_correction_of_delisting() -> None:
    """已审批官方更正可关闭退市知识版本并重建同日 LISTED 事实。"""
    previous_version = uuid4()
    engine = FakeEngine(
        [
            None,
            {"source_batch_id": uuid4()},
            None,
            [{"security_id": 8}],
            {
                "version_id": uuid4(),
                "status": "DELISTED",
                "listed_on": date(2001, 8, 27),
                "delisted_on": date(2026, 7, 1),
                "effective_from": date(2026, 7, 1),
                "effective_to": None,
                "evidence_kind": "EXPLICIT_DELISTING",
            },
            None,
            None,
            None,
            None,
            {"data_version": previous_version},
            None,
            None,
        ]
    )
    repository = _repository(engine)
    entry = EquityLifecycleEntry(
        identifier=EquityIdentifier.parse("SSE.600519"),
        status=EquityLifecycleStatus.LISTED,
        effective_on=date(2026, 7, 1),
        evidence_kind=EquityLifecycleEvidenceKind.OFFICIAL_CORRECTION,
        listed_on=date(2001, 8, 27),
        correction_approval_reference="equity-master-approval-42",
    )

    publication = repository.publish_lifecycle(
        exchange=Exchange.SSE,
        target_date=date(2026, 7, 2),
        entries=(entry,),
        provider_id="test-provider",
        source_payload_sha256="a" * 64,
        raw_uri="s3://test/raw.json",
        observed_at=datetime(2026, 7, 2, tzinfo=UTC),
        upstream_source="test-exchange",
        adapter_version="test-v1",
        schema_fingerprint="b" * 64,
    )

    assert publication.inserted_count == 1
    assert any("SET known_to" in statement for statement in engine.connection.statements)
    assert any(
        isinstance(parameters, dict)
        and parameters.get("correction_approval_reference") == "equity-master-approval-42"
        for parameters in engine.connection.parameters
    )


def test_official_correction_requires_manual_approval_reference() -> None:
    """官方更正没有人工审批引用时不得进入持久化层。"""
    with pytest.raises(ValueError, match="manual approval"):
        EquityLifecycleEntry(
            identifier=EquityIdentifier.parse("SSE.600519"),
            status=EquityLifecycleStatus.LISTED,
            effective_on=date(2026, 7, 1),
            evidence_kind=EquityLifecycleEvidenceKind.OFFICIAL_CORRECTION,
            listed_on=date(2001, 8, 27),
        )


def _repository(engine: FakeEngine) -> SqlAlchemyEquityLifecycleRepository:
    """使用类型转换后的引擎替身构造生命周期仓储。"""
    return SqlAlchemyEquityLifecycleRepository(DatabaseClient(engine=cast(Engine, engine)))


def _delisting_entry() -> EquityLifecycleEntry:
    """构造一条字段语义已确认的明确终止上市事实。"""
    return EquityLifecycleEntry(
        identifier=EquityIdentifier.parse("SSE.600519"),
        status=EquityLifecycleStatus.DELISTED,
        effective_on=date(2026, 7, 1),
        evidence_kind=EquityLifecycleEvidenceKind.EXPLICIT_DELISTING,
        listed_on=date(2001, 8, 27),
        delisted_on=date(2026, 7, 1),
    )
