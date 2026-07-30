"""申万仓储双时间修订、幂等发布、checkpoint 与读取投影单元测试。"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import AbstractContextManager
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import cast
from unittest.mock import Mock
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest
from sqlalchemy.orm import Session

from service_data_sync.application.ports.canonical_release import PublishedCanonicalRelease
from service_data_sync.application.ports.sw_sector import SwSourceObservation
from service_data_sync.domain.sw_sector import (
    SwIndustryLevel,
    SwIndustryNode,
    SwIndustrySnapshot,
    SwIndustryValuation,
    SwMethodology,
)
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.database.fenced_execution import (
    FencedExecution,
    fenced_execution,
)
from service_data_sync.infrastructure.persistence import sw_sector_repository
from service_data_sync.infrastructure.persistence.sw_sector_repository import (
    SqlAlchemySwSectorRepository,
)

_SNAPSHOT_DATE = date(2026, 7, 28)
_OBSERVED_AT = datetime(2026, 7, 28, 10, tzinfo=UTC)
_METHODOLOGY_ID = uuid5(
    NAMESPACE_URL,
    "quant-v2:sw-methodology:test-sw:1",
)
_SOURCE_BATCH_ID = UUID("10000000-0000-4000-8000-000000000010")
_TAXONOMY_VERSION = UUID("10000000-0000-4000-8000-000000000011")
_VALUATION_VERSION = UUID("10000000-0000-4000-8000-000000000012")


class FakeResult:
    """模拟 SQLAlchemy 映射与标量结果，保留零行语义。"""

    def __init__(self, rows: list[object]) -> None:
        """保存一个查询的确定性结果行。"""
        self._rows = rows

    def mappings(self) -> FakeResult:
        """返回自身以支持 SQLAlchemy 映射读取链。"""
        return self

    def all(self) -> list[Mapping[str, object]]:
        """返回全部映射行，并拒绝测试数据混入标量。"""
        assert all(isinstance(row, Mapping) for row in self._rows)
        return cast(list[Mapping[str, object]], self._rows)

    def one_or_none(self) -> Mapping[str, object] | None:
        """返回零或一条映射行。"""
        assert len(self._rows) <= 1
        if not self._rows:
            return None
        assert isinstance(self._rows[0], Mapping)
        return cast(Mapping[str, object], self._rows[0])

    def scalar_one(self) -> object | None:
        """返回唯一标量；空结果代表历史最大修订号不存在。"""
        assert len(self._rows) <= 1
        return None if not self._rows else self._rows[0]


class FakeConnection(AbstractContextManager["FakeConnection"]):
    """仅为查询消费预置结果，并完整记录查询和写入语句。"""

    def __init__(self, select_results: list[FakeResult]) -> None:
        """保存查询结果队列及所有 SQL 执行记录。"""
        self.select_results = select_results
        self.executions: list[tuple[object, object | None]] = []

    def __enter__(self) -> FakeConnection:
        """进入同步连接上下文。"""
        return self

    def __exit__(self, *arguments: object) -> None:
        """退出上下文且不吞掉仓储异常。"""
        del arguments

    def execute(self, statement: object, parameters: object | None = None) -> FakeResult:
        """记录 SQL；查询按顺序取结果，写入返回空结果。"""
        self.executions.append((statement, parameters))
        if bool(getattr(statement, "is_select", False)):
            assert self.select_results, f"缺少查询结果：{statement!s}"
            return self.select_results.pop(0)
        return FakeResult([])


class FakeDatabase:
    """用同一个可审计连接实现仓储所需会话与事务边界。"""

    def __init__(self, connection: FakeConnection) -> None:
        """保存测试连接。"""
        self.connection = connection

    def session(self) -> FakeConnection:
        """返回只读会话上下文。"""
        return self.connection

    def transaction(self) -> FakeConnection:
        """返回写事务上下文。"""
        return self.connection


def test_repository_publishes_new_snapshot_with_revisions_quality_and_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """首次快照应写入全部节点与估值、闭包、双发布、质量和 checkpoint。"""
    connection = FakeConnection(_empty_results(13))
    repository = _repository(connection)
    taxonomy_version = uuid4()
    valuation_version = uuid4()
    release_bridge = Mock(
        side_effect=(
            _release_result(data_version=taxonomy_version),
            _release_result(data_version=valuation_version),
        )
    )

    def record_source(*arguments: object, **keywords: object) -> UUID:
        """以固定来源批次隔离通用来源表实现。"""
        del arguments, keywords
        return _SOURCE_BATCH_ID

    monkeypatch.setattr(sw_sector_repository, "record_source_observation", record_source)
    monkeypatch.setattr(sw_sector_repository, "publish_legacy_snapshot", release_bridge)

    result = repository.publish_snapshot(snapshot=_snapshot(), source=_source())

    assert result.taxonomy.inserted_count == 3
    assert result.valuation.inserted_count == 3
    assert result.taxonomy.unchanged_count == 0
    assert result.valuation.unchanged_count == 0
    assert result.taxonomy.data_version == taxonomy_version
    assert result.valuation.data_version == valuation_version
    assert connection.select_results == []
    assert any(
        isinstance(parameters, list)
        and parameters
        and isinstance(parameters[0], dict)
        and "ancestor_code" in parameters[0]
        for _statement, parameters in connection.executions
    )
    assert release_bridge.call_args_list[0].kwargs["dataset_code"] == "sector.sw.taxonomy"
    assert release_bridge.call_args_list[1].kwargs["dataset_code"] == "sector.sw.valuation"
    assert len(release_bridge.call_args_list[0].kwargs["records"]) > len(_snapshot().nodes)
    assert not any(
        "INSERT INTO dataset_publication" in str(statement)
        for statement, _parameters in connection.executions
    )
    assert any(
        isinstance(parameters, list)
        and len(parameters) == 4
        and isinstance(parameters[0], dict)
        and "rule_code" in parameters[0]
        for _statement, parameters in connection.executions
    )


def test_repository_reuses_unchanged_revisions_and_publications(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """内容完全相同时复用 taxonomy dataVersion，并把它交给 fenced 控制面。"""
    snapshot = _snapshot()
    responses = [
        FakeResult([_methodology_row()]),
        *[
            FakeResult(
                [
                    {
                        "revision": 1,
                        "content_sha256": sw_sector_repository._node_sha256(  # noqa: SLF001
                            node,
                            methodology_id=_METHODOLOGY_ID,
                        ),
                    }
                ]
            )
            for node in snapshot.nodes
        ],
        *[
            FakeResult(
                [
                    {
                        "revision": 1,
                        "content_sha256": sw_sector_repository._valuation_sha256(  # noqa: SLF001
                            valuation,
                            methodology_id=_METHODOLOGY_ID,
                        ),
                    }
                ]
            )
            for valuation in snapshot.valuations
        ],
    ]
    connection = FakeConnection(responses)
    repository = _repository(connection)

    def record_source(*arguments: object, **keywords: object) -> UUID:
        """返回新的来源观察但不改变业务内容。"""
        del arguments, keywords
        return _SOURCE_BATCH_ID

    monkeypatch.setattr(sw_sector_repository, "record_source_observation", record_source)
    monkeypatch.setattr(
        sw_sector_repository,
        "publish_legacy_snapshot",
        Mock(
            side_effect=(
                _release_result(data_version=_TAXONOMY_VERSION, reused_publication=True),
                _release_result(data_version=_VALUATION_VERSION, reused_publication=True),
            )
        ),
    )
    database = FakeDatabase(connection)
    repository = SqlAlchemySwSectorRepository(cast(DatabaseClient, database))
    execution = FencedExecution(
        database=cast(DatabaseClient, database),
        run_id=uuid5(NAMESPACE_URL, "quant-v2:sw-fenced-test"),
        fencing_token=1,
        finalizer=_ignore_fenced_finalizer,
    )

    with fenced_execution(execution):
        result = repository.publish_snapshot(snapshot=snapshot, source=_source())

    assert result.taxonomy.data_version == _TAXONOMY_VERSION
    assert result.valuation.data_version == _VALUATION_VERSION
    assert result.taxonomy.inserted_count == 0
    assert result.taxonomy.unchanged_count == 3
    assert result.valuation.inserted_count == 0
    assert result.valuation.unchanged_count == 3
    assert execution.checkpoint_kind == "data-version"
    assert execution.checkpoint_position == str(_TAXONOMY_VERSION)
    assert connection.select_results == []
    assert not any(
        isinstance(parameters, list)
        and parameters
        and isinstance(parameters[0], dict)
        and "ancestor_code" in parameters[0]
        for _statement, parameters in connection.executions
    )


def test_repository_closes_changed_knowledge_and_supersedes_publications(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同日内容变化应关闭旧知识行、递增 revision 并切换两个发布版本。"""
    snapshot = _snapshot()
    responses = [FakeResult([_methodology_row()])]
    for _node in snapshot.nodes:
        responses.extend(
            [
                FakeResult([{"revision": 1, "content_sha256": "0" * 64}]),
                FakeResult([1]),
            ]
        )
    for _valuation in snapshot.valuations:
        responses.extend(
            [
                FakeResult([{"revision": 1, "content_sha256": "1" * 64}]),
                FakeResult([1]),
            ]
        )
    connection = FakeConnection(responses)
    repository = _repository(connection)

    def record_source(*arguments: object, **keywords: object) -> UUID:
        """返回固定来源批次以聚焦修订和发布切换。"""
        del arguments, keywords
        return _SOURCE_BATCH_ID

    monkeypatch.setattr(sw_sector_repository, "record_source_observation", record_source)
    monkeypatch.setattr(
        sw_sector_repository,
        "publish_legacy_snapshot",
        Mock(
            side_effect=(
                _release_result(data_version=uuid4()),
                _release_result(data_version=uuid4()),
            )
        ),
    )

    result = repository.publish_snapshot(snapshot=snapshot, source=_source())

    assert result.taxonomy.data_version != _TAXONOMY_VERSION
    assert result.valuation.data_version != _VALUATION_VERSION
    assert result.taxonomy.inserted_count == 3
    assert result.valuation.inserted_count == 3
    assert connection.select_results == []


