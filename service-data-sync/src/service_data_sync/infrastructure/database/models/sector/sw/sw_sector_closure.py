"""申万三级行业某个 `data_version` 内祖先到后代的完整父级闭包模型。"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, Index, SmallInteger, String
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ...base import Base


class SwSectorClosure(Base):
    """冻结一个 `taxonomy data_version` 中祖先到后代的完整闭包。

    闭包让一级、二级、三级行业的导航和聚合不必运行时递归，且能明确祖先、后代和层级距离。它只
    适用于与其绑定的发布版本，不能拿当前节点关系解释旧估值或旧 `taxonomy`；任何孤儿、环或层级
    跳跃必须在质量门阻断，不能通过查询时补父节点修复。
    """

    __tablename__ = "sw_sector_closure"
    __table_args__ = (
        CheckConstraint("depth BETWEEN 0 AND 2", name="ck_sw_sector_closure_depth"),
        CheckConstraint(
            "(depth = 0 AND ancestor_code = descendant_code) OR depth > 0",
            name="ck_sw_sector_closure_self_edge",
        ),
        Index(
            "ix_sw_sector_closure_descendant",
            "data_version",
            "descendant_code",
            "depth",
        ),
        {"comment": "申万 taxonomy 不可变发布中的自反及父级闭包边。"},
    )

    data_version: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("sw_sector_publication.data_version", ondelete="CASCADE"),
        primary_key=True,
        nullable=False,
        comment="闭包所属 taxonomy 不可变发布版本。",
    )
    ancestor_code: Mapped[str] = mapped_column(
        String(16), primary_key=True, nullable=False, comment="祖先申万行业代码。"
    )
    descendant_code: Mapped[str] = mapped_column(
        String(16), primary_key=True, nullable=False, comment="后代申万行业代码。"
    )
    depth: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, comment="零表示自反边，一或二表示父级距离。"
    )
