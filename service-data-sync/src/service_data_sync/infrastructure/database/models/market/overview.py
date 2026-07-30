"""市场概览组件发布、完整包和当前指针模型。"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CHAR,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base


class MarketOverviewComponentRelease(Base):
    """冻结一个 provider-neutral 市场组件分区及其来源、方法学和质量证据。"""

    __tablename__ = "market_overview_component_release"
    __table_args__ = (
        CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'",
            name="ck_market_overview_component_hash",
        ),
        CheckConstraint(
            "quality_status = 'passed'",
            name="ck_market_overview_component_quality",
        ),
        CheckConstraint(
            "finality = 'final'",
            name="ck_market_overview_component_finality",
        ),
        UniqueConstraint(
            "dataset_code",
            "partition_key",
            "content_hash",
            name="uq_market_overview_component_content",
        ),
        Index(
            "ix_market_overview_component_dataset_date",
            "dataset_code",
            "trade_date",
            "published_at",
        ),
        {"comment": "市场概览写时组合前的不可变组件发布；载荷已通过 provider-neutral schema。"},
    )

    component_release_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, nullable=False, comment="组件发布永久 UUID。"
    )
    dataset_code: Mapped[str] = mapped_column(
        String(120), nullable=False, comment="provider-neutral canonical 数据集编码。"
    )
    partition_key: Mapped[str] = mapped_column(
        String(240), nullable=False, comment="数据集内稳定分区键。"
    )
    trade_date: Mapped[date | None] = mapped_column(
        Date, nullable=True, comment="日频组件交易日；静态目录可为空。"
    )
    data_version: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), unique=True, nullable=False, comment="组件不可变数据版本。"
    )
    content_hash: Mapped[str] = mapped_column(
        CHAR(64), nullable=False, comment="规范化组件载荷 SHA-256。"
    )
    payload_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, comment="经过严格 schema 校验的 provider-neutral 组件载荷。"
    )
    source_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, comment="adapter、真实上游、数据产品和观察时间血缘。"
    )
    methodology_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, comment="组件值的语义族与版本化方法学。"
    )
    quality_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, comment="完整性、唯一性、单位和范围质量结果。"
    )
    quality_status: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="只有 passed 组件可参与完整包。"
    )
    finality: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="市场首页只接受收盘 final 组件。"
    )
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="服务观察上游响应的带时区时间。"
    )
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="组件通过质量门并可参与组合的时间。"
    )


class MarketOverviewBundle(Base):
    """冻结同一交易日所有必需组件和首页投影，绝不保存部分包。"""

    __tablename__ = "market_overview_bundle"
    __table_args__ = (
        CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'",
            name="ck_market_overview_bundle_hash",
        ),
        CheckConstraint(
            "quality_status = 'passed'",
            name="ck_market_overview_bundle_quality",
        ),
        CheckConstraint(
            "finality = 'final'",
            name="ck_market_overview_bundle_finality",
        ),
        UniqueConstraint(
            "trade_date",
            "content_hash",
            name="uq_market_overview_bundle_content",
        ),
        Index(
            "ix_market_overview_bundle_trade_date",
            "trade_date",
            "published_at",
        ),
        {"comment": "市场首页原子可见完整包；任一必需组件失败时不会创建。"},
    )

    bundle_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, nullable=False, comment="完整包永久 UUID。"
    )
    trade_date: Mapped[date] = mapped_column(
        Date, nullable=False, comment="全部必需日频组件共同交易日。"
    )
    data_version: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), unique=True, nullable=False, comment="首页缓存与 ETag 绑定版本。"
    )
    content_hash: Mapped[str] = mapped_column(
        CHAR(64), nullable=False, comment="manifest 与首页载荷规范化摘要。"
    )
    payload_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, comment="严格公开形状的完整市场首页载荷。"
    )
    manifest_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, comment="必需组件 dataset、版本、摘要和来源绑定清单。"
    )
    quality_status: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="完整包综合质量结论。"
    )
    finality: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="全部组件共同最终态。"
    )
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="完整包原子发布完成时间。"
    )


class MarketOverviewBundleComponent(Base):
    """固定完整包引用及本次复核证据，使内容复用不伪装成未重新采集。"""

    __tablename__ = "market_overview_bundle_component"
    __table_args__ = {"comment": "完整包到不可变组件发布的多对多 manifest。"}

    bundle_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("market_overview_bundle.bundle_id", ondelete="RESTRICT"),
        primary_key=True,
        nullable=False,
        comment="完整包 UUID。",
    )
    dataset_code: Mapped[str] = mapped_column(
        String(120), primary_key=True, nullable=False, comment="组件 canonical 数据集编码。"
    )
    component_release_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey(
            "market_overview_component_release.component_release_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
        comment="完整包固定采用的组件发布。",
    )
    verified_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="该 bundle 本次成功复核组件的时间。"
    )
    verification_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        comment="本次 candidate 的观察时间、请求证据摘要和规范化摘要。",
    )


class MarketOverviewCurrentPointer(Base):
    """保存市场维度唯一当前完整包指针；切换与完整包写入处于同一事务。"""

    __tablename__ = "market_overview_current_pointer"
    __table_args__ = {"comment": "市场首页 latest 完整包的原子指针；组件失败时保持旧值。"}

    market: Mapped[str] = mapped_column(
        String(32),
        primary_key=True,
        nullable=False,
        comment="市场稳定编码，当前固定 CN-A-SSE-SZSE。",
    )
    bundle_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("market_overview_bundle.bundle_id", ondelete="RESTRICT"),
        nullable=False,
        comment="当前可见完整包。",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="当前指针最后成功切换时间。"
    )


class MarketOverviewActiveBundle(Base):
    """保存每个交易日唯一公开可见 bundle，回滚不删除任何不可变 revision。"""

    __tablename__ = "market_overview_active_bundle"
    __table_args__ = (
        UniqueConstraint(
            "bundle_id",
            name="uq_market_overview_active_bundle",
        ),
        {"comment": "按交易日解析 exact/history reader 的公开可见 bundle。"},
    )

    market: Mapped[str] = mapped_column(
        String(32), primary_key=True, nullable=False, comment="市场稳定编码。"
    )
    trade_date: Mapped[date] = mapped_column(
        Date, primary_key=True, nullable=False, comment="公开可见 bundle 的交易日。"
    )
    bundle_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("market_overview_bundle.bundle_id", ondelete="RESTRICT"),
        nullable=False,
        comment="该交易日当前公开可见的不可变 bundle。",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="交易日可见指针最后切换时间。"
    )


class MarketOverviewPointerTransition(Base):
    """不可变记录发布、回滚和前滚动作，支持真实性 freshness 与运维审计。"""

    __tablename__ = "market_overview_pointer_transition"
    __table_args__ = (
        CheckConstraint(
            "action IN ('publish', 'rollback', 'forward')",
            name="ck_market_overview_pointer_transition_action",
        ),
        Index(
            "ix_market_overview_pointer_transition_market_date",
            "market",
            "trade_date",
            "changed_at",
        ),
        {"comment": "市场 bundle 公开可见指针的不可变变更审计。"},
    )

    transition_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, nullable=False, comment="指针动作永久 UUID。"
    )
    market: Mapped[str] = mapped_column(String(32), nullable=False, comment="市场稳定编码。")
    trade_date: Mapped[date] = mapped_column(Date, nullable=False, comment="动作影响的交易日。")
    from_bundle_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("market_overview_bundle.bundle_id", ondelete="RESTRICT"),
        nullable=True,
        comment="动作前公开 bundle；首次发布为空。",
    )
    to_bundle_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("market_overview_bundle.bundle_id", ondelete="RESTRICT"),
        nullable=False,
        comment="动作后公开 bundle。",
    )
    action: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="publish、rollback 或 forward。"
    )
    reason: Mapped[str] = mapped_column(
        String(500), nullable=False, comment="不可为空的运维变更原因。"
    )
    actor_ref: Mapped[str] = mapped_column(
        String(160), nullable=False, comment="受审计的操作者或 data-operation run 引用。"
    )
    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="指针事务提交时刻。"
    )


class MarketOverviewDerivationInputPointer(Base):
    """保存仅供写时派生的日线输入，不把 seed 误暴露为完整公开 bundle。"""

    __tablename__ = "market_overview_derivation_input_pointer"
    __table_args__ = (
        CheckConstraint(
            "dataset_code IN ('sector.quote.eod.dc', 'sw.market-data')",
            name="ck_market_overview_derivation_input_dataset",
        ),
        {"comment": "近期 bootstrap 日线输入指针；公开 reader 不直接读取。"},
    )

    dataset_code: Mapped[str] = mapped_column(
        String(120), primary_key=True, nullable=False, comment="允许参与派生的日线数据集。"
    )
    trade_date: Mapped[date] = mapped_column(
        Date, primary_key=True, nullable=False, comment="日线输入交易日。"
    )
    component_release_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey(
            "market_overview_component_release.component_release_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
        comment="已通过 schema 与来源质量门的不可变输入组件。",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="派生输入指针最后切换时间。"
    )


__all__ = [
    "MarketOverviewActiveBundle",
    "MarketOverviewBundle",
    "MarketOverviewBundleComponent",
    "MarketOverviewComponentRelease",
    "MarketOverviewCurrentPointer",
    "MarketOverviewDerivationInputPointer",
    "MarketOverviewPointerTransition",
]
