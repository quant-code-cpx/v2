"""股票发现横截面每证券每语义族可用性模型。"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ...base import Base


class EquityDiscoveryAvailability(Base):
    """原因化保存每行缺失，禁止消费者把来源不可用、合法空和不适用都当成零。"""

    __tablename__ = "equity_discovery_availability"
    __table_args__ = (
        CheckConstraint(
            "availability IN "
            "('DATA', 'LEGITIMATE_EMPTY', 'NOT_APPLICABLE', 'SOURCE_UNAVAILABLE', "
            "'QUARANTINED', 'STALE_LAST_GOOD')",
            name="ck_equity_discovery_availability_state",
        ),
        {"comment": "股票发现横截面每证券每语义族可用性与组件血缘。"},
    )

    release_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("dataset_release.release_id", ondelete="RESTRICT"),
        primary_key=True,
        nullable=False,
        comment="所属冻结横截面 release；消费者 dataVersion 由 publication 关联取得。",
    )
    security_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("equity_instrument.security_id", ondelete="RESTRICT"),
        primary_key=True,
        nullable=False,
        comment="可用性对应永久证券内部键。",
    )
    family: Mapped[str] = mapped_column(
        String(64), primary_key=True, nullable=False, comment="market、capitalization 等语义族。"
    )
    availability: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="数据、合法空、不适用、失败、隔离或陈旧状态。"
    )
    null_reason: Mapped[str | None] = mapped_column(
        String(160), nullable=True, comment="值为空时的稳定原因码；真实零值不得使用本字段。"
    )
    component_data_version: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True, comment="提供本语义族事实的组件 dataVersion。"
    )
    source_label: Mapped[str | None] = mapped_column(
        String(120), nullable=True, comment="允许内部消费者展示的来源标签。"
    )
    methodology: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True, comment="方法学代码、版本与语义族；不得存储凭据或 raw 字段。"
    )
