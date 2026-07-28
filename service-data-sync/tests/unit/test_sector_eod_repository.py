"""板块 EOD PostgreSQL 仓储的完整性、版本化和确定性排行单元测试。"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy.engine import Connection, Engine

from service_data_sync.application.ports.sector_eod import (
    SectorEodQualityResult,
    SectorEodRun,
)
from service_data_sync.domain.sector import (
    SectorEodQuote,
    SectorEodSort,
    SectorIdentifier,
    SectorScheme,
    SortOrder,
)
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.persistence import sector_eod_repository
from service_data_sync.infrastructure.persistence.sector_eod_repository import (
    SqlAlchemySectorEodRepository,
)


class FakeResult:
    """提供仓储 SQL 所需的映射、单值和可选单行读取接口。"""

    def __init__(self, value: object) -> None:
        """保存本次语句应返回的确定性替身值。"""
        self._value = value

    def mappings(self) -> FakeResult:
        """使字典和字典列表可直接模拟 SQLAlchemy mappings 结果。"""
        return self

    def one_or_none(self) -> object:
        """返回可空单行结果。"""
        return self._value

    def one(self) -> object:
        """返回必须存在的单行，用于任务 run 创建的 `RETURNING` 结果。"""
        assert self._value is not None
        return self._value

    def all(self) -> list[object]:
        """返回多行查询结果。"""
        assert isinstance(self._value, list)
        return self._value

    def scalar_one(self) -> object:
        """返回聚合 SQL 的单个标量结果。"""
        return self._value


class FakeConnection:
    """顺序返回预置 SQL 结果，并保存语句与参数供断言。"""

    def __init__(self, responses: list[object]) -> None:
        """初始化共享响应队列和执行记录。"""
        self.responses = responses
        self.statements: list[str] = []
        self.parameters: list[object] = []

    def execute(self, statement: object, parameters: object = None) -> FakeResult:
        """记录 SQL 并取出下一项模拟结果，未预置时返回 `None`。"""
        self.statements.append(str(statement))
        self.parameters.append(parameters)
        return FakeResult(self.responses.pop(0) if self.responses else None)


class FakeEngine:
    """提供可供仓储事务和只读连接复用的单连接引擎替身。"""

    def __init__(self, responses: list[object]) -> None:
        """构造共用连接，使发布和读取测试无需 PostgreSQL。"""
        self.connection = FakeConnection(responses)

    @contextmanager
    def begin(self) -> Iterator[FakeConnection]:
        """模拟可提交事务上下文。"""
        yield self.connection

    @contextmanager
    def connect(self) -> Iterator[FakeConnection]:
        """模拟只读连接上下文。"""
        yield self.connection


def test_publish_requires_exact_active_catalog_coverage_after_recording_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """遗漏 ACTIVE 板块必须阻止发布，但已归档来源观察仍可供人工修复后 replay。"""
    engine = FakeEngine([[{"sector_key": 1, "sector_code": "BK0001"}]])
    monkeypatch.setattr(
        sector_eod_repository,
        "record_source_observation",
        _record_source_observation,
    )
    repository = _repository(engine)

    with pytest.raises(ValueError, match="completely cover"):
        repository.publish_snapshot(
            scheme=SectorScheme.EASTMONEY_INDUSTRY,
            trade_date=date(2026, 7, 27),
            source_cutoff_at=datetime(2026, 7, 27, 8, 15, tzinfo=UTC),
            observed_at=datetime(2026, 7, 27, 8, 20, tzinfo=UTC),
            quotes=(_quote("BK0002"),),
            provider_id="test-provider",
            source_payload_sha256="a" * 64,
            raw_uri="s3://test/raw.json",
            adapter_version="test-v1",
            schema_fingerprint="b" * 64,
        )


def test_run_checkpoint_records_raw_observation_and_exposes_it_for_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """新观察必须持有 fenced lease、写入 source batch 并让 replay 只读取 checkpoint 指针。"""
    run_id = uuid4()
    source_batch_id = uuid4()
    engine = FakeEngine(
        [
            None,
            {"run_id": run_id},
            None,
            None,
            {"run_id": run_id},
            None,
            None,
            {"run_id": run_id},
            {
                "source_batch_id": source_batch_id,
                "raw_uri": "s3://test/raw.json",
                "provider_id": "test-provider",
                "observed_at": datetime(2026, 7, 27, 8, 20, tzinfo=UTC),
                "adapter_version": "test-v1",
                "schema_fingerprint": "b" * 64,
            },
        ]
    )
    monkeypatch.setattr(
        sector_eod_repository,
        "record_source_observation",
        _record_source_batch_with_fixed_id(source_batch_id),
    )
    repository = _repository(engine)

    run = repository.start_run(
        scheme=SectorScheme.EASTMONEY_INDUSTRY,
        trade_date=date(2026, 7, 27),
        reuse_archived_raw=False,
    )
    observation = repository.record_archived_observation(
        run=run,
        provider_id="test-provider",
        source_payload_sha256="a" * 64,
        raw_uri="s3://test/raw.json",
        observed_at=datetime(2026, 7, 27, 8, 20, tzinfo=UTC),
        adapter_version="test-v1",
        schema_fingerprint="b" * 64,
    )
    replay = repository.get_archived_observation(run=run)

    statements = "\n".join(engine.connection.statements)
    assert run.run_id == run_id
    assert observation.source_batch_id == source_batch_id
    assert replay.raw_uri == "s3://test/raw.json"
    assert "INSERT INTO sync_run" in statements
    assert "INSERT INTO sector_eod_sync_partition" in statements
    assert any(
        isinstance(parameters, dict) and parameters.get("stage") == "raw_archived"
        for parameters in engine.connection.parameters
    )


def test_failed_run_releases_only_the_current_fenced_lease() -> None:
    """失败 checkpoint 必须保留错误码并清空租约，避免旧 worker 阻塞后续接管。"""
    run = SectorEodRun(
        run_id=uuid4(),
        lease_token=uuid4(),
        scheme=SectorScheme.EASTMONEY_INDUSTRY,
        trade_date=date(2026, 7, 27),
    )
    engine = FakeEngine([])

    _repository(engine).mark_failed(run=run, error_code="schema")

    statements = "\n".join(engine.connection.statements)
    assert "lease_token = CASE WHEN :release_lease THEN NULL" in statements
    assert "SET status = 'failed'" in statements
    assert "WHERE scheme = :scheme" in statements


def test_renewal_and_reaper_preserve_fencing_and_requeue_expired_checkpoint() -> None:
    """当前 owner 续约必须保留 token；reaper 只释放过期分区并把恢复信息写回 queued 状态。"""
    run = SectorEodRun(
        run_id=uuid4(),
        lease_token=uuid4(),
        scheme=SectorScheme.EASTMONEY_INDUSTRY,
        trade_date=date(2026, 7, 27),
    )
    renewal_engine = FakeEngine([])

    _repository(renewal_engine).renew_lease(run=run)

    renewal_sql = "\n".join(renewal_engine.connection.statements)
    assert "lease_token = :lease_token" in renewal_sql
    assert "lease_expires_at > :now" in renewal_sql
    reaper_engine = FakeEngine(
        [
            [
                {
                    "run_id": run.run_id,
                    "scheme": run.scheme.value,
                    "trade_date": run.trade_date,
                    "stage": "raw_archived",
                    "last_source_batch_id": uuid4(),
                }
            ]
        ]
    )

    count = _repository(reaper_engine).requeue_expired_leases(
        now=datetime(2026, 7, 27, 8, 30, tzinfo=UTC)
    )

    reaper_sql = "\n".join(reaper_engine.connection.statements)
    assert count == 1
    assert "status = 'queued'" in reaper_sql
    assert '"stage":"raw_archived"' in str(reaper_engine.connection.parameters)
    assert 'errorCode":"lease-expired' in str(reaper_engine.connection.parameters)


def test_run_rejects_an_unexpired_lease_and_replay_without_raw() -> None:
    """其他 worker 的未过期租约不可接管，且 replay 没有 raw checkpoint 时必须失败。"""
    future = datetime.now(UTC) + timedelta(minutes=1)
    engine = FakeEngine(
        [
            {"status": "running", "lease_expires_at": future, "last_source_batch_id": None},
            {"status": "failed", "lease_expires_at": None, "last_source_batch_id": None},
        ]
    )
    repository = _repository(engine)

    with pytest.raises(RuntimeError, match="already leased"):
        repository.start_run(
            scheme=SectorScheme.EASTMONEY_INDUSTRY,
            trade_date=date(2026, 7, 27),
            reuse_archived_raw=False,
        )
    with pytest.raises(ValueError, match="requires an archived"):
        repository.start_run(
            scheme=SectorScheme.EASTMONEY_INDUSTRY,
            trade_date=date(2026, 7, 27),
            reuse_archived_raw=True,
        )


def test_fencing_and_quality_helpers_reject_cross_run_evidence_and_invalid_statuses() -> None:
    """fencing 查询、source batch 归属与质量状态必须拒绝旧 owner、跨分区证据和矛盾质量。"""
    run = SectorEodRun(
        run_id=uuid4(),
        lease_token=uuid4(),
        scheme=SectorScheme.EASTMONEY_INDUSTRY,
        trade_date=date(2026, 7, 27),
    )
    engine = FakeEngine([None, None])

    with pytest.raises(RuntimeError, match="no longer active"):
        sector_eod_repository._assert_active_run(
            cast(Connection, engine.connection),
            run=run,
            now=datetime(2026, 7, 27, 8, 20, tzinfo=UTC),
        )
    with pytest.raises(ValueError, match="does not belong"):
        sector_eod_repository._ensure_source_batch_belongs_to_run(
            cast(Connection, engine.connection), source_batch_id=uuid4(), run=run
        )
    warning = SectorEodQualityResult(
        rule_code="availability-market-value",
        severity="warning",
        passed=False,
        actual={"available": 90},
        threshold={"minimum": 95},
    )
    blocking = SectorEodQualityResult(
        rule_code="catalog-coverage",
        severity="blocking",
        passed=False,
        actual={"covered": 99},
        threshold={"expected": 100},
    )

    with pytest.raises(ValueError, match="requires warned"):
        sector_eod_repository._validate_quality_results(status="passed", results=(warning,))
    with pytest.raises(ValueError, match="blocking quality"):
        sector_eod_repository._validate_quality_results(status="warned", results=(blocking,))
    with pytest.raises(ValueError, match="rule code"):
        SectorEodQualityResult(
            rule_code="",
            severity="info",
            passed=True,
            actual={},
            threshold={},
        )
    with pytest.raises(ValueError, match="severity"):
        SectorEodQualityResult(
            rule_code="schema",
            severity="fatal",
            passed=False,
            actual={},
            threshold={},
        )


def test_publish_inserts_atomic_snapshot_quotes_quality_and_publication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """完整新内容应写入 header、报价、质量和 dataset publication，不修改旧 K 线表。"""
    engine = FakeEngine(
        [
            [{"sector_key": 1, "sector_code": "BK0001"}],
            None,
            1,
        ]
    )
    source_batch_id = uuid4()
    monkeypatch.setattr(
        sector_eod_repository,
        "record_source_observation",
        _record_source_batch_with_fixed_id(source_batch_id),
    )
    repository = _repository(engine)

    publication = repository.publish_snapshot(
        scheme=SectorScheme.EASTMONEY_INDUSTRY,
        trade_date=date(2026, 7, 27),
        source_cutoff_at=datetime(2026, 7, 27, 8, 15, tzinfo=UTC),
        observed_at=datetime(2026, 7, 27, 8, 20, tzinfo=UTC),
        quotes=(_quote("BK0001"),),
        provider_id="test-provider",
        source_payload_sha256="a" * 64,
        raw_uri="s3://test/raw.json",
        adapter_version="test-v1",
        schema_fingerprint="b" * 64,
    )

    statements = "\n".join(engine.connection.statements)
    assert publication.inserted is True
    assert "INSERT INTO sector_eod_snapshot" in statements
    assert "INSERT INTO sector_eod_quote" in statements
    assert "INSERT INTO sector_eod_quality_result" in statements
    assert "INSERT INTO dataset_publication" in statements
    assert "sector_daily_bar" not in statements


def test_ranked_read_uses_only_controlled_sort_expression_and_stable_position() -> None:
    """排行读取应以受控 `change_percent` 表达式、null-last 和 position 分页，不拼接外部列名。"""
    sector_id = uuid4()
    engine = FakeEngine(
        [
            [
                {
                    "sector_id": sector_id,
                    "scheme": "eastmoney.industry",
                    "sector_code": "BK0001",
                    "sector_name": "银行",
                    "latest_value": Decimal("1000"),
                    "change_value": Decimal("10"),
                    "change_percent": Decimal("1"),
                    "market_value": Decimal("1000000"),
                    "turnover_percent": Decimal("3"),
                    "advancers": 10,
                    "decliners": 3,
                    "leader_name": "示例证券",
                    "leader_change_percent": Decimal("5"),
                    "rank": 1,
                    "position": 1,
                }
            ]
        ]
    )
    repository = _repository(engine)

    rows = repository.list_ranked_quotes(
        snapshot_id=uuid4(),
        sort=SectorEodSort.CHANGE_PERCENT,
        order=SortOrder.DESC,
        after_position=None,
        limit=2,
    )

    assert rows[0].rank == 1
    assert rows[0].position == 1
    assert "quote.change_percent DESC NULLS LAST" in engine.connection.statements[0]
    assert 'sector.sector_code COLLATE "C" ASC' in engine.connection.statements[0]


def test_publish_reuses_current_revision_when_normalized_content_is_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同内容重跑必须新增来源观察但复用已发布 dataVersion，避免制造伪修订。"""
    quote = _quote("BK0001")
    data_version = uuid4()
    existing = {
        "snapshot_id": uuid4(),
        "data_version": data_version,
        "scheme": "eastmoney.industry",
        "trade_date": date(2026, 7, 27),
        "source_cutoff_at": datetime(2026, 7, 27, 8, 15, tzinfo=UTC),
        "observed_at": datetime(2026, 7, 27, 8, 20, tzinfo=UTC),
        "finality": "post_close_observation",
        "quality_status": "passed",
        "published_at": datetime(2026, 7, 27, 8, 20, tzinfo=UTC),
        "normalizer_version": "sector-eod-v1",
        "content_sha256": sector_eod_repository._snapshot_content_hash((quote,)),
    }
    engine = FakeEngine([[{"sector_key": 1, "sector_code": "BK0001"}], existing])
    monkeypatch.setattr(
        sector_eod_repository,
        "record_source_observation",
        _record_source_observation,
    )

    publication = _repository(engine).publish_snapshot(
        scheme=SectorScheme.EASTMONEY_INDUSTRY,
        trade_date=date(2026, 7, 27),
        source_cutoff_at=datetime(2026, 7, 27, 8, 15, tzinfo=UTC),
        observed_at=datetime(2026, 7, 27, 8, 20, tzinfo=UTC),
        quotes=(quote,),
        provider_id="test-provider",
        source_payload_sha256="a" * 64,
        raw_uri="s3://test/raw.json",
        adapter_version="test-v1",
        schema_fingerprint="b" * 64,
    )

    assert publication.inserted is False
    assert publication.snapshot.data_version == data_version
    assert "INSERT INTO sector_eod_snapshot" not in "\n".join(engine.connection.statements)


