"""资金流同步分区质量结果模型。"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, Index, Integer, Numeric, String, Text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base


class MoneyFlowQualityResult(Base):
    """记录 schema、身份、单位、完整性与度量规则的可审计结果。"""

    __tablename__ = "money_flow_quality_result"
    __table_args__ = (
        CheckConstraint(
            "dataset_kind IN ('daily', 'ranking', 'methodology')",
            name="ck_money_flow_quality_result_kind",
        ),
        CheckConstraint(
            "severity IN ('warn', 'error')",
            name="ck_money_flow_quality_result_severity",
        ),
        CheckConstraint(
            "status IN ('passed', 'warned', 'rejected')",
            name="ck_money_flow_quality_result_status",
        ),
        Index(
            "ix_money_flow_quality_result_partition",
            "partition_key",
            "created_at",
        ),
        {"comment": "资金流质量规则结果；诊断样本只引用 raw URI，不复制业务载荷。"},
    )

    result_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, nullable=False, comment="质量结果永久 UUID。"
    )
    dataset_kind: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="daily、ranking 或 methodology。"
    )
    partition_key: Mapped[str] = mapped_column(
        String(240), nullable=False, comment="定位同步分区的稳定键。"
    )
    rule_code: Mapped[str] = mapped_column(String(64), nullable=False, comment="稳定质量规则代码。")
    severity: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="warn 或阻断 publication 的 error。"
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="规则通过、告警或拒绝结果。"
    )
    actual_value: Mapped[Decimal | None] = mapped_column(
        Numeric(30, 8), nullable=True, comment="可数值表达的实际观测。"
    )
    threshold_value: Mapped[Decimal | None] = mapped_column(
        Numeric(30, 8), nullable=True, comment="本规则使用的数值阈值。"
    )
    affected_count: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="受该质量结果影响的行数。"
    )
    sample_raw_uri: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="最小诊断样本所在私有 raw URI。"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="质量规则执行完成时间。"
    )
