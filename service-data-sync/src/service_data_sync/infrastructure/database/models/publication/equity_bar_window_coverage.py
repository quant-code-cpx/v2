"""个股日、周、月行情成功同步窗口的不可变覆盖证据模型。"""

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


class EquityBarWindowCoverage(Base):
    """证明一个真实来源批次完整检查了某证券的行情日期闭区间。

    非空响应关联既有 `DATA` publication；合法空响应关联质量状态为 `passed` 的零记录
    publication。两种结果都冻结证券身份版本、请求窗口和来源批次，供应商不可用或请求失败
    不得写入本表。相同来源观察重放复用已有覆盖版本，后续真实观察只结束旧 current 行，
    不改写其不可变业务字段。
    """

    __tablename__ = "equity_bar_window_coverage"
    __table_args__ = (
        CheckConstraint(
            "period IN ('1d', '1w', '1mo')",
            name="ck_equity_bar_coverage_period",
        ),
        CheckConstraint(
            """
            (period = '1d' AND capability = 'equity.bar.1d.raw')
            OR (period = '1w' AND capability = 'equity.bar.1w.raw')
            OR (period = '1mo' AND capability = 'equity.bar.1mo.raw')
            """,
            name="ck_equity_bar_coverage_capability_period",
        ),
        CheckConstraint(
            "coverage_from <= coverage_to",
            name="ck_equity_bar_coverage_window",
        ),
        CheckConstraint(
            """
            (publication_kind = 'DATA' AND record_count > 0)
            OR (publication_kind = 'ZERO_RECORD_COVERAGE' AND record_count = 0)
            """,
            name="ck_equity_bar_coverage_publication_kind",
        ),
        CheckConstraint(
            "quality_status = 'passed'",
            name="ck_equity_bar_coverage_quality",
        ),
        CheckConstraint(
            "identity_hash ~ '^[0-9a-f]{64}$' "
            "AND universe_hash ~ '^[0-9a-f]{64}$' "
            "AND universe_size = 1",
            name="ck_equity_bar_coverage_identity",
        ),
        CheckConstraint(
            "superseded_at IS NULL OR superseded_at >= created_at",
            name="ck_equity_bar_coverage_superseded",
        ),
        UniqueConstraint(
            "capability",
            "security_id",
            "coverage_from",
            "coverage_to",
            "observed_at",
            name="uq_equity_bar_coverage_observation",
        ),
        Index(
            "uq_equity_bar_coverage_current",
            "capability",
            "security_id",
            "coverage_from",
            "coverage_to",
            unique=True,
            postgresql_where="superseded_at IS NULL",
        ),
        Index(
            "ix_equity_bar_coverage_read",
            "capability",
            "security_id",
            "coverage_from",
            "coverage_to",
            "created_at",
        ),
        Index("ix_equity_bar_coverage_source_batch", "source_batch_id"),
        {"comment": "个股三周期行情成功同步的逐证券窗口覆盖与零记录 publication 证据。"},
    )

    coverage_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, nullable=False, comment="覆盖观察永久 UUID。"
    )
    coverage_version: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        unique=True,
        nullable=False,
        comment="进入回填结果清单的不可变覆盖版本。",
    )
    period: Mapped[str] = mapped_column(
        String(8), nullable=False, comment="上游独立返回的日、周或月周期。"
    )
    capability: Mapped[str] = mapped_column(
        String(100), nullable=False, comment="与周期严格对应的 provider-neutral capability。"
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
        comment="请求窗口采用的已确认交易所代码身份版本。",
    )
    coverage_from: Mapped[date] = mapped_column(
        Date, nullable=False, comment="来源成功检查的包含式起始行情日期。"
    )
    coverage_to: Mapped[date] = mapped_column(
        Date, nullable=False, comment="来源成功检查的包含式结束行情日期。"
    )
    publication_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("dataset_publication.publication_id", ondelete="RESTRICT"),
        nullable=False,
        comment="真实数据或零记录覆盖对应的 immutable canonical publication。",
    )
    data_version: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("dataset_publication.data_version", ondelete="RESTRICT"),
        nullable=False,
        comment="与 publication_id 严格配对的消费者不可变数据版本。",
    )
    source_batch_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("source_batch.source_batch_id", ondelete="RESTRICT"),
        nullable=False,
        comment="产生本覆盖结论的精确真实来源观察。",
    )
    publication_kind: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="非空事实发布或通过质量门的零记录覆盖发布。",
    )
    quality_status: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="固定为 `passed`，失败来源不得形成覆盖。"
    )
    record_count: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="本次来源响应在请求窗口内返回的标准行情条数。"
    )
    identity_hash: Mapped[str] = mapped_column(
        CHAR(64), nullable=False, comment="证券与代码身份版本、不含窗口的稳定 SHA-256。"
    )
    universe_hash: Mapped[str] = mapped_column(
        CHAR(64), nullable=False, comment="证券身份版本和本次闭区间组成的稳定 SHA-256。"
    )
    universe_size: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="单证券行情窗口固定为一个身份分段。"
    )
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="来源响应被实际观察到的时间。"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment="覆盖完成质量门并进入数据库知识时间轴的时刻。",
    )
    superseded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="相同精确窗口被更新观察替代的时间；当前结论为空。",
    )
