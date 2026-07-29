"""个股累计后复权因子 `revision`、来源血缘与窗口发布模型。"""

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
    desc,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ...base import Base


class EquityAdjustmentFactor(Base):
    """保存稀疏累计后复权因子及其来源 `revision`，查询时才计算前后复权价格。

    因子不是日线价格，也不能拿来原地更新未复权 OHLC；它只描述在指定业务日将原始价格转换到
    约定基准的乘数。来源可在公司行动后更正历史因子，因此同一证券日期追加 `revision` 并保留
    观察时间和内容摘要；缺失日期由查询口径处理，不能用相邻因子臆造来源事实。
    """

    __tablename__ = "equity_adjustment_factor"
    __table_args__ = (
        CheckConstraint("revision > 0"),
        CheckConstraint("cumulative_factor > 0"),
        CheckConstraint("valid_to IS NULL OR valid_to > valid_from"),
        Index(
            "uq_equity_adjustment_factor_current",
            "security_id",
            "effective_date",
            unique=True,
            postgresql_where="valid_to IS NULL",
        ),
        Index(
            "ix_equity_adjustment_factor_lookup",
            "security_id",
            desc("effective_date"),
            postgresql_include=["cumulative_factor", "factor_version"],
            postgresql_where="valid_to IS NULL",
        ),
        {"comment": "个股稀疏累计后复权因子 revision。"},
    )

    security_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("equity_instrument.security_id", ondelete="RESTRICT"),
        primary_key=True,
        nullable=False,
        comment="因子所属永久证券内部键。",
    )
    effective_date: Mapped[date] = mapped_column(
        Date, primary_key=True, nullable=False, comment="累计因子开始生效的市场日期。"
    )
    revision: Mapped[int] = mapped_column(
        Integer, primary_key=True, nullable=False, comment="同一生效日的递增修订号。"
    )
    cumulative_factor: Mapped[Decimal] = mapped_column(
        Numeric(38, 18), nullable=False, comment="精确累计后复权因子 F(d)。"
    )
    factor_version: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False, comment="该完整因子发布的稳定版本。"
    )
    content_sha256: Mapped[bytes] = mapped_column(nullable=False, comment="因子业务值稳定摘要。")
    source_batch_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("source_batch.source_batch_id", ondelete="RESTRICT"),
        nullable=False,
        comment="支撑本因子 revision 的来源观察。",
    )
    valid_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="本修订开始可见的知识时间。"
    )
    valid_to: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="本修订被替换的知识时间。"
    )
