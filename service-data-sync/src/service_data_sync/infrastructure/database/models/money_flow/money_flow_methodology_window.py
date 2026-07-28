"""资金流方法学窗口定义模型。"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base


class MoneyFlowMethodologyWindow(Base):
    """冻结 daily source 与 supplier rolling 的不可混用窗口。"""

    __tablename__ = "money_flow_methodology_window"
    __table_args__ = (
        CheckConstraint(
            "window_type IN ('daily_source', 'supplier_day', 'supplier_rolling')",
            name="ck_money_flow_methodology_window_type",
        ),
        CheckConstraint(
            "window_size > 0",
            name="ck_money_flow_methodology_window_size",
        ),
        CheckConstraint(
            "(window_type IN ('daily_source', 'supplier_day') AND window_size = 1) "
            "OR (window_type = 'supplier_rolling' AND window_size > 1)",
            name="ck_money_flow_methodology_window_semantics",
        ),
        {"comment": "方法学版本支持的来源窗口；逐日序列与供应商滚动快照永不混用。"},
    )

    version_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("money_flow_methodology_version.version_id"),
        primary_key=True,
        nullable=False,
        comment="所属方法学版本。",
    )
    window_type: Mapped[str] = mapped_column(
        String(32), primary_key=True, nullable=False, comment="窗口语义类型。"
    )
    window_size: Mapped[int] = mapped_column(
        Integer, primary_key=True, nullable=False, comment="供应商声明的窗口大小。"
    )
    source_label: Mapped[str] = mapped_column(
        String(100), nullable=False, comment="上游用于该窗口的稳定展示标签。"
    )
