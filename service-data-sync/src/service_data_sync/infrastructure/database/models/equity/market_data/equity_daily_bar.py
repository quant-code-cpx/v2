"""按上海交易日年度物理分区的个股来源直取未复权日线 `revision` 模型。"""

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
    """保存来源直取的未复权日线 `revision`；年度子分区只优化存储，不改变业务表语义。

    OHLC、成交量和成交额均按来源原始交易口径保存：价格不因后续分红拆并回写，成交量单位为股、
    成交额单位为人民币元。相同证券/交易日内容变化时追加 `revision` 并以 `valid_from`/`valid_to`
    记录平台何时采用该版本；`is_final` 仅说明来源对收盘完整性的判断，不等同于交易所最终更正。
    """

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
