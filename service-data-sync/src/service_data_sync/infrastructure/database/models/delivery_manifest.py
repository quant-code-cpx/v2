"""定义数据运维来源交付清单、不可变分页与来源覆盖边界锁模型。"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class DataOperationDeliveryManifest(Base):
    """保存一次完整预检的不可变 header、根摘要、容量和可用窗口。"""

    __tablename__ = "data_operation_delivery_manifest"
    __table_args__ = (
        CheckConstraint(
            "schema_version = 'quant-v2.delivery-manifest.v1'",
            name="ck_data_operation_delivery_manifest_schema",
        ),
        CheckConstraint(
            "length(request_hash) = 64",
            name="ck_data_operation_delivery_manifest_request_hash",
        ),
        CheckConstraint(
            "length(root_hash) = 64",
            name="ck_data_operation_delivery_manifest_root_hash",
        ),
        CheckConstraint(
            "status IN ('ELIGIBLE','REJECTED')",
            name="ck_data_operation_delivery_manifest_status",
        ),
        CheckConstraint(
            "minimum_remaining_seconds >= 0",
            name="ck_data_operation_delivery_manifest_remaining",
        ),
        CheckConstraint(
            "available_until >= created_at + minimum_remaining_seconds * INTERVAL '1 second'",
            name="ck_data_operation_delivery_manifest_availability",
        ),
        CheckConstraint(
            "(status = 'ELIGIBLE' AND target_count > 0 AND page_count > 0) OR "
            "(status = 'REJECTED' AND target_count = 0 AND page_count = 0)",
            name="ck_data_operation_delivery_manifest_counts",
        ),
        Index(
            "ix_data_operation_delivery_manifest_request",
            "request_hash",
            "created_at",
        ),
        {
            "comment": (
                "一次预检最终冻结的来源交付清单 header；任何更新或删除均由数据库触发器拒绝。"
            )
        },
    )

    manifest_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, nullable=False, comment="不可变清单 UUID。"
    )
    schema_version: Mapped[str] = mapped_column(
        String(80), nullable=False, comment="根摘要和分页正文使用的固定合同版本。"
    )
    dataset_code: Mapped[str] = mapped_column(
        String(160), nullable=False, comment="本清单唯一对应的 canonical 数据集。"
    )
    provider_id: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="产生并复核交付证据的冻结来源标识。"
    )
    request_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="规范预检请求的 SHA-256。"
    )
    root_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="header 与有序页面摘要共同生成的 SHA-256。"
    )
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, comment="最终可执行或拒绝状态，不允许后续改写。"
    )
    target_count: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="全部页面业务目标总数。"
    )
    page_count: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="从零连续编号的页面总数。"
    )
    available_until: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="该清单允许新消费的绝对截止时间。"
    )
    minimum_remaining_seconds: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="新 command 受理时必须保留的最小可用窗口秒数。",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="完整预检结束并冻结清单的时间。"
    )


class DataOperationDeliveryManifestPage(Base):
    """保存一个不可拆交易日的交付证据页及独立完整性摘要。"""

    __tablename__ = "data_operation_delivery_manifest_page"
    __table_args__ = (
        CheckConstraint(
            "page_no >= 0",
            name="ck_data_operation_delivery_manifest_page_no",
        ),
        CheckConstraint(
            "date_from <= date_to",
            name="ck_data_operation_delivery_manifest_page_dates",
        ),
        CheckConstraint(
            "trade_date_count BETWEEN 1 AND 20",
            name="ck_data_operation_delivery_manifest_page_dates_count",
        ),
        CheckConstraint(
            "target_count BETWEEN 1 AND 256",
            name="ck_data_operation_delivery_manifest_page_target_count",
        ),
        CheckConstraint(
            "length(page_hash) = 64",
            name="ck_data_operation_delivery_manifest_page_hash",
        ),
        Index(
            "ix_data_operation_delivery_manifest_page_window",
            "manifest_id",
            "date_from",
            "date_to",
        ),
        {"comment": ("不可变交付证据页；每页最多二十个完整交易日和二百五十六个业务目标。")},
    )

    manifest_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("data_operation_delivery_manifest.manifest_id", ondelete="RESTRICT"),
        primary_key=True,
        nullable=False,
        comment="所属不可变清单 UUID。",
    )
    page_no: Mapped[int] = mapped_column(
        Integer, primary_key=True, nullable=False, comment="从零开始的连续页面序号。"
    )
    date_from: Mapped[date] = mapped_column(Date, nullable=False, comment="本页第一个完整交易日。")
    date_to: Mapped[date] = mapped_column(Date, nullable=False, comment="本页最后一个完整交易日。")
    trade_date_count: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="本页不重复交易日数量，最大二十。"
    )
    target_count: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="本页业务目标数量，最大二百五十六。"
    )
    page_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="页面边界、计数和正文共同生成的 SHA-256。"
    )
    evidence_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        comment="不含供应商正文的路径、版本、摘要和业务目标证据。",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="页面与 header 同事务冻结的时间。"
    )


class StockConnectStatusCoverageBoundaryLock(Base):
    """持久化状态来源开始强制覆盖的单向收紧边界。

    首次可信 preflight 会锁定一个 `required_from`；后续部署只允许把边界前移以补齐更多历史，
    不能通过修改环境变量或清单把它后移。数据库触发器同时禁止删除和非收紧更新。
    """

    __tablename__ = "stock_connect_status_coverage_boundary_lock"
    __table_args__ = (
        CheckConstraint(
            "length(first_manifest_sha256) = 64 AND length(current_manifest_sha256) = 64",
            name="ck_stock_connect_status_boundary_hashes",
        ),
        CheckConstraint(
            "first_locked_at <= tightened_at",
            name="ck_stock_connect_status_boundary_times",
        ),
        {
            "comment": (
                "互联互通状态 coverage requiredFrom 的不可后移持久化锁；"
                "不同 scope 可由同一门禁实现独立测试与演进。"
            )
        },
    )

    scope_key: Mapped[str] = mapped_column(
        String(160),
        primary_key=True,
        nullable=False,
        comment="稳定边界作用域；生产固定为互联互通通道状态数据集。",
    )
    required_from: Mapped[date] = mapped_column(
        Date,
        nullable=False,
        comment="自该市场日期起缺少最终状态即失败关闭；只允许前移。",
    )
    first_manifest_sha256: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="首次锁定边界时状态 coverage 清单原始字节 SHA-256。",
    )
    current_manifest_sha256: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="最近一次收紧边界时状态 coverage 清单原始字节 SHA-256。",
    )
    first_locked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment="首次成功锁定该边界的带时区时间。",
    )
    tightened_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment="最近一次把边界前移的带时区时间；未收紧时等于首次锁定时间。",
    )
