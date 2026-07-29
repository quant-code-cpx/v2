"""外部来源批次与可审计观测身份模型。"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CHAR,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base


class SourceBatch(Base):
    """记录一次不可变外部观察，并关联执行 run、分区、raw 证据和 adapter 版本。"""

    __tablename__ = "source_batch"
    __table_args__ = (
        CheckConstraint(
            "payload_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_source_batch_payload_sha256",
        ),
        CheckConstraint("observation_seq > 0", name="ck_source_batch_observation_seq"),
        UniqueConstraint(
            "run_id",
            "partition_key",
            "observation_seq",
            name="uq_source_batch_observation",
        ),
        Index(
            "ix_source_batch_payload_lookup",
            "provider_id",
            "capability",
            "payload_sha256",
        ),
        Index("ix_source_batch_source_dataset", "source_dataset_id"),
        {"comment": "外部来源的一次独立观察；相同 payload 也保留独立审计身份。"},
    )

    source_batch_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, nullable=False, comment="来源批次永久 UUID。"
    )
    provider_id: Mapped[str] = mapped_column(
        String(100), nullable=False, comment="提供本次观察的 adapter/provider 标识。"
    )
    capability: Mapped[str] = mapped_column(
        String(100), nullable=False, comment="本次观察对应的 provider-neutral capability。"
    )
    source_dataset_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("source_dataset.source_dataset_id", ondelete="RESTRICT"),
        nullable=True,
        comment="真实上游数据产品；历史观察兼容期内允许为空。",
    )
    payload_sha256: Mapped[str] = mapped_column(
        CHAR(64), nullable=False, comment="原始来源载荷的 SHA-256 十六进制摘要。"
    )
    raw_uri: Mapped[str] = mapped_column(
        Text, nullable=False, comment="不可变 raw evidence 的私有对象存储 URI。"
    )
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="adapter 实际观察到来源响应的时间。"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="canonical 系统登记该观察的时间。"
    )
    run_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("sync_run.run_id", name="fk_source_batch_run"),
        nullable=False,
        comment="产生本观察的可恢复同步运行。",
    )
    partition_key: Mapped[str] = mapped_column(
        String(240), nullable=False, comment="run 内定位可重试数据分区的稳定键。"
    )
    observation_seq: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="同一 run/分区内递增的独立观察序号。"
    )
    upstream_source: Mapped[str] = mapped_column(
        String(100), nullable=False, comment="上游数据源或方法论身份，不因中立 adapter 而丢失。"
    )
    adapter_version: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="归一化本观察所用 adapter 版本。"
    )
    schema_fingerprint: Mapped[str] = mapped_column(
        CHAR(64), nullable=False, comment="来源结构的 SHA-256 指纹，用于识别 schema drift。"
    )
