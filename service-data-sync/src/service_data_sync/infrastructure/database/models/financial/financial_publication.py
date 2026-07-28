"""财务能力消费者发布明细模型。"""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from sqlalchemy import (
    CHAR,
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base


class FinancialPublication(Base):
    """将财务 capability、证券和方法学绑定到一个不可变消费者 data_version。"""

    __tablename__ = "financial_publication"
    __table_args__ = (
        CheckConstraint(
            "capability IN ('financial.report', 'financial.provider-metric', "
            "'financial.derived-metric', 'financial.valuation')",
            name="ck_financial_publication_capability",
        ),
        CheckConstraint("row_count >= 0", name="ck_financial_publication_row_count"),
        CheckConstraint(
            "content_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_financial_publication_content_sha256",
        ),
        UniqueConstraint(
            "capability",
            "security_id",
            "methodology_id",
            "data_version",
            name="uq_financial_publication_scope_version",
        ),
        {"comment": "财务能力发布明细；research 或 quarantine 数据不得写入本表。"},
    )

    data_version: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("dataset_publication.data_version", ondelete="RESTRICT"),
        primary_key=True,
        nullable=False,
        comment="消费者可见的不可变数据版本，同时关联通用发布指针。",
    )
    capability: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="财务报表、供应商指标、派生指标或估值能力。"
    )
    security_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("equity_instrument.security_id", ondelete="RESTRICT"),
        nullable=False,
        comment="本发布唯一覆盖的永久证券内部键。",
    )
    methodology_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("financial_methodology.methodology_id", ondelete="RESTRICT"),
        nullable=False,
        comment="消费者查询必须显式指定的方法学版本。",
    )
    effective_as_of: Mapped[date] = mapped_column(
        Date, nullable=False, comment="本版本默认业务有效截点日期。"
    )
    knowledge_cutoff: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="本版本默认平台知识截点时间。"
    )
    row_count: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="本发布覆盖的 canonical 行数。"
    )
    content_sha256: Mapped[str] = mapped_column(
        CHAR(64), nullable=False, comment="本发布规范化内容稳定摘要。"
    )
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="开始对内部消费者可见的时间。"
    )
