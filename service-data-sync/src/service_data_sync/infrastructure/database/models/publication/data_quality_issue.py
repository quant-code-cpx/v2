"""需要运营处置的跨数据集质量问题模型。"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, desc
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base


class DataQualityIssue(Base):
    """记录需要运营处置的跨数据集质量问题，不替代细粒度规则结果表。

    这是一张面向工作流的异常清单，可关联 run、分区、来源批次或快照，但不能作为发布门的唯一
    证据；具体实际值、阈值和规则结论仍保存在所属数据域质量表。关闭问题表示人工处置完成，
    不会自动修复事实、重新发布数据或删除历史隔离证据。
    """

    __tablename__ = "data_quality_issue"
    __table_args__ = (
        CheckConstraint("severity IN ('warn', 'error')", name="ck_data_quality_issue_severity"),
        CheckConstraint("status IN ('open', 'resolved')", name="ck_data_quality_issue_status"),
        Index(
            "ix_data_quality_issue_open",
            "run_id",
            "partition_key",
            desc("created_at"),
            postgresql_where="status = 'open'",
        ),
        {"comment": "跨数据集的待处置质量问题及其审计状态。"},
    )

    issue_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, nullable=False, comment="质量问题永久 UUID。"
    )
    run_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("sync_run.run_id"),
        nullable=False,
        comment="发现问题的同步运行。",
    )
    partition_key: Mapped[str] = mapped_column(
        String(240), nullable=False, comment="问题所属的运行内数据分区。"
    )
    source_batch_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("source_batch.source_batch_id"),
        nullable=True,
        comment="相关来源观察；无单一观察时为空。",
    )
    snapshot_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("equity_master_snapshot.snapshot_id"),
        nullable=True,
        comment="相关主数据快照；不适用时为空。",
    )
    rule_code: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="触发问题的稳定质量规则编码。"
    )
    severity: Mapped[str] = mapped_column(String(16), nullable=False, comment="问题严重级别。")
    sample_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True, comment="用于人工定位的脱敏或有界样本。"
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="问题待处理或已解决状态。"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="问题被发现并登记的时间。"
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="人工或自动确认解决的时间。"
    )
