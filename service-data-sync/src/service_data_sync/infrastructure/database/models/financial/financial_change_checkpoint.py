"""财务摘要变更检测 checkpoint 模型。"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import CHAR, CheckConstraint, DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base


class FinancialChangeCheckpoint(Base):
    """记录已成功发布的摘要版本，防止无变化时重复抓取完整财务报表。"""

    __tablename__ = "financial_change_checkpoint"
    __table_args__ = (
        CheckConstraint(
            "summary_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_financial_change_checkpoint_summary_sha256",
        ),
        {"comment": "财务摘要变更检测和恢复 checkpoint；只在 publication 成功后推进。"},
    )

    capability: Mapped[str] = mapped_column(
        String(64), primary_key=True, nullable=False, comment="对应的 provider-neutral 财务能力。"
    )
    partition_key: Mapped[str] = mapped_column(
        String(240), primary_key=True, nullable=False, comment="能力内可恢复的稳定摘要分区键。"
    )
    summary_sha256: Mapped[str] = mapped_column(
        CHAR(64), nullable=False, comment="最近成功处理的摘要内容稳定摘要。"
    )
    provider_watermark: Mapped[str | None] = mapped_column(
        String(240), nullable=True, comment="已验证语义时记录的来源增量水位；未知时为空。"
    )
    last_data_version: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("dataset_publication.data_version", ondelete="RESTRICT"),
        nullable=True,
        comment="最近一次成功 publication 的不可变数据版本。",
    )
    last_success_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="最近成功发布完成时间。"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="checkpoint 最近写入时间。"
    )
