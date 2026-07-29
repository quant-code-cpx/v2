"""资金流方法学版本可支持的 `scope` 类型、样本池与来源范围模型。"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base


class MoneyFlowMethodologyScope(Base):
    """逐行声明方法学版本支持的 `scope` 类型和 `universe`。

    个股、板块和市场的净流入不因字段相同而可互换；每个 `scope` 还需明确哪些证券、板块或市场
    样本属于该统计范围。来源页面、指数成分或分类目录变更时，必须通过受控样本池版本表达，不能
    让应用层按当前名单随意过滤历史序列。
    """

    __tablename__ = "money_flow_methodology_scope"
    __table_args__ = (
        CheckConstraint(
            "scope_type IN ('equity', 'sector', 'market')",
            name="ck_money_flow_methodology_scope_type",
        ),
        {"comment": "方法学版本的适用 scope 与 universe；不以数组隐藏身份。"},
    )

    version_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("money_flow_methodology_version.version_id"),
        primary_key=True,
        nullable=False,
        comment="所属方法学版本。",
    )
    scope_type: Mapped[str] = mapped_column(
        String(16), primary_key=True, nullable=False, comment="equity、sector 或 market。"
    )
    universe_id: Mapped[str] = mapped_column(
        String(100), primary_key=True, nullable=False, comment="方法学声明的 universe 稳定标识。"
    )
