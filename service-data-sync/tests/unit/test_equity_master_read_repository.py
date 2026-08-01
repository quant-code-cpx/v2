"""证券主数据只读仓储的发布绑定、双时间 SQL 与映射单元测试。"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, date, datetime
from typing import cast
from uuid import UUID

import pytest

from service_data_sync.application.ports.equity_master_read import EquityMasterReadUnavailable
from service_data_sync.domain.equity import Exchange
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.persistence.equity_master_read_repository import (
    SqlAlchemyEquityMasterReadRepository,
)

_VERSION = UUID("10000000-0000-4000-8000-000000000001")
_INSTRUMENT_ID = UUID("20000000-0000-4000-8000-000000000001")
_CUTOFF = datetime(2026, 7, 2, 12, tzinfo=UTC)


class FakeResult:
    """模拟只读仓储使用的 SQLAlchemy 映射结果。"""

    def __init__(self, value: object) -> None:
        """保存当前 SQL 对应的可选行或行列表。"""
        self._value = value

    def mappings(self) -> FakeResult:
        """测试响应已经是映射形，直接返回当前替身。"""
        return self

    def one_or_none(self) -> object:
        """返回发布查询的单行或空值。"""
        return self._value

    def all(self) -> list[Mapping[str, object]]:
        """返回目录或历史查询的映射行列表。"""
        assert isinstance(self._value, list)
        return cast(list[Mapping[str, object]], self._value)


class FakeConnection:
    """记录只读 SQL 和参数，并按调用顺序回放响应。"""

    def __init__(self, responses: list[object]) -> None:
        """初始化共享响应队列与空的语句、参数记录。"""
        self._responses = responses
        self.statements: list[str] = []
        self.parameters: list[Mapping[str, object]] = []

    def execute(
        self,
        statement: object,
        parameters: Mapping[str, object] | None = None,
    ) -> FakeResult:
        """记录 SQL 与绑定值，然后消费下一项确定性响应。"""
        self.statements.append(str(statement))
        self.parameters.append({} if parameters is None else parameters)
        return FakeResult(self._responses.pop(0))


class FakeEngine:
    """提供仓储只读连接上下文所需的最小引擎形状。"""

    def __init__(self, responses: list[object]) -> None:
        """创建被所有连接上下文复用的替身连接。"""
        self.connection = FakeConnection(responses)

    @contextmanager
    def connect(self) -> Iterator[FakeConnection]:
        """产出不执行网络连接或事务提交的替身连接。"""
        yield self.connection


class FakeDatabase:
    """以短生命周期 Session 接口模拟 `DatabaseClient`。"""

    def __init__(self, engine: FakeEngine) -> None:
        """保存承载测试响应队列的连接替身。"""
        self._engine = engine

    @contextmanager
    def session(self) -> Iterator[FakeConnection]:
        """提供一次只读调用使用的模拟 Session。"""
        with self._engine.connect() as connection:
            yield connection


def test_repository_requires_complete_aggregate_publication_components() -> None:
    """全市场 resolved publication 只有解析完整六个输入组件时才可读取。"""
    engine = FakeEngine(
        [
            _publication_row(),
            _component_rows(),
            _publication_row(),
            _component_rows()[:-1],
        ]
    )
    repository = _repository(engine)

    publication = repository.get_current_publication(exchange=None)
    incomplete = repository.get_current_publication(exchange=None)

    assert publication is not None
    assert publication.data_version == _VERSION
    assert publication.publication_scope == "CN_A_STABLE"
    assert len(publication.components) == 6
    assert incomplete is None
    assert "dataset_publication_component" in engine.connection.statements[1]
    assert "resolved_leaf" in engine.connection.statements[1]


def test_repository_lists_published_slice_with_literal_prefix_and_keyset() -> None:
    """目录查询应绑定 resolved 组件、分开应用 cutoff 并转义前缀元字符。"""
    engine = FakeEngine([[_instrument_row()]])
    repository = _repository(engine)

    rows = repository.list_instruments(
        data_version=_VERSION,
        exchange=None,
        statuses=("LISTED",),
        query="浦_%",
        as_of=date(2026, 7, 1),
        known_at=_CUTOFF,
        after_exchange=Exchange.SSE,
        after_symbol="600000",
        after_instrument_id=_INSTRUMENT_ID,
        limit=2,
    )

    statement = engine.connection.statements[0]
    parameters = engine.connection.parameters[0]
    assert rows[0].instrument_id == _INSTRUMENT_ID
    assert rows[0].name.value == "浦发银行"
    assert "dataset_publication_component" in statement
    assert "identity_state" in statement
    assert "catalog_known_at" in statement
    assert "lifecycle_known_at" in statement
    assert "lower(name_projection.name)" in statement
    assert "ORDER BY equity_identifier_version.exchange" in statement
    assert parameters == {}


def test_repository_maps_projection_cardinality_failure_to_unavailable() -> None:
    """已发布身份缺名称或存在重复投影时必须失败，不能返回空页或伪造详情。"""
    broken = _instrument_row()
    broken["name_match_count"] = 0
    engine = FakeEngine([[broken]])
    repository = _repository(engine)

    with pytest.raises(EquityMasterReadUnavailable):
        repository.list_instruments(
            data_version=_VERSION,
            exchange=Exchange.SSE,
            statuses=(),
            query=None,
            as_of=date(2026, 7, 1),
            known_at=_CUTOFF,
            after_exchange=None,
            after_symbol=None,
            after_instrument_id=None,
            limit=2,
        )


def test_repository_resolves_current_open_or_historical_identifier() -> None:
    """详情查询未传 asOf 时只选开放代码，显式日期时使用历史有效区间。"""
    engine = FakeEngine([[_instrument_row()], [_instrument_row()]])
    repository = _repository(engine)

    current = repository.find_instruments(
        data_version=_VERSION,
        exchange=Exchange.SSE,
        symbol="600000",
        identifier_as_of=None,
        projection_as_of=date(2026, 7, 1),
        known_at=_CUTOFF,
    )
    historical = repository.find_instruments(
        data_version=_VERSION,
        exchange=Exchange.SSE,
        symbol="600000",
        identifier_as_of=date(2010, 1, 1),
        projection_as_of=date(2010, 1, 1),
        known_at=_CUTOFF,
    )

    assert current[0].identifier.symbol == "600000"
    assert historical[0].identifier.symbol == "600000"
    assert "effective_to IS NULL" in engine.connection.statements[0]
    assert "effective_from <= :effective_from_2" in engine.connection.statements[1]
    assert engine.connection.parameters[1] == {}


def test_repository_lists_auditable_listing_revisions_at_publication_cutoff() -> None:
    """历史查询应绑定交易所发布、按区间相交，并隐藏截止时间后的 knownTo。"""
    version_id = UUID("30000000-0000-4000-8000-000000000001")
    row = {
        "version_id": version_id,
        "status": "LISTED",
        "effective_from": date(2001, 8, 27),
        "effective_to": None,
        "effective_date_precision": "OFFICIAL_DATE",
        "known_from": datetime(2026, 6, 1, tzinfo=UTC),
        "visible_known_to": None,
        "observed_at": datetime(2026, 6, 1, 1, tzinfo=UTC),
        "evidence_kind": "EXPLICIT_LISTING",
        "source_batch_id": "50000000-0000-4000-8000-000000000002",
        "provider_id": "lifecycle-provider",
        "upstream_source": "sse.lifecycle",
        "quality_status": "passed",
    }
    engine = FakeEngine([[row]])
    repository = _repository(engine)

    rows = repository.list_listing_status_history(
        data_version=_VERSION,
        exchange=Exchange.SSE,
        security_id=8,
        known_at=_CUTOFF,
        effective_from=date(2000, 1, 1),
        effective_to=date(2030, 1, 1),
        after_effective_from=date(2001, 8, 27),
        after_known_from=datetime(2026, 5, 1, tzinfo=UTC),
        after_version_id=UUID("30000000-0000-4000-8000-000000000000"),
        limit=2,
    )

    statement = engine.connection.statements[0]
    parameters = engine.connection.parameters[0]
    assert rows[0].version_id == version_id
    assert rows[0].known_to is None
    assert "FROM publication_scope JOIN equity_listing_status_version" in statement
    assert (
        "equity_listing_status_version.known_to <= publication_scope.lifecycle_known_at"
        in statement
    )
    assert "ORDER BY equity_listing_status_version.effective_from" in statement
    assert parameters == {}


def _repository(engine: FakeEngine) -> SqlAlchemyEquityMasterReadRepository:
    """围绕带短 Session 边界的替身数据库构造仓储。"""
    return SqlAlchemyEquityMasterReadRepository(cast(DatabaseClient, FakeDatabase(engine)))


def _publication_row() -> dict[str, object]:
    """构造 resolved 聚合发布查询所需的完整映射行。"""
    return {
        "data_version": _VERSION,
        "published_at": datetime(2026, 7, 2, 12, 5, tzinfo=UTC),
        "effective_as_of": date(2026, 7, 1),
    }


def _component_rows() -> list[dict[str, object]]:
    """构造三所目录与生命周期输入组件，保留各自版本和独立 cutoff。"""
    return [
        {
            "component_key": f"{exchange}.{kind}",
            "dataset": (
                "equity.master.catalog" if kind == "catalog" else "equity.lifecycle.explicit"
            ),
            "partition_key": exchange,
            "data_version": UUID(f"40000000-0000-4000-8000-0000000000{index:02d}"),
            "published_at": datetime(2026, 7, 2, 12, 5, tzinfo=UTC),
            "effective_as_of": date(2026, 7, 1),
            "knowledge_cutoff": _CUTOFF,
            "quality_status": "passed",
        }
        for index, (exchange, kind) in enumerate(
            (
                ("SSE", "catalog"),
                ("SSE", "lifecycle"),
                ("SZSE", "catalog"),
                ("SZSE", "lifecycle"),
                ("BSE", "catalog"),
                ("BSE", "lifecycle"),
            ),
            start=1,
        )
    ]


def _instrument_row() -> dict[str, object]:
    """构造一行名称与生命周期投影均唯一的 canonical 查询结果。"""
    known_from = datetime(2026, 6, 1, tzinfo=UTC)
    observed_at = datetime(2026, 6, 1, 1, tzinfo=UTC)
    return {
        "security_id": 8,
        "instrument_id": _INSTRUMENT_ID,
        "exchange": "SSE",
        "symbol": "600000",
        "identifier_effective_from": date(1999, 11, 10),
        "identifier_effective_to": None,
        "identifier_date_precision": "OFFICIAL_DATE",
        "identifier_known_from": known_from,
        "identifier_observed_at": observed_at,
        "identifier_source_batch_id": "50000000-0000-4000-8000-000000000001",
        "identifier_provider_id": "catalog-provider",
        "identifier_upstream_source": "eastmoney.equity-catalog",
        "catalog_quality_status": "passed",
        "name_match_count": 1,
        "name": "浦发银行",
        "name_effective_from": date(1999, 11, 10),
        "name_effective_to": None,
        "name_date_precision": "OFFICIAL_DATE",
        "name_known_from": known_from,
        "name_observed_at": observed_at,
        "name_source_batch_id": "50000000-0000-4000-8000-000000000001",
        "name_provider_id": "catalog-provider",
        "name_upstream_source": "eastmoney.equity-catalog",
        "listing_match_count": 1,
        "status": "LISTED",
        "listed_on": date(1999, 11, 10),
        "delisted_on": None,
        "listing_effective_from": date(1999, 11, 10),
        "listing_effective_to": None,
        "listing_date_precision": "OFFICIAL_DATE",
        "listing_known_from": known_from,
        "listing_observed_at": observed_at,
        "listing_evidence_kind": "EXPLICIT_LISTING",
        "listing_source_batch_id": "50000000-0000-4000-8000-000000000002",
        "listing_provider_id": "lifecycle-provider",
        "listing_upstream_source": "sse.lifecycle",
        "lifecycle_quality_status": "passed",
    }
