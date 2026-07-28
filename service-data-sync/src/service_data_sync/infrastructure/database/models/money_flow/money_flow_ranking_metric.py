"""供应商资金流排行固定度量模型。"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID

from sqlalchemy import CheckConstraint, Date, ForeignKey, ForeignKeyConstraint, Integer, Numeric
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base


class MoneyFlowRankingMetric(Base):
    """保存一个排行位置和 bucket 的固定四度量，不使用 EAV。"""

    __tablename__ = "money_flow_ranking_metric"
    __table_args__ = (
        ForeignKeyConstraint(
            ["target_trade_date", "snapshot_id", "supplier_position"],
            [
                "money_flow_ranking_item.target_trade_date",
                "money_flow_ranking_item.snapshot_id",
                "money_flow_ranking_item.supplier_position",
            ],
            name="fk_money_flow_ranking_metric_item",
        ),
        CheckConstraint(
            "gross_inflow IS NULL OR gross_inflow >= 0",
            name="ck_money_flow_ranking_metric_inflow",
        ),
        CheckConstraint(
            "gross_outflow IS NULL OR gross_outflow >= 0",
            name="ck_money_flow_ranking_metric_outflow",
        ),
        CheckConstraint(
            "num_nonnulls(gross_inflow, gross_outflow, net_amount, net_ratio) > 0",
            name="ck_money_flow_ranking_metric_measure",
        ),
        {
            "comment": "供应商排行的固定四度量；不允许任意 key/value 事实。",
            "postgresql_partition_by": "RANGE (target_trade_date)",
        },
    )

    target_trade_date: Mapped[date] = mapped_column(
        Date, primary_key=True, nullable=False, comment="所属排行目标交易日。"
    )
    snapshot_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, nullable=False, comment="所属排行快照。"
    )
    supplier_position: Mapped[int] = mapped_column(
        Integer, primary_key=True, nullable=False, comment="所属供应商位置。"
    )
    bucket_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("money_flow_bucket_definition.bucket_id"),
        primary_key=True,
        nullable=False,
        comment="该度量所属方法学 bucket。",
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