def test_repository_rejects_invalid_source_and_conflicting_methodology(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """仓储应在事务副作用前拒绝错误来源，并阻止同版本方法学语义漂移。"""
    repository = _repository(FakeConnection([]))

    with pytest.raises(ValueError, match="capability"):
        repository.publish_snapshot(
            snapshot=_snapshot(),
            source=SwSourceObservation(
                **{
                    **_source_values(),
                    "capability": "sector.sw.taxonomy",
                }
            ),
        )
    with pytest.raises(ValueError, match="timezone"):
        repository.publish_snapshot(
            snapshot=_snapshot(),
            source=SwSourceObservation(
                **{
                    **_source_values(),
                    "observed_at": datetime(2026, 7, 28, 10),
                }
            ),
        )

    conflicting = FakeConnection(
        [
            FakeResult(
                [
                    {
                        **_methodology_row(),
                        "semantic_spec_sha256": "f" * 64,
                    }
                ]
            )
        ]
    )

    def record_source(*arguments: object, **keywords: object) -> UUID:
        """冲突在来源批次写入前发生，因此替身不应被调用。"""
        del arguments, keywords
        raise AssertionError("方法学冲突后不得登记来源批次")

    monkeypatch.setattr(sw_sector_repository, "record_source_observation", record_source)
    with pytest.raises(ValueError, match="conflicts"):
        _repository(conflicting).publish_snapshot(snapshot=_snapshot(), source=_source())


def test_repository_reads_checkpoint_publication_nodes_closure_and_valuations() -> None:
    """读取路径应处理空值、筛选、稳定游标和领域对象投影。"""
    publication_row = {
        "capability": "sector.sw.taxonomy",
        "data_version": _TAXONOMY_VERSION,
        "snapshot_date": _SNAPSHOT_DATE,
        "published_at": _OBSERVED_AT,
        "quality_status": "passed",
        "row_count": 3,
        "content_sha256": "a" * 64,
        **_methodology_row(),
    }
    node_row = _node_row()
    valuation_row = {
        **node_row,
        "node_revision": 2,
        "static_pe": None,
        "ttm_pe": Decimal("11.2"),
        "pb": Decimal("2.1"),
        "dividend_yield_ratio": None,
        "valuation_revision": 3,
    }
    connection = FakeConnection(
        [
            FakeResult([_checkpoint_row()]),
            FakeResult([]),
            FakeResult([publication_row]),
            FakeResult([]),
            FakeResult([node_row]),
            FakeResult([node_row]),
            FakeResult([]),
            FakeResult([_node_row(code="801010.SI", level=1, parent_code=None), node_row]),
            FakeResult([valuation_row]),
        ]
    )
    repository = _repository(connection)

    checkpoint = repository.get_checkpoint(snapshot_date=_SNAPSHOT_DATE)
    missing_checkpoint = repository.get_checkpoint(snapshot_date=date(2026, 7, 27))
    publication = repository.get_publication(
        capability="sector.sw.taxonomy",
        snapshot_date=_SNAPSHOT_DATE,
    )
    missing_publication = repository.get_publication(
        capability="sector.sw.valuation",
        snapshot_date=None,
    )
    nodes = repository.list_nodes(
        snapshot_date=_SNAPSHOT_DATE,
        level=2,
        parent_code="801010.SI",
        after_level=1,
        after_code="801010.SI",
        limit=10,
    )
    node = repository.get_node(snapshot_date=_SNAPSHOT_DATE, code="801016.SI")
    missing_node = repository.get_node(snapshot_date=_SNAPSHOT_DATE, code="000000.SI")
    ancestors = repository.list_ancestors(
        data_version=_TAXONOMY_VERSION,
        snapshot_date=_SNAPSHOT_DATE,
        descendant_code="850111.SI",
    )
    valuations = repository.list_valuations(
        snapshot_date=_SNAPSHOT_DATE,
        level=2,
        after_code="801010.SI",
        limit=10,
    )

    assert checkpoint is not None and checkpoint.raw_uri == "s3://raw/sw.json"
    assert checkpoint.last_data_version == _TAXONOMY_VERSION
    assert missing_checkpoint is None
    assert publication is not None and publication.methodology.code == "test-sw"
    assert missing_publication is None
    assert nodes[0].node.parent_code == "801010.SI"
    assert node is not None and node.revision == 2
    assert missing_node is None
    assert [value.node.code for value in ancestors] == ["801010.SI", "801016.SI"]
    assert valuations[0].valuation.static_pe is None
    assert valuations[0].valuation.ttm_pe == Decimal("11.2")
    assert valuations[0].revision == 3
    assert connection.select_results == []


def test_repository_validates_read_limits_and_composite_cursor() -> None:
    """仓储应拒绝越界页长和只提供一半的复合节点游标。"""
    repository = _repository(FakeConnection([]))

    with pytest.raises(ValueError, match="node limit"):
        repository.list_nodes(
            snapshot_date=_SNAPSHOT_DATE,
            level=None,
            parent_code=None,
            after_level=None,
            after_code=None,
            limit=0,
        )
    with pytest.raises(ValueError, match="cursor"):
        repository.list_nodes(
            snapshot_date=_SNAPSHOT_DATE,
            level=None,
            parent_code=None,
            after_level=1,
            after_code=None,
            limit=10,
        )
    with pytest.raises(ValueError, match="valuation limit"):
        repository.list_valuations(
            snapshot_date=_SNAPSHOT_DATE,
            level=None,
            after_code=None,
            limit=502,
        )


def _repository(connection: FakeConnection) -> SqlAlchemySwSectorRepository:
    """把可审计连接装配为仓储。"""
    database = cast(DatabaseClient, cast(object, FakeDatabase(connection)))
    return SqlAlchemySwSectorRepository(database)


def _empty_results(count: int) -> list[FakeResult]:
    """构造指定数量的零行查询结果。"""
    return [FakeResult([]) for _index in range(count)]


def _methodology_row() -> dict[str, object]:
    """构造与测试快照完全一致的方法学数据库行。"""
    return {
        "methodology_id": _METHODOLOGY_ID,
        "code": "test-sw",
        "version": 1,
        "status": "source_reported",
        "upstream_source": "test.sw",
        "semantic_spec_sha256": "b" * 64,
    }


def _release_result(
    *, data_version: UUID, reused_publication: bool = False
) -> PublishedCanonicalRelease:
    """构造统一 canonical 发布器的真实版本结果替身。"""
    return PublishedCanonicalRelease(
        release_id=uuid4(),
        data_version=data_version,
        reused_release=reused_publication,
        reused_publication=reused_publication,
        published_at=_OBSERVED_AT,
    )


def _published_row(*, data_version: UUID, content_sha256: str) -> dict[str, object]:
    """构造一个仍有效的消费者发布查询行。"""
    return {
        "data_version": data_version,
        "published_at": _OBSERVED_AT,
        "row_count": 3,
        "content_sha256": content_sha256,
    }


def _checkpoint_row() -> dict[str, object]:
    """构造包含完整 raw 与 normalized 血缘的 checkpoint 行。"""
    return {
        "snapshot_date": _SNAPSHOT_DATE,
        "summary_sha256": "c" * 64,
        "raw_sha256": "d" * 64,
        "raw_uri": "s3://raw/sw.json",
        "normalized_uri": "s3://normalized/sw.json",
        "provider_id": "fake-sw",
        "upstream_source": "test.sw",
        "adapter_version": "test-v1",
        "schema_fingerprint": "e" * 64,
        "observed_at": _OBSERVED_AT,
        "last_data_version": _TAXONOMY_VERSION,
    }


def _node_row(
    *,
    code: str = "801016.SI",
    level: int = 2,
    parent_code: str | None = "801010.SI",
) -> dict[str, object]:
    """构造一个通过质量门的当前节点投影行。"""
    return {
        "sector_code": code,
        "name": "种植业" if level == 2 else "农林牧渔",
        "level": level,
        "parent_code": parent_code,
        "component_count": 8,
        "revision": 2,
    }


def _snapshot() -> SwIndustrySnapshot:
    """构造完整三级、一一覆盖且包含可空估值的快照。"""
    nodes = (
        SwIndustryNode(
            code="801010.SI",
            name="农林牧渔",
            level=SwIndustryLevel.LEVEL_1,
            parent_code=None,
            component_count=8,
        ),
        SwIndustryNode(
            code="801016.SI",
            name="种植业",
            level=SwIndustryLevel.LEVEL_2,
            parent_code="801010.SI",
            component_count=8,
        ),
        SwIndustryNode(
            code="850111.SI",
            name="种子",
            level=SwIndustryLevel.LEVEL_3,
            parent_code="801016.SI",
            component_count=8,
        ),
    )
    valuations = tuple(
        SwIndustryValuation(
            code=node.code,
            snapshot_date=_SNAPSHOT_DATE,
            static_pe=None if node.level is SwIndustryLevel.LEVEL_2 else Decimal("10.1"),
            ttm_pe=Decimal("11.2"),
            pb=Decimal("2.1"),
            dividend_yield_ratio=Decimal("0.01"),
        )
        for node in nodes
    )
    return SwIndustrySnapshot(
        snapshot_date=_SNAPSHOT_DATE,
        nodes=nodes,
        valuations=valuations,
        methodology=SwMethodology(
            code="test-sw",
            version=1,
            status="source_reported",
            upstream_source="test.sw",
            semantic_spec_sha256="b" * 64,
        ),
    )


def _source_values() -> dict[str, object]:
    """构造可传入来源观察的全部字段。"""
    return {
        "provider_id": "fake-sw",
        "capability": "sector.sw.snapshot.raw",
        "source_payload_sha256": "a" * 64,
        "raw_uri": "s3://raw/sw.json",
        "normalized_payload_sha256": "b" * 64,
        "normalized_uri": "s3://normalized/sw.json",
        "observed_at": _OBSERVED_AT,
        "upstream_source": "test.sw",
        "adapter_version": "test-v1",
        "schema_fingerprint": "c" * 64,
    }


def _source() -> SwSourceObservation:
    """构造时区完整的来源观察。"""
    return SwSourceObservation(**_source_values())  # type: ignore[arg-type]


def _ignore_fenced_finalizer(_session: Session, _execution: FencedExecution) -> None:
    """为仓储单测提供不写控制面状态的最小终态回调。"""
