"""证券交易所代码的业务有效时间与平台知识时间双时态版本模型。"""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from sqlalchemy import (
    CHAR,
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


class EquityIdentifierVersion(Base):
    """表达证券代码在两条时间轴上的唯一身份，防止代码复用被静默混淆。

    `effective_range` 回答某代码在市场事实层何时属于该证券，`knowledge_range` 回答平台何时按此
    理解它；两者都可能因官方更正而改变。排斥约束禁止同一交易所代码在两条范围重叠时绑定两个
    永久 `security_id`，从而让历史行情、财务和成分记录能够按当时身份重新解析。
    """

    __tablename__ = "equity_identifier_version"
    __table_args__ = (
        CheckConstraint(
            "exchange IN ('SSE', 'SZSE', 'BSE')", name="ck_equity_identifier_version_exchange"
        ),
        CheckConstraint("symbol ~ '^[0-9]{6}$'", name="ck_equity_identifier_version_symbol"),
        CheckConstraint(
            "identity_state IN ('PENDING', 'CONFIRMED')",
            name="ck_equity_identifier_version_identity_state",
        ),
        CheckConstraint(
            "effective_date_precision IN ('OFFICIAL_DATE', 'OBSERVATION_DATE')",
            name="ck_equity_identifier_version_effective_date_precision",
        ),
        CheckConstraint("effective_to IS NULL OR effective_to > effective_from"),
        CheckConstraint("known_to IS NULL OR known_to > known_from"),
        ExcludeConstraint(
            ("security_id", "="),
            ("effective_range", "&&"),
            ("knowledge_range", "&&"),
            using="gist",
        ),
        ExcludeConstraint(
            ("exchange", "="),
            ("symbol", "="),
            ("effective_range", "&&"),
            ("knowledge_range", "&&"),
            using="gist",
        ),
        Index(
            "ix_equity_identifier_asof",
            "exchange",
            "symbol",
            desc("effective_from"),
            desc("known_from"),
            postgresql_include=["security_id", "effective_to", "known_to", "identity_state"],
        ),
        Index(
            "uq_equity_identifier_current_open",
            "exchange",
            "symbol",
            unique=True,
            postgresql_where="effective_to IS NULL AND known_to IS NULL",
        ),
        {"comment": "证券代码的双时间身份版本；同一业务/知识区间不得重叠。"},
    )

    version_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, nullable=False, comment="身份版本永久 UUID。"
    )
    security_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("equity_instrument.security_id"),
        nullable=False,
        comment="被该代码版本标识的永久证券。",
    )
    exchange: Mapped[str] = mapped_column(
        String(4), nullable=False, comment="代码有效期所属交易所。"
    )
    symbol: Mapped[str] = mapped_column(CHAR(6), nullable=False, comment="有效期内六位证券代码。")
    identity_state: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="身份待确认或已确认状态。"
    )
    effective_from: Mapped[date] = mapped_column(
        Date, nullable=False, comment="该代码开始适用的业务日期。"
    )
    effective_to: Mapped[date | None] = mapped_column(
        Date, nullable=True, comment="该代码停止适用的业务日期；开区间为空。"
    )
    known_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="系统开始知道该版本的时间。"
    )
    known_to: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="系统停止采用该知识版本的时间；开区间为空。"
    )
    effective_date_precision: Mapped[str] = mapped_column(
        String(24), nullable=False, comment="业务生效日期来自官方证据或观察日期。"
    )
    source_batch_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("source_batch.source_batch_id"),
        nullable=False,
        comment="支撑本版本的来源观察。",
    )
    content_sha256: Mapped[bytes] = mapped_column(
        nullable=False, comment="该版本业务内容的稳定摘要。"
    )
    effective_range: Mapped[Range[date] | None] = mapped_column(
        DATERANGE,
        Computed("daterange(effective_from, effective_to, '[)')", persisted=True),
        nullable=True,
        comment="由业务有效端点生成的半开日期范围。",
    )
    knowledge_range: Mapped[Range[datetime] | None] = mapped_column(
        TSTZRANGE,
        Computed("tstzrange(known_from, known_to, '[)')", persisted=True),
        nullable=True,
        comment="由知识时间端点生成的半开时间范围。",
    )
