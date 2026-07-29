"""分类体系内板块永久键、当前展示投影和来源激活状态模型。"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ...base import Base


class SectorEntity(Base):
    """保存分类体系内的板块永久键与当前展示状态。

    `sector_key` 是所有行情、成分和 `EOD` 事实使用的稳定内部身份，来源代码/名称只是该体系内的
    当前投影。目录同步可以把早先由行情创建的 `PENDING` 身份升级为 `ACTIVE`，但不能因当前目录
    缺席删除历史身份或把同名板块跨 `scheme` 合并。
    """

    __tablename__ = "sector_entity"
    __table_args__ = (
        CheckConstraint("BTRIM(sector_code) <> ''", name="ck_sector_entity_sector_code"),
        CheckConstraint(
            "status IN ('PENDING', 'ACTIVE', 'RETIRED')", name="ck_sector_entity_status"
        ),
        UniqueConstraint("scheme", "sector_code"),
        Index("ix_sector_entity_scheme_code", "scheme", "sector_code"),
        {"comment": "分类体系内板块永久身份与当前兼容投影。"},
    )

    sector_key: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        autoincrement=True,
        nullable=False,
        comment="数据库内部板块关联键，不对外暴露。",
    )
    sector_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), unique=True, nullable=False, comment="板块永久 UUID。"
    )
    scheme: Mapped[str] = mapped_column(
        String(64), ForeignKey("sector_scheme.scheme"), nullable=False, comment="板块所属分类体系。"
    )
    sector_code: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="分类体系内稳定板块代码。"
    )
    name: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="当前观察到的板块展示名称。"
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="待确认、可用或退役状态。"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="板块身份创建时间。"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="当前兼容投影更新时间。"
    )
