"""财务报表治理行项目模型。"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    CHAR,
    BigInteger,
    CheckConstraint,
    Date,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Numeric,
    String,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base


class FinancialStatementFact(Base):
    """保存已治理的报表行项目；空值必须保留原因，禁止用零值或 JSON 代替。"""

    __tablename__ = "financial_statement_fact"
    __table_args__ = (
        ForeignKeyConstraint(
            ["report_period", "revision_id"],
            ["financial_report_revision.report_period", "financial_report_revision.revision_id"],
            ondelete="RESTRICT",
            name="fk_financial_statement_fact_revision",
        ),
        CheckConstraint(
            "(value IS NOT NULL AND null_reason IS NULL) "
            "OR (value IS NULL AND null_reason IN "
            "('NOT_REPORTED', 'NOT_APPLICABLE', 'UPSTREAM_NULL'))",
            name="ck_financial_statement_fact_value",
        ),
        CheckConstraint(
            "(currency IS NOT NULL AND currency_null_reason IS NULL) "
            "OR (currency IS NULL AND currency_null_reason IN "
            "('NOT_APPLICABLE', 'UNKNOWN_SOURCE', 'MIXED_CURRENCIES'))",
            name="ck_financial_statement_fact_currency",
        ),
        CheckConstraint("scale_factor > 0", name="ck_financial_statement_fact_scale"),
        Index("ix_financial_statement_fact_metric_period", "metric_id", "report_period"),
        {
            "postgresql_partition_by": "RANGE (report_period)",
            "comment": "已治理的报表行项目父表；与报表 revision 使用相同报告年度分区。",
        },
    )

    report_period: Mapped[date] = mapped_column(
        Date, primary_key=True, nullable=False, comment="分区键及所属会计报告期。"
    )
    revision_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, nullable=False, comment="所属报表 revision UUID。"
    )
    metric_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("financial_metric_definition.metric_id", ondelete="RESTRICT"),
        primary_key=True,
        nullable=False,
        comment="已治理行项目字典键。",
    )
    value: Mapped[Decimal | None] = mapped_column(
        Numeric(38, 10), nullable=True, comment="精确规范化数值；空值必须附带 null_reason。"
    )
    null_reason: Mapped[str | None] = mapped_column(
        String(24), nullable=True, comment="数值为空时的来源语义原因。"
    )
    currency: Mapped[str | None] = mapped_column(
        CHAR(3), nullable=True, comment="金额或币种相关值的 ISO 4217 代码。"
    )
    currency_null_reason: Mapped[str | None] = mapped_column(
        String(24), nullable=True, comment="币种为空时的受控原因。"
    )
    original_unit: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="来源响应中的原始单位。"
    )
    canonical_unit: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="归一化后用于查询的标准单位。"
    )
    scale_factor: Mapped[Decimal] = mapped_column(
        Numeric(30, 12), nullable=False, comment="从原始单位换算到标准单位的正比例系数。"
    )
    sign_convention: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="本行应用的数值正负号规则。"
    )
