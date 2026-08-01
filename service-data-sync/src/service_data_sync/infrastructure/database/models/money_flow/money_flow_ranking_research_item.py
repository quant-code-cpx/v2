"""不完整供应商资金流排行的来源身份行模型。"""

from __future__ import annotations

from datetime import date
from uuid import UUID

from sqlalchemy import CheckConstraint, Date, ForeignKeyConstraint, Integer, String
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base


class MoneyFlowRankingResearchItem(Base):
    """保存供应商排行位置及未强行解析的来源 scope。

    板块排行常只有来源名称而没有稳定代码；这里保留标准化后的来源身份文本，而不是把
    名称猜成当前 `sector_entity`。因此研究数据可审计，但无法被正式消费者路径误用。
    """

    __tablename__ = "money_flow_ranking_research_item"
    __table_args__ = (
        ForeignKeyConstraint(
            ["target_trade_date", "research_observation_id"],
            [
                "money_flow_ranking_research_observation.target_trade_date",
                "money_flow_ranking_research_observation.research_observation_id",
            ],
            name="fk_money_flow_ranking_research_item_observation",
        ),
        CheckConstraint(
            "scope_type IN ('equity', 'sector')",
            name="ck_money_flow_ranking_research_item_scope",
        ),
        CheckConstraint(
            "supplier_position > 0",
            name="ck_money_flow_ranking_research_item_position",
        ),
        CheckConstraint(
            "(scope_type = 'equity' "
            "AND source_exchange IN ('SSE', 'SZSE', 'BSE') "
            "AND source_symbol ~ '^[0-9]{6}$' "
            "AND source_sector_scheme IS NULL AND source_sector_code IS NULL) "
            "OR (scope_type = 'sector' "
            "AND source_exchange IS NULL AND source_symbol IS NULL "
            "AND source_sector_scheme IS NOT NULL AND source_sector_code IS NOT NULL)",
            name="ck_money_flow_ranking_research_item_identity",
        ),
        {"comment": "不完整供应商排行位置与来源 scope，不绑定猜测的 canonical 身份。"},
    )

    target_trade_date: Mapped[date] = mapped_column(
        Date, primary_key=True, nullable=False, comment="所属排行目标交易日。"
    )
    research_observation_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, nullable=False, comment="所属研究观察 UUID。"
    )
    supplier_position: Mapped[int] = mapped_column(
        Integer, primary_key=True, nullable=False, comment="来源返回位置，平台绝不重排。"
    )
    scope_type: Mapped[str] = mapped_column(String(16), nullable=False, comment="来源 scope 类型。")
    source_exchange: Mapped[str | None] = mapped_column(
        String(8), nullable=True, comment="证券来源交易所；板块行为空。"
    )
    source_symbol: Mapped[str | None] = mapped_column(
        String(16), nullable=True, comment="证券来源代码；板块行为空。"
    )
    source_sector_scheme: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="板块来源分类体系；证券行为空。"
    )
    source_sector_code: Mapped[str | None] = mapped_column(
        String(200), nullable=True, comment="板块来源代码或明确未解析名称标识。"
    )
    source_name: Mapped[str | None] = mapped_column(
        String(200), nullable=True, comment="来源展示名称，仅作审计证据。"
    )
