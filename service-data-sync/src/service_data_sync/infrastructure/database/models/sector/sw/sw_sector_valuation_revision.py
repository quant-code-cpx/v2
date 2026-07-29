"""申万行业观测日估值字段的来源观察与知识时间 `revision` 模型。"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    CHAR,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ...base import Base


class SwSectorValuationRevision(Base):
    """保存来源页面对一个申万行业的日期估值观察及知识 `revision`。

    静态/滚动市盈率、市净率和股息率是指定行业、观测日期与方法学下的页面观察，不代表官方终态或
    可与其他行业体系直接比较。来源可能更正历史页面或平台晚些才观察到，故以知识版本保留；缺失、
    负值或不适用需按受控含义保存，不能用股价或其他估值表自行回算。
    """

    __tablename__ = "sw_sector_valuation_revision"
    __table_args__ = (
        CheckConstraint("revision > 0", name="ck_sw_sector_valuation_revision_number"),
        CheckConstraint(
            "known_to IS NULL OR known_to > known_from",
            name="ck_sw_sector_valuation_known_range",
        ),
        CheckConstraint(
            "finality = 'PROVIDER_OBSERVATION'",
            name="ck_sw_sector_valuation_finality",
        ),
        CheckConstraint(
            "content_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_sw_sector_valuation_content_sha256",
        ),
        CheckConstraint(
            "quality_status IN ('passed', 'warned', 'quarantined')",
            name="ck_sw_sector_valuation_quality_status",
        ),
        UniqueConstraint(
            "snapshot_date",
            "sector_code",
            "methodology_id",
            "revision",
            name="uq_sw_sector_valuation_revision",
        ),
        Index(
            "uq_sw_sector_valuation_current",
            "snapshot_date",
            "sector_code",
            "methodology_id",
            unique=True,
            postgresql_where="known_to IS NULL",
        ),
        Index("ix_sw_sector_valuation_date_code", "snapshot_date", "sector_code"),
        {"comment": "申万行业估值供应商日期观察的双时间知识修订。"},
    )

    valuation_revision_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, nullable=False, comment="估值修订永久 UUID。"
    )
    snapshot_date: Mapped[date] = mapped_column(
        Date, nullable=False, comment="估值所属的上海日历观测日期。"
    )
    node_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False, comment="由申万代码确定的稳定节点 UUID。"
    )
    sector_code: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="估值所属申万行业代码。"
    )
    methodology_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("sw_sector_methodology.methodology_id", ondelete="RESTRICT"),
        nullable=False,
        comment="本估值观察的来源展示方法学版本。",
    )
    revision: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="同日同行业同方法学内递增的修订号。"
    )
    static_pe: Mapped[Decimal | None] = mapped_column(
        Numeric(38, 12), nullable=True, comment="来源展示的静态市盈率；缺失保持空值。"
    )
    ttm_pe: Mapped[Decimal | None] = mapped_column(
        Numeric(38, 12), nullable=True, comment="来源展示的 TTM 滚动市盈率。"
    )
    pb: Mapped[Decimal | None] = mapped_column(
        Numeric(38, 12), nullable=True, comment="来源展示的市净率。"
    )
    dividend_yield_ratio: Mapped[Decimal | None] = mapped_column(
        Numeric(38, 12), nullable=True, comment="来源百分数除以一百后的股息率比例。"
    )
    finality: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="固定为供应商观察，不宣称官方最终值。"
    )
    known_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="平台首次可使用本修订的 UTC 时间。"
    )
    known_to: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="本知识修订被替代的半开区间结束时间。"
    )
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="实际取得来源响应的 UTC 时间。"
    )
    source_batch_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("source_batch.source_batch_id", ondelete="RESTRICT"),
        nullable=False,
        comment="支撑本估值修订的 raw evidence 来源批次。",
    )
    content_sha256: Mapped[str] = mapped_column(
        CHAR(64), nullable=False, comment="估值字段与单位语义的稳定摘要。"
    )
    quality_status: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="通过、警告或隔离的质量处置。"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="本 canonical 修订写入时间。"
    )
