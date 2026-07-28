"""财务报表逻辑身份模型。"""

from __future__ import annotations

from datetime import date
from uuid import UUID

from sqlalchemy import (
    CHAR,
    BigInteger,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base


class FinancialReport(Base):
    """保存一份报表的逻辑身份，业务值和双时态修订只存在于 revision 表。"""

    __tablename__ = "financial_report"
    __table_args__ = (
        CheckConstraint(
            "statement_type IN ('BALANCE_SHEET', 'INCOME_STATEMENT', 'CASH_FLOW_STATEMENT')",
            name="ck_financial_report_statement_type",
        ),
        CheckConstraint(
            "period_basis IN ('POINT_IN_TIME', 'YEAR_TO_DATE', 'SINGLE_QUARTER', 'TTM')",
            name="ck_financial_report_period_basis",
        ),
        CheckConstraint(
            "statement_scope IN ('CONSOLIDATED', 'PARENT', 'UNKNOWN')",
            name="ck_financial_report_statement_scope",
        ),
        CheckConstraint(
            "(currency IS NOT NULL AND currency_null_reason IS NULL) "
            "OR (currency IS NULL AND currency_null_reason IN "
            "('NOT_APPLICABLE', 'UNKNOWN_SOURCE', 'MIXED_CURRENCIES'))",
            name="ck_financial_report_currency",
        ),
        UniqueConstraint(
            "security_id",
            "methodology_id",
            "statement_type",
            "report_period",
            "period_basis",
            "statement_scope",
            "currency",
            "report_type",
            name="uq_financial_report_logical_key",
            postgresql_nulls_not_distinct=True,
        ),
        Index("ix_financial_report_security_period", "security_id", "report_period"),
        {"comment": "财务报表逻辑身份；公开 report_ref 不暴露内部自增主键。"},
    )

    financial_report_id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, nullable=False, comment="报表逻辑身份内部主键。"
    )
    report_ref: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), unique=True, nullable=False, comment="可安全公开的报表引用 UUID。"
    )
    security_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("equity_instrument.security_id", ondelete="RESTRICT"),
        nullable=False,
        comment="报表所属的永久证券内部键。",
    )
    methodology_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("financial_methodology.methodology_id", ondelete="RESTRICT"),
        nullable=False,
        comment="定义本报表语义的固定方法学版本。",
    )
    statement_type: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="资产负债表、利润表或现金流量表。"
    )
    report_period: Mapped[date] = mapped_column(
        Date, nullable=False, comment="会计报告期结束日期；不是平台可见日期。"
    )
    period_basis: Mapped[str] = mapped_column(
        String(24), nullable=False, comment="时点、累计、单季或 TTM 口径。"
    )
    statement_scope: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="合并、母公司或来源未知范围。"
    )
    currency: Mapped[str | None] = mapped_column(
        CHAR(3), nullable=True, comment="已知时的 ISO 4217 报告币种。"
    )
    currency_null_reason: Mapped[str | None] = mapped_column(
        String(24), nullable=True, comment="币种为空时的受控原因，不以伪造币种替代。"
    )
    report_type: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="供应商已映射的报告类别，例如年报或季报。"
    )
    superseded_by: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("financial_report.financial_report_id", ondelete="RESTRICT"),
        nullable=True,
        comment="逻辑身份被受控替代时指向新身份；通常为空。",
    )
