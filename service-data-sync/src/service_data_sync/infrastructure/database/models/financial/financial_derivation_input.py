"""平台派生指标到逐项报表输入、来源批次与冻结发布版本的血缘模型。"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base


class FinancialDerivationInput(Base):
    """逐项关联派生 `revision`、来源报表 `revision`、来源批次和输入 `publication`。

    同一公式可能使用当前累计、上期累计、上年全年或同期值；`input_sequence` 和 `input_role` 让
    每个输入在公式中的位置可重演。它冻结计算当时读取的报表 `data_version`，因此后来报表更正
    不会悄悄改写既有派生结果；币种或单位不可比时应在质量门阻断，而不是在此表隐式换算。
    """

    __tablename__ = "financial_derivation_input"
    __table_args__ = (
        CheckConstraint("input_sequence > 0", name="ck_financial_derivation_input_sequence"),
        CheckConstraint(
            "input_role IN ('CURRENT_YTD', 'PREVIOUS_YTD', 'PRIOR_ANNUAL', 'PRIOR_SAME_QUARTER')",
            name="ck_financial_derivation_input_role",
        ),
        CheckConstraint(
            "(input_currency IS NOT NULL AND input_currency_null_reason IS NULL) "
            "OR (input_currency IS NULL AND input_currency_null_reason IN "
            "('NOT_APPLICABLE', 'UNKNOWN_SOURCE', 'MIXED_CURRENCIES'))",
            name="ck_financial_derivation_input_currency",
        ),
        ForeignKeyConstraint(
            ("derived_report_period", "derived_metric_revision_id"),
            (
                "derived_financial_metric_revision.report_period",
                "derived_financial_metric_revision.metric_revision_id",
            ),
            ondelete="RESTRICT",
            name="fk_financial_derivation_input_derived_revision",
        ),
        ForeignKeyConstraint(
            ("input_report_period", "input_revision_id"),
            ("financial_report_revision.report_period", "financial_report_revision.revision_id"),
            ondelete="RESTRICT",
            name="fk_financial_derivation_input_report_revision",
        ),
        Index(
            "ix_financial_derivation_input_source",
            "input_source_batch_id",
            "input_report_period",
        ),
        Index(
            "ix_financial_derivation_input_publication",
            "input_data_version",
            "derived_report_period",
        ),
        {"comment": "平台派生指标逐项输入 manifest；可回链报表 revision、raw batch 和发布版本。"},
    )

    derived_report_period: Mapped[date] = mapped_column(
        Date, primary_key=True, nullable=False, comment="派生指标所在报告期。"
    )
    derived_metric_revision_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, nullable=False, comment="派生指标 revision UUID。"
    )
    input_sequence: Mapped[int] = mapped_column(
        Integer, primary_key=True, nullable=False, comment="公式输入的稳定顺序，从一开始。"
    )
    input_role: Mapped[str] = mapped_column(
        String(24), nullable=False, comment="当前累计、上期累计、上年全年或上年同期角色。"
    )
    input_report_period: Mapped[date] = mapped_column(
        Date, nullable=False, comment="来源报表事实的报告期。"
    )
    input_revision_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False, comment="来源报表 canonical revision UUID。"
    )
    input_metric_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("financial_metric_definition.metric_id", ondelete="RESTRICT"),
        nullable=False,
        comment="来源治理字段字典键。",
    )
    input_source_batch_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("source_batch.source_batch_id", ondelete="RESTRICT"),
        nullable=False,
        comment="可继续回链 raw URI 的来源观察批次。",
    )
    input_data_version: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("dataset_publication.data_version", ondelete="RESTRICT"),
        nullable=False,
        comment="计算时冻结的报表 publication 版本。",
    )
    input_value: Mapped[Decimal] = mapped_column(
        Numeric(38, 12), nullable=False, comment="参与公式的精确输入值。"
    )
    input_unit: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="输入事实规范单位；所有输入必须可比。"
    )
    input_currency: Mapped[str | None] = mapped_column(
        String(3), nullable=True, comment="输入事实已知时的 ISO 4217 币种。"
    )
    input_currency_null_reason: Mapped[str | None] = mapped_column(
        String(24), nullable=True, comment="输入币种为空时的受控原因。"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="输入 manifest 写入时间。"
    )
