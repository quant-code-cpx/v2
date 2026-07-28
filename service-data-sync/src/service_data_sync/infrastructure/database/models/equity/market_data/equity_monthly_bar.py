"""上游直取的个股未复权月线模型。"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    desc,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ...base import Base


class EquityMonthlyBar(Base):
    """保存上游月线接口直接返回的 revision，不允许由日线聚合写入。"""

    __tablename__ = "equity_monthly_bar"
    __table_args__ = (
        CheckConstraint("revision > 0"),
        CheckConstraint("open_price >= 0"),
        CheckConstraint("high_price >= 0"),
        CheckConstraint("low_price >= 0"),
        CheckConstraint("close_price >= 0"),
        CheckConstraint("volume_shares >= 0"),
        CheckConstraint("amount_cny >= 0"),
        CheckConstraint("low_price <= LEAST(open_price, close_price)"),
        CheckConstraint("high_price >= GREATEST(open_price, close_price)"),
        CheckConstraint("low_price <= high_price"),
        CheckConstraint("valid_to IS NULL OR valid_to > valid_from"),
        Index(
            "ix_equity_monthly_bar_read",
            "security_id",
            desc("period_end"),
            postgresql_include=[
                "open_price",
                "high_price",
                "low_price",
                "close_price",
                "volume_shares",
                "amount_cny",
            ],
        ),
        {
            "postgresql_partition_by": "RANGE (period_end)",
            "comment": "上游直取的未复权个股月线 revision；按 period_end 年度分区。",
        },
    )

    security_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("equity_instrument.security_id", ondelete="RESTRICT"),
        primary_key=True,
        nullable=False,
        comment="月线所属永久证券内部键。",
    )
    period_end: Mapped[date] = mapped_column(
        Date, primary_key=True, nullable=False, comment="上游月线周期结束日。"
    )
    revision: Mapped[int] = mapped_column(
        Integer, primary_key=True, nullable=False, comment="同一证券周期的递增修订号。"
    )
    open_price: Mapped[Decimal] = mapped_column(
        Numeric(20, 6), nullable=False, comment="上游月线直接提供的开盘价。"
    )
    high_price: Mapped[Decimal] = mapped_column(
        Numeric(20, 6), nullable=False, comment="上游月线直接提供的最高价。"
    )
    low_price: Mapped[Decimal] = mapped_column(
        Numeric(20, 6), nullable=False, comment="上游月线直接提供的最低价。"
    )
    close_price: Mapped[Decimal] = mapped_column(
        Numeric(20, 6), nullable=False, comment="上游月线直接提供的收盘价。"
    )
    volume_shares: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="上游月线成交量，统一为股。"
    )
    amount_cny: Mapped[Decimal] = mapped_column(
        Numeric(24, 4), nullable=False, comment="上游月线成交额，单位人民币元。"
    )
    turnover_rate: Mapped[Decimal | None] = mapped_column(
        Numeric(16, 10), nullable=True, comment="上游月线换手率，统一为小数。"
    )
    is_final: Mapped[bool] = mapped_column(
        Boolean, nullable=False, comment="该月线是否已经形成完整周期。"
    )
    content_sha256: Mapped[bytes] = mapped_column(nullable=False, comment="月线业务内容稳定摘要。")
    source_batch_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("source_batch.source_batch_id", ondelete="RESTRICT"),
        nullable=False,
        comment="支撑该月线 revision 的独立来源观察。",
    )
    valid_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="本修订开始可见的知识时间。"
    )
    valid_to: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="本修订被后继版本替换的知识时间。"
    )
