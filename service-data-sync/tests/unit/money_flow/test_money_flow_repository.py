"""资金流写仓储的质量门禁、修订计数、哈希与 publication 测试。"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, cast
from uuid import UUID

import pytest

from service_data_sync.application.ports.money_flow import MoneyFlowSourceObservation
from service_data_sync.domain.equity import Exchange
from service_data_sync.domain.money_flow import (
    MoneyFlowBucketDefinition,
    MoneyFlowDailyObservation,
    MoneyFlowFinality,
    MoneyFlowMeasure,
    MoneyFlowMethodology,
    MoneyFlowRankingItem,
    MoneyFlowRankingSnapshot,
    MoneyFlowScope,
    MoneyFlowScopeType,
    MoneyFlowSemanticFamily,
    MoneyFlowWindowType,
)
from service_data_sync.infrastructure.database.connection import DatabaseClient
from service_data_sync.infrastructure.persistence import money_flow_repository
from service_data_sync.infrastructure.persistence.equity_identity_resolver import (
    EquityIdentityWriteConflictError,
)

_METHOD_VERSION_ID = UUID("00000000-0000-4000-8000-000000000120")
_BUCKET_ID = UUID("00000000-0000-4000-8000-000000000121")
_UNIVERSE_ID = UUID("00000000-0000-4000-8000-000000000122")
_SERIES_ID = UUID("00000000-0000-4000-8000-000000000123")
_SOURCE_BATCH_ID = UUID("00000000-0000-4000-8000-000000000124")
_DATA_VERSION = UUID("00000000-0000-4000-8000-000000000125")
_OBSERVED_AT = datetime(2026, 7, 24, 10, tzinfo=UTC)


class FakeResult:
    """提供写仓储少量读取分支所需的 SQLAlchemy 结果接口。"""

    def __init__(
        self,
        *,
        scalar: object | None = None,
        mapping: Mapping[str, object] | None = None,
    ) -> None:
        """保存可选标量和映射。"""
        self._scalar = scalar
        self._mapping = mapping

    def scalar_one_or_none(self) -> object | None:
        """返回可选 current publication 标量。"""
        return self._scalar

    def mappings(self) -> FakeResult:
        """维持链式映射接口。"""
        return self

    def one_or_none(self) -> Mapping[str, object] | None:
        """返回可选 current 行。"""
        return self._mapping


class FakeSession:
    """记录写入表达式，并按队列返回读取结果。"""

    def __init__(self, results: Sequence[FakeResult] = ()) -> None:
        """复制结果队列。"""
        self.results = list(results)
        self.statements: list[object] = []

    def execute(self, statement: object) -> FakeResult:
        """记录 SQLAlchemy 表达式并返回下一结果。"""
        self.statements.append(statement)
        return self.results.pop(0) if self.results else FakeResult()


class FakeDatabase:
    """向写仓储提供单一原子事务。"""

    def __init__(self, session: FakeSession) -> None:
        """保存受控会话。"""
        self.session_value = session

    @contextmanager
    def transaction(self) -> Iterator[FakeSession]:
        """模拟事务上下文。"""
        yield self.session_value


def _repository(
    session: FakeSession,
) -> money_flow_repository.SqlAlchemyMoneyFlowRepository:
    """构造使用假数据库的真实写仓储。"""
    database = cast(DatabaseClient, cast(Any, FakeDatabase(session)))
    return money_flow_repository.SqlAlchemyMoneyFlowRepository(database)


def _methodology(
    *,
    production_enabled: bool = True,
    scope_types: tuple[MoneyFlowScopeType, ...] = (MoneyFlowScopeType.EQUITY,),
    universe_ids: tuple[str, ...] = ("cn-a",),
    bucket: str = "main",
    supported_measures: frozenset[MoneyFlowMeasure] = frozenset({MoneyFlowMeasure.NET_AMOUNT}),
) -> MoneyFlowMethodology:
    """构造可发布的最小方法学。"""
    return MoneyFlowMethodology(
        public_key="fixture-money-flow",
        version="1",
        status="validated" if production_enabled else "research",
        production_enabled=production_enabled,
        adapter_provider="fixture-adapter",
        upstream_source="fixture-source",
        source_dataset="fixture-dataset",
        semantic_family=MoneyFlowSemanticFamily.ORDER_SIZE,
        scope_types=scope_types,
        universe_ids=universe_ids,
        windows=((MoneyFlowWindowType.DAILY_SOURCE, 1, "来源日序列"),),
        buckets=(MoneyFlowBucketDefinition(code=bucket, label="分桶"),),
        supported_measures=supported_measures,
        ratio_denominator="供应商成交额",
        direction_definition="供应商订单规模净流入",
        finality=MoneyFlowFinality.UNKNOWN,
        currency=None,
        raw_amount_unit="unknown",
        standard_amount_unit=None,
        conversion_version=None,
    )


def _scope(
    *,
    scope_type: MoneyFlowScopeType = MoneyFlowScopeType.EQUITY,
) -> MoneyFlowScope:
    """构造一种唯一来源 scope。"""
    if scope_type is MoneyFlowScopeType.EQUITY:
        return MoneyFlowScope(
            scope_type=scope_type,
            exchange=Exchange.SSE,
            symbol="600000",
            name="浦发银行",
        )
    if scope_type is MoneyFlowScopeType.SECTOR:
        return MoneyFlowScope(
            scope_type=scope_type,
            sector_scheme="eastmoney.industry",
            sector_code="BK0475",
            name="银行",
        )
    return MoneyFlowScope(
        scope_type=scope_type,
        market_code="cn-a",
        name="A 股",
    )


def _observation(
    *,
    trade_date: date = date(2026, 7, 24),
    scope: MoneyFlowScope | None = None,
    gross_inflow: Decimal | None = None,
    net_amount: Decimal | None = Decimal("1"),
) -> MoneyFlowDailyObservation:
    """构造一个固定分桶日观察。"""
    return MoneyFlowDailyObservation(
        scope=scope or _scope(),
        bucket="main",
        trade_date=trade_date,
        observed_at=_OBSERVED_AT,
        gross_inflow=gross_inflow,
        gross_outflow=None,
        net_amount=net_amount,
        net_ratio=None,
    )


def _source() -> MoneyFlowSourceObservation:
    """构造已归档 raw evidence 血缘。"""
    return MoneyFlowSourceObservation(
        provider_id="fixture-provider",
        capability="money_flow.order_size.daily.equity.raw",
        source_payload_sha256="a" * 64,
        raw_uri="s3://private/raw/evidence.json",
        observed_at=_OBSERVED_AT,
        upstream_source="fixture-source",
        adapter_version="fixture-v1",
        schema_fingerprint="b" * 64,
    )


def _snapshot(
    *,
    scope: MoneyFlowScope | None = None,
    is_complete: bool = True,
) -> MoneyFlowRankingSnapshot:
    """构造一个有上游 total 证据的供应商排行。"""
    ranking_scope = scope or _scope()
    return MoneyFlowRankingSnapshot(
        target_trade_date=date(2026, 7, 24),
        observed_at=_OBSERVED_AT,
        source_cutoff_at=_OBSERVED_AT,
        scope_type=ranking_scope.scope_type,
        universe_id="cn-a",
        window_type=MoneyFlowWindowType.SUPPLIER_DAY,
        window_size=1,
        ranking_bucket="main",
        ranking_basis="supplier_reported_order",
        completeness_basis=("upstream_total_verified" if is_complete else "sdk_returned"),
        is_complete=is_complete,
        items=(
            MoneyFlowRankingItem(
                supplier_position=1,
                scope=ranking_scope,
                bucket="main",
                gross_inflow=None,
                gross_outflow=None,
                net_amount=Decimal("1"),
                net_ratio=None,
            ),
        ),
    )


def _patch_harness(
    monkeypatch: pytest.MonkeyPatch,
    repository: money_flow_repository.SqlAlchemyMoneyFlowRepository,
    *,
    outcomes: Sequence[str] = (),
    resolved_identity: int | None = 42,
    current_ranking: Mapping[str, object] | None = None,
    preflight_conflict: bool = False,
) -> dict[str, object]:
    """替换独立 SQL 细节，保留真实发布编排和质量决策。"""
    state: dict[str, object] = {
        "outcomes": list(outcomes),
        "quality": [],
        "published": [],
    }

    def ensure_methodology(
        _: object,
        methodology: MoneyFlowMethodology,
        *,
        now: datetime,
    ) -> money_flow_repository._StoredMethodology:
        """返回与测试分桶一致的冻结方法学身份。"""
        assert now.tzinfo is not None
        return money_flow_repository._StoredMethodology(
            version_id=_METHOD_VERSION_ID,
            bucket_ids={methodology.buckets[0].code: _BUCKET_ID},
        )

    def record_source(
        _: object,
        *,
        source: MoneyFlowSourceObservation,
        now: datetime,
        run_id: UUID | None,
        partition_key: str | None,
    ) -> UUID:
        """验证 raw evidence 已先于 canonical 写入。"""
        assert source.raw_uri.startswith("s3://private/")
        assert now.tzinfo is not None
        assert run_id is None
        assert partition_key is None
        return _SOURCE_BATCH_ID

    def preflight(
        _: object,
        *,
        observations: Sequence[MoneyFlowDailyObservation],
        known_at: datetime,
    ) -> dict[MoneyFlowScope, int]:
        """返回日期感知证券解析，或注入代码复用冲突。"""
        assert known_at.tzinfo is not None
        if preflight_conflict:
            raise EquityIdentityWriteConflictError("identity boundary")
        return {item.scope: 42 for item in observations}

    def ensure_universe(_: object, **__: object) -> UUID:
        """返回冻结 universe 版本。"""
        return _UNIVERSE_ID

    def resolve_scope(_: object, __: MoneyFlowScope, **___: object) -> int | None:
        """返回板块或市场解析结果。"""
        return resolved_identity

    def ensure_series(_: object, **__: object) -> UUID:
        """返回固定日序列身份。"""
        return _SERIES_ID

    def write_revision(_: object, **__: object) -> str:
        """按队列返回 inserted、revised 或 unchanged。"""
        values = cast(list[str], state["outcomes"])
        return values.pop(0)

    def record_quality(_: object, **kwargs: object) -> None:
        """记录质量拒绝原因。"""
        cast(list[dict[str, object]], state["quality"]).append(dict(kwargs))

    def publish_dataset(_: object, **kwargs: object) -> UUID:
        """记录 publication 并返回固定 dataVersion。"""
        cast(list[dict[str, object]], state["published"]).append(dict(kwargs))
        return _DATA_VERSION

    def resolve_ranking(
        _: object,
        *,
        snapshot: MoneyFlowRankingSnapshot,
        known_at: datetime,
    ) -> (
        dict[
            int,
            tuple[MoneyFlowScope, int, tuple[MoneyFlowRankingItem, ...]],
        ]
        | None
    ):
        """按供应商位置返回唯一 canonical identity。"""
        assert known_at.tzinfo is not None
        if resolved_identity is None:
            return None
        return {
            1: (
                snapshot.items[0].scope,
                resolved_identity,
                snapshot.items,
            )
        }

    def current(_: object, **__: object) -> Mapping[str, object] | None:
        """返回受控 current ranking。"""
        return current_ranking

    monkeypatch.setattr(repository, "_ensure_methodology", ensure_methodology)
    monkeypatch.setattr(money_flow_repository, "_record_source", record_source)
    monkeypatch.setattr(repository, "_preflight_equity_identity_batches", preflight)
    monkeypatch.setattr(repository, "_ensure_universe", ensure_universe)
    monkeypatch.setattr(repository, "_resolve_scope", resolve_scope)
    monkeypatch.setattr(repository, "_ensure_series", ensure_series)
    monkeypatch.setattr(repository, "_write_daily_revision", write_revision)
    monkeypatch.setattr(repository, "_record_quality", record_quality)
    monkeypatch.setattr(repository, "_publish_dataset", publish_dataset)
    monkeypatch.setattr(repository, "_resolve_ranking_items", resolve_ranking)
    monkeypatch.setattr(repository, "_current_ranking", current)
    return state


def test_publish_daily_counts_insert_revision_noop_and_publishes_latest_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一序列只为变化内容发布，并以最大事实日推进 publication。"""
    session = FakeSession()
    repository = _repository(session)
    state = _patch_harness(
        monkeypatch,
        repository,
        outcomes=("inserted", "revised", "unchanged"),
    )
    observations = (
        _observation(trade_date=date(2026, 7, 22)),
        _observation(trade_date=date(2026, 7, 24)),
        _observation(trade_date=date(2026, 7, 23)),
    )

    result = repository.publish_daily(
        methodology=_methodology(),
        observations=observations,
        source=_source(),
    )

    assert result.inserted_count == 1
    assert result.revised_count == 1
    assert result.unchanged_count == 1
    assert result.data_version == _DATA_VERSION
    assert result.published is True
    publications = cast(list[dict[str, object]], state["published"])
    assert publications[0]["effective_as_of"] == date(2026, 7, 24)


