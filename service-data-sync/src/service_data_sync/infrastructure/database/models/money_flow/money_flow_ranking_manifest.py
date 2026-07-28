"""供应商资金流排行来源完整性证据模型。"""

from __future__ import annotations

from datetime import date
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base


class MoneyFlowRankingManifest(Base):
    """记录 SDK 返回行数、上游 total 与可证明完整性。"""

    __tablename__ = "money_flow_ranking_manifest"
    __table_args__ = (
        ForeignKeyConstraint(
            ["target_trade_date", "snapshot_id"],
            [
                "money_flow_ranking_snapshot.target_trade_date",
                "money_flow_ranking_snapshot.snapshot_id",
            ],
            name="fk_money_flow_ranking_manifest_snapshot",
        ),
        UniqueConstraint(
            "target_trade_date",
            "source_batch_id",
            name="uq_money_flow_ranking_manifest_source",
        ),
        CheckConstraint(
            "completeness_basis IN ('sdk_returned', 'upstream_total_verified')",
            name="ck_money_flow_ranking_manifest_basis",
        ),
        CheckConstraint(
            "NOT is_complete OR "
            "(completeness_basis = 'upstream_total_verified' "
            "AND upstream_total IS NOT NULL AND source_row_count = upstream_total)",
            name="ck_money_flow_ranking_manifest_complete",
        ),
        {
            "comment": "供应商排行来源清单；SDK 合并 DataFrame 不能单独证明完整。",
            "postgresql_partition_by": "RANGE (target_trade_date)",
        },
    )

    target_trade_date: Mapped[date] = mapped_column(
        Date, primary_key=True, nullable=False, comment="所属排行目标交易日。"
    )
    snapshot_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, nullable=False, comment="所属排行快照。"
    )
    source_batch_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("source_batch.source_batch_id"),
        primary_key=True,
        nullable=False,
        comment="构成快照的不可变来源观察。",
    )
    source_row_count: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="adapter 返回的唯一 scope 行数。"
    )
    upstream_total: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="page-aware adapter 验证的上游总行数。"
    )
    completeness_basis: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="完整性证据类型。"
    )
    is_complete: Mapped[bool] = mapped_column(
        Boolean, nullable=False, comment="是否已证明所有来源分页完整。"
    )
