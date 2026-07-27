"""标准日线修订与发布 SQL 编排的单元测试。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine

from service_data_sync.domain.equity import EquityDailyBar, EquityIdentifier
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.persistence.equity_market_data_repository import (
    PossibleCodeReuseError,
    SqlAlchemyEquityMarketDataRepository,
    _bar_content_hash,
)


class FakeResult:
    """在不使用真实数据库时模拟仓储所需的 SQLAlchemy 映射结果。"""

    def __init__(self, value: object) -> None:
        """保存一个排队的单行、可选行或多行响应。"""
        self._value = value

    def mappings(self) -> FakeResult:
        """排队值已是映射形字典，因此返回当前替身。"""
        return self

    def one_or_none(self) -> object:
        """为允许无匹配的 SELECT 返回可选排队行。"""
        return self._value

    def one(self) -> object:
        """为 `INSERT ... RETURNING` 返回必需的排队行。"""
        assert self._value is not None
        return self._value

    def all(self) -> list[object]:
        """为有界目录和日线查询返回排队行列表。"""
        assert isinstance(self._value, list)
        return self._value


class FakeConnection:
    """按顺序回放 SQL 响应，并记录语句供断言使用。"""

    def __init__(self, responses: list[object]) -> None:
        """初始化由 `begin`/`connect` 上下文共享的确定性响应队列。"""
        self._responses = responses
        self.statements: list[str] = []

    def execute(self, statement: object, parameters: object = None) -> FakeResult:
        """记录 SQL 文本；调用方读取结果时才消费一项响应。"""
        self.statements.append(str(statement))
        del parameters
        response = self._responses.pop(0) if self._responses else None
        return FakeResult(response)


class FakeEngine:
    """暴露兼容 SQLAlchemy 引擎接口的事务与读取上下文。"""

    def __init__(self, responses: list[object]) -> None:
        """创建共享替身连接，保证跨调用的响应顺序确定。"""
        self.connection = FakeConnection(responses)

    @contextmanager
    def begin(self) -> Iterator[FakeConnection]:
        """产出事务形连接，但不提交外部状态。"""
        yield self.connection

    @contextmanager
    def connect(self) -> Iterator[FakeConnection]:
        """产出读取形连接，但不打开真实套接字。"""
        yield self.connection


def test_repository_appends_changed_bar_and_advances_publication() -> None:
    """在一个事务中创建首个修订、来源血缘和当前发布。"""
    instrument_id = uuid4()
    source_batch_id = uuid4()
    engine = FakeEngine(
        [
            {"source_batch_id": source_batch_id},
            [{"security_id": 1, "identity_state": "CONFIRMED"}],
            None,
            _instrument_row(instrument_id),
            None,
            None,
            None,
        ]
    )
    repository = _repository(engine)

    publication = repository.publish_daily_bars(
        identifier=EquityIdentifier.parse("SSE.600519"),
        bars=(_bar(),),
        provider_id="test-provider",
        source_payload_sha256="a" * 64,
        raw_uri="s3://test/raw.json",
        observed_at=datetime(2026, 7, 1, tzinfo=UTC),
    )

    assert publication.instrument.instrument_id == instrument_id
    assert publication.inserted_count == 1
    assert publication.unchanged_count == 0
    assert len(engine.connection.statements) == 8
    assert (
        "ON CONFLICT (provider_id, capability, payload_sha256)"
        not in engine.connection.statements[0]
    )


def test_repository_keeps_current_publication_when_all_bars_are_unchanged() -> None:
    """标准日线业务字段完全相同时，跳过修订和发布变更。"""
    bar = _bar()
    instrument_id = uuid4()
    data_version = uuid4()
    engine = FakeEngine(
        [
            {"source_batch_id": uuid4()},
            [{"security_id": 1, "identity_state": "CONFIRMED"}],
            None,
            _instrument_row(instrument_id),
            {"revision": 1, "content_sha256": _bar_content_hash(bar)},
            {"data_version": data_version},
        ]
    )
    repository = _repository(engine)

    publication = repository.publish_daily_bars(
        identifier=EquityIdentifier.parse("SSE.600519"),
        bars=(bar,),
        provider_id="test-provider",
        source_payload_sha256="b" * 64,
        raw_uri="s3://test/raw.json",
        observed_at=datetime(2026, 7, 1, tzinfo=UTC),
    )

    assert publication.data_version == data_version
    assert publication.inserted_count == 0
    assert publication.unchanged_count == 1
    assert len(engine.connection.statements) == 6


def test_repository_creates_pending_identity_version_for_unknown_daily_bar() -> None:
    """日线先到时，仓储必须以该来源批次创建不可公开的 PENDING 标识版本。"""
    engine = FakeEngine(
        [
            {"source_batch_id": uuid4()},
            [],
            None,
            {"security_id": 8},
            None,
            None,
            None,
            None,
        ]
    )
    repository = _repository(engine)

    publication = repository.publish_daily_bars(
        identifier=EquityIdentifier.parse("SZSE.000001"),
        bars=(_bar(),),
        provider_id="test-provider",
        source_payload_sha256="d" * 64,
        raw_uri="s3://test/raw.json",
        observed_at=datetime(2026, 7, 1, tzinfo=UTC),
    )

    assert publication.instrument.listing_status == "PENDING"
    assert "identity_state" in engine.connection.statements[4]
    assert "'PENDING'" in engine.connection.statements[4]
    assert len(engine.connection.statements) == 9


def test_repository_rejects_possible_code_reuse_after_explicit_delisting() -> None:
    """退市后同代码行情不得误绑旧证券或自行创建未经确认的新证券。"""
    engine = FakeEngine(
        [
            {"source_batch_id": uuid4()},
            [{"security_id": 8, "identity_state": "CONFIRMED"}],
            {"exists": 1},
        ]
    )
    repository = _repository(engine)

    with pytest.raises(PossibleCodeReuseError, match="possible code reuse"):
        repository.publish_daily_bars(
            identifier=EquityIdentifier.parse("SSE.600519"),
            bars=(_bar(),),
            provider_id="test-provider",
            source_payload_sha256="e" * 64,
            raw_uri="s3://test/raw.json",
            observed_at=datetime(2026, 7, 1, tzinfo=UTC),
        )

    joined = "\n".join(engine.connection.statements)
    assert "INSERT INTO equity_daily_bar" not in joined
    assert "INSERT INTO equity_instrument" not in joined


def test_repository_reads_instrument_catalog_and_current_daily_bars() -> None:
    """将 SQL 行映射为供内部读取方使用的标准领域值。"""
    instrument_id = uuid4()
    engine = FakeEngine(
        [
            _instrument_row(instrument_id),
            [_instrument_row(instrument_id)],
            [
                {
                    "trade_date": date(2026, 6, 30),
                    "open_price": Decimal("10"),
                    "high_price": Decimal("11"),
                    "low_price": Decimal("9"),
                    "close_price": Decimal("10.5"),
                    "volume_shares": 1_000,
                    "amount_cny": Decimal("10500"),
                    "turnover_rate": None,
                    "revision": 1,
                    "is_final": True,
                }
            ],
        ]
    )
    repository = _repository(engine)

    stored = repository.get_instrument(instrument_id)

    assert stored is not None
    assert stored.identifier.qualified_symbol == "SSE.600519"
    assert repository.list_instruments(query="600", limit=10)[0].instrument_id == instrument_id
    bars = repository.list_daily_bars(
        instrument_id=instrument_id,
        start=date(2026, 6, 1),
        end=date(2026, 6, 30),
    )

    assert bars[0][0].close_price == Decimal("10.5")
    assert bars[0][1:] == (1, True)


def _repository(engine: FakeEngine) -> SqlAlchemyEquityMarketDataRepository:
    """围绕类型转换后的替身引擎构造仓储，不加载运行时配置。"""
    return SqlAlchemyEquityMarketDataRepository(DatabaseClient(engine=cast(Engine, engine)))


def _instrument_row(instrument_id: UUID) -> dict[str, Any]:
    """返回仓储查询替身使用的一条稳定标准证券映射。"""
    return {
        "security_id": 1,
        "instrument_id": instrument_id,
        "exchange": "SSE",
        "symbol": "600519",
        "name": "贵州茅台",
        "listing_status": "LISTED",
    }


def _bar() -> EquityDailyBar:
    """构造供修订和哈希断言使用的一条有效当前日线。"""
    return EquityDailyBar(
        trade_date=date(2026, 6, 30),
        open_price=Decimal("10"),
        high_price=Decimal("11"),
        low_price=Decimal("9"),
        close_price=Decimal("10.5"),
        volume_shares=1_000,
        amount_cny=Decimal("10500"),
        turnover_rate=None,
    )
