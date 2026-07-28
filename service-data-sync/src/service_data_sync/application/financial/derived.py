"""基于已发布三表事实计算版本化单季与 TTM 指标。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from service_data_sync.application.ports.financial_derived import (
    DerivedFinancialMetricInput,
    FinancialDerivationRepository,
    FinancialDerivationUnavailable,
    FinancialDerivedPublication,
    ReportedFinancialFact,
)
from service_data_sync.domain.equity import Exchange

FORMULA_VERSION = 1


@dataclass(frozen=True, slots=True)
class FormulaSpec:
    """固化一个累计报表事实到单季和 TTM 输出的映射。"""

    input_code: str
    single_quarter_code: str
    single_quarter_label: str
    ttm_code: str
    ttm_label: str


FORMULAS = (
    FormulaSpec(
        input_code="statement.income_statement.total-operate-income",
        single_quarter_code="platform.operating_revenue.single_quarter",
        single_quarter_label="营业收入（单季）",
        ttm_code="platform.operating_revenue.ttm",
        ttm_label="营业收入（TTM）",
    ),
    FormulaSpec(
        input_code="statement.income_statement.parent-netprofit",
        single_quarter_code="platform.net_profit_parent.single_quarter",
        single_quarter_label="归母净利润（单季）",
        ttm_code="platform.net_profit_parent.ttm",
        ttm_label="归母净利润（TTM）",
    ),
)
_CURRENCY_NULL_REASONS = frozenset({"NOT_APPLICABLE", "UNKNOWN_SOURCE", "MIXED_CURRENCIES"})


@dataclass(frozen=True, slots=True)
class FinancialDerivationResult:
    """汇总一次派生运行的发布版本、产出和因输入缺失跳过数量。"""

    publication: FinancialDerivedPublication
    computed_count: int
    skipped_count: int


class FinancialDerivedMetricService:
    """只消费已发布报表事实，按固定公式生成独立平台派生 publication。"""

    def __init__(self, *, repository: FinancialDerivationRepository) -> None:
        """保存派生输入与发布端口，不依赖 provider 或 ORM。"""
        self._repository = repository

    def derive(
        self,
        *,
        exchange: Exchange,
        symbol: str,
        derivation_run_id: UUID,
        computed_at: datetime,
    ) -> FinancialDerivationResult:
        """冻结当前报表版本、计算公式并在来源版本未变化时原子发布。"""
        if computed_at.tzinfo is None:
            raise ValueError("computed_at must include a timezone")
        snapshot = self._repository.load_inputs(exchange=exchange, symbol=symbol)
        metrics, skipped_count = derive_metrics(snapshot.facts)
        publication = self._repository.publish(
            snapshot=snapshot,
            metrics=metrics,
            derivation_run_id=derivation_run_id,
            computed_at=computed_at,
        )
        return FinancialDerivationResult(
            publication=publication,
            computed_count=len(metrics),
            skipped_count=skipped_count,
        )


def derive_metrics(
    facts: tuple[ReportedFinancialFact, ...],
) -> tuple[tuple[DerivedFinancialMetricInput, ...], int]:
    """按来源代码和报表范围计算单季/TTM；缺一项即省略而不以零替代。"""
    indexed = _index_facts(facts)
    outputs: list[DerivedFinancialMetricInput] = []
    skipped_count = 0
    scopes = sorted({scope for _, scope in indexed})
    for formula in FORMULAS:
        for scope in scopes:
            series = indexed.get((formula.input_code, scope), {})
            for report_period in sorted(series):
                single_inputs = _single_quarter_inputs(series, report_period)
                if single_inputs is None:
                    skipped_count += 1
                else:
                    outputs.append(
                        _derived_metric(
                            code=formula.single_quarter_code,
                            label=formula.single_quarter_label,
                            report_period=report_period,
                            period_basis="SINGLE_QUARTER",
                            inputs=single_inputs,
                            value=_single_quarter_value(single_inputs, report_period),
                        )
                    )
                ttm_inputs = _ttm_inputs(series, report_period)
                if ttm_inputs is None:
                    skipped_count += 1
                else:
                    outputs.append(
                        _derived_metric(
                            code=formula.ttm_code,
                            label=formula.ttm_label,
                            report_period=report_period,
                            period_basis="TTM",
                            inputs=ttm_inputs,
                            value=_ttm_value(ttm_inputs, report_period),
                        )
                    )
    return (
        tuple(
            sorted(
                outputs,
                key=_derived_metric_order_key,
            )
        ),
        skipped_count,
    )


def _derived_metric_order_key(
    item: DerivedFinancialMetricInput,
) -> tuple[date, str, str]:
    """按报告期、报表范围和指标代码固定派生结果顺序，确保摘要可重放。"""
    return item.report_period, item.statement_scope, item.metric_code


def _index_facts(
    facts: tuple[ReportedFinancialFact, ...],
) -> dict[tuple[str, str], dict[date, ReportedFinancialFact]]:
    """建立公式代码、报表范围和报告期索引，并拒绝歧义或不可比较单位。"""
    result: dict[tuple[str, str], dict[date, ReportedFinancialFact]] = {}
    units: dict[tuple[str, str], tuple[str, str | None, str | None]] = {}
    formula_inputs = {formula.input_code for formula in FORMULAS}
    for fact in facts:
        if fact.metric_code not in formula_inputs:
            continue
        _assert_input_semantics(fact)
        key = (fact.metric_code, fact.statement_scope)
        comparability = (fact.unit, fact.currency, fact.currency_null_reason)
        previous_comparability = units.setdefault(key, comparability)
        if previous_comparability != comparability:
            raise FinancialDerivationUnavailable(
                f"incomparable units in derived input series {fact.metric_code}"
            )
        series = result.setdefault(key, {})
        if fact.report_period in series:
            raise FinancialDerivationUnavailable(
                f"ambiguous derived input {fact.metric_code} on {fact.report_period}"
            )
        series[fact.report_period] = fact
    return result


def _assert_input_semantics(fact: ReportedFinancialFact) -> None:
    """拒绝空单位或币种空值语义不完整的输入，避免派生 manifest 丢失口径。"""
    if not fact.unit:
        raise FinancialDerivationUnavailable(f"derived input unit is empty: {fact.metric_code}")
    if fact.currency is not None and fact.currency_null_reason is None:
        return
    if fact.currency is None and fact.currency_null_reason in _CURRENCY_NULL_REASONS:
        return
    raise FinancialDerivationUnavailable(
        f"derived input currency semantics are invalid: {fact.metric_code}"
    )


def _single_quarter_inputs(
    series: dict[date, ReportedFinancialFact], report_period: date
) -> tuple[ReportedFinancialFact, ...] | None:
    """返回单季公式需要的累计输入；一季度直接使用本期，其他季度减上期。"""
    quarter = _quarter(report_period)
    current = series[report_period]
    if quarter == 1:
        return (current,)
    previous_period = _quarter_end(report_period.year, quarter - 1)
    previous = series.get(previous_period)
    return None if previous is None else (current, previous)


def _ttm_inputs(
    series: dict[date, ReportedFinancialFact], report_period: date
) -> tuple[ReportedFinancialFact, ...] | None:
    """返回 TTM 公式输入；年末直接用全年，其余用本期累计加上年全年减上年同期。"""
    quarter = _quarter(report_period)
    current = series[report_period]
    if quarter == 4:
        return (current,)
    prior_annual = series.get(date(report_period.year - 1, 12, 31))
    prior_same_quarter = series.get(_quarter_end(report_period.year - 1, quarter))
    if prior_annual is None or prior_same_quarter is None:
        return None
    return (current, prior_annual, prior_same_quarter)


def _single_quarter_value(
    inputs: tuple[ReportedFinancialFact, ...], report_period: date
) -> Decimal:
    """计算单季值；一季度为累计值，其余为本期累计减上期累计。"""
    if _quarter(report_period) == 1:
        return inputs[0].value
    return inputs[0].value - inputs[1].value


def _ttm_value(inputs: tuple[ReportedFinancialFact, ...], report_period: date) -> Decimal:
    """计算 TTM；年末为全年值，其余为本期累计加上年全年减上年同期。"""
    if _quarter(report_period) == 4:
        return inputs[0].value
    return inputs[0].value + inputs[1].value - inputs[2].value


def _derived_metric(
    *,
    code: str,
    label: str,
    report_period: date,
    period_basis: str,
    inputs: tuple[ReportedFinancialFact, ...],
    value: Decimal,
) -> DerivedFinancialMetricInput:
    """构造派生输出，并把公式、来源 revision 与 publication 固化进双摘要。"""
    manifest_rows = [
        {
            "sequence": sequence,
            "reportPeriod": item.report_period.isoformat(),
            "metricCode": item.metric_code,
            "revisionId": str(item.revision_id),
            "sourceBatchId": str(item.source_batch_id),
            "value": format(item.value, "f"),
            "unit": item.unit,
            "currency": item.currency,
            "currencyNullReason": item.currency_null_reason,
        }
        for sequence, item in enumerate(inputs, start=1)
    ]
    manifest = json.dumps(
        {"formulaVersion": FORMULA_VERSION, "inputs": manifest_rows},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    manifest_sha = hashlib.sha256(manifest.encode()).hexdigest()
    content = json.dumps(
        {
            "metricCode": code,
            "reportPeriod": report_period.isoformat(),
            "periodBasis": period_basis,
            "statementScope": inputs[0].statement_scope,
            "value": format(value, "f"),
            "unit": inputs[0].unit,
            "currency": inputs[0].currency,
            "currencyNullReason": inputs[0].currency_null_reason,
            "formulaVersion": FORMULA_VERSION,
            "inputManifestSha256": manifest_sha,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return DerivedFinancialMetricInput(
        metric_code=code,
        label=label,
        report_period=report_period,
        period_basis=period_basis,
        statement_scope=inputs[0].statement_scope,
        value=value,
        unit=inputs[0].unit,
        currency=inputs[0].currency,
        currency_null_reason=inputs[0].currency_null_reason,
        formula_version=FORMULA_VERSION,
        effective_from=max(item.effective_from for item in inputs),
        observed_at=max(item.observed_at for item in inputs),
        inputs=inputs,
        input_manifest_sha256=manifest_sha,
        content_sha256=hashlib.sha256(content.encode()).hexdigest(),
    )


def _quarter(value: date) -> int:
    """把标准季度末映射为 1–4，拒绝非季度末报告期进入累计差分。"""
    mapping = {(3, 31): 1, (6, 30): 2, (9, 30): 3, (12, 31): 4}
    try:
        return mapping[(value.month, value.day)]
    except KeyError as error:
        raise FinancialDerivationUnavailable(
            f"derived input is not a standard quarter end: {value.isoformat()}"
        ) from error


def _quarter_end(year: int, quarter: int) -> date:
    """返回指定年份与季度的标准期末日期。"""
    month_day = {1: (3, 31), 2: (6, 30), 3: (9, 30), 4: (12, 31)}[quarter]
    return date(year, *month_day)
