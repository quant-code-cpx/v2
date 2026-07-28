"""财务报表双时态修订模型。"""

from __future__ import annotations

from datetime import date, datetime
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
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import DATERANGE, TSTZRANGE
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base


class FinancialReportRevision(Base):
    """追加报表 revision 并保存有效时间和平台知识时间，物理分区按报告年度创建。"""

    __tablename__ = "financial_report_revision"
    __table_args__ = (
        CheckConstraint("revision > 0", name="ck_financial_report_revision_number"),
        CheckConstraint(
            "audit_status IN ('AUDITED', 'UNAUDITED', 'UNKNOWN')",
            name="ck_financial_report_revision_audit_status",
        ),
        CheckConstraint(
            "knowledge_basis IN ('OFFICIAL_ANNOUNCEMENT', 'PROVIDER_UPDATE', 'OBSERVED_AT')",
            name="ck_financial_report_revision_knowledge_basis",
        ),
        CheckConstraint(
            "knowledge_confidence IN ('HIGH', 'MEDIUM', 'CONSERVATIVE')",
            name="ck_financial_report_revision_knowledge_confidence",
        ),
        CheckConstraint(
            "quality_status IN ('passed', 'warned', 'quarantined')",
            name="ck_financial_report_revision_quality_status",
        ),
        CheckConstraint(
            "content_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_financial_report_revision_content_sha256",
        ),
        CheckConstraint(
            "effective_to IS NULL OR effective_to > effective_from",
            name="ck_financial_report_revision_effective_range",
        ),
        CheckConstraint(
            "known_to IS NULL OR known_to > known_from",
            name="ck_financial_report_revision_knowledge_range",
        ),
        CheckConstraint(
            "known_from >= observed_at",
            name="ck_financial_report_revision_known_after_observed",
        ),
        UniqueConstraint(
            "report_period",
            "financial_report_id",
            "revision",
            name="uq_financial_report_revision_number",
        ),
        Index(
            "uq_financial_report_revision_current",
            "report_period",
            "financial_report_id",
            unique=True,
            postgresql_where="known_to IS NULL",
        ),
        {
            "postgresql_partition_by": "RANGE (report_period)",
            "comment": "财务报表追加修订父表；按 report_period 年度物理分区。",
        },
    )

    report_period: Mapped[date] = mapped_column(
        Date, primary_key=True, nullable=False, comment="分区键和该 revision 的会计报告期。"
    )
    revision_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, nullable=False, comment="报表 revision 永久 UUID。"
    )
    financial_report_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("financial_report.financial_report_id", ondelete="RESTRICT"),
        nullable=False,
        comment="所属报表逻辑身份。",
    )
    revision: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="同一报表逻辑身份内递增的修订序号。"
    )
    announcement_date: Mapped[date | None] = mapped_column(
        Date, nullable=True, comment="已验证时的公告日期；未知时不得猜测。"
    )
    provider_update_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="供应商提供且已验证语义的更新时间。"
    )
    audit_status: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="审计状态；来源不明时明确记录 UNKNOWN。"
    )
    effective_from: Mapped[date] = mapped_column(
        Date, nullable=False, comment="此 revision 业务上开始有效的保守日期。"
    )
    effective_to: Mapped[date | None] = mapped_column(
        Date, nullable=True, comment="被后续业务有效 revision 替换时的半开区间结束日。"
    )
    known_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="平台首次可以使用本 revision 的时间。"
    )
    known_to: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="被后续知识修订替换时的半开区间结束时间。"
    )
    knowledge_basis: Mapped[str] = mapped_column(
        String(24), nullable=False, comment="公告、供应商更新或实际观察的知识时间依据。"
    )
    knowledge_confidence: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="知识时间依据的置信等级。"
    )
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="平台实际成功观察到来源响应的时间。"
    )
    source_batch_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("source_batch.source_batch_id", ondelete="RESTRICT"),
        nullable=False,
        comment="支撑此 revision 的不可变来源观察。",
    )
    content_sha256: Mapped[str] = mapped_column(
        CHAR(64), nullable=False, comment="规范化报表内容稳定摘要。"
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
        comment="由平台知识起止时间生成的半开时间范围。",
    )
