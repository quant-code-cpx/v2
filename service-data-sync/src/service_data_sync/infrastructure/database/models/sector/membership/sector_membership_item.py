"""按月物理分区的已确认板块成分模型。"""

from __future__ import annotations

from datetime import date
from uuid import UUID

from sqlalchemy import CHAR, BigInteger, CheckConstraint, Date, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ...base import Base


class SectorMembershipItem(Base):
    """保存一个快照内已确认成分；物理月分区不形成额外 ORM class。"""

    __tablename__ = "sector_membership_item"
    __table_args__ = (
        CheckConstraint(
            "source_symbol ~ '^[0-9]{6}$'", name="ck_sector_membership_item_source_symbol"
        ),
        CheckConstraint("BTRIM(source_name) <> ''", name="ck_sector_membership_item_source_name"),
        UniqueConstraint("snapshot_date", "snapshot_id", "source_symbol"),
        {
            "postgresql_partition_by": "RANGE (snapshot_date)",
            "comment": "已确认板块成分快照行；按 snapshot_date 物理分区。",
        },
    )

    snapshot_date: Mapped[date] = mapped_column(
        Date, primary_key=True, nullable=False, comment="物理分区和快照对应的观察日期。"
    )
    snapshot_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("sector_membership_snapshot.snapshot_id"),
        primary_key=True,
        nullable=False,
        comment="所属完整成分快照。",
    )
    security_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("equity_instrument.security_id"),
        primary_key=True,
        nullable=False,
        comment="已确认的永久证券内部键。",
    )
    source_symbol: Mapped[str] = mapped_column(
        CHAR(6), nullable=False, comment="来源观察到的六位证券代码。"
    )
    source_name: Mapped[str] = mapped_column(
        String(200), nullable=False, comment="来源观察到的证券名称。"
    )
    content_sha256: Mapped[bytes] = mapped_column(
        nullable=False, comment="该成分行归一化内容摘要。"
    )
