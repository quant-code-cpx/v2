"""跨资产根身份、双时间标识、交易场所与新资产扩展模型。"""

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
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    desc,
)
from sqlalchemy.dialects.postgresql import (
    DATERANGE,
    TSTZRANGE,
    ExcludeConstraint,
    Range,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base

_ENTITY_KINDS = (
    "EQUITY",
    "ETF_LISTING",
    "INDEX",
    "FUND",
    "FUND_SHARE",
    "DERIVATIVE_PRODUCT",
    "FUTURE",
    "OPTION",
)


class TradingVenue(Base):
    """保存交易所或指数管理人等场所字典，避免业务事实直接复制场所名称。"""

    __tablename__ = "trading_venue"
    __table_args__ = (
        CheckConstraint("country ~ '^[A-Z]{2}$'", name="ck_trading_venue_country"),
        CheckConstraint(
            "timezone IN ('Asia/Shanghai', 'Asia/Hong_Kong', 'UTC')",
            name="ck_trading_venue_timezone",
        ),
        UniqueConstraint("code", name="uq_trading_venue_code"),
        UniqueConstraint("mic", name="uq_trading_venue_mic"),
        {"comment": "交易场所和管理人相关场所字典；代码为稳定业务标识。"},
    )

    venue_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, nullable=False, comment="场所永久 UUID。"
    )
    mic: Mapped[str | None] = mapped_column(
        String(8), nullable=True, comment="可选 ISO 市场识别码；没有时为空。"
    )
    code: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="系统稳定场所代码，例如 SSE 或 CFFEX。"
    )
    name: Mapped[str] = mapped_column(Text, nullable=False, comment="场所正式展示名称。")
    timezone: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="场所与交易日历解释使用的 IANA 时区。"
    )
    country: Mapped[str] = mapped_column(
        CHAR(2), nullable=False, comment="场所所属 ISO 国家或地区代码。"
    )
    active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, comment="场所是否仍接受新数据能力接入。"
    )


class MarketEntity(Base):
    """提供跨资产不可变引用根；实体存在不代表它可在场所交易。"""

    __tablename__ = "market_entity"
    __table_args__ = (
        CheckConstraint(
            f"entity_kind IN ({', '.join(repr(kind) for kind in _ENTITY_KINDS)})",
            name="ck_market_entity_kind",
        ),
        CheckConstraint(
            "retired_at IS NULL OR retired_at >= created_at", name="ck_market_entity_retired"
        ),
        UniqueConstraint("entity_id", "entity_kind", name="uq_market_entity_id_kind"),
        {"comment": "跨域不可变实体根；指数和法律基金可被引用但不等同于可交易工具。"},
    )

    entity_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, nullable=False, comment="跨域实体永久 UUID。"
    )
    entity_kind: Mapped[str] = mapped_column(
        String(24), nullable=False, comment="资产或实体种类，用于强制领域关系边界。"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="实体首次进入本系统的时间。"
    )
    retired_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="实体确认退休时间；未退休时为空。"
    )


class MarketInstrument(Base):
    """表达可在明确场所交易的实体子集，禁止把指数或法律基金放入交易工具层。"""

    __tablename__ = "market_instrument"
    __table_args__ = (
        CheckConstraint(
            "instrument_kind IN ('EQUITY', 'ETF_LISTING', 'FUTURE', 'OPTION')",
            name="ck_market_instrument_kind",
        ),
        CheckConstraint(
            "tradable_to IS NULL OR tradable_from IS NULL OR tradable_to > tradable_from",
            name="ck_market_instrument_tradable_range",
        ),
        ForeignKeyConstraint(
            ["instrument_id", "instrument_kind"],
            ["market_entity.entity_id", "market_entity.entity_kind"],
            name="fk_market_instrument_entity_kind",
            ondelete="RESTRICT",
        ),
        {"comment": "可交易实体子表；交易代码仍由双时间 identifier version 表维护。"},
    )

    instrument_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        nullable=False,
        comment="同时引用 market_entity 的工具 UUID。",
    )
    instrument_kind: Mapped[str] = mapped_column(
        String(24), nullable=False, comment="可交易工具种类，必须与根实体种类相同。"
    )
    primary_venue_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("trading_venue.venue_id", ondelete="RESTRICT"),
        nullable=True,
        comment="可选主要交易场所；跨场所或未知时为空。",
    )
    tradable_from: Mapped[date | None] = mapped_column(
        Date, nullable=True, comment="可交易状态开始日期；证据不足时为空。"
    )
    tradable_to: Mapped[date | None] = mapped_column(
        Date, nullable=True, comment="可交易状态结束日期；开区间表示仍可能交易。"
    )


