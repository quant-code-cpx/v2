"""证券主数据快照成员模型。"""

from __future__ import annotations

from datetime import date
from uuid import UUID

from sqlalchemy import (
    CHAR,
    BigInteger,
    CheckConstraint,
    Date,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ...base import Base


class EquityMasterSnapshotMember(Base):
    """保存来源快照内每一行及其身份解析结果，允许未确认项留在快照中。"""

    __tablename__ = "equity_master_snapshot_member"
    __table_args__ = (
        CheckConstraint("row_ordinal > 0"),
        CheckConstraint(
            "exchange IN ('SSE', 'SZSE', 'BSE')", name="ck_equity_master_snapshot_member_exchange"
        ),
        CheckConstraint("symbol ~ '^[0-9]{6}$'", name="ck_equity_master_snapshot_member_symbol"),
        CheckConstraint(
            "effective_date_precision IN ('OFFICIAL_DATE', 'OBSERVATION_DATE')",
            name="ck_equity_master_snapshot_member_effective_date_precision",
        ),
        CheckConstraint(
            "resolution_status IN ('resolved', 'pending', 'conflict', 'rejected')",
            name="ck_equity_master_snapshot_member_resolution_status",
        ),
        UniqueConstraint("snapshot_id", "exchange", "symbol"),
        {"comment": "来源主数据快照的逐行观察和身份解析结果。"},
    )

    snapshot_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("equity_master_snapshot.snapshot_id"),
        primary_key=True,
        nullable=False,
        comment="所属主数据快照。",
    )
    row_ordinal: Mapped[int] = mapped_column(
        Integer, primary_key=True, nullable=False, comment="来源响应内从一开始的稳定行序号。"
    )
    exchange: Mapped[str] = mapped_column(String(4), nullable=False, comment="来源行声明的交易所。")
    symbol: Mapped[str] = mapped_column(
        CHAR(6), nullable=False, comment="来源行声明的六位证券代码。"
    )
    name: Mapped[str | None] = mapped_column(
        String(200), nullable=True, comment="来源行观察到的名称。"
    )
    listed_on: Mapped[date | None] = mapped_column(
        Date, nullable=True, comment="来源行可提供的上市日期。"
    )
    candidate_status: Mapped[str | None] = mapped_column(
        String(24), nullable=True, comment="来源行建议的生命周期状态。"
    )
    candidate_status_date: Mapped[date | None] = mapped_column(
        Date, nullable=True, comment="来源行建议状态的日期。"
    )
    effective_date_precision: Mapped[str] = mapped_column(
        String(24), nullable=False, comment="来源行生效日期的证据精度。"
    )
    security_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("equity_instrument.security_id"),
        nullable=True,
        comment="解析成功时绑定的永久证券；未解析时为空。",
    )
    resolution_status: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="身份解析结果。"
    )
    content_sha256: Mapped[bytes] = mapped_column(
        nullable=False, comment="来源行归一化内容稳定摘要。"
    )
