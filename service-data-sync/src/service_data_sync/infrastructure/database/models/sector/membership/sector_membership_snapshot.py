"""板块成分来源快照头模型。"""

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
    Index,
    Integer,
    String,
    UniqueConstraint,
    desc,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ...base import Base


class SectorMembershipSnapshot(Base):
    """保存一个板块完整成分观察；只有 COMPLETE 快照可推进 observed 区间。"""

    __tablename__ = "sector_membership_snapshot"
    __table_args__ = (
        CheckConstraint(
            "status IN ('COMPLETE', 'QUARANTINED')", name="ck_sector_membership_snapshot_status"
        ),
        CheckConstraint(
            "quality_status IN ('passed', 'warned', 'rejected')",
            name="ck_sector_membership_snapshot_quality_status",
        ),
        CheckConstraint("member_count > 0"),
        CheckConstraint("verified_count >= 0"),
        CheckConstraint("pending_count >= 0"),
        CheckConstraint("quarantine_count >= 0"),
        CheckConstraint("member_count = verified_count + pending_count + quarantine_count"),
        UniqueConstraint("sector_key", "observed_at", "content_sha256"),
        Index(
            "ix_sector_membership_snapshot_complete",
            "sector_key",
            desc("observed_at"),
            postgresql_where="status = 'COMPLETE'",
        ),
        {"comment": "板块成分完整来源观察；不伪造实际调入或调出日期。"},
    )

    snapshot_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, nullable=False, comment="成分快照永久 UUID。"
    )
    sector_key: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("sector_entity.sector_key"),
        nullable=False,
        comment="本快照观察的内部板块键。",
    )
    source_batch_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("source_batch.source_batch_id"),
        unique=True,
        nullable=False,
        comment="承载本快照 raw evidence 的来源观察。",
    )
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="来源实际观察成分列表的时间。"
    )
    observation_date: Mapped[date] = mapped_column(
        Date, nullable=False, comment="观察对应的上海时区市场日期。"
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="完整可用或已隔离状态。"
    )
    quality_status: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="成分快照质量评估状态。"
    )
    member_count: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="来源返回的成分总数。"
    )
    verified_count: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="已解析到永久证券身份的成分数。"
    )
    pending_count: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="身份待确认的成分数。"
    )
    quarantine_count: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="因冲突或异常被隔离的成分数。"
    )
    content_sha256: Mapped[bytes] = mapped_column(nullable=False, comment="成分业务内容稳定摘要。")
    idempotency_key: Mapped[str] = mapped_column(
        CHAR(64), unique=True, nullable=False, comment="定位同一板块观察请求的幂等键。"
    )
