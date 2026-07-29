"""新市场数据域共享的 revision 血缘与时间列 mixin。"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import CHAR, DateTime, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column


class CanonicalRevisionMixin:
    """为强类型事实提供不可变 release、方法学、来源、知识时间和质量字段。"""

    methodology_version_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("methodology_version.methodology_version_id", ondelete="RESTRICT"),
        nullable=False,
        comment="解释本事实字段口径的冻结方法学版本 UUID。",
    )
    release_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("dataset_release.release_id", ondelete="RESTRICT"),
        nullable=False,
        comment="包含本事实的不可变 canonical release UUID。",
    )
    revision_no: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="同一逻辑事实内容变化时递增的修订号。"
    )
    source_batch_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("source_batch.source_batch_id", ondelete="RESTRICT"),
        nullable=False,
        comment="为本事实提供直接证据的来源观察批次。",
    )
    source_published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="可验证来源发布时间；无证据时为空。"
    )
    source_time_precision: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="来源公开时间精度，精确、仅日期或未知。"
    )
    public_usable_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="市场 PIT 可最早安全使用的时间；不确定时为空。",
    )
    availability_basis: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="公开可见性依据，禁止以平台观察时间伪装来源时间。"
    )
    known_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="系统开始采用本修订知识版本的时间。"
    )
    known_to: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="系统停止采用本修订知识版本的时间。"
    )
    content_hash: Mapped[str] = mapped_column(
        CHAR(64), nullable=False, comment="本强类型事实业务内容的 SHA-256 摘要。"
    )
    quality_status: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="行或快照质量结论，不能以 release 状态替代。"
    )
