"""同步请求内可独立重试的数据分区、租约和恢复 checkpoint 模型。"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base


class SyncPartition(Base):
    """保存一个 run 内可独立重试分区的租约、状态、错误码与恢复 checkpoint。

    一次 `SyncRun` 可拆成多个业务日期、证券或分类体系分区；任一分区失败不应重做已成功同伴。
    `lease_owner`、到期时间和心跳防止两个 worker 同时发布同一分区，`attempt` 与 `next_retry_at`
    记录可恢复重试节奏。这里的 `checkpoint_json` 是任务内部进度，不能误作已发布的 canonical
    水位；只有后续 publication 成功才能推进跨运行 checkpoint。
    """

    __tablename__ = "sync_partition"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'partial', 'failed', 'cancelled')",
            name="ck_sync_partition_status",
        ),
        CheckConstraint("attempt > 0", name="ck_sync_partition_attempt"),
        Index(
            "ix_sync_partition_reclaim",
            "lease_until",
            "next_retry_at",
            postgresql_where="status IN ('queued', 'running', 'partial')",
        ),
        {"comment": "run 内一个可恢复分区的任务账本；租约过期后可由 reaper 回收。"},
    )

    run_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("sync_run.run_id"),
        primary_key=True,
        nullable=False,
        comment="所属同步运行。",
    )
    partition_key: Mapped[str] = mapped_column(
        String(240), primary_key=True, nullable=False, comment="run 内唯一的数据分区稳定键。"
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="该分区当前执行或终态状态。"
    )
    attempt: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="该分区已开始的处理尝试次数。"
    )
    lease_owner: Mapped[str | None] = mapped_column(
        String(128), nullable=True, comment="当前持有处理租约的 worker 标识。"
    )
    lease_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="当前租约失效时间；为空表示未占用。"
    )
    heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="worker 最近一次续租心跳时间。"
    )
    next_retry_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="可重试失败允许再次调度的最早时间。"
    )
    checkpoint_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True, comment="恢复任务所需的结构化进度，不存放 raw 响应。"
    )
    error_code: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="最近一次终止或待重试失败的稳定错误码。"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="分区账本最近更新时间。"
    )
