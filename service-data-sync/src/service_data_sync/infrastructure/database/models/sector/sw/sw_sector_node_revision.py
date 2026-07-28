"""申万行业节点双时间修订模型。"""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from sqlalchemy import (
    CHAR,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ...base import Base


class SwSectorNodeRevision(Base):
    """保存指定观测日的三级节点、直接父级和成分数知识修订。"""

    __tablename__ = "sw_sector_node_revision"
    __table_args__ = (
        CheckConstraint("level BETWEEN 1 AND 3", name="ck_sw_sector_node_level"),
        CheckConstraint("component_count >= 0", name="ck_sw_sector_node_component_count"),
        CheckConstraint("revision > 0", name="ck_sw_sector_node_revision_number"),
        CheckConstraint(
            "(level = 1 AND parent_code IS NULL) OR (level > 1 AND parent_code IS NOT NULL)",
            name="ck_sw_sector_node_parent_level",
        ),
        CheckConstraint(
            "known_to IS NULL OR known_to > known_from",
            name="ck_sw_sector_node_known_range",
        ),
        CheckConstraint(
            "content_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_sw_sector_node_content_sha256",
        ),
        CheckConstraint(
            "quality_status IN ('passed', 'warned', 'quarantined')",
            name="ck_sw_sector_node_quality_status",
        ),
        UniqueConstraint(
            "snapshot_date",
            "sector_code",
            "revision",
            name="uq_sw_sector_node_snapshot_code_revision",
        ),
        Index(
            "uq_sw_sector_node_current",
            "snapshot_date",
            "sector_code",
            unique=True,
            postgresql_where="known_to IS NULL",
        ),
        Index(
            "ix_sw_sector_node_hierarchy",
            "snapshot_date",
            "level",
            "parent_code",
            "sector_code",
        ),
        {"comment": "申万三级 taxonomy 节点的按观测日双时间知识修订。"},
    )

    node_revision_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, nullable=False, comment="节点修订永久 UUID。"
    )
    node_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False, comment="由申万代码确定的跨快照稳定节点 UUID。"
    )
    snapshot_date: Mapped[date] = mapped_column(
        Date, nullable=False, comment="该完整 taxonomy 快照的上海日历观测日期。"
    )
    sector_code: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="带 `.SI` 后缀的申万稳定行业代码。"
    )
    name: Mapped[str] = mapped_column(
        String(200), nullable=False, comment="该修订中的申万行业显示名称。"
    )
    level: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, comment="申万一级、二级或三级层级。"
    )
    parent_code: Mapped[str | None] = mapped_column(
        String(16), nullable=True, comment="二级或三级节点的直接父级申万代码。"
    )
    component_count: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="上游页面在该观测日展示的成分数量。"
    )
    methodology_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("sw_sector_methodology.methodology_id", ondelete="RESTRICT"),
        nullable=False,
        comment="支撑本节点语义的不可变方法学版本。",
    )
    revision: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="同一观测日与代码内递增的知识修订号。"
    )
    known_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="平台首次可使用本修订的 UTC 时间。"
    )
    known_to: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="本知识修订被替代的半开区间结束时间。"
    )
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="实际取得上游完整快照的 UTC 时间。"
    )
    source_batch_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("source_batch.source_batch_id", ondelete="RESTRICT"),
        nullable=False,
        comment="支撑本节点修订的 raw evidence 来源批次。",
    )
    content_sha256: Mapped[str] = mapped_column(
        CHAR(64), nullable=False, comment="节点名称、层级、父级和成分数的稳定摘要。"
    )
    quality_status: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="通过、警告或隔离的质量处置。"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="本 canonical 修订写入时间。"
    )
