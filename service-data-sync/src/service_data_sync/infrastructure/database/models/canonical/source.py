"""真实上游权利主体及其数据产品目录模型。"""

from __future__ import annotations

from datetime import date
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base


class DataSource(Base):
    """登记真实数据所有者及其权利状态，避免把技术 adapter 误当作来源主体。

    adapter 是本服务如何取得数据的技术实现，而 `DataSource` 表示数据实际来自谁、时区如何
    解释、能否保存或再分发。来源身份稳定后，变更抓取库或 HTTP 实现不会改写已有事实的权利
    归属；无法证明的权利信息必须保留证据引用，而不能默认允许消费。
    """

    __tablename__ = "data_source"
    __table_args__ = (
        CheckConstraint("length(btrim(code)) > 0", name="ck_data_source_code"),
        CheckConstraint("length(btrim(source_kind)) > 0", name="ck_data_source_kind"),
        CheckConstraint("length(btrim(timezone)) > 0", name="ck_data_source_timezone"),
        CheckConstraint("length(btrim(rights_status)) > 0", name="ck_data_source_rights_status"),
        {"comment": "真实上游数据所有者及其权限审计信息，不等同于 adapter。"},
    )

    source_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, nullable=False, comment="真实上游来源永久 UUID。"
    )
    code: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False, comment="真实来源的稳定内部编码。"
    )
    legal_name: Mapped[str] = mapped_column(
        Text, nullable=False, comment="来源所有者或授权主体的法定名称。"
    )
    source_kind: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="官方、商业、公开或平台等来源类别。"
    )
    timezone: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="来源公告或业务日期解释采用的 IANA 时区。"
    )
    rights_status: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="研究、内部或可再分发等当前权限状态。"
    )
    rights_evidence_ref: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="权限条款、采购凭据或审核记录的私有引用。"
    )


class SourceDataset(Base):
    """登记真实来源下的具体数据产品，而不是单个 adapter 请求。

    `capability` 描述 provider-neutral 业务能力，`native_grain`、单位、历史范围和许可范围保留
    上游原始口径；多个 adapter 可以服务同一数据产品，但不应把不同产品或不同许可混为一项。
    `active` 仅控制新观察能否采用，不会抹去已有来源血缘。
    """

    __tablename__ = "source_dataset"
    __table_args__ = (
        CheckConstraint(
            "history_to IS NULL OR history_from IS NULL OR history_to >= history_from",
            name="ck_source_dataset_history_range",
        ),
        UniqueConstraint("source_id", "code", name="uq_source_dataset_source_code"),
        Index("ix_source_dataset_capability", "capability"),
        {"comment": "真实上游的具体数据产品、能力、原始粒度和许可范围。"},
    )

    source_dataset_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, nullable=False, comment="来源数据产品永久 UUID。"
    )
    source_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("data_source.source_id", ondelete="RESTRICT"),
        nullable=False,
        comment="拥有该数据产品的真实上游来源。",
    )
    code: Mapped[str] = mapped_column(
        String(120), nullable=False, comment="来源产品或栏目在来源范围内的稳定编码。"
    )
    capability: Mapped[str] = mapped_column(
        String(120), nullable=False, comment="该产品能够提供的 provider-neutral 数据能力。"
    )
    native_grain: Mapped[str] = mapped_column(
        Text, nullable=False, comment="来源记录的原始业务粒度说明。"
    )
    native_unit_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, comment="来源字段单位、缩放和币种等非事实元数据。"
    )
    history_from: Mapped[date | None] = mapped_column(
        Date, nullable=True, comment="已确认可获取历史的起始业务日期。"
    )
    history_to: Mapped[date | None] = mapped_column(
        Date, nullable=True, comment="已确认可获取历史的结束业务日期；持续提供时为空。"
    )
    license_scope: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="该来源产品允许的存储与使用范围。"
    )
    active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, comment="来源产品是否仍允许被新的同步运行使用。"
    )
