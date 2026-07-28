"""个股分红送转事件 revision 模型。"""

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
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    desc,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ...base import Base


class EquityCorporateActionVersion(Base):
    """保存公司行动的可修订事件状态与关键实施日期。"""

    __tablename__ = "equity_corporate_action_version"
    __table_args__ = (
        CheckConstraint("revision > 0"),
        CheckConstraint("cash_dividend_per_10 IS NULL OR cash_dividend_per_10 >= 0"),
        CheckConstraint("bonus_shares_per_10 IS NULL OR bonus_shares_per_10 >= 0"),
        CheckConstraint("transfer_shares_per_10 IS NULL OR transfer_shares_per_10 >= 0"),
        CheckConstraint("valid_to IS NULL OR valid_to > valid_from"),
        UniqueConstraint("security_id", "source_event_key", "revision"),
        Index(
            "uq_equity_corporate_action_current",
            "action_id",
            unique=True,
            postgresql_where="valid_to IS NULL",
        ),
        Index(
            "ix_equity_corporate_action_read",
            "security_id",
            desc("report_period"),
            postgresql_where="valid_to IS NULL",
        ),
        {"comment": "个股分红、送股和转增方案的追加 revision。"},
    )

    action_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, nullable=False, comment="平台稳定公司行动 UUID。"
    )
    revision: Mapped[int] = mapped_column(
        Integer, primary_key=True, nullable=False, comment="同一事件的递增修订号。"
    )
    security_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("equity_instrument.security_id", ondelete="RESTRICT"),
        nullable=False,
        comment="事件所属永久证券内部键。",
    )
    source_event_key: Mapped[str] = mapped_column(
        String(160), nullable=False, comment="由来源稳定字段构成的事件身份。"
    )
    report_period: Mapped[date] = mapped_column(
        Date, nullable=False, comment="分配方案所属报告期。"
    )
    status: Mapped[str] = mapped_column(
        String(80), nullable=False, comment="来源当前报告的方案进度。"
    )
    announcement_date: Mapped[date | None] = mapped_column(
        Date, nullable=True, comment="最近公告日期。"
    )
    record_date: Mapped[date | None] = mapped_column(Date, nullable=True, comment="股权登记日。")
    ex_date: Mapped[date | None] = mapped_column(Date, nullable=True, comment="除权除息日。")
    cash_dividend_per_10: Mapped[Decimal | None] = mapped_column(
        Numeric(30, 10), nullable=True, comment="每十股现金分红，单位人民币元。"
    )
    bonus_shares_per_10: Mapped[Decimal | None] = mapped_column(
        Numeric(30, 10), nullable=True, comment="每十股送股数量。"
    )
    transfer_shares_per_10: Mapped[Decimal | None] = mapped_column(
        Numeric(30, 10), nullable=True, comment="每十股转增数量。"
    )
    content_sha256: Mapped[bytes] = mapped_column(nullable=False, comment="事件业务内容稳定摘要。")
    source_batch_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("source_batch.source_batch_id", ondelete="RESTRICT"),
        nullable=False,
        comment="支撑本事件 revision 的来源观察。",
    )
    valid_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="本修订开始可见的知识时间。"
    )
    valid_to: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="本修订被后继版本替换的知识时间。"
    )
    source_description: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="保留已标准化的现金分红说明，缺失时为空。"
    )
