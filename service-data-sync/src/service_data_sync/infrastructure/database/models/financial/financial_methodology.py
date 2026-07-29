"""财务来源、算法、单位与适用口径的固定方法学版本模型。"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import CHAR, CheckConstraint, DateTime, SmallInteger, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base


class FinancialMethodology(Base):
    """固化一种财务能力的来源或算法版本，禁止消费者隐式选择“最新”版本。

    方法学决定披露字段如何解释、是否允许派生、使用什么币种/单位和何种知识时间依据。相同指标名
    在不同来源、合并范围或计算规则下可能不可比，因此消费者发布必须显式绑定此键；停用方法学
    只阻止新生产，不会改变已发布历史的含义。
    """

    __tablename__ = "financial_methodology"
    __table_args__ = (
        CheckConstraint("version > 0", name="ck_financial_methodology_version"),
        CheckConstraint(
            "capability IN ('financial.report', 'financial.provider-metric', "
            "'financial.derived-metric', 'financial.valuation')",
            name="ck_financial_methodology_capability",
        ),
        CheckConstraint(
            "status IN ('draft', 'validated', 'retired')",
            name="ck_financial_methodology_status",
        ),
        CheckConstraint(
            "semantic_spec_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_financial_methodology_semantic_spec_sha256",
        ),
        UniqueConstraint("code", "version", name="uq_financial_methodology_code_version"),
        {"comment": "财务来源或算法的不可变版本身份；不同版本和口径不得混算。"},
    )

    methodology_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, nullable=False, comment="方法学永久 UUID。"
    )
    code: Mapped[str] = mapped_column(
        String(80), nullable=False, comment="稳定的方法学代码，不携带供应商 URL。"
    )
    version: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, comment="同一方法学代码内递增的不可变版本号。"
    )
    capability: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="该方法学唯一服务的财务 canonical 能力。"
    )
    source_code: Mapped[str] = mapped_column(
        String(80), nullable=False, comment="已批准来源或平台算法身份；不保存请求地址。"
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="草拟、已验证或已退役状态；只有已验证版本可发布。"
    )
    semantic_spec_sha256: Mapped[str] = mapped_column(
        CHAR(64), nullable=False, comment="方法、单位、时间和范围语义说明的稳定摘要。"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="该不可变方法学版本创建时间。"
    )
