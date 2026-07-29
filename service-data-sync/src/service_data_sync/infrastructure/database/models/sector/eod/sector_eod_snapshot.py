"""一个分类体系一个交易日完整板块 `EOD` 横截面的候选/发布 `revision` 模型。"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    UniqueConstraint,
    desc,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ...base import Base


class SectorEodSnapshot(Base):
    """保存一个分类体系和交易日的完整横截面 `revision`，发布切换在同一事务中完成。

    快照头记录来源、目录覆盖、质量状态、候选/隔离/已发布阶段和内容摘要；只有行数与预期全集
    相符并通过发布门的完整候选，才能与 `dataset_publication` 在一个事务中切换。相同内容重跑保持
    幂等，新版本或受控回滚只改变可见指针，绝不删除历史报价、质量证据或更晚候选。
    """

    __tablename__ = "sector_eod_snapshot"
    __table_args__ = (
        CheckConstraint("revision > 0"),
        CheckConstraint("finality = 'post_close_observation'"),
        CheckConstraint(
            "state IN ('candidate', 'quarantined', 'published', 'superseded')",
            name="ck_sector_eod_snapshot_state",
        ),
        CheckConstraint(
            "quality_status IN ('passed', 'warned', 'quarantined')",
            name="ck_sector_eod_snapshot_quality_status",
        ),
        CheckConstraint("record_count >= 0"),
        CheckConstraint("expected_count >= 0"),
        CheckConstraint("coverage_ratio >= 0 AND coverage_ratio <= 1"),
        CheckConstraint("observed_at >= source_cutoff_at"),
        CheckConstraint(
            "(state = 'published' AND published_at IS NOT NULL AND superseded_at IS NULL "
            "AND quality_status IN ('passed', 'warned')) "
            "OR (state = 'superseded' AND published_at IS NOT NULL "
            "AND superseded_at IS NOT NULL) "
            "OR (state IN ('candidate', 'quarantined') AND published_at IS NULL)"
        ),
        UniqueConstraint("scheme", "trade_date", "revision"),
        Index(
            "uq_sector_eod_snapshot_current",
            "scheme",
            "trade_date",
            unique=True,
            postgresql_where="state = 'published' AND superseded_at IS NULL",
        ),
        Index(
            "ix_sector_eod_snapshot_latest",
            "scheme",
            desc("trade_date"),
            postgresql_include=["data_version", "observed_at", "quality_status", "published_at"],
            postgresql_where="state = 'published' AND superseded_at IS NULL",
        ),
        {"comment": "板块收盘后完整横截面 revision；finality 仅表示内部截点后的观察。"},
    )

    snapshot_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, nullable=False, comment="EOD 快照永久 UUID。"
    )
    data_version: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        unique=True,
        nullable=False,
        comment="消费者缓存、ETag 与 cursor 绑定的不可变版本。",
    )
    scheme: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("sector_scheme.scheme"),
        nullable=False,
        comment="横截面所属分类体系。",
    )
    trade_date: Mapped[date] = mapped_column(Date, nullable=False, comment="上海时区目标交易日。")
    revision: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="同 scheme/交易日内容变化产生的递增 revision。"
    )
    source_cutoff_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="内部允许进行 EOD 观察的截点时间。"
    )
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="来源批量响应实际观察时间。"
    )
    finality: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="固定为 post_close_observation，不宣称供应商或交易所最终态。",
    )
    state: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="候选、隔离、当前发布或已 supersede 状态。"
    )
    quality_status: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="EOD 快照质量评估结果。"
    )
    record_count: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="快照中持久化报价行数。"
    )
    expected_count: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="运行开始时冻结的 ACTIVE 板块预期数。"
    )
    coverage_ratio: Mapped[Decimal] = mapped_column(
        Numeric(9, 8), nullable=False, comment="报价行数相对预期板块数的覆盖率。"
    )
    normalizer_version: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="归一化和质量规则版本。"
    )
    content_sha256: Mapped[bytes] = mapped_column(
        LargeBinary, nullable=False, comment="完整横截面规范化内容稳定摘要。"
    )
    source_batch_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("source_batch.source_batch_id", ondelete="RESTRICT"),
        nullable=False,
        comment="支撑本 EOD revision 的来源观察。",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="该 EOD revision 创建时间。"
    )
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="开始作为当前消费者版本可见的时间。"
    )
    superseded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="被新 revision 或受控 rollback 切换后标记的时间。",
    )
