"""平台派生财务指标的输入快照、公式输出与发布端口。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Protocol
from uuid import UUID

from service_data_sync.domain.equity import Exchange


class FinancialDerivationUnavailable(RuntimeError):
    """表示没有完整、唯一且仍为当前版本的来源事实可安全派生。"""


@dataclass(frozen=True, slots=True)
class ReportedFinancialFact:
    """描述一个已发布报表 revision 中可作为公式输入的精确事实。"""

    report_period: date
    statement_scope: str
    metric_id: int
    metric_code: str
    value: Decimal
    unit: str
    currency: str | None
    currency_null_reason: str | None
    revision_id: UUID
    source_batch_id: UUID
    effective_from: date
    known_from: datetime
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class FinancialDerivationSnapshot:
    """冻结一个报表 publication 及其公式可见输入集合。"""

    data_version: UUID
    security_id: int
    methodology_id: UUID
    effective_as_of: date
    knowledge_cutoff: datetime
    facts: tuple[ReportedFinancialFact, ...]


@dataclass(frozen=True, slots=True)
class DerivedFinancialMetricInput:
    """描述一个公式版本输出及其完整来源事实清单。"""

    metric_code: str
    label: str
    report_period: date
    period_basis: str
    statement_scope: str
    value: Decimal
    unit: str
    currency: str | None
    currency_null_reason: str | None
    formula_version: int
    effective_from: date
    observed_at: datetime
    inputs: tuple[ReportedFinancialFact, ...]
    input_manifest_sha256: str
    content_sha256: str


@dataclass(frozen=True, slots=True)
class FinancialDerivedPublication:
    """描述一次平台派生能力的不可变消费者发布。"""

    data_version: UUID
    inserted_count: int
    unchanged_count: int
    row_count: int


class FinancialDerivationRepository(Protocol):
    """负责冻结报表输入、写入派生 revision/血缘并原子推进 publication。"""

    def load_inputs(self, *, exchange: Exchange, symbol: str) -> FinancialDerivationSnapshot:
        """读取当前已验证报表 publication 的点时输入；缺失或歧义时失败。"""
        ...

    def publish(
        self,
        *,
        snapshot: FinancialDerivationSnapshot,
        metrics: Sequence[DerivedFinancialMetricInput],
        derivation_run_id: UUID,
        computed_at: datetime,
    ) -> FinancialDerivedPublication:
        """重验来源 publication 后追加派生 revision、输入血缘并发布。"""
        ...
