"""消费者可见数据版本、有效时间和知识截止点模型。"""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, Date, DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base


class DatasetPublication(Base):
    """声明一个数据集分区对消费者可见的冻结版本，历史版本通过 `superseded_at` 保留。

    `data_version` 是读取、缓存、ETag 和分页游标必须绑定的稳定标识；它指向已质量判定的
    release，而非可变表中“最新一行”。`effective_as_of` 表示业务事实适用时间，
    `knowledge_cutoff` 表示本次视图允许使用的信息截止点，两者不能混为抓取时间。回滚通过
    受控地切换当前 publication 实现，历史版本和其来源仍保留。
    """

    __tablename__ = "dataset_publication"
    __table_args__ = (
        CheckConstraint(
            "quality_status IN ('passed', 'warned', 'partial')",
            name="ck_dataset_publication_quality_status",
        ),
        UniqueConstraint("dataset", "partition_key", "data_version"),
        Index(
            "uq_dataset_publication_current",
            "dataset",
            "partition_key",
            unique=True,
            postgresql_where="superseded_at IS NULL",
        ),
        Index(
            "uq_dataset_publication_release",
            "release_id",
            unique=True,
            postgresql_where="release_id IS NOT NULL",
        ),
        {"comment": "面向消费者的分区版本指针；替换版本时保留被 supersede 的发布记录。"},
    )

    publication_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, nullable=False, comment="发布记录永久 UUID。"
    )
    dataset: Mapped[str] = mapped_column(
        String(100), nullable=False, comment="canonical 数据集名称。"
    )
    partition_key: Mapped[str] = mapped_column(
        String(240), nullable=False, comment="数据集内的可见分区键。"
    )
    data_version: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        unique=True,
        nullable=False,
        comment="消费者缓存和重试绑定的不可变数据版本。",
    )
    release_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("dataset_release.release_id", ondelete="RESTRICT"),
        nullable=True,
        comment="新 canonical 数据集发布绑定的 immutable release；历史发布兼容期内为空。",
    )
    quality_status: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="发布时通过的质量级别。"
    )
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="本版本开始对消费者可见的时间。"
    )
    superseded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="被新版本替换的时间；当前版本为空。"
    )
    effective_as_of: Mapped[date | None] = mapped_column(
        Date, nullable=True, comment="本发布所表达业务事实的有效日期。"
    )
    knowledge_cutoff: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="生成该发布时可使用证据的知识截止时间。"
    )
