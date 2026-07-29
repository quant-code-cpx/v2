"""按业务交易日和平台知识时间保存的资金流日序列 `revision` 模型。"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    CHAR,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base


class MoneyFlowDailyObservation(Base):
    """按 `trade_date` 保存供应商日序列的双时态 `revision`。

    一条观察绑定方法学、scope、样本池、分桶和窗口，因此只在同一完整序列内才可比较。`trade_date`
    是来源定义的业务交易日，不是抓取时间；`known_*` 保留平台何时知道该值。来源更正、单位治理
    或内容改变追加版本，不能用排行快照、相邻交易日或其他方法学数值填补缺失。
    """

    __tablename__ = "money_flow_daily_observation"
    __table_args__ = (
        UniqueConstraint(
            "series_id",
            "trade_date",
            "revision",
            name="uq_money_flow_daily_observation_revision",
        ),
        CheckConstraint(
            "gross_inflow IS NULL OR gross_inflow >= 0",
            name="ck_money_flow_daily_observation_inflow",
        ),
        CheckConstraint(
            "gross_outflow IS NULL OR gross_outflow >= 0",
            name="ck_money_flow_daily_observation_outflow",
        ),
        CheckConstraint(
            "num_nonnulls(gross_inflow, gross_outflow, net_amount, net_ratio) > 0",
            name="ck_money_flow_daily_observation_measure",
        ),
        CheckConstraint(
            "known_to IS NULL OR known_from < known_to",
            name="ck_money_flow_daily_observation_knowledge",
        ),
        CheckConstraint(
            "content_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_money_flow_daily_observation_hash",
        ),
        CheckConstraint(
            "quality_status IN ('passed', 'warned')",
            name="ck_money_flow_daily_observation_quality",
        ),
        Index(
            "uq_money_flow_daily_observation_current",
            "series_id",
            "trade_date",
            unique=True,
            postgresql_where="known_to IS NULL",
        ),
        Index(
            "ix_money_flow_daily_observation_read",
            "series_id",
            "trade_date",
            postgresql_where="known_to IS NULL",
        ),
        {
            "comment": "资金流逐日知识修订；回补按真实 known_from 追加，不回写历史。",
            "postgresql_partition_by": "RANGE (trade_date)",
        },
    )

    trade_date: Mapped[date] = mapped_column(
        Date, primary_key=True, nullable=False, comment="来源记录对应的交易日。"
    )
    observation_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, nullable=False, comment="日观察修订永久 UUID。"
    )
    series_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("money_flow_series.series_id"),
        nullable=False,
        comment="所属强身份序列。",
    )
    revision: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="同一序列交易日内递增的知识修订号。"
    )
    gross_inflow: Mapped[Decimal | None] = mapped_column(
        Numeric(30, 6), nullable=True, comment="来源支持时的流入总额。"
    )
    gross_outflow: Mapped[Decimal | None] = mapped_column(
        Numeric(30, 6), nullable=True, comment="来源支持时的流出总额。"
    )
    net_amount: Mapped[Decimal | None] = mapped_column(
        Numeric(30, 6), nullable=True, comment="来源报告的净额。"
    )
    net_ratio: Mapped[Decimal | None] = mapped_column(
        Numeric(20, 8), nullable=True, comment="统一为十进制比率的净占比。"
    )
    source_batch_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("source_batch.source_batch_id"),
        nullable=False,
        comment="本修订对应的不可变来源观察。",
    )
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="adapter 实际取得来源响应的时刻。"
    )
    known_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="平台开始知道该修订的真实时刻。"
    )
    known_to: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="被后续修订替换的知识时刻。"
    )
    content_sha256: Mapped[str] = mapped_column(
        CHAR(64), nullable=False, comment="四度量 canonical 内容哈希。"
    )
    quality_status: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="进入 publication 前通过的质量级别。"
    )