def test_repository_reads_exact_snapshot_and_single_quote() -> None:
    """读取路径必须保留精确日期与单板块快照语义，不依赖目录当前名称或 supplier 排名。"""
    snapshot_id = uuid4()
    data_version = uuid4()
    engine = FakeEngine(
        [
            {
                "snapshot_id": snapshot_id,
                "data_version": data_version,
                "scheme": "eastmoney.industry",
                "trade_date": date(2026, 7, 27),
                "source_cutoff_at": datetime(2026, 7, 27, 8, 15, tzinfo=UTC),
                "observed_at": datetime(2026, 7, 27, 8, 20, tzinfo=UTC),
                "finality": "post_close_observation",
                "quality_status": "warned",
                "published_at": datetime(2026, 7, 27, 8, 21, tzinfo=UTC),
            },
            {
                "sector_id": uuid4(),
                "scheme": "eastmoney.industry",
                "sector_code": "BK0001",
                "sector_name": "银行",
                "latest_value": Decimal("1000"),
                "change_value": Decimal("10"),
                "change_percent": Decimal("1"),
                "market_value": Decimal("1000000"),
                "turnover_percent": Decimal("3"),
                "advancers": 10,
                "decliners": 3,
                "leader_name": "示例证券",
                "leader_change_percent": Decimal("5"),
            },
        ]
    )
    repository = _repository(engine)

    snapshot = repository.get_published_snapshot(
        scheme=SectorScheme.EASTMONEY_INDUSTRY,
        trade_date=date(2026, 7, 27),
    )
    quote = repository.get_snapshot_quote(
        snapshot_id=snapshot_id,
        identifier=SectorIdentifier(SectorScheme.EASTMONEY_INDUSTRY, "BK0001"),
    )

    assert snapshot is not None
    assert snapshot.data_version == data_version
    assert quote is not None
    assert quote.rank is None
    assert quote.quote.name == "银行"


