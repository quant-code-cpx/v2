"""证券事件成功同步窗口的不可变覆盖证据模型。"""

from __future__ import annotations

from datetime import date, datetime
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
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base


class EquityEventWindowCoverage(Base):
    """证明一个来源 publication 已完整检查某证券的一个事件日期闭区间。

    该表不是业务事件事实。`record_count=0` 表示来源成功返回且该 publication 的当前知识
    快照在覆盖窗口内没有对应事件；它仍绑定真实来源批次、方法学 release 和 publication。
    相同精确窗口的新观察会结束旧 current 行，但历史行保留，供 `knownAt` 查询重建当时结论。
    """

    __tablename__ = "equity_event_window_coverage"
    __table_args__ = (
        CheckConstraint(
            "event_family IN "
            "('CORPORATE_ACTION', 'EARNINGS_FORECAST', 'EARNINGS_EXPRESS', "
            "'DRAGON_TIGER', 'BLOCK_TRADE')",
            name="ck_equity_event_coverage_family",
        ),
        CheckConstraint(
            """
            (
              dataset = 'equity.corporate_action'
              AND event_family = 'CORPORATE_ACTION'
            )
            OR (
              dataset = 'equity.corporate_event.earnings.reported'
              AND event_family IN ('EARNINGS_FORECAST', 'EARNINGS_EXPRESS')
            )
            OR (
              dataset = 'equity.dragon_tiger.disclosure.reported'
              AND event_family = 'DRAGON_TIGER'
            )
            OR (
              dataset = 'equity.block_trade.execution.reported'
              AND event_family = 'BLOCK_TRADE'
            )
            """,
            name="ck_equity_event_coverage_dataset_family",
        ),
        CheckConstraint(
            "coverage_from <= coverage_to",
            name="ck_equity_event_coverage_window",
        ),
        CheckConstraint(
            "record_count >= 0",
            name="ck_equity_event_coverage_record_count",
        ),
        CheckConstraint(
            "coverage_scope IN ('INSTRUMENT', 'GLOBAL')",
            name="ck_equity_event_coverage_scope",
        ),
        CheckConstraint(
            "universe_size > 0 AND universe_hash ~ '^[0-9a-f]{64}$'",
            name="ck_equity_event_coverage_universe",
        ),
        CheckConstraint(
            "superseded_at IS NULL OR superseded_at >= created_at",
            name="ck_equity_event_coverage_superseded",
        ),
        UniqueConstraint(
            "dataset",
            "event_family",
            "security_id",
            "coverage_from",
            "coverage_to",
            "observed_at",
            name="uq_equity_event_coverage_observation",
        ),
        Index(
            "uq_equity_event_coverage_current",
            "dataset",
            "event_family",
            "security_id",
            "coverage_from",
            "coverage_to",
            unique=True,
            postgresql_where="superseded_at IS NULL",
        ),
        Index(
            "ix_equity_event_coverage_read",
            "dataset",
            "event_family",
            "security_id",
            "coverage_from",
            "coverage_to",
            "created_at",
        ),
        {"comment": "证券事件成功同步的逐证券窗口覆盖与零记录 publication 证据。"},
    )

    coverage_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, nullable=False, comment="覆盖观察永久 UUID。"
    )
    coverage_version: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        unique=True,
        nullable=False,
        comment="进入复合 dataVersion 与 ETag 的不可变覆盖版本。",
    )
    dataset: Mapped[str] = mapped_column(
        String(100), nullable=False, comment="事件 canonical dataset 稳定代码。"
    )
    event_family: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="该覆盖证明的公开事件族。"
    )
    security_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("equity_instrument.security_id", ondelete="RESTRICT"),
        nullable=False,
        comment="被证明覆盖的永久证券身份。",
    )
    identifier_version_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("equity_identifier_version.version_id", ondelete="RESTRICT"),
        nullable=False,
        comment="来源请求窗口所采用的交易所代码身份版本。",
    )
    coverage_from: Mapped[date] = mapped_column(
        Date, nullable=False, comment="来源成功检查的包含式起始事件日期。"
    )
    coverage_to: Mapped[date] = mapped_column(
        Date, nullable=False, comment="来源成功检查的包含式结束事件日期。"
    )
    publication_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("dataset_publication.publication_id", ondelete="RESTRICT"),
        nullable=False,
        comment="覆盖结论对应的 immutable canonical publication。",
    )
    source_batch_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("source_batch.source_batch_id", ondelete="RESTRICT"),
        nullable=False,
        comment="产生该覆盖结论的真实来源观察。",
    )
    record_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="该 publication 当前知识快照在本证券、本事件族和窗口内的事件数。",
    )
    coverage_scope: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment="来源请求是单证券精确窗口或成功响应后的全市场身份枚举。",
    )
    universe_hash: Mapped[str] = mapped_column(
        CHAR(64),
        nullable=False,
        comment="本次响应实际枚举身份版本和分段窗口清单的稳定 SHA-256。",
    )
    universe_size: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="本次响应实际枚举的身份分段数量；用于审计全市场覆盖完整性。",
    )
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="来源响应被实际观察到的时间。"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment="覆盖结论完成质量门禁并进入数据库知识时间轴的时刻。",
    )
    superseded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="相同精确窗口被更新观察替代的时间；当前结论为空。",
    )
