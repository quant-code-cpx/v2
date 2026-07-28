"""证券名称双时间版本模型。"""

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
    text,
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


class EquityNameVersion(Base):
    """保存证券显示名称在业务时间和知识时间上的可复验历史。"""

    __tablename__ = "equity_name_version"
    __table_args__ = (
        CheckConstraint("btrim(name) <> ''", name="ck_equity_name_version_name"),
        CheckConstraint(
            "effective_date_precision IN ('OFFICIAL_DATE', 'OBSERVATION_DATE')",
            name="ck_equity_name_version_effective_date_precision",
        ),
        CheckConstraint("effective_to IS NULL OR effective_to > effective_from"),
        CheckConstraint("known_to IS NULL OR known_to > known_from"),
        ExcludeConstraint(
            ("security_id", "="),
            ("effective_range", "&&"),
            ("knowledge_range", "&&"),
            using="gist",
        ),
        Index(
            "ix_equity_name_current_prefix",
            text("lower(name) text_pattern_ops"),
            postgresql_where="known_to IS NULL",
        ),
        {"comment": "证券名称双时间历史；不以当前 instrument.name 覆盖已知历史。"},
    )

    version_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, nullable=False, comment="名称版本永久 UUID。"
    )
    security_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("equity_instrument.security_id"),
        nullable=False,
        comment="名称所属永久证券。",
    )
    name: Mapped[str] = mapped_column(
        String(200), nullable=False, comment="该双时间版本内有效的证券显示名称。"
    )
    effective_from: Mapped[date] = mapped_column(
        Date, nullable=False, comment="名称开始适用的业务日期。"
    )
    effective_to: Mapped[date | None] = mapped_column(
        Date, nullable=True, comment="名称停止适用的业务日期；开区间为空。"
    )
    known_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="系统开始知道该名称版本的时间。"
    )
    known_to: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="系统停止采用该知识版本的时间；开区间为空。"
    )
    effective_date_precision: Mapped[str] = mapped_column(
        String(24), nullable=False, comment="名称业务生效日期的证据精度。"
    )
    source_batch_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("source_batch.source_batch_id"),
        nullable=False,
        comment="支撑该名称版本的来源观察。",
    )
    content_sha256: Mapped[bytes] = mapped_column(
        nullable=False, comment="名称版本业务内容的稳定摘要。"
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
