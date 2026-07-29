"""资金流方法学版本内订单规模或全量成交分桶的受控定义模型。"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, Numeric, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base


class MoneyFlowBucketDefinition(Base):
    """保存一个方法学版本内的 `bucket` 及已知或未知阈值。

    主力、超大单、大单等标签并非跨供应商通用事实；它们由该方法学的订单规模或交易方向定义决定。
    阈值已知时冻结数值和单位，未知时必须显式保留未知而非编造范围；因此后续来源口径变化要建立
    新方法学/版本，不能修改旧分桶来重解释历史净流入。
    """

    __tablename__ = "money_flow_bucket_definition"
    __table_args__ = (
        UniqueConstraint(
            "version_id",
            "bucket_code",
            name="uq_money_flow_bucket_definition",
        ),
        CheckConstraint(
            "definition_status IN ('documented', 'inferred_unapproved', 'unknown')",
            name="ck_money_flow_bucket_definition_status",
        ),
        CheckConstraint(
            "threshold_min IS NULL OR threshold_max IS NULL OR threshold_min <= threshold_max",
            name="ck_money_flow_bucket_definition_range",
        ),
        CheckConstraint(
            "definition_status <> 'unknown' OR (threshold_min IS NULL AND threshold_max IS NULL)",
            name="ck_money_flow_bucket_definition_unknown",
        ),
        {"comment": "资金流 bucket 定义；未知阈值保持空值而不推测。"},
    )

    bucket_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, nullable=False, comment="分桶永久 UUID。"
    )
    version_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("money_flow_methodology_version.version_id"),
        nullable=False,
        comment="所属方法学版本。",
    )
    bucket_code: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="版本内稳定 bucket 代码。"
    )
    label: Mapped[str] = mapped_column(
        String(100), nullable=False, comment="面向消费者的分桶名称。"
    )
    definition_status: Mapped[str] = mapped_column(
        String(24), nullable=False, comment="阈值定义证据状态。"
    )
    threshold_min: Mapped[Decimal | None] = mapped_column(
        Numeric(30, 6), nullable=True, comment="已证实的包含下界。"
    )
    threshold_max: Mapped[Decimal | None] = mapped_column(
        Numeric(30, 6), nullable=True, comment="已证实的排除或包含上界，由方法学说明。"
    )
    threshold_unit: Mapped[str | None] = mapped_column(
        String(32), nullable=True, comment="阈值所用单位；未知阈值为空。"
    )
