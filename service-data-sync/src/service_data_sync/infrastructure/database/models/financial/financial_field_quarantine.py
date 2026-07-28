"""财务来源字段隔离模型。"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import CHAR, CheckConstraint, DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base


class FinancialFieldQuarantine(Base):
    """记录未治理字段或结构漂移证据，只保存摘要而不保存来源明文样例。"""

    __tablename__ = "financial_field_quarantine"
    __table_args__ = (
        CheckConstraint(
            "status IN ('open', 'resolved', 'ignored')",
            name="ck_financial_field_quarantine_status",
        ),
        CheckConstraint(
            "schema_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_financial_field_quarantine_schema_fingerprint",
        ),
        CheckConstraint(
            "sample_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_financial_field_quarantine_sample_sha256",
        ),
        UniqueConstraint(
            "capability",
            "schema_fingerprint",
            "upstream_field",
            name="uq_financial_field_quarantine_field",
        ),
        Index(
            "ix_financial_field_quarantine_open",
            "status",
            "last_seen_at",
            postgresql_where="status = 'open'",
        ),
        {"comment": "财务未知字段与 schema drift 治理队列；不存来源明文样例。"},
    )

    quarantine_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, nullable=False, comment="隔离记录永久 UUID。"
    )
    source_batch_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("source_batch.source_batch_id", ondelete="RESTRICT"),
        nullable=False,
        comment="首次发现该字段的来源观察。",
    )
    capability: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="发生未知字段的 provider-neutral 财务能力。"
    )
    statement_type: Mapped[str | None] = mapped_column(
        String(32), nullable=True, comment="已知时所属三大报表类别；其他能力为空。"
    )
    upstream_field: Mapped[str] = mapped_column(
        String(200), nullable=False, comment="来源字段名，仅作为治理键而不直接服务消费者。"
    )
    upstream_type: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="来源字段声明或观察到的数据类型。"
    )
    schema_fingerprint: Mapped[str] = mapped_column(
        CHAR(64), nullable=False, comment="来源结构稳定摘要，用于识别 schema drift。"
    )
    sample_sha256: Mapped[str] = mapped_column(
        CHAR(64), nullable=False, comment="脱敏样例稳定摘要，原文只保留在受限 raw evidence。"
    )
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="首次发现该未知字段的时间。"
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="最近一次发现该未知字段的时间。"
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="待治理、已解析或明确忽略状态。"
    )
    resolution: Mapped[str | None] = mapped_column(
        String(500), nullable=True, comment="字段治理结论或关联字典版本说明。"
    )
