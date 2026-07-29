"""稳定 canonical dataset 身份模型。"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base


class CanonicalDataset(Base):
    """定义跨运行保持稳定的数据集及其不可混用的 schema 版本。"""

    __tablename__ = "canonical_dataset"
    __table_args__ = (
        CheckConstraint("schema_version > 0", name="ck_canonical_dataset_schema_version"),
        CheckConstraint(
            "status IN ('research', 'candidate', 'production', 'retired')",
            name="ck_canonical_dataset_status",
        ),
        UniqueConstraint("code", "schema_version", name="uq_canonical_dataset_code_version"),
        {"comment": "canonical 数据集的稳定身份、schema 版本和可用状态。"},
    )

    dataset_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, nullable=False, comment="数据集永久 UUID。"
    )
    code: Mapped[str] = mapped_column(
        String(120), nullable=False, comment="消费者和内部契约使用的稳定数据集编码。"
    )
    schema_version: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="不可与同编码其他结构混用的 schema 主版本。"
    )
    domain: Mapped[str] = mapped_column(
        String(48), nullable=False, comment="指数、ETF、融资融券等所属业务数据域。"
    )
    grain: Mapped[str] = mapped_column(
        Text, nullable=False, comment="一条事实的业务粒度与唯一性说明。"
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="研究、候选、生产或退役状态。"
    )
    owner_service: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="负责该数据集写入和发布的服务标识。"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="数据集记录首次登记时间。"
    )