def test_publish_daily_rejects_bad_batch_and_unsupported_measures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """空批、混合 scope、未支持度量和全缺受支持度量均不得写库。"""
    repository = _repository(FakeSession())
    with pytest.raises(ValueError, match="must not be empty"):
        repository.publish_daily(
            methodology=_methodology(),
            observations=(),
            source=_source(),
        )
    with pytest.raises(ValueError, match="exactly one scope"):
        repository.publish_daily(
            methodology=_methodology(),
            observations=(
                _observation(),
                _observation(scope=_scope(scope_type=MoneyFlowScopeType.MARKET)),
            ),
            source=_source(),
        )
    with pytest.raises(ValueError, match="gross_inflow"):
        repository.publish_daily(
            methodology=_methodology(),
            observations=(_observation(gross_inflow=Decimal("1")),),
            source=_source(),
        )
    with pytest.raises(ValueError, match="entirely missing"):
        repository.publish_daily(
            methodology=_methodology(
                supported_measures=frozenset(
                    {
                        MoneyFlowMeasure.GROSS_INFLOW,
                        MoneyFlowMeasure.NET_AMOUNT,
                    }
                )
            ),
            observations=(
                _observation(
                    gross_inflow=Decimal("1"),
                    net_amount=None,
                ),
            ),
            source=_source(),
        )
    assert monkeypatch is not None