class InstrumentIdentifierVersion(Base):
    """保存新资产代码的有效时间和知识时间版本，阻止代码复用产生静默误绑。"""

    __tablename__ = "instrument_identifier_version"
    __table_args__ = (
        CheckConstraint(
            "effective_to IS NULL OR effective_to > effective_from",
            name="ck_instrument_identifier_effective_range",
        ),
        CheckConstraint(
            "known_to IS NULL OR known_to > known_from",
            name="ck_instrument_identifier_knowledge_range",
        ),
        CheckConstraint(
            "source_time_precision IN ('EXACT', 'DATE_ONLY', 'UNKNOWN')",
            name="ck_instrument_identifier_source_time_precision",
        ),
        ForeignKeyConstraint(
            ["entity_id", "entity_kind"],
            ["market_entity.entity_id", "market_entity.entity_kind"],
            name="fk_instrument_identifier_entity_kind",
            ondelete="RESTRICT",
        ),
        ExcludeConstraint(
            ("entity_kind", "="),
            ("identifier_scheme", "="),
            ("identifier_value", "="),
            ("effective_range", "&&"),
            ("knowledge_range", "&&"),
            using="gist",
            name="ex_instrument_identifier_code_time",
        ),
        ExcludeConstraint(
            ("entity_id", "="),
            ("identifier_scheme", "="),
            ("effective_range", "&&"),
            ("knowledge_range", "&&"),
            using="gist",
            name="ex_instrument_identifier_entity_time",
        ),
        Index(
            "ix_instrument_identifier_asof",
            "entity_kind",
            "venue_id",
            "identifier_scheme",
            "identifier_value",
            desc("effective_from"),
            desc("known_from"),
        ),
        {
            "comment": "ETF、指数和衍生品代码的双时间解析；股票代码仍由 "
            "equity_identifier_version 权威维护。"
        },
    )

    version_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, nullable=False, comment="标识版本永久 UUID。"
    )
    entity_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False, comment="被此标识版本引用的根实体 UUID。"
    )
    entity_kind: Mapped[str] = mapped_column(
        String(24), nullable=False, comment="根实体种类，作为复合外键的一部分。"
    )
    venue_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("trading_venue.venue_id", ondelete="RESTRICT"),
        nullable=True,
        comment="标识所属场所；非交易实体标识可以为空。",
    )
    identifier_scheme: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="标识方案，例如 SSE_TICKER 或 CFFEX_CONTRACT。"
    )
    identifier_value: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="来源确认的代码或其他标识文本。"
    )
    effective_from: Mapped[date] = mapped_column(
        Date, nullable=False, comment="该标识开始适用的业务日期。"
    )
    effective_to: Mapped[date | None] = mapped_column(
        Date, nullable=True, comment="该标识停止适用的业务日期；开区间为空。"
    )
    known_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="系统开始采用该标识知识版本的时间。"
    )
    known_to: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="系统停止采用该标识知识版本的时间。"
    )
    source_time_precision: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="来源发布时间精度，未知不允许伪造精确时刻。"
    )
    source_batch_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("source_batch.source_batch_id", ondelete="RESTRICT"),
        nullable=False,
        comment="支撑该标识版本的来源观察批次。",
    )
    effective_range: Mapped[Range[date] | None] = mapped_column(
        DATERANGE,
        Computed("daterange(effective_from, effective_to, '[)')", persisted=True),
        nullable=True,
        comment="由有效时间端点生成的半开日期范围。",
    )
    knowledge_range: Mapped[Range[datetime] | None] = mapped_column(
        TSTZRANGE,
        Computed("tstzrange(known_from, known_to, '[)')", persisted=True),
        nullable=True,
        comment="由知识时间端点生成的半开时间范围。",
    )


