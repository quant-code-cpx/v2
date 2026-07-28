"""个股市场数据能力的增量同步检查点模型。"""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from sqlalchemy import Date, DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ...base import Base


class EquitySyncCheckpoint(Base):
    """按能力和证券分区记录最近成功窗口，不跨周期共享水位。"""

    __tablename__ = "equity_sync_checkpoint"
    __table_args__ = ({"comment": "个股行情、因子、公司行动与概况的独立同步检查点。"},)

    capability: Mapped[str] = mapped_column(
        String(80), primary_key=True, nullable=False, comment="数据源无关能力名。"
    )
    partition_key: Mapped[str] = mapped_column(
        String(160), primary_key=True, nullable=False, comment="通常为交易所限定证券代码。"
    )
    last_window_end: Mapped[date | None] = mapped_column(
        Date, nullable=True, comment="最近成功包含端窗口日期；无日期能力为空。"
    )
    data_version: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("dataset_publication.data_version", ondelete="RESTRICT"),
        nullable=False,
        comment="最近成功发布的数据版本。",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="检查点最近推进时间。"
    )
