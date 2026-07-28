"""供应商财务指标双时态修订模型。"""

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


class ProviderFinancialMetricRevision(Base):
    """保存供应商直接给出的指标 revision，不与披露事实或平台派生指标共表。"""

    __tablename__ = "provider_financial_metric_revision"
    __table_args__ = (
        CheckConstraint("revision > 0", name="ck_provider_financial_metric_revision_number"),
        CheckConstraint(
            "period_basis IN ('POINT_IN_TIME', 'YEAR_TO_DATE', 'SINGLE_QUARTER', 'TTM')",
            name="ck_provider_financial_metric_period_basis",
        ),
        CheckConstraint(
            "statement_scope IN ('CONSOLIDATED', 'PARENT', 'UNKNOWN')",
            name="ck_provider_financial_metric_statement_scope",
        ),
        CheckConstraint(
            "(currency IS NOT NULL AND currency_null_reason IS NULL) "
            "OR (currency IS NULL AND currency_null_reason IN "
            "('NOT_APPLICABLE', 'UNKNOWN_SOURCE', 'MIXED_CURRENCIES'))",
            name="ck_provider_financial_metric_currency",
        ),
        CheckConstraint(
            "knowledge_basis IN ('OFFICIAL_ANNOUNCEMENT', 'PROVIDER_UPDATE', 'OBSERVED_AT')",
            name="ck_provider_financial_metric_knowledge_basis",
        ),
        CheckConstraint(
            "knowledge_confidence IN ('HIGH', 'MEDIUM', 'CONSERVATIVE')",
            name="ck_provider_financial_metric_knowledge_confidence",
        ),
        CheckConstraint(
            "quality_status IN ('passed', 'warned', 'quarantined')",
            name="ck_provider_financial_metric_quality_status",
        ),
        CheckConstraint(
            "content_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_provider_financial_metric_content_sha256",
        ),
        CheckConstraint(
            "effective_to IS NULL OR effective_to > effective_from",
            name="ck_provider_financial_metric_effective_range",
        ),
        CheckConstraint(
            "known_to IS NULL OR known_to > known_from",
            name="ck_provider_financial_metric_knowledge_range",
        ),
        CheckConstraint(
            "known_from >= observed_at",
            name="ck_provider_financial_metric_known_after_observed",
        ),
        UniqueConstraint(
            "report_period",
            "security_id",
            "metric_id",
            "methodology_id",
            "period_basis",
            "statement_scope",
            "revision",
            name="uq_provider_financial_metric_revision",
        ),
        Index(
            "uq_provider_financial_metric_current",
            "report_period",
            "security_id",
            "metric_id",
            "methodology_id",
            "period_basis",
            "statement_scope",
            unique=True,
            postgresql_where="known_to IS NULL",
        ),
        Index(
            "ix_provider_financial_metric_series",
            "security_id",
            "metric_id",
            "report_period",
        ),
        {
            "postgresql_partition_by": "RANGE (report_period)",
            "comment": "供应商直接指标 revision 父表；按 report_period 年度物理分区。",
        },
    )

    report_period: Mapped[date] = mapped_column(
        Date, primary_key=True, nullable=False, comment="分区键及指标对应的报告期。"
    )
    metric_revision_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        nullable=False,
        comment="供应商指标 revision UUID。",
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
        comment="供应商指标方法学版本。",
    )
    period_basis: Mapped[str] = mapped_column(
        String(24), nullable=False, comment="指标报告期口径。"
    )
    statement_scope: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="合并、母公司或来源未知范围。"
    )
    value: Mapped[Decimal] = mapped_column(
        Numeric(38, 12), nullable=False, comment="供应商指标的精确规范化值。"
    )
    unit: Mapped[str] = mapped_column(String(32), nullable=False, comment="指标规范化后的单位。")
    currency: Mapped[str | None] = mapped_column(
        CHAR(3), nullable=True, comment="已知时的 ISO 4217 币种。"
    )
    currency_null_reason: Mapped[str | None] = mapped_column(
        String(24), nullable=True, comment="币种为空时的受控原因。"
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
        DateTime(timezone=True), nullable=False, comment="实际取得来源响应的时间。"
    )
    source_batch_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("source_batch.source_batch_id", ondelete="RESTRICT"),
        nullable=False,
        comment="支撑本指标 revision 的来源观察。",
    )
    revision: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="同一指标逻辑键的递增修订序号。"
    )
    content_sha256: Mapped[str] = mapped_column(
        CHAR(64), nullable=False, comment="规范化指标内容稳定摘要。"
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