def test_publish_daily_fails_closed_on_identity_boundary_or_unresolved_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """跨代码复用边界和板块零解均保留 raw、记录质量且不 publication。"""
    conflict_repository = _repository(FakeSession())
    conflict_state = _patch_harness(
        monkeypatch,
        conflict_repository,
        preflight_conflict=True,
    )
    conflict = conflict_repository.publish_daily(
        methodology=_methodology(),
        observations=(_observation(),),
        source=_source(),
    )
    assert conflict.quality_status == "partial"
    assert conflict.published is False
    assert (
        cast(list[dict[str, object]], conflict_state["quality"])[0]["rule_code"]
        == "identity-resolution"
    )

    unresolved_repository = _repository(FakeSession())
    unresolved_state = _patch_harness(
        monkeypatch,
        unresolved_repository,
        resolved_identity=None,
    )
    sector_scope = _scope(scope_type=MoneyFlowScopeType.SECTOR)
    unresolved = unresolved_repository.publish_daily(
        methodology=_methodology(
            scope_types=(MoneyFlowScopeType.SECTOR,),
            universe_ids=("eastmoney-industry",),
        ),
        observations=(_observation(scope=sector_scope),),
        source=_source(),
    )
    assert unresolved.quality_status == "partial"
    assert unresolved.inserted_count == 0
    assert len(cast(list[dict[str, object]], unresolved_state["quality"])) == 1


