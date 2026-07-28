"""供应商资金流排行快照修订模型。"""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from sqlalchemy import (
    CHAR,
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

from ..base import Base


class MoneyFlowRankingSnapshot(Base):
    """保存已验证完整的 supplier ranking 不可变 revision header。"""

    __tablename__ = "money_flow_ranking_snapshot"
    __table_args__ = (
        CheckConstraint(
            "scope_type IN ('equity', 'sector')",
            name="ck_money_flow_ranking_snapshot_scope",
        ),
        CheckConstraint(
            "window_type IN ('supplier_day', 'supplier_rolling')",
            name="ck_money_flow_ranking_snapshot_window",
        ),
        CheckConstraint(
            "(window_type = 'supplier_day' AND window_size = 1) "
            "OR (window_type = 'supplier_rolling' AND window_size > 1)",
            name="ck_money_flow_ranking_snapshot_window_size",
        ),
        CheckConstraint(
            "ranking_basis IN ('supplier_reported_order', 'supplier_order_unknown')",
            name="ck_money_flow_ranking_snapshot_basis",
        ),
        CheckConstraint(
            "quality_status IN ('passed', 'warned')",
            name="ck_money_flow_ranking_snapshot_quality",
        ),
        CheckConstraint(
            "status IN ('published', 'superseded')",
            name="ck_money_flow_ranking_snapshot_status",
        ),
        CheckConstraint(
            "business_hash ~ '^[0-9a-f]{64}$'",
            name="ck_money_flow_ranking_snapshot_hash",
        ),
        Index(
            "uq_money_flow_ranking_snapshot_current",
            "target_trade_date",
            "methodology_version_id",
            "scope_type",
            "universe_version_id",
            "window_type",
            "window_size",
            "ranking_bucket_id",
            unique=True,
            postgresql_where="superseded_at IS NULL",
        ),
        {
            "comment": "已验证完整的供应商排行修订；绝不从逐日序列重建。",
            "postgresql_partition_by": "RANGE (target_trade_date)",
        },
    )

    target_trade_date: Mapped[date] = mapped_column(
        Date, primary_key=True, nullable=False, comment="快照声称对应的目标交易日。"
    )
    snapshot_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, nullable=False, comment="排行快照修订永久 UUID。"
    )
    methodology_version_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("money_flow_methodology_version.version_id"),
        nullable=False,
        comment="排行所用方法学版本。",
    )
    scope_type: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="排行对象为 equity 或 sector。"
    )
    universe_version_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("money_flow_universe_version.universe_version_id"),
        nullable=False,
        comment="排行样本池版本。",
    )
    window_type: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="供应商日或滚动窗口。"
    )
    window_size: Mapped[int] = mapped_column(Integer, nullable=False, comment="供应商窗口大小。")
    ranking_bucket_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("money_flow_bucket_definition.bucket_id"),
        nullable=False,
        comment="决定供应商排序的 bucket。",
    )
    ranking_basis: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="供应商位置是否为明确报告顺序。"
    )
    source_cutoff_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="来源快照的数据截点。"
    )
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="adapter 观察快照的时刻。"
    )
    revision: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="同一逻辑排行身份内递增的修订号。"
    )
    row_count: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="快照包含的唯一 supplier position 数量。"
    )
    business_hash: Mapped[str] = mapped_column(
        CHAR(64), nullable=False, comment="快照身份、位置和全部度量的 canonical 哈希。"
    )
    quality_status: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="发布前通过的质量级别。"
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="当前发布或已被替换状态。"
    )
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="快照开始对消费者可见的时间。"
    )
    superseded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="被同身份新 revision 替换的时间。"
    )
