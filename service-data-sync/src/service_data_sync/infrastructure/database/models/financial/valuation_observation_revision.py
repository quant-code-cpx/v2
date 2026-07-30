"""按估值观察日期年度分区的供应商估值双时态 `revision` 模型。"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
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
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import DATERANGE, TSTZRANGE
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base


class ValuationObservationRevision(Base):
    """保存供应商估值日期观察，不将其伪装成交易所或供应商官方最终值。

    PE、PB、股息率等值是特定来源、特定日期、特定方法学下的观察，可能因报价、股本、财报或计算
    规则更新而修订。`observation_date` 不是平台抓取时间，双时态范围则保留何时适用和何时可知；
    空值、负值或不适用应按受控口径保存，不能用另一来源或当天收盘价自行补算。
    """

    __tablename__ = "valuation_observation_revision"
    __table_args__ = (
        CheckConstraint("revision > 0", name="ck_valuation_observation_revision_number"),
        CheckConstraint(
            "finality = 'PROVIDER_OBSERVATION'",
            name="ck_valuation_observation_finality",
        ),
        CheckConstraint(
            "(currency IS NOT NULL AND currency_null_reason IS NULL) "
            "OR (currency IS NULL AND currency_null_reason IN "
            "('NOT_APPLICABLE', 'UNKNOWN_SOURCE', 'MIXED_CURRENCIES'))",
            name="ck_valuation_observation_currency",
        ),
        CheckConstraint(
            "knowledge_basis IN ('OFFICIAL_ANNOUNCEMENT', 'PROVIDER_UPDATE', 'OBSERVED_AT')",
            name="ck_valuation_observation_knowledge_basis",
        ),
        CheckConstraint(
            "knowledge_confidence IN ('HIGH', 'MEDIUM', 'CONSERVATIVE')",
            name="ck_valuation_observation_knowledge_confidence",
        ),
        CheckConstraint(
            "quality_status IN ('passed', 'warned', 'quarantined')",
            name="ck_valuation_observation_quality_status",
        ),
        CheckConstraint(
            "content_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_valuation_observation_content_sha256",
        ),
        CheckConstraint(
            "effective_from = observation_date "
            "AND effective_to IS NOT NULL "
            "AND effective_to = observation_date + 1",
            name="ck_valuation_observation_effective_range",
        ),
        CheckConstraint(
            "known_to IS NULL OR known_to > known_from",
            name="ck_valuation_observation_knowledge_range",
        ),
        CheckConstraint(
            "known_from >= observed_at",
            name="ck_valuation_observation_known_after_observed",
        ),
        UniqueConstraint(
            "observation_date",
            "security_id",
            "metric_id",
            "methodology_id",
            "revision",
            name="uq_valuation_observation_revision",
        ),
        Index(
            "uq_valuation_observation_current",
            "observation_date",
            "security_id",
            "metric_id",
            "methodology_id",
            unique=True,
            postgresql_where="known_to IS NULL",
        ),
        Index(
            "ix_valuation_observation_series",
            "security_id",
            "metric_id",
            "observation_date",
            postgresql_include=["value", "methodology_id"],
        ),
        {
            "postgresql_partition_by": "RANGE (observation_date)",
            "comment": "估值日期观察 revision 父表；按 observation_date 年度物理分区。",
        },
    )

    observation_date: Mapped[date] = mapped_column(
        Date, primary_key=True, nullable=False, comment="分区键及估值所属日期。"
    )
    valuation_revision_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, nullable=False, comment="估值观察 revision UUID。"
    )
    security_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("equity_instrument.security_id", ondelete="RESTRICT"),
        nullable=False,
        comment="估值所属永久证券内部键。",
    )
    metric_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("financial_metric_definition.metric_id", ondelete="RESTRICT"),
        nullable=False,
        comment="估值指标字典键。",
    )
    methodology_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("financial_methodology.methodology_id", ondelete="RESTRICT"),
        nullable=False,
        comment="估值来源和计算方法学版本。",
    )
    revision: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="同一估值逻辑键的递增修订序号。"
    )
    value: Mapped[Decimal] = mapped_column(
        Numeric(38, 12), nullable=False, comment="精确估值观察值。"
    )
    unit: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="估值指标规范化后的单位。"
    )
    currency: Mapped[str | None] = mapped_column(
        CHAR(3), nullable=True, comment="已知时的 ISO 4217 币种。"
    )
    currency_null_reason: Mapped[str | None] = mapped_column(
        String(24), nullable=True, comment="币种为空时的受控原因。"
    )
    finality: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="固定为 PROVIDER_OBSERVATION，不宣称官方最终态。"
    )
    effective_from: Mapped[date] = mapped_column(
        Date, nullable=False, comment="固定等于估值观察日。"
    )
    effective_to: Mapped[date | None] = mapped_column(
        Date, nullable=True, comment="固定为估值观察日次日，使业务半开区间仅覆盖当日。"
    )
    known_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="平台首次可以使用本 revision 的时间。"
    )
    known_to: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="平台知识半开区间的结束时间。"
    )
    knowledge_basis: Mapped[str] = mapped_column(
        String(24), nullable=False, comment="知识时间使用的公告、供应商更新或实际观察依据。"
    )
    knowledge_confidence: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="知识时间依据置信等级。"
    )
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="实际取得来源响应的时间。"
    )
    source_batch_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("source_batch.source_batch_id", ondelete="RESTRICT"),
        nullable=False,
        comment="支撑本估值 revision 的来源观察。",
    )
    content_sha256: Mapped[str] = mapped_column(
        CHAR(64), nullable=False, comment="规范化估值内容稳定摘要。"
    )
    quality_status: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="通过、警告或隔离质量状态。"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="canonical revision 写入时间。"
    )
    effective_range: Mapped[object] = mapped_column(
        DATERANGE,
        Computed("daterange(effective_from, effective_to, '[)')", persisted=True),
        nullable=True,
        comment="由有效起止日期生成的半开业务时间范围。",
    )
    knowledge_range: Mapped[object] = mapped_column(
        TSTZRANGE,
        Computed("tstzrange(known_from, known_to, '[)')", persisted=True),
        nullable=True,
        comment="由知识起止时间生成的半开时间范围。",
    )
