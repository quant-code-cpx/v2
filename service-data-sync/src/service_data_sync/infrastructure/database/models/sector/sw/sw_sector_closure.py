"""申万行业发布级父级闭包模型。"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, Index, SmallInteger, String
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ...base import Base


class SwSectorClosure(Base):
    """冻结一个 taxonomy dataVersion 中祖先到后代的完整闭包。"""

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
