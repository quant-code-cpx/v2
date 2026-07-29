"""按方法学、对象、样本池、分桶和窗口组成的资金流强身份序列模型。"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base


class MoneyFlowSeries(Base):
    """绑定方法学、`scope`、`universe`、`bucket` 和窗口形成不可混用的序列。

    一条序列必须恰好指向证券、板块或市场之一，且唯一性索引在未退役状态下阻止相同定义重复创建。
    来源、样本池、订单分桶或窗口任何一项改变都意味着另一序列；这使时间序列查询不会把个股与
    板块、当日值与滚动排行、不同供应商单位或历史/当前身份投影混到一起。
    """

    __tablename__ = "money_flow_series"
    __table_args__ = (
        CheckConstraint(
            "scope_type IN ('equity', 'sector', 'market')",
            name="ck_money_flow_series_scope_type",
        ),
        CheckConstraint(
            "num_nonnulls(security_id, sector_key, market_code) = 1",
            name="ck_money_flow_series_scope_identity",
        ),
        CheckConstraint(
            "(scope_type = 'equity' AND security_id IS NOT NULL) "
            "OR (scope_type = 'sector' AND sector_key IS NOT NULL) "
            "OR (scope_type = 'market' AND market_code IS NOT NULL)",
            name="ck_money_flow_series_scope_match",
        ),
        CheckConstraint(
            "window_type IN ('daily_source', 'supplier_day', 'supplier_rolling')",
            name="ck_money_flow_series_window_type",
        ),
        CheckConstraint("window_size > 0", name="ck_money_flow_series_window_size"),
        Index(
            "uq_money_flow_series_equity",
            "methodology_version_id",
            "security_id",
            "universe_version_id",
            "bucket_id",
            "window_type",
            "window_size",
            unique=True,
            postgresql_where="scope_type = 'equity' AND retired_at IS NULL",
        ),
        Index(
            "uq_money_flow_series_sector",
            "methodology_version_id",
            "sector_key",
            "universe_version_id",
            "bucket_id",
            "window_type",
            "window_size",
            unique=True,
            postgresql_where="scope_type = 'sector' AND retired_at IS NULL",
        ),
        Index(
            "uq_money_flow_series_market",
            "methodology_version_id",
            "market_code",
            "universe_version_id",
            "bucket_id",
            "window_type",
            "window_size",
            unique=True,
            postgresql_where="scope_type = 'market' AND retired_at IS NULL",
        ),
        {"comment": "资金流强身份序列；不同来源、范围、窗口或 bucket 永不混写。"},
    )

    series_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, nullable=False, comment="序列永久 UUID。"
    )
    methodology_version_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("money_flow_methodology_version.version_id"),
        nullable=False,
        comment="冻结来源与算法的版本。",
    )
    scope_type: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="equity、sector 或 market。"
    )
    security_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("equity_instrument.security_id"),
        nullable=True,
        comment="日期感知 resolver 得到的证券永久身份。",
    )
    sector_key: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("sector_entity.sector_key"),
        nullable=True,
        comment="稳定板块内部身份。",
    )
    market_code: Mapped[str | None] = mapped_column(
        String(32), nullable=True, comment="来源定义的市场 scope 代码。"
    )
    universe_version_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("money_flow_universe_version.universe_version_id"),
        nullable=False,
        comment="序列适用的样本池版本。",
    )
    bucket_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("money_flow_bucket_definition.bucket_id"),
        nullable=False,
        comment="序列使用的方法学分桶。",
    )
    window_type: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="逐日来源或供应商快照窗口。"
    )
    window_size: Mapped[int] = mapped_column(Integer, nullable=False, comment="来源窗口大小。")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="序列身份创建时间。"
    )
    retired_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="序列停止新增修订的时间。"
    )
