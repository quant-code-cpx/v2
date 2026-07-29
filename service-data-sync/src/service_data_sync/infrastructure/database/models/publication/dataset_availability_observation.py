"""记录不产生 canonical 事实的同步可用性、空集与原因观测。"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, Index, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base


class DatasetAvailabilityObservation(Base):
    """保存空集或来源不可用状态，避免向强约束事实表写入伪空行。

    合法空结果、未注册来源和暂时不可用都属于观察结果，但它们不等同于业务事实为零，更不能
    用一条虚构记录污染金额、价格或成分表。新的观察通过 `superseded_at` 替代旧可用性判断；
    消费者可据此决定展示空列表、暂不可用或保留既有发布，而不是误读为数据已被删除。
    """

    __tablename__ = "dataset_availability_observation"
    __table_args__ = (
        CheckConstraint(
            "availability IN ('empty', 'source_unavailable')",
            name="ck_dataset_availability_observation_state",
        ),
        UniqueConstraint(
            "dataset",
            "partition_key",
            "observed_at",
            name="uq_dataset_availability_observation_time",
        ),
        Index(
            "uq_dataset_availability_observation_current",
            "dataset",
            "partition_key",
            unique=True,
            postgresql_where="superseded_at IS NULL",
        ),
        {"comment": "空集与来源不可用的可审计观测；不代表 canonical 事实发布。"},
    )

    observation_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, nullable=False, comment="观测永久 UUID。"
    )
    dataset: Mapped[str] = mapped_column(
        String(100), nullable=False, comment="provider-neutral canonical dataset 名称。"
    )
    partition_key: Mapped[str] = mapped_column(
        String(240), nullable=False, comment="请求身份与时间窗口构成的稳定观测分区。"
    )
    availability: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="empty 或 source_unavailable。"
    )
    reason_code: Mapped[str] = mapped_column(
        String(80), nullable=False, comment="稳定诊断码；不保存上游异常原文。"
    )
    provider_id: Mapped[str | None] = mapped_column(
        String(100), nullable=True, comment="产生或尝试产生该观测的 adapter 标识。"
    )
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="来源响应或失败被观察到的 UTC 时刻。"
    )
    superseded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="同分区较新观测取代本观测的时刻。"
    )
    detail: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="受限、无敏感信息的运维补充说明。"
    )
