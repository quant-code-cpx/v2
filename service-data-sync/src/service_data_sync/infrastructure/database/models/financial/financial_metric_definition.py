"""财务行项目与指标治理字典模型。"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base


class FinancialMetricDefinition(Base):
    """定义可进入 canonical 表的财务字段，未知字段必须进入 quarantine。"""

    __tablename__ = "financial_metric_definition"
    __table_args__ = (
        CheckConstraint("dictionary_version > 0", name="ck_financial_metric_definition_version"),
        CheckConstraint(
            "origin IN ('statement_fact', 'provider_reported', 'platform_derived', 'valuation')",
            name="ck_financial_metric_definition_origin",
        ),
        CheckConstraint(
            "statement_type IS NULL OR statement_type IN "
            "('BALANCE_SHEET', 'INCOME_STATEMENT', 'CASH_FLOW_STATEMENT')",
            name="ck_financial_metric_definition_statement_type",
        ),
        CheckConstraint(
            "value_domain IN ('monetary', 'ratio', 'count', 'per_share', 'other')",
            name="ck_financial_metric_definition_value_domain",
        ),
        CheckConstraint(
            "status IN ('draft', 'active', 'retired')",
            name="ck_financial_metric_definition_status",
        ),
        CheckConstraint(
            "(origin = 'statement_fact' AND statement_type IS NOT NULL) "
            "OR (origin <> 'statement_fact' AND statement_type IS NULL)",
            name="ck_financial_metric_definition_origin_statement_type",
        ),
        UniqueConstraint("code", "dictionary_version", name="uq_financial_metric_definition_code"),
        Index(
            "uq_financial_metric_definition_active_code",
            "code",
            unique=True,
            postgresql_where="status = 'active'",
        ),
        {"comment": "受治理的财务行项目和指标字典；没有 JSON 或 EAV 旁路。"},
    )

    metric_id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, nullable=False, comment="财务字段内部稳定键。"
    )
    code: Mapped[str] = mapped_column(String(80), nullable=False, comment="机器可读字段代码。")
    label: Mapped[str] = mapped_column(
        String(160), nullable=False, comment="面向维护者和消费者的受控字段名称。"
    )
    origin: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="披露事实、供应商指标、平台派生或估值观察来源类别。"
    )
    statement_type: Mapped[str | None] = mapped_column(
        String(32), nullable=True, comment="披露事实所属三大报表；其他来源为空。"
    )
    value_domain: Mapped[str] = mapped_column(
        String(24), nullable=False, comment="金额、比例、数量、每股或其他值域。"
    )
    canonical_unit: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="该字段规范化后使用的单位代码。"
    )
    currency_required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, comment="该字段是否必须表达币种或明确币种空值原因。"
    )
    sign_convention: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="数值正负号的受控约定版本。"
    )
    dictionary_version: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="字段字典版本号。"
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="草拟、启用或退役状态。"
    )
