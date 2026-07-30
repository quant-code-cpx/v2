"""沪深港通中心的官方日历、通道状态与原子可见完整包模型。

通道统计和活跃证券仍保存在各自的 canonical revision 表；本模块只保存它们与官方状态、
日历证据组成的不可变消费完整包。消费者永远不直接读取暂存事实，当前指针只在完整事务成功后切换。
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base


class StockConnectCalendarObservation(Base):
    """保存一份 HKEX 官方互联互通年度日历中的单日事实及文件证据。"""

    __tablename__ = "stock_connect_calendar_observation"
    __table_args__ = (
        UniqueConstraint(
            "calendar_date",
            "source_file_sha256",
            name="uq_stock_connect_calendar_source_day",
        ),
        CheckConstraint(
            "source_file_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_stock_connect_calendar_digest",
        ),
        Index("ix_stock_connect_calendar_day", "calendar_date", "observed_at"),
        {"comment": "HKEX Stock Connect 官方日历逐日观察；抓取时间不等于交易日。"},
    )

    observation_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, nullable=False, comment="日历观察 UUID。"
    )
    calendar_date: Mapped[date] = mapped_column(
        Date, nullable=False, comment="官方 CSV Date 列给出的业务日期。"
    )
    northbound_trading: Mapped[bool] = mapped_column(
        Boolean, nullable=False, comment="官方 Northbound Trading 列是否开放。"
    )
    southbound_trading: Mapped[bool] = mapped_column(
        Boolean, nullable=False, comment="官方 Southbound Trading 列是否开放。"
    )
    hong_kong_state: Mapped[str] = mapped_column(
        String(48), nullable=False, comment="官方 Hong Kong 列原始开闭市语义。"
    )
    mainland_state: Mapped[str] = mapped_column(
        String(48), nullable=False, comment="官方 Shanghai & Shenzhen 列原始开闭市语义。"
    )
    source_publication_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="官方文件发布时间或文件元数据时间。"
    )
    source_file_sha256: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="官方日历文件 SHA-256。"
    )
    source_ref: Mapped[dict[str, object]] = mapped_column(
        JSONB, nullable=False, comment="符合内部合同的 HKEX_CALENDAR 来源引用。"
    )
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="平台实际取得该文件的带时区时间。"
    )


class StockConnectChannelStatusRevision(Base):
    """保存官方日终通道状态、订单接受标志和人民币额度事实。"""

    __tablename__ = "stock_connect_channel_status_revision"
    __table_args__ = (
        CheckConstraint("channel IN ('SH', 'SZ')", name="ck_stock_connect_status_channel"),
        CheckConstraint(
            "direction IN ('NORTHBOUND', 'SOUTHBOUND')",
            name="ck_stock_connect_status_direction",
        ),
        CheckConstraint(
            "session_state IN ('OPEN', 'CLOSED', 'HALTED', 'NOT_OPEN', 'UNKNOWN')",
            name="ck_stock_connect_status_session",
        ),
        CheckConstraint(
            "session_availability IN ('DERIVED', 'REPORTED', 'SOURCE_MISSING')",
            name="ck_stock_connect_status_session_availability",
        ),
        CheckConstraint(
            "quota_state IN "
            "('SUFFICIENT', 'ACTUAL_REPORTED', 'EXHAUSTED', 'NOT_APPLICABLE', "
            "'SOURCE_MISSING')",
            name="ck_stock_connect_status_quota",
        ),
        CheckConstraint(
            "quota_balance IS NULL OR quota_balance >= 0",
            name="ck_stock_connect_status_balance",
        ),
        CheckConstraint(
            "(quota_state = 'ACTUAL_REPORTED' AND quota_balance IS NOT NULL) OR "
            "(quota_state = 'EXHAUSTED' AND quota_balance = 0) OR "
            "(quota_state IN ('SUFFICIENT', 'NOT_APPLICABLE', 'SOURCE_MISSING') "
            "AND quota_balance IS NULL)",
            name="ck_stock_connect_status_balance_state",
        ),
        UniqueConstraint(
            "trade_date",
            "channel",
            "direction",
            "content_hash",
            name="uq_stock_connect_status_content",
        ),
        Index(
            "ix_stock_connect_status_current",
            "channel",
            "direction",
            "trade_date",
            "published_at",
        ),
        {"comment": "官方日终通道状态不可变 revision；额度币种固定人民币。"},
    )

    status_revision_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, nullable=False, comment="通道状态 revision UUID。"
    )
    trade_date: Mapped[date] = mapped_column(Date, nullable=False, comment="官方业务交易日。")
    channel: Mapped[str] = mapped_column(String(8), nullable=False, comment="沪或深通道。")
    direction: Mapped[str] = mapped_column(String(16), nullable=False, comment="北向或南向方向。")
    trading_day: Mapped[bool] = mapped_column(
        Boolean, nullable=False, comment="官方日历和状态共同确认的交易日标志。"
    )
    session_state: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="来源报告或有证据派生的日终交易会话状态。"
    )
    session_availability: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment="会话状态为来源报告、可审计派生或来源缺失。",
    )
    buy_order_accepted: Mapped[bool | None] = mapped_column(
        Boolean, nullable=True, comment="来源明确披露的买单接受标志；未提供时为空。"
    )
    sell_order_accepted: Mapped[bool | None] = mapped_column(
        Boolean, nullable=True, comment="来源明确披露的卖单接受标志；未提供时为空。"
    )
    quota_state: Mapped[str] = mapped_column(
        String(24), nullable=False, comment="额度实际值、充足、耗尽或来源缺失状态。"
    )
    quota_balance: Mapped[Decimal | None] = mapped_column(
        Numeric(38, 6), nullable=True, comment="来源报告的剩余额度，基础单位人民币。"
    )
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="状态消息时间或平台确认历史缺源的时间。"
    )
    source_ref: Mapped[dict[str, object] | None] = mapped_column(
        JSONB, nullable=True, comment="实际状态交付来源；历史缺源状态可为空。"
    )
    content_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="状态业务内容 SHA-256。"
    )
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="状态 revision 写入时间。"
    )


class StockConnectBundlePublication(Base):
    """保存一个通道方向一天的不可变消费完整包和当前可见指针。"""

    __tablename__ = "stock_connect_bundle_publication"
    __table_args__ = (
        CheckConstraint("channel IN ('SH', 'SZ')", name="ck_stock_connect_bundle_channel"),
        CheckConstraint(
            "direction IN ('NORTHBOUND', 'SOUTHBOUND')",
            name="ck_stock_connect_bundle_direction",
        ),
        CheckConstraint(
            "quality_status IN ('APPROVED', 'APPROVED_WITH_WARNINGS')",
            name="ck_stock_connect_bundle_quality",
        ),
        CheckConstraint(
            "active_security_count BETWEEN 0 AND 10",
            name="ck_stock_connect_bundle_active_count",
        ),
        CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'",
            name="ck_stock_connect_bundle_digest",
        ),
        UniqueConstraint("data_version", name="uq_stock_connect_bundle_data_version"),
        UniqueConstraint(
            "trade_date",
            "channel",
            "direction",
            "content_hash",
            name="uq_stock_connect_bundle_content",
        ),
        Index(
            "uq_stock_connect_bundle_current",
            "trade_date",
            "channel",
            "direction",
            unique=True,
            postgresql_where=text("superseded_at IS NULL"),
        ),
        Index(
            "ix_stock_connect_bundle_latest",
            "channel",
            "direction",
            "trade_date",
            "published_at",
        ),
        {"comment": "互联互通消费完整包；只有该表当前行可被内部 API 读取。"},
    )

    bundle_release_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, nullable=False, comment="完整包发布 UUID。"
    )
    data_version: Mapped[str] = mapped_column(
        String(160), nullable=False, comment="响应和游标绑定的不可变数据版本。"
    )
    trade_date: Mapped[date] = mapped_column(Date, nullable=False, comment="完整包交易日。")
    channel: Mapped[str] = mapped_column(String(8), nullable=False, comment="沪或深通道。")
    direction: Mapped[str] = mapped_column(String(16), nullable=False, comment="北向或南向方向。")
    market_release_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("dataset_release.release_id", ondelete="RESTRICT"),
        nullable=False,
        comment="本包固定的市场统计 immutable release。",
    )
    active_release_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("dataset_release.release_id", ondelete="RESTRICT"),
        nullable=True,
        comment="本包固定的活跃榜 release；零成交合法空榜时为空。",
    )
    status_revision_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("stock_connect_channel_status_revision.status_revision_id"),
        nullable=False,
        comment="本包固定的日终通道状态 revision。",
    )
    calendar_observation_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("stock_connect_calendar_observation.observation_id"),
        nullable=False,
        comment="确认该方向交易日的 HKEX 官方日历观察。",
    )
    summary_json: Mapped[dict[str, object]] = mapped_column(
        JSONB, nullable=False, comment="符合内部合同的 ChannelSummary 投影。"
    )
    active_securities_json: Mapped[list[dict[str, object]]] = mapped_column(
        JSONB, nullable=False, comment="只包含官方活跃榜证券及可审计派生净额。"
    )
    quality_status: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="批准或带明确警告批准状态。"
    )
    quality_issues: Mapped[list[dict[str, str]]] = mapped_column(
        JSONB, nullable=False, comment="受合同枚举约束的非阻断质量问题。"
    )
    source_refs: Mapped[list[dict[str, object]]] = mapped_column(
        JSONB, nullable=False, comment="一至五条实际官方来源引用。"
    )
    content_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="完整包业务内容 SHA-256。"
    )
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="完整包原子变为可见的时间。"
    )
    superseded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="被更正完整包替代的时间；当前为空。"
    )
    active_security_count: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="官方活跃榜行数，范围零至十。"
    )


class StockConnectOverviewPublication(Base):
    """保存同一交易日请求所选非空通道子集均齐备后的原子总览 publication。"""

    __tablename__ = "stock_connect_overview_publication"
    __table_args__ = (
        CheckConstraint(
            "quality_status IN ('APPROVED', 'APPROVED_WITH_WARNINGS')",
            name="ck_stock_connect_overview_quality",
        ),
        CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'",
            name="ck_stock_connect_overview_digest",
        ),
        UniqueConstraint("data_version", name="uq_stock_connect_overview_data_version"),
        UniqueConstraint(
            "trade_date",
            "channel_set",
            "content_hash",
            name="uq_stock_connect_overview_content",
        ),
        Index(
            "uq_stock_connect_overview_current",
            "trade_date",
            "channel_set",
            unique=True,
            postgresql_where=text("superseded_at IS NULL"),
        ),
        Index("ix_stock_connect_overview_latest", "trade_date", "published_at"),
        {"comment": "请求所选通道子集同日齐备后的不可变总览；失败通道不阻塞健康子集。"},
    )

    overview_release_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, nullable=False, comment="总览完整包 UUID。"
    )
    data_version: Mapped[str] = mapped_column(
        String(160), nullable=False, comment="总览响应及其趋势选择绑定的数据版本。"
    )
    trade_date: Mapped[date] = mapped_column(Date, nullable=False, comment="所选通道共同交易日。")
    channel_set: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="按字典序连接的请求通道集合稳定键。"
    )
    component_bundle_ids: Mapped[dict[str, str]] = mapped_column(
        JSONB, nullable=False, comment="所选通道到不可变通道 bundle UUID 的映射。"
    )
    quality_status: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="所选组件质量状态合并后的批准结论。"
    )
    quality_issues: Mapped[list[dict[str, str]]] = mapped_column(
        JSONB, nullable=False, comment="所选通道质量问题稳定去重后的集合。"
    )
    source_refs: Mapped[list[dict[str, object]]] = mapped_column(
        JSONB, nullable=False, comment="四通道实际来源引用，最多十二条。"
    )
    content_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="所选通道 bundle ID 和来源质量的 SHA-256。"
    )
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="总览原子变为可见的时间。"
    )
    superseded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="被同日更正总览替代的时间。"
    )


class StockConnectBundleRollbackAudit(Base):
    """保存一次受 fencing 保护的完整包回滚及其 overview 指针变更证据。"""

    __tablename__ = "stock_connect_bundle_rollback_audit"
    __table_args__ = (
        CheckConstraint("channel IN ('SH', 'SZ')", name="ck_stock_connect_rollback_channel"),
        CheckConstraint(
            "direction IN ('NORTHBOUND', 'SOUTHBOUND')",
            name="ck_stock_connect_rollback_direction",
        ),
        CheckConstraint(
            "from_bundle_release_id <> to_bundle_release_id",
            name="ck_stock_connect_rollback_distinct_bundles",
        ),
        CheckConstraint("fencing_token > 0", name="ck_stock_connect_rollback_fencing"),
        CheckConstraint(
            "length(btrim(actor_ref)) BETWEEN 1 AND 128",
            name="ck_stock_connect_rollback_actor",
        ),
        CheckConstraint(
            "length(btrim(reason)) BETWEEN 8 AND 2000",
            name="ck_stock_connect_rollback_reason",
        ),
        CheckConstraint(
            "jsonb_typeof(from_overview_release_ids) = 'object' "
            "AND jsonb_typeof(to_overview_release_ids) = 'object'",
            name="ck_stock_connect_rollback_overview_maps",
        ),
        UniqueConstraint(
            "operation_run_id",
            name="uq_stock_connect_rollback_operation_run",
        ),
        Index(
            "ix_stock_connect_rollback_scope_time",
            "trade_date",
            "channel",
            "direction",
            "rolled_back_at",
        ),
        {"comment": ("互联互通完整包 fenced 回滚审计；行由数据库触发器禁止修改、删除和截断。")},
    )

    rollback_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, nullable=False, comment="回滚审计永久 UUID。"
    )
    operation_run_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("data_operation_run.run_id", ondelete="RESTRICT"),
        nullable=False,
        comment="执行回滚的权威 data operation child run。",
    )
    fencing_token: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="提交回滚事务时验证通过的全局单调 fencing token。"
    )
    trade_date: Mapped[date] = mapped_column(Date, nullable=False, comment="被回滚完整包交易日。")
    channel: Mapped[str] = mapped_column(String(8), nullable=False, comment="被回滚沪或深通道。")
    direction: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="被回滚北向或南向方向。"
    )
    from_bundle_release_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("stock_connect_bundle_publication.bundle_release_id", ondelete="RESTRICT"),
        nullable=False,
        comment="回滚前当前可见完整包 UUID。",
    )
    to_bundle_release_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("stock_connect_bundle_publication.bundle_release_id", ondelete="RESTRICT"),
        nullable=False,
        comment="回滚后重新激活的历史完整包 UUID。",
    )
    from_overview_release_ids: Mapped[dict[str, str]] = mapped_column(
        JSONB,
        nullable=False,
        comment="回滚前同日全部当前 channel-set 到 overview UUID 的映射。",
    )
    to_overview_release_ids: Mapped[dict[str, str]] = mapped_column(
        JSONB,
        nullable=False,
        comment="回滚后同日全部当前 channel-set 到 overview UUID 的映射。",
    )
    actor_ref: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="与权威 command 完全一致的不透明操作主体。"
    )
    reason: Mapped[str] = mapped_column(
        Text, nullable=False, comment="与权威 command 完全一致的强制回滚原因。"
    )
    request_id: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="与权威 command 完全一致的跨服务请求标识。"
    )
    rolled_back_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="完整包和 overview 指针原子切换时间。"
    )