def test_historical_reference_reads_only_latest_prior_published_snapshot() -> None:
    """跨日质量只能比较目标日前 current published 快照，并按稳定代码返回市值原生值。"""
    snapshot_id = uuid4()
    engine = FakeEngine(
        [
            {
                "snapshot_id": snapshot_id,
                "trade_date": date(2026, 7, 26),
                "content_sha256": b"a" * 32,
            },
            [{"sector_code": "BK0001", "market_value": Decimal("1000000")}],
        ]
    )

    reference = _repository(engine).get_historical_reference(
        scheme=SectorScheme.EASTMONEY_INDUSTRY,
        before_trade_date=date(2026, 7, 27),
    )

    assert reference is not None
    assert reference.trade_date == date(2026, 7, 26)
    assert reference.market_values == {"BK0001": Decimal("1000000")}
    assert "trade_date < :before_trade_date" in engine.connection.statements[0]


def test_quarantine_inserts_evidence_without_superseding_or_publishing() -> None:
    """阻断质量候选应保存快照、行和规则证据，但不得关闭既有 publication 或创建新可见版本。"""
    run = SectorEodRun(
        run_id=uuid4(),
        lease_token=uuid4(),
        scheme=SectorScheme.EASTMONEY_INDUSTRY,
        trade_date=date(2026, 7, 27),
    )
    source_batch_id = uuid4()
    engine = FakeEngine(
        [
            {"run_id": run.run_id},
            {"source_batch_id": source_batch_id},
            [{"sector_key": 1, "sector_code": "BK0001"}],
            1,
        ]
    )
    quality = SectorEodQualityResult(
        rule_code="cross-day-content-stale",
        severity="blocking",
        passed=False,
        actual={"previousTradeDate": "2026-07-26"},
        threshold={"mustDiffer": "true"},
    )

    _repository(engine).store_quarantined_snapshot(
        scheme=run.scheme,
        trade_date=run.trade_date,
        source_cutoff_at=datetime(2026, 7, 27, 8, 15, tzinfo=UTC),
        observed_at=datetime(2026, 7, 27, 8, 20, tzinfo=UTC),
        quotes=(_quote("BK0001"),),
        provider_id="test-provider",
        source_payload_sha256="a" * 64,
        raw_uri="s3://test/raw.json",
        adapter_version="test-v1",
        schema_fingerprint="b" * 64,
        run=run,
        source_batch_id=source_batch_id,
        quality_results=(quality,),
    )

    statements = "\n".join(engine.connection.statements)
    assert "'quarantined'" in statements
    assert "INSERT INTO sector_eod_quality_result" in statements
    assert "INSERT INTO dataset_publication" not in statements
    assert "SET state = 'superseded'" not in statements


