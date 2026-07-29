"""申万三级行业完整快照的结构、闭包、覆盖和估值有限性质量证据模型。"""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, Date, DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ...base import Base


class SwSectorQualityResult(Base):
    """记录 `schema`、三级闭包、覆盖和估值有限数检查结果。

    结构错误、缺级、循环父子关系、重复代码、覆盖不足或非有限估值都会阻断该观测日的发布；每条规则
    保存实际值、阈值和有限样本，便于重放时复核。质量通过不代表来源成为官方最终值，只说明在冻结
    方法学下足以形成可读 `taxonomy`/估值 `data_version`。
    """

    __tablename__ = "sw_sector_quality_result"
    __table_args__ = (
        CheckConstraint(
            "capability IN ('sector.sw.taxonomy', 'sector.sw.valuation')",
            name="ck_sw_sector_quality_capability",
        ),
        CheckConstraint(
            "status IN ('passed', 'warned', 'failed')",
            name="ck_sw_sector_quality_status",
        ),
        {"comment": "申万完整快照的低基数质量规则证据与发布处置。"},
    )

    quality_result_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, nullable=False, comment="质量结果永久 UUID。"
    )
    source_batch_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("source_batch.source_batch_id", ondelete="RESTRICT"),
        nullable=False,
        comment="被校验的 raw evidence 来源批次。",
    )
    data_version: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("sw_sector_publication.data_version", ondelete="RESTRICT"),
        nullable=False,
        comment="该规则支撑的不可变消费者发布。",
    )
    capability: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="taxonomy 或估值质量规则所属能力。"
    )
    snapshot_date: Mapped[date] = mapped_column(
        Date, nullable=False, comment="被校验完整快照的上海日历日期。"
    )
    rule_code: Mapped[str] = mapped_column(
        String(80), nullable=False, comment="稳定低基数质量规则代码。"
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="通过、警告或失败结论。"
    )
    actual: Mapped[object] = mapped_column(
        JSONB, nullable=False, comment="不含原始响应的实际计数或摘要。"
    )
    expected: Mapped[object] = mapped_column(
        JSONB, nullable=False, comment="该规则固定的期望范围或不变量。"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="质量规则执行完成时间。"
    )
