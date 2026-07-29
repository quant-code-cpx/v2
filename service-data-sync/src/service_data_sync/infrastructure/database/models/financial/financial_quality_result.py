"""财务数据按规则记录完整性、币种、身份和双时态校验结果的模型。"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    SmallInteger,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base


class FinancialQualityResult(Base):
    """保存可审计财务质量门结果，失败或隔离记录不能推进消费者发布。

    质量结果绑定具体输入、方法学与规则版本，而不是给证券贴一个笼统“健康”标签。它保留实际值、
    阈值和有限样本，支持复核报告期、币种、单位、范围及双时态边界；阻断失败必须停在候选或隔离
    区，不能因同批其他指标通过而部分发布。
    """

    __tablename__ = "financial_quality_result"
    __table_args__ = (
        CheckConstraint("rule_version > 0", name="ck_financial_quality_result_rule_version"),
        CheckConstraint(
            "severity IN ('info', 'warning', 'blocking')",
            name="ck_financial_quality_result_severity",
        ),
        CheckConstraint(
            "status IN ('passed', 'warned', 'failed', 'quarantined')",
            name="ck_financial_quality_result_status",
        ),
        UniqueConstraint(
            "source_batch_id",
            "rule_code",
            "rule_version",
            name="uq_financial_quality_result_batch_rule",
        ),
        Index(
            "ix_financial_quality_result_failed",
            "created_at",
            postgresql_where="status IN ('failed', 'quarantined')",
        ),
        {"comment": "财务质量门证据；只保留结构化测量值，不保存敏感 raw 内容。"},
    )

    quality_result_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, nullable=False, comment="质量结果永久 UUID。"
    )
    source_batch_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("source_batch.source_batch_id", ondelete="RESTRICT"),
        nullable=False,
        comment="触发本质量规则的来源观察。",
    )
    data_version: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True, comment="质量通过后关联的消费者数据版本；隔离时为空。"
    )
    rule_code: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="稳定的质量规则代码。"
    )
    rule_version: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, comment="质量规则实现与阈值版本。"
    )
    severity: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="信息、警告或阻断级别。"
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="通过、警告、失败或隔离结果。"
    )
    measured: Mapped[Decimal | None] = mapped_column(
        Numeric(38, 12), nullable=True, comment="可量化时记录的实际测量值。"
    )
    threshold: Mapped[Decimal | None] = mapped_column(
        Numeric(38, 12), nullable=True, comment="可量化时记录的规则阈值。"
    )
    dimension: Mapped[str | None] = mapped_column(
        String(200), nullable=True, comment="质量规则适用的非敏感维度说明。"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="质量结果创建时间。"
    )