def test_shadow_candidate_keeps_consumer_publication_unchanged() -> None:
    """通过质量门的 shadow 只能保存 candidate 与证据，不能让消费者读取到候选版本。"""
    run = SectorEodRun(
        run_id=uuid4(),
        lease_token=uuid4(),
        scheme=SectorScheme.EASTMONEY_INDUSTRY,
        trade_date=date(2026, 7, 27),
    )
    source_batch_id = uuid4()
    engine = FakeEngine(
        [
            {"run_id": run.run_id},
            {"source_batch_id": source_batch_id},
            [{"sector_key": 1, "sector_code": "BK0001"}],
            None,
            None,
            None,
            1,
        ]
    )
    quality = SectorEodQualityResult(
        rule_code="schema",
        severity="blocking",
        passed=True,
        actual={"recordCount": 1},
        threshold={"maximumRecordCount": 2000},
    )

    stored = _repository(engine).store_shadow_snapshot(
        scheme=run.scheme,
        trade_date=run.trade_date,
        source_cutoff_at=datetime(2026, 7, 27, 8, 15, tzinfo=UTC),
        observed_at=datetime(2026, 7, 27, 8, 20, tzinfo=UTC),
        quotes=(_quote("BK0001"),),
        provider_id="test-provider",
        source_payload_sha256="a" * 64,
        raw_uri="s3://test/raw.json",
        adapter_version="test-v1",
        schema_fingerprint="b" * 64,
        run=run,
        source_batch_id=source_batch_id,
        quality_status="passed",
        quality_results=(quality,),
    )

    statements = "\n".join(engine.connection.statements)
    assert stored.inserted is True
    assert stored.snapshot.published_at is None
    assert "'candidate'" in statements
    assert "INSERT INTO dataset_publication" not in statements
    assert "SET state = 'superseded'" not in statements


