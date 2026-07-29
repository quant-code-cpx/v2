"""按快照日期月度物理分区的已确认板块成分行与来源证据模型。"""

from __future__ import annotations

from datetime import date
from uuid import UUID

from sqlalchemy import CHAR, BigInteger, CheckConstraint, Date, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ...base import Base


class SectorMembershipItem(Base):
    """保存一个快照内已确认成分；物理月分区不形成额外 `ORM` 类。

    行只有在来源代码被安全解析为永久 `security_id` 后写入，保存快照日期、板块、来源权重/排序等
    观测信息；它不是当前成员投影，也不自动形成观察区间。分区按快照月创建以优化存储和反向查询，
    历史重放必须落回原业务月，不能按今天日期或当前证券代码改写成员。
    """

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
