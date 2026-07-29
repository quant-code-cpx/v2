"""记录来源证据、确定性规范化运行及其强类型事实审计索引。"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CHAR,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base


class RawPayloadManifest(Base):
    """把来源观察关联的 raw、标准化载荷或附件逐项固定为不可变证据元数据。

    数据库只保存对象 URI、摘要、大小和取得时间，不保存可能庞大或受限的载荷正文；对象内容由
    私有存储的保留策略管理。一个 `SourceBatch` 可以有多个对象和角色，序号与摘要使历史重放、
    schema 排障和证据校验不依赖“最新文件名”。成功路径未归档时也允许没有对应证据项。
    """

    __tablename__ = "raw_payload_manifest"
    __table_args__ = (
        CheckConstraint("sequence_no > 0", name="ck_raw_payload_manifest_sequence"),
        CheckConstraint(
            "role IN ('raw', 'normalized', 'attachment')", name="ck_raw_payload_manifest_role"
        ),
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="ck_raw_payload_manifest_sha256"),
        CheckConstraint("byte_size >= 0", name="ck_raw_payload_manifest_byte_size"),
        UniqueConstraint(
            "source_batch_id", "role", "sequence_no", name="uq_raw_payload_manifest_position"
        ),
        {"comment": "一个来源批次中每页、文件或附件的不可变对象存储清单。"},
    )

    raw_payload_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        nullable=False,
        comment="原始对象清单记录永久 UUID。",
    )
    source_batch_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("source_batch.source_batch_id", ondelete="RESTRICT"),
        nullable=False,
        comment="该对象所属的来源观察批次。",
    )
    sequence_no: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="同一批次与角色内从一开始的稳定顺序。"
    )
    role: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="原始响应、标准化载荷或附件证据角色。"
    )
    object_uri: Mapped[str] = mapped_column(
        Text, nullable=False, comment="私有对象存储中的不可变对象 URI。"
    )
    sha256: Mapped[str] = mapped_column(
        CHAR(64), nullable=False, comment="对象字节流的 SHA-256 十六进制摘要。"
    )
    content_type: Mapped[str] = mapped_column(
        String(120), nullable=False, comment="对象写入时确认的媒体类型。"
    )
    byte_size: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="对象字节数，不能为负。"
    )
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="对象从上游获取并归档的时间。"
    )


class NormalizationRun(Base):
    """记录将来源证据转换为一个 canonical 候选分区的确定性运行。

    `adapter_version`、`mapping_version`、`schema_fingerprint` 与 `input_set_hash` 冻结本次转换的
    可复算条件；同样原始数据在规则更新后可以产生新的运行，而不会覆盖旧候选。运行完成不等于
    对消费者可见，仍须通过质量门并由不可变 release/publication 选择后才发布。
    """

    __tablename__ = "normalization_run"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'passed', 'failed')", name="ck_normalization_run_status"
        ),
        CheckConstraint(
            "schema_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_normalization_run_schema_fingerprint",
        ),
        CheckConstraint(
            "input_set_hash ~ '^[0-9a-f]{64}$'", name="ck_normalization_run_input_set_hash"
        ),
        CheckConstraint(
            "(status = 'running' AND finished_at IS NULL) OR "
            "(status IN ('passed', 'failed') AND finished_at IS NOT NULL)",
            name="ck_normalization_run_finished_at",
        ),
        UniqueConstraint(
            "dataset_id",
            "partition_key",
            "input_set_hash",
            "mapping_version",
            name="uq_normalization_run_input",
        ),
        {"comment": "从 adapter 标准载荷到 canonical 候选事实的一次确定性转换运行。"},
    )

    normalization_run_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, nullable=False, comment="规范化运行永久 UUID。"
    )
    dataset_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("canonical_dataset.dataset_id", ondelete="RESTRICT"),
        nullable=False,
        comment="本次运行写入的 canonical 数据集。",
    )
    partition_key: Mapped[str] = mapped_column(
        String(240), nullable=False, comment="数据集内由本次运行处理的稳定分区键。"
    )
    run_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("sync_run.run_id", ondelete="RESTRICT"),
        nullable=False,
        comment="承载该规范化步骤的可恢复同步运行。",
    )
    adapter_version: Mapped[str] = mapped_column(
        String(96), nullable=False, comment="生成输入标准载荷的 adapter 固定版本。"
    )
    schema_fingerprint: Mapped[str] = mapped_column(
        CHAR(64), nullable=False, comment="输入来源结构的 SHA-256 指纹。"
    )
    mapping_version: Mapped[str] = mapped_column(
        String(96), nullable=False, comment="canonical 字段映射及校验规则版本。"
    )
    input_set_hash: Mapped[str] = mapped_column(
        CHAR(64), nullable=False, comment="全部输入 raw 清单的顺序无关 SHA-256 摘要。"
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="运行中、通过或失败状态。"
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="开始读取已归档证据的时间。"
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="通过或失败完成时间；运行中为空。"
    )


class NormalizedRecordManifest(Base):
    """建立规范化输入记录到强类型事实行的审计索引，不充当消费者查询投影。

    它用内容摘要和 canonical 主键把一条输入定位到实际写入的领域表，`disposition` 同时记录
    接受、去重、隔离等处置结果。消费者不能把此表当作最新事实，因为其行既可能来自未发布候选，
    也可能只是失败审计；读取必须依附相应 release 或 publication。
    """

    __tablename__ = "normalized_record_manifest"
    __table_args__ = (
        CheckConstraint(
            "record_key_hash ~ '^[0-9a-f]{64}$'",
            name="ck_normalized_record_manifest_key_hash",
        ),
        CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'",
            name="ck_normalized_record_manifest_content_hash",
        ),
        CheckConstraint(
            "disposition IN ('candidate', 'accepted', 'quarantined')",
            name="ck_normalized_record_manifest_disposition",
        ),
        {"comment": "规范化记录到强类型事实主键的审计索引，禁止用作通用业务查询。"},
    )

    normalization_run_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("normalization_run.normalization_run_id", ondelete="RESTRICT"),
        primary_key=True,
        nullable=False,
        comment="产生该规范化记录的确定性运行。",
    )
    record_key_hash: Mapped[str] = mapped_column(
        CHAR(64), primary_key=True, nullable=False, comment="canonical 业务键的 SHA-256 摘要。"
    )
    canonical_table: Mapped[str] = mapped_column(
        String(96), nullable=False, comment="承载该事实的强类型 canonical 表名。"
    )
    canonical_pk: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, comment="仅供审计定位的强类型事实主键 JSON。"
    )
    content_hash: Mapped[str] = mapped_column(
        CHAR(64), nullable=False, comment="规范化记录内容的 SHA-256 摘要。"
    )
    disposition: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="候选、已接受或已隔离的处理结果。"
    )
