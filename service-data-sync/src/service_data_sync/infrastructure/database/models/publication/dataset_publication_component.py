"""聚合发布所固定使用的分区版本清单模型。"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base


class DatasetPublicationComponent(Base):
    """固定一个聚合发布使用的各分区 `data_version`，避免读取时混用时间点。

    例如全市场视图需要同时选定多个交易所或业务日期分区；若只按“当前最新”临时查询，读取
    期间可能混进刚发布的新分区。此清单让聚合 publication 可重放、可缓存和可回滚，并且每个
    组件仍保留自身独立的质量与来源血缘。
    """

    __tablename__ = "dataset_publication_component"
    __table_args__ = {"comment": "聚合发布到组成分区版本的不可变映射。"}

    aggregate_publication_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("dataset_publication.publication_id"),
        primary_key=True,
        nullable=False,
        comment="聚合发布记录。",
    )
    component_partition_key: Mapped[str] = mapped_column(
        String(240), primary_key=True, nullable=False, comment="聚合内一个组成分区的稳定键。"
    )
    component_data_version: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False, comment="该组成分区固定采用的数据版本。"
    )
