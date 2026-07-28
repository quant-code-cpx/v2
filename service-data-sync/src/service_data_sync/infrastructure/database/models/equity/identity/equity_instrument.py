"""证券永久身份锚模型。"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Index, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ...base import Base


class EquityInstrument(Base):
    """保存证券永久 UUID 与当前兼容投影；历史解析必须使用版本表。"""

    __tablename__ = "equity_instrument"
    __table_args__ = (
        CheckConstraint("exchange IN ('SSE', 'SZSE', 'BSE')", name="ck_equity_instrument_exchange"),
        CheckConstraint("symbol ~ '^[0-9]{6}$'", name="ck_equity_instrument_symbol"),
        CheckConstraint(
            "listing_status IN ('PENDING', 'LISTED', 'SUSPENDED', 'DELISTED')",
            name="ck_equity_instrument_listing_status",
        ),
        UniqueConstraint("exchange", "symbol"),
        Index("ix_equity_instrument_exchange_symbol", "exchange", "symbol"),
        {"comment": "证券内部永久身份与当前兼容投影；不以名称或状态作为永久身份。"},
    )

    security_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        autoincrement=True,
        nullable=False,
        comment="数据库内部大表关联键，不对外暴露。",
    )
    instrument_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        unique=True,
        nullable=False,
        comment="证券永久 UUID，不随代码复用或状态变化。",
    )
    exchange: Mapped[str] = mapped_column(
        String(4), nullable=False, comment="当前兼容投影所属交易所。"
    )
    symbol: Mapped[str] = mapped_column(
        String(6), nullable=False, comment="当前六位证券代码；历史以 identifier version 为准。"
    )
    name: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="当前展示名称；历史名称以 name version 为准。"
    )
    listing_status: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="当前兼容投影上市状态；历史以 lifecycle version 为准。"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="证券身份记录创建时间。"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="当前兼容投影最近更新时间。"
    )
    master_confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="主数据身份最近被确认的时间。"
    )
    current_master_version: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True, comment="当前主数据版本标识，供兼容读取定位。"
    )
