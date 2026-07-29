"""融资融券和沪深港通的强类型事实、快照与制度版本模型。"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    Date,
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
from sqlalchemy.dialects.postgresql import ARRAY, DATERANGE, TSTZRANGE, ExcludeConstraint, Range
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base
from .revision_mixin import CanonicalRevisionMixin


class MarginMarketDailyRevision(CanonicalRevisionMixin, Base):
    """保存场所日频两融市场汇总，绝不由当期证券明细反向累加替代。"""

    __tablename__ = "margin_market_daily_revision"
    __table_args__ = (
        CheckConstraint("revision_no > 0", name="ck_margin_market_revision_no"),
        CheckConstraint(
            "financing_balance IS NULL OR financing_balance >= 0",
            name="ck_margin_market_financing_balance",
        ),
        CheckConstraint(
            "lending_balance_amount IS NULL OR lending_balance_amount >= 0",
            name="ck_margin_market_lending_amount",
        ),
        CheckConstraint(
            "lending_balance_qty IS NULL OR lending_balance_qty >= 0",
            name="ck_margin_market_lending_qty",
        ),
        UniqueConstraint(
            "trade_date",
            "venue_id",
            "methodology_version_id",
            "revision_no",
            name="uq_margin_market_daily_revision",
        ),
        Index("ix_margin_market_date", "venue_id", desc("trade_date"), desc("known_from")),
        {
            "postgresql_partition_by": "RANGE (trade_date)",
            "comment": "融资融券场所日汇总 revision 父表；不得由当前证券名单求和重建。",
        },
    )

    trade_date: Mapped[date] = mapped_column(
        Date, primary_key=True, nullable=False, comment="交易所业务日期。"
    )
    row_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        nullable=False,
        comment="两融市场汇总 revision 行 UUID。",
    )
    venue_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("trading_venue.venue_id", ondelete="RESTRICT"),
        nullable=False,
        comment="披露该市场汇总的交易场所 UUID。",
    )
    financing_balance: Mapped[Decimal | None] = mapped_column(
        Numeric(24, 4), nullable=True, comment="融资余额；未披露时为空而非零。"
    )
    financing_buy_amount: Mapped[Decimal | None] = mapped_column(
        Numeric(24, 4), nullable=True, comment="当日融资买入金额；未披露时为空。"
    )
    financing_repayment_amount: Mapped[Decimal | None] = mapped_column(
        Numeric(24, 4), nullable=True, comment="来源直报融资偿还金额；无直报时为空。"
    )
    lending_balance_amount: Mapped[Decimal | None] = mapped_column(
        Numeric(24, 4), nullable=True, comment="融券余额金额；未披露时为空。"
    )
    lending_balance_qty: Mapped[Decimal | None] = mapped_column(
        Numeric(28, 8), nullable=True, comment="融券余额数量；单位由 quantity_unit 指定。"
    )
    lending_sell_qty: Mapped[Decimal | None] = mapped_column(
        Numeric(28, 8), nullable=True, comment="融券卖出数量；未披露时为空。"
    )
    lending_repayment_qty: Mapped[Decimal | None] = mapped_column(
        Numeric(28, 8), nullable=True, comment="来源直报融券偿还数量；未披露时为空。"
    )
    total_balance: Mapped[Decimal | None] = mapped_column(
        Numeric(24, 4), nullable=True, comment="来源明确给出的两融总余额；不由字段自动相加。"
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False, comment="金额币种 ISO 代码。")
    quantity_unit: Mapped[str | None] = mapped_column(
        String(16), nullable=True, comment="融券数量单位；金额专用记录可为空。"
    )


class MarginSecurityDailyRevision(CanonicalRevisionMixin, Base):
    """保存证券日频两融明细，直报偿还与派生偿还不能在同一行混用。"""

    __tablename__ = "margin_security_daily_revision"
    __table_args__ = (
        CheckConstraint("revision_no > 0", name="ck_margin_security_revision_no"),
        CheckConstraint(
            "financing_balance IS NULL OR financing_balance >= 0",
            name="ck_margin_security_financing_balance",
        ),
        CheckConstraint(
            "lending_balance_qty IS NULL OR lending_balance_qty >= 0",
            name="ck_margin_security_lending_qty",
        ),
        CheckConstraint(
            "financing_repayment_reported IS NULL OR financing_repayment_derived IS NULL",
            name="ck_margin_security_repayment_source",
        ),
        UniqueConstraint(
            "trade_date",
            "security_id",
            "methodology_version_id",
            "revision_no",
            name="uq_margin_security_daily_revision",
        ),
        Index("ix_margin_security_asof", "security_id", desc("trade_date"), desc("known_from")),
        {
            "postgresql_partition_by": "RANGE (trade_date)",
            "comment": "融资融券证券日明细 revision 父表；直报与派生偿还字段保持隔离。",
        },
    )

    trade_date: Mapped[date] = mapped_column(
        Date, primary_key=True, nullable=False, comment="证券两融事实所属交易日。"
    )
    row_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        nullable=False,
        comment="证券两融 revision 行 UUID。",
    )
    security_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("equity_instrument.security_id", ondelete="RESTRICT"),
        nullable=False,
        comment="A 股永久证券内部键。",
    )
    financing_balance: Mapped[Decimal | None] = mapped_column(
        Numeric(24, 4), nullable=True, comment="融资余额；未披露时为空。"
    )
    financing_buy_amount: Mapped[Decimal | None] = mapped_column(
        Numeric(24, 4), nullable=True, comment="融资买入金额；未披露时为空。"
    )
    financing_repayment_reported: Mapped[Decimal | None] = mapped_column(
        Numeric(24, 4), nullable=True, comment="来源直报融资偿还金额。"
    )
    financing_repayment_derived: Mapped[Decimal | None] = mapped_column(
        Numeric(24, 4), nullable=True, comment="由版本化方法学派生的融资偿还金额。"
    )
    derived_methodology_version_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("methodology_version.methodology_version_id", ondelete="RESTRICT"),
        nullable=True,
        comment="派生偿还金额的方法学版本；直报时为空。",
    )
    lending_balance_qty: Mapped[Decimal | None] = mapped_column(
        Numeric(28, 8), nullable=True, comment="融券余额数量；单位由 quantity_unit 指定。"
    )
    quantity_unit: Mapped[str | None] = mapped_column(
        String(16), nullable=True, comment="融券数量单位；无数量字段时为空。"
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False, comment="金额币种 ISO 代码。")
    null_reason: Mapped[str | None] = mapped_column(
        String(32), nullable=True, comment="字段或分区缺值的受控原因。"
    )


class MarginEligibilityRevision(CanonicalRevisionMixin, Base):
    """保存证券两融资格双时间版本，当前名单不能倒推历史资格。"""

    __tablename__ = "margin_eligibility_revision"
    __table_args__ = (
        CheckConstraint(
            "effective_to IS NULL OR effective_to > effective_from",
            name="ck_margin_eligibility_effective_range",
        ),
        CheckConstraint(
            "known_to IS NULL OR known_to > known_from",
            name="ck_margin_eligibility_knowledge_range",
        ),
        ExcludeConstraint(
            ("security_id", "="),
            ("effective_range", "&&"),
            ("knowledge_range", "&&"),
            using="gist",
            name="ex_margin_eligibility_time",
        ),
        Index(
            "ix_margin_eligibility_asof", "security_id", desc("effective_from"), desc("known_from")
        ),
        {"comment": "证券两融资格双时间 revision；来源当前清单只能产生观察时点后的知识版本。"},
    )

    eligibility_revision_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, nullable=False, comment="两融资格 revision UUID。"
    )
    security_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("equity_instrument.security_id", ondelete="RESTRICT"),
        nullable=False,
        comment="A 股永久证券内部键。",
    )
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, comment="可融资融券、仅融资、仅融券或不适格状态。"
    )
    evidence_basis: Mapped[str | None] = mapped_column(
        String(24),
        nullable=True,
        comment="资格事实来自官方公告或当前观察名单；迁移前历史空值不得被猜测为任一证据。",
    )
    announcement_at: Mapped[date | None] = mapped_column(
        Date, nullable=True, comment="来源仅提供日期的公告日；精确时间另由共享列表示。"
    )
    effective_from: Mapped[date] = mapped_column(Date, nullable=False, comment="资格开始适用日期。")
    effective_to: Mapped[date | None] = mapped_column(
        Date, nullable=True, comment="资格结束适用日期；开区间为空。"
    )
    effective_range: Mapped[Range[date] | None] = mapped_column(
        DATERANGE,
        Computed("daterange(effective_from, effective_to, '[)')", persisted=True),
        nullable=True,
        comment="由有效日期端点生成的半开范围。",
    )
    knowledge_range: Mapped[Range[datetime] | None] = mapped_column(
        TSTZRANGE,
        Computed("tstzrange(known_from, known_to, '[)')", persisted=True),
        nullable=True,
        comment="由知识时间端点生成的半开范围。",
    )


class MarginSystemRiskDailyRevision(CanonicalRevisionMixin, Base):
    """保存 P2 两融系统风险指标，独立于市场与证券明细发布。"""

    __tablename__ = "margin_system_risk_daily_revision"
    __table_args__ = (
        CheckConstraint("revision_no > 0", name="ck_margin_system_risk_revision_no"),
        UniqueConstraint(
            "trade_date",
            "scope_code",
            "metric_code",
            "methodology_version_id",
            "revision_no",
            name="uq_margin_system_risk_daily_revision",
        ),
        {
            "postgresql_partition_by": "RANGE (trade_date)",
            "comment": "两融系统风险日指标 revision 父表；P2 只在独立数据集发布。",
        },
    )

    trade_date: Mapped[date] = mapped_column(
        Date, primary_key=True, nullable=False, comment="系统风险指标所属交易日。"
    )
    row_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        nullable=False,
        comment="系统风险 revision 行 UUID。",
    )
    scope_code: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="来源明确的市场或制度统计范围代码。"
    )
    metric_code: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="系统风险指标稳定编码。"
    )
    metric_value: Mapped[Decimal | None] = mapped_column(
        Numeric(24, 8), nullable=True, comment="来源披露的风险指标数值；未披露时为空。"
    )
    metric_unit: Mapped[str | None] = mapped_column(
        String(24), nullable=True, comment="风险指标单位；无量纲时为空。"
    )
    account_count: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="可选账户数量；不与证券数混用。"
    )
    guarantee_amount: Mapped[Decimal | None] = mapped_column(
        Numeric(24, 4), nullable=True, comment="可选担保金额；未披露时为空。"
    )


class StockConnectDisclosureRegime(Base):
    """保存沪深港通通道与方向的制度版本，2024 披露断点必须显式建模。"""

    __tablename__ = "stock_connect_disclosure_regime"
    __table_args__ = (
        CheckConstraint("channel IN ('SH', 'SZ')", name="ck_stock_connect_regime_channel"),
        CheckConstraint(
            "direction IN ('NORTHBOUND', 'SOUTHBOUND')", name="ck_stock_connect_regime_direction"
        ),
        CheckConstraint(
            "effective_to IS NULL OR effective_to > effective_from",
            name="ck_stock_connect_regime_effective_range",
        ),
        ExcludeConstraint(
            ("channel", "="),
            ("direction", "="),
            ("effective_range", "&&"),
            using="gist",
            name="ex_stock_connect_regime_time",
        ),
        {"comment": "沪深港通披露制度版本；制度变化后缺字段表达为未披露而不是零。"},
    )

    regime_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, nullable=False, comment="披露制度版本 UUID。"
    )
    channel: Mapped[str] = mapped_column(
        String(8), nullable=False, comment="沪股通或深股通通道代码。"
    )
    direction: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="北向或南向交易方向。"
    )
    effective_from: Mapped[date] = mapped_column(Date, nullable=False, comment="制度开始适用日期。")
    effective_to: Mapped[date | None] = mapped_column(
        Date, nullable=True, comment="制度结束适用日期；开区间为空。"
    )
    available_fields: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, comment="该制度实际公开的字段白名单。"
    )
    methodology_version_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("methodology_version.methodology_version_id", ondelete="RESTRICT"),
        nullable=False,
        comment="制度解释方法学版本 UUID。",
    )
    evidence_ref: Mapped[str] = mapped_column(
        Text, nullable=False, comment="制度证据或官方规则引用。"
    )
    effective_range: Mapped[Range[date] | None] = mapped_column(
        DATERANGE,
        Computed("daterange(effective_from, effective_to, '[)')", persisted=True),
        nullable=True,
        comment="由制度有效日期端点生成的半开范围。",
    )


class StockConnectChannelDailyRevision(CanonicalRevisionMixin, Base):
    """保存通道日频统计，买卖、成交、净买与额度余额均按制度字段独立保留。"""

    __tablename__ = "stock_connect_channel_daily_revision"
    __table_args__ = (
        CheckConstraint("channel IN ('SH', 'SZ')", name="ck_stock_connect_channel_channel"),
        CheckConstraint(
            "direction IN ('NORTHBOUND', 'SOUTHBOUND')", name="ck_stock_connect_channel_direction"
        ),
        CheckConstraint("revision_no > 0", name="ck_stock_connect_channel_revision_no"),
        UniqueConstraint(
            "trade_date",
            "channel",
            "direction",
            "methodology_version_id",
            "revision_no",
            name="uq_stock_connect_channel_daily_revision",
        ),
        Index(
            "ix_stock_connect_channel_asof",
            "channel",
            "direction",
            desc("trade_date"),
            desc("known_from"),
        ),
        {
            "postgresql_partition_by": "RANGE (trade_date)",
            "comment": "沪深港通通道日统计 revision 父表；未披露字段保持 NULL。",
        },
    )

    trade_date: Mapped[date] = mapped_column(
        Date, primary_key=True, nullable=False, comment="通道统计所属交易日。"
    )
    row_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        nullable=False,
        comment="通道统计 revision 行 UUID。",
    )
    channel: Mapped[str] = mapped_column(
        String(8), nullable=False, comment="沪股通或深股通通道代码。"
    )
    direction: Mapped[str] = mapped_column(String(16), nullable=False, comment="北向或南向方向。")
    regime_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("stock_connect_disclosure_regime.regime_id", ondelete="RESTRICT"),
        nullable=False,
        comment="解释该日字段可用性的制度版本 UUID。",
    )
    buy_amount: Mapped[Decimal | None] = mapped_column(
        Numeric(24, 4), nullable=True, comment="通道买入金额；未披露时为空。"
    )
    sell_amount: Mapped[Decimal | None] = mapped_column(
        Numeric(24, 4), nullable=True, comment="通道卖出金额；未披露时为空。"
    )
    turnover_amount: Mapped[Decimal | None] = mapped_column(
        Numeric(24, 4), nullable=True, comment="通道成交金额；未披露时为空。"
    )
    net_buy_amount: Mapped[Decimal | None] = mapped_column(
        Numeric(24, 4), nullable=True, comment="通道净买入额；未披露时为空。"
    )
    quota_balance: Mapped[Decimal | None] = mapped_column(
        Numeric(24, 4), nullable=True, comment="通道额度余额；制度不披露时为空。"
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False, comment="金额币种 ISO 代码。")
    availability_status: Mapped[str] = mapped_column(
        String(24), nullable=False, comment="完整、部分披露或制度性不可用状态。"
    )


class StockConnectActiveSecurityRevision(CanonicalRevisionMixin, Base):
    """保存通道日活跃证券排行，A/H 或跨市场身份无法解析时应隔离而非按代码合并。"""

    __tablename__ = "stock_connect_active_security_revision"
    __table_args__ = (
        CheckConstraint("rank_no > 0", name="ck_stock_connect_active_rank"),
        UniqueConstraint(
            "trade_date",
            "channel",
            "direction",
            "rank_no",
            "release_id",
            name="uq_stock_connect_active_rank_release",
        ),
        {
            "postgresql_partition_by": "RANGE (trade_date)",
            "comment": "沪深港通活跃证券日排行 revision 父表；排行集合与通道统计独立。",
        },
    )

    trade_date: Mapped[date] = mapped_column(
        Date, primary_key=True, nullable=False, comment="活跃榜所属交易日。"
    )
    row_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        nullable=False,
        comment="活跃证券 revision 行 UUID。",
    )
    channel: Mapped[str] = mapped_column(
        String(8), nullable=False, comment="沪股通或深股通通道代码。"
    )
    direction: Mapped[str] = mapped_column(String(16), nullable=False, comment="北向或南向方向。")
    instrument_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("market_instrument.instrument_id", ondelete="RESTRICT"),
        nullable=False,
        comment="已准确解析的跨市场可交易工具 UUID。",
    )
    market_stat_release_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey(
            "dataset_release.release_id",
            name="fk_stock_connect_active_market_stat_release",
            ondelete="RESTRICT",
        ),
        nullable=True,
        comment="同日通道市场统计使用的 immutable release；历史空值不得推测绑定版本。",
    )
    rank_no: Mapped[int] = mapped_column(Integer, nullable=False, comment="来源活跃榜名次。")
    buy_amount: Mapped[Decimal | None] = mapped_column(
        Numeric(24, 4), nullable=True, comment="证券买入金额；未披露时为空。"
    )
    sell_amount: Mapped[Decimal | None] = mapped_column(
        Numeric(24, 4), nullable=True, comment="证券卖出金额；未披露时为空。"
    )
    turnover_amount: Mapped[Decimal | None] = mapped_column(
        Numeric(24, 4), nullable=True, comment="证券通道成交金额；未披露时为空。"
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False, comment="金额币种 ISO 代码。")


class StockConnectHoldingSnapshot(CanonicalRevisionMixin, Base):
    """保存完整持股快照头，日频和季频不因共享证券字段而混合发布。"""

    __tablename__ = "stock_connect_holding_snapshot"
    __table_args__ = (
        CheckConstraint("channel IN ('SH', 'SZ')", name="ck_stock_connect_holding_channel"),
        CheckConstraint(
            "direction IN ('NORTHBOUND', 'SOUTHBOUND')", name="ck_stock_connect_holding_direction"
        ),
        CheckConstraint(
            "frequency IN ('DAILY', 'QUARTERLY')", name="ck_stock_connect_holding_frequency"
        ),
        CheckConstraint(
            "expected_count IS NULL OR expected_count >= 0",
            name="ck_stock_connect_holding_expected_count",
        ),
        UniqueConstraint(
            "snapshot_date",
            "channel",
            "direction",
            "methodology_version_id",
            "revision_no",
            name="uq_stock_connect_holding_snapshot",
        ),
        {
            "postgresql_partition_by": "RANGE (snapshot_date)",
            "comment": "沪深港通持股完整快照头父表；日频和季频使用独立 frequency 语义。",
        },
    )

    snapshot_date: Mapped[date] = mapped_column(
        Date, primary_key=True, nullable=False, comment="持股快照对应日期。"
    )
    snapshot_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, nullable=False, comment="持股快照 UUID。"
    )
    channel: Mapped[str] = mapped_column(
        String(8), nullable=False, comment="沪股通或深股通通道代码。"
    )
    direction: Mapped[str] = mapped_column(String(16), nullable=False, comment="北向或南向方向。")
    frequency: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="日频或季频披露频率。"
    )
    period_end: Mapped[date | None] = mapped_column(
        Date, nullable=True, comment="季度或其他披露期末；日频可为空。"
    )
    disclosed_at: Mapped[date | None] = mapped_column(
        Date, nullable=True, comment="来源仅日期粒度的披露日期；精确时间另由共享列表示。"
    )
    is_complete: Mapped[bool] = mapped_column(
        Boolean, nullable=False, comment="来源页集合是否经完整性门确认。"
    )
    expected_count: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="来源给出的预期持股行数；未知时为空。"
    )


class StockConnectHoldingItem(Base):
    """保存持股快照内证券项，不按后续公司行动回写原始来源持股事实。"""

    __tablename__ = "stock_connect_holding_item"
    __table_args__ = (
        CheckConstraint("holding_qty >= 0", name="ck_stock_connect_holding_item_qty"),
        CheckConstraint(
            "ownership_ratio IS NULL OR (ownership_ratio >= 0 AND ownership_ratio <= 1)",
            name="ck_stock_connect_holding_item_ratio",
        ),
        ForeignKeyConstraint(
            ["snapshot_date", "snapshot_id"],
            [
                "stock_connect_holding_snapshot.snapshot_date",
                "stock_connect_holding_snapshot.snapshot_id",
            ],
            name="fk_stock_connect_holding_item_snapshot",
            ondelete="RESTRICT",
        ),
        {
            "postgresql_partition_by": "RANGE (snapshot_date)",
            "comment": "沪深港通持股快照项父表；与快照头同日期分区并保留原始数量。",
        },
    )

    snapshot_date: Mapped[date] = mapped_column(
        Date, primary_key=True, nullable=False, comment="所属持股快照日期。"
    )
    snapshot_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, nullable=False, comment="所属持股快照 UUID。"
    )
    instrument_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("market_instrument.instrument_id", ondelete="RESTRICT"),
        primary_key=True,
        nullable=False,
        comment="已准确解析的证券或跨市场工具 UUID。",
    )
    holding_qty: Mapped[Decimal] = mapped_column(
        Numeric(28, 8), nullable=False, comment="来源持股数量。"
    )
    quantity_unit: Mapped[str] = mapped_column(String(16), nullable=False, comment="持股数量单位。")
    holding_value: Mapped[Decimal | None] = mapped_column(
        Numeric(24, 4), nullable=True, comment="来源披露持股市值；未披露时为空。"
    )
    currency: Mapped[str | None] = mapped_column(
        String(3), nullable=True, comment="持股市值币种；无市值字段时为空。"
    )
    ownership_ratio: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 10), nullable=True, comment="来源披露持股比例；未披露时为空。"
    )
