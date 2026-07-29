"""跨数据域质量评估、规则结果和隔离索引模型。"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CHAR,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    desc,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base


class QualityEvaluation(Base):
    """记录一套版本化质量策略对一个规范化分区运行的完整判定。"""

    __tablename__ = "quality_evaluation"
    __table_args__ = (
        CheckConstraint("policy_version > 0", name="ck_quality_evaluation_policy_version"),
        CheckConstraint(
            "status IN ('passed', 'warned', 'blocked')", name="ck_quality_evaluation_status"
        ),
        CheckConstraint(
            "score IS NULL OR (score >= 0 AND score <= 1)",
            name="ck_quality_evaluation_score",
        ),
        UniqueConstraint(
            "normalization_run_id",
            "policy_code",
            "policy_version",
            name="uq_quality_evaluation_policy",
        ),
        Index(
            "ix_quality_evaluation_dataset_partition",
            "dataset_id",
            "partition_key",
            desc("evaluated_at"),
        ),
        {"comment": "一个质量策略版本对一个规范化分区的评估总记录。"},
    )

    evaluation_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, nullable=False, comment="质量评估永久 UUID。"
    )
    dataset_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("canonical_dataset.dataset_id", ondelete="RESTRICT"),
        nullable=False,
        comment="被评估的 canonical 数据集。",
    )
    partition_key: Mapped[str] = mapped_column(
        String(240), nullable=False, comment="被评估数据集分区的稳定键。"
    )
    normalization_run_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("normalization_run.normalization_run_id", ondelete="RESTRICT"),
        nullable=False,
        comment="被质量策略评估的规范化运行。",
    )
    policy_code: Mapped[str] = mapped_column(
        String(96), nullable=False, comment="质量策略稳定编码。"
    )
    policy_version: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="质量策略不可变版本号。"
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="通过、警告或阻断状态。"
    )
    score: Mapped[Decimal | None] = mapped_column(
        Numeric(8, 5), nullable=True, comment="可选归一化质量分数，取值闭区间为零到一。"
    )
    evaluated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="完成质量评估的时间。"
    )


class QualityResult(Base):
    """保存质量评估中每条规则的可审计通过结果和有限样本。"""

    __tablename__ = "quality_result"
    __table_args__ = (
        CheckConstraint(
            "severity IN ('info', 'warn', 'blocking')", name="ck_quality_result_severity"
        ),
        CheckConstraint("affected_count >= 0", name="ck_quality_result_affected_count"),
        {"comment": "质量评估内逐规则结果；阻断失败必须阻止对应 release。"},
    )

    evaluation_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("quality_evaluation.evaluation_id", ondelete="RESTRICT"),
        primary_key=True,
        nullable=False,
        comment="所属质量评估。",
    )
    rule_code: Mapped[str] = mapped_column(
        String(96), primary_key=True, nullable=False, comment="质量规则稳定编码。"
    )
    severity: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="提示、警告或阻断级别。"
    )
    passed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, comment="规则在本次评估中是否通过。"
    )
    actual_value: Mapped[Decimal | None] = mapped_column(
        Numeric(38, 12), nullable=True, comment="规则计算出的可选数值结果。"
    )
    threshold_value: Mapped[Decimal | None] = mapped_column(
        Numeric(38, 12), nullable=True, comment="规则配置的可选数值阈值。"
    )
    sample_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True, comment="脱敏且有界的失败或通过样本。"
    )
    affected_count: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="受该规则影响的记录总数。"
    )


class QuarantineRecord(Base):
    """建立统一隔离索引，保留问题记录的私有证据引用但不向业务 API 暴露。"""

    __tablename__ = "quarantine_record"
    __table_args__ = (
        CheckConstraint(
            "record_key_hash IS NULL OR record_key_hash ~ '^[0-9a-f]{64}$'",
            name="ck_quarantine_record_key_hash",
        ),
        CheckConstraint("status IN ('open', 'resolved')", name="ck_quarantine_record_status"),
        CheckConstraint(
            "(status = 'open' AND resolved_at IS NULL) OR "
            "(status = 'resolved' AND resolved_at IS NOT NULL)",
            name="ck_quarantine_record_resolved_at",
        ),
        Index(
            "ix_quarantine_record_open",
            "dataset_id",
            "partition_key",
            desc("created_at"),
            postgresql_where="status = 'open'",
        ),
        {"comment": "跨数据域隔离记录索引；证据和样本均不进入业务读取接口。"},
    )

    quarantine_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, nullable=False, comment="隔离记录永久 UUID。"
    )
    dataset_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("canonical_dataset.dataset_id", ondelete="RESTRICT"),
        nullable=False,
        comment="隔离记录所属 canonical 数据集。",
    )
    partition_key: Mapped[str] = mapped_column(
        String(240), nullable=False, comment="隔离记录所属数据集分区。"
    )
    source_batch_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("source_batch.source_batch_id", ondelete="RESTRICT"),
        nullable=True,
        comment="导致隔离的单一来源批次；无法唯一归因时为空。",
    )
    record_key_hash: Mapped[str | None] = mapped_column(
        CHAR(64), nullable=True, comment="被隔离 canonical 记录键摘要；批次级问题为空。"
    )
    reason_code: Mapped[str] = mapped_column(
        String(96), nullable=False, comment="隔离原因的稳定代码。"
    )
    payload_ref: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="仅内部可访问的原始或规范化证据引用。"
    )
    sample_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True, comment="脱敏且有界的人工定位样本。"
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, comment="待处置或已解决状态。")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="记录进入隔离区的时间。"
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="确认解决时间；开放隔离记录为空。"
    )
