"""板块成分快照逐规则数值证据、严重级别与发布处置模型。"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Numeric, String
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ...base import Base


class SectorMembershipQualityResult(Base):
    """记录一个成分快照对每条质量规则的数值证据和发布处置。

    完整性、重复项、身份解析、覆盖率和异常数量等结论必须逐规则保存实际值、阈值与严重级别，不能
    只存一个笼统状态。阻断规则会让快照留在候选/隔离路径，警告是否可发布由受控策略决定；质量行
    本身不修改成员、区间或旧 `release`，以便后续审计能复现当时为什么放行或拦截。
    """

    __tablename__ = "sector_membership_quality_result"
    __table_args__ = (
        CheckConstraint(
            "severity IN ('warn', 'error')", name="ck_sector_membership_quality_result_severity"
        ),
        CheckConstraint(
            "disposition IN ('publish', 'quarantine')",
            name="ck_sector_membership_quality_result_disposition",
        ),
        {"comment": "板块成分快照的结构化质量规则结果。"},
    )

    snapshot_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("sector_membership_snapshot.snapshot_id"),
        primary_key=True,
        nullable=False,
        comment="被评估的成分快照。",
    )
    rule_code: Mapped[str] = mapped_column(
        String(64), primary_key=True, nullable=False, comment="稳定质量规则编码。"
    )
    severity: Mapped[str] = mapped_column(String(16), nullable=False, comment="规则结果严重程度。")
    disposition: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="规则要求发布或隔离的处置。"
    )
    actual_value: Mapped[Decimal | None] = mapped_column(
        Numeric, nullable=True, comment="规则计算得到的实际数值；不适用时为空。"
    )
    expected_value: Mapped[Decimal | None] = mapped_column(
        Numeric, nullable=True, comment="规则阈值或期望数值；不适用时为空。"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="质量结果生成时间。"
    )
