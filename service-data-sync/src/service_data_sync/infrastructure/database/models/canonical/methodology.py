"""跨新数据域使用的方法学版本模型。"""

from __future__ import annotations

from datetime import date
from uuid import UUID

from sqlalchemy import (
    CHAR,
    CheckConstraint,
    Date,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base


class MethodologyVersion(Base):
    """冻结 reported 或 derived 数据解释规则，供 release 绑定而不是运行时覆盖。"""

    __tablename__ = "methodology_version"
    __table_args__ = (
        CheckConstraint("version > 0", name="ck_methodology_version_positive"),
        CheckConstraint("kind IN ('reported', 'derived')", name="ck_methodology_version_kind"),
        CheckConstraint(
            "status IN ('candidate', 'validated', 'retired')",
            name="ck_methodology_version_status",
        ),
        CheckConstraint(
            "effective_to IS NULL OR effective_from IS NULL OR effective_to >= effective_from",
            name="ck_methodology_version_effective_range",
        ),
        CheckConstraint(
            "formula_hash IS NULL OR formula_hash ~ '^[0-9a-f]{64}$'",
            name="ck_methodology_version_formula_hash",
        ),
        UniqueConstraint("code", "version", name="uq_methodology_version_code_version"),
        {"comment": "跨数据域的方法学不可变版本；现有财务与资金流方法学暂不迁入。"},
    )

    methodology_version_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, nullable=False, comment="方法学版本永久 UUID。"
    )
    code: Mapped[str] = mapped_column(String(120), nullable=False, comment="方法学稳定编码。")
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="同一方法学编码内单调递增的版本号。"
    )
    semantic_family: Mapped[str] = mapped_column(
        String(80), nullable=False, comment="reported、derived 等跨数据集语义家族说明。"
    )
    kind: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="来源报告值或平台派生值。"
    )
    formula_hash: Mapped[str | None] = mapped_column(
        CHAR(64), nullable=True, comment="派生公式或规范化规则的 SHA-256 摘要。"
    )
    effective_from: Mapped[date | None] = mapped_column(
        Date, nullable=True, comment="该方法学开始适用的业务日期；未知时为空。"
    )
    effective_to: Mapped[date | None] = mapped_column(
        Date, nullable=True, comment="该方法学停止适用的业务日期；当前版本为空。"
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="候选、已验证或退役状态。"
    )
    documentation_ref: Mapped[str] = mapped_column(
        Text, nullable=False, comment="可审计方法学文档或受控证据的引用。"
    )
