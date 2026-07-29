"""申万 `taxonomy` 和估值按观测日/方法学对消费者可见的冻结 `data_version` 明细模型。"""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from sqlalchemy import CHAR, CheckConstraint, Date, DateTime, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ...base import Base


class SwSectorPublication(Base):
    """把 `capability`、观测日和方法学绑定到不可变 `data_version`。

    行业结构和估值能力可分别发布，但每一版本都精确指向同一类完整、质量通过的候选；消费者读取
    必须按该明细锁定观测日、方法学、业务/知识截点，不能临时取“最新节点”与“最新估值”。回滚
    通过受控切换版本实现，已发布数据、质量和来源血缘均保留。
    """

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
