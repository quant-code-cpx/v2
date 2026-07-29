"""衍生品真实合约规格与交易所日行情的不可变 revision 模型。"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    CHAR,
    CheckConstraint,
    Computed,
    Date,
    ForeignKey,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    desc,
)
from sqlalchemy.dialects.postgresql import DATERANGE, TSTZRANGE, ExcludeConstraint, Range
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base
from .revision_mixin import CanonicalRevisionMixin


class DerivativeContractRevision(CanonicalRevisionMixin, Base):
    """保存真实合约规格的双时间版本，后续更正绝不覆盖已知条款。"""

    __tablename__ = "derivative_contract_revision"
    __table_args__ = (
        CheckConstraint("revision_no > 0", name="ck_derivative_contract_revision_no"),
        CheckConstraint(
            "effective_to IS NULL OR effective_to > effective_from",
            name="ck_derivative_contract_revision_effective_range",
        ),
        CheckConstraint(
            "known_to IS NULL OR known_to > known_from",
            name="ck_derivative_contract_revision_knowledge_range",
        ),
        CheckConstraint(
            "tick_size > 0 AND contract_multiplier > 0",
            name="ck_derivative_contract_revision_positive_spec",
        ),
        ExcludeConstraint(
            ("contract_id", "="),
            ("effective_range", "&&"),
            ("knowledge_range", "&&"),
            using="gist",
            name="ex_derivative_contract_revision_time",
        ),
        Index(
            "ix_derivative_contract_revision_asof",
            "contract_id",
            desc("effective_from"),
            desc("known_from"),
        ),
        {"comment": "真实衍生品合约规格双时间版本；连续代码和平台换月规则绝不进入此表。"},
    )

    contract_revision_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, nullable=False, comment="合约规格 revision UUID。"
    )
    contract_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("derivative_contract.instrument_id", ondelete="RESTRICT"),
        nullable=False,
        comment="真实可交易衍生品合约 UUID。",
    )
    effective_from: Mapped[date] = mapped_column(
        Date, nullable=False, comment="此规格开始适用的交易业务日期。"
    )
    effective_to: Mapped[date | None] = mapped_column(
        Date, nullable=True, comment="此规格停止适用的业务日期；开区间为空。"
    )
    last_trade_date: Mapped[date | None] = mapped_column(
        Date, nullable=True, comment="来源确认的最后交易日期；未知时为空。"
    )
    exercise_date: Mapped[date | None] = mapped_column(
        Date, nullable=True, comment="期权行权日期；非期权或未知时为空。"
    )
    delivery_start_date: Mapped[date | None] = mapped_column(
        Date, nullable=True, comment="实物交割开始日期；不适用或未知时为空。"
    )
    delivery_end_date: Mapped[date | None] = mapped_column(
        Date, nullable=True, comment="实物交割结束日期；不适用或未知时为空。"
    )
    contract_multiplier: Mapped[Decimal] = mapped_column(
        Numeric(28, 8), nullable=False, comment="每手或每张合约对应标的数量。"
    )
    tick_size: Mapped[Decimal] = mapped_column(
        Numeric(24, 8), nullable=False, comment="报价最小变动单位。"
    )
    quote_unit: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="价格报价单位，例如元每吨或指数点。"
    )
    volume_unit: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="成交量单位，必须显式为手、张或来源确认单位。"
    )
    volume_convention: Mapped[str] = mapped_column(
        String(24), nullable=False, comment="成交量单双边或其他来源明确统计口径。"
    )
    settlement_type: Mapped[str] = mapped_column(
        String(24), nullable=False, comment="现金、实物或来源未知的结算方式。"
    )
    session_code: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="关联场所交易时段规则代码；未知时为空。"
    )
    specification_reference: Mapped[str | None] = mapped_column(
        String(300), nullable=True, comment="官方合约规则或调整公告定位引用。"
    )
    effective_range: Mapped[Range[date] | None] = mapped_column(
        DATERANGE,
        Computed("daterange(effective_from, effective_to, '[)')", persisted=True),
        nullable=True,
        comment="规格有效业务日期半开范围。",
    )
    knowledge_range: Mapped[Range[datetime] | None] = mapped_column(
        TSTZRANGE,
        Computed("tstzrange(known_from, known_to, '[)')", persisted=True),
        nullable=True,
        comment="系统采用本规格版本的知识时间半开范围。",
    )


class DerivativeDailyBarRevision(CanonicalRevisionMixin, Base):
    """保存交易所真实合约日行情 revision，结算价与收盘价保持独立字段。"""

    __tablename__ = "derivative_daily_bar_revision"
    __table_args__ = (
        CheckConstraint("revision_no > 0", name="ck_derivative_daily_bar_revision_no"),
        CheckConstraint(
            "open_price >= 0 AND high_price >= 0 AND low_price >= 0 AND close_price >= 0",
            name="ck_derivative_daily_bar_prices",
        ),
        CheckConstraint(
            "low_price <= LEAST(open_price, close_price) "
            "AND high_price >= GREATEST(open_price, close_price)",
            name="ck_derivative_daily_bar_ohlc",
        ),
        CheckConstraint(
            "volume_value >= 0 AND open_interest_value >= 0",
            name="ck_derivative_daily_bar_non_negative_position",
        ),
        CheckConstraint(
            "turnover_value IS NULL OR turnover_value >= 0",
            name="ck_derivative_daily_bar_non_negative_turnover",
        ),
        UniqueConstraint(
            "trade_date",
            "contract_id",
            "methodology_version_id",
            "revision_no",
            name="uq_derivative_daily_bar_revision",
        ),
        Index(
            "ix_derivative_daily_bar_asof",
            "contract_id",
            desc("trade_date"),
            desc("known_from"),
        ),
        {
            "postgresql_partition_by": "RANGE (trade_date)",
            "comment": "真实衍生品合约日行情 revision 父表；不存主力或连续序列派生价格。",
        },
    )

    trade_date: Mapped[date] = mapped_column(
        Date,
        primary_key=True,
        nullable=False,
        comment="交易所归属的业务交易日，夜盘不按自然日推断。",
    )
    row_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, nullable=False, comment="日行情 revision 行 UUID。"
    )
    contract_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("derivative_contract.instrument_id", ondelete="RESTRICT"),
        nullable=False,
        comment="真实衍生品合约 UUID。",
    )
    open_price: Mapped[Decimal] = mapped_column(
        Numeric(24, 8), nullable=False, comment="交易所或经批准来源直接给出的开盘价。"
    )
    high_price: Mapped[Decimal] = mapped_column(
        Numeric(24, 8), nullable=False, comment="交易所或经批准来源直接给出的最高价。"
    )
    low_price: Mapped[Decimal] = mapped_column(
        Numeric(24, 8), nullable=False, comment="交易所或经批准来源直接给出的最低价。"
    )
    close_price: Mapped[Decimal] = mapped_column(
        Numeric(24, 8), nullable=False, comment="交易所或经批准来源直接给出的收盘价。"
    )
    pre_close_price: Mapped[Decimal | None] = mapped_column(
        Numeric(24, 8), nullable=True, comment="来源直报前收盘价；未披露时为空。"
    )
    settlement_price: Mapped[Decimal | None] = mapped_column(
        Numeric(24, 8), nullable=True, comment="当日结算价；不得以收盘价替代。"
    )
    pre_settlement_price: Mapped[Decimal | None] = mapped_column(
        Numeric(24, 8), nullable=True, comment="前结算价；来源未披露时为空。"
    )
    volume_value: Mapped[Decimal] = mapped_column(
        Numeric(28, 8), nullable=False, comment="来源成交量数值，单位由合约规格和本行口径解释。"
    )
    open_interest_value: Mapped[Decimal] = mapped_column(
        Numeric(28, 8), nullable=False, comment="来源持仓量数值，不与成交量混用。"
    )
    turnover_value: Mapped[Decimal | None] = mapped_column(
        Numeric(24, 4), nullable=True, comment="来源成交额；未披露时为空。"
    )
    turnover_currency: Mapped[str | None] = mapped_column(
        CHAR(3), nullable=True, comment="成交额币种 ISO 代码；无成交额时为空。"
    )
    turnover_unit: Mapped[str | None] = mapped_column(
        String(24), nullable=True, comment="成交额来源单位或缩放口径；无成交额时为空。"
    )
    trade_status: Mapped[str | None] = mapped_column(
        String(24), nullable=True, comment="来源明确交易状态；无成交不能据此推断停牌。"
    )