def test_rollback_atomically_restores_a_superseded_passed_revision() -> None:
    """publication rollback 只能恢复历史通过版本，并保留当前错误 revision 与全部证据。"""
    current = {
        "snapshot_id": uuid4(),
        "data_version": uuid4(),
        "scheme": "eastmoney.industry",
        "trade_date": date(2026, 7, 27),
        "source_cutoff_at": datetime(2026, 7, 27, 8, 15, tzinfo=UTC),
        "observed_at": datetime(2026, 7, 27, 8, 30, tzinfo=UTC),
        "finality": "post_close_observation",
        "quality_status": "passed",
        "published_at": datetime(2026, 7, 27, 8, 31, tzinfo=UTC),
        "normalizer_version": "sector-eod-v1",
        "content_sha256": b"a" * 32,
    }
    target = {
        **current,
        "snapshot_id": uuid4(),
        "data_version": uuid4(),
        "observed_at": datetime(2026, 7, 27, 8, 20, tzinfo=UTC),
        "published_at": datetime(2026, 7, 27, 8, 21, tzinfo=UTC),
        "content_sha256": b"b" * 32,
    }
    engine = FakeEngine([current, target])

    restored = _repository(engine).rollback_published_snapshot(
        scheme=SectorScheme.EASTMONEY_INDUSTRY,
        trade_date=date(2026, 7, 27),
        revision=1,
    )

    statements = "\n".join(engine.connection.statements)
    assert restored.snapshot_id == target["snapshot_id"]
    assert restored.data_version == target["data_version"]
    assert "SET state = 'superseded'" in statements
    assert "SET state = 'published', superseded_at = NULL" in statements
    assert "UPDATE dataset_publication" in statements
    assert "DELETE" not in statements


