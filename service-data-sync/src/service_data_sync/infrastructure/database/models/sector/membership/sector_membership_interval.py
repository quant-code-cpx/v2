"""板块成分观察区间模型。"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
    Index,
    UniqueConstraint,
    desc,
)
from sqlalchemy.dialects.postgresql import TSTZRANGE, ExcludeConstraint, Range
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ...base import Base


class SectorMembershipInterval(Base):
    """表达完整快照观察到的半开成分区间，不宣称真实调入或调出发生时间。"""

    __tablename__ = "sector_membership_interval"
    __table_args__ = (
        CheckConstraint("observed_to IS NULL OR observed_to > observed_from"),
        CheckConstraint(
            "(observed_to IS NULL AND close_snapshot_id IS NULL) "
            "OR (observed_to IS NOT NULL AND close_snapshot_id IS NOT NULL)"
        ),
        UniqueConstraint("sector_key", "security_id", "observed_from"),
        ExcludeConstraint(
            ("sector_key", "="), ("security_id", "="), ("observation_range", "&&"), using="gist"
        ),
        Index(
            "uq_sector_membership_interval_current",
            "sector_key",
            "security_id",
            unique=True,
            postgresql_where="observed_to IS NULL",
        ),
        Index("ix_sector_membership_interval_reverse", "security_id", desc("observed_from")),
        {"comment": "完整快照推导的成分观察区间；时间端点不是实际事件日期。"},
    )

    interval_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        autoincrement=True,
        nullable=False,
        comment="观察区间内部自增键。",
    )
    sector_key: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("sector_entity.sector_key"),
        nullable=False,
        comment="观察该成分的内部板块键。",
    )
    security_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("equity_instrument.security_id"),
        nullable=False,
        comment="被观察到的永久证券内部键。",
    )
    observed_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="首次在完整快照中观察到该关系的时间。"
    )
    observed_to: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="首次确认缺席的观察时间；当前关系为空。"
    )
    open_snapshot_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("sector_membership_snapshot.snapshot_id"),
        nullable=False,
        comment="打开该观察区间的完整快照。",
    )
    close_snapshot_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("sector_membership_snapshot.snapshot_id"),
        nullable=True,
        comment="关闭该观察区间的完整快照；当前关系为空。",
    )
    observation_range: Mapped[Range[datetime] | None] = mapped_column(
        TSTZRANGE,
        Computed("tstzrange(observed_from, observed_to, '[)')", persisted=True),
        nullable=True,
        comment="由观察端点生成、供排斥约束使用的半开时间范围。",
    )
