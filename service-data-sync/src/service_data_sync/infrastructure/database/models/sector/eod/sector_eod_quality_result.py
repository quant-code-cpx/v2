"""板块 EOD 快照质量证据模型。"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ...base import Base


class SectorEodQualityResult(Base):
    """记录 EOD 快照每条质量规则的结构化实际值、阈值与是否通过。"""

    __tablename__ = "sector_eod_quality_result"
    __table_args__ = (
        CheckConstraint(
            "severity IN ('info', 'warning', 'blocking')",
            name="ck_sector_eod_quality_result_severity",
        ),
        UniqueConstraint("snapshot_id", "rule_code"),
        Index("ix_sector_eod_quality_result_lookup", "snapshot_id", "severity", "passed"),
        {"comment": "EOD 完整横截面质量门的结构化证据，不保存可从 raw 恢复的响应。"},
    )

    quality_result_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, nullable=False, comment="质量结果永久 UUID。"
    )
    snapshot_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("sector_eod_snapshot.snapshot_id", ondelete="RESTRICT"),
        nullable=False,
        comment="被评估的 EOD 快照。",
    )
    rule_code: Mapped[str] = mapped_column(String(64), nullable=False, comment="稳定质量规则编码。")
    severity: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="规则失败时的严重程度。"
    )
    passed: Mapped[bool] = mapped_column(nullable=False, comment="该规则是否通过。")
    actual: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, comment="规则实际计算值和有界证据。"
    )
    threshold: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, comment="本次规则使用的阈值或期望。"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="质量结果生成时间。"
    )
