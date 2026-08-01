"""不完整供应商资金流排行的来源分桶度量模型。"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID

from sqlalchemy import CheckConstraint, Date, ForeignKey, ForeignKeyConstraint, Integer, Numeric
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base


class MoneyFlowRankingResearchMetric(Base):
    """保存研究排行中每个供应商位置与来源分桶的原始报告度量。"""

    __tablename__ = "money_flow_ranking_research_metric"
    __table_args__ = (
        ForeignKeyConstraint(
            ["target_trade_date", "research_observation_id", "supplier_position"],
            [
                "money_flow_ranking_research_item.target_trade_date",
                "money_flow_ranking_research_item.research_observation_id",
                "money_flow_ranking_research_item.supplier_position",
            ],
            name="fk_money_flow_ranking_research_metric_item",
        ),
        CheckConstraint(
            "gross_inflow IS NULL OR gross_inflow >= 0",
            name="ck_money_flow_ranking_research_metric_inflow",
        ),
        CheckConstraint(
            "gross_outflow IS NULL OR gross_outflow >= 0",
            name="ck_money_flow_ranking_research_metric_outflow",
        ),
        CheckConstraint(
            "num_nonnulls(gross_inflow, gross_outflow, net_amount, net_ratio) > 0",
            name="ck_money_flow_ranking_research_metric_measure",
        ),
        {"comment": "不完整供应商排行的来源报告度量，不生成正式行情事实。"},
    )

    target_trade_date: Mapped[date] = mapped_column(
        Date, primary_key=True, nullable=False, comment="所属排行目标交易日。"
    )
    research_observation_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, nullable=False, comment="所属研究观察 UUID。"
    )
    supplier_position: Mapped[int] = mapped_column(
        Integer, primary_key=True, nullable=False, comment="所属供应商位置。"
    )
    bucket_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("money_flow_bucket_definition.bucket_id"),
        primary_key=True,
        nullable=False,
        comment="来源度量所属方法学分桶。",
    )
    gross_inflow: Mapped[Decimal | None] = mapped_column(
        Numeric(30, 6), nullable=True, comment="来源报告流入额。"
    )
    gross_outflow: Mapped[Decimal | None] = mapped_column(
        Numeric(30, 6), nullable=True, comment="来源报告流出额。"
    )
    net_amount: Mapped[Decimal | None] = mapped_column(
        Numeric(30, 6), nullable=True, comment="来源报告净额。"
    )
    net_ratio: Mapped[Decimal | None] = mapped_column(
        Numeric(20, 8), nullable=True, comment="adapter 标准化后的来源净占比。"
    )
