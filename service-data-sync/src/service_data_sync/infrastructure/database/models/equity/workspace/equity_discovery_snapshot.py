"""股票发现页面冻结 EOD 横截面主行模型。"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Numeric,
    String,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ...base import Base


class EquityDiscoverySnapshot(Base):
    """保存单一 `dataVersion` 内可稳定筛选排序的 A 股 EOD 投影。

    本表仅由已发布 canonical 组件重建，不接触 Provider。价格为未复权收盘价，
    金额为人民币元，股本为股，百分比字段使用一比一小数。
    """

    __tablename__ = "equity_discovery_snapshot"
    __table_args__ = (
        CheckConstraint("exchange IN ('SSE', 'SZSE', 'BSE')", name="ck_equity_discovery_exchange"),
        CheckConstraint(
            "lifecycle_status IN ('LISTED', 'SUSPENDED', 'DELISTED')",
            name="ck_equity_discovery_lifecycle",
        ),
        CheckConstraint(
            "trading_status IN "
            "('TRADED', 'TRADE_SUSPENDED', 'NO_SESSION', 'NOT_APPLICABLE', 'UNKNOWN')",
            name="ck_equity_discovery_trading_status",
        ),
        Index("ix_equity_discovery_symbol", "release_id", "symbol", "exchange", "security_id"),
        Index("ix_equity_discovery_name", "release_id", "name", "exchange", "symbol"),
        Index("ix_equity_discovery_close", "release_id", "close_price", "exchange", "symbol"),
        Index(
            "ix_equity_discovery_change",
            "release_id",
            "change_percent",
            "exchange",
            "symbol",
        ),
        Index("ix_equity_discovery_amount", "release_id", "amount_cny", "exchange", "symbol"),
        Index(
            "ix_equity_discovery_turnover",
            "release_id",
            "turnover_rate",
            "exchange",
            "symbol",
        ),
        Index(
            "ix_equity_discovery_total_cap",
            "release_id",
            "total_market_cap_cny",
            "exchange",
            "symbol",
        ),
        Index(
            "ix_equity_discovery_float_cap",
            "release_id",
            "float_market_cap_cny",
            "exchange",
            "symbol",
        ),
        Index("ix_equity_discovery_pe", "release_id", "pe_ttm", "exchange", "symbol"),
        Index("ix_equity_discovery_pb", "release_id", "pb", "exchange", "symbol"),
        Index(
            "ix_equity_discovery_money_flow",
            "release_id",
            "money_flow_net_amount_cny",
            "exchange",
            "symbol",
        ),
        {"comment": "股票中心统一列表冻结 EOD 投影；全部字段绑定同一组件清单。"},
    )

    release_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("dataset_release.release_id", ondelete="RESTRICT"),
        primary_key=True,
        nullable=False,
        comment="冻结横截面所属不可变发布；消费者 dataVersion 由 publication 关联取得。",
    )
    security_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("equity_instrument.security_id", ondelete="RESTRICT"),
        primary_key=True,
        nullable=False,
        comment="横截面行所属永久证券内部键。",
    )
    exchange: Mapped[str] = mapped_column(String(8), nullable=False, comment="公开交易所代码。")
    symbol: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="该发布切片内的公开证券代码。"
    )
    name: Mapped[str] = mapped_column(
        String(200), nullable=False, comment="该发布切片内的证券名称。"
    )
    lifecycle_status: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="上市、暂停上市或退市生命周期状态。"
    )
    trading_status: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="普通交易状态；不得与暂停上市混名。"
    )
    trading_status_reason: Mapped[str | None] = mapped_column(
        String(500), nullable=True, comment="来源披露的普通停复牌原因。"
    )
    listed_on: Mapped[date | None] = mapped_column(
        Date, nullable=True, comment="已发布生命周期中的上市日期。"
    )
    delisted_on: Mapped[date | None] = mapped_column(
        Date, nullable=True, comment="已发布生命周期中的退市日期。"
    )
    trade_date: Mapped[date | None] = mapped_column(
        Date, nullable=True, comment="本行行情对应的 Asia/Shanghai 交易日。"
    )
    close_price: Mapped[Decimal | None] = mapped_column(
        Numeric(20, 6), nullable=True, comment="目标日未复权收盘价。"
    )
    previous_close_price: Mapped[Decimal | None] = mapped_column(
        Numeric(20, 6), nullable=True, comment="前一可比实际交易记录的未复权收盘价。"
    )
    change_amount: Mapped[Decimal | None] = mapped_column(
        Numeric(20, 6), nullable=True, comment="收盘价相对前一可比交易记录的变化额。"
    )
    change_percent: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 10), nullable=True, comment="收盘价变化比例，一比一小数。"
    )
    volume_shares: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="目标日成交量，单位为股。"
    )
    amount_cny: Mapped[Decimal | None] = mapped_column(
        Numeric(24, 4), nullable=True, comment="目标日成交额，单位为人民币元。"
    )
    turnover_rate: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 10), nullable=True, comment="来源报告换手率，一比一小数。"
    )
    capital_effective_on: Mapped[date | None] = mapped_column(
        Date, nullable=True, comment="用于市值计算的股本生效日。"
    )
    total_shares: Mapped[Decimal | None] = mapped_column(
        Numeric(24, 0), nullable=True, comment="用于市值计算的总股本，单位为股。"
    )
    listed_tradable_a_shares: Mapped[Decimal | None] = mapped_column(
        Numeric(24, 0), nullable=True, comment="用于流通市值计算的已上市流通 A 股。"
    )
    total_market_cap_cny: Mapped[Decimal | None] = mapped_column(
        Numeric(30, 4), nullable=True, comment="未复权收盘价乘总股本，单位人民币元。"
    )
    float_market_cap_cny: Mapped[Decimal | None] = mapped_column(
        Numeric(30, 4), nullable=True, comment="未复权收盘价乘已上市流通 A 股。"
    )
    valuation_date: Mapped[date | None] = mapped_column(
        Date, nullable=True, comment="估值来源观察交易日。"
    )
    pe_ttm: Mapped[Decimal | None] = mapped_column(
        Numeric(24, 10), nullable=True, comment="来源报告滚动市盈率；亏损时可不适用。"
    )
    pb: Mapped[Decimal | None] = mapped_column(
        Numeric(24, 10), nullable=True, comment="来源报告市净率。"
    )
    ps_ttm: Mapped[Decimal | None] = mapped_column(
        Numeric(24, 10), nullable=True, comment="来源报告滚动市销率。"
    )
    valuation_source_label: Mapped[str | None] = mapped_column(
        String(120), nullable=True, comment="可公开展示的估值来源标签。"
    )
    valuation_methodology_code: Mapped[str | None] = mapped_column(
        String(120), nullable=True, comment="估值方法学稳定代码。"
    )
    valuation_methodology_version: Mapped[str | None] = mapped_column(
        String(32), nullable=True, comment="估值方法学版本。"
    )
    money_flow_date: Mapped[date | None] = mapped_column(
        Date, nullable=True, comment="日频资金流观测交易日。"
    )
    money_flow_net_amount_cny: Mapped[Decimal | None] = mapped_column(
        Numeric(30, 4), nullable=True, comment="供应商 order-size 净额，单位人民币元。"
    )
    money_flow_net_ratio: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 10), nullable=True, comment="供应商报告净额比例，一比一小数。"
    )
    money_flow_source_label: Mapped[str | None] = mapped_column(
        String(120), nullable=True, comment="可公开展示的资金流来源标签。"
    )
    money_flow_methodology_code: Mapped[str | None] = mapped_column(
        String(120), nullable=True, comment="供应商资金流方法学稳定代码。"
    )
    money_flow_methodology_version: Mapped[str | None] = mapped_column(
        String(32), nullable=True, comment="供应商资金流方法学版本。"
    )
