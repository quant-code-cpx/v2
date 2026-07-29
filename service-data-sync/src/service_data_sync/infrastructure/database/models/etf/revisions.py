"""ETF 强类型 revision 模型；价格、NAV、份额和状态绝不静默混算。"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    CHAR,
    Boolean,
    CheckConstraint,
    Computed,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    desc,
)
from sqlalchemy.dialects.postgresql import DATERANGE, TSTZRANGE, ExcludeConstraint, Range
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base


class EtfProfileVersion(Base):
    """保存 ETF 上市工具的产品属性双时间版本，基金、份额和 listing 保持分层。"""

    __tablename__ = "etf_profile_version"
    __table_args__ = (
        CheckConstraint(
            "effective_to IS NULL OR effective_to > effective_from",
            name="ck_etf_profile_effective_range",
        ),
        CheckConstraint(
            "known_to IS NULL OR known_to > known_from", name="ck_etf_profile_knowledge_range"
        ),
        CheckConstraint(
            "source_time_precision IN ('EXACT', 'DATE_ONLY', 'UNKNOWN')",
            name="ck_etf_profile_source_time_precision",
        ),
        ExcludeConstraint(
            ("etf_id", "="),
            ("effective_range", "&&"),
            ("knowledge_range", "&&"),
            using="gist",
            name="ex_etf_profile_time",
        ),
        Index("ix_etf_profile_type_status", "etf_type", "listing_status", desc("effective_from")),
        {"comment": "ETF 上市工具产品属性双时间版本；不以行情缺席推断上市或终止状态。"},
    )

    profile_version_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        nullable=False,
        comment="ETF 产品资料版本永久 UUID。",
    )
    etf_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("etf_listing.instrument_id", ondelete="RESTRICT"),
        nullable=False,
        comment="ETF 上市工具 UUID。",
    )
    etf_type: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="来源确认的 ETF 产品类型。"
    )
    management_mode: Mapped[str] = mapped_column(
        String(24), nullable=False, comment="被动、主动或其他管理方式。"
    )
    manager_name: Mapped[str | None] = mapped_column(
        String(200), nullable=True, comment="来源提供的管理人原文；未知时为空。"
    )
    custodian_name: Mapped[str | None] = mapped_column(
        String(200), nullable=True, comment="来源提供的托管人原文；未知时为空。"
    )
    established_on: Mapped[date | None] = mapped_column(
        Date, nullable=True, comment="基金成立日期；缺失不由 listing 日期代替。"
    )
    listed_on: Mapped[date | None] = mapped_column(
        Date, nullable=True, comment="交易所上市日期；缺失时为空。"
    )
    delisted_on: Mapped[date | None] = mapped_column(
        Date, nullable=True, comment="交易所摘牌日期；未确认时为空。"
    )
    quote_currency: Mapped[str] = mapped_column(
        CHAR(3), nullable=False, comment="二级市场报价币种 ISO 代码。"
    )
    nav_currency: Mapped[str] = mapped_column(
        CHAR(3), nullable=False, comment="基金 NAV 币种 ISO 代码。"
    )
    listing_status: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="上市工具当前版本状态，不代替申购或赎回状态。"
    )
    effective_from: Mapped[date] = mapped_column(
        Date, nullable=False, comment="产品资料开始适用的业务日期。"
    )
    effective_to: Mapped[date | None] = mapped_column(
        Date, nullable=True, comment="产品资料停止适用的业务日期；开区间为空。"
    )
    known_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="系统开始采用产品资料版本的时间。"
    )
    known_to: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="系统停止采用产品资料版本的时间。"
    )
    source_time_precision: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="来源发布日期或时间的精度。"
    )
    methodology_version_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey(
            "methodology_version.methodology_version_id",
            name="fk_etf_profile_methodology",
            ondelete="RESTRICT",
        ),
        nullable=True,
        comment="产品资料字段口径方法学版本；迁移前历史空值不得补造。",
    )
    release_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey(
            "dataset_release.release_id", name="fk_etf_profile_release", ondelete="RESTRICT"
        ),
        nullable=True,
        comment="包含该资料版本的 immutable release；迁移前历史空值不得推测 data version。",
    )
    source_batch_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("source_batch.source_batch_id", ondelete="RESTRICT"),
        nullable=False,
        comment="支撑产品资料版本的来源批次。",
    )
    content_hash: Mapped[str] = mapped_column(
        CHAR(64), nullable=False, comment="产品资料业务内容 SHA-256 摘要。"
    )
    effective_range: Mapped[Range[date] | None] = mapped_column(
        DATERANGE,
        Computed("daterange(effective_from, effective_to, '[)')", persisted=True),
        nullable=True,
        comment="由有效日期生成的半开范围。",
    )
    knowledge_range: Mapped[Range[datetime] | None] = mapped_column(
        TSTZRANGE,
        Computed("tstzrange(known_from, known_to, '[)')", persisted=True),
        nullable=True,
        comment="由知识时间生成的半开范围。",
    )


class EtfTrackingRelationVersion(Base):
    """保存 ETF 与跟踪对象的关系版本；无法解析的对象保留原文而不强行映射。"""

    __tablename__ = "etf_tracking_relation_version"
    __table_args__ = (
        CheckConstraint(
            "(target_instrument_id IS NULL) <> (target_name_raw IS NULL)",
            name="ck_etf_tracking_target",
        ),
        CheckConstraint(
            "effective_to IS NULL OR effective_to > effective_from",
            name="ck_etf_tracking_effective_range",
        ),
        CheckConstraint(
            "known_to IS NULL OR known_to > known_from", name="ck_etf_tracking_knowledge_range"
        ),
        ExcludeConstraint(
            ("etf_id", "="),
            ("relation_kind", "="),
            ("effective_range", "&&"),
            ("knowledge_range", "&&"),
            using="gist",
            name="ex_etf_tracking_relation_time",
        ),
        Index("ix_etf_tracking_target", "target_instrument_id", desc("effective_from")),
        {"comment": "ETF 跟踪关系双时间版本；目标身份未知时保留来源名称且不进入隐式匹配。"},
    )

    relation_version_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, nullable=False, comment="ETF 跟踪关系版本 UUID。"
    )
    etf_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("etf_listing.instrument_id", ondelete="RESTRICT"),
        nullable=False,
        comment="ETF 上市工具 UUID。",
    )
    target_instrument_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("market_entity.entity_id", ondelete="RESTRICT"),
        nullable=True,
        comment="已准确解析的跟踪目标实体 UUID。",
    )
    target_name_raw: Mapped[str | None] = mapped_column(
        String(300), nullable=True, comment="未解析时保留的目标来源名称。"
    )
    relation_kind: Mapped[str] = mapped_column(
        String(24), nullable=False, comment="指数跟踪、商品跟踪或其他明确关系类别。"
    )
    replication_method: Mapped[str | None] = mapped_column(
        String(24), nullable=True, comment="完全复制、抽样或其他来源声明复制方式。"
    )
    methodology_version_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("methodology_version.methodology_version_id", ondelete="RESTRICT"),
        nullable=False,
        comment="解释关系映射的冻结方法学版本。",
    )
    effective_from: Mapped[date] = mapped_column(Date, nullable=False, comment="关系开始适用日期。")
    effective_to: Mapped[date | None] = mapped_column(
        Date, nullable=True, comment="关系结束适用日期；开区间为空。"
    )
    known_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="系统开始采用关系版本的时间。"
    )
    known_to: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="系统停止采用关系版本的时间。"
    )
    source_batch_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("source_batch.source_batch_id", ondelete="RESTRICT"),
        nullable=False,
        comment="支撑关系版本的来源批次。",
    )
    content_hash: Mapped[str] = mapped_column(
        CHAR(64), nullable=False, comment="关系业务内容 SHA-256 摘要。"
    )
    effective_range: Mapped[Range[date] | None] = mapped_column(
        DATERANGE,
        Computed("daterange(effective_from, effective_to, '[)')", persisted=True),
        nullable=True,
        comment="由有效日期生成的半开范围。",
    )
    knowledge_range: Mapped[Range[datetime] | None] = mapped_column(
        TSTZRANGE,
        Computed("tstzrange(known_from, known_to, '[)')", persisted=True),
        nullable=True,
        comment="由知识时间生成的半开范围。",
    )


class EtfDailyBarRevision(Base):
    """保存 ETF 未复权日线 revision，状态或行动不会原地修正原始交易价格。"""

    __tablename__ = "etf_daily_bar_revision"
    __table_args__ = (
        CheckConstraint("revision_no > 0", name="ck_etf_daily_bar_revision_no"),
        CheckConstraint(
            "open_price >= 0 AND high_price >= 0 AND low_price >= 0 AND close_price >= 0",
            name="ck_etf_daily_bar_prices",
        ),
        CheckConstraint(
            "low_price <= LEAST(open_price, close_price) "
            "AND high_price >= GREATEST(open_price, close_price)",
            name="ck_etf_daily_bar_ohlc",
        ),
        CheckConstraint(
            "volume_value >= 0 AND amount_value >= 0", name="ck_etf_daily_bar_non_negative"
        ),
        UniqueConstraint(
            "trade_date",
            "etf_id",
            "methodology_version_id",
            "revision_no",
            name="uq_etf_daily_bar_revision",
        ),
        Index("ix_etf_daily_bar_asof", "etf_id", desc("trade_date"), desc("known_from")),
        {
            "postgresql_partition_by": "RANGE (trade_date)",
            "comment": "ETF 未复权日行情 revision 父表；按交易日年度物理分区。",
        },
    )

    trade_date: Mapped[date] = mapped_column(
        Date, primary_key=True, nullable=False, comment="场所交易日。"
    )
    row_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, nullable=False, comment="日行情 revision 行 UUID。"
    )
    etf_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("etf_listing.instrument_id", ondelete="RESTRICT"),
        nullable=False,
        comment="ETF 上市工具 UUID。",
    )
    methodology_version_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("methodology_version.methodology_version_id", ondelete="RESTRICT"),
        nullable=False,
        comment="来源行情方法学版本 UUID。",
    )
    release_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("dataset_release.release_id", ondelete="RESTRICT"),
        nullable=False,
        comment="包含本行的不可变 release UUID。",
    )
    revision_no: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="同一 ETF 和交易日的递增内容修订号。"
    )
    open_price: Mapped[Decimal] = mapped_column(
        Numeric(24, 8), nullable=False, comment="来源直接提供的开盘价。"
    )
    high_price: Mapped[Decimal] = mapped_column(
        Numeric(24, 8), nullable=False, comment="来源直接提供的最高价。"
    )
    low_price: Mapped[Decimal] = mapped_column(
        Numeric(24, 8), nullable=False, comment="来源直接提供的最低价。"
    )
    close_price: Mapped[Decimal] = mapped_column(
        Numeric(24, 8), nullable=False, comment="来源直接提供的收盘价。"
    )
    volume_value: Mapped[Decimal] = mapped_column(
        Numeric(28, 8), nullable=False, comment="成交量原始标准化数值。"
    )
    volume_unit: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="成交量单位，股和手不可混用。"
    )
    amount_value: Mapped[Decimal] = mapped_column(
        Numeric(24, 4), nullable=False, comment="成交额标准化数值。"
    )
    currency: Mapped[str] = mapped_column(
        CHAR(3), nullable=False, comment="成交额与报价币种 ISO 代码。"
    )
    trade_status: Mapped[str | None] = mapped_column(
        String(24), nullable=True, comment="来源明确的交易状态；缺失不从成交量推断。"
    )
    source_published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="可验证来源发布时间；无证据时为空。"
    )
    public_usable_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="市场 PIT 最早可安全使用时刻。"
    )
    availability_basis: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="精确、日期保守或仅观察的可见性依据。"
    )
    known_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="系统开始知道本行情 revision 的时间。"
    )
    known_to: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="系统停止采用本行情 revision 的时间。"
    )
    source_batch_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("source_batch.source_batch_id", ondelete="RESTRICT"),
        nullable=False,
        comment="支撑该行情 revision 的来源批次。",
    )
    content_hash: Mapped[str] = mapped_column(
        CHAR(64), nullable=False, comment="日行情业务内容 SHA-256 摘要。"
    )
    quality_status: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="行质量结论，不以发布状态代替。"
    )


class EtfNavRevision(Base):
    """保存 ETF 单位、累计 NAV 或 IOPV revision，盘中 IOPV 与日终 NAV 分 dataset 发布。"""

    __tablename__ = "etf_nav_revision"
    __table_args__ = (
        CheckConstraint("revision_no > 0", name="ck_etf_nav_revision_no"),
        CheckConstraint("nav_value > 0", name="ck_etf_nav_positive"),
        CheckConstraint("nav_kind IN ('UNIT', 'ACCUMULATED', 'IOPV')", name="ck_etf_nav_kind"),
        UniqueConstraint(
            "nav_date",
            "etf_id",
            "nav_kind",
            "methodology_version_id",
            "revision_no",
            name="uq_etf_nav_revision",
        ),
        Index("ix_etf_nav_asof", "etf_id", desc("nav_date"), desc("known_from")),
        {
            "postgresql_partition_by": "RANGE (nav_date)",
            "comment": "ETF NAV revision 父表；日终 NAV 与实时 IOPV 以方法学和数据集隔离。",
        },
    )

    nav_date: Mapped[date] = mapped_column(
        Date, primary_key=True, nullable=False, comment="净值或 IOPV 所属估值日期。"
    )
    row_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, nullable=False, comment="净值 revision 行 UUID。"
    )
    etf_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("etf_listing.instrument_id", ondelete="RESTRICT"),
        nullable=False,
        comment="ETF 上市工具 UUID。",
    )
    nav_kind: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="单位 NAV、累计 NAV 或 IOPV 类型。"
    )
    nav_value: Mapped[Decimal] = mapped_column(
        Numeric(24, 8), nullable=False, comment="来源确认的净值数值。"
    )
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False, comment="净值币种 ISO 代码。")
    finality: Mapped[str] = mapped_column(
        String(24), nullable=False, comment="临时、最终或来源未知终态声明。"
    )
    null_reason: Mapped[str | None] = mapped_column(
        String(32), nullable=True, comment="无数值时的显式原因；当前正值 NAV 行为空。"
    )
    methodology_version_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("methodology_version.methodology_version_id", ondelete="RESTRICT"),
        nullable=False,
        comment="净值口径方法学版本 UUID。",
    )
    release_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("dataset_release.release_id", ondelete="RESTRICT"),
        nullable=False,
        comment="包含本行的不可变 release UUID。",
    )
    revision_no: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="同一 NAV 逻辑事实的递增修订号。"
    )
    source_published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="可验证来源发布时间；缺失时为空。"
    )
    public_usable_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="市场 PIT 最早安全使用时刻。"
    )
    availability_basis: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="可见性依据；日期精度必须保守处理。"
    )
    known_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="系统开始知道本净值 revision 的时间。"
    )
    known_to: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="系统停止采用本净值 revision 的时间。"
    )
    source_batch_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("source_batch.source_batch_id", ondelete="RESTRICT"),
        nullable=False,
        comment="支撑本净值 revision 的来源批次。",
    )
    content_hash: Mapped[str] = mapped_column(
        CHAR(64), nullable=False, comment="净值业务内容 SHA-256 摘要。"
    )
    quality_status: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="净值行质量结论。"
    )


class EtfShareRevision(Base):
    """保存 ETF 份额存量 revision；不把价格乘份额得到的规模写回来源事实。"""

    __tablename__ = "etf_share_revision"
    __table_args__ = (
        CheckConstraint("revision_no > 0", name="ck_etf_share_revision_no"),
        CheckConstraint("shares >= 0", name="ck_etf_share_non_negative"),
        UniqueConstraint(
            "stat_date",
            "etf_id",
            "methodology_version_id",
            "revision_no",
            name="uq_etf_share_revision",
        ),
        Index("ix_etf_share_asof", "etf_id", desc("stat_date"), desc("known_from")),
        {
            "postgresql_partition_by": "RANGE (stat_date)",
            "comment": "ETF 份额存量 revision 父表；按统计日期年度物理分区。",
        },
    )

    stat_date: Mapped[date] = mapped_column(
        Date, primary_key=True, nullable=False, comment="份额统计日期。"
    )
    row_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, nullable=False, comment="份额 revision 行 UUID。"
    )
    etf_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("etf_listing.instrument_id", ondelete="RESTRICT"),
        nullable=False,
        comment="ETF 上市工具 UUID。",
    )
    shares: Mapped[Decimal] = mapped_column(
        Numeric(30, 6), nullable=False, comment="标准化后的基金份额数。"
    )
    quantity_unit: Mapped[str] = mapped_column(String(16), nullable=False, comment="份额数量单位。")
    scale_factor: Mapped[Decimal] = mapped_column(
        Numeric(18, 8), nullable=False, comment="从来源单位换算到标准份额的缩放因子。"
    )
    is_post_clearing: Mapped[bool] = mapped_column(
        Boolean, nullable=False, comment="是否为清算后份额口径。"
    )
    methodology_version_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("methodology_version.methodology_version_id", ondelete="RESTRICT"),
        nullable=False,
        comment="份额单位与时间口径方法学版本 UUID。",
    )
    release_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("dataset_release.release_id", ondelete="RESTRICT"),
        nullable=False,
        comment="包含本行的不可变 release UUID。",
    )
    revision_no: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="同一份额逻辑事实的递增修订号。"
    )
    source_published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="可验证来源发布时间；缺失时为空。"
    )
    public_usable_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="市场 PIT 最早安全使用时刻。"
    )
    availability_basis: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="份额可见性依据。"
    )
    known_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="系统开始知道份额 revision 的时间。"
    )
    known_to: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="系统停止采用份额 revision 的时间。"
    )
    source_batch_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("source_batch.source_batch_id", ondelete="RESTRICT"),
        nullable=False,
        comment="支撑份额 revision 的来源批次。",
    )
    content_hash: Mapped[str] = mapped_column(
        CHAR(64), nullable=False, comment="份额业务内容 SHA-256 摘要。"
    )
    quality_status: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="份额行质量结论。"
    )


class EtfStatusRevision(Base):
    """保存二级交易、申购和赎回状态的双时间版本，三个状态维度绝不互相替代。"""

    __tablename__ = "etf_status_revision"
    __table_args__ = (
        CheckConstraint(
            "status_dimension IN ('TRADING', 'SUBSCRIPTION', 'REDEMPTION')",
            name="ck_etf_status_dimension",
        ),
        CheckConstraint(
            "effective_to IS NULL OR effective_to > effective_from",
            name="ck_etf_status_effective_range",
        ),
        CheckConstraint(
            "known_to IS NULL OR known_to > known_from", name="ck_etf_status_knowledge_range"
        ),
        ExcludeConstraint(
            ("etf_id", "="),
            ("status_dimension", "="),
            ("effective_range", "&&"),
            ("knowledge_range", "&&"),
            using="gist",
            name="ex_etf_status_time",
        ),
        Index(
            "ix_etf_status_asof",
            "etf_id",
            "status_dimension",
            desc("effective_from"),
            desc("known_from"),
        ),
        {"comment": "ETF 市场状态双时间版本；停牌不自动意味着申购或赎回暂停。"},
    )

    status_revision_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, nullable=False, comment="ETF 状态 revision UUID。"
    )
    etf_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("etf_listing.instrument_id", ondelete="RESTRICT"),
        nullable=False,
        comment="ETF 上市工具 UUID。",
    )
    status_dimension: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="交易、申购或赎回状态维度。"
    )
    status_code: Mapped[str] = mapped_column(
        String(24), nullable=False, comment="来源确认的该维度状态码。"
    )
    reason: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="状态变化原因原文或受控说明。"
    )
    effective_from: Mapped[date] = mapped_column(Date, nullable=False, comment="状态开始适用日期。")
    effective_to: Mapped[date | None] = mapped_column(
        Date, nullable=True, comment="状态结束适用日期；开区间为空。"
    )
    known_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="系统开始采用状态 revision 的时间。"
    )
    known_to: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="系统停止采用状态 revision 的时间。"
    )
    methodology_version_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("methodology_version.methodology_version_id", ondelete="RESTRICT"),
        nullable=False,
        comment="状态解释方法学版本 UUID。",
    )
    release_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("dataset_release.release_id", ondelete="RESTRICT"),
        nullable=False,
        comment="包含本状态的不可变 release UUID。",
    )
    revision_no: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="同一状态逻辑事实的递增修订号。"
    )
    source_batch_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("source_batch.source_batch_id", ondelete="RESTRICT"),
        nullable=False,
        comment="支撑状态 revision 的来源批次。",
    )
    content_hash: Mapped[str] = mapped_column(
        CHAR(64), nullable=False, comment="状态业务内容 SHA-256 摘要。"
    )
    quality_status: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="状态行质量结论。"
    )
    effective_range: Mapped[Range[date] | None] = mapped_column(
        DATERANGE,
        Computed("daterange(effective_from, effective_to, '[)')", persisted=True),
        nullable=True,
        comment="由有效日期生成的半开范围。",
    )
    knowledge_range: Mapped[Range[datetime] | None] = mapped_column(
        TSTZRANGE,
        Computed("tstzrange(known_from, known_to, '[)')", persisted=True),
        nullable=True,
        comment="由知识时间生成的半开范围。",
    )


class EtfActionVersion(Base):
    """保存基金分红、拆分和合并等行动版本，行动只作为复权和份额对账输入。"""

    __tablename__ = "etf_action_version"
    __table_args__ = (
        CheckConstraint(
            "action_type IN ('CASH_DISTRIBUTION', 'SPLIT', 'CONSOLIDATION', 'OTHER')",
            name="ck_etf_action_type",
        ),
        CheckConstraint("revision_no > 0", name="ck_etf_action_revision_no"),
        UniqueConstraint(
            "etf_id", "source_event_key", "revision_no", name="uq_etf_action_source_revision"
        ),
        {"comment": "ETF 基金行动版本；不直接覆盖原始日线或份额事实。"},
    )

    action_version_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, nullable=False, comment="ETF 行动版本 UUID。"
    )
    etf_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("etf_listing.instrument_id", ondelete="RESTRICT"),
        nullable=False,
        comment="ETF 上市工具 UUID。",
    )
    source_event_key: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="来源提供的稳定行动事件标识。"
    )
    action_type: Mapped[str] = mapped_column(
        String(24), nullable=False, comment="现金分配、拆分、合并或其他行动类型。"
    )
    announcement_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="官方公告时间；仅日期或未知时为空。"
    )
    record_date: Mapped[date | None] = mapped_column(
        Date, nullable=True, comment="权益登记日期；缺失时为空。"
    )
    ex_date: Mapped[date | None] = mapped_column(
        Date, nullable=True, comment="除权除息日期；缺失时为空。"
    )
    payment_date: Mapped[date | None] = mapped_column(
        Date, nullable=True, comment="现金支付日期；缺失时为空。"
    )
    conversion_date: Mapped[date | None] = mapped_column(
        Date, nullable=True, comment="拆分或合并转换日期；缺失时为空。"
    )
    cash_per_share: Mapped[Decimal | None] = mapped_column(
        Numeric(24, 10), nullable=True, comment="每份现金分配金额；非现金行动为空。"
    )
    conversion_ratio: Mapped[Decimal | None] = mapped_column(
        Numeric(24, 12), nullable=True, comment="新份额除以旧份额的转换比例；非转换行动为空。"
    )
    currency: Mapped[str | None] = mapped_column(
        CHAR(3), nullable=True, comment="现金分配币种；无现金金额时为空。"
    )
    methodology_version_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("methodology_version.methodology_version_id", ondelete="RESTRICT"),
        nullable=False,
        comment="行动字段与单位方法学版本 UUID。",
    )
    release_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("dataset_release.release_id", ondelete="RESTRICT"),
        nullable=False,
        comment="包含本行动的不可变 release UUID。",
    )
    revision_no: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="同一行动逻辑事实的递增修订号。"
    )
    known_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="系统开始知道行动版本的时间。"
    )
    known_to: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="系统停止采用行动版本的时间。"
    )
    source_batch_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("source_batch.source_batch_id", ondelete="RESTRICT"),
        nullable=False,
        comment="支撑行动版本的来源批次。",
    )
    content_hash: Mapped[str] = mapped_column(
        CHAR(64), nullable=False, comment="行动业务内容 SHA-256 摘要。"
    )
    quality_status: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="行动行质量结论。"
    )


class EtfPremiumRevision(Base):
    """保存收盘价与单位 NAV 的派生折溢价，输入版本变化必须产生新 revision。"""

    __tablename__ = "etf_premium_revision"
    __table_args__ = (
        CheckConstraint("revision_no > 0", name="ck_etf_premium_revision_no"),
        CheckConstraint(
            "comparability_status IN ('COMPARABLE', 'STALE_UNDERLYING', "
            "'CALENDAR_MISMATCH', 'BASIS_MISMATCH', 'MISSING_INPUT')",
            name="ck_etf_premium_comparability",
        ),
        CheckConstraint(
            "(comparability_status = 'COMPARABLE' AND premium_ratio IS NOT NULL) "
            "OR (comparability_status <> 'COMPARABLE' AND premium_ratio IS NULL)",
            name="ck_etf_premium_value_comparability",
        ),
        UniqueConstraint(
            "trade_date",
            "etf_id",
            "methodology_version_id",
            "revision_no",
            name="uq_etf_premium_revision",
        ),
        Index("ix_etf_premium_asof", "etf_id", desc("trade_date"), desc("known_from")),
        {
            "postgresql_partition_by": "RANGE (trade_date)",
            "comment": "ETF 收盘折溢价派生 revision 父表；只在可比较时暴露数值。",
        },
    )

    trade_date: Mapped[date] = mapped_column(
        Date, primary_key=True, nullable=False, comment="价格与 NAV 对齐的交易日期。"
    )
    row_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, nullable=False, comment="折溢价 revision 行 UUID。"
    )
    etf_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("etf_listing.instrument_id", ondelete="RESTRICT"),
        nullable=False,
        comment="ETF 上市工具 UUID。",
    )
    price_release_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("dataset_release.release_id", ondelete="RESTRICT"),
        nullable=False,
        comment="收盘价格输入 release UUID。",
    )
    nav_release_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("dataset_release.release_id", ondelete="RESTRICT"),
        nullable=False,
        comment="单位 NAV 输入 release UUID。",
    )
    premium_ratio: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 10), nullable=True, comment="可比较时的折溢价比例；不可比较时为空。"
    )
    comparability_status: Mapped[str] = mapped_column(
        String(24), nullable=False, comment="输入时间、日历和口径可比性状态。"
    )
    comparability_reason: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="不可比较或警告的受控原因。"
    )
    methodology_version_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("methodology_version.methodology_version_id", ondelete="RESTRICT"),
        nullable=False,
        comment="派生折溢价公式方法学版本 UUID。",
    )
    release_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("dataset_release.release_id", ondelete="RESTRICT"),
        nullable=False,
        comment="包含本派生行的不可变 release UUID。",
    )
    revision_no: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="同一折溢价逻辑事实的递增修订号。"
    )
    public_usable_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="两个输入均可用后的最早安全时刻。"
    )
    availability_basis: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="派生可见性依据。"
    )
    known_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="系统开始知道派生 revision 的时间。"
    )
    known_to: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="系统停止采用派生 revision 的时间。"
    )
    source_batch_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("source_batch.source_batch_id", ondelete="RESTRICT"),
        nullable=False,
        comment="派生输入来源批次或计算证据批次。",
    )
    content_hash: Mapped[str] = mapped_column(
        CHAR(64), nullable=False, comment="派生折溢价内容 SHA-256 摘要。"
    )
    quality_status: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="派生行质量结论。"
    )
