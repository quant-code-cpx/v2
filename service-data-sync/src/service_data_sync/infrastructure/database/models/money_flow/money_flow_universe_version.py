"""资金流 `universe` 的业务有效范围、成员血缘和版本化身份模型。"""

from __future__ import annotations

from datetime import date
from uuid import UUID

from sqlalchemy import CHAR, CheckConstraint, Date, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base


class MoneyFlowUniverseVersion(Base):
    """保存来源样本池的版本及其与身份目录或成分发布的血缘。

    样本池决定一条资金流值覆盖哪些证券、板块或市场对象，必须按业务有效区间版本化；它可关联已发布
    身份目录或成分 `release`，来源未说明成员时也允许明确标记未知。未知 `universe` 不能被伪造成
    当前全市场名单，成员数量和内容摘要只是可验证证据，不能替代精确成员记录。
    """

    __tablename__ = "money_flow_universe_version"
    __table_args__ = (
        UniqueConstraint(
            "universe_code",
            "scope_type",
            "effective_from",
            name="uq_money_flow_universe_version",
        ),
        CheckConstraint(
            "scope_type IN ('equity', 'sector', 'market')",
            name="ck_money_flow_universe_version_scope_type",
        ),
        CheckConstraint(
            "effective_to IS NULL OR effective_from < effective_to",
            name="ck_money_flow_universe_version_effective_range",
        ),
        CheckConstraint(
            "content_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_money_flow_universe_version_hash",
        ),
        {"comment": "资金流样本池版本；未知供应商 universe 不伪造成员明细。"},
    )

    universe_version_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, nullable=False, comment="universe 版本永久 UUID。"
    )
    universe_code: Mapped[str] = mapped_column(
        String(100), nullable=False, comment="方法学声明的 universe 稳定代码。"
    )
    scope_type: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="universe 所覆盖的 scope 类型。"
    )
    identity_data_version: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("dataset_publication.data_version"),
        nullable=True,
        comment="证券身份目录的发布版本；来源未知样本池可为空。",
    )
    membership_release_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        nullable=True,
        comment="板块成分 release 身份；不适用或来源未知时为空。",
    )
    effective_from: Mapped[date] = mapped_column(
        Date, nullable=False, comment="universe 版本业务有效起点。"
    )
    effective_to: Mapped[date | None] = mapped_column(
        Date, nullable=True, comment="业务有效半开区间终点。"
    )
    member_count: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="可验证成员数量；来源未知时为空。"
    )
    content_sha256: Mapped[str] = mapped_column(
        CHAR(64), nullable=False, comment="成员身份或 unknown 描述的稳定内容哈希。"
    )
