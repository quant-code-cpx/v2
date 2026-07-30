"""股票发现横截面的多值分类过滤索引模型。"""

from __future__ import annotations

from datetime import date
from uuid import UUID

from sqlalchemy import BigInteger, Date, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ...base import Base


class EquityDiscoveryMembership(Base):
    """保存冻结横截面中的行业、概念和申万归属，用于无 N+1 过滤与展示。"""

    __tablename__ = "equity_discovery_membership"
    __table_args__ = (
        Index(
            "ix_equity_discovery_membership_filter",
            "release_id",
            "scheme",
            "code",
            "security_id",
        ),
        {"comment": "股票发现横截面内行业、概念及申万多值归属索引。"},
    )

    release_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("dataset_release.release_id", ondelete="RESTRICT"),
        primary_key=True,
        nullable=False,
        comment="所属冻结横截面 release；消费者 dataVersion 由 publication 关联取得。",
    )
    security_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("equity_instrument.security_id", ondelete="RESTRICT"),
        primary_key=True,
        nullable=False,
        comment="归属对应永久证券内部键。",
    )
    scheme: Mapped[str] = mapped_column(
        String(64), primary_key=True, nullable=False, comment="分类体系稳定代码。"
    )
    code: Mapped[str] = mapped_column(
        String(64), primary_key=True, nullable=False, comment="分类体系内节点代码。"
    )
    name: Mapped[str] = mapped_column(
        String(200), nullable=False, comment="横截面冻结时的分类节点名称。"
    )
    level: Mapped[str | None] = mapped_column(
        String(24), nullable=True, comment="申万等层级体系中的级别；不适用时为空。"
    )
    observed_on: Mapped[date] = mapped_column(
        Date, nullable=False, comment="该归属来源快照的观察日期。"
    )
