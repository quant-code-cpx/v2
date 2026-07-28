"""显式上市生命周期恢复检查点模型。"""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, Date, DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ...base import Base


class EquityLifecycleCheckpoint(Base):
    """保存每所最后成功发布及其 raw/标准证据位置，供无上游请求的确定性 replay。"""

    __tablename__ = "equity_lifecycle_checkpoint"
    __table_args__ = (
        CheckConstraint(
            "exchange IN ('SSE', 'SZSE', 'BSE')",
            name="ck_equity_lifecycle_checkpoint_exchange",
        ),
        CheckConstraint(
            "schema_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_equity_lifecycle_checkpoint_schema_fingerprint",
        ),
        {"comment": "显式上市生命周期最后成功发布与确定性恢复检查点。"},
    )

    exchange: Mapped[str] = mapped_column(
        String(4),
        primary_key=True,
        nullable=False,
        comment="检查点所属交易所。",
    )
    target_date: Mapped[date] = mapped_column(
        Date,
        nullable=False,
        comment="本批生命周期证据对应的目标市场日。",
    )
    data_version: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("dataset_publication.data_version", ondelete="RESTRICT"),
        nullable=False,
        comment="成功发布后可见的目录数据版本。",
    )
    snapshot_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("equity_master_snapshot.snapshot_id", ondelete="RESTRICT"),
        nullable=False,
        comment="最后成功生命周期证据快照。",
    )
    source_batch_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("source_batch.source_batch_id", ondelete="RESTRICT"),
        nullable=False,
        comment="最后成功发布所消费的来源观测批次。",
    )
    raw_uri: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="供应商原始响应的不可变对象位置。",
    )
    normalized_uri: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="provider-neutral 标准批次的不可变对象位置，供 replay 解码。",
    )
    provider_id: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        comment="产生原始证据的 adapter 身份。",
    )
    upstream_source: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        comment="adapter 记录的真实上游来源。",
    )
    adapter_version: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="生成标准批次的固定 adapter 版本。",
    )
    schema_fingerprint: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="最后成功批次的原始表头指纹。",
    )
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment="原始来源事实首次被观测的 UTC 时刻。",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment="检查点与 publication 同事务推进的 UTC 时刻。",
    )
