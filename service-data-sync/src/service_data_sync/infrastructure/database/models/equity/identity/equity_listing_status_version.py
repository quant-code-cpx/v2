"""证券上市生命周期双时间版本模型。"""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Computed,
    Date,
    DateTime,
    ForeignKey,
    Index,
    String,
    desc,
)
from sqlalchemy.dialects.postgresql import (
    DATERANGE,
    TSTZRANGE,
    ExcludeConstraint,
    Range,
)
from sqlalchemy.dialects.postgresql import (
    UUID as PG_UUID,
)
from sqlalchemy.orm import Mapped, mapped_column

from ...base import Base


class EquityListingStatusVersion(Base):
    """记录上市、暂停、退市及官方更正的双时间生命周期证据。"""

    __tablename__ = "equity_listing_status_version"
    __table_args__ = (
        CheckConstraint(
            "status IN ('LISTED', 'SUSPENDED', 'DELISTED')",
            name="ck_equity_listing_status_version_status",
        ),
        CheckConstraint(
            "effective_date_precision IN ('OFFICIAL_DATE', 'OBSERVATION_DATE')",
            name="ck_equity_listing_status_version_effective_date_precision",
        ),
        CheckConstraint(
            "evidence_kind IN ('CATALOG', 'EXPLICIT_LISTING', 'EXPLICIT_SUSPENSION', "
            "'EXPLICIT_RESUMPTION', 'EXPLICIT_DELISTING', 'OFFICIAL_CORRECTION')",
            name="ck_equity_listing_status_version_evidence_kind",
        ),
        CheckConstraint("effective_to IS NULL OR effective_to > effective_from"),
        CheckConstraint("known_to IS NULL OR known_to > known_from"),
        CheckConstraint("delisted_on IS NULL OR listed_on IS NULL OR delisted_on >= listed_on"),
        CheckConstraint(
            "status <> 'DELISTED' OR evidence_kind IN ('EXPLICIT_DELISTING', 'OFFICIAL_CORRECTION')"
        ),
        CheckConstraint(
            "evidence_kind <> 'OFFICIAL_CORRECTION' OR correction_approval_reference IS NOT NULL",
            name="ck_equity_listing_status_correction_approval",
        ),
        ExcludeConstraint(
            ("security_id", "="),
            ("effective_range", "&&"),
            ("knowledge_range", "&&"),
            using="gist",
        ),
        Index(
            "ix_equity_listing_status_asof",
            "security_id",
            desc("effective_from"),
            desc("known_from"),
            postgresql_include=["status", "effective_to", "known_to"],
        ),
        {"comment": "证券上市生命周期双时间历史；目录缺席不能隐式产生退市。"},
    )

    version_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, nullable=False, comment="生命周期版本永久 UUID。"
    )
    security_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("equity_instrument.security_id"),
        nullable=False,
        comment="生命周期所属永久证券。",
    )
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, comment="该版本的上市、暂停或退市状态。"
    )
    listed_on: Mapped[date | None] = mapped_column(
        Date, nullable=True, comment="可审计证据支持的上市日期。"
    )
    delisted_on: Mapped[date | None] = mapped_column(
        Date, nullable=True, comment="可审计证据支持的退市日期。"
    )
    effective_from: Mapped[date] = mapped_column(
        Date, nullable=False, comment="状态开始适用的业务日期。"
    )
    effective_to: Mapped[date | None] = mapped_column(
        Date, nullable=True, comment="状态停止适用的业务日期；开区间为空。"
    )
    known_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="系统开始知道该状态版本的时间。"
    )
    known_to: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="系统停止采用该知识版本的时间；开区间为空。"
    )
    effective_date_precision: Mapped[str] = mapped_column(
        String(24), nullable=False, comment="状态业务生效日期的证据精度。"
    )
    evidence_kind: Mapped[str] = mapped_column(
        String(24), nullable=False, comment="生命周期事实的来源证据类型。"
    )
    source_batch_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("source_batch.source_batch_id"),
        nullable=False,
        comment="支撑该状态版本的来源观察。",
    )
    content_sha256: Mapped[bytes] = mapped_column(
        nullable=False, comment="生命周期版本业务内容的稳定摘要。"
    )
    effective_range: Mapped[Range[date] | None] = mapped_column(
        DATERANGE,
        Computed("daterange(effective_from, effective_to, '[)')", persisted=True),
        nullable=True,
        comment="由业务时间端点生成的半开日期范围。",
    )
    knowledge_range: Mapped[Range[datetime] | None] = mapped_column(
        TSTZRANGE,
        Computed("tstzrange(known_from, known_to, '[)')", persisted=True),
        nullable=True,
        comment="由知识时间端点生成的半开时间范围。",
    )
    correction_approval_reference: Mapped[str | None] = mapped_column(
        String(128), nullable=True, comment="官方更正的来源证据引用；字段名为历史兼容名称。"
    )
