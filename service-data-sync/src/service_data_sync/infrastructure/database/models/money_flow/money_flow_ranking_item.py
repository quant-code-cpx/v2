"""供应商资金流排行位置与 canonical scope 模型。"""

from __future__ import annotations

from datetime import date
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base


class MoneyFlowRankingItem(Base):
    """把 supplier position 绑定到目标日唯一解析的证券或板块身份。"""

    __tablename__ = "money_flow_ranking_item"
    __table_args__ = (
        ForeignKeyConstraint(
            ["target_trade_date", "snapshot_id"],
            [
                "money_flow_ranking_snapshot.target_trade_date",
                "money_flow_ranking_snapshot.snapshot_id",
            ],
            name="fk_money_flow_ranking_item_snapshot",
        ),
        CheckConstraint(
            "scope_type IN ('equity', 'sector')",
            name="ck_money_flow_ranking_item_scope_type",
        ),
        CheckConstraint(
            "num_nonnulls(security_id, sector_key) = 1",
            name="ck_money_flow_ranking_item_scope_identity",
        ),
        CheckConstraint(
            "(scope_type = 'equity' AND security_id IS NOT NULL) "
            "OR (scope_type = 'sector' AND sector_key IS NOT NULL)",
            name="ck_money_flow_ranking_item_scope_match",
        ),
        CheckConstraint(
            "supplier_position > 0",
            name="ck_money_flow_ranking_item_position",
        ),
        Index(
            "uq_money_flow_ranking_item_equity",
            "target_trade_date",
            "snapshot_id",
            "security_id",
            unique=True,
            postgresql_where="scope_type = 'equity'",
        ),
        Index(
            "uq_money_flow_ranking_item_sector",
            "target_trade_date",
            "snapshot_id",
            "sector_key",
            unique=True,
            postgresql_where="scope_type = 'sector'",
        ),
        {
            "comment": "供应商排行位置与 canonical scope；位置不由平台重新计算。",
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
        Integer, primary_key=True, nullable=False, comment="供应商返回的稳定页序位置。"
    )
    scope_type: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="位置对象为 equity 或 sector。"
    )
    security_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("equity_instrument.security_id"),
        nullable=True,
        comment="目标日唯一解析的证券永久身份。",
    )
    sector_key: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("sector_entity.sector_key"),
        nullable=True,
        comment="目标日稳定板块内部身份。",
    )
    scope_name_at_snapshot: Mapped[str | None] = mapped_column(
        String(200), nullable=True, comment="来源快照展示名称，仅作展示证据而非身份。"
    )
