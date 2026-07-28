"""资金流领域对象的强身份、方法学和完整性不变量测试。"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

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

_OBSERVED_AT = datetime(2026, 7, 24, 10, tzinfo=UTC)


def _methodology(
    *,
    status: str = "research",
    production_enabled: bool = False,
    scope_types: tuple[MoneyFlowScopeType, ...] = (MoneyFlowScopeType.EQUITY,),
    universe_ids: tuple[str, ...] = ("cn-a",),
    windows: tuple[tuple[MoneyFlowWindowType, int, str], ...] = (
        (MoneyFlowWindowType.DAILY_SOURCE, 1, "来源日序列"),
    ),
    buckets: tuple[MoneyFlowBucketDefinition, ...] = (
        MoneyFlowBucketDefinition(code="main", label="主力"),
    ),
    supported_measures: frozenset[MoneyFlowMeasure] = frozenset({MoneyFlowMeasure.NET_AMOUNT}),
    public_key: str = "fixture-money-flow",
) -> MoneyFlowMethodology:
    """构造可按单一不变量改写的有效方法学。"""
    return MoneyFlowMethodology(
        public_key=public_key,
        version="1",
        status=status,
        production_enabled=production_enabled,
        adapter_provider="fixture-adapter",
        upstream_source="fixture-source",
        source_dataset="fixture-dataset",
        semantic_family=MoneyFlowSemanticFamily.ORDER_SIZE,
        scope_types=scope_types,
        universe_ids=universe_ids,
        windows=windows,
        buckets=buckets,
        supported_measures=supported_measures,
        ratio_denominator="供应商成交额",
        direction_definition="供应商订单规模净流入",
        finality=MoneyFlowFinality.UNKNOWN,
        currency=None,
        raw_amount_unit="unknown",
        standard_amount_unit=None,
        conversion_version=None,
    )


def _equity_scope(symbol: str = "600000") -> MoneyFlowScope:
    """构造日期感知解析前的证券来源身份。"""
    return MoneyFlowScope(
        scope_type=MoneyFlowScopeType.EQUITY,
        exchange=Exchange.SSE,
        symbol=symbol,
    )


def _ranking_item(
    *,
    position: int = 1,
    scope: MoneyFlowScope | None = None,
    bucket: str = "main",
) -> MoneyFlowRankingItem:
    """构造带一个净额度量的供应商排行项。"""
    return MoneyFlowRankingItem(
        supplier_position=position,
        scope=scope or _equity_scope(),
        bucket=bucket,
        gross_inflow=None,
        gross_outflow=None,
        net_amount=Decimal("1"),
        net_ratio=None,
    )


def _ranking_snapshot(
    *,
    items: tuple[MoneyFlowRankingItem, ...] | None = None,
    is_complete: bool = False,
    completeness_basis: str = "sdk_returned",
) -> MoneyFlowRankingSnapshot:
    """构造未冒充完整分页的供应商快照。"""
    return MoneyFlowRankingSnapshot(
        target_trade_date=date(2026, 7, 24),
        observed_at=_OBSERVED_AT,
        source_cutoff_at=_OBSERVED_AT,
        scope_type=MoneyFlowScopeType.EQUITY,
        universe_id="cn-a",
        window_type=MoneyFlowWindowType.SUPPLIER_DAY,
        window_size=1,
        ranking_bucket="main",
        ranking_basis="supplier_reported_order",
        completeness_basis=completeness_basis,
        is_complete=is_complete,
        items=items or (_ranking_item(),),
    )


def test_bucket_definition_rejects_ambiguous_thresholds() -> None:
    """未知阈值不得被伪造，已知上下界也不得反向。"""
    with pytest.raises(ValueError, match="code and label"):
        MoneyFlowBucketDefinition(code="", label="主力")
    with pytest.raises(ValueError, match="range"):
        MoneyFlowBucketDefinition(
            code="main",
            label="主力",
            definition_status="documented",
            threshold_min=Decimal("2"),
            threshold_max=Decimal("1"),
        )
    with pytest.raises(ValueError, match="must not invent"):
        MoneyFlowBucketDefinition(
            code="main",
            label="主力",
            threshold_min=Decimal("1"),
        )


def test_methodology_rejects_invalid_identity_sets_and_production_state() -> None:
    """方法学必须具备唯一范围、universe、分桶和受控生产状态。"""
    with pytest.raises(ValueError, match="must not be blank"):
        _methodology(public_key=" ")
    with pytest.raises(ValueError, match="status"):
        _methodology(status="draft")
    with pytest.raises(ValueError, match="only validated"):
        _methodology(production_enabled=True)
    with pytest.raises(ValueError, match="scopes"):
        _methodology(scope_types=())
    with pytest.raises(ValueError, match="scopes"):
        _methodology(scope_types=(MoneyFlowScopeType.EQUITY, MoneyFlowScopeType.EQUITY))
    with pytest.raises(ValueError, match="universes"):
        _methodology(universe_ids=())
    with pytest.raises(ValueError, match="universes"):
        _methodology(universe_ids=("cn-a", "cn-a"))
    with pytest.raises(ValueError, match="requires windows"):
        _methodology(windows=())
    with pytest.raises(ValueError, match="requires windows"):
        _methodology(buckets=())
    with pytest.raises(ValueError, match="requires windows"):
        _methodology(supported_measures=frozenset())
    duplicate_bucket = MoneyFlowBucketDefinition(code="main", label="重复")
    with pytest.raises(ValueError, match="bucket codes"):
        _methodology(
            buckets=(
                MoneyFlowBucketDefinition(code="main", label="主力"),
                duplicate_bucket,
            )
        )


def test_methodology_rejects_window_semantic_drift() -> None:
    """日序列、单日快照和滚动快照的窗口大小不得混用。"""
    invalid_windows = (
        ((MoneyFlowWindowType.DAILY_SOURCE, 0, "来源"),),
        ((MoneyFlowWindowType.DAILY_SOURCE, 2, "来源"),),
        ((MoneyFlowWindowType.SUPPLIER_DAY, 2, "今日"),),
        ((MoneyFlowWindowType.SUPPLIER_ROLLING, 1, "滚动"),),
        ((MoneyFlowWindowType.SUPPLIER_ROLLING, 3, ""),),
    )
    for windows in invalid_windows:
        with pytest.raises(ValueError, match="window"):
            _methodology(windows=windows)


def test_scope_requires_exactly_one_complete_identity() -> None:
    """证券、板块和市场 scope 必须各自携带一组且仅一组身份。"""
    with pytest.raises(ValueError, match="exactly one"):
        MoneyFlowScope(
            scope_type=MoneyFlowScopeType.EQUITY,
            exchange=Exchange.SSE,
            symbol="600000",
            market_code="cn-a",
        )
    with pytest.raises(ValueError, match="exchange and symbol"):
        MoneyFlowScope(scope_type=MoneyFlowScopeType.EQUITY, symbol="600000")
    with pytest.raises(ValueError, match="six digits"):
        _equity_scope("60000A")
    with pytest.raises(ValueError, match="scheme and code"):
        MoneyFlowScope(
            scope_type=MoneyFlowScopeType.SECTOR,
            sector_scheme="eastmoney.industry",
        )
    with pytest.raises(ValueError, match="market code"):
        MoneyFlowScope(scope_type=MoneyFlowScopeType.MARKET, market_code="")


def test_observation_and_ranking_items_require_valid_measures() -> None:
    """事实时间、gross 符号、位置和至少一个度量必须可验证。"""
    valid = MoneyFlowDailyObservation(
        scope=_equity_scope(),
        bucket="main",
        trade_date=date(2026, 7, 24),
        observed_at=_OBSERVED_AT,
        gross_inflow=None,
        gross_outflow=None,
        net_amount=Decimal("1"),
        net_ratio=None,
    )
    with pytest.raises(ValueError, match="bucket"):
        replace(valid, bucket="")
    with pytest.raises(ValueError, match="timezone"):
        replace(valid, observed_at=datetime(2026, 7, 24, 10))
    with pytest.raises(ValueError, match="inflow"):
        replace(valid, gross_inflow=Decimal("-1"))
    with pytest.raises(ValueError, match="outflow"):
        replace(valid, gross_outflow=Decimal("-1"))
    with pytest.raises(ValueError, match="at least one"):
        replace(
            valid,
            gross_inflow=None,
            gross_outflow=None,
            net_amount=None,
            net_ratio=None,
        )
    with pytest.raises(ValueError, match="position and bucket"):
        replace(_ranking_item(), supplier_position=0)
    with pytest.raises(ValueError, match="at least one"):
        replace(
            _ranking_item(),
            gross_inflow=None,
            gross_outflow=None,
            net_amount=None,
            net_ratio=None,
        )


def test_ranking_snapshot_enforces_position_identity_and_completeness_evidence() -> None:
    """同一位置只绑定一个 scope，完整排行必须有上游 total 证据。"""
    with pytest.raises(ValueError, match="timestamps"):
        replace(_ranking_snapshot(), observed_at=datetime(2026, 7, 24, 10))
    with pytest.raises(ValueError, match="universe and items"):
        replace(_ranking_snapshot(), universe_id="")
    duplicate = (_ranking_item(), _ranking_item())
    with pytest.raises(ValueError, match="keys must be unique"):
        _ranking_snapshot(items=duplicate)
    mixed_scope = (
        _ranking_item(),
        _ranking_item(
            scope=MoneyFlowScope(
                scope_type=MoneyFlowScopeType.EQUITY,
                exchange=Exchange.SZSE,
                symbol="000001",
            ),
            bucket="large",
        ),
    )
    with pytest.raises(ValueError, match="one scope"):
        _ranking_snapshot(items=mixed_scope)
    with pytest.raises(ValueError, match="verified upstream total"):
        _ranking_snapshot(is_complete=True)
    complete = _ranking_snapshot(
        is_complete=True,
        completeness_basis="upstream_total_verified",
    )
    assert complete.is_complete is True
