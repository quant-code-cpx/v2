"""证券历史股本结构的双时间 `revision` 模型。"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ...base import Base


class EquityShareCapitalRevision(Base):
    """保存来源报告的股本历史，供未复权收盘价派生市值。

    数量单位统一为股；`listed_tradable_a_shares` 表示已上市流通 A 股，
    不是供应商所谓自由流通盘。禁止从供应商市值或成交额反推本表数值。
    """

    __tablename__ = "equity_share_capital_revision"
    __table_args__ = (
        CheckConstraint("revision > 0", name="ck_equity_share_capital_revision_no"),
        CheckConstraint("total_shares > 0", name="ck_equity_share_capital_total"),
        CheckConstraint(
            "listed_tradable_a_shares IS NULL OR "
            "(listed_tradable_a_shares >= 0 AND listed_tradable_a_shares <= total_shares)",
            name="ck_equity_share_capital_listed_a",
        ),
        CheckConstraint(
            "restricted_shares IS NULL OR "
            "(restricted_shares >= 0 AND restricted_shares <= total_shares)",
            name="ck_equity_share_capital_restricted",
        ),
        CheckConstraint(
            "known_to IS NULL OR known_to > known_from",
            name="ck_equity_share_capital_knowledge",
        ),
        Index(
            "uq_equity_share_capital_revision_current",
            "security_id",
            "effective_on",
            unique=True,
            postgresql_where="known_to IS NULL",
        ),
        Index(
            "ix_equity_share_capital_effective",
            "security_id",
            "effective_on",
            "known_from",
        ),
        {"comment": "A 股来源报告股本历史；股数单位为股，禁止由市值反推。"},
    )

    security_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("equity_instrument.security_id", ondelete="RESTRICT"),
        primary_key=True,
        nullable=False,
        comment="股本事实所属永久证券内部键。",
    )
    effective_on: Mapped[date] = mapped_column(
        Date, primary_key=True, nullable=False, comment="来源披露的股本结构生效日。"
    )
    revision: Mapped[int] = mapped_column(
        Integer, primary_key=True, nullable=False, comment="同证券同生效日递增修订号。"
    )
    total_shares: Mapped[Decimal] = mapped_column(
        Numeric(24, 0), nullable=False, comment="公司总股本，单位为股。"
    )
    listed_tradable_a_shares: Mapped[Decimal | None] = mapped_column(
        Numeric(24, 0), nullable=True, comment="已上市流通 A 股，单位为股；未知时为空。"
    )
    restricted_shares: Mapped[Decimal | None] = mapped_column(
        Numeric(24, 0), nullable=True, comment="来源披露限售股，单位为股；未知时为空。"
    )
    change_reason: Mapped[str | None] = mapped_column(
        String(500), nullable=True, comment="来源披露股本变化原因；未知时为空。"
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
        DateTime(timezone=True), nullable=False, comment="平台开始采用本修订的知识时刻。"
    )
    known_to: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="被后继修订替换的时刻；当前版本为空。"
    )
