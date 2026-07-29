"""因结构、来源或身份冲突被隔离的板块成分私有证据模型。"""

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


class SectorMembershipQuarantine(Base):
    """保存因结构或身份冲突被隔离的成分行，绝不作为 `canonical` 成员发布。

    隔离项保留所属快照、来源原文、原因和有限证据，供修复映射或上游异常排查；它不是对证券退市、
    板块删除或成员退出的业务结论。任何隔离存在都应影响快照完整性与质量门，不能为凑齐覆盖率
    把它临时写入已确认成员表或关闭已有观察区间。
    """

    __tablename__ = "sector_membership_quarantine"
    __table_args__ = (
        CheckConstraint("row_ordinal > 0"),
        CheckConstraint(
            "source_symbol ~ '^[0-9]{6}$'", name="ck_sector_membership_quarantine_source_symbol"
        ),
        CheckConstraint(
            "BTRIM(source_name) <> ''", name="ck_sector_membership_quarantine_source_name"
        ),
        UniqueConstraint("snapshot_id", "row_ordinal"),
        {"comment": "质量或身份冲突隔离的板块成分行。"},
    )

    quarantine_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        autoincrement=True,
        nullable=False,
        comment="隔离行内部自增键。",
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
    reason_code: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="隔离原因稳定编码。"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="隔离行登记时间。"
    )
