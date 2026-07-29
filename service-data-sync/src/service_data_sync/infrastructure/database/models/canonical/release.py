"""canonical release、血缘与跨运行检查点模型。"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CHAR,
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base


class CanonicalCheckpoint(Base):
    """仅在 publication 成功提交后推进跨运行水位，并用 fencing token 拒绝旧 worker 覆盖。

    这是跨运行的已发布进度，不是单次任务的分页游标或租约；位置变化必须与对应 release/
    publication 在一个事务内完成。比较交换使用单调 `fencing_token`，因此租约过期后恢复的旧
    worker 即使晚到，也不能把更新后的水位回退。失败、隔离或未发布候选绝不推进这里。
    """

    __tablename__ = "canonical_checkpoint"
    __table_args__ = (
        CheckConstraint("fencing_token >= 0", name="ck_canonical_checkpoint_fencing_token"),
        {"comment": "跨运行成功发布水位；不替代 run 内分页或租约 checkpoint。"},
    )

    dataset_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("canonical_dataset.dataset_id", ondelete="RESTRICT"),
        primary_key=True,
        nullable=False,
        comment="检查点所属 canonical 数据集。",
    )
    partition_key: Mapped[str] = mapped_column(
        String(240), primary_key=True, nullable=False, comment="检查点所属稳定数据集分区。"
    )
    checkpoint_kind: Mapped[str] = mapped_column(
        String(48),
        primary_key=True,
        nullable=False,
        comment="发布日期、水位或其他已定义 checkpoint 类型。",
    )
    position_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, comment="由 checkpoint 类型解释的可比较位置对象。"
    )
    last_release_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("dataset_release.release_id", ondelete="RESTRICT"),
        nullable=True,
        comment="最近成功发布并推进该水位的 immutable release。",
    )
    fencing_token: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="CAS 更新使用的单调 fencing token。"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="检查点最后成功比较交换时间。"
    )


class DatasetRelease(Base):
    """冻结一个数据集分区的不可变候选内容集合，作为 publication 可切换的实际目标。

    内容摘要、方法学、规范化运行、数量和业务日期范围共同定义一个可复验快照；同内容重跑由
    唯一约束复用，不会制造无意义的新 release。release 自身不等于“当前可见”：publication
    才选择消费者版本。受控回滚只需把 publication 指向已验收的旧 release，不删除新证据或
    修改任一历史 release。
    """

    __tablename__ = "dataset_release"
    __table_args__ = (
        CheckConstraint("content_hash ~ '^[0-9a-f]{64}$'", name="ck_dataset_release_content_hash"),
        CheckConstraint(
            "quality_status IN ('passed', 'warned', 'partial')",
            name="ck_dataset_release_quality_status",
        ),
        CheckConstraint("record_count >= 0", name="ck_dataset_release_record_count"),
        CheckConstraint(
            "fact_max IS NULL OR (fact_min IS NOT NULL AND fact_max >= fact_min)",
            name="ck_dataset_release_fact_range",
        ),
        UniqueConstraint(
            "dataset_id",
            "partition_key",
            "methodology_version_id",
            "content_hash",
            name="uq_dataset_release_content",
        ),
        Index(
            "ix_dataset_release_dataset_partition_created",
            "dataset_id",
            "partition_key",
            "created_at",
        ),
        {"comment": "不可变 canonical 内容集合；publication 只切换到完整且已质量判定的 release。"},
    )

    release_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        nullable=False,
        comment="不可变 release 永久 UUID。",
    )
    dataset_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("canonical_dataset.dataset_id", ondelete="RESTRICT"),
        nullable=False,
        comment="release 所属 canonical 数据集。",
    )
    partition_key: Mapped[str] = mapped_column(
        String(240), nullable=False, comment="release 冻结的稳定数据集分区。"
    )
    methodology_version_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("methodology_version.methodology_version_id", ondelete="RESTRICT"),
        nullable=False,
        comment="解释本 release 事实口径的冻结方法学版本。",
    )
    normalization_run_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("normalization_run.normalization_run_id", ondelete="RESTRICT"),
        nullable=False,
        comment="产生本 release 候选内容的规范化运行。",
    )
    content_hash: Mapped[str] = mapped_column(
        CHAR(64), nullable=False, comment="完整 canonical 内容集合的 SHA-256 摘要。"
    )
    quality_status: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="release 可见前获得的质量结论。"
    )
    record_count: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="release 内 canonical 记录数量。"
    )
    fact_min: Mapped[date | None] = mapped_column(
        Date, nullable=True, comment="release 中最早业务事实日期；无日期粒度时为空。"
    )
    fact_max: Mapped[date | None] = mapped_column(
        Date, nullable=True, comment="release 中最晚业务事实日期；无日期粒度时为空。"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="release 内容冻结时间。"
    )


class CanonicalRecordLineage(Base):
    """记录 release 内每条 canonical 事实到来源观察和证据对象的多对多血缘。

    同一事实可同时有主要、佐证或派生输入来源，因而不能把来源字段直接覆盖在事实行上。
    `transform_hash` 指向从证据到事实所用规则，`raw_payload_id` 在可精确定位时才填写；成功
    路径没有保留对象正文时，仍可通过批次和摘要证明观察身份。
    """

    __tablename__ = "canonical_record_lineage"
    __table_args__ = (
        CheckConstraint(
            "record_key_hash ~ '^[0-9a-f]{64}$'",
            name="ck_canonical_record_lineage_key_hash",
        ),
        CheckConstraint(
            "role IN ('primary', 'corroborating', 'input')",
            name="ck_canonical_record_lineage_role",
        ),
        CheckConstraint(
            "transform_hash ~ '^[0-9a-f]{64}$'",
            name="ck_canonical_record_lineage_transform_hash",
        ),
        Index("ix_canonical_record_lineage_source_batch", "source_batch_id"),
        {"comment": "release 内 canonical 记录到来源批次和 raw 证据的多对多可追溯血缘。"},
    )

    release_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("dataset_release.release_id", ondelete="RESTRICT"),
        primary_key=True,
        nullable=False,
        comment="包含该 canonical 记录的 immutable release。",
    )
    record_key_hash: Mapped[str] = mapped_column(
        CHAR(64), primary_key=True, nullable=False, comment="release 内 canonical 业务记录键摘要。"
    )
    source_batch_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("source_batch.source_batch_id", ondelete="RESTRICT"),
        primary_key=True,
        nullable=False,
        comment="为该 canonical 记录提供证据的来源观察批次。",
    )
    role: Mapped[str] = mapped_column(
        String(16), primary_key=True, nullable=False, comment="主要、佐证或派生输入血缘角色。"
    )
    raw_payload_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("raw_payload_manifest.raw_payload_id", ondelete="RESTRICT"),
        nullable=True,
        comment="可精确定位时关联的 raw、标准化或附件对象；否则为空。",
    )
    transform_hash: Mapped[str] = mapped_column(
        CHAR(64), nullable=False, comment="从证据到 canonical 记录的转换规则 SHA-256 摘要。"
    )
