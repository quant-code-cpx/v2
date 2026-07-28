"""个股公司概况 revision 模型。"""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ...base import Base


class EquityProfileVersion(Base):
    """保存巨潮公司概况的内容修订，空字段不用于覆盖既有非空值。"""

    __tablename__ = "equity_profile_version"
    __table_args__ = (
        CheckConstraint("revision > 0"),
        CheckConstraint("valid_to IS NULL OR valid_to > valid_from"),
        Index(
            "uq_equity_profile_current",
            "security_id",
            unique=True,
            postgresql_where="valid_to IS NULL",
        ),
        {"comment": "个股公司概况的追加 revision。"},
    )

    security_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("equity_instrument.security_id", ondelete="RESTRICT"),
        primary_key=True,
        nullable=False,
        comment="公司概况所属永久证券内部键。",
    )
    revision: Mapped[int] = mapped_column(
        Integer, primary_key=True, nullable=False, comment="同一证券概况的递增修订号。"
    )
    company_name: Mapped[str] = mapped_column(
        String(300), nullable=False, comment="公司法定中文名称。"
    )
    english_name: Mapped[str | None] = mapped_column(
        String(500), nullable=True, comment="公司英文名称。"
    )
    industry: Mapped[str | None] = mapped_column(
        String(300), nullable=True, comment="巨潮报告的所属行业。"
    )
    legal_representative: Mapped[str | None] = mapped_column(
        String(160), nullable=True, comment="法定代表人。"
    )
    established_on: Mapped[date | None] = mapped_column(
        Date, nullable=True, comment="公司成立日期。"
    )
    website: Mapped[str | None] = mapped_column(String(1000), nullable=True, comment="官方网站。")
    email: Mapped[str | None] = mapped_column(String(500), nullable=True, comment="公开联系邮箱。")
    phone: Mapped[str | None] = mapped_column(String(300), nullable=True, comment="公开联系电话。")
    registered_address: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="注册地址。"
    )
    office_address: Mapped[str | None] = mapped_column(Text, nullable=True, comment="办公地址。")
    main_business: Mapped[str | None] = mapped_column(Text, nullable=True, comment="主营业务。")
    business_scope: Mapped[str | None] = mapped_column(Text, nullable=True, comment="经营范围。")
    summary: Mapped[str | None] = mapped_column(Text, nullable=True, comment="机构简介。")
    content_sha256: Mapped[bytes] = mapped_column(nullable=False, comment="概况业务内容稳定摘要。")
    source_batch_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("source_batch.source_batch_id", ondelete="RESTRICT"),
        nullable=False,
        comment="支撑本概况 revision 的来源观察。",
    )
    valid_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="本修订开始可见的知识时间。"
    )
    valid_to: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="本修订被后继版本替换的知识时间。"
    )
