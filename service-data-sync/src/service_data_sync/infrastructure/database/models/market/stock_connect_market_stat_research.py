"""AKShare 沪深港通市场统计的私有 research 观察模型。

该组表保存 adapter 已标准化、但尚未达到官方完整包门槛的来源观察。它们只关联来源批次、
规范化和质量记录，绝不关联 `DatasetRelease`、`DatasetPublication`、PIT revision 或现有
HKEX 官方 bundle；研究成功也只能使用摘要型 `unretained://` manifest。
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CHAR,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base


class StockConnectMarketStatResearchBatch(Base):
    """保存一次 AKShare 标准市场统计批次的 research-only 血缘头。

    一次抓取即使没有返回交易日记录，也需要留下来源 batch、digest-only manifest、规范化和
    质量结论。该头不代表正式通道数据集，不允许产生 `dataVersion` 或被业务读取仓储选择。
    """

    __tablename__ = "stock_connect_market_stat_research_batch"
    __table_args__ = (
        CheckConstraint("channel IN ('SH', 'SZ')", name="ck_sc_msr_batch_channel"),
        CheckConstraint(
            "direction IN ('NORTHBOUND', 'SOUTHBOUND')",
            name="ck_sc_msr_batch_direction",
        ),
        CheckConstraint("record_count >= 0", name="ck_sc_msr_batch_record_count"),
        CheckConstraint(
            "quality_status IN ('passed', 'warned')",
            name="ck_sc_msr_batch_quality",
        ),
        CheckConstraint("status = 'research'", name="ck_sc_msr_batch_status"),
        CheckConstraint(
            "normalized_payload_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_sc_msr_batch_normalized_hash",
        ),
        UniqueConstraint("source_batch_id", name="uq_sc_msr_batch_source_batch"),
        UniqueConstraint(
            "research_batch_id",
            "channel",
            "direction",
            name="uq_sc_msr_batch_channel_direction",
        ),
        Index(
            "ix_sc_msr_batch_lookup",
            "channel",
            "direction",
            "observed_at",
        ),
        {
            "comment": (
                "AKShare 港通市场统计的私有研究批次；不创建正式 release、publication 或 PIT。"
            )
        },
    )

    research_batch_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        nullable=False,
        comment="research 批次永久 UUID。",
    )
    dataset_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("canonical_dataset.dataset_id", ondelete="RESTRICT"),
        nullable=False,
        comment="固定为 research-only 市场统计 canonical 数据集。",
    )
    source_batch_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("source_batch.source_batch_id", ondelete="RESTRICT"),
        nullable=False,
        comment="本次 AKShare 标准载荷对应的精确来源批次。",
    )
    normalization_run_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("normalization_run.normalization_run_id", ondelete="RESTRICT"),
        nullable=False,
        comment="将该标准载荷映射为 research 观察的确定性规范化运行。",
    )
    channel: Mapped[str] = mapped_column(
        String(8), nullable=False, comment="沪股通或深股通通道代码。"
    )
    direction: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="北向或南向交易方向。"
    )
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="adapter 实际观察来源响应的时间。"
    )
    record_count: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="该标准批次中唯一交易日观察数；可为零。"
    )
    normalized_payload_sha256: Mapped[str] = mapped_column(
        CHAR(64), nullable=False, comment="标准化 JSON 载荷的 SHA-256 摘要。"
    )
    quality_status: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="research 可用性质量结论；不构成消费者发布资格。"
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="固定为 research，防止被正式港通读取路径选择。"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="research 批次写入数据库的时间。"
    )


class StockConnectMarketStatResearchObservation(Base):
    """保存一日来源报告的港通市场统计，字段缺失保持真实空值。

    该表不复用官方 `stock_connect_channel_daily_revision`，因此不存在披露制度推断、PIT
    有效期、release 或 publication。金额、币种、可用性和逐字段状态都只表示该次 AKShare
    观察，不能与官方来源或其他日期补齐。
    """

    __tablename__ = "stock_connect_market_stat_research_observation"
    __table_args__ = (
        ForeignKeyConstraint(
            ["research_batch_id", "channel", "direction"],
            [
                "stock_connect_market_stat_research_batch.research_batch_id",
                "stock_connect_market_stat_research_batch.channel",
                "stock_connect_market_stat_research_batch.direction",
            ],
            name="fk_sc_msr_observation_batch",
            ondelete="RESTRICT",
        ),
        CheckConstraint("channel IN ('SH', 'SZ')", name="ck_sc_msr_observation_channel"),
        CheckConstraint(
            "direction IN ('NORTHBOUND', 'SOUTHBOUND')",
            name="ck_sc_msr_observation_direction",
        ),
        CheckConstraint(
            "currency IS NULL OR currency ~ '^[A-Z]{3}$'",
            name="ck_sc_msr_observation_currency",
        ),
        CheckConstraint(
            "availability_status IS NULL OR length(btrim(availability_status)) > 0",
            name="ck_sc_msr_observation_availability",
        ),
        CheckConstraint(
            "field_availability IS NULL OR jsonb_typeof(field_availability) = 'object'",
            name="ck_sc_msr_observation_field_availability",
        ),
        CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'",
            name="ck_sc_msr_observation_content_hash",
        ),
        Index(
            "ix_sc_msr_observation_lookup",
            "channel",
            "direction",
            "trade_date",
        ),
        {
            "comment": (
                "AKShare 港通市场统计单日 research 观察；可选字段保持来源缺失，不进入正式读取。"
            )
        },
    )

    research_batch_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        nullable=False,
        comment="所属 research 批次 UUID。",
    )
    trade_date: Mapped[date] = mapped_column(
        Date, primary_key=True, nullable=False, comment="来源报告的交易日。"
    )
    channel: Mapped[str] = mapped_column(
        String(8), nullable=False, comment="与所属批次一致的沪或深通道代码。"
    )
    direction: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="与所属批次一致的北向或南向方向。"
    )
    buy_amount: Mapped[Decimal | None] = mapped_column(
        Numeric(38, 6), nullable=True, comment="来源报告买入成交额；未提供时为空。"
    )
    sell_amount: Mapped[Decimal | None] = mapped_column(
        Numeric(38, 6), nullable=True, comment="来源报告卖出成交额；未提供时为空。"
    )
    turnover_amount: Mapped[Decimal | None] = mapped_column(
        Numeric(38, 6), nullable=True, comment="来源报告总成交额；当前来源未提供时为空。"
    )
    net_buy_amount: Mapped[Decimal | None] = mapped_column(
        Numeric(38, 6), nullable=True, comment="来源报告当日净买额；不能由其他金额推导。"
    )
    quota_balance: Mapped[Decimal | None] = mapped_column(
        Numeric(38, 6), nullable=True, comment="来源报告当日额度余额；未提供时为空。"
    )
    currency: Mapped[str | None] = mapped_column(
        String(3), nullable=True, comment="来源报告金额币种 ISO 代码；缺失时为空。"
    )
    availability_status: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="来源批次报告的整体字段可用性状态；缺失时为空。"
    )
    field_availability: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB(none_as_null=True),
        nullable=True,
        comment="来源明确给出的逐字段可用性状态；未提供时为 SQL 空值。",
    )
    content_hash: Mapped[str] = mapped_column(
        CHAR(64), nullable=False, comment="该标准观察全部字段的稳定 SHA-256 摘要。"
    )
