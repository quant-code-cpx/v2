"""融资融券与沪深港通的强类型日频事实、快照、制度版本和独立发布模型。

两融市场、证券资格、港通通道统计、活跃榜和持股快照有不同来源粒度与制度断点；不能从其中
一张表反推另一张，也不能跨通道、方向、币种或披露制度混合统计。
"""

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
    """保存场所日频两融市场汇总；绝不由当期证券明细反向累加替代。

    来源公布的融资、融券余额及交易汇总可能包含未下发证券、口径调整或截止时间差异，因此是独立
    事实。业务交易日、场所、币种、单位和方法学共同决定可比性；内容更正追加 `revision`，不为
    与明细相等而改写原始汇总或将缺失明细解释为零。
    """

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
    """保存证券日频两融明细；直报偿还与派生偿还不能在同一行混用。

    融资买入、偿还、余额和融券相关字段按来源属性保存，派生值必须有单独受控标记和输入血缘，不能
    伪装为直报事实。证券身份、场所、交易日、单位和币种不一致时不可比较；负数或缺失也不能靠
    相邻日期、市场汇总或价格序列自动修正。
    """

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
    """保存证券两融资格双时态版本；当前名单不能倒推历史资格。

    资格由场所、融资/融券维度、业务有效范围和来源证据决定，可能先后纳入、暂停或移除。`known_*`
    区间保留平台何时得知名单变化，历史研究必须同时过滤业务和知识时间；目录缺席或明细交易量为零
    不构成资格取消证据，必须等待批准来源的显式事实。
    """

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
    """保存 `P2` 两融系统风险指标，独立于市场汇总与证券明细发布。

    风险指标的定义、窗口、阈值和来源可与两融余额不同，不能从余额相除或临时汇总生成并混入同一
    数据集。每行以方法学、场所、交易日和 `revision` 固定口径，质量失败或来源策略未批准时保持
    不可发布，而不是返回伪零值或复用上一次指标。
    """

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
    """保存沪深港通通道与方向的披露制度版本；2024 断点必须显式建模。

    不同制度可能改变字段定义、额度、披露时点或统计方式，因而相同列名跨断点未必可比。每条通道/方向
    日频事实都要解析到适用制度，不能以当前规则解释历史数值；制度未知或冲突时应阻断发布，而不是
    选择“最近”版本补齐。
    """

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
    """保存通道日频统计；买卖、成交、净买与额度余额均按制度字段独立保留。

    一条记录绑定通道、方向、交易日和已解析披露制度；买卖额、成交额、净买额及额度余额不能在
    不同规则下重算或相互替代。来源修订关闭旧知识版本并追加新行，消费者读取须锁定同一方法学和
    `release`，防止跨制度/跨时间点组合出看似完整但不可比的序列。
    """

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
    """保存通道日活跃证券排行；`A/H` 或跨市场身份无法解析时应隔离而非按代码合并。

    排行位置依赖同日通道统计的 `release`，缺少该依赖时不能单独发布；证券、场所和币种必须精确
    解析，六位代码或名称相同不能证明是同一 `A/H` 工具。每个名次是独立来源事实，排行重排或金额
    更正形成新 `revision`，不会覆盖历史榜单。
    """

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
    """保存完整持股快照头；日频和季频不因共享证券字段而混合发布。

    快照头界定通道、方向、观察日期、来源频率、完整性和内容摘要，所有持股项必须属于同一完整集合。
    日频与季频的披露时点、覆盖范围和可用性不同，不能按证券字段拼成一个“最新持股”；只有通过
    完整性和身份质量门的快照才可进入相应 `publication`。
    """

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
    """保存持股快照内证券项，不按后续公司行动回写原始来源持股事实。

    数量、市值、占比、币种与单位表达来源在快照时刻看到的持股，不是经过分红、拆并股或当前价格
    回算后的持仓。证券身份解析必须精确；无法解析的项目应隔离或保留来源标识，不可用代码猜测
    并入其他市场。后续更正通过新快照与来源批次表达，旧快照保持不可变。
    """

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
