"""保存港股通范围内由 HKEX 稳定证券 ID 锚定的工具身份。"""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, Date, DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base


class StockConnectHkexInstrumentIdentity(Base):
    """把 HKEX Securities Master 的稳定证券 ID 唯一绑定到平台工具。

    来源代码只进入日期化 `InstrumentIdentifierVersion`，不会参与永久实体 ID 的生成；本表仅登记
    曾进入港股通活跃榜范围的证券，完整 Securities Master 快照只用于确认这些身份的存续与缺席。
    """

    __tablename__ = "stock_connect_hkex_instrument_identity"
    __table_args__ = (
        CheckConstraint(
            "length(btrim(source_security_id)) > 0",
            name="ck_stock_connect_hkex_identity_source_id",
        ),
        CheckConstraint(
            "last_seen_on >= first_seen_on",
            name="ck_stock_connect_hkex_identity_seen_range",
        ),
        CheckConstraint(
            "updated_at >= created_at",
            name="ck_stock_connect_hkex_identity_timestamps",
        ),
        UniqueConstraint(
            "instrument_id",
            name="uq_stock_connect_hkex_identity_instrument",
        ),
        Index(
            "ix_stock_connect_hkex_identity_last_seen",
            "last_seen_on",
        ),
        {"comment": ("港股通范围 HKEX 稳定证券 ID 到平台工具的唯一映射；代码和名称不承担根身份。")},
    )

    source_security_id: Mapped[str] = mapped_column(
        String(64),
        primary_key=True,
        nullable=False,
        comment="Securities Master 官方稳定证券 ID，按来源原文保留。",
    )
    instrument_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("market_instrument.instrument_id", ondelete="RESTRICT"),
        nullable=False,
        comment="由稳定证券 ID 唯一生成的平台工具 UUID。",
    )
    first_seen_on: Mapped[date] = mapped_column(
        Date,
        nullable=False,
        comment="该稳定证券 ID 首次进入港股通范围身份集合的业务日。",
    )
    last_seen_on: Mapped[date] = mapped_column(
        Date,
        nullable=False,
        comment="完整 Securities Master 最近一次确认该证券仍在册的业务日。",
    )
    first_source_batch_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("source_batch.source_batch_id", ondelete="RESTRICT"),
        nullable=False,
        comment="首次建立稳定身份的官方主档来源批次。",
    )
    last_source_batch_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("source_batch.source_batch_id", ondelete="RESTRICT"),
        nullable=False,
        comment="最近一次确认稳定身份仍在册的官方主档来源批次。",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment="平台首次登记该稳定映射的时间。",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment="最近一次完整主档确认或生命周期变更的时间。",
    )
