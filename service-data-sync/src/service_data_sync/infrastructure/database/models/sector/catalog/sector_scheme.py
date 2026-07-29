"""板块行业/概念分类体系的稳定命名空间与展示元数据模型。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ...base import Base


class SectorScheme(Base):
    """定义一个不与其他分类体系混用的板块身份命名空间。

    `scheme` 是机器可读的来源/分类边界，行业与概念更不能互相替代；同一来源代码在另一体系可能
    代表完全不同对象。显示名只服务维护与消费展示，不能作为外键或去重依据；新体系需显式登记，
    不能在同步时临时创建未治理 `scheme`。
    """

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
