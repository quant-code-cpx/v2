"""申万三级行业归属快照行模型。"""

from __future__ import annotations

from datetime import date
from uuid import UUID

from sqlalchemy import BigInteger, Date, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ...base import Base


class SwMembershipItem(Base):
    """保存快照内证券到申万三级节点的唯一映射及来源纳入日期。"""

    __tablename__ = "sw_membership_item"
    __table_args__ = (
        Index("ix_sw_membership_item_node", "release_id", "third_level_node_code"),
        {"comment": "申万三级行业证券归属快照行；未解析身份不得写入。"},
    )

    release_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("sw_membership_release.release_id", ondelete="RESTRICT"),
        primary_key=True,
        nullable=False,
        comment="所属完整申万归属快照。",
    )
    security_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("equity_instrument.security_id", ondelete="RESTRICT"),
        primary_key=True,
        nullable=False,
        comment="已解析的永久证券内部键。",
    )
    third_level_node_code: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="对应申万三级行业稳定代码。"
    )
    source_included_on: Mapped[date | None] = mapped_column(
        Date,
        nullable=True,
        comment="来源披露的纳入日期；只作来源字段，不推断退出日期。",
    )
    source_symbol: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="来源观察到的证券代码，用于血缘核验。"
    )
    resolution_status: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="固定为 RESOLVED；未解析行进入隔离证据。"
    )
