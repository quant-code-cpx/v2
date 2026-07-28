"""板块 EOD 分区恢复账本模型。"""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, Date, DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ...base import Base


class SectorEodSyncPartition(Base):
    """保存一个 scheme/交易日 EOD 同步的租约、阶段、来源 checkpoint 与稳定错误码。"""

    __tablename__ = "sector_eod_sync_partition"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'partial', 'failed', 'cancelled')",
            name="ck_sector_eod_sync_partition_status",
        ),
        CheckConstraint(
            "stage IN ('requested', 'fetched', 'raw_archived', 'normalized', "
            "'quality_passed', 'published')",
            name="ck_sector_eod_sync_partition_stage",
        ),
        CheckConstraint("attempt >= 0"),
        CheckConstraint(
            "(lease_owner IS NULL AND lease_token IS NULL AND lease_expires_at IS NULL) "
            "OR (lease_owner IS NOT NULL AND lease_token IS NOT NULL "
            "AND lease_expires_at IS NOT NULL)"
        ),
        Index(
            "ix_sector_eod_sync_partition_reclaim",
            "status",
            "lease_expires_at",
            postgresql_where="status IN ('queued', 'running', 'partial')",
        ),
        Index("ix_sector_eod_sync_partition_run", "run_id"),
        {"comment": "EOD scheme/交易日恢复账本；lease token 防止过期 worker 继续发布。"},
    )

    scheme: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("sector_scheme.scheme"),
        primary_key=True,
        nullable=False,
        comment="EOD 分类体系。",
    )
    trade_date: Mapped[date] = mapped_column(
        Date, primary_key=True, nullable=False, comment="上海时区目标交易日。"
    )
    run_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("sync_run.run_id"),
        nullable=False,
        comment="当前或最近处理该分区的同步运行。",
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="EOD 分区执行或终态状态。"
    )
    stage: Mapped[str] = mapped_column(
        String(24), nullable=False, comment="EOD 处理链条已完成的最后阶段。"
    )
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, comment="该分区已尝试处理次数。")
    lease_owner: Mapped[str | None] = mapped_column(
        String(100), nullable=True, comment="当前持有 EOD 分区租约的 worker 标识。"
    )
    lease_token: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True, comment="fencing token；写入必须匹配当前租约。"
    )
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="EOD 分区租约失效时间。"
    )
    last_source_batch_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("source_batch.source_batch_id"),
        nullable=True,
        comment="最近已归档来源观察；raw replay 从此恢复。",
    )
    last_error_code: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="最近失败稳定错误码。"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="分区账本最后更新时间。"
    )
