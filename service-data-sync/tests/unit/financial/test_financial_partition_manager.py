"""财务年度分区与双时态排斥约束的单元测试。"""

from __future__ import annotations

from datetime import date
from typing import cast

from sqlalchemy.orm import Session

from service_data_sync.infrastructure.database.partition_manager import (
    ensure_financial_year_partitions,
)


class RecordingSession:
    """记录分区管理器提交的 DDL，不连接真实 PostgreSQL。"""

    def __init__(self) -> None:
        """初始化空 DDL 记录。"""
        self.statements: list[str] = []

    def execute(self, statement: object) -> None:
        """保留 SQLAlchemy DDL 文本，供测试断言固定表集合和年份边界。"""
        self.statements.append(str(statement))


def test_financial_partition_manager_creates_all_yearly_tables_and_constraints() -> None:
    """一个报告年度必须覆盖三类 revision、报表事实和估值，并保护四类逻辑键。"""
    session = RecordingSession()

    ensure_financial_year_partitions(cast(Session, session), date(2026, 3, 31))

    rendered = "\n".join(session.statements)
    assert "financial_report_revision_2026" in rendered
    assert "financial_statement_fact_2026" in rendered
    assert "provider_financial_metric_revision_2026" in rendered
    assert "derived_financial_metric_revision_2026" in rendered
    assert "valuation_observation_revision_2026" in rendered
    assert "FROM ('2026-01-01') TO ('2027-01-01')" in rendered
    assert "ex_financial_report_revision_2026_bitemporal" in rendered
    assert "ex_provider_financial_metric_revision_2026_bitemporal" in rendered
    assert "ex_derived_financial_metric_revision_2026_bitemporal" in rendered
    assert "ex_valuation_observation_revision_2026_bitemporal" in rendered
