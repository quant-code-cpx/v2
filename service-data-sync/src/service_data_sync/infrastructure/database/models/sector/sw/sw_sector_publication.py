"""申万 taxonomy 与估值消费者发布明细模型。"""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from sqlalchemy import CHAR, CheckConstraint, Date, DateTime, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ...base import Base


class SwSectorPublication(Base):
    """把 capability、观测日和方法学绑定到不可变 dataVersion。"""

    __tablename__ = "sw_sector_publication"
    __table_args__ = (
        CheckConstraint(
            "capability IN ('sector.sw.taxonomy', 'sector.sw.valuation')",
            name="ck_sw_sector_publication_capability",
        ),
        CheckConstraint("row_count > 0", name="ck_sw_sector_publication_row_count"),
        CheckConstraint(
            "content_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_sw_sector_publication_content_sha256",
        ),
        {"comment": "申万 taxonomy 或估值消费者发布的不可变明细。"},
    )

    data_version: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("dataset_publication.data_version", ondelete="RESTRICT"),
        primary_key=True,
        nullable=False,
        comment="同时关联通用发布指针的消费者不可变版本。",
    )
    capability: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="申万 taxonomy 或行业估值能力。"
    )
    snapshot_date: Mapped[date] = mapped_column(
        Date, nullable=False, comment="本发布完整覆盖的上海日历观测日期。"
    )
    methodology_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("sw_sector_methodology.methodology_id", ondelete="RESTRICT"),
        nullable=False,
        comment="本发布冻结的来源展示方法学版本。",
    )
    row_count: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="本发布包含的完整行业行数。"
    )
    content_sha256: Mapped[str] = mapped_column(
        CHAR(64), nullable=False, comment="本发布规范化完整内容稳定摘要。"
    )
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="本版本开始对内部消费者可见的时间。"
    )
