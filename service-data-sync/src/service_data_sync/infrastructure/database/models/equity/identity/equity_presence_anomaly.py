"""已知证券在来源目录中连续缺席的观测异常与人工处置模型。"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CHAR,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ...base import Base


class EquityPresenceAnomaly(Base):
    """记录已知证券连续缺席目录快照的异常，不把缺席自动解释为退市。

    来源截断、权限变化、上市地过滤和代码展示差异都可能导致缺席；本表只累计可审计的异常证据并
    支持人工跟进。它不修改 `EquityListingStatusVersion`、不回收 `security_id`、也不关闭代码版本，
    避免一次不完整目录把消费者历史和当前可读性错误地切断。
    """

    __tablename__ = "equity_presence_anomaly"
    __table_args__ = (
        CheckConstraint(
            "exchange IN ('SSE', 'SZSE', 'BSE')", name="ck_equity_presence_anomaly_exchange"
        ),
        CheckConstraint("symbol ~ '^[0-9]{6}$'", name="ck_equity_presence_anomaly_symbol"),
        CheckConstraint("consecutive_count > 0"),
        CheckConstraint("status IN ('open', 'resolved')", name="ck_equity_presence_anomaly_status"),
        Index(
            "uq_equity_presence_anomaly_open",
            "security_id",
            unique=True,
            postgresql_where="status = 'open'",
        ),
        {"comment": "目录连续缺席异常；需要人工或专用生命周期证据处理。"},
    )

    anomaly_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, nullable=False, comment="缺席异常永久 UUID。"
    )
    security_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("equity_instrument.security_id"),
        nullable=False,
        comment="发生缺席异常的永久证券。",
    )
    exchange: Mapped[str] = mapped_column(
        String(4), nullable=False, comment="异常时证券所属交易所兼容投影。"
    )
    symbol: Mapped[str] = mapped_column(
        CHAR(6), nullable=False, comment="异常时观察到的六位证券代码。"
    )
    first_missing_snapshot_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("equity_master_snapshot.snapshot_id"),
        nullable=False,
        comment="首次发现缺席的主数据快照。",
    )
    last_missing_snapshot_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("equity_master_snapshot.snapshot_id"),
        nullable=False,
        comment="最近发现缺席的主数据快照。",
    )
    consecutive_count: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="连续缺席快照次数。"
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="异常待处理或已解决状态。"
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="异常被确认解决的时间。"
    )
