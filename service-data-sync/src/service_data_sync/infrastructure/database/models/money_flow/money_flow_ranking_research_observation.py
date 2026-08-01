"""不完整供应商资金流排行的私有研究观察头模型。"""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from sqlalchemy import (
    CHAR,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base


class MoneyFlowRankingResearchObservation(Base):
    """保存未证明完整性的排行来源观察，永不产生消费者 `publication`。

    `AKShare` 合并后的排行页没有分页总数，因而只能作为研究观察保存。该表与正式
    `money_flow_ranking_snapshot` 分离，防止不完整来源页借由状态或时间字段误入 PIT
    读取；每条记录都绑定精确 `source_batch` 与标准载荷摘要，便于后续复核或方法学评审。
    """

    __tablename__ = "money_flow_ranking_research_observation"
    __table_args__ = (
        CheckConstraint(
            "scope_type IN ('equity', 'sector')",
            name="ck_money_flow_ranking_research_scope",
        ),
        CheckConstraint(
            "window_type IN ('supplier_day', 'supplier_rolling')",
            name="ck_money_flow_ranking_research_window",
        ),
        CheckConstraint(
            "(window_type = 'supplier_day' AND window_size = 1) "
            "OR (window_type = 'supplier_rolling' AND window_size > 1)",
            name="ck_money_flow_ranking_research_window_size",
        ),
        CheckConstraint(
            "ranking_basis IN ('supplier_reported_order', 'supplier_order_unknown')",
            name="ck_money_flow_ranking_research_basis",
        ),
        CheckConstraint(
            "completeness_basis IN ('sdk_returned', 'upstream_total_verified')",
            name="ck_money_flow_ranking_research_completeness_basis",
        ),
        CheckConstraint(
            "is_complete IS FALSE",
            name="ck_money_flow_ranking_research_incomplete",
        ),
        CheckConstraint(
            "quality_status = 'partial'",
            name="ck_money_flow_ranking_research_quality",
        ),
        CheckConstraint(
            "status = 'research'",
            name="ck_money_flow_ranking_research_status",
        ),
        CheckConstraint(
            "normalized_payload_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_money_flow_ranking_research_normalized_hash",
        ),
        UniqueConstraint(
            "target_trade_date",
            "source_batch_id",
            name="uq_money_flow_ranking_research_source",
        ),
        Index(
            "ix_money_flow_ranking_research_lookup",
            "methodology_version_id",
            "scope_type",
            "window_type",
            "window_size",
            "target_trade_date",
        ),
        {
            "comment": "不完整供应商排行研究观察；禁止成为正式 canonical publication。",
            "postgresql_partition_by": "RANGE (target_trade_date)",
        },
    )

    target_trade_date: Mapped[date] = mapped_column(
        Date, primary_key=True, nullable=False, comment="来源声称对应的目标交易日。"
    )
    research_observation_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, nullable=False, comment="研究观察永久 UUID。"
    )
    methodology_version_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("money_flow_methodology_version.version_id"),
        nullable=False,
        comment="观察采用的方法学版本。",
    )
    scope_type: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="来源排行对象为证券或板块。"
    )
    universe_code: Mapped[str] = mapped_column(
        String(120), nullable=False, comment="来源声明的样本池标识，未提升为 canonical universe。"
    )
    window_type: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="来源的当日或滚动窗口类型。"
    )
    window_size: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="来源窗口长度，单位由方法学版本解释。"
    )
    ranking_bucket_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("money_flow_bucket_definition.bucket_id"),
        nullable=False,
        comment="供应商声明的排序分桶。",
    )
    ranking_basis: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="供应商位置排序依据。"
    )
    source_cutoff_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="来源快照截点；未声明时等于观察时刻。"
    )
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="adapter 观察到该页的时刻。"
    )
    source_batch_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("source_batch.source_batch_id"),
        nullable=False,
        comment="构成研究观察的精确来源批次。",
    )
    source_row_count: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="adapter 返回的唯一供应商位置数。"
    )
    upstream_total: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="来源声明的总行数；当前 AKShare 合并页通常未知。"
    )
    completeness_basis: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="完整性结论依据。"
    )
    is_complete: Mapped[bool] = mapped_column(
        Boolean, nullable=False, comment="固定为假，研究观察不得转为正式排行。"
    )
    normalized_payload_sha256: Mapped[str] = mapped_column(
        CHAR(64), nullable=False, comment="adapter 标准化 JSON 载荷摘要。"
    )
    quality_status: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="固定 partial，表示页完整性未证明。"
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="固定 research，不可被公开读取仓储选择。"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="研究观察入库时间。"
    )
