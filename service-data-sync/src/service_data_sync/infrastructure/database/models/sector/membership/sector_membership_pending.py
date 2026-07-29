"""无法安全解析为永久证券的板块成分待确认行与后续处置模型。"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CHAR,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ...base import Base


class SectorMembershipPending(Base):
    """保存无法安全解析为永久证券的成分行，禁止其推进成分区间。

    来源代码、交易所或证券生命周期不充分时，保留原文、快照和原因以便后续治理，而不是强制关联
    同名或当前代码证券。`pending` 不等于成员不存在，也不等于隔离错误；只要快照含未确认项，就
    不能宣布该板块完整、更新观察区间或纳入面向消费者的成分 `release`。
    """

    __tablename__ = "sector_membership_pending"
    __table_args__ = (
        CheckConstraint("row_ordinal > 0"),
        CheckConstraint(
            "source_symbol ~ '^[0-9]{6}$'", name="ck_sector_membership_pending_source_symbol"
        ),
        CheckConstraint(
            "BTRIM(source_name) <> ''", name="ck_sector_membership_pending_source_name"
        ),
        CheckConstraint(
            "inferred_exchange IN ('SSE', 'SZSE', 'BSE')",
            name="ck_sector_membership_pending_inferred_exchange",
        ),
        UniqueConstraint("snapshot_id", "row_ordinal"),
        {"comment": "无法安全确认永久证券身份的板块成分行。"},
    )

    pending_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        autoincrement=True,
        nullable=False,
        comment="待确认行内部自增键。",
    )
    snapshot_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("sector_membership_snapshot.snapshot_id"),
        nullable=False,
        comment="所属成分快照。",
    )
    row_ordinal: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="来源响应内从一开始的稳定行序号。"
    )
    source_symbol: Mapped[str] = mapped_column(
        CHAR(6), nullable=False, comment="来源观察到的六位证券代码。"
    )
    source_name: Mapped[str] = mapped_column(
        String(200), nullable=False, comment="来源观察到的证券名称。"
    )
    inferred_exchange: Mapped[str | None] = mapped_column(
        String(4), nullable=True, comment="仅供人工处理参考的推断交易所。"
    )
    reason_code: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="身份无法确认的稳定原因编码。"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="待确认行登记时间。"
    )
