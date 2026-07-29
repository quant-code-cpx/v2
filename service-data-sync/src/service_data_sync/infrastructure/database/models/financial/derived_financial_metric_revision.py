"""平台公式计算的财务指标双时态 `revision`、输入摘要与年度分区模型。"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    CHAR,
    BigInteger,
    CheckConstraint,
    Computed,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import DATERANGE, TSTZRANGE
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base


class DerivedFinancialMetricRevision(Base):
    """保存可重算的平台派生指标；公式或冻结输入变化必须追加新 `revision`。

    它与供应商直接给出的指标分表，避免把“披露值”“来源计算值”和“平台公式值”混作同一事实。
    `formula_version`、输入清单摘要、方法学和双时态范围共同说明该数值如何得到、何时业务有效、
    何时平台可以知道。质量未通过的计算可保留审计，但不能进入消费者 `publication`。
    """

    __tablename__ = "derived_financial_metric_revision"
    __table_args__ = (
        CheckConstraint("revision > 0", name="ck_derived_financial_metric_revision_number"),
        CheckConstraint("formula_version > 0", name="ck_derived_financial_metric_formula_version"),
        CheckConstraint(
            "period_basis IN ('POINT_IN_TIME', 'YEAR_TO_DATE', 'SINGLE_QUARTER', 'TTM')",
            name="ck_derived_financial_metric_period_basis",
        ),
        CheckConstraint(
            "statement_scope IN ('CONSOLIDATED', 'PARENT', 'UNKNOWN')",
            name="ck_derived_financial_metric_statement_scope",
        ),
        CheckConstraint(
            "(currency IS NOT NULL AND currency_null_reason IS NULL) "
            "OR (currency IS NULL AND currency_null_reason IN "
            "('NOT_APPLICABLE', 'UNKNOWN_SOURCE', 'MIXED_CURRENCIES'))",
            name="ck_derived_financial_metric_currency",
        ),
        CheckConstraint(
            "knowledge_basis IN ('OFFICIAL_ANNOUNCEMENT', 'PROVIDER_UPDATE', 'OBSERVED_AT')",
            name="ck_derived_financial_metric_knowledge_basis",
        ),
        CheckConstraint(
            "knowledge_confidence IN ('HIGH', 'MEDIUM', 'CONSERVATIVE')",
            name="ck_derived_financial_metric_knowledge_confidence",
        ),
        CheckConstraint(
            "quality_status IN ('passed', 'warned', 'quarantined')",
            name="ck_derived_financial_metric_quality_status",
        ),
        CheckConstraint(
            "input_manifest_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_derived_financial_metric_input_manifest_sha256",
        ),
        CheckConstraint(
            "content_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_derived_financial_metric_content_sha256",
        ),
        CheckConstraint(
            "effective_to IS NULL OR effective_to > effective_from",
            name="ck_derived_financial_metric_effective_range",
        ),
        CheckConstraint(
            "known_to IS NULL OR known_to > known_from",
            name="ck_derived_financial_metric_knowledge_range",
        ),
        CheckConstraint(
            "known_from >= observed_at",
            name="ck_derived_financial_metric_known_after_observed",
        ),
        UniqueConstraint(
            "report_period",
            "security_id",
            "metric_id",
            "methodology_id",
            "period_basis",
            "statement_scope",
            "formula_version",
            "revision",
            name="uq_derived_financial_metric_revision",
        ),
        Index(
            "uq_derived_financial_metric_current",
            "report_period",
            "security_id",
            "metric_id",
            "methodology_id",
            "period_basis",
            "statement_scope",
            "formula_version",
            unique=True,
            postgresql_where="known_to IS NULL",
        ),
        Index(
            "ix_derived_financial_metric_series",
            "security_id",
            "metric_id",
            "report_period",
        ),
        {
            "postgresql_partition_by": "RANGE (report_period)",
            "comment": "平台派生指标 revision 父表；按 report_period 年度物理分区。",
        },
    )

    report_period: Mapped[date] = mapped_column(
        Date, primary_key=True, nullable=False, comment="分区键及指标对应的报告期。"
    )
    metric_revision_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, nullable=False, comment="派生指标 revision UUID。"
    )
    security_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("equity_instrument.security_id", ondelete="RESTRICT"),
        nullable=False,
        comment="指标所属永久证券内部键。",
    )
    metric_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("financial_metric_definition.metric_id", ondelete="RESTRICT"),
        nullable=False,
        comment="受治理指标字典键。",
    )
    methodology_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("financial_methodology.methodology_id", ondelete="RESTRICT"),
        nullable=False,
        comment="派生算法方法学版本。",
    )
    period_basis: Mapped[str] = mapped_column(
        String(24), nullable=False, comment="派生指标的报告期口径。"
    )
    statement_scope: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="合并、母公司或来源未知范围。"
    )
    value: Mapped[Decimal] = mapped_column(
        Numeric(38, 12), nullable=False, comment="平台派生后的精确数值。"
    )
    unit: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="派生指标规范化后的单位。"
    )
    currency: Mapped[str | None] = mapped_column(
        CHAR(3), nullable=True, comment="已知时的 ISO 4217 币种。"
    )
    currency_null_reason: Mapped[str | None] = mapped_column(
        String(24), nullable=True, comment="币种为空时的受控原因。"
    )
    formula_version: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="派生公式不可变版本号。"
    )
    input_manifest_sha256: Mapped[str] = mapped_column(
        CHAR(64), nullable=False, comment="公式输入 publication 与 revision 清单稳定摘要。"
    )
    derivation_run_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("sync_run.run_id", ondelete="RESTRICT"),
        nullable=False,
        comment="执行该公式的可恢复运行账本 UUID。",
    )
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="平台完成公式计算的时间。"
    )
    effective_from: Mapped[date] = mapped_column(
        Date, nullable=False, comment="该 revision 业务上开始有效的保守日期。"
    )
    effective_to: Mapped[date | None] = mapped_column(
        Date, nullable=True, comment="业务有效半开区间的结束日期。"
    )
    known_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="平台首次可以使用本 revision 的时间。"
    )
    known_to: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="平台知识半开区间的结束时间。"
    )
    knowledge_basis: Mapped[str] = mapped_column(
        String(24), nullable=False, comment="知识时间使用的公告、供应商更新或实际观察依据。"
    )
    knowledge_confidence: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="知识时间依据置信等级。"
    )
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="最晚一项原始输入实际观察时间。"
    )
    source_batch_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("source_batch.source_batch_id", ondelete="RESTRICT"),
        nullable=False,
        comment="触发本次计算的来源观察。",
    )
    revision: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="同一派生指标逻辑键的递增修订序号。"
    )
    content_sha256: Mapped[str] = mapped_column(
        CHAR(64), nullable=False, comment="规范化派生指标内容稳定摘要。"
    )
    quality_status: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="通过、警告或隔离质量状态。"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="canonical revision 写入时间。"
    )
    effective_range: Mapped[object] = mapped_column(
        DATERANGE,
        Computed("daterange(effective_from, effective_to, '[)')", persisted=True),
        nullable=True,
        comment="由有效起止日期生成的半开业务时间范围。",
    )
    knowledge_range: Mapped[object] = mapped_column(
        TSTZRANGE,
        Computed("tstzrange(known_from, known_to, '[)')", persisted=True),
        nullable=True,
        comment="由知识起止时间生成的半开时间范围。",
    )
