"""平台派生财务公式、缺失语义与发布隔离的单元测试。"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

import pytest

from service_data_sync.application.financial.derived import (
    FinancialDerivedMetricService,
    derive_metrics,
)
from service_data_sync.application.ports.financial_derived import (
    DerivedFinancialMetricInput,
    FinancialDerivationSnapshot,
    FinancialDerivationUnavailable,
    FinancialDerivedPublication,
    ReportedFinancialFact,
)
from service_data_sync.domain.equity import Exchange


class RecordingDerivationRepository:
    """保存固定输入快照并记录应用层交给 publication 的完整派生结果。"""

    def __init__(self, snapshot: FinancialDerivationSnapshot) -> None:
        """初始化固定快照和空发布调用记录。"""
        self.snapshot = snapshot
        self.published_metrics: tuple[DerivedFinancialMetricInput, ...] | None = None

    def load_inputs(self, *, exchange: Exchange, symbol: str) -> FinancialDerivationSnapshot:
        """验证证券请求后返回同一已冻结报表 publication。"""
        assert exchange is Exchange.SSE
        assert symbol == "600519"
        return self.snapshot

    def publish(
        self,
        *,
        snapshot: FinancialDerivationSnapshot,
        metrics: Sequence[DerivedFinancialMetricInput],
        derivation_run_id: UUID,
        computed_at: datetime,
        before_final_publication: Callable[[], None] | None = None,
    ) -> FinancialDerivedPublication:
        """记录空或非空目标集，模拟仓储原子推进独立派生 publication。"""
        assert snapshot is self.snapshot
        assert derivation_run_id == UUID("70000000-0000-4000-8000-000000000001")
        assert computed_at == datetime(2026, 7, 28, 9, tzinfo=UTC)
        if before_final_publication is not None:
            before_final_publication()
        self.published_metrics = tuple(metrics)
        return FinancialDerivedPublication(
            data_version=UUID("80000000-0000-4000-8000-000000000001"),
            inserted_count=len(metrics),
            unchanged_count=0,
            row_count=len(metrics),
        )


def test_formula_computes_single_quarter_and_ttm_with_fixed_input_lineage() -> None:
    """累计利润表序列应生成单季差分和 TTM 滚动值，并保留参与公式的输入顺序。"""
    facts = (
        _fact(date(2024, 3, 31), "100"),
        _fact(date(2024, 6, 30), "250"),
        _fact(date(2024, 9, 30), "400"),
        _fact(date(2024, 12, 31), "600"),
        _fact(date(2025, 3, 31), "180"),
        _fact(date(2025, 6, 30), "380"),
    )

    metrics, skipped = derive_metrics(facts)
    indexed = {(metric.report_period, metric.period_basis): metric for metric in metrics}

    assert indexed[(date(2024, 6, 30), "SINGLE_QUARTER")].value == Decimal("150")
    assert indexed[(date(2024, 12, 31), "SINGLE_QUARTER")].value == Decimal("200")
    assert indexed[(date(2024, 12, 31), "TTM")].value == Decimal("600")
    assert indexed[(date(2025, 3, 31), "TTM")].value == Decimal("680")
    assert indexed[(date(2025, 6, 30), "TTM")].value == Decimal("730")
    assert tuple(fact.report_period for fact in indexed[(date(2025, 6, 30), "TTM")].inputs) == (
        date(2025, 6, 30),
        date(2024, 12, 31),
        date(2024, 6, 30),
    )
    assert indexed[(date(2025, 6, 30), "TTM")].formula_version == 1
    assert skipped == 3


def test_formula_omits_output_when_any_dependency_is_missing() -> None:
    """缺少上季度、上年年报或上年同期时必须省略结果，禁止用零补齐。"""
    metrics, skipped = derive_metrics((_fact(date(2025, 6, 30), "380"),))

    assert metrics == ()
    assert skipped == 2


def test_formula_rejects_incomparable_units_before_arithmetic() -> None:
    """同一指标跨期单位或币种变化时必须失败，禁止无换算规则直接相减。"""
    facts = (
        _fact(date(2025, 3, 31), "180"),
        _fact(date(2025, 6, 30), "380", unit="ten_thousand_yuan"),
    )

    with pytest.raises(FinancialDerivationUnavailable, match="incomparable units"):
        derive_metrics(facts)


def test_formula_rejects_incomplete_currency_null_semantics() -> None:
    """币种与受控空值原因不满足严格二选一时，禁止生成不可审计派生值。"""
    facts = (
        _fact(
            date(2025, 3, 31),
            "180",
            currency=None,
            currency_null_reason=None,
        ),
    )

    with pytest.raises(FinancialDerivationUnavailable, match="currency semantics"):
        derive_metrics(facts)


def test_service_publishes_empty_target_to_close_stale_derived_rows() -> None:
    """当前报表没有完整依赖时仍发布空目标集，使仓储能关闭旧派生值。"""
    snapshot = FinancialDerivationSnapshot(
        data_version=UUID("10000000-0000-4000-8000-000000000001"),
        security_id=8,
        methodology_id=UUID("20000000-0000-4000-8000-000000000001"),
        effective_as_of=date(2026, 3, 31),
        knowledge_cutoff=datetime(2026, 4, 28, 8, tzinfo=UTC),
        facts=(),
    )
    repository = RecordingDerivationRepository(snapshot)

    result = FinancialDerivedMetricService(repository=repository).derive(
        exchange=Exchange.SSE,
        symbol="600519",
        derivation_run_id=UUID("70000000-0000-4000-8000-000000000001"),
        computed_at=datetime(2026, 7, 28, 9, tzinfo=UTC),
    )

    assert repository.published_metrics == ()
    assert result.computed_count == 0
    assert result.publication.row_count == 0


def _fact(
    report_period: date,
    value: str,
    *,
    unit: str = "yuan",
    currency: str | None = "CNY",
    currency_null_reason: str | None = None,
) -> ReportedFinancialFact:
    """构造一个带完整 revision、来源批次和时间血缘的累计营业收入事实。"""
    suffix = report_period.strftime("%Y%m%d")
    return ReportedFinancialFact(
        report_period=report_period,
        statement_scope="CONSOLIDATED",
        metric_id=1,
        metric_code="statement.income_statement.total-operate-income",
        value=Decimal(value),
        unit=unit,
        currency=currency,
        currency_null_reason=currency_null_reason,
        revision_id=UUID(f"30000000-0000-4000-8000-{suffix}0000"),
        source_batch_id=UUID(f"40000000-0000-4000-8000-{suffix}0000"),
        effective_from=report_period,
        known_from=datetime(
            report_period.year,
            report_period.month,
            report_period.day,
            tzinfo=UTC,
        ),
        observed_at=datetime(
            report_period.year,
            report_period.month,
            report_period.day,
            tzinfo=UTC,
        ),
    )
