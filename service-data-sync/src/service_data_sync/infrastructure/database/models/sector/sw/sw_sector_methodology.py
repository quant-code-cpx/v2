"""申万行业来源与估值方法学版本模型。"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import CHAR, CheckConstraint, DateTime, SmallInteger, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ...base import Base


class SwSectorMethodology(Base):
    """固化乐咕申万展示口径，避免 taxonomy 与估值失去来源版本。"""

    __tablename__ = "sw_sector_methodology"
    __table_args__ = (
        CheckConstraint("version > 0", name="ck_sw_sector_methodology_version"),
        CheckConstraint(
            "status IN ('source_reported', 'retired')",
            name="ck_sw_sector_methodology_status",
        ),
        CheckConstraint(
            "semantic_spec_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_sw_sector_methodology_semantic_sha256",
        ),
        UniqueConstraint("code", "version", name="uq_sw_sector_methodology_code_version"),
        {"comment": "申万行业 taxonomy 与估值上游展示方法学的不可变版本身份。"},
    )

    methodology_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, nullable=False, comment="方法学永久 UUID。"
    )
    code: Mapped[str] = mapped_column(
        String(80), nullable=False, comment="不含 URL 的稳定方法学代码。"
    )
    version: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, comment="同一方法学代码内递增的不可变版本。"
    )
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, comment="来源报告或已退役状态。"
    )
    upstream_source: Mapped[str] = mapped_column(
        String(120), nullable=False, comment="展示 taxonomy 与估值的上游来源身份。"
    )
    semantic_spec_sha256: Mapped[str] = mapped_column(
        CHAR(64), nullable=False, comment="层级、单位、最终态和字段语义说明摘要。"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="方法学版本首次登记时间。"
    )
