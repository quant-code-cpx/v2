"""板块周频直接行情 revision 模型。"""

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
    String,
    desc,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ...base import Base


class SectorWeeklyBar(Base):
    """保存上游直接提供的板块周线；不由日线聚合。"""

    __tablename__ = "sector_weekly_bar"
    __table_args__ = (
        CheckConstraint("revision > 0"),
        CheckConstraint("open_price >= 0"),
        CheckConstraint("high_price >= 0"),
        CheckConstraint("low_price >= 0"),
        CheckConstraint("close_price >= 0"),
        CheckConstraint("volume_value >= 0"),
        CheckConstraint("volume_unit = 'provider_native'"),
        CheckConstraint("amount_cny >= 0"),
        CheckConstraint("low_price <= LEAST(open_price, close_price)"),
        CheckConstraint("high_price >= GREATEST(open_price, close_price)"),
        CheckConstraint("low_price <= high_price"),
        CheckConstraint("amplitude_percent IS NULL OR amplitude_percent >= 0"),
        CheckConstraint("turnover_percent IS NULL OR turnover_percent >= 0"),
        Index(
            "uq_sector_weekly_bar_current",
            "sector_key",
            "period_end",
            unique=True,
            postgresql_where="valid_to IS NULL",
        ),
        Index(
            "ix_sector_weekly_bar_current_read",
            "sector_key",
            desc("period_end"),
            postgresql_include=[
                "open_price",
                "high_price",
                "low_price",
                "close_price",
                "volume_value",
                "volume_unit",
                "amount_cny",
                "amplitude_percent",
                "change_percent",
                "change_amount",
                "turnover_percent",
                "is_final",
                "revision",
            ],
            postgresql_where="valid_to IS NULL",
        ),
        {"comment": "上游直接板块周线 revision；当前行由 valid_to 为空表示。"},
    )

    sector_key: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("sector_entity.sector_key"),
        primary_key=True,
        nullable=False,
        comment="行情所属内部板块键。",
    )
    period_end: Mapped[date] = mapped_column(
        Date, primary_key=True, nullable=False, comment="周线周期结束交易日。"
    )
    revision: Mapped[int] = mapped_column(
        Integer, primary_key=True, nullable=False, comment="同一板块周期末的递增修订号。"
    )
    open_price: Mapped[Decimal] = mapped_column(
        Numeric(20, 6), nullable=False, comment="来源直接提供的开盘值。"
    )
    high_price: Mapped[Decimal] = mapped_column(
        Numeric(20, 6), nullable=False, comment="来源直接提供的最高值。"
    )
    low_price: Mapped[Decimal] = mapped_column(
        Numeric(20, 6), nullable=False, comment="来源直接提供的最低值。"
    )
    close_price: Mapped[Decimal] = mapped_column(
        Numeric(20, 6), nullable=False, comment="来源直接提供的收盘值。"
    )
    volume_value: Mapped[Decimal] = mapped_column(
        Numeric(24, 4), nullable=False, comment="来源原生单位的成交量或成交额数值。"
    )
    volume_unit: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="成交量字段单位，固定 provider_native。"
    )
    amount_cny: Mapped[Decimal] = mapped_column(
        Numeric(24, 4), nullable=False, comment="成交额，单位为人民币元。"
    )
    amplitude_percent: Mapped[Decimal | None] = mapped_column(
        Numeric(16, 10), nullable=True, comment="振幅百分比；来源缺失时为空。"
    )
    change_percent: Mapped[Decimal | None] = mapped_column(
        Numeric(16, 10), nullable=True, comment="涨跌幅百分比；来源缺失时为空。"
    )
    change_amount: Mapped[Decimal | None] = mapped_column(
        Numeric(24, 6), nullable=True, comment="涨跌额；来源缺失时为空。"
    )
    turnover_percent: Mapped[Decimal | None] = mapped_column(
        Numeric(16, 10), nullable=True, comment="换手率百分比；来源缺失时为空。"
    )
    is_final: Mapped[bool] = mapped_column(
        Boolean, nullable=False, comment="来源是否声明该周期值为最终值。"
    )
    content_sha256: Mapped[bytes] = mapped_column(nullable=False, comment="行情业务内容稳定摘要。")
    source_batch_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("source_batch.source_batch_id"),
        nullable=False,
        comment="支撑本 revision 的来源观察。",
    )
    valid_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="本 revision 开始作为当前版本的时间。"
    )
    valid_to: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="本 revision 被替换时间；当前行为空。"
    )
