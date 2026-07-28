"""资金流方法学不可变版本模型。"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base


class MoneyFlowMethodologyVersion(Base):
    """冻结供应商来源、算法、方向、单位、最终态与支持度量。"""

    __tablename__ = "money_flow_methodology_version"
    __table_args__ = (
        UniqueConstraint(
            "methodology_id",
            "version",
            name="uq_money_flow_methodology_version",
        ),
        CheckConstraint(
            "status IN ('unknown', 'research', 'validated', 'retired')",
            name="ck_money_flow_methodology_version_status",
        ),
        CheckConstraint(
            "semantic_family IN ('trade_direction_flow', 'order_size_flow')",
            name="ck_money_flow_methodology_version_semantic_family",
        ),
        CheckConstraint(
            "finality IN ('source_reported_daily', 'post_close_observation', 'unknown')",
            name="ck_money_flow_methodology_version_finality",
        ),
        CheckConstraint(
            "supports_gross_inflow OR supports_gross_outflow "
            "OR supports_net_amount OR supports_net_ratio",
            name="ck_money_flow_methodology_version_measure",
        ),
        CheckConstraint(
            "NOT production_enabled OR status = 'validated'",
            name="ck_money_flow_methodology_version_production",
        ),
        {"comment": "资金流方法学不可变版本；任何来源或单位变化均创建新版本。"},
    )

    version_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, nullable=False, comment="方法学版本永久 UUID。"
    )
    methodology_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("money_flow_methodology.methodology_id"),
        nullable=False,
        comment="所属稳定方法学身份。",
    )
    version: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="方法学不可变版本字符串。"
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, comment="技术验证状态。")
    adapter_provider: Mapped[str] = mapped_column(
        String(100), nullable=False, comment="内部 adapter 标识，不对公开 API 暴露。"
    )
    upstream_source: Mapped[str] = mapped_column(
        String(100), nullable=False, comment="可解释的上游来源身份。"
    )
    source_dataset: Mapped[str] = mapped_column(
        String(160), nullable=False, comment="来源数据集身份。"
    )
    semantic_family: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="交易方向或订单规模语义族。"
    )
    direction_definition: Mapped[str] = mapped_column(
        Text, nullable=False, comment="来源方向判定与净额语义。"
    )
    ratio_denominator: Mapped[str] = mapped_column(
        Text, nullable=False, comment="净占比分母定义；未知时显式说明未知。"
    )
    finality: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="来源对 observation 最终态的声明。"
    )
    currency: Mapped[str | None] = mapped_column(
        String(3), nullable=True, comment="已验证币种；来源未知时为空。"
    )
    raw_amount_unit: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="原始金额单位或 unknown。"
    )
    standard_amount_unit: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="完成可复验换算后的标准单位。"
    )
    conversion_version: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="单位或百分数换算规则版本。"
    )
    supports_gross_inflow: Mapped[bool] = mapped_column(
        Boolean, nullable=False, comment="来源是否支持流入总额。"
    )
    supports_gross_outflow: Mapped[bool] = mapped_column(
        Boolean, nullable=False, comment="来源是否支持流出总额。"
    )
    supports_net_amount: Mapped[bool] = mapped_column(
        Boolean, nullable=False, comment="来源是否支持净额。"
    )
    supports_net_ratio: Mapped[bool] = mapped_column(
        Boolean, nullable=False, comment="来源是否支持净占比。"
    )
    production_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, comment="是否通过技术门禁并允许消费者 publication。"
    )
    effective_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="本方法学版本开始适用的知识时刻。"
    )
    retired_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="版本停止新增 publication 的时刻。"
    )