class InstrumentLifecycleVersion(Base):
    """保存上市、停编、到期和摘牌等新资产生命周期事实版本。"""

    __tablename__ = "instrument_lifecycle_version"
    __table_args__ = (
        CheckConstraint(
            "effective_to IS NULL OR effective_to > effective_from",
            name="ck_instrument_lifecycle_effective_range",
        ),
        CheckConstraint(
            "known_to IS NULL OR known_to > known_from",
            name="ck_instrument_lifecycle_knowledge_range",
        ),
        CheckConstraint(
            "status_code IN ('PENDING', 'ACTIVE', 'SUSPENDED', 'EXPIRED', 'RETIRED')",
            name="ck_instrument_lifecycle_status",
        ),
        ForeignKeyConstraint(
            ["entity_id", "entity_kind"],
            ["market_entity.entity_id", "market_entity.entity_kind"],
            name="fk_instrument_lifecycle_entity_kind",
            ondelete="RESTRICT",
        ),
        ExcludeConstraint(
            ("entity_id", "="),
            ("effective_range", "&&"),
            ("knowledge_range", "&&"),
            using="gist",
            name="ex_instrument_lifecycle_entity_time",
        ),
        Index(
            "ix_instrument_lifecycle_asof", "entity_id", desc("effective_from"), desc("known_from")
        ),
        {
            "comment": "新资产生命周期双时间版本；股票上市状态继续由 "
            "equity_listing_status_version 权威维护。"
        },
    )

    version_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, nullable=False, comment="生命周期版本永久 UUID。"
    )
    entity_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False, comment="生命周期所属根实体 UUID。"
    )
    entity_kind: Mapped[str] = mapped_column(
        String(24), nullable=False, comment="根实体种类，作为复合外键的一部分。"
    )
    status_code: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="待定、活跃、暂停、到期或退休状态。"
    )
    event_kind: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="导致此生命周期版本的事件类别。"
    )
    effective_from: Mapped[date] = mapped_column(
        Date, nullable=False, comment="生命周期状态开始适用日期。"
    )
    effective_to: Mapped[date | None] = mapped_column(
        Date, nullable=True, comment="生命周期状态结束适用日期；开区间为空。"
    )
    known_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="系统开始知道此版本的时间。"
    )
    known_to: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="系统停止采用此版本的时间。"
    )
    evidence_ref: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="可选官方证据或私有归档引用。"
    )
    source_batch_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("source_batch.source_batch_id", ondelete="RESTRICT"),
        nullable=False,
        comment="支撑生命周期事实的来源观察批次。",
    )
    effective_range: Mapped[Range[date] | None] = mapped_column(
        DATERANGE,
        Computed("daterange(effective_from, effective_to, '[)')", persisted=True),
        nullable=True,
        comment="由有效时间端点生成的半开日期范围。",
    )
    knowledge_range: Mapped[Range[datetime] | None] = mapped_column(
        TSTZRANGE,
        Computed("tstzrange(known_from, known_to, '[)')", persisted=True),
        nullable=True,
        comment="由知识时间端点生成的半开时间范围。",
    )


