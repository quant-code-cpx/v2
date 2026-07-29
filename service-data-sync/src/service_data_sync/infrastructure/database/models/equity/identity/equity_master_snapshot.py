"""按交易所取得的证券主数据来源快照头、完整性和质量结论模型。"""

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
    String,
    desc,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ...base import Base


class EquityMasterSnapshot(Base):
    """记录一个交易所主数据来源快照的完整性、质量与业务摘要。

    快照头是“一次来源目录观察”的不可变封面，记录覆盖范围、行数、来源摘要、质量和处理状态；
    具体行在成员表中保存。只有确认完整且通过质量门的快照才能激活或更新当前身份投影，空响应、
    截断或解析异常只能留下审计结果，不能据此关闭已有证券或生成生命周期事件。
    """

    __tablename__ = "equity_master_snapshot"
    __table_args__ = (
        CheckConstraint(
            "exchange IN ('SSE', 'SZSE', 'BSE')", name="ck_equity_master_snapshot_exchange"
        ),
        CheckConstraint(
            "snapshot_kind IN ('CATALOG', 'LIFECYCLE')", name="ck_equity_master_snapshot_kind"
        ),
        CheckConstraint("row_count >= 0"),
        CheckConstraint("schema_fingerprint ~ '^[0-9a-f]{64}$'"),
        CheckConstraint(
            "completeness IN ('COMPLETE', 'PARTIAL', 'REJECTED')",
            name="ck_equity_master_snapshot_completeness",
        ),
        CheckConstraint(
            "quality_status IN ('passed', 'warned', 'rejected')",
            name="ck_equity_master_snapshot_quality_status",
        ),
        Index("ix_equity_master_snapshot_lookup", "exchange", "snapshot_kind", desc("observed_at")),
        {"comment": "一次交易所主数据或生命周期来源快照的质量和内容摘要。"},
    )

    snapshot_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, nullable=False, comment="主数据快照永久 UUID。"
    )
    exchange: Mapped[str] = mapped_column(String(4), nullable=False, comment="本快照覆盖的交易所。")
    snapshot_kind: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="目录或生命周期快照类型。"
    )
    target_date: Mapped[date] = mapped_column(
        Date, nullable=False, comment="同步请求针对的业务日期。"
    )
    source_batch_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("source_batch.source_batch_id"),
        unique=True,
        nullable=False,
        comment="承载本快照原始证据的唯一来源观察。",
    )
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="来源实际观察时间。"
    )
    row_count: Mapped[int] = mapped_column(Integer, nullable=False, comment="来源快照行数。")
    schema_fingerprint: Mapped[str] = mapped_column(
        CHAR(64), nullable=False, comment="来源行结构 SHA-256 指纹。"
    )
    completeness: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="来源快照完整、部分或拒绝状态。"
    )
    quality_status: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="快照质量评估结果。"
    )
    business_sha256: Mapped[bytes] = mapped_column(
        nullable=False, comment="归一化业务内容的稳定摘要。"
    )
