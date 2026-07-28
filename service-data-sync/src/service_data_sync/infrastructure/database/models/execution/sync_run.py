"""同步运行总账模型。"""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, Date, DateTime, Index, String, desc
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base


class SyncRun(Base):
    """记录一个可重跑同步请求的整体状态，不能替代各分区 checkpoint。"""

    __tablename__ = "sync_run"
    __table_args__ = (
        CheckConstraint(
            "mode IN ('manual', 'scheduled', 'backfill', 'legacy')",
            name="ck_sync_run_mode",
        ),
        CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'partial', 'failed', 'cancelled')",
            name="ck_sync_run_status",
        ),
        Index("ix_sync_run_capability_requested_at", "capability", desc("requested_at")),
        {"comment": "一次同步请求的总账；细粒度恢复状态由 sync_partition 保存。"},
    )

    run_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, nullable=False, comment="同步运行永久 UUID。"
    )
    capability: Mapped[str] = mapped_column(
        String(100), nullable=False, comment="本运行请求的中立数据能力。"
    )
    mode: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="手工、调度、回补或迁移遗留运行方式。"
    )
    request_key: Mapped[str] = mapped_column(
        String(240), unique=True, nullable=False, comment="保证同一逻辑请求幂等的稳定键。"
    )
    target_date: Mapped[date | None] = mapped_column(
        Date, nullable=True, comment="任务目标交易日或观察日；无日期任务为空。"
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="运行整体终态或进行中状态。"
    )
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="请求进入同步系统的时间。"
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="实际开始处理时间；尚未开始为空。"
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="最终停止时间；运行中为空。"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="运行账本记录创建时间。"
    )
