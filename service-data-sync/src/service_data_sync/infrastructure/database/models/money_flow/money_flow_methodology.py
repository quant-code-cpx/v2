"""资金流方法学稳定身份模型。"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base


class MoneyFlowMethodology(Base):
    """保存不随算法版本变化的公开方法学身份。"""

    __tablename__ = "money_flow_methodology"
    __table_args__ = (
        UniqueConstraint(
            "public_key",
            name="uq_money_flow_methodology_public_key",
        ),
        {"comment": "资金流方法学稳定身份；具体来源、算法与单位由版本表冻结。"},
    )

    methodology_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, nullable=False, comment="方法学永久 UUID。"
    )
    public_key: Mapped[str] = mapped_column(
        String(100), nullable=False, comment="API 使用的稳定方法学标识。"
    )
    owner: Mapped[str] = mapped_column(
        String(100), nullable=False, comment="维护该方法学定义的平台能力标识。"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="方法学稳定身份创建时间。"
    )
