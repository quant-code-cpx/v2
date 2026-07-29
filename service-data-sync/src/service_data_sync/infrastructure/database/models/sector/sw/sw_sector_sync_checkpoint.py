"""申万行业指定观测日成功发布后的证据摘要与可重放 `checkpoint` 模型。"""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from sqlalchemy import CHAR, CheckConstraint, Date, DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ...base import Base


class SwSectorSyncCheckpoint(Base):
    """保存精确观测日最近成功发布后的证据摘要与中立载荷重放位置。

    检查点只在完整 `taxonomy`/估值质量通过并发布后更新，记录来源、哈希、私有对象引用和适用方法学；
    重放先校验摘要再解码，避免遭篡改对象形成新版本。新成功批次采用失败留证策略时可能没有完整
    正文，历史 checkpoint 仍可回放；失败、隔离或未发布候选绝不推进它。
    """

    __tablename__ = "sw_sector_sync_checkpoint"
    __table_args__ = (
        CheckConstraint(
            "summary_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_sw_sector_checkpoint_summary_sha256",
        ),
        CheckConstraint(
            "raw_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_sw_sector_checkpoint_raw_sha256",
        ),
        CheckConstraint(
            "schema_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_sw_sector_checkpoint_schema_fingerprint",
        ),
        {"comment": "申万快照按日恢复 checkpoint；只在两项 publication 成功后推进。"},
    )

    capability: Mapped[str] = mapped_column(
        String(64), primary_key=True, nullable=False, comment="完整申万快照中立抓取能力。"
    )
    partition_key: Mapped[str] = mapped_column(
        String(64), primary_key=True, nullable=False, comment="固定 scheme 与观测日组成的恢复分区。"
    )
    snapshot_date: Mapped[date] = mapped_column(
        Date, nullable=False, comment="checkpoint 对应的上海日历观测日期。"
    )
    summary_sha256: Mapped[str] = mapped_column(
        CHAR(64), nullable=False, comment="可重放中立载荷的 SHA-256 摘要。"
    )
    raw_sha256: Mapped[str] = mapped_column(
        CHAR(64), nullable=False, comment="供应商原始响应的 SHA-256 摘要。"
    )
    raw_uri: Mapped[str] = mapped_column(
        String(1024), nullable=False, comment="服务私有桶中的供应商原始响应 URI。"
    )
    normalized_uri: Mapped[str] = mapped_column(
        String(1024), nullable=False, comment="服务私有桶中的中立可重放载荷 URI。"
    )
    provider_id: Mapped[str] = mapped_column(
        String(120), nullable=False, comment="产生该快照的 adapter provider 身份。"
    )
    upstream_source: Mapped[str] = mapped_column(
        String(120), nullable=False, comment="快照实际展示来源身份。"
    )
    adapter_version: Mapped[str] = mapped_column(
        String(120), nullable=False, comment="生成中立载荷的 adapter 版本。"
    )
    schema_fingerprint: Mapped[str] = mapped_column(
        CHAR(64), nullable=False, comment="该来源列集合的冻结 fingerprint。"
    )
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="实际取得来源响应的 UTC 时间。"
    )
    last_data_version: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("sw_sector_publication.data_version", ondelete="RESTRICT"),
        nullable=False,
        comment="最近成功 taxonomy publication 的不可变版本。",
    )
    last_success_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="两项发布均成功完成的时间。"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="checkpoint 最近推进时间。"
    )
