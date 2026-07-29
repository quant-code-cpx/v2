"""申万行业结构、估值字段、来源页面和语义摘要的固定方法学版本模型。"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import CHAR, CheckConstraint, DateTime, SmallInteger, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ...base import Base


class SwSectorMethodology(Base):
    """固化申万展示口径，避免 `taxonomy` 与估值失去来源/版本关系。

    行业代码层级、父级名称解析、成分数、静态/滚动市盈率、市净率和股息率的含义均取决于来源页面与
    方法学版本。语义摘要和来源引用让结构与估值能在历史回放中一起解释；口径变化必须新增版本，
    不能把新页面字段映射覆盖到旧观测日。
    """

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
