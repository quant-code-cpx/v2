"""普通停复牌日事实的双时间 `revision` 模型。"""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ...base import Base


class EquityTradingStatusRevision(Base):
    """保存普通交易停复牌事实，并与暂停上市生命周期严格分离。

    `SUSPENDED` 在本表仅表示目标交易日普通停牌，不能写入
    `equity_listing_status_version`。同一证券和交易日内容变化时追加
    `revision`，知识区间只表达平台何时采用该事实。
    """

    __tablename__ = "equity_trading_status_revision"
    __table_args__ = (
        CheckConstraint(
            "status IN ('TRADED', 'SUSPENDED', 'RESUMED')",
            name="ck_equity_trading_status_revision_status",
        ),
        CheckConstraint("revision > 0", name="ck_equity_trading_status_revision_no"),
        CheckConstraint(
            "known_to IS NULL OR known_to > known_from",
            name="ck_equity_trading_status_revision_knowledge",
        ),
        Index(
            "uq_equity_trading_status_revision_current",
            "security_id",
            "trade_date",
            unique=True,
            postgresql_where="known_to IS NULL",
        ),
        Index(
            "ix_equity_trading_status_revision_date",
            "trade_date",
            "status",
            "security_id",
        ),
        {"comment": "A 股普通停复牌日事实；不表达暂停上市或退市生命周期。"},
    )

    security_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("equity_instrument.security_id", ondelete="RESTRICT"),
        primary_key=True,
        nullable=False,
        comment="交易状态所属永久证券内部键。",
    )
    trade_date: Mapped[date] = mapped_column(
        Date, primary_key=True, nullable=False, comment="Asia/Shanghai 交易日。"
    )
    revision: Mapped[int] = mapped_column(
        Integer, primary_key=True, nullable=False, comment="同证券同交易日递增修订号。"
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="普通交易状态：成交、停牌或复牌。"
    )
    market: Mapped[str] = mapped_column(
        String(8), nullable=False, comment="来源明确披露的 SSE、SZSE 或 BSE 市场。"
    )
    suspended_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="来源披露的停牌时刻；仅有日期时为空。"
    )
    resumed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="来源披露的复牌时刻；仅有日期时为空。"
    )
    reason: Mapped[str | None] = mapped_column(
        String(500), nullable=True, comment="来源披露的停复牌原因；未知时为空。"
    )
    source_batch_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("source_batch.source_batch_id", ondelete="RESTRICT"),
        nullable=False,
        comment="支撑本修订的真实来源批次。",
    )
    content_sha256: Mapped[bytes] = mapped_column(
        nullable=False, comment="标准业务内容的 SHA-256 摘要。"
    )
    known_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="本修订开始作为平台当前知识的时刻。"
    )
    known_to: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="被后继修订替换的时刻；当前版本为空。"
    )
