"""板块分类体系模型。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ...base import Base


class SectorScheme(Base):
    """定义一个不与其他分类体系混用的板块身份命名空间。"""

    __tablename__ = "sector_scheme"
    __table_args__ = (
        CheckConstraint(
            "classification_kind IN ('industry', 'concept')",
            name="ck_sector_scheme_classification_kind",
        ),
        {"comment": "板块分类体系命名空间；行业和概念永不混算。"},
    )

    scheme: Mapped[str] = mapped_column(
        String(64), primary_key=True, nullable=False, comment="分类体系稳定 machine-readable 标识。"
    )
    display_name: Mapped[str] = mapped_column(
        Text, nullable=False, comment="面向维护者和消费者的分类体系展示名称。"
    )
    classification_kind: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="行业或概念分类类型。"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="分类体系登记时间。"
    )