class MarketEntityRelationVersion(Base):
    """保存跟踪、underlying、基金份额和产品合约等类型化双时间关系。"""

    __tablename__ = "market_entity_relation_version"
    __table_args__ = (
        CheckConstraint(
            "from_entity_id <> to_entity_id", name="ck_market_entity_relation_not_self"
        ),
        CheckConstraint(
            "effective_to IS NULL OR effective_to > effective_from",
            name="ck_market_entity_relation_effective_range",
        ),
        CheckConstraint(
            "known_to IS NULL OR known_to > known_from",
            name="ck_market_entity_relation_knowledge_range",
        ),
        CheckConstraint(
            "relation_kind IN ('TRACKS', 'UNDERLYING', 'FUND_SHARE', "
            "'LISTED_AS', 'PRODUCT_CONTRACT')",
            name="ck_market_entity_relation_kind",
        ),
        ExcludeConstraint(
            ("from_entity_id", "="),
            ("to_entity_id", "="),
            ("relation_kind", "="),
            ("effective_range", "&&"),
            ("knowledge_range", "&&"),
            using="gist",
            name="ex_market_entity_relation_time",
        ),
        Index(
            "ix_market_entity_relation_to",
            "to_entity_id",
            desc("effective_from"),
            desc("known_from"),
        ),
        {"comment": "跨资产关系双时间版本；禁止用名称或代码推断关系。"},
    )

    relation_version_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, nullable=False, comment="关系版本永久 UUID。"
    )
    from_entity_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("market_entity.entity_id", ondelete="RESTRICT"),
        nullable=False,
        comment="关系起点实体 UUID。",
    )
    to_entity_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("market_entity.entity_id", ondelete="RESTRICT"),
        nullable=False,
        comment="关系终点实体 UUID。",
    )
    relation_kind: Mapped[str] = mapped_column(
        String(24), nullable=False, comment="跟踪、底层、份额、上市或产品合约关系类别。"
    )
    effective_from: Mapped[date] = mapped_column(
        Date, nullable=False, comment="关系开始生效的业务日期。"
    )
    effective_to: Mapped[date | None] = mapped_column(
        Date, nullable=True, comment="关系停止生效的业务日期；开区间为空。"
    )
    known_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="系统开始采用关系知识版本的时间。"
    )
    known_to: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="系统停止采用关系知识版本的时间。"
    )
    source_batch_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("source_batch.source_batch_id", ondelete="RESTRICT"),
        nullable=False,
        comment="支撑该关系的来源观察批次。",
    )
    methodology_version_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("methodology_version.methodology_version_id", ondelete="RESTRICT"),
        nullable=True,
        comment="派生或映射关系采用的方法学版本；直报关系为空。",
    )
    effective_range: Mapped[Range[date] | None] = mapped_column(
        DATERANGE,
        Computed("daterange(effective_from, effective_to, '[)')", persisted=True),
        nullable=True,
        comment="由有效时间端点生成的半开日期范围。",
    )
    knowledge_range: Mapped[Range[datetime] | None] = mapped_column(
        TSTZRANGE,
        Computed("tstzrange(known_from, known_to, '[)')", persisted=True),
        nullable=True,
        comment="由知识时间端点生成的半开时间范围。",
    )


class MarketCalendarDay(Base):
    """保存版本化交易、结算或披露日历日，未知日历不会被自动推断为开市。"""

    __tablename__ = "market_calendar_day"
    __table_args__ = (
        CheckConstraint(
            "day_kind IN ('TRADING', 'SETTLEMENT', 'DISCLOSURE')",
            name="ck_market_calendar_day_kind",
        ),
        CheckConstraint("version > 0", name="ck_market_calendar_day_version"),
        UniqueConstraint(
            "calendar_id",
            "calendar_date",
            "day_kind",
            "version",
            name="uq_market_calendar_day_version",
        ),
        Index("ix_market_calendar_day_open", "calendar_date", "is_open"),
        {"comment": "场所版本化日历日；每种日历用途都显式保留开闭市状态。"},
    )

    calendar_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("trading_venue.venue_id", ondelete="RESTRICT"),
        primary_key=True,
        nullable=False,
        comment="关联场所作为日历稳定身份。",
    )
    calendar_date: Mapped[date] = mapped_column(
        Date, primary_key=True, nullable=False, comment="场所时区下的业务日历日期。"
    )
    day_kind: Mapped[str] = mapped_column(
        String(16), primary_key=True, nullable=False, comment="交易、结算或披露日历用途。"
    )
    version: Mapped[int] = mapped_column(
        Integer, primary_key=True, nullable=False, comment="同一日历日期的不可变版本号。"
    )
    is_open: Mapped[bool] = mapped_column(
        Boolean, nullable=False, comment="该日期是否对指定用途开放。"
    )
    source_batch_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("source_batch.source_batch_id", ondelete="RESTRICT"),
        nullable=False,
        comment="支撑日历结论的来源观察批次。",
    )
    known_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="系统开始采用此日历版本的时间。"
    )
    known_to: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="系统停止采用此日历版本的时间。"
    )


