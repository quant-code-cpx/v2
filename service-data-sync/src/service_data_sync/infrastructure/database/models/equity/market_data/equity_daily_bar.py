"""按年物理分区的个股未复权日线模型。"""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ...base import Base


class EquityDailyBar(Base):
    """保存个股未复权日线 revision；物理年度子分区由 partition manager 管理。"""

    __tablename__ = "equity_daily_bar"
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
        {
            "postgresql_partition_by": "RANGE (trade_date)",
            "comment": "未复权个股日线 revision 父表；按 trade_date 年度物理分区。",
        },
    )

    security_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("equity_instrument.security_id"),
        primary_key=True,
        nullable=False,
        comment="日线所属永久证券内部键。",
    )
    trade_date: Mapped[date] = mapped_column(
        Date, primary_key=True, nullable=False, comment="上海时区交易日。"
    )
    revision: Mapped[int] = mapped_column(
        Integer, primary_key=True, nullable=False, comment="同一证券交易日的递增修订号。"
    )
    open_price: Mapped[float] = mapped_column(
        Numeric(20, 6), nullable=False, comment="来源直接提供的开盘价。"
    )
    high_price: Mapped[float] = mapped_column(
        Numeric(20, 6), nullable=False, comment="来源直接提供的最高价。"
    )
    low_price: Mapped[float] = mapped_column(
        Numeric(20, 6), nullable=False, comment="来源直接提供的最低价。"
    )
    close_price: Mapped[float] = mapped_column(
        Numeric(20, 6), nullable=False, comment="来源直接提供的收盘价。"
    )
    volume_shares: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="成交量，单位为股。"
    )
    amount_cny: Mapped[float] = mapped_column(
        Numeric(24, 4), nullable=False, comment="成交额，单位为人民币元。"
    )
    turnover_rate: Mapped[float | None] = mapped_column(
        Numeric(16, 10), nullable=True, comment="来源提供的换手率；缺失时为空。"
    )
    is_final: Mapped[bool] = mapped_column(
        Boolean, nullable=False, comment="来源是否标记该日线为最终值。"
    )
    content_sha256: Mapped[bytes] = mapped_column(nullable=False, comment="日线业务内容稳定摘要。")
    source_batch_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("source_batch.source_batch_id"),
        nullable=False,
        comment="支撑该日线 revision 的来源观察。",
    )
    valid_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="本修订开始作为当前版本可见的时间。"
    )
    valid_to: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="本修订被替换的时间；当前 revision 为空。"
    )