def test_list_queued_runs_returns_only_recoverable_partition_identity() -> None:
    """自动 reaper 只能接收 scheme/date，不能因读取 lease 或 raw 字段而越过仓储边界。"""
    engine = FakeEngine(
        [
            [
                {"scheme": "eastmoney.industry", "trade_date": date(2026, 7, 27)},
                {"scheme": "eastmoney.concept", "trade_date": date(2026, 7, 27)},
            ]
        ]
    )

    queued = _repository(engine).list_queued_runs()

    assert [(item.scheme.value, item.trade_date) for item in queued] == [
        ("eastmoney.industry", date(2026, 7, 27)),
        ("eastmoney.concept", date(2026, 7, 27)),
    ]
    assert "WHERE status = 'queued'" in engine.connection.statements[0]


def _repository(engine: FakeEngine) -> SqlAlchemySectorEodRepository:
    """将本地引擎替身适配为仓储需要的数据库客户端。"""
    return SqlAlchemySectorEodRepository(DatabaseClient(engine=cast(Engine, engine)))


def _quote(code: str) -> SectorEodQuote:
    """构造覆盖质量门与摘要计算所需的完整单板块来源原生报价。"""
    return SectorEodQuote(
        identifier=SectorIdentifier(SectorScheme.EASTMONEY_INDUSTRY, code),
        name="银行",
        latest_value=Decimal("1000"),
        change_value=Decimal("10"),
        change_percent=Decimal("1"),
        market_value=Decimal("1000000"),
        turnover_percent=Decimal("3"),
        advancers=10,
        decliners=3,
        leader_name="示例证券",
        leader_change_percent=Decimal("5"),
    )


def _record_source_observation(*_args: object, **_kwargs: object) -> UUID:
    """返回新的来源批次 UUID，隔离仓储单测与共享账本 SQL 实现。"""
    return uuid4()


def _record_source_batch_with_fixed_id(
    source_batch_id: UUID,
) -> Callable[..., UUID]:
    """创建返回固定来源批次 UUID 的测试回调，便于断言插入引用。"""

    def record_source_observation(*_args: object, **_kwargs: object) -> UUID:
        """忽略输入并返回调用方预置的来源批次 UUID。"""
        return source_batch_id

    return record_source_observation
