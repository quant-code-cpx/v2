"""完整板块 `EOD` 横截面内单板块来源报价、单位和可用性模型。"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Numeric, String
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ...base import Base


class SectorEodQuote(Base):
    """保存完整横截面中一个板块的观察值；供应商排名不进入 `canonical` 字段。

    价格、涨跌、成交量、成交额、市值和换手率必须按该快照的来源单位和业务交易日保存；行与快照
    共同才代表完整横截面，不能脱离头表单独发布。来源展示的排名、广告指标或临时附加列不应混入
    稳定字段；单位未知、数值异常或身份冲突应由质量门阻断而不是填零。
    """

    __tablename__ = "sector_eod_quote"
    __table_args__ = (
        CheckConstraint("BTRIM(sector_name) <> ''", name="ck_sector_eod_quote_sector_name"),
        CheckConstraint("latest_value IS NULL OR latest_value >= 0"),
        CheckConstraint("latest_value_unit = 'provider_native'"),
        CheckConstraint("market_value IS NULL OR market_value >= 0"),
        CheckConstraint("market_value_unit = 'provider_native'"),
        CheckConstraint("turnover_percent IS NULL OR turnover_percent >= 0"),
        CheckConstraint("advancers IS NULL OR advancers >= 0"),
        CheckConstraint("decliners IS NULL OR decliners >= 0"),
        {"comment": "EOD 快照内板块报价；数值保留来源原生单位与观察时名称。"},
    )

    snapshot_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("sector_eod_snapshot.snapshot_id", ondelete="RESTRICT"),
        primary_key=True,
        nullable=False,
        comment="所属 EOD 完整横截面快照。",
    )
    sector_key: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("sector_entity.sector_key"),
        primary_key=True,
        nullable=False,
        comment="报价所属内部板块键。",
    )
    sector_name: Mapped[str] = mapped_column(
        String(200), nullable=False, comment="本次观察时来源返回的板块名称。"
    )
    latest_value: Mapped[Decimal | None] = mapped_column(
        Numeric(24, 6), nullable=True, comment="来源原生口径最新值；非货币声明。"
    )
    latest_value_unit: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="最新值单位，固定 provider_native。"
    )
    change_value: Mapped[Decimal | None] = mapped_column(
        Numeric(24, 6), nullable=True, comment="来源原生口径涨跌额。"
    )
    change_percent: Mapped[Decimal | None] = mapped_column(
        Numeric(16, 10), nullable=True, comment="涨跌幅，单位为百分比。"
    )
    market_value: Mapped[Decimal | None] = mapped_column(
        Numeric(30, 4), nullable=True, comment="来源原生口径总市值，币种和缩放未假定。"
    )
    market_value_unit: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="总市值单位，固定 provider_native。"
    )
    turnover_percent: Mapped[Decimal | None] = mapped_column(
        Numeric(16, 10), nullable=True, comment="换手率，单位为百分比。"
    )
    advancers: Mapped[int | None] = mapped_column(
        nullable=True, comment="上涨家数；来源缺失时为空。"
    )
    decliners: Mapped[int | None] = mapped_column(
        nullable=True, comment="下跌家数；来源缺失时为空。"
    )
    leader_name: Mapped[str | None] = mapped_column(
        String(200), nullable=True, comment="来源返回的领涨证券名称，不绑定证券外键。"
    )
    leader_change_percent: Mapped[Decimal | None] = mapped_column(
        Numeric(16, 10), nullable=True, comment="领涨证券涨跌幅，单位为百分比。"
    )
    row_sha256: Mapped[bytes] = mapped_column(nullable=False, comment="报价行规范化内容稳定摘要。")