def test_publish_ranking_rejects_unproven_completeness_and_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SDK 合并表和任何 scope 零解都不能进入 canonical ranking。"""
    incomplete_repository = _repository(FakeSession())
    incomplete_state = _patch_harness(monkeypatch, incomplete_repository)
    incomplete = incomplete_repository.publish_ranking(
        methodology=_methodology(),
        snapshot=_snapshot(is_complete=False),
        source=_source(),
    )
    assert incomplete.quality_status == "partial"
    assert (
        cast(list[dict[str, object]], incomplete_state["quality"])[0]["rule_code"]
        == "ranking-completeness"
    )

    unresolved_repository = _repository(FakeSession())
    unresolved_state = _patch_harness(
        monkeypatch,
        unresolved_repository,
        resolved_identity=None,
    )
    unresolved = unresolved_repository.publish_ranking(
        methodology=_methodology(),
        snapshot=_snapshot(),
        source=_source(),
    )
    assert unresolved.quality_status == "partial"
    assert (
        cast(list[dict[str, object]], unresolved_state["quality"])[0]["rule_code"]
        == "identity-resolution"
    )


def test_publish_ranking_inserts_revises_and_noops_by_canonical_hash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """排行哈希相同则 no-op，变化则 supersede，首次写入保留供应商位置。"""
    snapshot = _snapshot()
    resolved = {1: (snapshot.items[0].scope, 42, snapshot.items)}
    business_hash = money_flow_repository._ranking_hash(snapshot, resolved)

    inserted_session = FakeSession()
    inserted_repository = _repository(inserted_session)
    _patch_harness(monkeypatch, inserted_repository)
    inserted = inserted_repository.publish_ranking(
        methodology=_methodology(),
        snapshot=snapshot,
        source=_source(),
    )
    assert inserted.inserted_count == 1
    assert inserted.data_version == _DATA_VERSION
    assert len(inserted_session.statements) == 4

    noop_session = FakeSession([FakeResult(scalar=_DATA_VERSION)])
    noop_repository = _repository(noop_session)
    _patch_harness(
        monkeypatch,
        noop_repository,
        current_ranking={
            "snapshot_id": UUID("00000000-0000-4000-8000-000000000126"),
            "revision": 1,
            "business_hash": business_hash,
            "quality_status": "passed",
        },
    )
    noop = noop_repository.publish_ranking(
        methodology=_methodology(),
        snapshot=snapshot,
        source=_source(),
    )
    assert noop.unchanged_count == 1
    assert noop.data_version == _DATA_VERSION

    revised_session = FakeSession()
    revised_repository = _repository(revised_session)
    _patch_harness(
        monkeypatch,
        revised_repository,
        current_ranking={
            "snapshot_id": UUID("00000000-0000-4000-8000-000000000127"),
            "revision": 2,
            "business_hash": "c" * 64,
            "quality_status": "passed",
        },
    )
    revised = revised_repository.publish_ranking(
        methodology=_methodology(),
        snapshot=snapshot,
        source=_source(),
    )
    assert revised.revised_count == 1
    assert revised.inserted_count == 0
    assert len(revised_session.statements) == 5


def test_repository_helpers_freeze_methodology_hashes_and_partition_keys() -> None:
    """方法学不可变列、universe、精确小数哈希与分区键必须稳定。"""
    methodology = _methodology()
    values = money_flow_repository._methodology_version_values(
        methodology,
        methodology_id=UUID("00000000-0000-4000-8000-000000000128"),
        version_id=_METHOD_VERSION_ID,
        now=_OBSERVED_AT,
    )
    money_flow_repository._assert_immutable_methodology(dict(values), values)
    changed = dict(values)
    changed["source_dataset"] = "changed"
    with pytest.raises(RuntimeError, match="immutable"):
        money_flow_repository._assert_immutable_methodology(changed, values)

    assert (
        money_flow_repository._universe_for_scope(
            methodology,
            MoneyFlowScopeType.EQUITY,
        )
        == "cn-a"
    )
    sector_methodology = _methodology(
        scope_types=(MoneyFlowScopeType.SECTOR,),
        universe_ids=("cn-a", "eastmoney-industry"),
    )
    assert (
        money_flow_repository._universe_for_scope(
            sector_methodology,
            MoneyFlowScopeType.SECTOR,
        )
        == "eastmoney-industry"
    )
    single = _methodology(universe_ids=("provider-page",))
    assert (
        money_flow_repository._universe_for_scope(single, MoneyFlowScopeType.EQUITY)
        == "provider-page"
    )
    ambiguous = _methodology(universe_ids=("one", "two"))
    with pytest.raises(ValueError, match="unambiguous"):
        money_flow_repository._universe_for_scope(
            ambiguous,
            MoneyFlowScopeType.EQUITY,
        )

    observation = _observation(net_amount=Decimal("1.2300"))
    assert money_flow_repository._daily_hash(observation) == (
        money_flow_repository._daily_hash(observation)
    )
    assert (
        money_flow_repository._daily_partition_key(methodology, observation.scope)
        == "fixture-money-flow/1/daily/equity/SSE/600000"
    )
    snapshot = _snapshot()
    assert money_flow_repository._ranking_partition_key(methodology, snapshot).endswith(
        "/supplier_day/1/main/2026-07-24"
    )