class MarketSessionVersion(Base):
    """表达夜盘、日盘和临时调整的版本化会话，跨自然日归属由 trade_date 明确指定。"""

    __tablename__ = "market_session_version"
    __table_args__ = (
        CheckConstraint("closes_at > opens_at", name="ck_market_session_time_order"),
        CheckConstraint(
            "known_to IS NULL OR known_to > known_from", name="ck_market_session_knowledge_range"
        ),
        CheckConstraint(
            "session_kind IN ('DAY', 'NIGHT', 'AUCTION', 'BREAK')", name="ck_market_session_kind"
        ),
        ExcludeConstraint(
            ("calendar_id", "="),
            ("session_kind", "="),
            ("time_range", "&&"),
            using="gist",
            name="ex_market_session_overlap",
        ),
        Index("ix_market_session_trade_date", "calendar_id", "trade_date", desc("known_from")),
        {"comment": "场所会话版本；夜盘不会因自然日变化而被错误归入下一交易日。"},
    )

    session_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, nullable=False, comment="会话版本永久 UUID。"
    )
    calendar_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("trading_venue.venue_id", ondelete="RESTRICT"),
        nullable=False,
        comment="会话所属场所日历身份。",
    )
    product_scope: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="可选品种范围；为空表示场所默认会话。"
    )
    trade_date: Mapped[date] = mapped_column(
        Date, nullable=False, comment="该会话归属的交易日期，而非自然日。"
    )
    opens_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="会话实际开启时刻。"
    )
    closes_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="会话实际结束时刻。"
    )
    session_kind: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="日盘、夜盘、集合竞价或休市时段。"
    )
    known_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="系统开始采用会话版本的时间。"
    )
    known_to: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="系统停止采用会话版本的时间。"
    )
    source_batch_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("source_batch.source_batch_id", ondelete="RESTRICT"),
        nullable=False,
        comment="支撑会话规则的来源观察批次。",
    )
    time_range: Mapped[Range[datetime] | None] = mapped_column(
        TSTZRANGE,
        Computed("tstzrange(opens_at, closes_at, '[)')", persisted=True),
        nullable=True,
        comment="用于排斥重叠会话的半开时间范围。",
    )


class FundLegalEntity(Base):
    """保存法律基金根实体，交易代码必须归属于份额类别或 ETF 上市工具。"""

    __tablename__ = "fund_legal_entity"
    __table_args__ = (
        CheckConstraint(
            "fund_type IN ('ETF', 'MUTUAL_FUND', 'OTHER')", name="ck_fund_legal_entity_type"
        ),
        {"comment": "法律基金实体扩展；不直接保存交易所交易代码。"},
    )

    entity_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("market_entity.entity_id", ondelete="RESTRICT"),
        primary_key=True,
        nullable=False,
        comment="对应 FUND 根实体 UUID。",
    )
    legal_fund_code: Mapped[str | None] = mapped_column(
        String(64), unique=True, nullable=True, comment="可选法律基金外部代码；无稳定代码时为空。"
    )
    manager_entity_ref: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("market_entity.entity_id", ondelete="RESTRICT"),
        nullable=True,
        comment="可选基金管理人实体引用。",
    )
    fund_type: Mapped[str] = mapped_column(
        String(24), nullable=False, comment="ETF、普通公募基金或其他法律基金类型。"
    )
    base_currency: Mapped[str] = mapped_column(
        CHAR(3), nullable=False, comment="法律基金基础币种 ISO 代码。"
    )


class FundShareClass(Base):
    """保存法律基金下的份额类别，避免把份额、基金和上市工具合并为同一身份。"""

    __tablename__ = "fund_share_class"
    __table_args__ = (
        UniqueConstraint("fund_entity_id", "share_class_code", name="uq_fund_share_class_code"),
        {"comment": "法律基金份额类别；可由一个或多个上市工具代表交易。"},
    )

    entity_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("market_entity.entity_id", ondelete="RESTRICT"),
        primary_key=True,
        nullable=False,
        comment="对应 FUND_SHARE 根实体 UUID。",
    )
    fund_entity_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("fund_legal_entity.entity_id", ondelete="RESTRICT"),
        nullable=False,
        comment="所属法律基金实体 UUID。",
    )
    share_class_code: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="基金内稳定份额类别代码。"
    )
    currency: Mapped[str] = mapped_column(
        CHAR(3), nullable=False, comment="该份额类别计价币种 ISO 代码。"
    )
    accumulation_kind: Mapped[str] = mapped_column(
        String(24), nullable=False, comment="累积、分配或其他份额收益处理方式。"
    )


