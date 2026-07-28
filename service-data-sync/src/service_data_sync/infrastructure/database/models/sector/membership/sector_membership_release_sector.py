"""板块成分 release 组成清单模型。"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ...base import Base


class SectorMembershipReleaseSector(Base):
    """固定 release 中每个板块采用的成分快照，避免消费者读取混用时间点。"""

    __tablename__ = "sector_membership_release_sector"
    __table_args__ = (
        UniqueConstraint("release_id", "snapshot_id"),
        {"comment": "成分 release 到每个板块固定快照的不可变映射。"},
    )

    release_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("sector_membership_release.release_id"),
        primary_key=True,
        nullable=False,
        comment="所属成分 release。",
    )
    sector_key: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("sector_entity.sector_key"),
        primary_key=True,
        nullable=False,
        comment="release 内一个内部板块键。",
    )
    snapshot_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("sector_membership_snapshot.snapshot_id"),
        nullable=False,
        comment="该板块固定采用的完整成分快照。",
    )
    carried_forward: Mapped[bool] = mapped_column(
        Boolean, nullable=False, comment="是否沿用早于 release 目标日期的快照。"
    )
    snapshot_observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="所选快照实际观察时间。"
    )
