"""板块成分消费者读取使用的完整快照集合固定 `release` 模型。"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, Numeric, String
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ...base import Base


class SectorMembershipRelease(Base):
    """固定一个分类体系成分读取所使用的完整快照集合，允许受控 `carry forward`。

    一个 `release` 声明消费者在某次读取中应使用哪些板块的已验收快照，防止逐板块“当前最新”造成
    混用时间点。受控延用只可引用先前完整、质量通过的快照并留存原因，不能把失败/空响应当作空集；
    新 `release` 或回滚均是指针切换，不修改历史快照、成员或观察区间。
    """

    __tablename__ = "sector_membership_release"
    __table_args__ = (
        CheckConstraint(
            "quality_status IN ('passed', 'warned')",
            name="ck_sector_membership_release_quality_status",
        ),
        CheckConstraint("expected_sector_count > 0"),
        CheckConstraint("fresh_sector_count >= 0"),
        CheckConstraint("carried_forward_sector_count >= 0"),
        CheckConstraint("identity_coverage_percent = 100"),
        CheckConstraint("excluded_identity_count = 0"),
        CheckConstraint(
            "fresh_sector_count + carried_forward_sector_count = expected_sector_count"
        ),
        Index(
            "uq_sector_membership_release_current",
            "scheme",
            unique=True,
            postgresql_where="superseded_at IS NULL",
        ),
        {"comment": "一个分类体系成分读取的不可变快照清单和可见数据版本。"},
    )

    release_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, nullable=False, comment="成分 release 永久 UUID。"
    )
    scheme: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("sector_scheme.scheme"),
        nullable=False,
        comment="release 所属分类体系。",
    )
    release_as_of: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="release 中最新组成快照的观察时间。"
    )
    coverage_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="release 中最早组成快照的观察时间。"
    )
    data_version: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        unique=True,
        nullable=False,
        comment="消费者读取绑定的不可变数据版本。",
    )
    quality_status: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="release 可见质量状态。"
    )
    expected_sector_count: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="发布时分类体系应覆盖的 ACTIVE 板块数。"
    )
    fresh_sector_count: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="使用当日新完整快照的板块数。"
    )
    carried_forward_sector_count: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="受控沿用较早快照的板块数。"
    )
    identity_coverage_percent: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), nullable=False, comment="release 内已确认永久身份的覆盖率，固定为 100%。"
    )
    excluded_identity_count: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="因身份问题被排除的数量，固定为零。"
    )
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="release 开始对读取方可见的时间。"
    )
    superseded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="被新 release 替换的时间；当前 release 为空。",
    )
