"""申万三级行业证券归属完整快照发布模型。"""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ...base import Base


class SwMembershipRelease(Base):
    """冻结一次申万三级行业成分观察，不把当前快照伪造成历史退出事实。"""

    __tablename__ = "sw_membership_release"
    __table_args__ = (
        CheckConstraint(
            "quality_status IN ('passed', 'warned')",
            name="ck_sw_membership_release_quality",
        ),
        CheckConstraint("record_count >= 0", name="ck_sw_membership_release_count"),
        UniqueConstraint(
            "scheme_version",
            "node_code",
            "observation_date",
            "source_batch_id",
            name="uq_sw_membership_release_observation",
        ),
        {"comment": "申万三级行业当前成分完整快照；观察日期不等于真实调入调出日。"},
    )

    release_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("dataset_release.release_id", ondelete="RESTRICT"),
        primary_key=True,
        nullable=False,
        comment="归属快照所属 canonical release；消费者 dataVersion 由 publication 关联取得。",
    )
    scheme_version: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="申万分类版本，例如 SW2021。"
    )
    node_code: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="本 release 对应的申万三级行业节点代码。"
    )
    observation_date: Mapped[date] = mapped_column(
        Date, nullable=False, comment="Asia/Shanghai 当前成分快照观察日期。"
    )
    source_batch_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("source_batch.source_batch_id", ondelete="RESTRICT"),
        nullable=False,
        comment="支撑完整快照的真实来源批次。",
    )
    quality_status: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="通过或带告警的完整性质量结论。"
    )
    record_count: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="已安全解析的证券归属行数。"
    )
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="本快照进入消费者可见面的时间。"
    )
