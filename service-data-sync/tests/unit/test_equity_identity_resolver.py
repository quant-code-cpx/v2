"""证券标识双时间解析器的单元测试。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from typing import cast

import pytest
from sqlalchemy import Connection, Engine

from service_data_sync.domain.equity import Exchange
from service_data_sync.domain.equity_master import EquityIdentityResolutionStatus
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.persistence.equity_identity_resolver import (
    SqlAlchemyEquityIdentityResolver,
    resolve_identity_on_connection,
)


class FakeResult:
    """提供解析器所需的 SQLAlchemy 映射结果读取面。"""

    def __init__(self, rows: list[dict[str, object]]) -> None:
        """保存一次 SELECT 将返回的映射行列表。"""
        self._rows = rows

    def mappings(self) -> FakeResult:
        """映射行已经准备好，无需额外转换。"""
        return self

    def all(self) -> list[dict[str, object]]:
        """返回当前查询的全部候选身份。"""
        return self._rows


class FakeConnection:
    """按顺序返回查询行，并记录 SQL 以验证不读取身份锚当前列。"""

    def __init__(self, responses: list[list[dict[str, object]]]) -> None:
        """初始化一次或多次双时间查询的确定性响应队列。"""
        self._responses = responses
        self.statements: list[str] = []

    def execute(self, statement: object, parameters: object = None) -> FakeResult:
        """记录 SQL 文本并返回下一批候选行。"""
        self.statements.append(str(statement))
        del parameters
        return FakeResult(self._responses.pop(0))


class FakeEngine:
    """暴露解析器只读连接所需的最小引擎接口。"""

    def __init__(self, responses: list[list[dict[str, object]]]) -> None:
        """创建由 connect 上下文复用的假连接。"""
        self.connection = FakeConnection(responses)

    @contextmanager
    def connect(self) -> Iterator[FakeConnection]:
        """产出不触及外部数据库的只读连接替身。"""
        yield self.connection


def test_resolver_returns_resolved_not_found_and_conflict_without_fallback() -> None:
    """标识历史只能产生确定的三类结果，多个候选绝不按任意顺序选取。"""
    engine = FakeEngine(
        [
            [{"security_id": 8, "identity_state": "CONFIRMED"}],
            [],
            [
                {"security_id": 8, "identity_state": "CONFIRMED"},
                {"security_id": 9, "identity_state": "CONFIRMED"},
            ],
        ]
    )
    known_at = datetime(2026, 7, 27, tzinfo=UTC)

    resolved = resolve_identity_on_connection(
        cast(Connection, engine.connection),
        exchange=Exchange.SSE,
        symbol="600519",
        fact_date=date(2026, 7, 26),
        known_at=known_at,
    )
    not_found = resolve_identity_on_connection(
        cast(Connection, engine.connection),
        exchange=Exchange.SSE,
        symbol="600520",
        fact_date=date(2026, 7, 26),
        known_at=known_at,
    )
    conflict = resolve_identity_on_connection(
        cast(Connection, engine.connection),
        exchange=Exchange.SSE,
        symbol="600521",
        fact_date=date(2026, 7, 26),
        known_at=known_at,
    )

    assert resolved.status is EquityIdentityResolutionStatus.RESOLVED
    assert resolved.security_id == 8
    assert not_found.status is EquityIdentityResolutionStatus.NOT_FOUND
    assert conflict.status is EquityIdentityResolutionStatus.CONFLICT
    assert "effective_range @> :fact_date" in engine.connection.statements[0]
    assert "FROM equity_instrument" not in engine.connection.statements[0]


def test_resolver_requires_timezone_and_filters_pending_from_current_open_read() -> None:
    """历史写入必须冻结知识时刻，而当前公开读取不得返回 PENDING 占位。"""
    engine = FakeEngine([[]])
    resolver = SqlAlchemyEquityIdentityResolver(DatabaseClient(engine=cast(Engine, engine)))

    with pytest.raises(ValueError, match="known_at"):
        resolve_identity_on_connection(
            cast(Connection, engine.connection),
            exchange=Exchange.SSE,
            symbol="600519",
            fact_date=date(2026, 7, 26),
            known_at=datetime(2026, 7, 27),
        )

    current = resolver.resolve_current_open(exchange=Exchange.SSE, symbol="600519")

    assert current.status is EquityIdentityResolutionStatus.NOT_FOUND
    assert "identity_state = 'CONFIRMED'" in engine.connection.statements[0]
