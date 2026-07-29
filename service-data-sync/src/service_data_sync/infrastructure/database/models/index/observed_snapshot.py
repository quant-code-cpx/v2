"""指数目录和当前成分、权重观察的 `P0-A` 研究态隔离存储模型。

所有表都表达“本系统在何时看到什么”，而不是指数成分何时正式生效；来源缺失、代码变更和交易所
信息不足必须保持观察或隔离状态，不能自动升级为可供回测或生产读取的 `PIT` 事实。
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    CHAR,
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    desc,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base


class IndexDefinition(Base):
    """为 `P0-A` 观察建立管理人范围内的暂定指数身份，不断言代码延续或跨资产根身份。

    同一六位代码在不同管理人下并不天然相同，代码变更、合并或退役也需要官方事件才能建立链路。
    因此本表的身份只在“管理人 + 当前来源代码”范围内稳定，`status` 是观察或已确认状态，不能
    把目录缺席直接视为停编，更不能替代跨资产 `MarketEntity`。
    """

    __tablename__ = "index_definition"
    __table_args__ = (
        CheckConstraint(
            "administrator_code IN ('CSI', 'CNI')", name="ck_index_definition_administrator"
        ),
        CheckConstraint("source_index_code ~ '^[0-9]{6}$'", name="ck_index_definition_source_code"),
        CheckConstraint(
            "status IN ('observed', 'active', 'retired')", name="ck_index_definition_status"
        ),
        UniqueConstraint(
            "administrator_code", "source_index_code", name="uq_index_definition_administrator_code"
        ),
        Index("ix_index_definition_administrator_status", "administrator_code", "status"),
        {"comment": "P0-A 指数暂定身份；正式代码延续和生命周期必须由官方事件另行确认。"},
    )

    index_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, nullable=False, comment="指数观察身份永久 UUID。"
    )
    administrator_code: Mapped[str] = mapped_column(
        String(8), nullable=False, comment="中证或国证管理人代码。"
    )
    source_index_code: Mapped[str] = mapped_column(
        String(6), nullable=False, comment="当前观察到的六位来源指数代码。"
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="仅观察、已确认活动或有官方证据的退役状态。"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="暂定指数身份首次被本系统观察到的时间。"
    )


class IndexCatalogObservation(Base):
    """固定一次管理人目录观察；目录为空或截断时不据此关闭任何指数身份。

    来源批次、规范化运行、观察时间、行数和内容摘要共同证明这次目录响应，不把它误读成完整官方
    生命周期公告。每次观察独立保留，便于排查源端变更；只有质量规则确认完整后才可作为研究比较，
    仍不能因某个条目缺席自动退役 `IndexDefinition`。
    """

    __tablename__ = "index_catalog_observation"
    __table_args__ = (
        CheckConstraint(
            "administrator_code IN ('CSI', 'CNI')",
            name="ck_index_catalog_observation_administrator",
        ),
        CheckConstraint("record_count > 0", name="ck_index_catalog_observation_record_count"),
        CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'", name="ck_index_catalog_observation_content_hash"
        ),
        UniqueConstraint("source_batch_id", name="uq_index_catalog_observation_source_batch"),
        Index(
            "ix_index_catalog_observation_administrator_observed",
            "administrator_code",
            desc("observed_at"),
        ),
        {"comment": "一次完整目录的来源观察；不以缺席记录推断停编或退市。"},
    )

    catalog_observation_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, nullable=False, comment="目录观察永久 UUID。"
    )
    administrator_code: Mapped[str] = mapped_column(
        String(8), nullable=False, comment="提供该目录观察的指数管理人。"
    )
    source_batch_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("source_batch.source_batch_id", ondelete="RESTRICT"),
        nullable=False,
        comment="可回链原始目录响应的来源观察批次。",
    )
    normalization_run_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("normalization_run.normalization_run_id", ondelete="RESTRICT"),
        nullable=False,
        comment="将目录响应映射为观察条目的规范化运行。",
    )
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="系统观察到目录响应的时间。"
    )
    record_count: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="目录观察中成功规范化的指数条目数。"
    )
    content_hash: Mapped[str] = mapped_column(
        CHAR(64), nullable=False, comment="规范化目录内容的 SHA-256 摘要。"
    )


class IndexCatalogObservationItem(Base):
    """保存目录观察内的来源名称和可选元数据；名称变化不覆盖历史观察。

    名称、基日、基点和样本数是管理人目录在该时刻的声明，未验证单位或缺失字段只作为观察值保留。
    它们不构成指数方法学、正式成分规则或代码延续证明；后来更名会形成新目录项，不能回写旧快照
    或以名称相同把两个暂定指数合并。
    """

    __tablename__ = "index_catalog_observation_item"
    __table_args__ = (
        CheckConstraint(
            "constituent_count IS NULL OR constituent_count >= 0",
            name="ck_index_catalog_item_count",
        ),
        {"comment": "一次目录观察内的指数条目；来源字段保留观察语义而非正式生命周期。"},
    )

    catalog_observation_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("index_catalog_observation.catalog_observation_id", ondelete="RESTRICT"),
        primary_key=True,
        nullable=False,
        comment="所属不可变目录观察。",
    )
    index_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("index_definition.index_id", ondelete="RESTRICT"),
        primary_key=True,
        nullable=False,
        comment="目录条目对应的 P0-A 暂定指数身份。",
    )
    source_name: Mapped[str] = mapped_column(
        String(200), nullable=False, comment="本次目录观察到的来源指数简称。"
    )
    source_full_name: Mapped[str | None] = mapped_column(
        String(300), nullable=True, comment="本次目录观察到的可选来源全称。"
    )
    source_base_date: Mapped[date | None] = mapped_column(
        Date, nullable=True, comment="来源声明的基日；缺失不推断。"
    )
    source_base_value: Mapped[Decimal | None] = mapped_column(
        Numeric(24, 8), nullable=True, comment="来源声明的基点；单位未确认时仅作观察字段。"
    )
    source_published_date: Mapped[date | None] = mapped_column(
        Date, nullable=True, comment="来源声明的发布日期；不替代发布时间精度。"
    )
    constituent_count: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="来源声明的样本数；无法验证时为空。"
    )


class IndexObservedSnapshot(Base):
    """保存当前成分或权重观察，明确禁止将其作为正式有效区间或 `PIT` 输入。

    `source_as_of_date` 只有在来源明确给出时才可记录，观察到响应的时间仍由 `observed_at` 表示；
    二者都不能自动证明成分实际调入/调出时间。质量通过只说明本次研究态观察结构可用，不产生
    消费者 `publication`，也不能用于回测避免未来函数或替代正式指数发布。
    """

    __tablename__ = "index_observed_snapshot"
    __table_args__ = (
        CheckConstraint(
            "observation_kind IN ('constituent_current', 'weight_snapshot')",
            name="ck_index_observed_snapshot_kind",
        ),
        CheckConstraint("item_count > 0", name="ck_index_observed_snapshot_item_count"),
        CheckConstraint(
            "quality_status IN ('passed', 'warned', 'blocked')",
            name="ck_index_observed_snapshot_quality_status",
        ),
        CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'", name="ck_index_observed_snapshot_content_hash"
        ),
        UniqueConstraint("source_batch_id", name="uq_index_observed_snapshot_source_batch"),
        Index(
            "ix_index_observed_snapshot_index_observed",
            "index_id",
            desc("observed_at"),
        ),
        {"comment": "当前成分或权重观察；没有官方有效证据时不得用于 PIT 或正式发布。"},
    )

    snapshot_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, nullable=False, comment="指数观察快照永久 UUID。"
    )
    index_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("index_definition.index_id", ondelete="RESTRICT"),
        nullable=False,
        comment="被观察的 P0-A 暂定指数身份。",
    )
    dataset_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("canonical_dataset.dataset_id", ondelete="RESTRICT"),
        nullable=False,
        comment="成分或权重观察所属的 canonical 研究态数据集。",
    )
    source_batch_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("source_batch.source_batch_id", ondelete="RESTRICT"),
        nullable=False,
        comment="可回链原始响应的来源观察批次。",
    )
    normalization_run_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("normalization_run.normalization_run_id", ondelete="RESTRICT"),
        nullable=False,
        comment="将标准载荷转换为观察条目的规范化运行。",
    )
    observation_kind: Mapped[str] = mapped_column(
        String(24), nullable=False, comment="当前成分观察或来源日期权重快照。"
    )
    source_as_of_date: Mapped[date | None] = mapped_column(
        Date, nullable=True, comment="来源明确给出的快照日期；中证当前成分等未知时为空。"
    )
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="系统实际观察到来源响应的时间。"
    )
    item_count: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="本次观察快照中已规范化的条目数。"
    )
    quality_status: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="观察完整性与身份可解析性规则的质量结论。"
    )
    content_hash: Mapped[str] = mapped_column(
        CHAR(64), nullable=False, comment="规范化观察内容的 SHA-256 摘要。"
    )


class IndexObservedSnapshotItem(Base):
    """保存观察快照内的来源证券标识和可选权重，不猜测证券主数据身份或交易所。

    六位代码在缺少交易所时可能对应多个市场工具，来源名称也不是永久身份键；因此只有明确证据才
    填写交易所或后续关联。权重以零到一比例保存，缺失并非零权重；本表不应被拿来拼接历史成分、
    反推正式调仓日期或绕过 `PIT`/身份质量门。
    """

    __tablename__ = "index_observed_snapshot_item"
    __table_args__ = (
        CheckConstraint(
            "source_symbol ~ '^[0-9]{6}$'", name="ck_index_observed_snapshot_item_symbol"
        ),
        CheckConstraint(
            "weight_value IS NULL OR (weight_value >= 0 AND weight_value <= 1)",
            name="ck_index_observed_snapshot_item_weight",
        ),
        {"comment": "观察快照内的来源成分与权重；交易所或证券身份未知时保持空值。"},
    )

    snapshot_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("index_observed_snapshot.snapshot_id", ondelete="RESTRICT"),
        primary_key=True,
        nullable=False,
        comment="所属当前成分或权重观察快照。",
    )
    source_symbol: Mapped[str] = mapped_column(
        String(6), primary_key=True, nullable=False, comment="来源提供的六位证券代码。"
    )
    source_name: Mapped[str] = mapped_column(
        String(200), nullable=False, comment="来源提供的证券名称；不作为身份解析键。"
    )
    source_exchange: Mapped[str | None] = mapped_column(
        String(32), nullable=True, comment="来源明确提供的交易所；国证缺失时保持空值。"
    )
    source_industry: Mapped[str | None] = mapped_column(
        String(200), nullable=True, comment="来源提供的可选行业原文。"
    )
    weight_value: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 10), nullable=True, comment="权重比例零到一；成分观察或未知时为空。"
    )
    weight_kind: Mapped[str | None] = mapped_column(
        String(24), nullable=True, comment="官方收盘、观察值等来源权重口径；无权重时为空。"
    )
