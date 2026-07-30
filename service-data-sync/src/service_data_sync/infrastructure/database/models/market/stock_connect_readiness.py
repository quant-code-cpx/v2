"""保存互联互通官方日历预检与执行就绪度的版本化证据。"""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base


class StockConnectReadinessSnapshot(Base):
    """记录一次在远端探针之前开始、并以稳定终态结束的 readiness 尝试。"""

    __tablename__ = "stock_connect_readiness_snapshot"
    __table_args__ = (
        CheckConstraint(
            "schema_version = 'quant-v2.stock-connect-readiness-snapshot.v1'",
            name="ck_stock_connect_readiness_snapshot_schema",
        ),
        CheckConstraint(
            "length(request_hash) = 64",
            name="ck_stock_connect_readiness_snapshot_request_hash",
        ),
        CheckConstraint(
            "status IN ('PROBING','PENDING','FAILED','SOURCE_MISSING')",
            name="ck_stock_connect_readiness_snapshot_status",
        ),
        CheckConstraint(
            "reason_code IN ("
            "'PREFLIGHT_PENDING','PREFLIGHT_FAILED','CALENDAR_SOURCE_MISSING',"
            "'DELIVERY_ENTITLEMENT_MISSING','DELIVERY_OBJECT_MISSING',"
            "'STATUS_SOURCE_MISSING','COMMAND_NOT_SUBMITTED')",
            name="ck_stock_connect_readiness_snapshot_reason",
        ),
        CheckConstraint(
            "(status = 'PROBING' AND completed_at IS NULL "
            "AND manifest_id IS NULL AND calendar_data_version IS NULL) OR "
            "(status <> 'PROBING' AND completed_at IS NOT NULL "
            "AND calendar_data_version IS NOT NULL)",
            name="ck_stock_connect_readiness_snapshot_completion",
        ),
        CheckConstraint(
            "calendar_data_version IS NULL OR length(calendar_data_version) = 64",
            name="ck_stock_connect_readiness_snapshot_calendar_version",
        ),
        CheckConstraint(
            "calendar_manifest_sha256 IS NULL OR length(calendar_manifest_sha256) = 64",
            name="ck_stock_connect_readiness_snapshot_calendar_manifest",
        ),
        CheckConstraint(
            "completed_at IS NULL OR completed_at >= created_at",
            name="ck_stock_connect_readiness_snapshot_times",
        ),
        Index(
            "ix_stock_connect_readiness_snapshot_selection",
            "selected_channel_set",
            "completed_at",
        ),
        {"comment": ("互联互通预检 readiness 尝试；先提交 PROBING，再只允许一次进入持久化终态。")},
    )

    snapshot_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        nullable=False,
        comment="readiness 尝试 UUID。",
    )
    schema_version: Mapped[str] = mapped_column(
        String(80),
        nullable=False,
        comment="持久化 readiness snapshot 合同版本。",
    )
    request_hash: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="规范化同步目标的 SHA-256。",
    )
    selected_channel_set: Mapped[str] = mapped_column(
        String(160),
        nullable=False,
        comment="按公开通道代码排序并用逗号连接的请求通道全集。",
    )
    status: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        comment="探针进行中、待执行、失败或来源缺失状态。",
    )
    reason_code: Mapped[str] = mapped_column(
        String(48),
        nullable=False,
        comment="不含供应商路径、凭据或原文的稳定低基数原因。",
    )
    manifest_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("data_operation_delivery_manifest.manifest_id", ondelete="RESTRICT"),
        nullable=True,
        comment="成功预检绑定的不可变 delivery manifest；失败尝试为空。",
    )
    calendar_data_version: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="本次日历证据全集规范化后的 SHA-256。",
    )
    calendar_manifest_sha256: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="适配器启动时已严格校验的年度日历目录摘要。",
    )
    failure_detail: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="仅保存固定安全说明，不保存供应商异常、路径或正文。",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment="远端探针开始前持久化 PROBING 的时间。",
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="探针与 manifest 持久化形成明确结论的时间。",
    )


class StockConnectReadinessCalendarDay(Base):
    """冻结 snapshot 内一个日期和通道方向的官方日历判定及 publication 语义。"""

    __tablename__ = "stock_connect_readiness_calendar_day"
    __table_args__ = (
        CheckConstraint(
            "channel IN ('SH','SZ')",
            name="ck_stock_connect_readiness_calendar_day_channel",
        ),
        CheckConstraint(
            "direction IN ('NORTHBOUND','SOUTHBOUND')",
            name="ck_stock_connect_readiness_calendar_day_direction",
        ),
        CheckConstraint(
            "calendar_state IN ('OPEN','CLOSED','UNKNOWN')",
            name="ck_stock_connect_readiness_calendar_day_state",
        ),
        CheckConstraint(
            "publication_availability IN ('REPORTED','NOT_REPORTED','SOURCE_MISSING')",
            name="ck_stock_connect_readiness_calendar_day_publication",
        ),
        CheckConstraint(
            "(publication_availability = 'SOURCE_MISSING' "
            "AND calendar_state = 'UNKNOWN' AND source_file_sha256 IS NULL "
            "AND source_publication_at IS NULL AND source_observed_at IS NULL) OR "
            "(publication_availability <> 'SOURCE_MISSING' "
            "AND calendar_state <> 'UNKNOWN' AND length(source_file_sha256) = 64 "
            "AND source_observed_at IS NOT NULL "
            "AND ((publication_availability = 'REPORTED' "
            "AND source_publication_at IS NOT NULL) OR "
            "(publication_availability = 'NOT_REPORTED' "
            "AND source_publication_at IS NULL)))",
            name="ck_stock_connect_readiness_calendar_day_source",
        ),
        Index(
            "ix_stock_connect_readiness_calendar_day_lookup",
            "calendar_date",
            "channel",
            "direction",
        ),
        {"comment": ("由摘要钉住的官方年度日历生成；UNKNOWN 只表达来源缺失，不能猜测休市。")},
    )

    snapshot_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("stock_connect_readiness_snapshot.snapshot_id", ondelete="RESTRICT"),
        primary_key=True,
        nullable=False,
        comment="所属 readiness snapshot。",
    )
    calendar_date: Mapped[date] = mapped_column(
        Date,
        primary_key=True,
        nullable=False,
        comment="官方日历中的公历日期，不是抓取日期。",
    )
    channel: Mapped[str] = mapped_column(
        String(8),
        primary_key=True,
        nullable=False,
        comment="互联互通路由通道 SH 或 SZ。",
    )
    direction: Mapped[str] = mapped_column(
        String(16),
        primary_key=True,
        nullable=False,
        comment="北向或南向业务方向。",
    )
    calendar_state: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment="该通道方向在该日明确 OPEN、CLOSED 或来源 UNKNOWN。",
    )
    source_file_sha256: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="产生该判断的官方年度日历原始文件 SHA-256；缺源时为空。",
    )
    source_publication_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="来源明确提供的日历 publication 时间；不得用 observedAt 回填。",
    )
    publication_availability: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        comment="说明 publication 时间是已报告、未报告或来源整体缺失。",
    )
    source_observed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="年度日历目录中冻结的来源观察时间；缺源时为空。",
    )
    evidence_observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment="本次探针形成该 readiness 证据的时间，不具有 publication 语义。",
    )