class EtfListing(Base):
    """保存份额类别在单一场所的 ETF 上市工具，不将它等同于法律基金。"""

    __tablename__ = "etf_listing"
    __table_args__ = (
        UniqueConstraint(
            "share_class_entity_id", "venue_id", name="uq_etf_listing_share_class_venue"
        ),
        {"comment": "ETF 上市工具扩展；交易代码由 instrument_identifier_version 管理。"},
    )

    instrument_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("market_instrument.instrument_id", ondelete="RESTRICT"),
        primary_key=True,
        nullable=False,
        comment="对应 ETF_LISTING 可交易工具 UUID。",
    )
    share_class_entity_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("fund_share_class.entity_id", ondelete="RESTRICT"),
        nullable=False,
        comment="对应基金份额类别实体 UUID。",
    )
    venue_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("trading_venue.venue_id", ondelete="RESTRICT"),
        nullable=False,
        comment="ETF 上市交易场所 UUID。",
    )
    management_mode: Mapped[str] = mapped_column(
        String(24), nullable=False, comment="被动、主动或其他管理方式。"
    )


class DerivativeProduct(Base):
    """保存场所内期货或期权品种实体，真实合约另由 derivative_contract 表表达。"""

    __tablename__ = "derivative_product"
    __table_args__ = (
        CheckConstraint(
            "asset_kind IN ('FUTURE', 'OPTION')", name="ck_derivative_product_asset_kind"
        ),
        UniqueConstraint("venue_id", "product_code", name="uq_derivative_product_venue_code"),
        {"comment": "场所乘品种的衍生品产品实体；不把连续序列当成真实产品。"},
    )

    entity_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("market_entity.entity_id", ondelete="RESTRICT"),
        primary_key=True,
        nullable=False,
        comment="对应 DERIVATIVE_PRODUCT 根实体 UUID。",
    )
    venue_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("trading_venue.venue_id", ondelete="RESTRICT"),
        nullable=False,
        comment="产品所属场所 UUID。",
    )
    product_code: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="场所内产品稳定代码。"
    )
    asset_kind: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="期货或期权产品种类。"
    )
    underlying_entity_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("market_entity.entity_id", ondelete="RESTRICT"),
        nullable=True,
        comment="可选底层实体；来源未确认时为空。",
    )
    currency: Mapped[str] = mapped_column(
        CHAR(3), nullable=False, comment="产品报价或结算币种 ISO 代码。"
    )


class DerivativeContract(Base):
    """保存一个真实可交易衍生品合约，连续序列必须在独立派生表中表达。"""

    __tablename__ = "derivative_contract"
    __table_args__ = (
        CheckConstraint(
            "(call_put IS NULL AND strike_price IS NULL) OR "
            "(call_put IN ('CALL', 'PUT') AND strike_price IS NOT NULL)",
            name="ck_derivative_contract_option_structure",
        ),
        CheckConstraint(
            "expiry_date IS NULL OR listed_date IS NULL OR expiry_date >= listed_date",
            name="ck_derivative_contract_dates",
        ),
        {"comment": "真实可交易期货或期权合约；期权方向和行权价必须成对出现。"},
    )

    instrument_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("market_instrument.instrument_id", ondelete="RESTRICT"),
        primary_key=True,
        nullable=False,
        comment="对应 FUTURE 或 OPTION 可交易工具 UUID。",
    )
    product_entity_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("derivative_product.entity_id", ondelete="RESTRICT"),
        nullable=True,
        comment="所属衍生品产品实体 UUID；目录尚未披露时为空。",
    )
    expiry_date: Mapped[date | None] = mapped_column(
        Date, nullable=True, comment="可选到期日期；来源未确认时为空。"
    )
    call_put: Mapped[str | None] = mapped_column(
        String(8), nullable=True, comment="期权看涨或看跌；期货为空。"
    )
    strike_price: Mapped[Decimal | None] = mapped_column(
        Numeric(24, 8), nullable=True, comment="期权行权价；期货为空。"
    )
    underlying_entity_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("market_entity.entity_id", ondelete="RESTRICT"),
        nullable=True,
        comment="期权或合约可选底层实体引用。",
    )
    listed_date: Mapped[date | None] = mapped_column(
        Date, nullable=True, comment="来源确认的合约挂牌日期；仅有日线时为空。"
    )
