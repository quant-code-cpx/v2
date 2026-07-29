"""证券代码复用、交易所冲突或无法解析身份的私有隔离模型。"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ...base import Base


class EquityIdentityQuarantine(Base):
    """隔离可能代码复用或身份冲突的主数据行，禁止自动绑定错误证券。

    目录代码相同、交易所信息缺失或来源时间矛盾时，宁可保留待处置证据，也不能猜测归属并污染
    永久身份。隔离记录不是退市结论，不会关闭现有代码版本或删除快照成员；只有可追溯的人工或
    官方证据完成解析后，仓储才可以创建新的双时态版本。
    """

    __tablename__ = "equity_identity_quarantine"
    __table_args__ = (
        CheckConstraint(
            "status IN ('open', 'resolved', 'dismissed')",
            name="ck_equity_identity_quarantine_status",
        ),
        ForeignKeyConstraint(
            ["snapshot_id", "row_ordinal"],
            [
                "equity_master_snapshot_member.snapshot_id",
                "equity_master_snapshot_member.row_ordinal",
            ],
        ),
        Index(
            "ix_equity_identity_quarantine_open",
            "snapshot_id",
            "row_ordinal",
            postgresql_where="status = 'open'",
        ),
        {"comment": "主数据身份冲突隔离队列；必须经明确审核后才可解析。"},
    )

    issue_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, nullable=False, comment="身份隔离问题永久 UUID。"
    )
    snapshot_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("equity_master_snapshot.snapshot_id"),
        nullable=False,
        comment="产生冲突的主数据快照。",
    )
    row_ordinal: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="快照内发生冲突的来源行序号。"
    )
    conflict_code: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="稳定的身份冲突分类编码。"
    )
    candidate_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, comment="待审核来源行及候选身份的结构化证据。"
    )
    related_security_ids: Mapped[list[int]] = mapped_column(
        ARRAY(BigInteger),
        server_default=text("'{}'"),
        nullable=False,
        comment="可能与该冲突相关的永久证券内部键集合。",
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="隔离问题待处理、已解决或已驳回状态。"
    )
    reviewed_by: Mapped[str | None] = mapped_column(
        String(128), nullable=True, comment="完成审核的操作者标识。"
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="审核完成时间。"
    )
    resolution: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="审核结论或处理方式编码。"
    )
