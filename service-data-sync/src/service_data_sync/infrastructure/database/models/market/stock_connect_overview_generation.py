"""保存一次互联互通运行内待原子发布的通道组件集合。"""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    SmallInteger,
    String,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base


class StockConnectOverviewGeneration(Base):
    """冻结一个 run 在一个交易日必须共同成功的精确通道集合。"""

    __tablename__ = "stock_connect_overview_generation"
    __table_args__ = (
        CheckConstraint(
            "expected_channel_count BETWEEN 1 AND 4",
            name="ck_stock_connect_overview_generation_expected_count",
        ),
        CheckConstraint(
            "length(btrim(channel_set)) > 0",
            name="ck_stock_connect_overview_generation_channel_set",
        ),
        CheckConstraint(
            "completed_at IS NULL OR completed_at >= created_at",
            name="ck_stock_connect_overview_generation_completed",
        ),
        Index(
            "ix_stock_connect_overview_generation_date",
            "trade_date",
            "completed_at",
        ),
        {
            "comment": (
                "一次 data-operation run 在同一交易日的原子总览候选；未齐备时旧总览保持可见。"
            )
        },
    )

    generation_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        nullable=False,
        comment="由执行器传入的 data-operation run UUID；组件清单保留其可审计引用。",
    )
    trade_date: Mapped[date] = mapped_column(
        Date,
        primary_key=True,
        nullable=False,
        comment="本 generation 对应的精确互联互通交易日。",
    )
    channel_set: Mapped[str] = mapped_column(
        String(160),
        nullable=False,
        comment="按公开通道代码排序并以逗号连接的精确目标集合。",
    )
    expected_channel_count: Mapped[int] = mapped_column(
        SmallInteger,
        nullable=False,
        comment="该交易日必须全部成功的目标通道数量。",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment="首次通道组件进入 staging 的时间。",
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="精确通道集合齐备并原子推进总览的时间；未齐备为空。",
    )


class StockConnectOverviewGenerationComponent(Base):
    """固定 generation 内一个通道实际产生的不可变 bundle release。"""

    __tablename__ = "stock_connect_overview_generation_component"
    __table_args__ = (
        ForeignKeyConstraint(
            ["generation_id", "trade_date"],
            [
                "stock_connect_overview_generation.generation_id",
                "stock_connect_overview_generation.trade_date",
            ],
            name="fk_stock_connect_overview_component_generation",
            ondelete="RESTRICT",
        ),
        Index(
            "ix_stock_connect_overview_component_bundle",
            "bundle_release_id",
        ),
        {
            "comment": (
                "generation 的不可变通道组件清单；重试只能复用同一 bundle，"
                "不能替换已 staging 组件。"
            )
        },
    )

    generation_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        nullable=False,
        comment="所属 data-operation run generation。",
    )
    trade_date: Mapped[date] = mapped_column(
        Date,
        primary_key=True,
        nullable=False,
        comment="所属 generation 的精确交易日。",
    )
    channel_code: Mapped[str] = mapped_column(
        String(32),
        primary_key=True,
        nullable=False,
        comment="本组件对应的公开通道代码。",
    )
    bundle_release_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("stock_connect_bundle_publication.bundle_release_id", ondelete="RESTRICT"),
        nullable=False,
        comment="本次运行对该通道产生或幂等复用的不可变 bundle release。",
    )
    staged_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment="组件在 bundle 事务内进入 generation 的时间。",
    )
